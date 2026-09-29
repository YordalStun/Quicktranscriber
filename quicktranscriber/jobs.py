"""Background job queue. One heavy job runs at a time so the computer stays usable."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable, Optional

from . import db

log = logging.getLogger("qt.jobs")


class JobCancelled(Exception):
    pass


class UserFacingError(Exception):
    """An error whose message is shown to the user as-is."""


class JobContext:
    def __init__(self, runner: "JobRunner", job: dict[str, Any]):
        self.runner = runner
        self.job = job
        self.job_id = job["id"]
        self.meeting_id = job.get("meeting_id")
        self._cancel = threading.Event()
        self._last_write = 0.0

    # progress ---------------------------------------------------------------
    def stage(self, key: str, label: str) -> None:
        live = self.runner.live.setdefault(self.job_id, {})
        live.update(stage=key, stage_label=label, progress=0.0, message=label, partial=[], stage_started=time.time(),
                    eta=None)
        stages = live.setdefault("stages", [])
        for s in stages:
            if s["key"] == key:
                s["status"] = "running"
            elif s["status"] == "running":
                s["status"] = "done"
        self._persist(force=True)

    def plan(self, stages: list[tuple[str, str]], done: list[str]) -> None:
        live = self.runner.live.setdefault(self.job_id, {})
        live["stages"] = [{"key": k, "label": l, "status": "done" if k in done else "pending"} for k, l in stages]

    def progress(self, value: float, message: Optional[str] = None, partial: Optional[str] = None,
                 eta: Optional[float] = None) -> None:
        if self._cancel.is_set():
            raise JobCancelled()
        live = self.runner.live.setdefault(self.job_id, {})
        live["progress"] = max(0.0, min(1.0, float(value)))
        if message:
            live["message"] = message
        if partial:
            lines = live.setdefault("partial", [])
            if not lines or lines[-1] != partial:
                lines.append(partial)
                del lines[:-6]
        started = live.get("stage_started") or time.time()
        if eta is not None:
            live["eta"] = eta
        elif value > 0.03:
            elapsed = time.time() - started
            live["eta"] = elapsed / value * (1 - value)
        self._persist()

    def info(self, **values: Any) -> None:
        self.runner.live.setdefault(self.job_id, {}).update(values)

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def _persist(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_write < 2.0:
            return
        self._last_write = now
        live = self.runner.live.get(self.job_id, {})
        try:
            db.execute(
                "UPDATE jobs SET stage = ?, progress = ?, message = ? WHERE id = ?",
                (live.get("stage", ""), live.get("progress", 0), live.get("message", ""), self.job_id),
            )
            if self.meeting_id:
                db.execute(
                    "UPDATE meetings SET stage = ?, progress = ? WHERE id = ?",
                    (live.get("stage", ""), live.get("progress", 0), self.meeting_id),
                )
        except Exception:  # noqa: BLE001 - progress is best effort
            log.debug("progress write failed", exc_info=True)


Handler = Callable[[JobContext], None]


class JobRunner:
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self.live: dict[int, dict[str, Any]] = {}
        self.current: Optional[JobContext] = None
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stop = False

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def start(self) -> None:
        # Jobs interrupted by closing the app are resumed
        db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        db.execute("UPDATE meetings SET status = 'queued' WHERE status = 'processing'")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="job-runner")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self.current:
            self.current._cancel.set()
        self._wake.set()

    def submit(self, kind: str, meeting_id: Optional[str] = None, params: Optional[dict[str, Any]] = None) -> int:
        if meeting_id:
            # a newer request for the same meeting replaces a queued one
            db.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE meeting_id = ? AND kind = ? AND status = 'queued'",
                (time.time(), meeting_id, kind),
            )
        cur = db.execute(
            "INSERT INTO jobs (meeting_id, kind, params, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
            (meeting_id, kind, db.dumps(params or {}), time.time()),
        )
        if meeting_id:
            db.execute(
                "UPDATE meetings SET status = 'queued', error = NULL WHERE id = ? AND status != 'processing'",
                (meeting_id,),
            )
        self._wake.set()
        return int(cur.lastrowid)

    def cancel(self, job_id: int) -> bool:
        if self.current and self.current.job_id == job_id:
            self.current._cancel.set()
            return True
        row = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row and row["status"] == "queued":
            db.execute("UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ?", (time.time(), job_id))
            if row["meeting_id"]:
                self._settle_meeting(row["meeting_id"], "cancelled")
            return True
        return False

    def cancel_meeting(self, meeting_id: str) -> None:
        for row in db.query("SELECT id FROM jobs WHERE meeting_id = ? AND status IN ('queued', 'running')", (meeting_id,)):
            self.cancel(row["id"])

    def snapshot(self) -> list[dict[str, Any]]:
        rows = db.query(
            "SELECT j.*, m.title AS meeting_title, m.duration AS meeting_duration FROM jobs j "
            "LEFT JOIN meetings m ON m.id = j.meeting_id "
            "WHERE j.status IN ('queued', 'running') OR j.finished_at > ? ORDER BY j.id",
            (time.time() - 30,),
        )
        out = []
        for r in rows:
            r["params"] = db.loads(r["params"], {})
            live = self.live.get(r["id"])
            if live and r["status"] == "running":
                r.update({k: v for k, v in live.items() if k != "stage_started"})
            out.append(r)
        return out

    # -- worker loop -----------------------------------------------------------

    def _next(self) -> Optional[dict[str, Any]]:
        return db.one("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1")

    def _loop(self) -> None:
        while not self._stop:
            job = None
            try:
                job = self._next()
            except Exception:  # noqa: BLE001
                log.exception("job queue read failed")
            if not job:
                self._wake.wait(5)
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job: dict[str, Any]) -> None:
        job["params"] = db.loads(job.get("params"), {})
        ctx = JobContext(self, job)
        self.current = ctx
        self.live[job["id"]] = {"stages": [], "progress": 0.0, "message": "Starting"}
        db.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (time.time(), job["id"]))
        if job.get("meeting_id"):
            db.execute("UPDATE meetings SET status = 'processing', error = NULL WHERE id = ?", (job["meeting_id"],))
        handler = self.handlers.get(job["kind"])
        status, error = "done", None
        try:
            if handler is None:
                raise UserFacingError(f"Unknown job type {job['kind']}")
            handler(ctx)
        except JobCancelled:
            status = "cancelled"
        except UserFacingError as exc:
            status, error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Job %s failed:\n%s", job["id"], traceback.format_exc())
            status, error = "error", f"{exc}" or exc.__class__.__name__
        finally:
            self.current = None
        db.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ?, progress = ? WHERE id = ?",
            (status, error, time.time(), 1.0 if status == "done" else self.live.get(job["id"], {}).get("progress", 0),
             job["id"]),
        )
        if job.get("meeting_id"):
            self._settle_meeting(job["meeting_id"], status, error)
        self.live.pop(job["id"], None)

    def _settle_meeting(self, meeting_id: str, status: str, error: Optional[str] = None) -> None:
        pending = db.one(
            "SELECT id FROM jobs WHERE meeting_id = ? AND status IN ('queued', 'running')", (meeting_id,)
        )
        if pending:
            return
        meeting_status = {"done": "ready", "cancelled": "cancelled", "error": "error"}[status]
        if status == "cancelled":
            # keep a usable transcript if we got that far
            has_segments = db.one("SELECT id FROM segments WHERE meeting_id = ? LIMIT 1", (meeting_id,))
            if has_segments:
                meeting_status = "ready"
        db.execute(
            "UPDATE meetings SET status = ?, error = ?, stage = '', progress = ? WHERE id = ?",
            (meeting_status, error, 1.0 if meeting_status == "ready" else 0.0, meeting_id),
        )


runner = JobRunner()
