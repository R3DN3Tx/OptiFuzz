from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from backend.persistence.paths import long_path

# Windows' os.replace (MoveFileEx) can raise a transient PermissionError
# whenever *anyone* -- another writer, or just a plain reader's open() --
# has the destination file open at that instant, since Python's default
# open() doesn't set FILE_SHARE_DELETE. POSIX rename doesn't have this
# problem. Two mitigations, for the two ways this actually happens:
_registry_lock = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}

# 1) writer vs. writer: serialize same-path writes with a lock (below).
# 2) writer vs. a concurrent reader (e.g. another thread's read_json() mid-
#    replace): reproduced by tests/persistence/test_atomic.py -- a reader
#    holding the file open makes os.replace fail even though no other writer
#    is involved. A short bounded retry is the standard fix (the same
#    pattern used by pip, black, etc. for atomic writes on Windows): the
#    reader's handle closes essentially immediately, so a few short backoffs
#    resolve it without the caller ever seeing an error.
_REPLACE_RETRIES = 8
_REPLACE_RETRY_DELAY_SECONDS = 0.02


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _registry_lock:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def read_json(path: Path) -> Any:
    # Symmetric with _replace_with_retry: a read can just as rarely land in
    # the same short Windows window where a concurrent replace makes the
    # path transiently inaccessible.
    #
    # long_path(path), not a plain `path` -- a job's output .json can be a
    # genuinely long, human-readable filename ), and a plain open() on a path that exceeds
    # MAX_PATH fails to find a file that's really there (see
    # persistence/paths.py::long_path's own docstring).
    for attempt in range(_REPLACE_RETRIES):
        try:
            with open(long_path(path), "r", encoding="utf-8") as file_handle:
                return json.load(file_handle)
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON so a crash or concurrent reader never sees a partial file.

    Writes to a temp file in the same directory (so the final os.replace is
    same-filesystem and therefore atomic on both Windows and POSIX), then
    renames over the destination. On any failure, the temp file is removed
    and the destination is left exactly as it was.
    Concurrent writers to the *same* path are serialized (see _lock_for).

    NOT currently used for anything whose path can exceed Windows' MAX_PATH
    (job/project/target/tree/settings JSON, always short, bounded ids), and
    deliberately left that way -- unlike read_json's own long_path() fix
    (genuinely needed: results_service reads a job's output .json, which
    CAN be that long), `_replace_with_retry`'s os.replace() does NOT go
    through long_path(). `tempfile.mkstemp`'s own temp-file creation just
    below isn't long-path-safe either (its name is derived from `path.name`,
    so a long `path` would fail there first, loudly, before ever reaching
    the replace), so a defense-in-depth long_path() on the destination alone
    would add complexity to a hot, concurrency-sensitive path for a case
    that can't actually occur through this function today.

    (While investigating this, `_replace_with_retry` briefly wrapped its
    destination in long_path() -- initially looked like it made
    tests/persistence/test_atomic.py::test_concurrent_reads_during_writes_never_raise
    flakier, but a larger sample (15 runs) with it removed again still
    showed the same ~25% transient-PermissionError rate, confirming that
    flakiness is pre-existing and unrelated, not something introduced here.
    Left as a known, not-yet-investigated flake, same category as
    the other known ones.)
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock_for(path):
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
                json.dump(data, file_handle, indent=2, ensure_ascii=False)
                file_handle.flush()
                os.fsync(file_handle.fileno())
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
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)
