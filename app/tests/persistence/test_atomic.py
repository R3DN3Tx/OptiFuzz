import threading
import time
from pathlib import Path

import pytest

from backend.persistence.atomic import read_json, write_json_atomic


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "job.json"

    write_json_atomic(path, {"status": "RUNNING", "id": "job_1"})

    assert read_json(path) == {"status": "RUNNING", "id": "job_1"}


def test_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "p1" / "targets" / "t1" / "target.json"

    write_json_atomic(path, {"name": "t1"})

    assert read_json(path) == {"name": "t1"}


def test_no_leftover_temp_files_after_successful_write(tmp_path: Path) -> None:
    path = tmp_path / "job.json"

    write_json_atomic(path, {"a": 1})

    assert list(tmp_path.iterdir()) == [path]


def test_failed_write_leaves_existing_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "job.json"
    write_json_atomic(path, {"status": "COMPLETED"})

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr("backend.persistence.atomic.json.dump", _boom)

    with pytest.raises(RuntimeError):
        write_json_atomic(path, {"status": "CORRUPT"})

    # Original content survives -- no half-written replacement, no stray
    # temp file left behind.
    assert read_json(path) == {"status": "COMPLETED"}
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_writers_never_corrupt_the_file(tmp_path: Path) -> None:
    path = tmp_path / "job.json"
    write_json_atomic(path, {"writer": -1, "n": -1})

    def _writer(writer_id: int) -> None:
        for n in range(20):
            write_json_atomic(path, {"writer": writer_id, "n": n})

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Whichever write happened last, it must be one complete, valid payload
    # -- never a torn/partial file.
    data = read_json(path)
    assert set(data) == {"writer", "n"}
    assert 0 <= data["writer"] < 4
    assert 0 <= data["n"] < 20


def test_concurrent_reads_during_writes_never_raise(tmp_path: Path) -> None:
    # Regression test: this reproduced a real PermissionError from Windows'
    # os.replace when a reader's plain open() handle was alive at the exact
    # moment a writer tried to replace the same file -- a *different* race
    # than writer-vs-writer above, caught by JobManager's own test suite
    # first.
    #
    # Readers sleep briefly between iterations -- a tight, unthrottled busy
    # loop across 4 threads made every write starve for retries under
    # Windows Defender's real-time scan, turning a ~1s test into a multi-
    # minute hang the first time this was written. The `finally: stop.set()`
    # is what actually prevents a hang if the writer ever raises: without
    # it, a writer exception leaves the readers spinning forever and
    # reader.join() blocks indefinitely.
    path = tmp_path / "job.json"
    write_json_atomic(path, {"n": -1})
    errors: list[BaseException] = []
    stop = threading.Event()

    def _writer() -> None:
        try:
            for n in range(40):
                write_json_atomic(path, {"n": n})
        except BaseException as error:  # a write that fails is a lost save: it must fail this test, not just a thread
            errors.append(error)
        finally:
            stop.set()

    def _reader() -> None:
        while not stop.is_set():
            try:
                read_json(path)
            except (OSError, ValueError) as error:
                errors.append(error)
            time.sleep(0.001)

    readers = [threading.Thread(target=_reader) for _ in range(4)]
    for reader in readers:
        reader.start()
    writer = threading.Thread(target=_writer)
    writer.start()

    writer.join(timeout=30)
    stop.set()
    for reader in readers:
        reader.join(timeout=5)

    assert not writer.is_alive(), "writer thread did not finish"
    assert all(not r.is_alive() for r in readers), "a reader thread did not finish"
    assert errors == []
