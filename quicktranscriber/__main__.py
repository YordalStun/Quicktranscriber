"""Start QuickTranscriber: python -m quicktranscriber [--port 8765] [--no-browser]"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import APP_NAME, __version__, paths

DEFAULT_PORT = 8765


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _already_running(port: int) -> bool:
    try:
        import requests

        r = requests.get(f"http://127.0.0.1:{port}/api/status", timeout=2)
        return r.ok and r.json().get("app") == APP_NAME
    except Exception:  # noqa: BLE001
        return False


def _setup_logging(verbose: bool) -> None:
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.LOGS / "app.log", maxBytes=5_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.WARNING if not verbose else logging.DEBUG)
    root.addHandler(ch)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _app_window(url: str) -> bool:
    """Open the UI in a clean app-style window (Edge/Chrome), falling back to the browser."""
    candidates: list[str] = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            candidates += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                           str(Path(base) / "Google/Chrome/Application/chrome.exe")]
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            p = Path(f"/Applications/{app}.app/Contents/MacOS/{app}")
            candidates.append(str(p))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for exe in candidates:
        if Path(exe).exists():
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1440,920"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    return webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--port", type=int, default=int(os.environ.get("QT_PORT", DEFAULT_PORT)))
    parser.add_argument("--no-browser", action="store_true", help="don't open a window")
    parser.add_argument("--browser", action="store_true", help="open in the normal browser, not an app window")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    paths.keep_everything_local()
    paths.ensure_dirs()
    if sys.stdout is None or sys.stderr is None:
        # started without a console (pythonw / QuickTranscriber.exe): keep output in a log file
        console = open(paths.LOGS / "console.log", "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or console
        sys.stderr = sys.stderr or console
    _setup_logging(args.verbose)

    port = args.port
    url = f"http://127.0.0.1:{port}"
    if not _port_free(port):
        if _already_running(port):
            print(f"{APP_NAME} is already running - opening it.")
            if not args.no_browser:
                _app_window(url) if not args.browser else webbrowser.open(url)
            return
        for p in range(port + 1, port + 50):
            if _port_free(p):
                port = p
                break
        url = f"http://127.0.0.1:{port}"

    from . import db
    from .jobs import runner
    from .llm import runtime
    from . import pipeline  # noqa: F401  (registers the job handler)
    from .server import app

    db.init()
    runtime.kill_orphan()
    runner.start()
    from . import watcher

    watcher.start()

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                            timeout_keep_alive=30, log_config=None)
    server = uvicorn.Server(config)

    def open_when_ready() -> None:
        for _ in range(100):
            if not _port_free(port):
                break
            time.sleep(0.1)
        if not args.no_browser:
            if args.browser:
                webbrowser.open(url)
            else:
                _app_window(url)

    banner = (
        f"\n  {APP_NAME} {__version__} is running at {url}\n"
        f"  Everything stays on this computer, in {paths.ROOT}\n"
        f"  Keep this window open while you use the app. Close it (or press Ctrl+C) to quit.\n"
    )
    print(banner, flush=True)
    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.run()
    finally:
        runtime.shutdown()
        runner.stop()


if __name__ == "__main__":
    main()
