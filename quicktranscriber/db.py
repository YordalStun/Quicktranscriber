"""SQLite storage (data/quicktranscriber.db)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

import numpy as np

from . import paths

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    recorded_at REAL,
    duration REAL DEFAULT 0,
    source TEXT DEFAULT 'upload',
    original_name TEXT,
    audio_file TEXT,
    status TEXT DEFAULT 'new',
    stage TEXT DEFAULT '',
    progress REAL DEFAULT 0,
    error TEXT,
    language TEXT,
    options TEXT DEFAULT '{}',
    notes TEXT,
    notes_meta TEXT DEFAULT '{}',
    summary TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    favorite INTEGER DEFAULT 0,
    stats TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    speaker TEXT,
    text TEXT NOT NULL,
    words TEXT,
    edited INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id, idx);

CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    color TEXT,
    notes TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS meeting_speakers (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'unknown',
    confidence REAL DEFAULT 0,
    suggested_id INTEGER,
    color TEXT,
    talk_time REAL DEFAULT 0,
    embedding BLOB,
    PRIMARY KEY (meeting_id, key)
);

CREATE TABLE IF NOT EXISTS speaker_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    meeting_id TEXT,
    start REAL,
    end REAL,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL,
    clip TEXT,
    source TEXT DEFAULT 'meeting',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_speaker ON speaker_samples(speaker_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT,
    kind TEXT NOT NULL,
    params TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',
    stage TEXT DEFAULT '',
    progress REAL DEFAULT 0,
    message TEXT DEFAULT '',
    error TEXT,
    created_at REAL,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS perf (
    key TEXT PRIMARY KEY,
    value REAL,
    samples INTEGER DEFAULT 0
);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False
FTS_AVAILABLE = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        init()
        c = _connect()
        _local.conn = c
    return c


def init() -> None:
    global _initialized, FTS_AVAILABLE
    with _init_lock:
        if _initialized:
            return
        paths.DATA.mkdir(parents=True, exist_ok=True)
        c = _connect()
        c.executescript(SCHEMA)
        try:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
                "text, meeting_id UNINDEXED, segment_id UNINDEXED, start UNINDEXED, "
                "tokenize='unicode61 remove_diacritics 2')"
            )
            FTS_AVAILABLE = True
        except sqlite3.OperationalError:
            FTS_AVAILABLE = False
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.close()
        _initialized = True


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
    row = conn().execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return conn().execute(sql, tuple(params))


# --- helpers ---------------------------------------------------------------


def to_blob(vec: Optional[np.ndarray]) -> Optional[bytes]:
    if vec is None:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32).copy()


def loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def now() -> float:
    return time.time()


# --- search ------------------------------------------------------------------


def reindex_meeting(meeting_id: str) -> None:
    if not FTS_AVAILABLE:
        return
    with transaction() as c:
        c.execute("DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,))
        rows = c.execute(
            "SELECT id, text, start FROM segments WHERE meeting_id = ? ORDER BY idx", (meeting_id,)
        ).fetchall()
        c.executemany(
            "INSERT INTO search_index (text, meeting_id, segment_id, start) VALUES (?, ?, ?, ?)",
            [(r["text"], meeting_id, r["id"], r["start"]) for r in rows],
        )


def drop_index(meeting_id: str) -> None:
    if FTS_AVAILABLE:
        execute("DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,))


# --- performance memory (to estimate how long things take on this PC) ---------


def record_perf(key: str, value: float) -> None:
    """Exponential moving average of e.g. seconds-of-processing per second-of-audio."""
    row = one("SELECT value, samples FROM perf WHERE key = ?", (key,))
    if row is None:
        execute("INSERT INTO perf (key, value, samples) VALUES (?, ?, 1)", (key, value))
    else:
        alpha = 0.5 if row["samples"] < 3 else 0.3
        execute(
            "UPDATE perf SET value = ?, samples = samples + 1 WHERE key = ?",
            (row["value"] * (1 - alpha) + value * alpha, key),
        )


def get_perf() -> dict[str, float]:
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM perf")}
