import asyncio
import functools
import http.server
import threading
import time
from pathlib import Path

import pytest

from backend.engine.dirsearch import DirsearchEngine
from backend.events.bus import EventBus
from backend.job_manager.manager import JobManager
from backend.models.job import JobStatus
from backend.models.job_config import DirsearchJobConfig
from backend.persistence import app_settings

pytestmark = pytest.mark.asyncio


def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


@pytest.fixture()
def local_site(tmp_path: Path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("ok", encoding="utf-8")
    (site_dir / "admin.html").write_text("admin", encoding="utf-8")

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_real_dirsearch_completes_and_reports_results(
    workspace_root, tmp_path: Path, local_site: str
) -> None:
    # Real dirsearch, real subprocess -- not mocked. DirsearchEngine.uses_pty
    # is False (ConPTY was confirmed to cause a real, unfixable-from-here OS
    # focus-stealing bug -- explicit user decision to drop it entirely rather
    # than keep fighting it) -- but this DOES have live job.progress again
    # since 2026-09-24, without ConPTY: dirsearch's own progress bar only
    # ever needed shutil.get_terminal_size() to report a wide enough width,
    # which JobManager satisfies via plain COLUMNS/LINES env vars on this
    # ordinary pipe (see manager.py's PROGRESS_ENV_COLUMNS comment). Asserts
    # on that end-to-end, against real dirsearch output, not just the parser
    # in isolation (test_pty_output.py already covers that).
    bus = EventBus()
    loop = asyncio.get_running_loop()
    queue = bus.subscribe(loop)

    manager = JobManager(DirsearchEngine(), event_bus=bus)
    wordlist = tmp_path / "words.txt"
    wordlist.write_text("\n".join(f"word{i}" for i in range(100)) + "\nindex\nadmin\n", encoding="utf-8")
    config = DirsearchJobConfig(
        url=local_site, threads=2, extensions=("html",), force_extensions=True, delay=0.01
    )

    manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[wordlist],
    )

    result_events = []
    progress_events = []
    deadline = loop.time() + 15
    final_status = None
    while loop.time() < deadline:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if event.name == "job.result":
            result_events.append(event.data)
        elif event.name == "job.progress":
            progress_events.append(event.data)
        elif event.name in ("job.completed", "job.failed", "job.stopped"):
            final_status = event.name
            break

    assert final_status == "job.completed"

    found_paths = {r["url"] for batch in result_events for r in batch["results"]}
    assert any("index" in u or "admin" in u for u in found_paths)

    # More than one progress event proves dirsearch's own progress bar
    # actually made it through the plain pipe live, throughout the run --
    # not just that a single job.progress event fired at some point.
    assert len(progress_events) > 1


async def test_force_extensions_with_no_chosen_extensions_only_adds_the_trailing_slash(
    workspace_root, tmp_path: Path, local_site: str
) -> None:
    # Real, reported bug (2026-09-25): dirsearch's own options.py falls back
    # to its shipped config.ini's "default-extensions" (php,asp,aspx,jsp,
    # html,htm) whenever -e is never passed at all -- Force extensions with
    # an EMPTY extensions field silently tested 8 real requests per eligible
    # wordlist line (line, line/, plus the 6 unchosen extensions) instead of
    # the 2 (line, line/) a user picking that combination would reasonably
    # expect. Explicitly opting into "use none" here (the Settings option
    # added the same day, once the user pointed out the fix shouldn't just
    # be silently forced on everyone -- see app_settings.
    # get_default_extensions_fallback's own docstring) via
    # `set_default_extensions_fallback("")`; this proves the resulting
    # override actually takes effect against a REAL dirsearch subprocess,
    # not just the argv shape (test_dirsearch_engine.py already covers
    # that) -- dirsearch's own live-reported progress.length (surfaced via
    # job.progress's own overall_total, since 2026-09-25's progress-denominator
    # fix) is the actual proof: it must equal 2, the wordlist_resolver.py
    # estimate for one eligible line with force_extensions and zero
    # extensions -- if the override weren't taking effect, it would be 8
    # instead.
    app_settings.set_default_extensions_fallback("")
    bus = EventBus()
    loop = asyncio.get_running_loop()
    queue = bus.subscribe(loop)

    manager = JobManager(DirsearchEngine(), event_bus=bus)
    wordlist = tmp_path / "words.txt"
    wordlist.write_text("onlyword\n", encoding="utf-8")
    config = DirsearchJobConfig(url=local_site, threads=1, force_extensions=True, delay=0.01)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[wordlist],
    )
    assert job.total_wordlist_requests == 2  # our own estimate: {onlyword, onlyword/}

    progress_events = []
    final_status = None
    deadline = loop.time() + 15
    while loop.time() < deadline:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if event.name == "job.progress":
            progress_events.append(event.data)
        elif event.name in ("job.completed", "job.failed", "job.stopped"):
            final_status = event.name
            break

    assert final_status == "job.completed"
    assert progress_events, "expected at least one live progress event"
    # dirsearch's OWN real total for this run, surfaced via overall_total --
    # 2, not 8, proves the config override actually suppressed the silent
    # default-extensions fallback in the real subprocess, not just in argv.
    assert all(event["overall_total"] == 2 for event in progress_events)


async def test_real_dirsearch_stop_kills_the_actual_process(
    workspace_root, tmp_path: Path, local_site: str
) -> None:
    import psutil

    manager = JobManager(DirsearchEngine(), event_bus=EventBus())
    wordlist = tmp_path / "words.txt"
    wordlist.write_text("\n".join(f"word{i}" for i in range(500)), encoding="utf-8")
    config = DirsearchJobConfig(url=local_site, threads=1, delay=0.05)

    job = manager.create_job(
        project_id="p1", target_id="t1", path="/",
        config=config, wordlist_files=[wordlist],
    )

    _wait_until(lambda: manager.get_job(job.id).pid is not None, timeout=10)
    pid = manager.get_job(job.id).pid
    assert psutil.pid_exists(pid)

    manager.stop_job(job.id)

    _wait_until(lambda: manager.get_job(job.id).status == JobStatus.STOPPED, timeout=10)
    _wait_until(lambda: not psutil.pid_exists(pid), timeout=5)
