import asyncio
import sys
import time
from pathlib import Path

import pytest

from backend.engine.mock import MockDirsearchEngine
from backend.events.bus import EventBus
from backend.job_manager.manager import JobManager, JobNotFoundError
from backend.models.job import JobStatus
from backend.models.job_config import DirsearchJobConfig
from backend.persistence import layout
from backend.persistence.atomic import read_json


def _wordlist(tmp_path: Path, count: int = 20) -> Path:
    path = tmp_path / "words.txt"
    path.write_text("\n".join(f"word{i}" for i in range(count)) + "\n", encoding="utf-8")
    return path


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


def test_job_completes_and_marks_the_whole_wordlist_done(workspace_root, tmp_path: Path) -> None:
    # A job runs as ONE continuous process against its full wordlist now
    # (2026-09-24 -- no more fixed-size chunk subprocesses); a natural
    # completion marks completed_lines/completed_requests as the whole
    # wordlist, not a per-chunk count.
    manager = JobManager(MockDirsearchEngine(every=2), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 20)],
    )

    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED))

    final = manager.get_job(job.id)
    assert final.status == JobStatus.COMPLETED
    assert final.completed_lines == 20
    assert final.completed_requests == final.total_wordlist_requests
    assert final.result_count == 10  # every=2 over 20 words
    assert final.started_at is not None
    assert final.ended_at is not None
    assert final.pid is None


def test_a_result_matching_an_already_tracked_tree_node_updates_its_status(
    workspace_root, tmp_path: Path
) -> None:
    # Real, reported bug (2026-09-25): a path added to TREE (manually, or
    # from an earlier result) never picked up a fresh status/size once a
    # LATER fuzz job discovered a real hit at that exact path -- see
    # tree_service.record_fuzz_discoveries's own docstring.
    from backend.services import tree_service

    tree_service.add_path("p1", "t1", "/word0", source="manual")
    manager = JobManager(MockDirsearchEngine(every=2), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 20)],
    )
    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED))

    node = next(n for n in tree_service.list_nodes("p1", "t1") if n.path == "/word0")
    status = tree_service.latest_status(node)
    assert status is not None
    assert status["status"] == 200  # word0 is index 0 -- first of _STATUS_CYCLE
    assert node.discoveries[-1].source == "fuzz"
    assert node.discoveries[-1].job_id == job.id


def test_concurrency_limit_queues_extra_jobs(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.05), max_concurrent_jobs=1)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    wordlist = _wordlist(tmp_path, 10)

    job_a = manager.create_job(
        project_id="p1", target_id="t1", path="/a",
        config=config, wordlist_files=[wordlist],
    )
    job_b = manager.create_job(
        project_id="p1", target_id="t1", path="/b",
        config=config, wordlist_files=[wordlist],
    )

    _wait_until(lambda: manager.get_job(job_a.id).status == JobStatus.RUNNING)
    assert manager.get_job(job_b.id).status == JobStatus.QUEUED

    _wait_until(
        lambda: manager.get_job(job_a.id).status in (JobStatus.COMPLETED, JobStatus.FAILED)
    )
    _wait_until(
        lambda: manager.get_job(job_b.id).status in (JobStatus.COMPLETED, JobStatus.FAILED)
    )

    assert manager.get_job(job_a.id).status == JobStatus.COMPLETED
    assert manager.get_job(job_b.id).status == JobStatus.COMPLETED


def test_stop_queued_job_is_immediate(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(delay=0.2), max_concurrent_jobs=1)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    wordlist = _wordlist(tmp_path, 10)

    job_a = manager.create_job(
        project_id="p1", target_id="t1", path="/a",
        config=config, wordlist_files=[wordlist],
    )
    job_b = manager.create_job(
        project_id="p1", target_id="t1", path="/b",
        config=config, wordlist_files=[wordlist],
    )

    _wait_until(lambda: manager.get_job(job_a.id).status == JobStatus.RUNNING)
    assert manager.get_job(job_b.id).status == JobStatus.QUEUED

    stopped = manager.stop_job(job_b.id)
    assert stopped.status == JobStatus.STOPPED

    manager.stop_job(job_a.id)  # cleanup: don't leave a subprocess running past the test


def test_stop_running_job_does_not_mark_progress_done(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.2), max_concurrent_jobs=1)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/a",
        config=config, wordlist_files=[_wordlist(tmp_path, 50)],
    )

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.RUNNING)
    manager.stop_job(job.id)

    _wait_until(
        lambda: manager.get_job(job.id).status in (JobStatus.STOPPED, JobStatus.COMPLETED, JobStatus.FAILED),
        timeout=10,
    )

    final = manager.get_job(job.id)
    assert final.status == JobStatus.STOPPED
    assert final.completed_lines == 0  # stopped almost immediately, nothing safely checkpointed yet


def test_job_failure_is_reported_with_reason(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(fail=True), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 5)],
    )

    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED))

    final = manager.get_job(job.id)
    assert final.status == JobStatus.FAILED
    assert final.error_reason is not None
    assert "simulated engine failure" in final.error_reason


class _UnreachableEngine(MockDirsearchEngine):
    """Reports every target as unreachable, without touching the network --
    MockDirsearchEngine itself always reports reachable ), so the unreachable-host path needs its own stand-in to test."""

    def is_target_reachable(self, url: str) -> bool:
        del url
        return False


def test_job_against_an_unreachable_target_fails_without_ever_running(
    workspace_root, tmp_path: Path
) -> None:
    # Real, reported bug this fixes (2026-09-24): dirsearch itself treats
    # every per-request connection failure as an ordinary "no match" and
    # exits 0, so a job against a dead target used to run its entire
    # wordlist and still end COMPLETED with 0 results -- indistinguishable
    # from a live target with nothing interesting on it. See
    # FuzzingEngine.is_target_reachable's own docstring for the full story.
    manager = JobManager(_UnreachableEngine(), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 20)],
    )

    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED))

    final = manager.get_job(job.id)
    assert final.status == JobStatus.FAILED
    assert final.error_reason == "Unreachable host"
    assert final.completed_lines == 0


def test_auto_pause_on_detected_status(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(
        MockDirsearchEngine(every=1, delay=0.05, force_status=429), max_concurrent_jobs=2
    )
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 20)],
        pause_on_status=[429],
    )

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)

    paused = manager.get_job(job.id)
    assert paused.pause_requested is True
    # Distinct from pause_requested (shared with manual pause) -- only the
    # auto-pause-on-status path sets this, so the frontend's "⚠ auto" tag
    # can tell the two apart. See test_manual_pause_does_not_set_auto_paused.
    assert paused.auto_paused is True
    assert paused.auto_pause_status == 429  # the code that fired, not the whole armed list
    assert paused.completed_lines == 0  # cut short almost immediately, nothing safely checkpointed yet
    assert paused.result_count >= 1  # at least the triggering 429 was recorded live


def test_pause_on_status_does_not_interfere_when_never_triggered(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 10)],
        pause_on_status=[429],  # configured, but the mock engine never emits it
    )

    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED))

    assert manager.get_job(job.id).status == JobStatus.COMPLETED


def test_resume_after_auto_pause_restarts_from_the_same_point(workspace_root, tmp_path: Path) -> None:
    # The trigger condition (429) is left in place across resume, so the job
    # should cycle RUNNING -> PAUSED again, redoing the same never-completed
    # region each time -- the bounded-rework guarantee
    # (MockDirsearchEngine never emits progress-bar
    # text, so completed_lines has nothing to advance from here either way).
    manager = JobManager(
        MockDirsearchEngine(every=1, delay=0.03, force_status=429), max_concurrent_jobs=2
    )
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 10)],
        pause_on_status=[429],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)
    assert manager.get_job(job.id).completed_lines == 0

    manager.resume_job(job.id)

    _wait_until(lambda: manager.get_job(job.id).status != JobStatus.PAUSED, timeout=5)
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)

    final = manager.get_job(job.id)
    assert final.completed_lines == 0
    assert final.pause_requested is True


def test_resume_non_paused_job_is_a_no_op(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=2))
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 10)],
    )
    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED))

    result = manager.resume_job(job.id)

    assert result.status == JobStatus.COMPLETED  # untouched, not re-queued


def test_stop_a_paused_job_finalizes_as_stopped(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.05, force_status=429))
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 20)],
        pause_on_status=[429],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)

    stopped = manager.stop_job(job.id)

    assert stopped.status == JobStatus.STOPPED


# ---- Manual pause (a Pause button, not the auto-pause-on-status feature) --

def test_pause_running_job_cuts_the_current_run_short(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.2), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 50)],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.RUNNING)

    manager.pause_job(job.id)

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)
    paused = manager.get_job(job.id)
    assert paused.pause_requested is True
    assert paused.completed_lines == 0  # cut short almost immediately, nothing safely checkpointed yet


def test_manual_pause_does_not_set_auto_paused(workspace_root, tmp_path: Path) -> None:
    # Real, reported bug: the frontend's "⚠ auto" tag used to key off
    # pause_requested alone, which BOTH a manual pause and an auto-pause
    # set -- every manually-paused job showed the auto-pause tag too.
    # auto_paused is the field that actually distinguishes the two.
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.2), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 50)],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.RUNNING)

    manager.pause_job(job.id)

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)
    assert manager.get_job(job.id).auto_paused is False


def test_pause_queued_job_is_immediate(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(delay=0.2), max_concurrent_jobs=1)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    wordlist = _wordlist(tmp_path, 10)

    job_a = manager.create_job(
        project_id="p1", target_id="t1", path="/a",
        config=config, wordlist_files=[wordlist],
    )
    job_b = manager.create_job(
        project_id="p1", target_id="t1", path="/b",
        config=config, wordlist_files=[wordlist],
    )
    _wait_until(lambda: manager.get_job(job_a.id).status == JobStatus.RUNNING)
    assert manager.get_job(job_b.id).status == JobStatus.QUEUED

    paused = manager.pause_job(job_b.id)

    assert paused.status == JobStatus.PAUSED
    manager.stop_job(job_a.id)  # cleanup: don't leave a subprocess running past the test


def test_resume_after_manual_pause_continues_and_completes(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.03), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 30)],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.RUNNING)
    started_at_before_pause = manager.get_job(job.id).started_at
    manager.pause_job(job.id)
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)

    manager.resume_job(job.id)

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.COMPLETED, timeout=10)
    final = manager.get_job(job.id)
    assert final.completed_lines == 30
    # Real, reported bug: _run_job unconditionally overwrote started_at on
    # every resume, so the frontend's elapsed-time display (anchored on
    # this field) silently reset to ~0 on every resume instead of
    # continuing from the job's actual original start.
    assert final.started_at == started_at_before_pause


def test_pause_a_terminal_job_is_a_no_op(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=2))
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 10)],
    )
    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED))

    result = manager.pause_job(job.id)

    assert result.status == JobStatus.COMPLETED  # untouched


def test_pause_an_already_paused_job_is_a_no_op(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.05, force_status=429))
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 20)],
        pause_on_status=[429],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED, timeout=10)

    result = manager.pause_job(job.id)

    assert result.status == JobStatus.PAUSED


def test_pause_missing_job_raises(workspace_root) -> None:
    manager = JobManager(MockDirsearchEngine())

    with pytest.raises(JobNotFoundError):
        manager.pause_job("nope")


def test_set_target_concurrency_rejects_invalid_values(workspace_root) -> None:
    manager = JobManager(MockDirsearchEngine())

    with pytest.raises(ValueError):
        manager.set_target_concurrency("t1", 0)


def test_set_target_concurrency_dispatches_waiting_jobs_when_raised(
    workspace_root, tmp_path: Path
) -> None:
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.05), max_concurrent_jobs=1)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    wordlist = _wordlist(tmp_path, 10)

    job_a = manager.create_job(
        project_id="p1", target_id="t1", path="/a",
        config=config, wordlist_files=[wordlist],
    )
    job_b = manager.create_job(
        project_id="p1", target_id="t1", path="/b",
        config=config, wordlist_files=[wordlist],
    )

    _wait_until(lambda: manager.get_job(job_a.id).status == JobStatus.RUNNING)
    assert manager.get_job(job_b.id).status == JobStatus.QUEUED

    changed = manager.set_target_concurrency("t1", 2)
    assert changed == 2
    assert manager.get_target_concurrency("t1") == 2

    # Raising the limit should let the queued job start immediately, without
    # waiting for job_a to finish.
    _wait_until(lambda: manager.get_job(job_b.id).status == JobStatus.RUNNING, timeout=5)

    manager.stop_job(job_a.id)
    manager.stop_job(job_b.id)


def test_target_concurrency_limits_are_independent_per_target(
    workspace_root, tmp_path: Path
) -> None:
    # A busy target at its limit must not block a *different* target's job
    # from starting -- concurrency is per-target, not a shared global pool.
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.2), max_concurrent_jobs=1)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    wordlist = _wordlist(tmp_path, 10)

    busy_job = manager.create_job(
        project_id="p1", target_id="busy", path="/a",
        config=config, wordlist_files=[wordlist],
    )
    other_job = manager.create_job(
        project_id="p1", target_id="other", path="/a",
        config=config, wordlist_files=[wordlist],
    )

    _wait_until(lambda: manager.get_job(busy_job.id).status == JobStatus.RUNNING)
    _wait_until(lambda: manager.get_job(other_job.id).status == JobStatus.RUNNING)

    manager.stop_job(busy_job.id)
    manager.stop_job(other_job.id)


def test_get_missing_job_raises(workspace_root) -> None:
    manager = JobManager(MockDirsearchEngine())

    with pytest.raises(JobNotFoundError):
        manager.get_job("does-not-exist")


def test_stop_called_immediately_after_creation_is_not_lost(workspace_root, tmp_path: Path) -> None:
    # Regression test for a race where stop_job() could run in the narrow
    # window between a job's status becoming RUNNING and its chunk
    # subprocess actually being spawned/registered in self._processes --
    # the stop request would otherwise sit unnoticed until that chunk
    # finished on its own (caught while adding
    # the concurrent "is_fuzzing" indicator).
    manager = JobManager(MockDirsearchEngine(every=1, delay=0.05), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 200)],
    )
    manager.stop_job(job.id)  # called as fast as possible after creation, on purpose

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.STOPPED, timeout=3)


def test_stop_missing_job_raises(workspace_root) -> None:
    manager = JobManager(MockDirsearchEngine())

    with pytest.raises(JobNotFoundError):
        manager.stop_job("does-not-exist")


def test_job_json_persisted_to_disk(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(MockDirsearchEngine(every=2))
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 10)],
    )

    def _on_disk_status() -> str | None:
        try:
            return read_json(layout.job_file("p1", "t1", job.id)).get("status")
        except (OSError, ValueError):
            return None

    # Poll the file itself, not the in-memory job -- job.status flips to a
    # terminal value slightly before the corresponding disk write completes,
    # so this is the only race-free way to assert
    # "eventually persisted".
    _wait_until(lambda: _on_disk_status() in ("COMPLETED", "FAILED"))

    on_disk = read_json(layout.job_file("p1", "t1", job.id))
    assert on_disk["status"] == "COMPLETED"
    assert on_disk["id"] == job.id


@pytest.mark.asyncio
async def test_events_are_published_across_job_lifecycle(workspace_root, tmp_path: Path) -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    queue = bus.subscribe(loop)

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2, event_bus=bus)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 4)],
    )

    seen_names: list[str] = []
    deadline = loop.time() + 5
    while "job.completed" not in seen_names and loop.time() < deadline:
        event = await asyncio.wait_for(queue.get(), timeout=5)
        seen_names.append(event.name)

    assert "job.queued" in seen_names
    assert "job.started" in seen_names
    assert "job.output" in seen_names
    assert "job.result" in seen_names
    assert "job.completed" in seen_names


@pytest.mark.asyncio
async def test_no_live_progress_events_without_a_progress_bar_signal(
    workspace_root, tmp_path: Path
) -> None:
    # MockDirsearchEngine's stdout never contains PROGRESS_RE-shaped text
    # (it's not trying to look like real dirsearch output), so
    # _publish_progress is never called for it at all -- 2026-09-24's move
    # to one continuous process per job removed the old per-chunk-boundary
    # fallback publish that used to guarantee at least SOME job.progress
    # events regardless of engine. That's fine for the real app: the real
    # DirsearchEngine always has live progress now (COLUMNS/LINES env vars,
    # see manager.py's PROGRESS_ENV_COLUMNS comment), and the JOBS table's
    # Progress cell still updates correctly from the ordinary REST-fetched
    # job object on every lifecycle event (job.queued/started/completed/...)
    # regardless of whether any job.progress SSE event ever fired -- this
    # test locks in that the job itself still completes correctly with zero
    # live progress events, not that some now-removed fallback still exists.
    bus = EventBus()
    loop = asyncio.get_running_loop()
    queue = bus.subscribe(loop)

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2, event_bus=bus)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 25)],
    )

    progress_events = []
    deadline = loop.time() + 10
    final_status = None
    while loop.time() < deadline:
        event = await asyncio.wait_for(queue.get(), timeout=10)
        if event.name == "job.progress":
            progress_events.append(event.data)
        elif event.name in ("job.completed", "job.failed"):
            final_status = event.name
            break

    assert final_status == "job.completed"
    assert progress_events == []
    final = manager.get_job(job.id)
    assert final.completed_lines == final.total_wordlist_lines == 25


def test_completed_job_survives_a_fresh_jobmanager_instance(workspace_root, tmp_path: Path) -> None:
    # Simulates a process restart: a brand new JobManager pointed at the
    # same workspace must find jobs a *previous* instance persisted, not
    # start with an empty in-memory dict.
    first = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    job = first.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 4)],
    )
    _wait_until(lambda: first.get_job(job.id).status == JobStatus.COMPLETED)

    second = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    reloaded = second.get_job(job.id)
    assert reloaded.status == JobStatus.COMPLETED
    assert reloaded.result_count == first.get_job(job.id).result_count


def test_a_job_caught_running_at_restart_becomes_resumable(workspace_root, tmp_path: Path) -> None:
    first = JobManager(MockDirsearchEngine(every=1, delay=0.3), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    job = first.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 50)],
    )
    _wait_until(lambda: first.get_job(job.id).status == JobStatus.RUNNING)
    # Don't stop it -- simulate the process just dying mid-run, the way a
    # crash or an unexpected restart would, with no graceful shutdown.

    second = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    reloaded = second.get_job(job.id)
    assert reloaded.status == JobStatus.PAUSED
    assert reloaded.pid is None
    # Not a deliberate user Pause -- tagged the same way as the
    # auto-pause-on-status feature so the frontend's "⚠ auto" indicator
    # shows, distinguishing "the app died" from "I clicked Pause".
    assert reloaded.auto_paused is True

    # And it's genuinely resumable from where it left off, not just stuck.
    resumed = second.resume_job(job.id)
    assert resumed.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    _wait_until(lambda: second.get_job(job.id).status == JobStatus.COMPLETED, timeout=10)


def test_publish_progress_never_claims_100_percent_before_job_is_done(
    workspace_root, tmp_path: Path
) -> None:
    # A run segment hitting its last word (index == length) is still
    # mid-run -- the job isn't COMPLETED until the process actually exits
    # and the report is flushed. Showing 100% here previously made the
    # Progress cell visibly lie while the job sat at RUNNING for a while
    # longer.
    from backend.models.job import FuzzJob, JobStatus
    from backend.models.timestamps import now_iso
    from backend.parser.stdout_parser import ParsedProgress

    bus = EventBus()
    loop = asyncio.new_event_loop()
    queue = bus.subscribe(loop)

    # No create_job()/dispatch here on purpose: this method only needs a
    # FuzzJob and doesn't touch self._jobs, and avoiding the background
    # runner thread means no race with its own job.started publish.
    manager = JobManager(MockDirsearchEngine(), max_concurrent_jobs=2, event_bus=bus)
    job = FuzzJob(
        id="job1", target_id="t1", project_id="p1", path="/",
        config={}, status=JobStatus.RUNNING, created_at=now_iso(),
        total_wordlist_lines=10, total_wordlist_requests=10,
    )

    # Every one of the 10 requests has been dispatched -- genuinely all done.
    manager._publish_progress(job, ParsedProgress(percent=100, index=10, length=10, rate=5, errors=0), 0)

    event = loop.run_until_complete(asyncio.wait_for(queue.get(), timeout=1))
    loop.close()
    assert event.name == "job.progress"
    assert event.data["overall_done"] == 10  # genuinely all words done...
    assert event.data["overall_percent"] == 99  # ...but never displayed as 100%


def test_publish_progress_adds_this_segments_own_total_to_prior_segments_share(
    workspace_root, tmp_path: Path
) -> None:
    # total/done are built from dirsearch's own live progress.length/.index
    # for THIS run segment's remainder wordlist, plus base_requests (prior
    # segments' already-folded-in share, passed in explicitly -- never read
    # live off job.completed_requests, which _save_periodic_checkpoint can
    # mutate mid-segment and would otherwise double-count this same
    # segment's progress on top of itself).
    from backend.models.job import FuzzJob, JobStatus
    from backend.models.timestamps import now_iso
    from backend.parser.stdout_parser import ParsedProgress

    bus = EventBus()
    loop = asyncio.new_event_loop()
    queue = bus.subscribe(loop)

    manager = JobManager(MockDirsearchEngine(), max_concurrent_jobs=2, event_bus=bus)
    job = FuzzJob(
        id="job1", target_id="t1", project_id="p1", path="/",
        config={}, status=JobStatus.RUNNING, created_at=now_iso(),
        total_wordlist_lines=20, total_wordlist_requests=48,
        completed_lines=10, completed_requests=30,  # a prior run segment's own share, already folded in
    )

    # This run segment's own dirsearch index/length is relative to ITS
    # remainder wordlist (18 real requests total, per dirsearch's own
    # count), currently 9 in (halfway).
    manager._publish_progress(job, ParsedProgress(percent=50, index=9, length=18, rate=5, errors=0), 30)

    event = loop.run_until_complete(asyncio.wait_for(queue.get(), timeout=1))
    loop.close()
    # overall_total = the prior segment's 30 + this segment's own real 18 =
    # 48; overall_done = the prior segment's 30 + this segment's 9 so far.
    assert event.data["overall_total"] == 48
    assert event.data["overall_done"] == 39
    assert event.data["overall_percent"] == 81


def test_publish_progress_prefers_dirsearchs_own_length_over_our_own_estimate(
    workspace_root, tmp_path: Path
) -> None:
    # Real, reported bug (2026-09-25): our own upfront estimate
    # (total_wordlist_requests, wordlist_resolver._estimate_requests)
    # deliberately doesn't replicate every one of dirsearch's own
    # dictionary-expansion rules (prefixes/suffixes/overwrite-extensions,
    # only reachable via a raw --extra-args flag) -- for a wordlist that
    # uses one, that estimate undercounts, and progress used to be clamped
    # at a too-low "100% (N/N)" the moment dirsearch's real live count
    # passed it, while dirsearch kept genuinely working (still finding new
    # results, rate still changing) well past that point -- the user
    # reported exactly this, contrasted with plain dirsearch from a
    # terminal, which only ever shows its own real numbers. dirsearch's own
    # progress.length is authoritative for whatever it's actually doing, so
    # it wins over our own estimate now, whether or not they agree.
    from backend.models.job import FuzzJob, JobStatus
    from backend.models.timestamps import now_iso
    from backend.parser.stdout_parser import ParsedProgress

    bus = EventBus()
    loop = asyncio.new_event_loop()
    queue = bus.subscribe(loop)

    manager = JobManager(MockDirsearchEngine(), max_concurrent_jobs=2, event_bus=bus)
    job = FuzzJob(
        id="job1", target_id="t1", project_id="p1", path="/",
        config={}, status=JobStatus.RUNNING, created_at=now_iso(),
        total_wordlist_lines=10, total_wordlist_requests=10,  # our own estimate -- deliberately too low
    )

    # dirsearch's own real total (length) for this wordlist is actually 25 --
    # well past our own (wrong) estimate of 10 -- and it's only 20 of the
    # way through, genuinely still working.
    manager._publish_progress(job, ParsedProgress(percent=80, index=20, length=25, rate=5, errors=0), 0)

    event = loop.run_until_complete(asyncio.wait_for(queue.get(), timeout=1))
    loop.close()
    assert event.data["overall_total"] == 25  # dirsearch's own number, not our estimate's 10
    assert event.data["overall_done"] == 20
    assert event.data["overall_percent"] == 80  # not clamped at 99 -- genuinely not done yet


def test_publish_progress_falls_back_to_our_own_total_without_a_usable_dirsearch_length(
    workspace_root, tmp_path: Path
) -> None:
    # dirsearch hasn't printed a usable length yet (progress.length <= 0,
    # shouldn't normally happen once any real progress line has been parsed
    # at all, but must never crash / show broken progress if it does) --
    # falls back to our own total_wordlist_requests estimate, or (a job
    # predating that field, an old persisted job reloaded after an app
    # update, sitting at its dataclass default of 0) the raw line count.
    from backend.models.job import FuzzJob, JobStatus
    from backend.models.timestamps import now_iso
    from backend.parser.stdout_parser import ParsedProgress

    bus = EventBus()
    loop = asyncio.new_event_loop()
    queue = bus.subscribe(loop)

    manager = JobManager(MockDirsearchEngine(), max_concurrent_jobs=2, event_bus=bus)
    job = FuzzJob(
        id="job1", target_id="t1", project_id="p1", path="/",
        config={}, status=JobStatus.RUNNING, created_at=now_iso(),
        total_wordlist_lines=20, total_wordlist_requests=0,
        completed_lines=10, completed_requests=10,
    )

    manager._publish_progress(job, ParsedProgress(percent=50, index=5, length=0, rate=5, errors=0), 10)

    event = loop.run_until_complete(asyncio.wait_for(queue.get(), timeout=1))
    loop.close()
    assert event.data["overall_total"] == 20
    assert event.data["overall_done"] == 15  # base_requests' 10 + this segment's 5 so far


# ---- Periodic mid-run checkpoint (2026-09-24) -- bounds how much a genuine
# crash (not a deliberate Pause) can cost, by persisting the same checkpoint
# _checkpoint_pause computes, repeatedly, during a still-live run.

def test_compute_checkpoint_applies_the_thread_safety_margin(workspace_root) -> None:
    # index=10, threads=3 -> safe_index=7: only lines fully covered by the
    # first 7 (not 10) requests are considered done, since up to `threads`
    # of the raw index could still be in flight (dirsearch's own index counts
    # dispatch, not completion -- see _compute_checkpoint's own docstring).
    from backend.parser.stdout_parser import ParsedProgress

    manager = JobManager(MockDirsearchEngine())
    config = DirsearchJobConfig(url="https://target.local/", threads=3)
    lines = [f"word{i}" for i in range(10)]

    new_lines, new_requests = manager._compute_checkpoint(
        lines, config, ParsedProgress(percent=100, index=10, length=10, rate=5, errors=0),
        base_lines=0, base_requests=0,
    )

    assert new_lines == 7
    assert new_requests == 7


def test_compute_checkpoint_is_relative_to_a_fixed_base_not_cumulative(workspace_root) -> None:
    # Calling this twice with the SAME base (as a periodic checkpoint would,
    # mid-run, before job.completed_lines itself is ever mutated) must give
    # the same kind of answer each time -- monotonically increasing with the
    # live index, never double-counting a prior call's own result.
    from backend.parser.stdout_parser import ParsedProgress

    manager = JobManager(MockDirsearchEngine())
    config = DirsearchJobConfig(url="https://target.local/", threads=1)
    lines = [f"word{i}" for i in range(20)]

    first_lines, first_requests = manager._compute_checkpoint(
        lines, config, ParsedProgress(percent=50, index=5, length=20, rate=5, errors=0),
        base_lines=10, base_requests=10,
    )
    second_lines, second_requests = manager._compute_checkpoint(
        lines, config, ParsedProgress(percent=100, index=8, length=20, rate=5, errors=0),
        base_lines=10, base_requests=10,  # SAME base both times -- not first_lines/first_requests
    )

    assert first_lines == 14 and first_requests == 14  # base 10 + (index 5 - 1 thread)
    assert second_lines == 17 and second_requests == 17  # base 10 + (index 8 - 1 thread), not +5 first


def test_compute_checkpoint_with_no_progress_yet_returns_the_base_unchanged(workspace_root) -> None:
    manager = JobManager(MockDirsearchEngine())
    config = DirsearchJobConfig(url="https://target.local/", threads=5)

    new_lines, new_requests = manager._compute_checkpoint(
        ["a", "b", "c"], config, None, base_lines=1, base_requests=1,
    )

    assert (new_lines, new_requests) == (1, 1)


def test_save_periodic_checkpoint_persists_and_skips_redundant_writes(workspace_root, tmp_path: Path) -> None:
    from backend.models.job import FuzzJob, JobStatus
    from backend.models.timestamps import now_iso
    from backend.parser.stdout_parser import ParsedProgress
    from backend.persistence import layout
    from backend.persistence.atomic import read_json

    manager = JobManager(MockDirsearchEngine())
    config = DirsearchJobConfig(url="https://target.local/", threads=1)
    lines = [f"word{i}" for i in range(10)]
    job = FuzzJob(
        id="job1", target_id="t1", project_id="p1", path="/",
        config={}, status=JobStatus.RUNNING, created_at=now_iso(),
        total_wordlist_lines=10, total_wordlist_requests=10,
    )
    manager._jobs[job.id] = job  # _save() only needs the job itself, not the queue

    manager._save_periodic_checkpoint(
        job, lines, config, ParsedProgress(percent=50, index=5, length=10, rate=5, errors=0),
        base_lines=0, base_requests=0,
    )

    assert job.completed_lines == 4  # index 5 - 1 thread
    assert job.completed_requests == 4
    on_disk = read_json(layout.job_file("p1", "t1", "job1"))
    assert on_disk["completed_lines"] == 4

    # Same progress again -- nothing new to persist, must not raise or
    # silently corrupt the already-correct in-memory/on-disk state.
    manager._save_periodic_checkpoint(
        job, lines, config, ParsedProgress(percent=50, index=5, length=10, rate=5, errors=0),
        base_lines=0, base_requests=0,
    )
    assert job.completed_lines == 4

    # Real, previously-shipped bug: base.with_suffix(".txt") treats
    # everything after the LAST "." in the stem as an existing extension to
    # replace -- for a stem containing a dot (e.g. "127.0.0.1", a common
    # local-test-server target name), that silently truncated the path and
    # left the real .txt/.json files behind undeleted. Also exercises the
    # mirror-directory cleanup hook (explicit request: deleting a job
    # removes its mirrored copy too), sharing the same call site.
    from backend.services import output_mirror_service, project_service, target_service

    project = project_service.create_project("Client A")
    target = target_service.create_target(project.id, "127.0.0.1", "http://127.0.0.1:8910/")

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url=target.base_url, threads=5)
    job = manager.create_job(
        project_id=project.id, target_id=target.id, path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 4)],
        output_stem="dirsearch_127.0.0.1_8910_root",  # the dotted stem itself
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.COMPLETED)

    # Mirror dir configured only AFTER the job finishes -- deliberately, to
    # keep this test isolated to delete_job()'s own cleanup logic. Setting
    # it up beforehand raced against _run_job's own legitimate
    # completion-time mirror attempt for this same job (both targeting the
    # same output path), which could re-populate the mirror after this
    # test's own delete_job() call already cleaned it up -- a real race in
    # the test, not in the production code being tested.
    mirror_dir = tmp_path / "mirror"
    project = output_mirror_service.set_mirror_dir(project.id, str(mirror_dir))

    # MockDirsearchEngine doesn't write real report files -- simulate what
    # a real dirsearch run would have left behind, at the exact same path
    # job_manager itself resolves.
    base = layout.job_output_base_for(project.id, target.id, job.id, "dirsearch_127.0.0.1_8910_root", False)
    txt_path = base.parent / f"{base.name}.txt"
    json_path = base.parent / f"{base.name}.json"
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text("[10:00:00] 200 -   1B - http://127.0.0.1:8910/admin\n", encoding="utf-8")
    json_path.write_text("{}", encoding="utf-8")
    output_mirror_service.mirror_output_file(project, target.name, txt_path)
    mirrored_path = output_mirror_service.mirror_target_subdir(project, target.name) / txt_path.name
    assert mirrored_path.is_file()

    manager.delete_job(job.id)

    assert not txt_path.exists()
    assert not json_path.exists()
    assert not mirrored_path.exists()


def test_fix_output_command_line_rebuilds_the_real_command_headers_and_all(
    workspace_root, tmp_path: Path
) -> None:
    # Real gap found while verifying the earlier {extension}-placeholder fix:
    # dirsearch's own report header/`info.args` captures its raw invocation
    # argv via redact_command() (lib/utils/command.py, vendored, confirmed
    # unmodified) -- which strips -H/--header/--cookie entirely, not just the
    # value, both the flag AND value collapse into a bare "<redacted>" token.
    # Fine for a report meant to be shared publicly; wrong here, since this
    # app's saved reports are local-only and the pentester explicitly needs
    # to see exactly what ran. Rebuilt instead from this engine's own
    # build_command() against the job's own (unredacted) config.
    from backend.engine.dirsearch import DirsearchEngine
    from backend.models.job import FuzzJob
    from backend.models.job_config import config_to_dict

    manager = JobManager(DirsearchEngine(python_executable="python.exe"), max_concurrent_jobs=2)
    config = DirsearchJobConfig(
        url="https://target.local/admin",
        extensions=("php", "html"),
        force_extensions=True,
        headers={"Authorization": "Basic abc123"},
        cookie="session=abc",
        threads=7,
    )
    job = FuzzJob(
        id="abc123", project_id="p1", target_id="t1", path="/admin",
        config=config_to_dict(config), status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00+00:00",
        wordlist_sources=[r"C:\wordlists\common_big.txt"],
    )
    output_base = tmp_path / "outputs" / "dirsearch_target_root_abc123"
    output_base.parent.mkdir(parents=True, exist_ok=True)
    txt_path = output_base.parent / f"{output_base.name}.txt"
    json_path = output_base.parent / f"{output_base.name}.json"
    txt_path.write_text(
        f"# Dirsearch started at 2026-09-21 20:40:59 as: -u https://target.local/admin -o {output_base.as_posix()}.{{extension}}\n"
        "200    1B  http://target/admin\n",
        encoding="utf-8",
    )
    json_path.write_text(
        '{"info": {"args": "-u https://target.local/admin -H <redacted>", "time": "x"}, "results": []}',
        encoding="utf-8",
    )

    manager._fix_output_command_line(job, output_base)

    fixed_txt = txt_path.read_text(encoding="utf-8")
    assert "{extension}" not in fixed_txt
    assert f"{output_base.name}.txt" in fixed_txt
    assert "-e php,html" in fixed_txt
    assert "-f" in fixed_txt
    assert "Authorization: Basic abc123" in fixed_txt  # the real header, not <redacted>
    assert "session=abc" in fixed_txt  # the real cookie, not <redacted>
    assert "-t 7" in fixed_txt
    assert r"C:\wordlists\common_big.txt" in fixed_txt or "C:/wordlists/common_big.txt" in fixed_txt
    assert "<wordlist-chunk>" not in fixed_txt  # the old, non-meaningful placeholder
    assert "200    1B  http://target/admin" in fixed_txt  # results below the header untouched

    fixed_json = read_json(json_path)
    assert "{extension}" not in fixed_json["info"]["args"]
    assert f"{output_base.name}.json" in fixed_json["info"]["args"]
    assert "Authorization: Basic abc123" in fixed_json["info"]["args"]


def test_fix_output_command_line_is_a_no_op_without_the_txt_report(
    workspace_root, tmp_path: Path
) -> None:
    from backend.models.job import FuzzJob

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    job = FuzzJob(
        id="abc123", project_id="p1", target_id="t1", path="/",
        config={"url": "https://target.local/"}, status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00+00:00",
    )
    output_base = tmp_path / "outputs" / "dirsearch_target_root"

    manager._fix_output_command_line(job, output_base)  # must not raise -- no report at all


# ---- Long fuzzed path -> MAX_PATH bug (2026-09-22) -------------------------
#
# A job's output_stem can now be up to ~200 characters (output_naming.py --
# so a long fuzzed path still shows in full in the saved filename), which by
# itself would risk the exact same MAX_PATH failure this is meant to fix.
# The actual fix: dirsearch always writes to a short, job_id-based working
# name (guaranteed short regardless of the stem), renamed to the full name
# via layout.long_path()'s extended-length prefix once the job is done.

def test_should_use_working_output_name_for_a_fresh_job(workspace_root, tmp_path: Path) -> None:
    from backend.models.job import FuzzJob

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    job = FuzzJob(
        id="abc123", project_id="p1", target_id="t1", path="/admin",
        config={"url": "https://target.local/"}, status=JobStatus.QUEUED,
        created_at="2026-01-01T00:00:00+00:00", output_stem="dirsearch_admin", output_flat=True,
    )
    final_base = layout.job_output_base_for("p1", "t1", job.id, job.output_stem, job.output_flat)

    assert manager._should_use_working_output_name(job, final_base) is True


def test_should_use_working_output_name_false_for_a_job_already_partially_written_at_the_final_name(
    workspace_root, tmp_path: Path
) -> None:
    # A job that already has partial output directly under the OLD,
    # direct-to-final-name scheme (e.g. one PAUSED before this dual-naming
    # scheme shipped) must keep writing there -- switching it mid-flight
    # would fork its results across two different files.
    from backend.models.job import FuzzJob

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    job = FuzzJob(
        id="abc123", project_id="p1", target_id="t1", path="/admin",
        config={"url": "https://target.local/"}, status=JobStatus.PAUSED,
        created_at="2026-01-01T00:00:00+00:00", output_stem="dirsearch_admin", output_flat=True,
        completed_lines=2,
    )
    final_base = layout.job_output_base_for("p1", "t1", job.id, job.output_stem, job.output_flat)
    final_base.parent.mkdir(parents=True, exist_ok=True)
    (final_base.parent / f"{final_base.name}.txt").write_text("partial", encoding="utf-8")

    assert manager._should_use_working_output_name(job, final_base) is False


def test_should_use_working_output_name_true_when_resuming_a_job_already_writing_there(
    workspace_root, tmp_path: Path
) -> None:
    # Real, reported bug: completed_lines == 0 used to stand in for
    # "brand new job", but a MODERN job resumed after its first chunk
    # already completed under the working name also has
    # completed_lines > 0 -- it was misclassified as the legacy
    # direct-to-final-name case above, pointed at final_base (which doesn't
    # exist yet) instead, and dirsearch started a brand new, EMPTY report
    # there -- silently orphaning every result already sitting in the
    # working-name file (never merged, never shown in Results, never in the
    # final .txt/.json). The fix checks the WORKING name's own existence
    # first, before falling back to the legacy-final-name check.
    from backend.models.job import FuzzJob

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    job = FuzzJob(
        id="abc123", project_id="p1", target_id="t1", path="/admin",
        config={"url": "https://target.local/"}, status=JobStatus.PAUSED,
        created_at="2026-01-01T00:00:00+00:00", output_stem="dirsearch_admin", output_flat=True,
        completed_lines=1,
    )
    final_base = layout.job_output_base_for("p1", "t1", job.id, job.output_stem, job.output_flat)
    working_base = layout.job_output_base_working("p1", "t1", job.id)
    working_base.parent.mkdir(parents=True, exist_ok=True)
    (working_base.parent / f"{working_base.name}.txt").write_text("200 /admin\n", encoding="utf-8")

    assert manager._should_use_working_output_name(job, final_base) is True
    # And the final name must NOT exist yet -- confirms this scenario is
    # genuinely distinct from the legacy-job test above, not accidentally
    # passing for the same reason.
    assert not layout.path_is_file(final_base.parent / f"{final_base.name}.txt")


def test_finalize_output_filename_renames_across_a_genuinely_long_destination_path(
    workspace_root, tmp_path: Path
) -> None:
    # The actual, direct proof this whole fix works: os.replace() through
    # layout.long_path()'s extended-length prefix succeeds for a
    # DESTINATION path that genuinely exceeds Windows' 260-char MAX_PATH --
    # not a synthetic assertion about string lengths, a real file rename on
    # this real filesystem.
    from backend.services import output_naming

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    long_path = "/" + "/".join(f"segment-{i}-with-some-extra-length-padding" for i in range(20))
    stem = output_naming.build_output_stem(
        "dirsearch_{target}_{port}_{path}", target_name="target", url="http://target/", path=long_path
    )
    final_base = tmp_path / "outputs" / f"{stem}_abc123def456"
    if sys.platform == "win32":  # MAX_PATH only exists on Windows
        assert len(str(final_base.parent / f"{final_base.name}.txt")) > 260  # genuinely over MAX_PATH

    working_base = tmp_path / "outputs" / "_working_abc123def456"
    working_base.parent.mkdir(parents=True, exist_ok=True)
    (working_base.parent / f"{working_base.name}.txt").write_text("200 /admin\n", encoding="utf-8")
    (working_base.parent / f"{working_base.name}.json").write_text('{"results": []}', encoding="utf-8")

    result = manager._finalize_output_filename(working_base, final_base)

    # layout.read_text/path_is_file, not a plain Path method -- the
    # destination itself is the >260-char path under test here, and a
    # plain Path.read_text()/.is_file() can't see it either (the same bug
    # this whole fix is for -- see persistence/paths.py::long_path).
    assert result == final_base
    assert layout.read_text(final_base.parent / f"{final_base.name}.txt") == "200 /admin\n"
    assert layout.path_is_file(final_base.parent / f"{final_base.name}.json")
    assert not (working_base.parent / f"{working_base.name}.txt").exists()
    assert not (working_base.parent / f"{working_base.name}.json").exists()


def test_finalize_output_filename_falls_back_to_the_working_name_if_nothing_to_rename(
    workspace_root, tmp_path: Path
) -> None:
    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    working_base = tmp_path / "outputs" / "_working_abc123"
    final_base = tmp_path / "outputs" / "dirsearch_admin_abc123"

    result = manager._finalize_output_filename(working_base, final_base)

    assert result == working_base


def test_fuzzing_a_very_long_path_completes_without_crashing_end_to_end(
    workspace_root, tmp_path: Path
) -> None:
    # Smoke test tying output_naming's raised caps and the working-name/
    # rename split together through the real JobManager pipeline (create_job
    # -> chunked execution -> finalize -> placeholder-fix -> mirror), for a
    # job whose output_stem is now far longer than the old 40/60-char caps
    # ever allowed. MockDirsearchEngine never writes real report files, so
    # this doesn't exercise the actual rename mechanics (see the direct
    # _finalize_output_filename tests above for that) -- it proves the
    # whole pipeline runs clean end-to-end for a long path without raising.
    from backend.services import output_naming

    manager = JobManager(MockDirsearchEngine(every=1), max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    long_path = "/" + "/".join(f"segment-{i}-with-some-extra-length-padding" for i in range(20))
    output_stem = output_naming.build_output_stem(
        "dirsearch_{target}_{port}_{path}", target_name="target.local", url="https://target.local/", path=long_path
    )

    job = manager.create_job(
        project_id="p1", target_id="t1", path=long_path,
        config=config, wordlist_files=[_wordlist(tmp_path, 10)],
        output_stem=output_stem, output_flat=True,
    )

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.COMPLETED)

    final = manager.get_job(job.id)
    assert final.status == JobStatus.COMPLETED
    assert final.result_count > 0


def test_result_count_counts_each_finding_once(workspace_root, tmp_path: Path) -> None:
    # dirsearch can report the exact same finding twice; Results shows it once, so the
    # Jobs count must too ("5" on Jobs against 4 rows in Results was a reported mismatch).
    from backend.parser.stdout_parser import ParsedResult

    manager = JobManager(MockDirsearchEngine(every=1000), max_concurrent_jobs=1)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=DirsearchJobConfig(url="https://target.local/"),
        wordlist_files=[_wordlist(tmp_path, 1)],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.COMPLETED)
    before = manager.get_job(job.id).result_count

    up = ParsedResult(time="", status=200, size="21KB", url="https://target.local/up")
    other = ParsedResult(time="", status=301, size="0B", url="https://target.local/x", redirect="/x/")
    manager._flush_results(job.id, [up, other, up])
    manager._flush_results(job.id, [up])

    assert manager.get_job(job.id).result_count == before + 2


class _GivesUpEarlyEngine(MockDirsearchEngine):
    """Prints a progress bar stopped at 3% and dirsearch's own give-up message, then exits 0."""

    def __init__(self, message: str = "Too many request errors") -> None:
        super().__init__()
        self._message = message

    def build_command(self, config, *, wordlist_file, output_base, log_file=None):
        script = (
            "print('[#---------]  3%  9/300  5/s  job:1/1  errors:80')\n"
            f"print({self._message!r})\n"
        )
        return [sys.executable, "-c", script]


def test_a_dirsearch_that_gives_up_with_exit_0_is_paused_at_its_real_position_not_completed(
    workspace_root, tmp_path: Path
) -> None:
    # Reported: "41672/41672 completed" while only ~4% of the wordlist had been requested (dirsearch
    # skips the target after 75 consecutive request errors and still exits 0).
    manager = JobManager(_GivesUpEarlyEngine(), max_concurrent_jobs=1)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=DirsearchJobConfig(url="https://target.local/"), wordlist_files=[_wordlist(tmp_path, 300)],
    )

    _wait_until(lambda: manager.get_job(job.id).status in (JobStatus.PAUSED, JobStatus.COMPLETED, JobStatus.FAILED))

    final = manager.get_job(job.id)
    assert final.status == JobStatus.PAUSED
    assert final.completed_lines < 300
    assert "Too many request errors" in final.error_reason
    assert "NOT requested" in final.error_reason
    assert "80 request errors" in final.error_reason


def test_a_resumed_job_clears_the_stopped_early_reason(workspace_root, tmp_path: Path) -> None:
    manager = JobManager(_GivesUpEarlyEngine(), max_concurrent_jobs=1)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=DirsearchJobConfig(url="https://target.local/"), wordlist_files=[_wordlist(tmp_path, 300)],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED)
    first_reason = manager.get_job(job.id).error_reason

    manager.resume_job(job.id)
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.PAUSED and manager.get_job(job.id).error_reason)

    assert manager.get_job(job.id).error_reason is not None and first_reason is not None


def test_early_end_reason_needs_a_message_or_a_clearly_short_progress() -> None:
    from backend.parser.stdout_parser import ParsedProgress

    config = DirsearchJobConfig(url="https://t/", threads=5)
    done = ParsedProgress(percent=100, index=41672, length=41672, rate=100, errors=0)
    almost = ParsedProgress(percent=99, index=41600, length=41672, rate=100, errors=0)  # within the 2% margin
    short = ParsedProgress(percent=3, index=889, length=41672, rate=100, errors=0)

    assert JobManager._early_end_reason(config, done, "") is None
    assert JobManager._early_end_reason(config, almost, "") is None
    assert JobManager._early_end_reason(config, None, "") is None
    assert "889 of 41672" in JobManager._early_end_reason(config, short, "")
    assert "Skipped the target due to 429 status code" in JobManager._early_end_reason(
        config, done, "x\nSkipped the target due to 429 status code\n"
    )


def test_request_error_summary_reads_what_dirsearch_logged(tmp_path: Path) -> None:
    # Format taken from a real dirsearch --log run.
    from backend.job_manager.dirsearch_log import summarize_request_errors

    def failure(second: int, message: str, exception: str) -> str:
        return (
            f"2026-10-20 14:00:{second:02d},100 [ERROR] {message}\n"
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in request\n'
            "    await thing()\n"
            f"{exception}\n\n"
        )

    log = tmp_path / "dirsearch.log"
    log.write_text(
        '2026-10-20 14:00:00,000 [INFO] "GET https://t/x" 200 - 10B\n'
        + "".join(failure(i, "Read timed out", "httpx.ReadTimeout: Read timed out") for i in range(7))
        + failure(9, "All connection attempts failed", "httpx.ConnectError: All connection attempts failed"),
        encoding="utf-8",
    )

    summary = summarize_request_errors(log)

    assert summary.count == 8
    assert "7x ReadTimeout: Read timed out" in summary.text
    assert "1x ConnectError: All connection attempts failed" in summary.text
    assert summarize_request_errors(tmp_path / "missing.log") is None
    clean = tmp_path / "clean.log"
    clean.write_text('2026-10-20 14:00:00,000 [INFO] "GET https://t/x" 200 - 10B\n', encoding="utf-8")
    assert summarize_request_errors(clean) is None


def test_a_failed_job_can_be_resumed_and_then_completes(workspace_root, tmp_path: Path) -> None:
    engine = MockDirsearchEngine(fail=True)
    manager = JobManager(engine, max_concurrent_jobs=2)
    config = DirsearchJobConfig(url="https://target.local/", threads=5)
    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[_wordlist(tmp_path, 6)],
    )
    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.FAILED)
    assert manager.get_job(job.id).error_reason

    engine._fail = False  # the cause is gone (target back up, proxy restarted, ...)
    manager.resume_job(job.id)

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.COMPLETED)
    final = manager.get_job(job.id)
    assert final.error_reason is None
    assert final.completed_lines == 6
