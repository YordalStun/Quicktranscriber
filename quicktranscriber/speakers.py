"""Speaker library: learned voiceprints, recognition and user-guided learning.

How learning works
------------------
* Each person in the library has *samples*: short clips of their voice plus a
  voice fingerprint (embedding) for each clip.
* When a meeting is processed, every detected speaker gets a fingerprint and is
  compared with everyone in the library:
    - very similar  -> recognised automatically ("auto")
    - fairly similar -> suggested, the user confirms or rejects ("suggested")
    - otherwise      -> stays "Speaker N" until the user names them
* Samples are only added when the user confirms a name (or, optionally, for
  very confident automatic matches), so mistakes do not pile up.
* Users can listen to every sample and delete bad ones; samples that do not
  sound like the rest are flagged as likely mistakes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import audio as audio_mod
from . import catalog, db, paths, settings
from .diarization import EmbeddingExtractor, normalize

log = logging.getLogger("qt.speakers")

PALETTE = [
    "#8b5cf6", "#06b6d4", "#f59e0b", "#ec4899", "#10b981", "#3b82f6", "#ef4444", "#84cc16",
    "#f97316", "#14b8a6", "#a855f7", "#eab308", "#0ea5e9", "#f43f5e", "#22c55e", "#6366f1",
]
MAX_SAMPLES_PER_SPEAKER = 40
SAMPLES_PER_LEARN = 5
SAMPLE_MIN_SECONDS = 2.5
SAMPLE_MAX_SECONDS = 12.0

_extractors: dict[str, EmbeddingExtractor] = {}
_extractor_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def current_model_key() -> str:
    key = settings.get("speaker_model")
    return key if catalog.speaker_model(key) else "titanet_small"


def model_path(key: str) -> Path:
    entry = catalog.speaker_model(key)
    return paths.SPEAKER_MODELS / entry["file"]


def segmentation_path() -> Path:
    return paths.SPEAKER_MODELS / catalog.SEGMENTATION["model_file"]


def models_installed(key: Optional[str] = None) -> bool:
    key = key or current_model_key()
    return model_path(key).exists() and segmentation_path().exists()


def extractor(key: Optional[str] = None) -> EmbeddingExtractor:
    key = key or current_model_key()
    with _extractor_lock:
        if key not in _extractors:
            from .hardware import cpu_threads

            _extractors[key] = EmbeddingExtractor(str(model_path(key)), num_threads=min(4, cpu_threads()))
        return _extractors[key]


def thresholds(key: Optional[str] = None) -> dict[str, float]:
    entry = catalog.speaker_model(key or current_model_key()) or catalog.SPEAKER_EMBEDDINGS[0]
    return {"cluster": entry["threshold"], **entry["match"]}


# ---------------------------------------------------------------------------
# Library queries
# ---------------------------------------------------------------------------


def next_color() -> str:
    used = {r["color"] for r in db.query("SELECT color FROM speakers")}
    for c in PALETTE:
        if c not in used:
            return c
    return PALETTE[len(used) % len(PALETTE)]


def create_speaker(name: str, color: Optional[str] = None) -> int:
    now = time.time()
    cur = db.execute(
        "INSERT INTO speakers (name, color, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name.strip() or "Unnamed", color or next_color(), now, now),
    )
    return int(cur.lastrowid)


def find_by_name(name: str) -> Optional[dict[str, Any]]:
    return db.one("SELECT * FROM speakers WHERE lower(name) = lower(?)", (name.strip(),))


def samples_for(speaker_id: int, model: Optional[str] = None) -> list[dict[str, Any]]:
    if model:
        return db.query(
            "SELECT * FROM speaker_samples WHERE speaker_id = ? AND model = ? ORDER BY created_at",
            (speaker_id, model),
        )
    return db.query("SELECT * FROM speaker_samples WHERE speaker_id = ? ORDER BY created_at", (speaker_id,))


def profiles(model: Optional[str] = None) -> list[dict[str, Any]]:
    """All speakers with at least one sample for ``model``, with their centroid."""
    model = model or current_model_key()
    rows = db.query(
        "SELECT s.id, s.name, s.color, ss.embedding FROM speakers s "
        "JOIN speaker_samples ss ON ss.speaker_id = s.id WHERE ss.model = ?",
        (model,),
    )
    by: dict[int, dict[str, Any]] = {}
    for r in rows:
        p = by.setdefault(r["id"], {"id": r["id"], "name": r["name"], "color": r["color"], "embs": []})
        emb = db.from_blob(r["embedding"])
        if emb is not None:
            p["embs"].append(emb)
    out = []
    for p in by.values():
        embs = normalize(np.vstack(p["embs"]))
        p["samples"] = embs
        p["centroid"] = normalize(embs.mean(axis=0))
        del p["embs"]
        out.append(p)
    return out


def library(model: Optional[str] = None) -> list[dict[str, Any]]:
    """Speakers with statistics for the Speakers page."""
    model = model or current_model_key()
    speakers = db.query("SELECT * FROM speakers ORDER BY lower(name)")
    stats = {
        r["speaker_id"]: r
        for r in db.query(
            "SELECT speaker_id, COUNT(*) AS n, SUM(end - start) AS secs, "
            "SUM(CASE WHEN model = ? THEN 1 ELSE 0 END) AS active "
            "FROM speaker_samples GROUP BY speaker_id",
            (model,),
        )
    }
    meetings = {
        r["speaker_id"]: r["n"]
        for r in db.query(
            "SELECT speaker_id, COUNT(DISTINCT meeting_id) AS n FROM meeting_speakers "
            "WHERE speaker_id IS NOT NULL GROUP BY speaker_id"
        )
    }
    profs = {p["id"]: p for p in profiles(model)}
    out = []
    for s in speakers:
        st = stats.get(s["id"], {})
        p = profs.get(s["id"])
        consistency = None
        if p is not None and len(p["samples"]) >= 2:
            sims = p["samples"] @ p["samples"].T
            iu = np.triu_indices(len(sims), 1)
            consistency = float(np.mean(sims[iu]))
        out.append(
            {
                **s,
                "samples": int(st.get("n") or 0),
                "active_samples": int(st.get("active") or 0),
                "seconds": round(float(st.get("secs") or 0), 1),
                "meetings": int(meetings.get(s["id"], 0)),
                "consistency": consistency,
                "strength": _strength(int(st.get("active") or 0), float(st.get("secs") or 0), consistency),
            }
        )
    return out


def _strength(samples: int, seconds: float, consistency: Optional[float]) -> str:
    if samples == 0:
        return "none"
    if samples < 3 or seconds < 15:
        return "weak"
    if samples >= 8 and seconds >= 60 and (consistency is None or consistency > 0.45):
        return "strong"
    return "good"


def sample_details(speaker_id: int) -> list[dict[str, Any]]:
    """Samples with an 'outlier' flag for ones that don't sound like the rest."""
    model = current_model_key()
    rows = samples_for(speaker_id)
    active = [r for r in rows if r["model"] == model]
    scores: dict[int, float] = {}
    if len(active) >= 3:
        embs = normalize(np.vstack([db.from_blob(r["embedding"]) for r in active]))
        total = embs.sum(axis=0)
        for i, r in enumerate(active):
            loo = normalize(total - embs[i])  # leave-one-out centroid
            scores[r["id"]] = float(embs[i] @ loo)
    limit = thresholds()["suggest"]
    meeting_ids = sorted({r["meeting_id"] for r in rows if r["meeting_id"]})
    titles = {}
    if meeting_ids:
        marks = ",".join("?" * len(meeting_ids))
        titles = {m["id"]: m["title"] for m in db.query(f"SELECT id, title FROM meetings WHERE id IN ({marks})", meeting_ids)}
    out = []
    for r in rows:
        score = scores.get(r["id"])
        out.append(
            {
                "id": r["id"],
                "meeting_id": r["meeting_id"],
                "meeting_title": titles.get(r["meeting_id"]),
                "start": r["start"],
                "end": r["end"],
                "duration": round((r["end"] or 0) - (r["start"] or 0), 1),
                "source": r["source"],
                "created_at": r["created_at"],
                "model": r["model"],
                "active": r["model"] == model,
                "has_clip": bool(r["clip"]) and paths.absolute(r["clip"]).exists(),
                "score": None if score is None else round(score, 3),
                "outlier": score is not None and score < limit,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Meeting speakers: fingerprints and recognition
# ---------------------------------------------------------------------------


def turn_voiceprint(wav: Path, turns: list[dict[str, Any]], key: Optional[str] = None,
                    max_turns: int = 8) -> Optional[np.ndarray]:
    """Average fingerprint of a speaker's longest turns."""
    ex = extractor(key)
    long_turns = sorted(
        [t for t in turns if t["end"] - t["start"] >= 1.5], key=lambda t: t["end"] - t["start"], reverse=True
    )[:max_turns]
    if not long_turns:
        long_turns = sorted(turns, key=lambda t: t["end"] - t["start"], reverse=True)[:max_turns]
    embs = []
    for t in long_turns:
        dur = min(t["end"] - t["start"], 10.0)
        mid = (t["start"] + t["end"]) / 2
        samples = audio_mod.load_wav(wav, max(0.0, mid - dur / 2), mid + dur / 2)
        emb = ex.embed_long(samples)
        if emb is not None:
            embs.append(emb * min(dur, 10.0))  # weight longer turns
    if not embs:
        return None
    return normalize(np.sum(embs, axis=0))


def recognise(voiceprints: dict[str, np.ndarray], model: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Match meeting speakers to library profiles.

    Returns ``{key: {"speaker_id", "status", "confidence", "suggested_id"}}``.
    """
    from scipy.optimize import linear_sum_assignment

    model = model or current_model_key()
    th = thresholds(model)
    profs = profiles(model)
    result = {k: {"speaker_id": None, "status": "unknown", "confidence": 0.0, "suggested_id": None}
              for k in voiceprints}
    if not profs or not voiceprints:
        return result
    keys = list(voiceprints)
    V = normalize(np.vstack([voiceprints[k] for k in keys]))
    scores = np.zeros((len(keys), len(profs)), dtype=np.float32)
    for j, p in enumerate(profs):
        centroid_sim = V @ p["centroid"]
        sample_sims = V @ p["samples"].T
        k = min(3, sample_sims.shape[1])
        topk = np.sort(sample_sims, axis=1)[:, -k:].mean(axis=1)
        scores[:, j] = 0.6 * centroid_sim + 0.4 * topk
    rows, cols = linear_sum_assignment(scores, maximize=True)
    for i, j in zip(rows, cols):
        s = float(scores[i, j])
        others = np.delete(scores[i], j)
        margin = s - float(others.max()) if len(others) else 1.0
        entry = result[keys[i]]
        entry["confidence"] = round(s, 3)
        if s >= th["auto"] and margin >= 0.05:
            entry.update(status="auto", speaker_id=profs[j]["id"])
        elif s >= th["suggest"]:
            entry.update(status="suggested", suggested_id=profs[j]["id"])
    return result


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


def learn_from_meeting(meeting_id: str, key: str, speaker_id: int, max_new: int = SAMPLES_PER_LEARN) -> int:
    """Add the clearest turns of meeting speaker ``key`` as samples of ``speaker_id``."""
    from .pipeline import load_turns  # local import to avoid a cycle

    model = current_model_key()
    if not models_installed(model):
        return 0
    mdir = paths.meeting_dir(meeting_id)
    wav = mdir / "audio.wav"
    if not wav.exists():
        return 0
    turns_data = load_turns(meeting_id)
    if not turns_data:
        return 0
    order = turns_data.get("order", [])
    idx = order.index(key) if key in order else None
    if idx is None:
        return 0
    turns = [t for t in turns_data["turns"] if t["speaker"] == idx]
    overlaps = _overlap_mask(turns_data["turns"])
    candidates = []
    for t in turns:
        dur = t["end"] - t["start"]
        if dur < SAMPLE_MIN_SECONDS:
            continue
        if overlaps.get((t["start"], t["end"], t["speaker"]), 0) > 0.25 * dur:
            continue
        candidates.append(t)
    candidates.sort(key=lambda t: t["end"] - t["start"], reverse=True)
    candidates = candidates[:16]
    if not candidates:
        return 0
    already = db.query(
        "SELECT start, end FROM speaker_samples WHERE meeting_id = ? AND speaker_id = ?", (meeting_id, speaker_id)
    )
    ex = extractor(model)
    scored = []
    for t in candidates:
        if any(abs(a["start"] - t["start"]) < 1 for a in already):
            continue
        dur = min(t["end"] - t["start"], SAMPLE_MAX_SECONDS)
        mid = (t["start"] + t["end"]) / 2
        start, end = max(0.0, mid - dur / 2), mid + dur / 2
        emb = ex.embed_long(audio_mod.load_wav(wav, start, end))
        if emb is not None:
            scored.append((start, end, emb))
    if not scored:
        return 0
    embs = normalize(np.vstack([e for _, _, e in scored]))
    centre = normalize(embs.mean(axis=0))
    ranking = np.argsort(-(embs @ centre))  # most typical first
    added = 0
    for i in ranking[:max_new]:
        start, end, emb = scored[i]
        add_sample(speaker_id, emb, model, meeting_id=meeting_id, start=start, end=end, wav=wav, source="meeting")
        added += 1
    prune(speaker_id)
    return added


def _overlap_mask(turns: list[dict[str, Any]]) -> dict[tuple, float]:
    out: dict[tuple, float] = {}
    srt = sorted(turns, key=lambda t: t["start"])
    for i, t in enumerate(srt):
        ov = 0.0
        for j in range(max(0, i - 20), min(len(srt), i + 20)):
            o = srt[j]
            if o is t or o["speaker"] == t["speaker"]:
                continue
            ov += max(0.0, min(t["end"], o["end"]) - max(t["start"], o["start"]))
        out[(t["start"], t["end"], t["speaker"])] = ov
    return out


def add_sample(
    speaker_id: int,
    emb: np.ndarray,
    model: str,
    meeting_id: Optional[str] = None,
    start: float = 0.0,
    end: float = 0.0,
    wav: Optional[Path] = None,
    source: str = "meeting",
    clip_audio: Optional[np.ndarray] = None,
) -> int:
    clip_rel = None
    clip_dir = paths.SPEAKERS / str(speaker_id)
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / f"{uuid.uuid4().hex[:12]}.wav"
    try:
        if clip_audio is not None:
            import soundfile as sf

            sf.write(str(clip_path), clip_audio, audio_mod.SAMPLE_RATE, subtype="PCM_16")
            clip_rel = paths.rel(clip_path)
        elif wav is not None:
            audio_mod.write_clip(wav, clip_path, start, end)
            clip_rel = paths.rel(clip_path)
    except Exception:  # noqa: BLE001 - a missing clip only disables playback
        log.exception("Could not save speaker clip")
    cur = db.execute(
        "INSERT INTO speaker_samples (speaker_id, meeting_id, start, end, model, embedding, clip, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (speaker_id, meeting_id, start, end, model, db.to_blob(normalize(emb)), clip_rel, source, time.time()),
    )
    db.execute("UPDATE speakers SET updated_at = ? WHERE id = ?", (time.time(), speaker_id))
    return int(cur.lastrowid)


def prune(speaker_id: int) -> None:
    """Keep the library lean: drop the most redundant samples above the cap."""
    model = current_model_key()
    rows = samples_for(speaker_id, model)
    while len(rows) > MAX_SAMPLES_PER_SPEAKER:
        embs = normalize(np.vstack([db.from_blob(r["embedding"]) for r in rows]))
        sims = embs @ embs.T
        np.fill_diagonal(sims, -1)
        redundant = int(np.argmax(sims.max(axis=1)))
        delete_sample(rows[redundant]["id"])
        rows.pop(redundant)


def delete_sample(sample_id: int) -> None:
    row = db.one("SELECT clip FROM speaker_samples WHERE id = ?", (sample_id,))
    db.execute("DELETE FROM speaker_samples WHERE id = ?", (sample_id,))
    if row and row["clip"]:
        # the same clip can back fingerprints from several voice models
        still_used = db.one("SELECT id FROM speaker_samples WHERE clip = ?", (row["clip"],))
        if not still_used:
            try:
                paths.absolute(row["clip"]).unlink(missing_ok=True)
            except OSError:
                pass


def remove_outliers(speaker_id: int) -> int:
    removed = 0
    for s in sample_details(speaker_id):
        if s["outlier"]:
            delete_sample(s["id"])
            removed += 1
    return removed


def forget_meeting(meeting_id: str) -> int:
    rows = db.query("SELECT id FROM speaker_samples WHERE meeting_id = ?", (meeting_id,))
    for r in rows:
        delete_sample(r["id"])
    return len(rows)


def delete_speaker(speaker_id: int) -> None:
    for r in db.query("SELECT id FROM speaker_samples WHERE speaker_id = ?", (speaker_id,)):
        delete_sample(r["id"])
    # meetings keep the name as a plain label
    row = db.one("SELECT name FROM speakers WHERE id = ?", (speaker_id,))
    if row:
        db.execute(
            "UPDATE meeting_speakers SET label = ?, status = 'unknown', speaker_id = NULL "
            "WHERE speaker_id = ?",
            (row["name"], speaker_id),
        )
    db.execute("UPDATE meeting_speakers SET suggested_id = NULL WHERE suggested_id = ?", (speaker_id,))
    db.execute("DELETE FROM speakers WHERE id = ?", (speaker_id,))
    try:
        d = paths.SPEAKERS / str(speaker_id)
        if d.exists():
            import shutil

            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def merge_speakers(source_id: int, target_id: int) -> None:
    if source_id == target_id:
        return
    db.execute("UPDATE speaker_samples SET speaker_id = ? WHERE speaker_id = ?", (target_id, source_id))
    db.execute("UPDATE meeting_speakers SET speaker_id = ? WHERE speaker_id = ?", (target_id, source_id))
    db.execute("UPDATE meeting_speakers SET suggested_id = ? WHERE suggested_id = ?", (target_id, source_id))
    db.execute("DELETE FROM speakers WHERE id = ?", (source_id,))
    prune(target_id)


def enroll(speaker_id: int, samples: np.ndarray) -> int:
    """Add a voice sample recorded directly for this person (e.g. 20 s of reading)."""
    model = current_model_key()
    ex = extractor(model)
    added = 0
    piece = int(8 * audio_mod.SAMPLE_RATE)
    # Split long recordings into several samples so outlier detection works
    for start in range(0, max(1, len(samples) - int(2 * audio_mod.SAMPLE_RATE)), piece):
        part = samples[start : start + piece]
        if len(part) < 2 * audio_mod.SAMPLE_RATE:
            continue
        emb = ex.embed_long(part)
        if emb is None:
            continue
        add_sample(speaker_id, emb, model, start=start / audio_mod.SAMPLE_RATE,
                   end=(start + len(part)) / audio_mod.SAMPLE_RATE, source="enroll", clip_audio=part)
        added += 1
    prune(speaker_id)
    return added


def reembed_all() -> int:
    """Recompute fingerprints of saved clips for the current voice model."""
    model = current_model_key()
    ex = extractor(model)
    done = 0
    rows = db.query("SELECT * FROM speaker_samples WHERE model != ? AND clip IS NOT NULL", (model,))
    for r in rows:
        clip = paths.absolute(r["clip"])
        if not clip.exists():
            continue
        exists = db.one(
            "SELECT id FROM speaker_samples WHERE speaker_id = ? AND model = ? AND clip = ?",
            (r["speaker_id"], model, r["clip"]),
        )
        if exists:
            continue
        emb = ex.embed_long(audio_mod.load_wav(clip))
        if emb is None:
            continue
        db.execute(
            "INSERT INTO speaker_samples (speaker_id, meeting_id, start, end, model, embedding, clip, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["speaker_id"], r["meeting_id"], r["start"], r["end"], model, db.to_blob(emb), r["clip"],
             r["source"], r["created_at"]),
        )
        done += 1
    return done
