"""Every file the app reads or writes lives inside the app folder.

Nothing is written to the user's profile, the registry or system folders, so
deleting the folder removes the app completely.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("QT_ROOT") or Path(__file__).resolve().parent.parent)

DATA = ROOT / "data"
MEETINGS = DATA / "meetings"
SPEAKERS = DATA / "speakers"
RECORDINGS = DATA / "recordings"  # in-progress recordings
LOGS = DATA / "logs"
DB_PATH = DATA / "quicktranscriber.db"
SETTINGS_PATH = DATA / "settings.json"

MODELS = ROOT / "models"
WHISPER_MODELS = MODELS / "whisper"
LLM_MODELS = MODELS / "llm"
SPEAKER_MODELS = MODELS / "speaker"
HF_CACHE = MODELS / "hf-cache"

RUNTIME = ROOT / "runtime"
LLAMA_DIR = RUNTIME / "llama.cpp"
TMP = RUNTIME / "tmp"

STATIC = Path(__file__).resolve().parent / "static"


def ensure_dirs() -> None:
    for d in (DATA, MEETINGS, SPEAKERS, RECORDINGS, LOGS, WHISPER_MODELS, LLM_MODELS,
              SPEAKER_MODELS, HF_CACHE, LLAMA_DIR, TMP):
        d.mkdir(parents=True, exist_ok=True)


def keep_everything_local() -> None:
    """Point third-party caches and temp files at the app folder, disable telemetry."""
    import tempfile

    os.environ["HF_HOME"] = str(HF_CACHE)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    TMP.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(TMP)


def meeting_dir(meeting_id: str) -> Path:
    return MEETINGS / meeting_id


def rel(path: Path) -> str:
    """Path relative to ROOT with forward slashes (stored in the database)."""
    return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()


def absolute(relative: str) -> Path:
    return ROOT / relative


def native(path: Path | str, start: Path | str | None = None) -> str:
    """Path to hand to the speech, speaker and LLM engines.

    On Windows some of them open files through narrow "ANSI" strings, so a
    folder with letters such as "é" or "ł" in its path (often the user name)
    makes them fail to find their model. The app runs from ROOT, so in that
    case the path is given relative to the working directory (or ``start``),
    which is plain ASCII.
    """
    p = os.path.abspath(path)
    if p.isascii():
        return p
    try:
        relative = os.path.relpath(p, start or os.getcwd())
    except ValueError:  # on another drive
        return p
    return relative if relative.isascii() else p
