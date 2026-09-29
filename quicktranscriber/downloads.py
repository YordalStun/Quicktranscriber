"""Background downloads with progress, resume and archive extraction."""

from __future__ import annotations

import logging
import shutil
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from . import __version__

log = logging.getLogger("qt.downloads")

USER_AGENT = f"QuickTranscriber/{__version__}"
CHUNK = 1024 * 1024


class DownloadCancelled(Exception):
    pass


class DownloadManager:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # -- public -------------------------------------------------------------

    def start(
        self,
        task_id: str,
        name: str,
        files: list[dict[str, Any]],
        dest: Path,
        on_done: Optional[Callable[[], None]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing and existing["status"] in ("queued", "downloading", "extracting"):
                return dict(existing)
            task = {
                "id": task_id,
                "name": name,
                "status": "queued",
                "total": sum(int(f.get("size") or 0) for f in files),
                "done": 0,
                "speed": 0.0,
                "error": None,
                "started": time.time(),
            }
            self._tasks[task_id] = task
            self._cancel[task_id] = threading.Event()
        threading.Thread(
            target=self._run, args=(task_id, files, Path(dest), on_done), daemon=True, name=f"dl-{task_id}"
        ).start()
        return dict(task)

    def cancel(self, task_id: str) -> None:
        ev = self._cancel.get(task_id)
        if ev:
            ev.set()

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._tasks.values()]

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            t = self._tasks.get(task_id)
            return dict(t) if t else None

    def active(self, task_id: str) -> bool:
        t = self.get(task_id)
        return bool(t and t["status"] in ("queued", "downloading", "extracting"))

    def clear_finished(self) -> None:
        with self._lock:
            for k in [k for k, t in self._tasks.items() if t["status"] in ("done", "cancelled")]:
                self._tasks.pop(k, None)

    # -- worker -------------------------------------------------------------

    def _update(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(changes)

    def _run(self, task_id: str, files: list[dict[str, Any]], dest: Path, on_done) -> None:
        cancel = self._cancel[task_id]
        dest.mkdir(parents=True, exist_ok=True)
        done_before = 0
        try:
            self._update(task_id, status="downloading")
            for f in files:
                target = dest / f["path"]
                if f.get("extract") and (dest / (f["path"] + ".extracted")).exists():
                    done_before += int(f.get("size") or 0)
                    continue
                if not f.get("extract") and target.exists() and (
                    not f.get("size") or target.stat().st_size == int(f["size"])
                ):
                    done_before += target.stat().st_size
                    self._update(task_id, done=done_before)
                    continue
                size = self._fetch(task_id, f["url"], target, int(f.get("size") or 0), done_before, cancel)
                done_before += size
                if f.get("extract"):
                    self._update(task_id, status="extracting")
                    extract_archive(target, dest)
                    target.unlink(missing_ok=True)
                    (dest / (f["path"] + ".extracted")).write_text("ok")
                    self._update(task_id, status="downloading")
            self._update(task_id, status="done", done=done_before, speed=0.0)
            if on_done:
                on_done()
        except DownloadCancelled:
            self._update(task_id, status="cancelled", speed=0.0)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            log.exception("Download %s failed", task_id)
            self._update(task_id, status="error", error=friendly_error(exc), speed=0.0)

    def _fetch(
        self, task_id: str, url: str, target: Path, expected: int, done_before: int, cancel: threading.Event
    ) -> int:
        part = target.with_name(target.name + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        attempts = 0
        while True:
            attempts += 1
            have = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": USER_AGENT}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(20, 60), allow_redirects=True) as r:
                    if r.status_code == 416:  # already complete
                        break
                    r.raise_for_status()
                    if have and r.status_code != 206:
                        have = 0  # server ignored the range request: restart
                    total = have + int(r.headers.get("Content-Length") or 0)
                    if total and total != expected:
                        with self._lock:
                            t = self._tasks[task_id]
                            t["total"] = t["total"] - expected + total
                        expected = total
                    mode = "ab" if have else "wb"
                    last_t, last_b, speed = time.time(), have, 0.0
                    with open(part, mode) as fh:
                        got = have
                        for chunk in r.iter_content(CHUNK):
                            if cancel.is_set():
                                raise DownloadCancelled()
                            if not chunk:
                                continue
                            fh.write(chunk)
                            got += len(chunk)
                            now = time.time()
                            if now - last_t >= 0.5:
                                inst = (got - last_b) / (now - last_t)
                                speed = inst if speed == 0 else speed * 0.7 + inst * 0.3
                                last_t, last_b = now, got
                                self._update(task_id, done=done_before + got, speed=speed)
                break
            except DownloadCancelled:
                raise
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempts >= 5:
                    raise
                time.sleep(2**attempts)
        size = part.stat().st_size
        if expected and size != expected:
            raise RuntimeError(f"Download incomplete ({size} of {expected} bytes). Please try again.")
        part.replace(target)
        self._update(task_id, done=done_before + size)
        return size


def _inside(dest: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(dest)
        return True
    except ValueError:
        return False


def extract_archive(archive: Path, dest: Path) -> None:
    """Extract .zip / .tar.* safely (no paths escaping ``dest``), keeping symlinks
    and executable bits so native libraries work on macOS/Linux."""
    import os
    import stat

    name = archive.name.lower()
    dest = dest.resolve()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                out = dest / member.filename
                if not _inside(dest, out):
                    raise RuntimeError("Unsafe path in archive")
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) and os.name != "nt":
                    link = z.read(member).decode("utf-8")
                    if not _inside(dest, out.parent / link):
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if out.is_symlink() or out.exists():
                        out.unlink()
                    os.symlink(link, out)
                    continue
                z.extract(member, dest)
                if mode & 0o111 and os.name != "nt" and not member.is_dir():
                    out.chmod(out.stat().st_mode | 0o755)
    elif name.endswith((".tar.bz2", ".tar.gz", ".tgz", ".tar.xz", ".tar")):
        with tarfile.open(archive) as t:
            members = []
            for member in t.getmembers():
                if not _inside(dest, dest / member.name):
                    raise RuntimeError("Unsafe path in archive")
                if (member.issym() or member.islnk()) and not _inside(
                    dest, (dest / member.name).parent / member.linkname
                ):
                    continue
                members.append(member)
            try:
                t.extractall(dest, members=members, filter="data")
            except TypeError:  # Python without extraction filters
                t.extractall(dest, members=members)
    else:
        raise RuntimeError(f"Unknown archive type: {archive.name}")


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        if code in (401, 403):
            return "The server refused the download (it may require a login or be unavailable in your region)."
        if code == 404:
            return "The file was not found on the server. It may have been moved or renamed."
        return f"Server error {code}. Please try again later."
    if isinstance(exc, requests.ConnectionError):
        return "No internet connection (needed only to download models)."
    if isinstance(exc, requests.Timeout):
        return "The download timed out. Please try again."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "Your disk is full."
    return str(exc) or exc.__class__.__name__


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


manager = DownloadManager()
