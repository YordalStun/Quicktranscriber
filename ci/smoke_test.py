"""End-to-end test of a built QuickTranscriber folder (run with the bundled Python).

    python ci/smoke_test.py <app folder> [--no-llm] [--launcher]

Downloads small models, transcribes a two-speaker sample, checks speaker
separation, names a speaker (learning), exports every format, installs the AI
engine and writes notes with a tiny model, asks a question, and optionally
starts QuickTranscriber.exe. Everything the test creates is removed afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

APP = Path(sys.argv[1]).resolve()
NO_LLM = "--no-llm" in sys.argv
LAUNCHER = "--launcher" in sys.argv
PORT = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--port=")), "18765"))
B = f"http://127.0.0.1:{PORT}"
H = {"X-QT": "1"}
SAMPLE_URL = "https://raw.githubusercontent.com/pyannote/pyannote-audio/develop/tutorials/assets/sample.wav"
TINY_LLM = "https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF/resolve/main/Qwen3.5-0.8B-Q4_0.gguf"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def python_exe() -> Path:
    for rel in ("runtime/python/python.exe", "runtime/venv/Scripts/python.exe", "runtime/python/bin/python3",
                "runtime/venv/bin/python"):
        p = APP / rel
        if p.exists():
            return p
    return Path(sys.executable)


def wait_for(fn, timeout: float, what: str, interval: float = 1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except requests.RequestException as exc:
            last = exc
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {what} (last: {last})")


def download(kind: str, key: str | None = None, url: str | None = None) -> None:
    if url:
        r = requests.post(B + "/api/models/llm/url", headers=H, json={"url": url})
    else:
        r = requests.post(B + "/api/models/download", headers=H, json={"kind": kind, "key": key})
    assert r.ok, r.text
    task = r.json()["id"]
    log(f"downloading {task}")

    def done():
        tasks = {t["id"]: t for t in requests.get(B + "/api/downloads").json()}
        t = tasks.get(task)
        if t and t["status"] == "error":
            raise AssertionError(f"download failed: {t['error']}")
        return t and t["status"] == "done"

    wait_for(done, 1800, f"download {task}", 2)


def wait_job(meeting_id: str, timeout: float = 1800) -> dict:
    last_msg = None

    def idle():
        nonlocal last_msg
        jobs = requests.get(B + "/api/jobs").json()["jobs"]
        active = [j for j in jobs if j["meeting_id"] == meeting_id and j["status"] in ("queued", "running")]
        if active:
            msg = (active[0].get("stage"), round(active[0].get("progress") or 0, 1), active[0].get("message"))
            if msg != last_msg:
                log(f"  {msg}")
                last_msg = msg
            return False
        return True

    wait_for(idle, timeout, "processing", 2)
    return requests.get(f"{B}/api/meetings/{meeting_id}").json()


def main() -> int:
    py = python_exe()
    log(f"app folder {APP}, python {py}")
    env = dict(os.environ, QT_ROOT=str(APP), PYTHONUTF8="1")
    try:
        requests.get(B + "/api/status", timeout=1)
        raise SystemExit(f"Port {PORT} is already in use - pass --port=<free port>")
    except requests.RequestException:
        pass
    server = subprocess.Popen([str(py), "-m", "quicktranscriber", "--no-browser", "--port", str(PORT)], cwd=APP, env=env)
    ok = False
    try:
        status = wait_for(lambda: requests.get(B + "/api/status", timeout=2).json(), 90, "server start")
        assert status["app"] == "QuickTranscriber"
        log(f"server up: {json.dumps(status['hardware'])[:300]}")
        r = requests.put(B + "/api/settings", headers=H, json={"onboarded": True, "language": "en"})
        assert r.ok

        # security: requests without the app header or from another host are refused
        assert requests.post(B + "/api/shutdown").status_code == 403
        assert requests.get(B + "/api/status", headers={"Host": "evil.example"}).status_code == 403

        download("whisper", "base")
        download("speaker", "titanet_small")

        sample = APP / "data" / "sample.wav"
        sample.write_bytes(requests.get(SAMPLE_URL, timeout=60).content)
        with open(sample, "rb") as fh:
            r = requests.post(B + "/api/meetings/upload", headers=H, files={"file": ("sample.wav", fh)},
                              data={"options": json.dumps({"whisper_model": "base", "language": "en", "notes": False})})
        assert r.ok, r.text
        mid = r.json()["id"]
        m = wait_job(mid)
        assert m["status"] == "ready", f"meeting status {m['status']}: {m.get('error')}"
        log(f"transcript: {len(m['segments'])} lines, {len(m['speakers'])} speakers, stats {m['stats']}")
        assert len(m["segments"]) >= 4, "too few transcript lines"
        assert len(m["speakers"]) == 2, f"expected 2 speakers, got {len(m['speakers'])}"
        text = " ".join(s["text"] for s in m["segments"]).lower()
        assert "new jersey" in text or "chicago" in text, text

        # name a speaker and learn their voice
        r = requests.patch(f"{B}/api/meetings/{mid}/speakers/S1", headers=H, json={"name": "Test Person", "learn": True})
        assert r.ok, r.text
        log(f"learned {r.json().get('learned')} samples")
        people = requests.get(B + "/api/speakers").json()["speakers"]
        assert people and people[0]["name"] == "Test Person"

        # regroup speakers instantly
        r = requests.post(f"{B}/api/meetings/{mid}/speakers/recluster", headers=H, json={"num_speakers": 2})
        assert r.ok, r.text

        for fmt in ("txt", "md", "docx", "html", "srt", "vtt", "json", "zip"):
            r = requests.get(f"{B}/api/meetings/{mid}/export", params={"format": fmt})
            assert r.ok and len(r.content) > 100, f"export {fmt} failed"
        log("exports ok")

        r = requests.get(B + "/api/search", params={"q": "Chicago"})
        assert r.ok and r.json(), "search found nothing"

        if not NO_LLM:
            download("engine")
            engine = requests.get(B + "/api/models").json()["engine"]["installed"]
            log(f"AI engine: {engine}")
            assert engine, "llama.cpp engine not installed"
            download("llm", url=TINY_LLM)
            r = requests.post(f"{B}/api/meetings/{mid}/notes", headers=H,
                              json={"llm_model": "file:custom/Qwen3.5-0.8B-Q4_0.gguf", "notes_detail": "brief"})
            assert r.ok, r.text
            m = wait_job(mid, 1800)
            meta = m["notes_meta"]
            log(f"notes meta: {meta}")
            assert m["status"] == "ready", m.get("error")
            assert meta.get("status") == "ready", meta
            assert m["notes"] and m["notes"].get("summary"), m["notes"]
            log(f"notes summary: {m['notes']['summary'][:300]}")

            answer = []
            with requests.post(f"{B}/api/meetings/{mid}/ask", headers=H, json={"question": "Where are the speakers from?"},
                               stream=True, timeout=600) as resp:
                resp.encoding = "utf-8"
                for line in resp.iter_lines(decode_unicode=True):
                    if line and line.startswith("data:"):
                        ev = json.loads(line[5:])
                        if ev["type"] == "token":
                            answer.append(ev["text"])
                        if ev["type"] == "error":
                            raise AssertionError(ev["message"])
            log(f"answer: {''.join(answer)[:300]}")
            assert "".join(answer).strip(), "empty answer"

        requests.delete(f"{B}/api/meetings/{mid}", headers=H)
        ok = True
    finally:
        try:
            requests.post(B + "/api/shutdown", headers=H, timeout=5)
        except requests.RequestException:
            pass
        try:
            server.wait(20)
        except subprocess.TimeoutExpired:
            server.kill()
        if not ok:
            for name in ("app.log", "llama-server.log"):
                p = APP / "data" / "logs" / name
                if p.exists():
                    print(f"----- {name} -----")
                    print(p.read_text("utf-8", errors="replace")[-8000:])

    if LAUNCHER and os.name == "nt":
        exe = APP / "QuickTranscriber.exe"
        log("starting QuickTranscriber.exe")
        proc = subprocess.Popen([str(exe)], cwd=APP)
        try:
            status = wait_for(lambda: requests.get("http://127.0.0.1:8765/api/status", timeout=2).json(), 120,
                              "launcher start")
            assert status["app"] == "QuickTranscriber"
            log("launcher started the app")
            requests.post("http://127.0.0.1:8765/api/shutdown", headers=H, timeout=5)
            proc.wait(60)
            log(f"launcher exited with {proc.returncode}")
        finally:
            if proc.poll() is None:
                proc.kill()

    # leave the folder exactly as it was built
    for rel in ("data", "models", "runtime/llama.cpp", "runtime/tmp"):
        shutil.rmtree(APP / rel, ignore_errors=True)
    for p in APP.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    log("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
