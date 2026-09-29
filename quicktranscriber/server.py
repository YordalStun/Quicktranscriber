"""Local web server: the app's user interface talks to this API.

It listens on 127.0.0.1 only. Requests must come from the app itself: the Host
header is checked (stops DNS-rebinding attacks) and every request that changes
something needs a custom header, which web pages on other sites cannot send.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import APP_NAME, __version__, catalog, db, downloads, exporters, hardware, paths, pipeline, settings, speakers
from . import audio as audio_mod
from .jobs import UserFacingError, runner
from .llm import ask as ask_mod
from .llm import client as llm_client
from .llm import notes as notes_mod
from .llm import runtime as llm_runtime

log = logging.getLogger("qt.server")

app = FastAPI(title=APP_NAME, version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
ALLOWED_HOSTS: set[str] = {"127.0.0.1", "localhost"}


class LocalOnly(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].strip("[]").lower()
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-qt") != "1":
            return JSONResponse({"detail": "Missing app header"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


app.add_middleware(LocalOnly)


@app.exception_handler(UserFacingError)
async def _user_error(_request: Request, exc: UserFacingError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(llm_client.LLMError)
async def _llm_error(_request: Request, exc: llm_client.LLMError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def _meeting(mid: str) -> dict[str, Any]:
    m = db.one("SELECT * FROM meetings WHERE id = ?", (mid,))
    if not m:
        raise HTTPException(404, "Meeting not found")
    return m


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Status, settings, hardware
# ---------------------------------------------------------------------------


@app.get("/api/status")
def status() -> dict[str, Any]:
    hw = hardware.info()
    s = settings.load()
    st = pipeline.state()
    whisper_key = pipeline.resolve_whisper_model()
    return {
        "app": APP_NAME,
        "version": __version__,
        "hardware": {**hw, "cuda_libs": hardware.cuda_libraries_installed()},
        "recommend": catalog.recommend(hw),
        "onboarded": s["onboarded"],
        "whisper_model": whisper_key,
        "whisper_installed": pipeline.whisper_installed(whisper_key),
        "speaker_models_installed": speakers.models_installed(),
        "llm": llm_client.status(),
        "device": pipeline.pick_device(),
        "cuda_failed": st.get("cuda_failed"),
        "search": db.FTS_AVAILABLE,
        "active_jobs": len([j for j in runner.snapshot() if j["status"] in ("queued", "running")]),
        "data_folder": str(paths.ROOT),
        "platform": sys.platform,
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {
        "settings": settings.load(),
        "languages": catalog.LANGUAGES,
        "templates": [{k: t[k] for k in ("id", "name", "icon", "description")} for t in notes_mod.TEMPLATES],
    }


@app.put("/api/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    data = await _json(request)
    before = settings.load()
    after = settings.update(data)
    if before["llm_device"] != after["llm_device"] or before["llm_context"] != after["llm_context"] \
            or before["llm_thinking"] != after["llm_thinking"]:
        llm_runtime.server.stop()
    if before["speaker_model"] != after["speaker_model"] and speakers.models_installed(after["speaker_model"]):
        threading.Thread(target=_safe(speakers.reembed_all), daemon=True).start()
    return {"settings": after}


def _safe(fn):
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:  # noqa: BLE001
            log.exception("background task failed")
    return run


@app.get("/api/hardware")
def get_hardware() -> dict[str, Any]:
    hardware.info.cache_clear()
    return {**hardware.info(), "cuda_libs": hardware.cuda_libraries_installed(), "free_vram_mb": hardware.free_vram_mb()}


# ---------------------------------------------------------------------------
# Models & downloads
# ---------------------------------------------------------------------------


def _task_status(task_id: str) -> Optional[dict[str, Any]]:
    t = downloads.manager.get(task_id)
    if t and t["status"] in ("queued", "downloading", "extracting", "error"):
        return t
    return None


@app.get("/api/models")
def models() -> dict[str, Any]:
    hw = hardware.info()
    rec = catalog.recommend(hw)
    perf = db.get_perf()
    device = pipeline.pick_device()
    whisper = []
    for m in catalog.WHISPER:
        rtf = perf.get(f"rtf:{m['key']}:{device}") or m["rtf_gpu" if device == "cuda" else "rtf_cpu"]
        if device == "cpu" and not perf.get(f"rtf:{m['key']}:cpu"):
            rtf *= max(0.5, 4 / max(1, hardware.cpu_threads()))
        whisper.append({
            **{k: v for k, v in m.items() if k != "files"},
            "size": catalog.size_of(m),
            "installed": pipeline.whisper_installed(m["key"]),
            "recommended": m["key"] == rec["whisper"],
            "minutes_per_hour": round(rtf * 60),
            "measured": bool(perf.get(f"rtf:{m['key']}:{device}")),
            "download": _task_status(f"whisper:{m['key']}"),
        })
    llms = []
    for m in catalog.LLM:
        ram = catalog.llm_ram_gb(m)
        fits_gpu = hw["vram_gb"] >= ram - 0.8
        llms.append({
            **{k: v for k, v in m.items() if k != "files"},
            "size": catalog.size_of(m),
            "ram_gb": ram,
            "fits": fits_gpu or hw["ram_gb"] >= ram + 2,
            "fits_gpu": fits_gpu,
            "installed": llm_client.model_file(m["key"]) is not None,
            "recommended": m["key"] == rec["llm"],
            "download": _task_status(f"llm:{m['key']}"),
        })
    custom = [{"key": c["key"], "name": c["name"], "size": c["size"], "installed": True}
              for c in llm_client.custom_models()]
    spk = [{
        **{k: v for k, v in m.items() if k not in ("files", "match")},
        "installed": speakers.models_installed(m["key"]),
        "recommended": m["key"] == rec["speaker"],
        "download": _task_status(f"speaker:{m['key']}") or _task_status("speaker:segmentation"),
    } for m in catalog.SPEAKER_EMBEDDINGS]
    engine = llm_runtime.installed()
    return {
        "whisper": whisper,
        "llm": llms,
        "custom_llm": custom,
        "speaker": spk,
        "engine": {
            "installed": engine,
            "preferred": llm_runtime.preferred_backend(),
            "download": _task_status("runtime:llama.cpp"),
        },
        "gpu_pack": {"installed": hardware.cuda_libraries_installed(), "nvidia": hw["nvidia"],
                     "task": _task_status("gpu:pack") or _gpu_task.get("status")},
        "hardware": hw,
        "recommend": rec,
        "device": device,
        "disk": {"models": downloads.dir_size(paths.MODELS), "data": downloads.dir_size(paths.DATA)},
    }


@app.post("/api/models/download")
async def download_model(request: Request) -> dict[str, Any]:
    data = await _json(request)
    kind, key = data.get("kind"), data.get("key")
    if kind == "whisper":
        m = catalog.whisper(key)
        if not m:
            raise HTTPException(404, "Unknown model")
        return downloads.manager.start(f"whisper:{key}", m["name"], m["files"], pipeline.whisper_dir(key))
    if kind == "llm":
        m = catalog.llm(key)
        if not m:
            raise HTTPException(404, "Unknown model")
        return downloads.manager.start(f"llm:{key}", m["name"], m["files"], paths.LLM_MODELS / key)
    if kind == "speaker":
        m = catalog.speaker_model(key)
        if not m:
            raise HTTPException(404, "Unknown model")
        seg = catalog.SEGMENTATION
        files = seg["files"] + m["files"]
        return downloads.manager.start(f"speaker:{key}", m["name"], files, paths.SPEAKER_MODELS)
    if kind == "engine":
        try:
            return llm_runtime.install(data.get("backend"))
        except llm_runtime.RuntimeError_ as exc:
            raise UserFacingError(str(exc))
        except Exception as exc:  # noqa: BLE001
            raise UserFacingError(downloads.friendly_error(exc))
    raise HTTPException(400, "Unknown model type")


@app.post("/api/models/llm/url")
async def download_custom_llm(request: Request) -> dict[str, Any]:
    data = await _json(request)
    url = (data.get("url") or "").strip()
    m = re.match(r"^https://huggingface\.co/([\w.\-]+/[\w.\-]+)/(?:resolve|blob)/([\w.\-]+)/(.+\.gguf)(\?.*)?$", url)
    if not m:
        raise UserFacingError("Paste a Hugging Face link to a .gguf file, e.g. "
                              "https://huggingface.co/user/model-GGUF/blob/main/model-Q4_K_M.gguf")
    repo, rev, file = m.group(1), m.group(2), m.group(3)
    name = Path(file).name
    files = [{"url": f"https://huggingface.co/{repo}/resolve/{rev}/{file}", "path": name, "size": 0}]
    return downloads.manager.start(f"llm:file:custom/{name}", name, files, paths.LLM_MODELS / "custom")


@app.delete("/api/models/{kind}/{key:path}")
def delete_model(kind: str, key: str) -> dict[str, Any]:
    if kind == "whisper" and catalog.whisper(key):
        downloads.remove(pipeline.whisper_dir(key))
    elif kind == "llm":
        llm_runtime.server.stop()
        if key.startswith("file:"):
            p = paths.LLM_MODELS / key[5:]
            if p.resolve().is_relative_to(paths.LLM_MODELS.resolve()):
                downloads.remove(p)
        elif catalog.llm(key):
            downloads.remove(paths.LLM_MODELS / key)
    elif kind == "speaker" and catalog.speaker_model(key):
        downloads.remove(paths.SPEAKER_MODELS / catalog.speaker_model(key)["file"])
    elif kind == "engine":
        llm_runtime.server.stop()
        for child in paths.LLAMA_DIR.iterdir():
            downloads.remove(child)
    else:
        raise HTTPException(404, "Unknown model")
    return {"ok": True}


@app.get("/api/downloads")
def list_downloads() -> list[dict[str, Any]]:
    return downloads.manager.status()


@app.post("/api/downloads/{task_id:path}/cancel")
def cancel_download(task_id: str) -> dict[str, Any]:
    downloads.manager.cancel(task_id)
    return {"ok": True}


# --- NVIDIA acceleration pack (cuBLAS + cuDNN into the app's own Python) ---------

_gpu_task: dict[str, Any] = {}


@app.post("/api/gpu/install")
def install_gpu_pack() -> dict[str, Any]:
    if _gpu_task.get("status", {}).get("status") == "downloading":
        return _gpu_task["status"]
    uv = _find_uv()
    req = paths.ROOT / "requirements-gpu.txt"
    if not uv or not req.exists():
        raise UserFacingError("Run the start script again to install GPU support "
                              "(it installs automatically when an NVIDIA card is found).")
    status = {"id": "gpu:pack", "name": "NVIDIA acceleration pack", "status": "downloading", "total": 0,
              "done": 0, "speed": 0, "error": None}
    _gpu_task["status"] = status

    def run() -> None:
        env = dict(os.environ)
        env.update(UV_CACHE_DIR=str(paths.RUNTIME / "cache"), UV_LINK_MODE="copy")
        cmd = [str(uv), "pip", "install", "--python", sys.executable, "-r", str(req)]
        if sys.prefix == sys.base_prefix:  # portable build: packages live in the bundled Python itself
            cmd.append("--break-system-packages")
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                 creationflags=0x08000000 if hardware.IS_WINDOWS else 0)
            if out.returncode != 0:
                status.update(status="error", error=(out.stderr or out.stdout)[-600:])
            else:
                status.update(status="done")
                pipeline.set_state(cuda_failed=None)
                hardware.info.cache_clear()
        except OSError as exc:
            status.update(status="error", error=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return status


def _find_uv() -> Optional[Path]:
    for name in ("uv.exe", "uv"):
        for p in (paths.RUNTIME / "uv").rglob(name) if (paths.RUNTIME / "uv").exists() else []:
            return p
    found = shutil.which("uv")
    return Path(found) if found else None


@app.post("/api/gpu/retry")
def retry_gpu() -> dict[str, Any]:
    pipeline.set_state(cuda_failed=None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


def _speaker_rows(mid: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT ms.key, ms.label, ms.speaker_id, ms.status, ms.confidence, ms.suggested_id, ms.color, ms.talk_time, "
        "s.name AS speaker_name, s2.name AS suggested_name FROM meeting_speakers ms "
        "LEFT JOIN speakers s ON s.id = ms.speaker_id LEFT JOIN speakers s2 ON s2.id = ms.suggested_id "
        "WHERE ms.meeting_id = ? ORDER BY CAST(substr(ms.key, 2) AS INTEGER)",
        (mid,),
    )
    for r in rows:
        r["name"] = r["speaker_name"] or r["label"] or r["key"]
    return rows


def _meeting_card(m: dict[str, Any], spk: Optional[list] = None) -> dict[str, Any]:
    meta = db.loads(m.get("notes_meta"), {})
    return {
        "id": m["id"],
        "title": m["title"],
        "created_at": m["created_at"],
        "recorded_at": m["recorded_at"],
        "duration": m["duration"],
        "status": m["status"],
        "stage": m["stage"],
        "progress": m["progress"],
        "error": m["error"],
        "summary": m["summary"],
        "favorite": bool(m["favorite"]),
        "tags": db.loads(m["tags"], []),
        "source": m["source"],
        "language": m["language"],
        "notes_status": meta.get("status") or ("ready" if m.get("notes") else None),
        "speakers": spk or [],
    }


@app.get("/api/meetings")
def list_meetings(q: str = "", favorites: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM meetings"
    args: list[Any] = []
    where = []
    if favorites:
        where.append("favorite = 1")
    if q.strip():
        where.append("(title LIKE ? OR summary LIKE ?)")
        args += [f"%{q.strip()}%", f"%{q.strip()}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(recorded_at, created_at) DESC"
    rows = db.query(sql, args)
    spk: dict[str, list] = {}
    for r in db.query(
        "SELECT ms.meeting_id, ms.key, ms.color, ms.talk_time, COALESCE(s.name, ms.label) AS name "
        "FROM meeting_speakers ms LEFT JOIN speakers s ON s.id = ms.speaker_id ORDER BY ms.talk_time DESC"
    ):
        spk.setdefault(r["meeting_id"], []).append({"name": r["name"], "color": r["color"]})
    return [_meeting_card(m, spk.get(m["id"])) for m in rows]


@app.post("/api/meetings/upload")
async def upload(file: UploadFile = File(...), options: str = Form("{}"), title: str = Form(""),
                 last_modified: float = Form(0)) -> dict[str, Any]:
    name = file.filename or "audio"
    ext = Path(name).suffix.lower()
    if ext and ext not in audio_mod.AUDIO_EXTENSIONS:
        raise UserFacingError(f"'{ext}' files are not supported. Use an audio or video file.")
    tmp = paths.TMP / f"upload-{uuid.uuid4().hex}{ext}"
    tmp.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(file.file, fh, 4 * 1024 * 1024)

    from starlette.concurrency import run_in_threadpool

    await run_in_threadpool(save)
    try:
        opts = json.loads(options or "{}")
    except ValueError:
        opts = {}
    recorded = last_modified / 1000 if last_modified > 1e11 else (last_modified or None)
    mid = pipeline.create_meeting(tmp, name, "upload", opts, title.strip() or None, recorded)
    return {"id": mid}


@app.get("/api/meetings/{mid}")
def get_meeting(mid: str) -> dict[str, Any]:
    m = _meeting(mid)
    segs = db.query(
        "SELECT id, start, end, speaker, text, words, edited FROM segments WHERE meeting_id = ? ORDER BY idx", (mid,)
    )
    for s in segs:
        words = db.loads(s.pop("words"), [])
        s["w"] = [[w[0], w[1], w[2]] for w in words]
    card = _meeting_card(m)
    card.update(
        options=pipeline.meeting_options(m),
        notes=db.loads(m["notes"], None),
        notes_meta=db.loads(m["notes_meta"], {}),
        stats=db.loads(m["stats"], {}),
        speakers=_speaker_rows(mid),
        segments=segs,
        has_audio=(paths.meeting_dir(mid) / "audio.wav").exists(),
        has_diarization=(paths.meeting_dir(mid) / "diarization.npz").exists(),
        learned_samples=(db.one("SELECT COUNT(*) AS n FROM speaker_samples WHERE meeting_id = ?", (mid,)) or {}).get("n", 0),
    )
    return card


@app.get("/api/meetings/{mid}/peaks")
def get_peaks(mid: str) -> Response:
    p = paths.meeting_dir(mid) / "peaks.json"
    if not p.exists():
        raise HTTPException(404, "No waveform yet")
    return Response(p.read_bytes(), media_type="application/json")


@app.get("/api/meetings/{mid}/audio")
def get_audio(mid: str) -> FileResponse:
    p = paths.meeting_dir(mid) / "audio.wav"
    if not p.exists():
        raise HTTPException(404, "No audio yet")
    return FileResponse(p, media_type="audio/wav")


@app.patch("/api/meetings/{mid}")
async def patch_meeting(mid: str, request: Request) -> dict[str, Any]:
    m = _meeting(mid)
    data = await _json(request)
    if "title" in data and str(data["title"]).strip():
        opts = db.loads(m["options"], {})
        opts["title_locked"] = True
        db.execute("UPDATE meetings SET title = ?, options = ? WHERE id = ?",
                   (str(data["title"]).strip()[:200], db.dumps(opts), mid))
    if "favorite" in data:
        db.execute("UPDATE meetings SET favorite = ? WHERE id = ?", (1 if data["favorite"] else 0, mid))
    if "tags" in data and isinstance(data["tags"], list):
        db.execute("UPDATE meetings SET tags = ? WHERE id = ?", (db.dumps([str(t)[:40] for t in data["tags"]][:20]), mid))
    return {"ok": True}


@app.delete("/api/meetings/{mid}")
def delete_meeting(mid: str, forget_voices: bool = False) -> dict[str, Any]:
    _meeting(mid)
    pipeline.delete_meeting(mid, forget_voices)
    return {"ok": True}


@app.post("/api/meetings/{mid}/reprocess")
async def reprocess(mid: str, request: Request) -> dict[str, Any]:
    _meeting(mid)
    data = await _json(request)
    stages = [s for s in data.get("stages", []) if s in pipeline.STAGES]
    if not stages:
        raise UserFacingError("Choose what to redo.")
    job = pipeline.reprocess(mid, stages, data.get("options") or {})
    return {"job": job}


@app.post("/api/meetings/{mid}/cancel")
def cancel_meeting(mid: str) -> dict[str, Any]:
    runner.cancel_meeting(mid)
    return {"ok": True}


# --- speakers within a meeting ---------------------------------------------------


@app.post("/api/meetings/{mid}/speakers/recluster")
async def recluster(mid: str, request: Request) -> dict[str, Any]:
    _meeting(mid)
    data = await _json(request)
    if not (paths.meeting_dir(mid) / "diarization.npz").exists():
        raise UserFacingError("Speaker analysis hasn't run for this meeting yet.")
    n = int(data.get("num_speakers") or 0) or None
    if n == 1:
        pipeline._single_speaker(mid)
    else:
        pipeline.apply_clustering(mid, n)
    opts = db.loads(_meeting(mid)["options"], {})
    opts["num_speakers"] = n or 0
    db.execute("UPDATE meetings SET options = ? WHERE id = ?", (db.dumps(opts), mid))
    pipeline.rebuild_segments(mid)
    return get_meeting(mid)


@app.patch("/api/meetings/{mid}/speakers/{key}")
async def patch_meeting_speaker(mid: str, key: str, request: Request) -> dict[str, Any]:
    _meeting(mid)
    row = db.one("SELECT * FROM meeting_speakers WHERE meeting_id = ? AND key = ?", (mid, key))
    if not row:
        raise HTTPException(404, "Speaker not found")
    data = await _json(request)
    action = data.get("action") or "assign"
    old_name = pipeline.speaker_names(mid).get(key, key)
    learned = 0
    if action == "reject":
        db.execute("UPDATE meeting_speakers SET status = 'unknown', suggested_id = NULL, "
                   "speaker_id = CASE WHEN status = 'auto' THEN NULL ELSE speaker_id END, "
                   "label = CASE WHEN status = 'auto' THEN ? ELSE label END WHERE meeting_id = ? AND key = ?",
                   (f"Speaker {key[1:]}", mid, key))
    elif action == "unlink":
        db.execute("UPDATE meeting_speakers SET status = 'unknown', speaker_id = NULL, label = ? "
                   "WHERE meeting_id = ? AND key = ?", (f"Speaker {key[1:]}", mid, key))
    elif action == "confirm":
        sid = row["speaker_id"] or row["suggested_id"]
        if not sid:
            raise UserFacingError("There is no suggestion to confirm.")
        learned = _link_speaker(mid, key, sid, learn=data.get("learn", True))
    else:
        learn = bool(data.get("learn", True))
        sid = data.get("speaker_id")
        name = (data.get("name") or "").strip()
        if not sid and not name:
            raise UserFacingError("Enter a name.")
        if not sid:
            existing = speakers.find_by_name(name)
            if existing:
                sid = existing["id"]
            elif learn:
                sid = speakers.create_speaker(name)
        if sid:
            learned = _link_speaker(mid, key, int(sid), learn=learn)
        else:
            # a name for this meeting only, don't remember the voice
            db.execute("UPDATE meeting_speakers SET label = ?, speaker_id = NULL, status = 'named', "
                       "suggested_id = NULL WHERE meeting_id = ? AND key = ?", (name, mid, key))
    new_name = pipeline.speaker_names(mid).get(key, key)
    pipeline.rename_in_notes(mid, old_name, new_name)
    result = get_meeting(mid)
    result["learned"] = learned
    return result


def _link_speaker(mid: str, key: str, speaker_id: int, learn: bool) -> int:
    sp = db.one("SELECT * FROM speakers WHERE id = ?", (speaker_id,))
    if not sp:
        raise HTTPException(404, "Person not found")
    # one person per meeting: if they were linked to another speaker key, merge those speakers
    other = db.one("SELECT key FROM meeting_speakers WHERE meeting_id = ? AND speaker_id = ? AND key != ?",
                   (mid, speaker_id, key))
    db.execute("UPDATE meeting_speakers SET speaker_id = ?, status = 'confirmed', suggested_id = NULL, label = ?, "
               "color = ? WHERE meeting_id = ? AND key = ?", (speaker_id, sp["name"], sp["color"], mid, key))
    if other:
        _merge_keys(mid, other["key"], key)
    learned = 0
    if learn:
        try:
            learned = speakers.learn_from_meeting(mid, key, speaker_id)
        except Exception:  # noqa: BLE001
            log.exception("learning failed")
    return learned


def _merge_keys(mid: str, source: str, target: str) -> None:
    db.execute("UPDATE segments SET speaker = ? WHERE meeting_id = ? AND speaker = ?", (target, mid, source))
    src = db.one("SELECT talk_time FROM meeting_speakers WHERE meeting_id = ? AND key = ?", (mid, source))
    if src:
        db.execute("UPDATE meeting_speakers SET talk_time = talk_time + ? WHERE meeting_id = ? AND key = ?",
                   (src["talk_time"] or 0, mid, target))
    db.execute("DELETE FROM meeting_speakers WHERE meeting_id = ? AND key = ?", (mid, source))
    # keep the merge if the transcript is rebuilt later
    turns = pipeline.load_turns(mid)
    if turns and source in turns["order"] and target in turns["order"]:
        si, ti = turns["order"].index(source), turns["order"].index(target)
        for t in turns["turns"]:
            if t["speaker"] == si:
                t["speaker"] = ti
        (paths.meeting_dir(mid) / "turns.json").write_text(json.dumps(turns), "utf-8")


@app.post("/api/meetings/{mid}/speakers/merge")
async def merge_meeting_speakers(mid: str, request: Request) -> dict[str, Any]:
    _meeting(mid)
    data = await _json(request)
    source, target = data.get("source"), data.get("target")
    if not source or not target or source == target:
        raise UserFacingError("Choose two different speakers.")
    old_name = pipeline.speaker_names(mid).get(source, source)
    _merge_keys(mid, source, target)
    pipeline.rename_in_notes(mid, old_name, pipeline.speaker_names(mid).get(target, target))
    return get_meeting(mid)


@app.post("/api/meetings/{mid}/speakers/confirm-all")
def confirm_all(mid: str) -> dict[str, Any]:
    _meeting(mid)
    learned = 0
    for r in db.query("SELECT * FROM meeting_speakers WHERE meeting_id = ?", (mid,)):
        if r["status"] in ("auto", "confirmed") and r["speaker_id"]:
            learned += _link_speaker(mid, r["key"], r["speaker_id"], learn=True)
        elif r["status"] == "suggested" and r["suggested_id"]:
            old = pipeline.speaker_names(mid).get(r["key"], r["key"])
            learned += _link_speaker(mid, r["key"], r["suggested_id"], learn=True)
            pipeline.rename_in_notes(mid, old, pipeline.speaker_names(mid).get(r["key"], r["key"]))
    result = get_meeting(mid)
    result["learned"] = learned
    return result


@app.delete("/api/meetings/{mid}/learning")
def forget_meeting_learning(mid: str) -> dict[str, Any]:
    return {"removed": speakers.forget_meeting(mid)}


# --- transcript edits --------------------------------------------------------------


@app.patch("/api/meetings/{mid}/segments/{sid}")
async def patch_segment(mid: str, sid: int, request: Request) -> dict[str, Any]:
    seg = db.one("SELECT * FROM segments WHERE id = ? AND meeting_id = ?", (sid, mid))
    if not seg:
        raise HTTPException(404, "Segment not found")
    data = await _json(request)
    if "text" in data:
        text = str(data["text"]).strip()
        if text:
            db.execute("UPDATE segments SET text = ?, edited = 1 WHERE id = ?", (text, sid))
    if data.get("speaker"):
        key = data["speaker"]
        if key == "new":
            n = 1 + max([int(r["key"][1:]) for r in db.query(
                "SELECT key FROM meeting_speakers WHERE meeting_id = ?", (mid,)) if r["key"][1:].isdigit()] or [0])
            key = f"S{n}"
            used = {r["color"] for r in db.query("SELECT color FROM meeting_speakers WHERE meeting_id = ?", (mid,))}
            color = next((c for c in speakers.PALETTE if c not in used), speakers.PALETTE[n % 16])
            db.execute("INSERT INTO meeting_speakers (meeting_id, key, label, color, status) VALUES (?, ?, ?, ?, 'unknown')",
                       (mid, key, f"Speaker {n}", color))
        elif not db.one("SELECT key FROM meeting_speakers WHERE meeting_id = ? AND key = ?", (mid, key)):
            raise UserFacingError("Unknown speaker")
        db.execute("UPDATE segments SET speaker = ?, edited = 1 WHERE id = ?", (key, sid))
        dur = seg["end"] - seg["start"]
        target = db.one("SELECT speaker_id FROM meeting_speakers WHERE meeting_id = ? AND key = ?", (mid, key))
        if data.get("learn") and target and target["speaker_id"] and dur >= speakers.SAMPLE_MIN_SECONDS:
            _learn_segment(mid, seg, target["speaker_id"])
        # keep talk time roughly right
        for k, delta in ((seg["speaker"], -dur), (key, dur)):
            db.execute("UPDATE meeting_speakers SET talk_time = MAX(0, talk_time + ?) WHERE meeting_id = ? AND key = ?",
                       (delta, mid, k))
    db.reindex_meeting(mid)
    return {"ok": True}


def _learn_segment(mid: str, seg: dict[str, Any], speaker_id: int) -> None:
    wav = paths.meeting_dir(mid) / "audio.wav"
    if not wav.exists() or not speakers.models_installed():
        return
    end = min(seg["end"], seg["start"] + speakers.SAMPLE_MAX_SECONDS)
    emb = speakers.extractor().embed_long(audio_mod.load_wav(wav, seg["start"], end))
    if emb is not None:
        speakers.add_sample(speaker_id, emb, speakers.current_model_key(), meeting_id=mid, start=seg["start"],
                            end=end, wav=wav, source="correction")


# --- notes ----------------------------------------------------------------------------


@app.patch("/api/meetings/{mid}/notes")
async def patch_notes(mid: str, request: Request) -> dict[str, Any]:
    _meeting(mid)
    data = await _json(request)
    notes = data.get("notes")
    if not isinstance(notes, dict):
        raise UserFacingError("Invalid notes")
    summary = (notes.get("summary") or "").split("\n")[0][:300]
    db.execute("UPDATE meetings SET notes = ?, summary = ? WHERE id = ?", (db.dumps(notes), summary, mid))
    return {"ok": True}


@app.post("/api/meetings/{mid}/notes")
async def regenerate_notes(mid: str, request: Request) -> dict[str, Any]:
    _meeting(mid)
    data = await _json(request)
    opts = {k: data[k] for k in ("template", "llm_model", "notes_detail", "notes_language", "instructions") if k in data}
    opts["notes"] = True
    job = pipeline.reprocess(mid, ["notes"], opts)
    return {"job": job}


@app.get("/api/action-items")
def action_items() -> list[dict[str, Any]]:
    out = []
    for m in db.query("SELECT id, title, recorded_at, created_at, notes FROM meetings WHERE notes IS NOT NULL "
                      "ORDER BY COALESCE(recorded_at, created_at) DESC"):
        notes = db.loads(m["notes"], {})
        for i, a in enumerate(notes.get("action_items") or []):
            out.append({**a, "index": i, "meeting_id": m["id"], "meeting_title": m["title"],
                        "date": m["recorded_at"] or m["created_at"]})
    return out


@app.patch("/api/meetings/{mid}/action-items/{index}")
async def patch_action_item(mid: str, index: int, request: Request) -> dict[str, Any]:
    m = _meeting(mid)
    data = await _json(request)
    notes = db.loads(m["notes"], {})
    items = notes.get("action_items") or []
    if not 0 <= index < len(items):
        raise HTTPException(404, "Action item not found")
    for k in ("done", "task", "owner", "due"):
        if k in data:
            items[index][k] = bool(data[k]) if k == "done" else str(data[k])
    db.execute("UPDATE meetings SET notes = ? WHERE id = ?", (db.dumps(notes), mid))
    return {"ok": True}


# --- ask your meeting ------------------------------------------------------------------


@app.get("/api/meetings/{mid}/chat")
def get_chat(mid: str) -> list[dict[str, Any]]:
    return db.query("SELECT role, content, created_at FROM chat WHERE meeting_id = ? ORDER BY id", (mid,))


@app.delete("/api/meetings/{mid}/chat")
def clear_chat(mid: str) -> dict[str, Any]:
    db.execute("DELETE FROM chat WHERE meeting_id = ?", (mid,))
    return {"ok": True}


@app.post("/api/meetings/{mid}/ask")
async def ask(mid: str, request: Request) -> StreamingResponse:
    m = _meeting(mid)
    data = await _json(request)
    question = (data.get("question") or "").strip()
    if not question:
        raise UserFacingError("Type a question.")
    segs = pipeline.segments_for(mid)
    if not segs:
        raise UserFacingError("This meeting has no transcript yet.")
    history = db.query("SELECT role, content FROM chat WHERE meeting_id = ? ORDER BY id DESC LIMIT 8", (mid,))[::-1]
    q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def worker() -> None:
        try:
            backend = llm_client.get_backend()
            q.put({"type": "status", "message": "Starting the AI engine"})
            backend.prepare(stop.is_set, lambda msg: q.put({"type": "status", "message": msg}))
            q.put({"type": "status", "message": "Thinking"})
            msgs = ask_mod.build_messages(backend, question, segs, pipeline.speaker_names(mid),
                                          db.loads(m["notes"], None), history)
            answer = backend.chat(msgs, max_tokens=1200, temperature=0.3,
                                  on_token=lambda t: q.put({"type": "token", "text": t}), cancelled=stop.is_set)
            now = time.time()
            db.execute("INSERT INTO chat (meeting_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                       (mid, question, now))
            db.execute("INSERT INTO chat (meeting_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                       (mid, answer, now + 0.001))
            q.put({"type": "done", "text": answer})
        except InterruptedError:
            q.put({"type": "done", "text": ""})
        except llm_client.LLMError as exc:
            q.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("ask failed")
            q.put({"type": "error", "message": str(exc)})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def events():
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            stop.set()

    return StreamingResponse(events(), media_type="text/event-stream")


# --- export -----------------------------------------------------------------------------


@app.get("/api/meetings/{mid}/export")
def export(mid: str, format: str = "txt", notes: bool = True, transcript: bool = True, timestamps: bool = True,
           speakers_: bool = Query(True, alias="speakers"), audio: bool = True) -> Response:
    _meeting(mid)
    try:
        body, name, mime = exporters.export(mid, format, notes, transcript, timestamps, speakers_, audio)
    except ValueError as exc:
        raise UserFacingError(str(exc))
    from urllib.parse import quote

    return Response(body, media_type=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---------------------------------------------------------------------------
# Recording (streamed in chunks so hours-long recordings are safe)
# ---------------------------------------------------------------------------


def _rec_dir(rid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", rid):
        raise HTTPException(400, "Bad recording id")
    return paths.RECORDINGS / rid


@app.post("/api/recordings")
async def start_recording(request: Request) -> dict[str, Any]:
    data = await _json(request)
    rid = uuid.uuid4().hex
    d = _rec_dir(rid)
    (d / "chunks").mkdir(parents=True)
    mime = str(data.get("mime") or "audio/webm")
    (d / "meta.json").write_text(json.dumps({"started": time.time(), "mime": mime}), "utf-8")
    return {"id": rid}


@app.post("/api/recordings/{rid}/chunk")
async def recording_chunk(rid: str, seq: int, request: Request) -> dict[str, Any]:
    d = _rec_dir(rid)
    if not d.exists():
        raise HTTPException(404, "Recording not found")
    body = await request.body()
    (d / "chunks" / f"{seq:07d}.part").write_bytes(body)
    return {"ok": True, "bytes": len(body)}


def _rec_ext(mime: str) -> str:
    mime = mime.lower()
    if "ogg" in mime:
        return ".ogg"
    if "mp4" in mime or "aac" in mime:
        return ".m4a"
    if "wav" in mime:
        return ".wav"
    return ".webm"


def _assemble(rid: str) -> tuple[Path, dict[str, Any]]:
    d = _rec_dir(rid)
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    out = d / ("recording" + _rec_ext(meta.get("mime", "")))
    with open(out, "wb") as fh:
        for part in sorted((d / "chunks").glob("*.part")):
            fh.write(part.read_bytes())
    return out, meta


@app.post("/api/recordings/{rid}/finish")
async def finish_recording(rid: str, request: Request) -> dict[str, Any]:
    d = _rec_dir(rid)
    if not d.exists():
        raise HTTPException(404, "Recording not found")
    data = await _json(request)
    out, meta = _assemble(rid)
    if out.stat().st_size < 1000:
        shutil.rmtree(d, ignore_errors=True)
        raise UserFacingError("The recording is empty - check that the microphone works.")
    started = meta.get("started") or time.time()
    title = (data.get("title") or "").strip() or f"Recording {time.strftime('%d %b %Y %H:%M', time.localtime(started))}"
    mid = pipeline.create_meeting(out, "Recording" + out.suffix, "recording", data.get("options") or {},
                                  title if data.get("title") else None, started)
    if not data.get("title"):
        db.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, mid))
    shutil.rmtree(d, ignore_errors=True)
    return {"id": mid}


@app.delete("/api/recordings/{rid}")
def discard_recording(rid: str) -> dict[str, Any]:
    shutil.rmtree(_rec_dir(rid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/recordings/unfinished")
def unfinished_recordings() -> list[dict[str, Any]]:
    out = []
    if paths.RECORDINGS.exists():
        for d in paths.RECORDINGS.iterdir():
            meta_file = d / "meta.json"
            if not d.is_dir() or not meta_file.exists():
                continue
            parts = list((d / "chunks").glob("*.part"))
            if not parts:
                continue
            newest = max(p.stat().st_mtime for p in parts)
            if time.time() - newest < 30:
                continue  # still recording in another window
            meta = json.loads(meta_file.read_text("utf-8"))
            out.append({"id": d.name, "started": meta.get("started"), "bytes": sum(p.stat().st_size for p in parts),
                        "last_chunk": newest})
    return out


# ---------------------------------------------------------------------------
# Speaker library
# ---------------------------------------------------------------------------


@app.get("/api/speakers")
def list_speakers() -> dict[str, Any]:
    return {"speakers": speakers.library(), "model": speakers.current_model_key(),
            "models_installed": speakers.models_installed()}


@app.post("/api/speakers")
async def create_speaker(request: Request) -> dict[str, Any]:
    data = await _json(request)
    name = (data.get("name") or "").strip()
    if not name:
        raise UserFacingError("Enter a name.")
    if speakers.find_by_name(name):
        raise UserFacingError(f"{name} is already in your speaker library.")
    return {"id": speakers.create_speaker(name, data.get("color"))}


@app.patch("/api/speakers/{spk_id}")
async def patch_speaker(spk_id: int, request: Request) -> dict[str, Any]:
    row = db.one("SELECT * FROM speakers WHERE id = ?", (spk_id,))
    if not row:
        raise HTTPException(404, "Person not found")
    data = await _json(request)
    if data.get("name", "").strip():
        name = data["name"].strip()
        clash = speakers.find_by_name(name)
        if clash and clash["id"] != spk_id:
            raise UserFacingError(f"{name} already exists - use Merge to combine them.")
        db.execute("UPDATE speakers SET name = ?, updated_at = ? WHERE id = ?", (name, time.time(), spk_id))
        db.execute("UPDATE meeting_speakers SET label = ? WHERE speaker_id = ?", (name, spk_id))
    if data.get("color") and re.fullmatch(r"#[0-9a-fA-F]{6}", data["color"]):
        db.execute("UPDATE speakers SET color = ? WHERE id = ?", (data["color"], spk_id))
        db.execute("UPDATE meeting_speakers SET color = ? WHERE speaker_id = ?", (data["color"], spk_id))
    if "notes" in data:
        db.execute("UPDATE speakers SET notes = ? WHERE id = ?", (str(data["notes"])[:2000], spk_id))
    return {"ok": True}


@app.delete("/api/speakers/{spk_id}")
def delete_speaker(spk_id: int) -> dict[str, Any]:
    speakers.delete_speaker(spk_id)
    return {"ok": True}


@app.post("/api/speakers/{spk_id}/merge")
async def merge_speaker(spk_id: int, request: Request) -> dict[str, Any]:
    data = await _json(request)
    target = int(data.get("into") or 0)
    if not db.one("SELECT id FROM speakers WHERE id = ?", (target,)):
        raise UserFacingError("Choose who to merge into.")
    speakers.merge_speakers(spk_id, target)
    return {"ok": True}


@app.get("/api/speakers/{spk_id}/samples")
def speaker_samples(spk_id: int) -> list[dict[str, Any]]:
    return speakers.sample_details(spk_id)


@app.get("/api/speakers/{spk_id}/meetings")
def speaker_meetings(spk_id: int) -> list[dict[str, Any]]:
    return db.query(
        "SELECT m.id, m.title, m.recorded_at, m.duration, ms.talk_time FROM meeting_speakers ms "
        "JOIN meetings m ON m.id = ms.meeting_id WHERE ms.speaker_id = ? ORDER BY m.recorded_at DESC",
        (spk_id,),
    )


@app.post("/api/speakers/{spk_id}/cleanup")
def cleanup_speaker(spk_id: int) -> dict[str, Any]:
    return {"removed": speakers.remove_outliers(spk_id)}


@app.post("/api/speakers/{spk_id}/enroll")
async def enroll_speaker(spk_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    if not db.one("SELECT id FROM speakers WHERE id = ?", (spk_id,)):
        raise HTTPException(404, "Person not found")
    if not speakers.models_installed():
        raise UserFacingError("Download the speaker recognition model first (Models page).")
    ext = Path(file.filename or "voice.webm").suffix or ".webm"
    tmp = paths.TMP / f"enroll-{uuid.uuid4().hex}{ext}"
    wav = tmp.with_suffix(".wav")
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    try:
        audio_mod.convert_to_wav(tmp, wav)
        samples = audio_mod.load_wav(wav)
        if len(samples) < 3 * audio_mod.SAMPLE_RATE:
            raise UserFacingError("Please record at least 5 seconds of speech.")
        added = speakers.enroll(spk_id, samples)
    finally:
        tmp.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
    return {"added": added}


@app.delete("/api/samples/{sample_id}")
def delete_sample(sample_id: int) -> dict[str, Any]:
    speakers.delete_sample(sample_id)
    return {"ok": True}


@app.get("/api/samples/{sample_id}/audio")
def sample_audio(sample_id: int) -> FileResponse:
    row = db.one("SELECT clip FROM speaker_samples WHERE id = ?", (sample_id,))
    if not row or not row["clip"] or not paths.absolute(row["clip"]).exists():
        raise HTTPException(404, "No clip")
    return FileResponse(paths.absolute(row["clip"]), media_type="audio/wav")


# ---------------------------------------------------------------------------
# Search, jobs, system
# ---------------------------------------------------------------------------


@app.get("/api/search")
def search(q: str) -> list[dict[str, Any]]:
    q = q.strip()
    if not q:
        return []
    if db.FTS_AVAILABLE:
        terms = [t for t in re.findall(r"[\w']+", q) if t]
        if not terms:
            return []
        match = " ".join(f'"{t}"*' for t in terms)
        try:
            rows = db.query(
                "SELECT meeting_id, segment_id, start, snippet(search_index, 0, '[[', ']]', '…', 14) AS snip "
                "FROM search_index WHERE search_index MATCH ? ORDER BY rank LIMIT 200",
                (match,),
            )
        except Exception:  # noqa: BLE001 - odd query syntax
            rows = []
    else:
        rows = db.query(
            "SELECT meeting_id, id AS segment_id, start, text AS snip FROM segments WHERE text LIKE ? LIMIT 200",
            (f"%{q}%",),
        )
    by: dict[str, dict[str, Any]] = {}
    titles = {m["id"]: m for m in db.query("SELECT id, title, recorded_at, created_at FROM meetings")}
    for r in rows:
        m = titles.get(r["meeting_id"])
        if not m:
            continue
        entry = by.setdefault(r["meeting_id"], {"meeting_id": r["meeting_id"], "title": m["title"],
                                                  "date": m["recorded_at"] or m["created_at"], "hits": []})
        if len(entry["hits"]) < 5:
            entry["hits"].append({"start": r["start"], "segment_id": r["segment_id"], "snippet": r["snip"]})
    return list(by.values())


@app.get("/api/jobs")
def jobs() -> dict[str, Any]:
    return {"jobs": runner.snapshot(), "downloads": downloads.manager.status()}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict[str, Any]:
    return {"ok": runner.cancel(job_id)}


@app.post("/api/open-folder")
async def open_folder(request: Request) -> dict[str, Any]:
    data = await _json(request)
    target = {"data": paths.DATA, "models": paths.MODELS, "app": paths.ROOT}.get(data.get("what"), paths.ROOT)
    if data.get("meeting"):
        target = paths.meeting_dir(str(data["meeting"]))
        if not target.resolve().is_relative_to(paths.MEETINGS.resolve()):
            raise HTTPException(400, "Bad folder")
    try:
        if hardware.IS_WINDOWS:
            os.startfile(str(target))  # noqa: S606
        elif hardware.IS_MAC:
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        raise UserFacingError(f"Could not open the folder: {exc}")
    return {"ok": True, "path": str(target)}


@app.post("/api/shutdown")
def shutdown() -> dict[str, Any]:
    def bye() -> None:
        time.sleep(0.5)
        llm_runtime.shutdown()
        runner.stop()
        os._exit(0)

    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Front-end
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> Response:
    html = (paths.STATIC / "index.html").read_text("utf-8").replace("{{VERSION}}", __version__)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStatic(directory=str(paths.STATIC)), name="static")
