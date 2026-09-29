"""Processing pipeline: audio -> transcript -> speakers -> notes."""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import align, catalog, db, diarization, hardware, paths, settings, speakers
from . import audio as audio_mod
from .downloads import manager as downloads
from .jobs import JobCancelled, JobContext, UserFacingError, runner
from .worker import StageCancelled, StageError, run_in_child

log = logging.getLogger("qt.pipeline")

STAGES = {
    "prepare": "Preparing audio",
    "transcribe": "Transcribing speech",
    "speakers": "Recognising speakers",
    "notes": "Writing meeting notes",
}
STATE_FILE = paths.DATA / "state.json"


# ---------------------------------------------------------------------------
# Small persistent state (e.g. "the GPU failed, use CPU")
# ---------------------------------------------------------------------------


def state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def set_state(**values: Any) -> None:
    s = state()
    s.update(values)
    STATE_FILE.write_text(json.dumps(s, indent=2), "utf-8")


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def resolve_whisper_model(key: str = "") -> str:
    key = key or settings.get("whisper_model")
    if catalog.whisper(key):
        return key
    return catalog.recommend(hardware.info())["whisper"]


def default_options(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    s = settings.load()
    opts = {
        "language": s["language"],
        "whisper_model": resolve_whisper_model(),
        "quality": s["whisper_quality"],
        "vocabulary": s["vocabulary"],
        "diarization": s["diarization"],
        "num_speakers": 0,
        "notes": s["auto_notes"],
        "template": s["notes_template"],
        "llm_model": s["llm_model"],
        "notes_language": s["notes_language"],
        "notes_detail": s["notes_detail"],
        "instructions": s["notes_instructions"],
    }
    for k, v in (overrides or {}).items():
        if k in opts and v is not None:
            opts[k] = v
    try:
        opts["num_speakers"] = max(0, min(20, int(opts["num_speakers"] or 0)))
    except (TypeError, ValueError):
        opts["num_speakers"] = 0
    return opts


def meeting_options(meeting: dict[str, Any]) -> dict[str, Any]:
    return default_options(db.loads(meeting.get("options"), {}))


# ---------------------------------------------------------------------------
# Creating meetings
# ---------------------------------------------------------------------------


def new_meeting_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def title_from_filename(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"^[0-9a-f]{8}-", "", stem)  # upload prefixes
    stem = re.sub(r"[_]+", " ", stem).strip()
    return stem[:80] or "Untitled meeting"


def create_meeting(
    src: Path,
    original_name: str,
    source: str = "upload",
    options: Optional[dict[str, Any]] = None,
    title: Optional[str] = None,
    recorded_at: Optional[float] = None,
    move: bool = True,
) -> str:
    mid = new_meeting_id()
    mdir = paths.meeting_dir(mid)
    mdir.mkdir(parents=True, exist_ok=True)
    ext = Path(original_name).suffix.lower() or src.suffix.lower() or ".bin"
    target = mdir / f"original{ext}"
    if move:
        shutil.move(str(src), target)
    else:
        shutil.copy2(src, target)
    opts = default_options(options)
    if title:
        opts["title_locked"] = True
    db.execute(
        "INSERT INTO meetings (id, title, created_at, recorded_at, source, original_name, status, options) "
        "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
        (
            mid,
            (title or title_from_filename(original_name)).strip(),
            time.time(),
            recorded_at or time.time(),
            source,
            original_name,
            db.dumps(opts),
        ),
    )
    runner.submit("process", mid, {})
    return mid


def reprocess(meeting_id: str, stages: list[str], options: Optional[dict[str, Any]] = None) -> int:
    meeting = db.one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        raise UserFacingError("Meeting not found")
    if options:
        current = db.loads(meeting["options"], {})
        current.update({k: v for k, v in options.items() if v is not None})
        db.execute("UPDATE meetings SET options = ? WHERE id = ?", (db.dumps(current), meeting_id))
    order = [s for s in STAGES if s in stages]
    force = list(order)
    if "transcribe" in order:
        # the notes should describe the new transcript
        opts = meeting_options(db.one("SELECT * FROM meetings WHERE id = ?", (meeting_id,)))
        if opts["notes"] and "notes" not in order:
            order.append("notes")
    if "prepare" not in order and not (paths.meeting_dir(meeting_id) / "audio.wav").exists():
        order.insert(0, "prepare")
    return runner.submit("process", meeting_id, {"stages": order, "force": force})


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def process_job(ctx: JobContext) -> None:
    meeting = db.one("SELECT * FROM meetings WHERE id = ?", (ctx.meeting_id,))
    if not meeting:
        raise UserFacingError("This meeting was deleted.")
    params = ctx.job["params"]
    opts = meeting_options(meeting)
    stages = params.get("stages") or list(STAGES)
    if not opts["notes"] and "notes" in stages and "notes" not in params.get("force", []):
        stages = [s for s in stages if s != "notes"]
    done: list[str] = params.get("done", [])
    force = set(params.get("force", []))
    ctx.plan([(s, STAGES[s]) for s in stages], done)
    for stage in stages:
        if stage in done:
            continue
        ctx.stage(stage, STAGES[stage])
        meeting = db.one("SELECT * FROM meetings WHERE id = ?", (ctx.meeting_id,))
        if not meeting:
            raise UserFacingError("This meeting was deleted.")
        opts = meeting_options(meeting)
        {"prepare": stage_prepare, "transcribe": stage_transcribe, "speakers": stage_speakers,
         "notes": stage_notes}[stage](meeting, opts, ctx, stage in force)
        done.append(stage)
        params["done"] = done
        db.execute("UPDATE jobs SET params = ? WHERE id = ?", (db.dumps(params), ctx.job_id))


def _original_file(meeting_id: str) -> Optional[Path]:
    hits = sorted(paths.meeting_dir(meeting_id).glob("original.*"))
    return hits[0] if hits else None


def stage_prepare(meeting: dict, opts: dict, ctx: JobContext, force: bool) -> None:
    mdir = paths.meeting_dir(meeting["id"])
    wav = mdir / "audio.wav"
    peaks = mdir / "peaks.json"
    if wav.exists() and peaks.exists() and not force:
        return
    src = _original_file(meeting["id"])
    if not src:
        raise UserFacingError("The original audio file is missing.")
    try:
        info = audio_mod.convert_to_wav(src, wav, lambda p: ctx.progress(p, "Converting audio"), ctx.cancelled)
    except InterruptedError:
        raise JobCancelled()
    except Exception as exc:  # noqa: BLE001
        raise UserFacingError(f"Could not read this audio file ({exc}). Is it a supported audio or video file?")
    audio_mod.save_peaks(peaks, info["peaks"])
    stats = db.loads(meeting["stats"], {})
    stats["rms"] = info["rms"]
    db.execute(
        "UPDATE meetings SET duration = ?, audio_file = ?, stats = ? WHERE id = ?",
        (info["duration"], paths.rel(wav), db.dumps(stats), meeting["id"]),
    )
    if info["duration"] < 0.5:
        raise UserFacingError("This recording is empty.")
    if not settings.get("keep_original_audio") and src.suffix.lower() != ".wav":
        src.unlink(missing_ok=True)


def _ensure_download(ctx: JobContext, task_id: str, name: str, files: list, dest: Path, label: str) -> None:
    missing = any(
        not (dest / f["path"]).exists() and not (dest / (f["path"] + ".extracted")).exists() for f in files
    )
    if not missing:
        return
    downloads.start(task_id, name, files, dest)
    while True:
        t = downloads.get(task_id)
        if not t:
            break
        if t["status"] == "done":
            return
        if t["status"] in ("error", "cancelled"):
            raise UserFacingError(f"Could not download {name}: {t.get('error') or t['status']}")
        total = t["total"] or 1
        speed = t["speed"] or 0
        eta = (total - t["done"]) / speed if speed > 0 else None
        ctx.progress(t["done"] / total, f"{label} ({t['done'] / 1e6:.0f} of {total / 1e6:.0f} MB)", eta=eta)
        time.sleep(0.5)


def whisper_dir(key: str) -> Path:
    return paths.WHISPER_MODELS / key


def whisper_installed(key: str) -> bool:
    entry = catalog.whisper(key)
    return bool(entry) and all((whisper_dir(key) / f["path"]).exists() for f in entry["files"])


def pick_device() -> str:
    choice = settings.get("device")
    hw = hardware.info()
    if choice == "cpu":
        return "cpu"
    if not hw["nvidia"] or not hardware.cuda_libraries_installed():
        return "cpu"
    if choice == "auto" and state().get("cuda_failed"):
        return "cpu"
    return "cuda"


def stage_transcribe(meeting: dict, opts: dict, ctx: JobContext, force: bool) -> None:
    mid = meeting["id"]
    mdir = paths.meeting_dir(mid)
    out = mdir / "transcript.json"
    if out.exists() and not force:
        rebuild_segments(mid)
        return
    key = resolve_whisper_model(opts["whisper_model"])
    entry = catalog.whisper(key)
    if not whisper_installed(key):
        _ensure_download(ctx, f"whisper:{key}", entry["name"], entry["files"], whisper_dir(key),
                         f"Downloading {entry['name']}")
    language = opts["language"] if opts["language"] != "auto" else None
    if entry.get("english_only"):
        language = "en"
    vocab = _vocabulary(opts)
    device = pick_device()
    started = time.time()
    duration = meeting["duration"] or audio_mod.duration_of(mdir / "audio.wav")
    perf_key = f"rtf:{key}:{device}"
    expected_rtf = db.get_perf().get(perf_key) or entry["rtf_gpu" if device == "cuda" else "rtf_cpu"]
    if device == "cpu":
        expected_rtf *= max(0.5, 4 / max(1, hardware.cpu_threads()))
    ctx.info(eta_total=duration * expected_rtf)

    def on_message(msg: dict[str, Any]) -> None:
        if msg.get("type") == "progress":
            value = float(msg.get("value", 0))
            eta = None
            elapsed = time.time() - started
            if value > 0.05:
                eta = elapsed / value * (1 - value)
            else:
                eta = max(0.0, duration * expected_rtf - elapsed)
            ctx.progress(value, msg.get("message"), partial=msg.get("partial"), eta=eta)
        elif msg.get("type") == "info":
            ctx.info(language=msg.get("language"))

    def run(dev: str) -> dict[str, Any]:
        params = {
            "wav": str(mdir / "audio.wav"),
            "model_dir": str(whisper_dir(key)),
            "model_key": key,
            "language": language,
            "vocabulary": vocab,
            "quality": opts["quality"],
            "device": dev,
            "compute_type": _compute_type(dev),
            "threads": hardware.cpu_threads(),
            "out": str(out),
        }
        return run_in_child("transcribe", params, on_message, ctx.cancelled)

    try:
        try:
            result = run(device)
        except StageError as exc:
            if device == "cuda" and exc.kind in ("cuda", "crash"):
                log.warning("GPU transcription failed, falling back to CPU: %s\n%s", exc, exc.details)
                set_state(cuda_failed=str(exc)[:500], cuda_failed_at=time.time())
                ctx.progress(0.0, "The GPU didn't work - continuing on the processor instead")
                device = "cpu"
                started = time.time()
                result = run("cpu")
            else:
                raise
    except StageCancelled:
        raise JobCancelled()
    except StageError as exc:
        log.error("Transcription failed: %s\n%s", exc, exc.details)
        raise UserFacingError(f"Transcription failed: {exc}")
    if device == "cuda" and state().get("cuda_failed"):
        set_state(cuda_failed=None)
    elapsed = time.time() - started
    if duration > 60:
        db.record_perf(f"rtf:{key}:{device}", elapsed / duration)
    stats = db.loads(meeting["stats"], {})
    stats.update(transcribe_seconds=round(elapsed, 1), whisper_model=key, device=device,
                 words=result.get("words"))
    db.execute("UPDATE meetings SET language = ?, stats = ? WHERE id = ?", (result["language"], db.dumps(stats), mid))
    rebuild_segments(mid)


def _compute_type(device: str) -> str:
    if device == "cpu":
        return "int8"
    vram = hardware.info()["vram_gb"]
    try:
        import ctranslate2

        supported = ctranslate2.get_supported_compute_types("cuda")
    except Exception:  # noqa: BLE001 - checked again in the worker
        supported = {"float16", "int8_float16", "float32"}
    for ct in (["float16", "int8_float16"] if vram >= 5 else ["int8_float16", "float16"]) + ["int8", "float32"]:
        if ct in supported:
            return ct
    return "float32"


def _vocabulary(opts: dict) -> str:
    words = [w.strip() for w in re.split(r"[,\n;]+", opts.get("vocabulary") or "") if w.strip()]
    names = [r["name"] for r in db.query("SELECT name FROM speakers ORDER BY updated_at DESC LIMIT 30")]
    seen, out = set(), []
    for w in words + names:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return ", ".join(out)[:600]


# ---------------------------------------------------------------------------
# Speakers
# ---------------------------------------------------------------------------


def speaker_step_seconds() -> float:
    return {"fast": 4.0, "balanced": 2.0, "precise": 1.0}.get(settings.get("speaker_detail"), 2.0)


def stage_speakers(meeting: dict, opts: dict, ctx: JobContext, force: bool) -> None:
    mid = meeting["id"]
    mdir = paths.meeting_dir(mid)
    if not opts["diarization"] or opts.get("num_speakers") == 1:
        _single_speaker(mid)
        rebuild_segments(mid)
        return
    key = speakers.current_model_key()
    entry = catalog.speaker_model(key)
    seg = catalog.SEGMENTATION
    _ensure_download(ctx, "speaker:segmentation", seg["name"], seg["files"], paths.SPEAKER_MODELS,
                     "Downloading the speaker models")
    _ensure_download(ctx, f"speaker:{key}", entry["name"], entry["files"], paths.SPEAKER_MODELS,
                     "Downloading the speaker models")
    npz = mdir / "diarization.npz"
    cached_ok = False
    if npz.exists() and not force:
        try:
            cached_ok = diarization.DiarizationData.load(str(npz)).embedding_model == key
        except Exception:  # noqa: BLE001
            cached_ok = False
    if not cached_ok:
        started = time.time()

        def on_message(msg: dict[str, Any]) -> None:
            if msg.get("type") == "progress":
                ctx.progress(float(msg.get("value", 0)), msg.get("message"))

        params = {
            "wav": str(mdir / "audio.wav"),
            "segmentation_model": str(speakers.segmentation_path()),
            "embedding_model": str(speakers.model_path(key)),
            "embedding_key": key,
            "step": speaker_step_seconds(),
            "threads": hardware.cpu_threads(),
            "out": str(npz),
        }
        try:
            run_in_child("diarize", params, on_message, ctx.cancelled)
        except StageCancelled:
            raise JobCancelled()
        except StageError as exc:
            log.error("Speaker analysis failed: %s\n%s", exc, exc.details)
            raise UserFacingError(f"Speaker recognition failed: {exc}")
        stats = db.loads(meeting["stats"], {})
        stats["speakers_seconds"] = round(time.time() - started, 1)
        db.execute("UPDATE meetings SET stats = ? WHERE id = ?", (db.dumps(stats), mid))
    ctx.progress(0.97, "Recognising known voices")
    apply_clustering(mid, opts.get("num_speakers") or None)
    rebuild_segments(mid)


def _single_speaker(mid: str) -> None:
    turns_path = paths.meeting_dir(mid) / "turns.json"
    turns_path.unlink(missing_ok=True)
    existing = db.one("SELECT * FROM meeting_speakers WHERE meeting_id = ? AND key = 'S1'", (mid,))
    db.execute("DELETE FROM meeting_speakers WHERE meeting_id = ? AND key != 'S1'", (mid,))
    if not existing:
        db.execute(
            "INSERT INTO meeting_speakers (meeting_id, key, label, color, status) VALUES (?, 'S1', 'Speaker 1', ?, 'unknown')",
            (mid, speakers.PALETTE[0]),
        )


def load_turns(meeting_id: str) -> Optional[dict[str, Any]]:
    p = paths.meeting_dir(meeting_id) / "turns.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def apply_clustering(meeting_id: str, num_speakers: Optional[int] = None) -> dict[str, Any]:
    """Group voices into speakers (fast, uses cached analysis) and recognise known people."""
    mdir = paths.meeting_dir(meeting_id)
    data = diarization.DiarizationData.load(str(mdir / "diarization.npz"))
    model = data.embedding_model or speakers.current_model_key()
    th = speakers.thresholds(model)
    anchors = None
    if model == speakers.current_model_key():
        profs = speakers.profiles(model)
        if profs:
            anchors = np.vstack([p["centroid"] for p in profs])
    result = diarization.cluster(
        data, num_speakers=num_speakers, threshold=th["cluster"], anchors=anchors,
        anchor_threshold=th["auto"],
    )
    k = len(result.speaking_time)
    order = [f"S{i + 1}" for i in range(k)]
    turns = [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in result.turns]
    (mdir / "turns.json").write_text(
        json.dumps({"order": order, "turns": turns, "num_speakers": num_speakers or 0, "model": model}), "utf-8"
    )
    wav = mdir / "audio.wav"
    voiceprints: dict[str, np.ndarray] = {}
    if model == speakers.current_model_key() and speakers.models_installed(model):
        for i, key in enumerate(order):
            vp = speakers.turn_voiceprint(wav, [t for t in turns if t["speaker"] == i], model)
            if vp is not None:
                voiceprints[key] = vp
    recognised = speakers.recognise(voiceprints, model) if voiceprints else {}

    # carry over the user's earlier decisions when re-grouping speakers
    old = {r["key"]: r for r in db.query("SELECT * FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))}
    carried: dict[str, dict] = {}
    for key, vp in voiceprints.items():
        best, best_sim = None, 0.0
        for ok, row in old.items():
            if row["status"] not in ("confirmed", "named") or not row["embedding"]:
                continue
            sim = float(vp @ diarization.normalize(db.from_blob(row["embedding"])))
            if sim > best_sim:
                best, best_sim = row, sim
        if best is not None and best_sim > 0.85:
            carried[key] = best

    auto_learn = bool(settings.get("auto_learn_confident"))
    db.execute("DELETE FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))
    used_colors: set[str] = set()
    for i, key in enumerate(order):
        rec = recognised.get(key, {"speaker_id": None, "status": "unknown", "confidence": 0.0, "suggested_id": None})
        label = f"Speaker {i + 1}"
        speaker_id, status, suggested = rec["speaker_id"], rec["status"], rec["suggested_id"]
        prev = carried.get(key)
        if prev is not None:
            speaker_id, status, suggested = prev["speaker_id"], prev["status"], None
            label = prev["label"] or label
        color = None
        if speaker_id:
            row = db.one("SELECT color, name FROM speakers WHERE id = ?", (speaker_id,))
            color = row["color"] if row else None
            if row:
                label = row["name"]
        if not color or color in used_colors:
            free = [c for c in speakers.PALETTE if c not in used_colors]
            color = free[0] if free else speakers.PALETTE[i % len(speakers.PALETTE)]
        used_colors.add(color)
        db.execute(
            "INSERT INTO meeting_speakers (meeting_id, key, label, speaker_id, status, confidence, suggested_id, "
            "color, talk_time, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meeting_id, key, label, speaker_id, status, rec["confidence"], suggested, color,
             round(result.speaking_time[i], 1), db.to_blob(voiceprints.get(key))),
        )
        if auto_learn and status == "auto" and speaker_id and rec["confidence"] >= speakers.thresholds(model)["auto"] + 0.08:
            try:
                speakers.learn_from_meeting(meeting_id, key, speaker_id, max_new=2)
            except Exception:  # noqa: BLE001
                log.exception("auto-learn failed")
    return {"speakers": k}


# ---------------------------------------------------------------------------
# Transcript segments
# ---------------------------------------------------------------------------


def load_transcript(meeting_id: str) -> Optional[dict[str, Any]]:
    p = paths.meeting_dir(meeting_id) / "transcript.json"
    if not p.exists():
        return None
    return json.loads(p.read_text("utf-8"))


def rebuild_segments(meeting_id: str) -> None:
    transcript = load_transcript(meeting_id)
    if transcript is None:
        return
    turns_data = load_turns(meeting_id)
    turns = []
    order = ["S1"]
    if turns_data and turns_data.get("turns"):
        order = turns_data["order"]
        turns = [diarization.Turn(t["start"], t["end"], t["speaker"]) for t in turns_data["turns"]]
    utterances = align.align(transcript, turns)
    rows = []
    for i, u in enumerate(utterances):
        idx = u["speaker"] if u["speaker"] is not None else 0
        key = order[idx] if idx < len(order) else f"S{idx + 1}"
        rows.append((meeting_id, i, u["start"], u["end"], key, u["text"], json.dumps(u["words"])))
    with db.transaction() as c:
        c.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
        c.executemany(
            "INSERT INTO segments (meeting_id, idx, start, end, speaker, text, words) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    used = {r[4] for r in rows}
    existing = {r["key"] for r in db.query("SELECT key FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))}
    for n, key in enumerate(sorted(used - existing, key=lambda k: int(k[1:]) if k[1:].isdigit() else 99)):
        db.execute(
            "INSERT INTO meeting_speakers (meeting_id, key, label, color, status) VALUES (?, ?, ?, ?, 'unknown')",
            (meeting_id, key, f"Speaker {key[1:]}", speakers.PALETTE[(int(key[1:]) - 1) % 16] if key[1:].isdigit()
             else speakers.PALETTE[n % 16]),
        )
    if not turns:
        # no speaker analysis: talk time is simply the speech time
        total = sum(u["end"] - u["start"] for u in utterances)
        db.execute("UPDATE meeting_speakers SET talk_time = ? WHERE meeting_id = ? AND key = 'S1'", (total, meeting_id))
    db.reindex_meeting(meeting_id)


def speaker_names(meeting_id: str) -> dict[str, str]:
    rows = db.query(
        "SELECT ms.key, ms.label, s.name FROM meeting_speakers ms LEFT JOIN speakers s ON s.id = ms.speaker_id "
        "WHERE ms.meeting_id = ?",
        (meeting_id,),
    )
    return {r["key"]: r["name"] or r["label"] or r["key"] for r in rows}


def segments_for(meeting_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, idx, start, end, speaker, text, edited FROM segments WHERE meeting_id = ? ORDER BY idx",
        (meeting_id,),
    )


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def stage_notes(meeting: dict, opts: dict, ctx: JobContext, force: bool) -> None:
    from .llm import client as llm_client
    from .llm import notes as notes_mod

    mid = meeting["id"]
    segs = segments_for(mid)
    if not segs:
        _set_notes_meta(mid, {"status": "empty", "reason": "No speech was found in this recording."})
        return
    try:
        backend = llm_client.get_backend(opts.get("llm_model") or None)
        ctx.progress(0.01, "Starting the AI engine")
        backend.prepare(ctx.cancelled, lambda m: ctx.progress(0.01, m))
    except llm_client.LLMUnavailable as exc:
        _set_notes_meta(mid, {"status": "unavailable", "reason": str(exc)})
        return
    except InterruptedError:
        raise JobCancelled()
    except llm_client.LLMError as exc:
        raise UserFacingError(str(exc))
    language = opts.get("notes_language")
    if not language or language == "auto":
        language = meeting.get("language") or None
    names = speaker_names(mid)
    try:
        notes = notes_mod.generate(
            backend, segs, names, meeting["duration"] or segs[-1]["end"],
            template_id=opts.get("template") or "general",
            detail_level=opts.get("notes_detail") or "standard",
            language=language,
            extra_instructions=opts.get("instructions") or "",
            progress=lambda v, m: ctx.progress(v, m),
            cancelled=ctx.cancelled,
        )
    except InterruptedError:
        raise JobCancelled()
    except llm_client.LLMError as exc:
        raise UserFacingError(f"Writing notes failed: {exc}")
    stats = notes.pop("_stats", {})
    meta = {
        "status": "ready",
        "model": backend.describe(),
        "backend": backend.name,
        "template": opts.get("template") or "general",
        "detail": opts.get("notes_detail") or "standard",
        "language": language,
        "created_at": time.time(),
        "names": names,
        **stats,
    }
    title = meeting["title"]
    if notes.get("title") and not opts.get("title_locked"):
        title = notes["title"]
    summary = (notes.get("summary") or "").split("\n")[0][:300]
    db.execute(
        "UPDATE meetings SET notes = ?, notes_meta = ?, summary = ?, title = ? WHERE id = ?",
        (db.dumps(notes), db.dumps(meta), summary, title, mid),
    )


def _set_notes_meta(mid: str, meta: dict) -> None:
    db.execute("UPDATE meetings SET notes_meta = ? WHERE id = ?", (db.dumps(meta), mid))


def rename_in_notes(meeting_id: str, old: str, new: str) -> None:
    """After naming a speaker, update the notes so they use the real name."""
    row = db.one("SELECT notes FROM meetings WHERE id = ?", (meeting_id,))
    if not row or not row["notes"] or not old or old == new:
        return
    pattern = re.compile(r"(?<![\w])" + re.escape(old) + r"(?![\w])")

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return pattern.sub(new, v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    notes = walk(json.loads(row["notes"]))
    summary = (notes.get("summary") or "").split("\n")[0][:300]
    db.execute("UPDATE meetings SET notes = ?, summary = ? WHERE id = ?", (db.dumps(notes), summary, meeting_id))


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def delete_meeting(meeting_id: str, forget_voices: bool = False) -> None:
    runner.cancel_meeting(meeting_id)
    if forget_voices:
        speakers.forget_meeting(meeting_id)
    db.drop_index(meeting_id)
    db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    shutil.rmtree(paths.meeting_dir(meeting_id), ignore_errors=True)


runner.register("process", process_job)
