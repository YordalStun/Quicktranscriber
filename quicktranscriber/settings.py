"""User settings, stored as JSON in data/settings.json."""

from __future__ import annotations

import copy
import json
import threading
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "onboarded": False,
    "theme": "dark",
    # --- transcription -------------------------------------------------
    "language": "auto",  # ISO code or "auto"
    "whisper_model": "",  # empty = pick recommended for this computer
    "whisper_quality": "accurate",  # fast | accurate | max
    "device": "auto",  # auto | cpu | cuda
    "vocabulary": "",  # names / jargon to help spelling
    # --- speakers --------------------------------------------------------
    "diarization": True,
    "speaker_model": "titanet_small",
    "speaker_detail": "balanced",  # fast | balanced | precise
    "auto_learn_confident": False,  # learn from auto-recognised speakers without asking
    # --- notes -------------------------------------------------------------
    "auto_notes": True,
    "llm_backend": "builtin",  # builtin | ollama | openai
    "llm_model": "",  # catalog key, custom:<file>, or backend model name
    "llm_device": "auto",  # auto | cpu | gpu
    "llm_context": 0,  # 0 = automatic
    "llm_thinking": False,
    "ollama_url": "http://127.0.0.1:11434",
    "openai_url": "http://127.0.0.1:1234/v1",
    "openai_key": "",
    "notes_template": "general",
    "notes_language": "auto",  # auto = same as the meeting
    "notes_detail": "standard",  # brief | standard | detailed
    "notes_instructions": "",
    "llama_server_path": "",  # custom llama-server binary
    # --- storage -----------------------------------------------------------
    "keep_original_audio": True,
    "watch_enabled": False,
    "watch_folder": "",
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def load() -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None:
            data: dict[str, Any] = {}
            if paths.SETTINGS_PATH.exists():
                try:
                    data = json.loads(paths.SETTINGS_PATH.read_text("utf-8"))
                except (OSError, ValueError):
                    data = {}
            merged = copy.deepcopy(DEFAULTS)
            merged.update({k: v for k, v in data.items() if k in DEFAULTS})
            _cache = merged
        return copy.deepcopy(_cache)


def get(key: str) -> Any:
    return load().get(key, DEFAULTS.get(key))


def update(changes: dict[str, Any]) -> dict[str, Any]:
    global _cache
    current = load()
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        elif isinstance(default, str):
            value = "" if value is None else str(value)
        current[key] = value
    with _lock:
        paths.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = paths.SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2), "utf-8")
        tmp.replace(paths.SETTINGS_PATH)
        _cache = copy.deepcopy(current)
    return current
