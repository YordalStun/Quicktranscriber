"""Heavy AI work runs in a separate process.

Keeping speech recognition and speaker analysis out of the web server process
means a GPU driver crash or running out of memory can never take the app
down, memory is fully released after each job, and "Cancel" can stop work
instantly.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
import traceback
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------
# Parent side
# --------------------------------------------------------------------------


class StageError(Exception):
    def __init__(self, message: str, kind: str = "error", details: str = ""):
        super().__init__(message)
        self.kind = kind
        self.details = details


class StageCancelled(Exception):
    pass


def run_in_child(
    stage: str,
    params: dict[str, Any],
    on_message: Callable[[dict[str, Any]], None],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Run ``stage`` in a fresh process, relaying progress messages."""
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    env_backup = None
    if sys.platform.startswith("linux") and params.get("device") == "cuda":
        # cuDNN loads its sub-libraries by name, so the child needs LD_LIBRARY_PATH
        from . import hardware

        env_backup = dict(os.environ)
        os.environ.update(hardware.linux_cuda_env())
    try:
        proc = ctx.Process(target=_child_main, args=(child_conn, stage, params), daemon=True)
        proc.start()
    finally:
        if env_backup is not None:
            os.environ.clear()
            os.environ.update(env_backup)
    child_conn.close()
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    try:
        while True:
            if cancelled():
                proc.terminate()
                proc.join(5)
                raise StageCancelled()
            if parent_conn.poll(0.25):
                try:
                    msg = parent_conn.recv()
                except EOFError:
                    break
                kind = msg.get("type")
                if kind == "result":
                    result = msg["data"]
                elif kind == "error":
                    error = msg
                else:
                    on_message(msg)
            elif not proc.is_alive():
                # drain anything left in the pipe
                while parent_conn.poll(0):
                    try:
                        msg = parent_conn.recv()
                    except EOFError:
                        break
                    if msg.get("type") == "result":
                        result = msg["data"]
                    elif msg.get("type") == "error":
                        error = msg
                    else:
                        on_message(msg)
                break
    finally:
        parent_conn.close()
        proc.join(10)
        if proc.is_alive():
            proc.kill()
    if error:
        raise StageError(error.get("message", "Unknown error"), error.get("kind", "error"), error.get("details", ""))
    if result is None:
        code = proc.exitcode
        hint = ""
        if params.get("device") == "cuda":
            raise StageError(
                "The GPU stopped unexpectedly (exit code %s)." % code, kind="cuda",
            )
        if code and code < 0 or code in (3221225477, -1073741819, 137, -9):
            hint = " The computer may have run out of memory - try a smaller model."
        raise StageError(f"The processing engine stopped unexpectedly (exit code {code}).{hint}", kind="crash")
    return result


# --------------------------------------------------------------------------
# Child side
# --------------------------------------------------------------------------


def _child_main(conn, stage: str, params: dict[str, Any]) -> None:
    def send(msg: dict[str, Any]) -> None:
        try:
            conn.send(msg)
        except (BrokenPipeError, OSError):
            os._exit(1)

    try:
        from . import paths

        paths.keep_everything_local()
        if params.get("device") == "cuda":
            from . import hardware

            hardware.prepare_cuda_libraries()
        if stage == "transcribe":
            data = _transcribe(params, send)
        elif stage == "diarize":
            data = _diarize(params, send)
        else:
            raise ValueError(f"unknown stage {stage}")
        send({"type": "result", "data": data})
    except Exception as exc:  # noqa: BLE001
        text = f"{exc.__class__.__name__}: {exc}"
        kind = "cuda" if _looks_like_cuda_error(text) else "error"
        send({"type": "error", "message": str(exc) or exc.__class__.__name__, "kind": kind,
              "details": traceback.format_exc()})
    finally:
        conn.close()


def _looks_like_cuda_error(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("cuda", "cublas", "cudnn", "gpu", "out of memory", "no kernel image"))


def _transcribe(p: dict[str, Any], send) -> dict[str, Any]:
    import numpy as np
    from faster_whisper import WhisperModel

    from .audio import load_wav

    send({"type": "progress", "value": 0.0, "message": "Loading the speech model"})
    t0 = time.time()
    model = WhisperModel(
        p["model_dir"],
        device=p["device"],
        compute_type=p["compute_type"],
        cpu_threads=p.get("threads", 0) if p["device"] == "cpu" else 0,
        num_workers=1,
    )
    load_seconds = time.time() - t0
    audio = load_wav(p["wav"])
    duration = len(audio) / 16000.0
    quality = p.get("quality", "accurate")
    beam = {"fast": 1, "accurate": 5, "max": 5}.get(quality, 5)
    options: dict[str, Any] = dict(
        language=p.get("language") or None,
        task="transcribe",
        beam_size=beam,
        best_of=5 if beam > 1 else 1,
        patience=1.0,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 1000, "speech_pad_ms": 300},
        condition_on_previous_text=(quality == "max"),
        hallucination_silence_threshold=2.0,
        hotwords=p.get("vocabulary") or None,
    )
    send({"type": "progress", "value": 0.01, "message": "Detecting the language" if not options["language"] else "Transcribing"})
    segments_iter, info = model.transcribe(audio, **options)
    send({"type": "info", "language": info.language, "probability": float(info.language_probability),
          "speech_seconds": float(info.duration_after_vad or 0)})
    segments = []
    last_send = 0.0
    t1 = time.time()
    for seg in segments_iter:
        words = [
            [round(float(w.start), 3), round(float(w.end), 3), w.word, round(float(w.probability), 3)]
            for w in (seg.words or [])
        ]
        segments.append(
            {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": seg.text.strip(),
                "words": words,
                "avg_logprob": round(float(seg.avg_logprob), 3),
                "no_speech_prob": round(float(seg.no_speech_prob), 3),
            }
        )
        now = time.time()
        if now - last_send > 0.7:
            last_send = now
            send({"type": "progress", "value": min(0.999, seg.end / max(duration, 1)),
                  "message": "Transcribing", "partial": seg.text.strip(), "at": float(seg.end)})
    elapsed = time.time() - t1
    result = {
        "language": info.language,
        "language_probability": float(info.language_probability),
        "duration": duration,
        "segments": segments,
        "model": p.get("model_key"),
        "device": p["device"],
        "compute_type": p["compute_type"],
        "seconds": elapsed,
        "load_seconds": load_seconds,
    }
    tmp = p["out"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    os.replace(tmp, p["out"])
    del model
    return {"language": info.language, "segments": len(segments), "seconds": elapsed, "duration": duration,
            "words": int(np.sum([len(s["words"]) for s in segments])) if segments else 0}


def _diarize(p: dict[str, Any], send) -> dict[str, Any]:
    from . import diarization as D
    from .audio import load_wav

    send({"type": "progress", "value": 0.0, "message": "Loading speaker models"})
    threads = int(p.get("threads") or 2)
    segmenter = D.Segmenter(p["segmentation_model"], num_threads=threads)
    extractor = D.EmbeddingExtractor(p["embedding_model"], num_threads=threads)
    audio = load_wav(p["wav"])
    t0 = time.time()

    def progress(value: float, message: str) -> None:
        send({"type": "progress", "value": value, "message": message})

    data = D.analyze(
        audio,
        segmenter,
        extractor,
        step_seconds=float(p.get("step", 2.0)),
        progress=progress,
        embedding_model=p.get("embedding_key", ""),
    )
    tmp = p["out"] + ".tmp.npz"
    data.save(tmp)
    os.replace(tmp, p["out"])
    return {"seconds": time.time() - t0, "chunks": data.num_chunks}
