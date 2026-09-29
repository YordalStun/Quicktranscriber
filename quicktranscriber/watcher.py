"""Watch folder: automatically transcribe new recordings that appear in a folder.

Handy when phone recordings sync to the PC (OneDrive, Google Drive, Dropbox,
a USB recorder...). Files already in the folder when watching starts are
skipped unless the user asks to import them.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import paths, settings
from .audio import AUDIO_EXTENSIONS

log = logging.getLogger("qt.watch")

STATE = paths.DATA / "watch.json"
SCAN_SECONDS = 30
SETTLE_SECONDS = 20  # a file must be unchanged this long (still syncing/recording otherwise)


def _load() -> dict:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except (OSError, ValueError):
        return {"folder": "", "seen": []}


def _save(state: dict) -> None:
    STATE.write_text(json.dumps(state), "utf-8")


def _key(p: Path) -> str:
    st = p.stat()
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


def _audio_files(folder: Path) -> list[Path]:
    try:
        return sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
             and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return []


def baseline(folder: str) -> int:
    """Remember the files already in ``folder`` so only new ones are imported."""
    files = _audio_files(Path(folder)) if folder else []
    _save({"folder": folder, "seen": [_key(p) for p in files]})
    return len(files)


def import_existing() -> int:
    """Forget the baseline so files already in the folder are imported too."""
    state = _load()
    state["seen"] = []
    _save(state)
    return len(_audio_files(Path(state["folder"]))) if state["folder"] else 0


def status() -> dict:
    s = settings.load()
    folder = s.get("watch_folder") or ""
    ok = bool(folder) and Path(folder).is_dir()
    state = _load()
    return {"enabled": bool(s.get("watch_enabled")), "folder": folder, "exists": ok,
            "files": len(_audio_files(Path(folder))) if ok else 0, "imported": len(state.get("seen", []))}


def scan_once() -> int:
    from .pipeline import create_meeting

    s = settings.load()
    folder = s.get("watch_folder") or ""
    if not s.get("watch_enabled") or not folder or not Path(folder).is_dir():
        return 0
    state = _load()
    if state.get("folder") != folder:
        baseline(folder)
        return 0
    seen = set(state.get("seen", []))
    added = 0
    for p in _audio_files(Path(folder)):
        try:
            key = _key(p)
            if key in seen or time.time() - p.stat().st_mtime < SETTLE_SECONDS:
                continue
            tmp = paths.TMP / f"watch-{uuid.uuid4().hex}{p.suffix.lower()}"
            shutil.copy2(p, tmp)
            create_meeting(tmp, p.name, "watch", {}, None, p.stat().st_mtime)
            seen.add(key)
            added += 1
            log.info("Imported %s from the watch folder", p.name)
        except OSError:
            log.warning("Could not import %s", p, exc_info=True)
    if added:
        state["seen"] = sorted(seen)
        _save(state)
    return added


def start() -> None:
    def loop() -> None:
        while True:
            try:
                scan_once()
            except Exception:  # noqa: BLE001 - never let the watcher die
                log.exception("watch folder scan failed")
            time.sleep(SCAN_SECONDS)

    threading.Thread(target=loop, daemon=True, name="watch-folder").start()
