"""Daily backup of the literature folder and the library database to a backup folder.

The backup folder is set in the Settings panel. It is typically a mounted network share. Files
missing there are copied, and files that are newer locally replace the copy of the same name.
Nothing is ever deleted there, even when removed locally. A snapshot of the database is saved to
<backup folder>/knowledge_map/library.db on every backup.

The daily run happens once per calendar day, at the first check after the configured hour. If the
backup folder isn't available then (e.g. the share isn't mounted), that day is skipped. This module
never mounts anything itself.
"""

import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path

from . import config, db

DB_DIRNAME = "knowledge_map"
STATE_PATH = db.DB_PATH.parent / "backup.json"
CHECK_EVERY_S = 300
PROBE_TIMEOUT_S = 15  # a stale network mount can hang instead of failing

_lock = threading.Lock()
_running = False


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def status() -> dict:
    state = _load_state()
    return {"enabled": bool(config.get("backup_dir")), "running": _running,
            "last_success": state.get("last_success"), "last_result": state.get("last_result")}


def _available(target: Path) -> bool:
    pool = ThreadPoolExecutor(1)
    try:
        return pool.submit(target.is_dir).result(timeout=PROBE_TIMEOUT_S)
    except FutureTimeout:
        return False
    finally:
        pool.shutdown(wait=False)  # don't wait on a probe that hangs


def _files_to_copy(sources: Path, target: Path) -> list[str]:
    """Relative paths of local files missing on the server or newer than its copy, by rsync dry run."""
    # --modify-window: SMB timestamps are coarser than local ones.
    out = subprocess.run(["rsync", "-rn", "--update", "--modify-window=2", "--out-format=%n", f"{sources}/", f"{target}/"],
                         capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise RuntimeError(f"rsync exited {out.returncode}: {out.stderr.strip()[-300:]}")
    return [name for name in out.stdout.splitlines() if not name.endswith("/")]


def _copy_file(src: Path, dst: Path) -> None:
    # Not rsync: it fails on GVFS because SMB through FUSE can't chmod its temp files.
    # Copying to a temp name means an interrupted copy never looks like a finished file.
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(f".{dst.name}.part")
    try:
        shutil.copyfile(src, part)
        if part.stat().st_size != src.stat().st_size:
            raise RuntimeError(f"incomplete copy of {src.name}")
        try:
            part.replace(dst)
        except OSError:  # SMB can refuse to rename over an existing file
            dst.unlink(missing_ok=True)
            part.replace(dst)
    finally:
        part.unlink(missing_ok=True)


def copy_files(sources: Path, target: Path) -> int:
    names = _files_to_copy(sources, target)
    for name in names:
        _copy_file(sources / name, target / name)
    return len(names)


def copy_database(target: Path) -> None:
    # The backup API gives a consistent snapshot even while the server is writing.
    with tempfile.TemporaryDirectory(dir=db.DB_PATH.parent) as tmp:
        snapshot = Path(tmp) / "library.db"
        source, dest = db.connect(), sqlite3.connect(snapshot)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        _copy_file(snapshot, target / DB_DIRNAME / "library.db")


def _run(trigger: str) -> None:
    global _running
    now = datetime.now().isoformat(timespec="seconds")
    result = {"ok": False, "at": now, "trigger": trigger, "copied": 0}
    try:
        sources, target = config.path("sources"), config.path("backup_dir")
        if not target:
            result["message"] = "No backup folder is set (see Settings)"
        elif not sources or not sources.is_dir():
            result["message"] = "The literature folder isn't set or doesn't exist (see Settings)"
        elif not _available(target):
            result["message"] = f"The backup folder {target} isn't available (not mounted?)"
        else:
            result["copied"] = copy_files(sources, target)
            copy_database(target)
            result["ok"] = True
            result["message"] = f"{result['copied']} new or changed file(s) copied, database saved"
    except Exception as e:
        result["message"] = str(e)[:300]
    finally:
        state = _load_state()
        state["last_result"] = result
        if result["ok"]:
            state["last_success"] = now
        _save_state(state)
        _running = False


def start(trigger: str = "manual") -> bool:
    """Run a backup in a background thread; False if one is already running."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True
    threading.Thread(target=_run, args=(trigger,), daemon=True).start()
    return True


def daily_tick(now: datetime | None = None) -> bool:
    """Start today's backup if it's due. Only one attempt is made per day."""
    now = now or datetime.now()
    today = now.date().isoformat()
    state = _load_state()
    if not config.get("backup_dir") or now.hour < config.get("backup_hour") \
            or state.get("last_daily_attempt") == today \
            or (state.get("last_success") or "").startswith(today):
        return False
    state["last_daily_attempt"] = today
    _save_state(state)
    return start("daily")


def start_scheduler() -> None:
    def loop():
        while True:
            time.sleep(CHECK_EVERY_S)  # also gives the network time to come up after boot
            daily_tick()
    threading.Thread(target=loop, daemon=True).start()
