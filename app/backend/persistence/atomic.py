from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from backend.persistence.paths import long_path

# Windows' os.replace (MoveFileEx) raises a transient PermissionError whenever *anyone* -- another writer,
# or just a plain reader's open() -- has the destination file open at that instant, since Python's default
# open() does not set FILE_SHARE_DELETE. POSIX rename does not have this problem. Mitigations, in order:
#
# 1) writer vs. writer, AND writer vs. reader inside this process: every read and write of the same path
#    takes the same lock (below), so they never overlap. This is what makes a lost write impossible
#    between the app's own threads (the job runner writing job.json while the UI polls it).
# 2) a program outside this process (antivirus, an editor, a backup tool) holding the file open: a bounded
#    retry with a growing wait, a few seconds in total. Such a handle is normally released within
#    milliseconds; if it is not, the error is raised to the caller instead of being swallowed.
_registry_lock = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}
_REPLACE_RETRIES = 40
_RETRY_FIRST_DELAY_SECONDS = 0.01
_RETRY_MAX_DELAY_SECONDS = 0.25  # 40 attempts add up to roughly 8 seconds


class DamagedFileError(ValueError):
    """A data file exists but cannot be parsed. Still a ValueError, so callers that already
    catch ValueError for a bad file keep working; `path` and `reason` let the UI explain it."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{Path(path).name} is damaged: {reason}")
        self.path = Path(path)
        self.reason = reason


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _registry_lock:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def _delay(attempt: int) -> float:
    return min(_RETRY_FIRST_DELAY_SECONDS * (1.5 ** attempt), _RETRY_MAX_DELAY_SECONDS)


def backup_path(path: Path) -> Path:
    """Where the previous good copy of `path` is kept (written by write_json_atomic)."""
    path = Path(path)
    return path.with_name(path.name + ".bak")


def read_json(path: Path) -> Any:
    """Parsed JSON of `path`. A file that exists but is not valid JSON raises DamagedFileError.

    long_path(path), not a plain `path` -- a job's output .json can be a genuinely long, human-readable
    filename, and a plain open() on a path that exceeds MAX_PATH fails to find a file that is really there
    (see persistence/paths.py::long_path)."""
    path = Path(path)
    with _lock_for(path):
        for attempt in range(_REPLACE_RETRIES):
            try:
                with open(long_path(path), "r", encoding="utf-8") as file_handle:
                    return json.load(file_handle)
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(_delay(attempt))
            except json.JSONDecodeError as exc:
                raise DamagedFileError(path, f"not valid JSON ({exc.msg}, line {exc.lineno} column {exc.colno})") from exc
            except UnicodeDecodeError as exc:
                raise DamagedFileError(path, "not valid UTF-8 text") from exc


def load_model(path: Path, from_dict) -> Any:
    """read_json + build the model; a file that parses but lacks fields is just as damaged."""
    data = read_json(path)
    try:
        return from_dict(data)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise DamagedFileError(Path(path), f"missing or invalid field ({type(exc).__name__}: {exc})") from exc


def _is_valid_json_file(path: Path) -> bool:
    try:
        with open(long_path(path), "r", encoding="utf-8") as file_handle:
            json.load(file_handle)
        return True
    except (OSError, ValueError):
        return False


def quarantine(path: Path) -> Path | None:
    """Moves a damaged file out of the way (never deletes it) and returns where it went."""
    path = Path(path)
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}-{counter}")
        counter += 1
    with _lock_for(path):
        os.replace(path, target)
    return target


def restore_backup(path: Path) -> bool:
    """Puts the last good copy back in place of a damaged `path` (the damaged file is kept aside).
    False when there is no usable backup."""
    path = Path(path)
    backup = backup_path(path)
    if not backup.is_file() or not _is_valid_json_file(backup):
        return False
    if path.exists():
        quarantine(path)
    with _lock_for(path):
        shutil.copy2(backup, path)
    return True


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON so a crash or concurrent reader never sees a partial file.

    Writes to a temp file in the same directory (so the final os.replace is same-filesystem and therefore
    atomic on both Windows and POSIX), then renames over the destination. On any failure the temp file is
    removed and the destination is left exactly as it was. Same-path reads and writes are serialized (see
    _lock_for). Before replacing a file that is still valid, its content is kept as `<name>.bak`, the copy
    the repair tools restore when the main file is damaged later.

    NOT used for anything whose path can exceed Windows' MAX_PATH (job/project/target/tree/settings JSON:
    short, bounded ids -- names are capped and ids truncated, see persistence/slug.py).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock_for(path):
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
                json.dump(data, file_handle, indent=2, ensure_ascii=False)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            if path.is_file() and _is_valid_json_file(path):
                try:
                    shutil.copy2(path, backup_path(path))
                except OSError:
                    pass  # a missing backup must never block saving the data itself
            _replace_with_retry(tmp_name, path)
        except BaseException:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_delay(attempt))
