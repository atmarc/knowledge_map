"""User settings, edited in the web UI's Settings panel and stored in data/config.json.

Environment variables (KM_SOURCES, KM_DOWNLOADS, ...) only provide the initial values: once a
setting is saved from the UI, the saved value is used.
"""

import json
import os
import threading
from pathlib import Path

from . import db

CONFIG_PATH = db.DB_PATH.parent / "config.json"

# Fields the search box can look in; the web UI ranks matches by field.
SEARCH_FIELDS = ("title", "authors", "year", "journal", "tags", "notes",
                 "contributions", "methodology", "abstract", "keywords")

# key: (environment variable, default)
FIELDS = {
    "sources": ("KM_SOURCES", ""),              # folder with the PDFs
    "downloads": ("KM_DOWNLOADS", "~/Downloads"),  # watched for new articles; empty = off
    "backup_dir": ("KM_BACKUP_DIR", ""),        # e.g. a mounted network share; empty = off
    "backup_hour": ("KM_BACKUP_HOUR", 12),      # daily backup runs at the first check after this hour
    "research_field": ("KM_RESEARCH_FIELD", ""),  # tells Claude what the library is about
    "claude_model": ("KM_CLAUDE_MODEL", ""),    # empty = Claude Code's default
    "openalex_key": ("KM_OPENALEX_KEY", ""),    # optional; gives OpenAlex requests their own budget
    "search_fields": (None, list(SEARCH_FIELDS)),  # what the search box looks in
}

_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def get(key: str):
    env, default = FIELDS[key]
    value = _load().get(key, os.environ.get(env, default) if env else default)
    if isinstance(default, list):
        return value if isinstance(value, list) else default
    return int(value) if isinstance(default, int) else (value or "")


def path(key: str) -> Path | None:
    """A folder setting as an absolute path, or None when it is unset."""
    value = get(key)
    # Not resolve(): it touches the filesystem, which can hang on a stale network mount.
    return Path(value).expanduser().absolute() if value else None


def all_settings() -> dict:
    return {key: get(key) for key in FIELDS}


def update(changes: dict) -> dict:
    with _lock:
        saved = _load()
        saved.update({k: v for k, v in changes.items() if k in FIELDS})
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=2))
        tmp.replace(CONFIG_PATH)
    return all_settings()
