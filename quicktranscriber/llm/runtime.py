"""Manages the bundled llama.cpp server (llama-server).

The server binary is downloaded from the official llama.cpp GitHub releases
into runtime/llama.cpp/ the first time it's needed. The right build is picked
for the computer: CUDA for NVIDIA GPUs, Metal on Apple Silicon, CPU otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .. import hardware, paths, settings
from ..downloads import USER_AGENT, manager

log = logging.getLogger("qt.llm.runtime")

RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
STATE_FILE = paths.LLAMA_DIR / "installed.json"
PID_FILE = paths.LLAMA_DIR / "server.pid"
IDLE_SHUTDOWN_SECONDS = 15 * 60
BINARY_NAMES = ("llama-server.exe", "llama-server")
OTHER_BACKENDS = ("cuda", "vulkan", "hip", "sycl", "opencl", "kompute", "rocm", "musa", "openvino", "cann", "radeon")


class RuntimeError_(Exception):
    pass


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _os_arch() -> tuple[str, str]:
    osname = "windows" if hardware.IS_WINDOWS else "mac" if hardware.IS_MAC else "linux"
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return osname, arch


def preferred_backend() -> str:
    choice = settings.get("llm_device")
    osname, _ = _os_arch()
    if osname == "mac":
        return "metal"
    if choice == "cpu":
        return "cpu"
    hw = hardware.info()
    if hw["nvidia"] and osname == "windows":
        return "cuda"
    if hw["nvidia"] and osname == "linux":
        return "vulkan"
    if choice == "gpu":
        return "vulkan"
    return "cpu"


def _cuda_version(name: str) -> Optional[tuple[int, int]]:
    m = re.search(r"cuda-?(\d+)\.(\d+)", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def pick_assets(assets: list[dict[str, Any]], osname: str, arch: str, backend: str,
                cuda_max: Optional[tuple[int, int]] = None) -> list[dict[str, Any]]:
    """Choose the release archive(s) for this computer from a GitHub release."""
    os_tokens = {"windows": ("-win-",), "mac": ("-macos-",), "linux": ("-ubuntu-", "-linux-")}[osname]
    arch_tokens = ("arm64", "aarch64") if arch == "arm64" else ("x64", "x86_64", "amd64")

    def is_archive(n: str) -> bool:
        return n.endswith((".zip", ".tar.gz", ".tgz"))

    main = [
        a for a in assets
        if a["name"].startswith("llama-") and "-bin-" in a["name"] and is_archive(a["name"])
        and any(t in a["name"] for t in os_tokens) and any(t in a["name"] for t in arch_tokens)
    ]
    if backend == "cuda":
        cands = [a for a in main if "cuda" in a["name"]]
        if cuda_max:
            cands = [a for a in cands if (_cuda_version(a["name"]) or (99, 0)) <= cuda_max]
        if not cands:
            return []
        best = max(cands, key=lambda a: _cuda_version(a["name"]) or (0, 0))
        ver = _cuda_version(best["name"])
        out = [best]
        if ver:
            cudart = [
                a for a in assets
                if a["name"].startswith("cudart") and _cuda_version(a["name"]) == ver
                and any(t in a["name"] for t in os_tokens) and is_archive(a["name"])
            ]
            out += cudart[:1]
        return out
    if backend == "vulkan":
        cands = [a for a in main if "vulkan" in a["name"]]
        return cands[:1]
    # cpu / metal: the plain build
    plain = [a for a in main if not any(b in a["name"] for b in OTHER_BACKENDS)]
    plain.sort(key=lambda a: ("-cpu-" not in a["name"], len(a["name"])))
    return plain[:1]


def latest_release() -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")  # only set in automated builds (avoids rate limits)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(RELEASES_API, headers=headers, timeout=30)
    if r.status_code == 403:
        raise RuntimeError_("GitHub is rate-limiting downloads right now. Please try again in an hour.")
    r.raise_for_status()
    return r.json()


def installed() -> Optional[dict[str, Any]]:
    custom = settings.get("llama_server_path")
    if custom and Path(custom).exists():
        return {"binary": str(Path(custom)), "backend": "custom", "tag": "custom"}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text("utf-8"))
            binary = paths.ROOT / state["binary"]
            if binary.exists():
                state["binary"] = str(binary)
                return state
        except (OSError, ValueError, KeyError):
            pass
    found = find_binary(paths.LLAMA_DIR)
    if found:
        return {"binary": str(found), "backend": "unknown", "tag": "manual"}
    return None


def find_binary(folder: Path) -> Optional[Path]:
    if not folder.exists():
        return None
    for name in BINARY_NAMES:
        hits = sorted(folder.rglob(name), key=lambda p: len(p.parts))
        for h in hits:
            if h.is_file():
                return h
    return None


def install(backend: Optional[str] = None) -> dict[str, Any]:
    """Start downloading llama-server. Returns the download task."""
    backend = backend or preferred_backend()
    osname, arch = _os_arch()
    release = latest_release()
    tag = release.get("tag_name", "latest")
    assets = [{"name": a["name"], "url": a["browser_download_url"], "size": a.get("size", 0)}
              for a in release.get("assets", [])]
    search_backend = "cpu" if backend == "metal" else backend
    chosen = pick_assets(assets, osname, arch, search_backend, hardware.cuda_driver_version())
    if not chosen and backend != "cpu":
        backend = "cpu"
        chosen = pick_assets(assets, osname, arch, "cpu")
    if not chosen:
        raise RuntimeError_(
            "Could not find a llama.cpp build for this computer. You can download llama-server manually "
            "and choose it in Settings."
        )
    dest = paths.LLAMA_DIR / f"{tag}-{backend}"
    files = [{"url": a["url"], "path": a["name"], "size": a["size"], "extract": True} for a in chosen]

    def done() -> None:
        binary = find_binary(dest)
        if not binary:
            raise RuntimeError_("The downloaded llama.cpp archive did not contain llama-server")
        if not hardware.IS_WINDOWS:
            binary.chmod(binary.stat().st_mode | 0o755)
        # the CUDA runtime DLLs must sit next to llama-server.exe
        for dll in dest.glob("*.dll"):
            target = binary.parent / dll.name
            if not target.exists():
                try:
                    dll.replace(target)
                except OSError:
                    pass
        STATE_FILE.write_text(json.dumps({"binary": paths.rel(binary), "backend": backend, "tag": tag}), "utf-8")
        server.stop()

    return manager.start("runtime:llama.cpp", f"AI engine (llama.cpp {tag}, {backend.upper()})", files, dest, done)


# ---------------------------------------------------------------------------
# Server process
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self.key: Optional[tuple] = None
        self.n_ctx = 0
        self.last_used = 0.0
        self.lock = threading.RLock()
        self._watchdog: Optional[threading.Thread] = None
        self.log_path = paths.LOGS / "llama-server.log"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def touch(self) -> None:
        self.last_used = time.time()

    def ensure(self, model_path: Path, ctx: int, gpu: bool, threads: int,
               cancelled=None, on_status=None) -> str:
        info = installed()
        if not info:
            raise RuntimeError_("The AI engine (llama.cpp) is not installed yet.")
        key = (str(model_path), ctx, gpu, info["binary"])
        with self.lock:
            if self.running() and self.key == key:
                self.touch()
                return self.url
            self.stop()
            kill_orphan()
            self.port = _free_port()
            cmd = [
                info["binary"], "-m", str(model_path), "-c", str(ctx), "--host", "127.0.0.1",
                "--port", str(self.port), "-np", "1", "--jinja", "--no-webui", "-t", str(threads),
            ]
            if not gpu:
                cmd += ["-ngl", "0"]
            env = dict(os.environ)
            bindir = str(Path(info["binary"]).parent)
            if hardware.IS_LINUX:
                env["LD_LIBRARY_PATH"] = os.pathsep.join([bindir, env.get("LD_LIBRARY_PATH", "")])
            elif hardware.IS_MAC:
                env["DYLD_LIBRARY_PATH"] = os.pathsep.join([bindir, env.get("DYLD_LIBRARY_PATH", "")])
            paths.LOGS.mkdir(parents=True, exist_ok=True)
            logf = open(self.log_path, "w", encoding="utf-8", errors="replace")
            log.info("Starting llama-server: %s", " ".join(cmd))
            kwargs: dict[str, Any] = {}
            if hardware.IS_WINDOWS:
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            try:
                self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=bindir, env=env, **kwargs)
            except OSError as exc:
                raise RuntimeError_(f"Could not start the AI engine: {exc}") from exc
            PID_FILE.write_text(str(self.proc.pid))
            self.key = key
            self._wait_ready(cancelled, on_status)
            self.touch()
            self._start_watchdog()
            return self.url

    def _wait_ready(self, cancelled, on_status) -> None:
        deadline = time.time() + 900  # large models can take minutes to load from a slow disk
        while time.time() < deadline:
            if cancelled and cancelled():
                self.stop()
                raise InterruptedError("cancelled")
            if self.proc.poll() is not None:
                tail = self.log_tail()
                self.proc = None
                raise RuntimeError_(explain_failure(tail))
            try:
                r = requests.get(self.url + "/health", timeout=2)
                if r.status_code == 200:
                    props = requests.get(self.url + "/props", timeout=5).json()
                    self.n_ctx = int(
                        props.get("default_generation_settings", {}).get("n_ctx") or props.get("n_ctx") or 0
                    )
                    return
                if on_status:
                    on_status("Loading the AI model into memory")
            except requests.RequestException:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError_("The AI engine took too long to start.")

    def log_tail(self, lines: int = 30) -> str:
        try:
            return "\n".join(self.log_path.read_text("utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def stop(self) -> None:
        with self.lock:
            if self.proc is not None:
                try:
                    self.proc.terminate()
                    self.proc.wait(10)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
                self.proc = None
            self.key = None
            PID_FILE.unlink(missing_ok=True)

    def _start_watchdog(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return

        def watch() -> None:
            while True:
                time.sleep(30)
                with self.lock:
                    if not self.running():
                        return
                    if time.time() - self.last_used > IDLE_SHUTDOWN_SECONDS:
                        log.info("Stopping idle AI engine to free memory")
                        self.stop()
                        return

        self._watchdog = threading.Thread(target=watch, daemon=True, name="llama-watchdog")
        self._watchdog.start()


def kill_orphan() -> None:
    """Stop a llama-server left behind by a previous crash of the app."""
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return
    try:
        import psutil

        p = psutil.Process(pid)
        if "llama-server" in p.name().lower():
            p.kill()
    except Exception:  # noqa: BLE001 - process already gone
        pass
    PID_FILE.unlink(missing_ok=True)


def explain_failure(log_tail: str) -> str:
    t = log_tail.lower()
    if "out of memory" in t or "failed to allocate" in t or "cudamalloc failed" in t or "unable to allocate" in t:
        return "Not enough memory to load this AI model. Choose a smaller model in Settings."
    if "unknown model architecture" in t or "unsupported model" in t:
        return "This AI model is too new for the installed engine. Update the AI engine in Settings > Models."
    if "failed to load model" in t or "error loading model" in t:
        return "The AI model file could not be loaded (it may be damaged). Delete and download it again."
    if "cudart" in t or ".dll" in t:
        return "The GPU version of the AI engine could not start. Reinstall the engine with CPU in Settings."
    return "The AI engine stopped while starting.\n" + log_tail[-800:]


server = LlamaServer()


def shutdown() -> None:
    server.stop()


def _on_exit(*_args) -> None:
    shutdown()


if hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread():
    try:
        signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))
    except ValueError:
        pass
