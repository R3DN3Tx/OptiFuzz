from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from backend import platform_compat
from backend.engine.base import FuzzingEngine
from backend.events.bus import EventBus
from backend.events.bus import event_bus as default_event_bus
from backend.job_manager.dirsearch_log import RequestErrorSummary, summarize_request_errors
from backend.job_manager.pty_output import LiveOutputParser
from backend.job_manager.result_batcher import ResultBatcher
from backend.job_manager.wordlist_resolver import (
    estimate_requests,
    find_resume_point,
    load_wordlist_lines,
    resolve_wordlist,
    write_remainder_wordlist,
)
from backend.models.job import TERMINAL_STATUSES, FuzzJob, JobStatus
from backend.models.job_config import DirsearchJobConfig, config_from_dict, config_to_dict
from backend.models.result import Result
from backend.models.timestamps import now_iso
from backend.parser.stdout_parser import ParsedProgress, ParsedResult
from backend.persistence import app_settings, layout
from backend.persistence.atomic import read_json, write_json_atomic
from backend.services import output_mirror_service, project_service, results_service, target_service, tree_service

if TYPE_CHECKING:
    # Only for type hints -- the real import is lazy (see _run_chunk_pty)
    # because the winpty package it depends on is Windows-only and must
    # never be a hard import-time requirement for the whole module (spec:
    # keep a clean seam for a future Linux port).
    from backend.job_manager.pty_runner import PtyChunkProcess

# HISTORICAL (no longer the active path): every chunk used to spawn its own
# PtyChunkProcess (a fresh ConPTY, via pywinpty) for live progress, and that
# spawn+teardown had a fixed, ~8-SECOND cost, independent of what actually
# ran. ConPTY was dropped entirely (DirsearchEngine.uses_pty = False now --
# a real, confirmed, unfixable-from-here OS focus-stealing bug) in favor of
# the plain-pipe `_run_segment` path below. `_run_chunk_pty`/PtyChunkProcess
# are kept only for MockDirsearchEngine's own opt-in PTY testing path.
#
# HISTORICAL, PART 2 -- fixed-size chunked execution itself, removed
# 2026-09-24. A job used to slice its wordlist into many small fixed-size
# files (DEFAULT_CHUNK_SIZE=200 lines) and run one SEPARATE subprocess per
# chunk, sequentially -- ~1.7-1.9s of pure interpreter-startup/import
# overhead PER CHUNK, confirmed by direct benchmark 2026-09-23 (the bundled
# 24,348-line wordlist ran as ~122 chunks, ~3.5-4 minutes of pure spawn
# overhead alone, before a single real HTTP request -- a real, reported
# "fuzzing feels much slower with chunking" complaint, not a perception).
# That model existed purely to make pause/resume durable (redo at most one
# chunk) -- it had NOTHING to do with progress visibility even at the time
# (chunk size never affected live-progress granularity either way).
#
# Replaced with: ONE continuous dirsearch process per run segment, against
# the job's full resolved wordlist (`wordlist_resolver.resolve_wordlist`,
# written once at job creation). Pausing kills that process (exactly as
# before -- `_terminate_with_escalation`, no OS signal needed) and computes
# a safe resume point from dirsearch's own live progress index instead of a
# pre-sliced chunk boundary (`_checkpoint_pause`) -- see its own comment for
# the "dispatched, not completed" subtlety this has to account for. A
# resume re-slices a "remainder" wordlist (`write_remainder_wordlist`) from
# that point instead of restarting a fixed 200-line file.
#
# This was investigated as an alternative to dirsearch's own `-s`/
# `--session` resumable-session mechanism (confirmed real, JSON-based --
# `lib/controller/session.py` -- not a hypothetical) after that route hit an
# empirically-confirmed dead end on Windows: dirsearch's own graceful-pause
# handler only fires on SIGINT/SIGTERM (`lib/controller/controller.py`), and
# NONE of Windows' three ways to signal a child process can reach it for a
# console-less subprocess like every one this app spawns --
# `CTRL_BREAK_EVENT` is delivered as `SIGBREAK`, which dirsearch doesn't
# handle; `CTRL_C_EVENT` can't be targeted at a single process (broadcasts
# to a whole console process group); and `os.kill(pid, SIGTERM)`/
# `.terminate()` map to `TerminateProcess()`, a hard kill that never runs
# any Python-level handler at all. Verified directly, not assumed: sent a
# real `CTRL_BREAK_EVENT` to a real dirsearch child (spawned with
# `CREATE_NEW_PROCESS_GROUP`) mid-run -- it kept fuzzing, no pause menu ever
# appeared. Reopening that path would mean reintroducing some form of
# console (real or ConPTY) purely to make a signal deliverable -- exactly
# the door this project already closed after three rounds of focus-stealing
# investigation -- so the line-level
# checkpoint approach here was chosen specifically because it never needs a
# signal delivered at all: pausing is still just killing the process, the
# same primitive already used everywhere else in this file.
#
# Known, accepted tradeoff (documented, not silently swallowed): this only
# checkpoints `completed_lines`/`completed_requests` at a deliberate pause,
# not periodically during a live run. A genuine crash (app killed, power
# loss -- as opposed to a deliberate Pause) still leaves an orphaned
# RUNNING/QUEUED job `PAUSED` on the next start (`_load_persisted_jobs`,
# ), and it's still genuinely resumable -- but now redoes the whole
# CURRENT RUN SEGMENT from wherever completed_lines last was, not just "one
# chunk" as before, since nothing incrementally persists progress mid-run.
DEFAULT_MAX_CONCURRENT_JOBS = 2
STOP_GRACE_SECONDS = 5.0
RESULT_BATCH_INTERVAL_SECONDS = 0.25
STDERR_TAIL_LINES = 20

# dirsearch can end a scan on its own and still exit with code 0: after
# MAX_CONSECUTIVE_REQUEST_ERRORS (75) request errors in a row, a skip-on-status
# hit, or the target time limit, it prints one of these messages and moves on
# (with a single target, that is the end). Exit code 0 alone therefore does not
# mean the wordlist was finished.
EARLY_END_MESSAGE_RE = re.compile(
    r"(Too many request errors|Skipped the target due to \d+ status code"
    r"|Runtime for target exceeded[^\r\n]*|Canceled due to an error)"
)
# Any request error dirsearch cannot absorb (e.g. "Failed to read response body: <url>" when
# the server resets a connection) aborts the scan the same way: it prints the error as the
# last thing before "Task Completed" and exits 0. Such a line is neither a result
# ("[hh:mm:ss] 200 - ...") nor a progress bar ("12% 123/400").
_RESULT_LINE_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]")
_PROGRESS_TEXT_RE = re.compile(r"\d+%\s+\d+/\d+")
STDOUT_TAIL_CHARS = 4096
# The last progress dirsearch printed may trail the true end a little; only an end
# further from the total than this (a fraction of it, or a few threads' worth) is
# treated as "stopped early".
EARLY_END_TOLERANCE_FRACTION = 0.02
RAW_TAIL_CHARS_FOR_ERRORS = 4000  # PTY mode has no separate stderr (see below)
PROGRESS_PUBLISH_MIN_INTERVAL_SECONDS = 0.2  # don't flood the SSE stream
STDOUT_READ_CHUNK_SIZE = 4096
# How often a live run persists its own checkpoint to disk (2026-09-24),
# independent of a deliberate Pause -- bounds how much a genuine crash (app
# killed, power loss) can cost to "since the last periodic checkpoint"
# instead of "the whole current run segment", the tradeoff this module's own
# comment above DEFAULT_MAX_CONCURRENT_JOBS previously flagged as a known,
# accepted gap. Every write is a full job.json rewrite (cheap, already done
# on every other state change) and the checkpoint math itself
# (`_compute_checkpoint`) is the same one a real Pause already runs, just
# invoked repeatedly instead of once -- 15s keeps disk writes infrequent
# while still bounding worst-case crash loss to a small, human-reasonable
# window.
PERIODIC_CHECKPOINT_INTERVAL_SECONDS = 15.0

# Real, live progress on the plain-pipe path (2026-09-24), no ConPTY. Root
# cause of "a plain pipe never gets dirsearch's progress bar" was never the
# missing console -- it's dirsearch's own width guard on the progress bar
# (`interface.last_path`, lib/view/terminal.py): `if
# len(clean_color(progress_bar)) >= shutil.get_terminal_size()[0]: return`.
# With no real terminal attached, Python's shutil.get_terminal_size() falls
# back to a hardcoded 80 columns, and the bar (~85-90 chars, same "~90 chars"
# pty_runner.DIMENSIONS was already sized around) never fits -- so dirsearch
# silently drops every single progress line, verified directly: a real
# dirsearch run with stdout redirected to a plain file produced zero
# progress lines at the default width, and correct, real ones (`[#### ] 22%
# 12/53  7/s  job:1/1  errors:0`) the instant COLUMNS/LINES were set wide in
# the child's own environment -- no PTY, no console, no focus-stealing risk
# at all (confirmed: colorama's own GetConsoleScreenBufferInfo call, used by
# dirsearch's cursor-erase logic, silently ignores failure on a non-console
# handle rather than raising -- that was never the blocker either). `-q` is
# gone from build_command (see its own comment) so dirsearch actually emits
# this bar; these two env vars are what make it wide enough to survive its
# own guard.
PROGRESS_ENV_COLUMNS = "250"
PROGRESS_ENV_LINES = "50"


class JobNotFoundError(Exception):
    pass


class JobManager:
    """Owns the whole Fuzz Job lifecycle: queueing, per-target concurrency
    limits, continuous-subprocess execution with pause/resume checkpointing,
    and live event publishing. One instance is
    shared by every
    Project/Target -- each target queues/dispatches against its own
    concurrency limit (get_target_concurrency/set_target_concurrency),
    independent of every other target's.

    Process orchestration runs entirely in plain threads (never asyncio
    subprocess), for Windows safety.
    """

    def __init__(
        self,
        engine: FuzzingEngine,
        *,
        max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
        event_bus: EventBus | None = None,
    ) -> None:
        self._engine = engine
        # Fallback used for any target with no explicit per-target override
        # persisted yet (see get_target_concurrency/set_target_concurrency).
        self._default_max_concurrent_jobs = max_concurrent_jobs
        self._event_bus = event_bus or default_event_bus

        self._lock = threading.RLock()
        self._pending: deque[str] = deque()
        self._running: set[str] = set()
        self._jobs: dict[str, FuzzJob] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._pty_processes: dict[str, "PtyChunkProcess"] = {}
        # Results of every job whose dirsearch process is live right now --
        # see results_service.list_results. Seeded from the report on disk
        # before a segment starts, appended as results arrive, dropped once
        # the process has exited (the file is safe to read again then).
        self._live_results: dict[str, list[Result]] = {}
        # Findings already counted in each running job's result_count, so the
        # Jobs count matches what Results shows (see _finding_key).
        self._counted_findings: dict[str, set[tuple]] = {}
        # Why a job whose dirsearch exited 0 did not actually reach the end of its
        # wordlist (see _early_end_reason); read once by _run_job.
        self._early_ends: dict[str, str] = {}

        self._load_persisted_jobs()

    def _load_persisted_jobs(self) -> None:
        """Without this, every process restart (a crash, a code reload, the
        user closing the terminal) silently wipes every job -- and with it
        every Result -- from the app, even though job.json and the dirsearch
        reports are still sitting on disk untouched. A job caught RUNNING or
        QUEUED had its subprocess die with the old process; treat it exactly
        like a normal pause (resumable from completed_lines -- see
        FuzzJob's own docstring) rather than leaving it stuck claiming to be
        active forever.
        """
        projects_root = layout.projects_dir()
        if not projects_root.is_dir():
            return
        for project_dir in projects_root.iterdir():
            targets_root = project_dir / "targets"
            if not targets_root.is_dir():
                continue
            for target_dir in targets_root.iterdir():
                jobs_root = target_dir / "jobs"
                if not jobs_root.is_dir():
                    continue
                for job_file in jobs_root.glob("*.json"):
                    try:
                        job = FuzzJob.from_dict(read_json(job_file))
                    except (OSError, ValueError, KeyError):
                        continue
                    if job.status in (JobStatus.RUNNING, JobStatus.QUEUED):
                        job.status = JobStatus.PAUSED
                        job.pid = None
                        # Not a deliberate user Pause -- the app/process
                        # itself died -- so tag it the same way the
                        # auto-pause-on-status feature does, purely so the
                        # frontend's "⚠ auto" indicator tells the pentester
                        # this wasn't their own action (explicit request,
                        # 2026-09-24).
                        job.auto_paused = True
                        self._save(job)
                    self._jobs[job.id] = job

    # -- creation ---------------------------------------------------------

    def create_job(
        self,
        *,
        project_id: str,
        target_id: str,
        path: str,
        config: DirsearchJobConfig,
        wordlist_files: list[Path],
        pause_on_status: list[int] | None = None,
        output_stem: str = "",
        output_flat: bool = False,
    ) -> FuzzJob:
        job_id = uuid.uuid4().hex[:12]
        wordlist_dir = layout.job_wordlist_dir(project_id, target_id, job_id)
        resolved = resolve_wordlist(
            wordlist_files, wordlist_dir / "full.txt",
            extensions=list(config.extensions), force_extensions=config.force_extensions,
        )

        job = FuzzJob(
            id=job_id,
            target_id=target_id,
            project_id=project_id,
            path=path,
            config=config_to_dict(config),
            status=JobStatus.QUEUED,
            created_at=now_iso(),
            total_wordlist_lines=resolved.total_lines,
            total_wordlist_requests=resolved.total_requests,
            wordlist_sources=[str(p) for p in wordlist_files],
            pause_on_status=list(pause_on_status or []),
            output_stem=output_stem,
            output_flat=output_flat,
        )
        self._save(job)

        with self._lock:
            self._jobs[job_id] = job
            self._pending.append(job_id)

        self._publish("job.queued", job)
        self._dispatch()
        return job

    # -- queries ------------------------------------------------------------

    def get_job(self, job_id: str) -> FuzzJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def list_jobs(self, target_id: str) -> list[FuzzJob]:
        with self._lock:
            return [job for job in self._jobs.values() if job.target_id == target_id]

    def engine_version(self) -> str | None:
        return self._engine.detect_version()

    def get_target_concurrency(self, target_id: str) -> int:
        return app_settings.get_target_concurrency(target_id) or self._default_max_concurrent_jobs

    def set_target_concurrency(self, target_id: str, value: int) -> int:
        """Change one target's concurrency limit at runtime -- per-target, not
        global (each target queues/dispatches against its own limit; other
        targets' jobs are unaffected). Raising it can immediately dispatch
        jobs of *this* target that were waiting in the queue; lowering it
        never interrupts jobs already RUNNING."""
        if value < 1:
            raise ValueError("max_concurrent_jobs must be >= 1")
        app_settings.set_target_concurrency(target_id, value)
        self._dispatch()
        return value

    def dispatch(self) -> None:
        """Public trigger to re-check the queue (e.g. after a settings change
        that didn't itself go through create_job/resume_job/stop_job)."""
        self._dispatch()

    def live_results(self) -> dict[str, list[Result]]:
        with self._lock:
            return {job_id: list(items) for job_id, items in self._live_results.items()}

    # -- control --------------------------------------------------------------

    def stop_job(self, job_id: str) -> FuzzJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.status in TERMINAL_STATUSES:
                return job

            job.stop_requested = True
            proc = self._processes.get(job_id)
            pty_proc = self._pty_processes.get(job_id)
            was_pending = job_id in self._pending
            was_paused = job.status == JobStatus.PAUSED
            if was_pending:
                self._pending.remove(job_id)

        if proc is not None:
            self._terminate_with_escalation(proc)
        elif pty_proc is not None:
            pty_proc.terminate(force=True)
        elif was_pending or was_paused:
            # QUEUED (never started) or PAUSED (no active process either way)
            # -- nothing to terminate, finalize directly instead of waiting
            # for a runner thread that will never run again on its own.
            with self._lock:
                self._set_status_and_save(job, JobStatus.STOPPED)
            self._publish("job.stopped", job)

        return job

    def pause_job(self, job_id: str) -> FuzzJob:
        """Manually pause a QUEUED/RUNNING job -- the exact same graceful
        cut-the-current-run-short mechanism `_maybe_trigger_auto_pause`
        already uses for the auto-pause-on-status feature, just triggered by
        the pentester directly (a Pause button) instead of a detected status
        code. Resumable later via resume_job(), from `job.completed_lines`
        (see `_checkpoint_pause`) -- only the handful of requests already
        in flight when the process was killed get redone, not a whole fixed
        chunk (2026-09-24 -- see this module's own comment above
        DEFAULT_MAX_CONCURRENT_JOBS for the full story).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.status in TERMINAL_STATUSES or job.status == JobStatus.PAUSED:
                return job

            job.pause_requested = True
            job.auto_paused = False
            job.auto_pause_status = None  # a manual pause is never the auto-pause-on-status feature
            proc = self._processes.get(job_id)
            pty_proc = self._pty_processes.get(job_id)
            was_pending = job_id in self._pending
            if was_pending:
                self._pending.remove(job_id)

        if proc is not None:
            self._terminate_with_escalation(proc)
        elif pty_proc is not None:
            pty_proc.terminate(force=True)
        elif was_pending:
            # QUEUED, never actually started running -- nothing to
            # terminate, finalize directly instead of waiting for a runner
            # thread that will now never run for it (same reasoning as
            # stop_job's own was_pending/was_paused branch).
            with self._lock:
                job.status = JobStatus.PAUSED
                job.ended_at = now_iso()
            self._save(job)
            self._publish("job.paused", job)

        return job

    def pause_all(self) -> int:
        """Pause every QUEUED/RUNNING job (any target); returns how many were
        asked to pause. Used by the in-app update, which replaces the code the
        runner threads and dirsearch run from."""
        with self._lock:
            active = [j.id for j in self._jobs.values() if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)]
        for job_id in active:
            self.pause_job(job_id)
        return len(active)

    def resume_job(self, job_id: str) -> FuzzJob:
        """Resume a PAUSED (or FAILED) job from `job.completed_lines` -- a freshly-sliced remainder wordlist, not a pre-existing
        chunk file (see `_run_job`). Only the small, thread-bounded window of
        requests that were in flight when the run was cut short gets redone,
        the same "never lose a word" guarantee as before, just far more
        fine-grained."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.status not in (JobStatus.PAUSED, JobStatus.FAILED):
                return job

            job.status = JobStatus.QUEUED
            job.pause_requested = False
            job.auto_paused = False
            job.auto_pause_status = None
            job.error_reason = None
            job.stop_requested = False
            job.pid = None
            self._pending.append(job_id)

        self._save(job)
        self._publish("job.queued", job)
        self._dispatch()
        return job

    def delete_job(self, job_id: str) -> None:
        """Permanently remove a finished job: its record, its resolved
        wordlist, and its saved report files (spec: deleting a job removes
        the row, its results, and its output .txt). Refuses to delete an
        active job rather than silently killing it first."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.status not in TERMINAL_STATUSES:
                raise ValueError("Only a finished job (completed/failed/stopped) can be deleted")
            del self._jobs[job_id]
            self._running.discard(job_id)

        shutil.rmtree(
            layout.jobs_dir(job.project_id, job.target_id) / job_id, ignore_errors=True
        )
        layout.job_file(job.project_id, job.target_id, job_id).unlink(missing_ok=True)

        # Best-effort mirror cleanup (explicit request: a deleted job's
        # output should disappear from the configured mirror directory
        # too) -- looked up and removed BEFORE the primary .txt is deleted
        # below, purely so this always runs even if something about the
        # primary cleanup path below were to raise; never allowed to block
        # or fail the job's own deletion either way.
        try:
            project = project_service.get_project(job.project_id)
            if project.outputs_mirror_dir:
                target = target_service.get_target(job.project_id, job.target_id)
                # job_output_base_current, not _for: whatever name was
                # ACTUALLY used when this job's output was mirrored (almost
                # always the final name, but the working name in the rare
                # case its rename never succeeded -- see
                # JobManager._finalize_output_filename).
                base = layout.job_output_base_current(job.project_id, job.target_id, job)
                output_mirror_service.remove_mirrored_file(project, target.name, f"{base.name}.txt")
        except Exception:
            pass

        if job.output_stem and not job.output_flat:
            shutil.rmtree(
                layout.job_output_dir(job.project_id, job.target_id, job_id), ignore_errors=True
            )
        else:
            # NOT base.with_suffix(".txt")/(".json"): the stem can itself
            # contain dots (e.g. an IP address in the target name, like
            # "127.0.0.1") -- with_suffix() treats everything after the
            # LAST dot as an existing extension to replace, silently
            # truncating the path (same bug already documented and worked
            # around in services/results_service.py -- this call site had
            # it too, unfixed, until now).
            # layout.unlink, not a plain Path.unlink() -- base can be the
            # job's full, human-readable final name, which can genuinely
            # exceed MAX_PATH (see persistence/paths.py::long_path's own
            # docstring).
            base = layout.job_output_base_current(job.project_id, job.target_id, job)
            layout.unlink(base.parent / f"{base.name}.txt", missing_ok=True)
            layout.unlink(base.parent / f"{base.name}.json", missing_ok=True)

    def _terminate_with_escalation(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        platform_compat.terminate_process(proc)

        def _escalate() -> None:
            try:
                proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()

        threading.Thread(target=_escalate, daemon=True).start()

    # -- dispatch / execution -----------------------------------------------

    def _dispatch(self) -> None:
        with self._lock:
            running_per_target: dict[str, int] = {}
            for jid in self._running:
                t = self._jobs[jid].target_id
                running_per_target[t] = running_per_target.get(t, 0) + 1

            still_pending: deque[str] = deque()
            while self._pending:
                job_id = self._pending.popleft()
                job = self._jobs[job_id]
                if job.stop_requested:
                    self._set_status_and_save(job, JobStatus.STOPPED)
                    self._publish("job.stopped", job)
                    continue

                current = running_per_target.get(job.target_id, 0)
                if current < self.get_target_concurrency(job.target_id):
                    running_per_target[job.target_id] = current + 1
                    self._running.add(job_id)
                    threading.Thread(target=self._run_job, args=(job_id,), daemon=True).start()
                else:
                    still_pending.append(job_id)
            self._pending = still_pending

    def _run_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        job.status = JobStatus.RUNNING
        # Only the FIRST time this job ever runs -- every chunk after a
        # pause/resume (or an auto-pause) re-enters here too, and
        # overwriting it on each one was a real, reported bug: the
        # frontend's elapsed-time display (computeElapsedMs) anchors on
        # this exact field, so it silently reset to ~0 on every resume
        # instead of continuing from where the job actually started.
        if job.started_at is None:
            job.started_at = now_iso()
        self._save(job)
        self._publish("job.started", job)

        config = config_from_dict(job.config)
        final_base = layout.job_output_base_for(
            job.project_id, job.target_id, job_id, job.output_stem, job.output_flat
        )
        using_working_name = self._should_use_working_output_name(job, final_base)
        output_base = (
            layout.job_output_base_working(job.project_id, job.target_id, job_id)
            if using_working_name
            else final_base
        )
        output_base.parent.mkdir(parents=True, exist_ok=True)
        wordlist_dir = layout.job_wordlist_dir(job.project_id, job.target_id, job_id)
        full_wordlist = wordlist_dir / "full.txt"
        batcher: ResultBatcher[ParsedResult] = ResultBatcher(
            flush=lambda items: self._flush_results(job_id, items),
            interval=RESULT_BATCH_INTERVAL_SECONDS,
        )

        final_status = JobStatus.COMPLETED
        try:
            if job.stop_requested:
                final_status = JobStatus.STOPPED
            elif job.pause_requested:
                final_status = JobStatus.PAUSED
            elif not self._engine.is_target_reachable(config.url):
                # Pre-flight check, before ever spawning dirsearch or reading
                # the wordlist -- see FuzzingEngine.is_target_reachable's own
                # docstring for why this can't be detected any other way.
                job.error_reason = "Unreachable host"
                final_status = JobStatus.FAILED
            else:
                # One continuous process for the whole remaining wordlist
                # (2026-09-24 -- no more fixed-size chunk subprocesses, see
                # this module's own comment above DEFAULT_MAX_CONCURRENT_JOBS).
                # `lines` is needed both to build the remainder file below
                # AND later by `_checkpoint_pause` if this segment gets cut
                # short, so it's read once, up front, rather than twice.
                #
                # Reading the wordlist file directly (OSError possible, e.g.
                # the workspace/job directory vanished from under a still-
                # running job) is new here -- the old chunk-file model never
                # had this app's own Python code open a wordlist file at all,
                # only handed dirsearch's subprocess a path and let ITS
                # exit code report a missing file. Caught the same way here:
                # a real, reported FAILED job with a reason, never an
                # unhandled exception silently killing this whole background
                # thread and leaving the job stuck at RUNNING forever.
                try:
                    lines = load_wordlist_lines(full_wordlist)
                    segment_wordlist = full_wordlist
                    if job.completed_lines > 0:
                        segment_wordlist = wordlist_dir / "remainder.txt"
                        write_remainder_wordlist(lines, job.completed_lines, segment_wordlist)
                except OSError as exc:
                    job.error_reason = f"Could not read this job's wordlist: {exc}"
                    final_status = JobStatus.FAILED
                    lines = None

                if lines is not None:
                    with self._lock:
                        self._live_results[job_id] = results_service.read_job_results(
                            job.project_id, job.target_id, job
                        )
                        self._counted_findings[job_id] = {
                            _finding_key(r) for r in self._live_results[job_id]
                        }
                    if self._engine.uses_pty:
                        exit_code, error_reason = self._run_chunk_pty(
                            job, segment_wordlist, output_base, config, batcher, lines
                        )
                    else:
                        exit_code, error_reason = self._run_segment(
                            job, segment_wordlist, output_base, config, batcher, lines
                        )

                    if job.stop_requested:
                        final_status = JobStatus.STOPPED
                    elif job.pause_requested:
                        final_status = JobStatus.PAUSED
                    elif exit_code != 0:
                        job.error_reason = error_reason or f"dirsearch exited with code {exit_code}"
                        final_status = JobStatus.FAILED
                    elif job.id in self._early_ends:
                        # dirsearch gave up on its own: PAUSED at the real position,
                        # with the reason, so Resume can finish it (100% is not claimed).
                        job.error_reason = self._early_ends.pop(job.id)
                        final_status = JobStatus.PAUSED
                    else:
                        job.completed_lines = job.total_wordlist_lines
                        job.completed_requests = job.total_wordlist_requests
                        final_status = JobStatus.COMPLETED
        finally:
            batcher.flush_now()
            with self._lock:
                self._live_results.pop(job_id, None)
                self._counted_findings.pop(job_id, None)

        self._set_status_and_save(job, final_status)
        if final_status != JobStatus.PAUSED and using_working_name:
            # Not on PAUSE -- the job isn't actually done yet (a resume will
            # follow, still stays at its working name for that) --
            # genuinely finished (or given up): rename working -> final
            # BEFORE the placeholder-fix/mirror below, so both operate on
            # the real, full, human-readable name -- best-effort (falls
            # back to leaving it at its working name if the rename itself
            # fails, rather than losing track of it entirely).
            output_base = self._finalize_output_filename(output_base, final_base)
        self._fix_output_command_line(job, output_base)
        if final_status != JobStatus.PAUSED:
            # Not on PAUSE -- the job isn't actually done yet (a resume will
            # follow, the report is only partial); mirror once it's
            # genuinely finished (or has given up), same as the primary
            # outputs/ report itself is only ever considered "the result"
            # at that point.
            self._mirror_output_if_configured(job, output_base)
        self._publish(f"job.{final_status.value.lower()}", job)

        with self._lock:
            self._running.discard(job_id)
        self._dispatch()

    def _should_use_working_output_name(self, job: FuzzJob, final_base: Path) -> bool:
        """New jobs write to a short, always-Windows-safe working name WHILE
        dirsearch might still be appending to them (job_id alone, ~12 hex
        chars -- see layout.job_output_base_working's own comment for why:
        an unbounded fuzzed path embedded directly in the filename could
        push the full on-disk path past Windows' MAX_PATH and fail the job
        outright, a real reported bug). Renamed to the full, human-readable
        final name by `_finalize_output_filename`, once nothing will ever
        write to it again.

        Decided by actual file existence (mirrors `layout.job_output_base_current`'s
        own read-side logic exactly, per that function's own docstring), NOT
        by `last_completed_chunk` -- real, reported bug fixed here:
        `last_completed_chunk == 0` used to stand in for "this is a brand
        new job", but a job resumed after 1+ chunks had already completed
        under the working name (any pause -- auto or manual -- past its
        first chunk) also has `last_completed_chunk > 0`, and got
        misclassified as the OLD legacy direct-to-final-name scheme. That
        pointed the resumed run at finalBase instead, which didn't exist yet
        -- dirsearch happily started a brand new, empty report there, and
        every result already sitting in the working-name file (and already
        counted in job.result_count, which is never affected either way)
        was silently orphaned: never merged, never shown in Results, never
        in the final .txt/.json, and never renamed by `_finalize_output_filename`
        either (which only touches `working_base`, not this stranded file).
        """
        working_base = layout.job_output_base_working(job.project_id, job.target_id, job.id)
        # path_is_file, not a plain Path.is_file() -- final_base can
        # genuinely exceed MAX_PATH once it's a long, human-readable name
        # (see persistence/paths.py::long_path's own docstring).
        if layout.path_is_file(working_base.parent / f"{working_base.name}.txt"):
            return True  # already using the working name from an earlier run segment -- keep using it
        legacy_txt = final_base.parent / f"{final_base.name}.txt"
        if layout.path_is_file(legacy_txt):
            return False  # pre-upgrade job, already writing directly to its final name
        return True  # brand new job, its very first chunk

    def _finalize_output_filename(self, working_base: Path, final_base: Path) -> Path:
        """Renames a job's report files from their short, Windows-safe
        working name to the full, human-readable final name, once the job
        has reached a terminal status and nothing will ever write to them
        again.

        Uses `layout.long_path()` (Windows' extended-length path prefix) for
        the DESTINATION only -- this app's own code did the write to the
        (short) source, so this is the one place a long destination path is
        actually safe to rely on; dirsearch's own subprocess never saw a
        long path at all, by design, so its internal file I/O is never at
        risk here regardless of length.

        Best-effort: if a rename fails (a locked handle, permissions, ...),
        that one file is simply left at its working name rather than
        raising -- `layout.job_output_base_current()` already knows to fall
        back to a job's working name when its final name isn't there, so a
        failed rename degrades to "shows a less friendly filename", never
        "results silently disappear".
        """
        final_base.parent.mkdir(parents=True, exist_ok=True)
        for ext in ("txt", "json"):
            src = working_base.parent / f"{working_base.name}.{ext}"
            if not src.is_file():
                continue
            dst = final_base.parent / f"{final_base.name}.{ext}"
            try:
                os.replace(layout.long_path(src), layout.long_path(dst))
            except OSError:
                pass
        # path_is_file, not a plain Path.is_file() -- final_base can
        # genuinely exceed MAX_PATH (see paths.py::long_path's own
        # docstring): a plain is_file() check here would report the rename
        # as having failed even when it actually succeeded.
        renamed = layout.path_is_file(final_base.parent / f"{final_base.name}.txt")
        return final_base if renamed else working_base

    def _fix_output_command_line(self, job: FuzzJob, output_base: Path) -> None:
        """Replaces the saved report's own command-line header (.txt's
        first line, .json's `info.args`) with the REAL command this app
        actually ran, instead of dirsearch's own capture of it -- two
        separate problems with the latter, not just one:

        - It embeds this app's own `-o <path>.{extension}` CLI argument
          literally (dirsearch's required template syntax for writing
          multiple report formats from one invocation), since dirsearch
          only substitutes `{extension}` per format internally, when it
          actually opens each file -- never rewriting the saved argv
          string itself.
        - dirsearch's own `redact_command()` (vendored,
          `lib/utils/command.py`, confirmed byte-for-byte unmodified) wipes
          `-H`/`--header`/`--cookie`/`--auth`/`--proxy` entirely -- not
          just the value, the flag *and* value both collapse into one bare
          `<redacted>` token. Appropriate for a report meant to be shared
          publicly; wrong for this app, whose saved reports are local-only
          and where the pentester explicitly needs to see exactly what
          ran, headers and all (confirmed directly: `redact_command()`
          against a representative real argv left extensions/method/
          timeout/proxy/rate/delay/status-filters all intact, but reduced
          every `-H ...`/`--cookie ...` pair to `<redacted>`).

        Rebuilt from this engine's own `build_command()` -- the exact same
        function that built every real chunk invocation -- against this
        job's own `config` (unredacted; it's the source of truth
        `build_command()` itself already used) and the real final
        extension in place of the `{extension}` template. `-w` uses
        `job.wordlist_sources` -- the pentester's own originally-selected
        file(s) (joined with a comma, dirsearch's real multi-wordlist
        syntax), not any internal per-chunk temp file -- explicit request:
        an earlier version used a bare "<wordlist-chunk>" placeholder here
        instead, reasoning the per-chunk path "wasn't meaningful", but that
        meant the saved command no longer showed a real wordlist path at
        all.

        Only ever needs doing once: `FileReportMixin.initiate()` (vendored
        dirsearch) only writes this header when the file doesn't already
        exist -- every later chunk of the same job just appends results,
        never touching it again -- so this is naturally idempotent (a
        header already rebuilt is simply rebuilt again, identically) and
        safe to call after every `_run_job` exit, paused or not.

        `output_base` can be the full, human-readable final name (once
        renamed), which can genuinely exceed MAX_PATH -- every read/write
        here goes through `layout`'s long-path-safe helpers rather than a
        plain Path method for that reason (see `layout.long_path`'s own
        docstring).
        """
        try:
            config = config_from_dict(job.config)
            wordlist_display = ",".join(job.wordlist_sources) if job.wordlist_sources else "<wordlist>"
            argv = self._engine.build_command(
                config, wordlist_file=Path(wordlist_display), output_base=output_base
            )
            command_line = " ".join(str(a) for a in argv)
        except Exception:
            return  # best-effort -- a stale header is not worth failing the job over

        txt_path = output_base.parent / f"{output_base.name}.txt"
        json_path = output_base.parent / f"{output_base.name}.json"
        try:
            if layout.path_is_file(txt_path):
                text = layout.read_text(txt_path)
                new_first_line = re.sub(
                    r"^(# Dirsearch started at .* as: ).*$",
                    lambda m: m.group(1) + command_line.replace("{extension}", "txt", 1),
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
                if new_first_line != text:
                    layout.write_text(txt_path, new_first_line)
        except OSError:
            pass  # best-effort -- a stale header is not worth failing the job over
        try:
            if layout.path_is_file(json_path):
                data = json.loads(layout.read_text(json_path))
                real_args = command_line.replace("{extension}", "json", 1)
                if data.get("info", {}).get("args") != real_args:
                    data.setdefault("info", {})["args"] = real_args
                    layout.write_text(json_path, json.dumps(data, sort_keys=True, indent=4))
        except (OSError, ValueError, KeyError):
            pass

    def _mirror_output_if_configured(self, job: FuzzJob, output_base: Path) -> None:
        try:
            project = project_service.get_project(job.project_id)
            if not project.outputs_mirror_dir:
                return
            target = target_service.get_target(job.project_id, job.target_id)
        except Exception:
            return  # best-effort -- never fail/affect the job itself over this
        txt_path = output_base.parent / f"{output_base.name}.txt"
        output_mirror_service.mirror_output_file(project, target.name, txt_path)

    def _run_segment(
        self,
        job: FuzzJob,
        wordlist_file: Path,
        output_base: Path,
        config: DirsearchJobConfig,
        batcher: ResultBatcher[ParsedResult],
        lines: list[str],
    ) -> tuple[int, str | None]:
        """Runs ONE continuous dirsearch subprocess against `wordlist_file`
        (the job's full wordlist on a fresh run, or a freshly-sliced
        remainder on a resume -- see `_run_job`) from start to natural
        completion, a stop, or a pause. `lines` is the FULL original
        wordlist (not just this segment's slice) -- needed by
        `_checkpoint_pause` below to map a cut-short run back onto the
        job's own line numbering.
        """
        # Fixed for this whole segment -- both the periodic mid-run
        # checkpoint and the final pause-time one recompute the checkpoint
        # from THIS same starting point plus the current live progress
        # index each time (never by adding to an already-updated
        # completed_lines), so repeated calls stay consistent instead of
        # drifting/double-counting.
        base_lines, base_requests = job.completed_lines, job.completed_requests
        if base_lines:
            # A resumed segment is deduplicated on its own by dirsearch, so paths already requested
            # before the pause are requested again: the true total is what is done + this remainder.
            # Fixing it here keeps the total identical in the live bar and in every REST refresh.
            total = base_requests + estimate_requests(
                lines[base_lines:], list(config.extensions), config.force_extensions,
            )
            if total != job.total_wordlist_requests:
                job.total_wordlist_requests = total
                self._save(job)

        # dirsearch's own log: the only place the reason of a failed request is ever written
        # (fresh for every segment, so a stop is explained by THIS run's errors).
        log_file = layout.job_wordlist_dir(job.project_id, job.target_id, job.id) / "dirsearch.log"
        try:
            log_file.unlink(missing_ok=True)
        except OSError:
            pass
        argv = self._engine.build_command(
            config, wordlist_file=wordlist_file, output_base=output_base, log_file=log_file
        )

        # COLUMNS/LINES: the one thing dirsearch's own progress bar actually
        # needs to not silently no-op on a plain pipe -- see this module's
        # own comment above PROGRESS_ENV_COLUMNS for the full story (it was
        # never about needing a real console). Copies the real environment
        # rather than replacing it outright -- dirsearch/its own dependencies
        # may read other inherited vars (PATH, etc.), and there's no reason
        # to narrow that just to inject these two.
        env = os.environ.copy()
        env["COLUMNS"] = PROGRESS_ENV_COLUMNS
        env["LINES"] = PROGRESS_ENV_LINES

        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            env=env,
            # Without this, Windows auto-allocates a brand new (visible,
            # since stdout/stderr are redirected elsewhere, it just sits
            # there blank) console window for python.exe -- a
            # console-subsystem executable spawned from a console-less
            # parent (pythonw.exe) always gets one unless explicitly told
            # not to, regardless of stdout/stderr redirection. Found the
            # hard way: reported as "an empty terminal steals focus every so
            # often" even *after* dropping ConPTY specifically to eliminate
            # that -- ConPTY wasn't the only source, this plain subprocess
            # path had the exact same underlying Windows behavior the whole
            # time, just never exercised for a real dirsearch run until
            # uses_pty was set to False (see DirsearchEngine's own comment).
            **platform_compat.no_window_kwargs(),
        )
        with self._lock:
            self._processes[job.id] = proc
            job.pid = proc.pid
            # A stop/pause requested in the narrow window between this job's
            # status becoming RUNNING and the process actually existing
            # would otherwise go unnoticed until the segment finishes on its
            # own -- stop_job()/the auto-pause check can only terminate a
            # process they can find in self._processes. Catch that here,
            # right as it becomes visible, instead of leaving it unnoticed
            # until the whole segment naturally ends.
            terminate_immediately = job.stop_requested or job.pause_requested
        self._save(job)
        if terminate_immediately:
            self._terminate_with_escalation(proc)

        stderr_lines: list[str] = []
        progress_state: dict[str, Any] = {"last": None, "tail": ""}
        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(job, proc, batcher, progress_state, lines, config, base_lines, base_requests),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr, args=(proc, stderr_lines), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        exit_code = proc.wait()
        stdout_thread.join()
        stderr_thread.join()

        with self._lock:
            self._processes.pop(job.id, None)
            job.pid = None

        # The progress bar only redraws when a response arrives, so request errors that kill or
        # stall the run never reach job.errors through it: the log has them whatever the end.
        failures = summarize_request_errors(log_file)
        if failures:
            job.errors = max(job.errors, failures.count)

        if job.pause_requested and not job.stop_requested:
            self._checkpoint_pause(job, lines, config, progress_state["last"], base_lines, base_requests)
        elif exit_code != 0 and not job.stop_requested:
            # A crash or an error exit: keep the real position so a FAILED job can be resumed
            # from where it got to instead of from the last periodic checkpoint.
            self._checkpoint_pause(job, lines, config, progress_state["last"], base_lines, base_requests)
        elif exit_code == 0 and not job.stop_requested:
            reason = self._early_end_reason(config, progress_state["last"], progress_state["tail"], failures)
            if reason:
                # Keep the true position (not 100%) so Resume continues from there.
                self._checkpoint_pause(job, lines, config, progress_state["last"], base_lines, base_requests)
                done = job.completed_requests
                total = job.total_wordlist_requests
                percent = f" ({round(done / total * 100)}%)" if total else ""
                self._early_ends[job.id] = (
                    f"dirsearch stopped early at about {done} of {total} requests{percent}: {reason}. "
                    "The rest of the wordlist was NOT requested. Resume continues from where it stopped."
                )

        error_reason = None
        if exit_code != 0:
            error_reason = "\n".join(stderr_lines[-STDERR_TAIL_LINES:]) or None
        return exit_code, error_reason

    @staticmethod
    def _error_before_task_completed(stdout_tail: str) -> str | None:
        """The error line dirsearch printed right before "Task Completed", if what precedes
        it is not an ordinary result or progress line (see _RESULT_LINE_RE)."""
        head, found, _ = stdout_tail.rpartition("Task Completed")
        if not found:
            return None
        for line in reversed(re.split(r"[\r\n]+", head)):
            line = line.strip()
            if not line:
                continue
            if _RESULT_LINE_RE.match(line) or _PROGRESS_TEXT_RE.search(line):
                return None
            return line[:300]
        return None

    @staticmethod
    def _early_end_reason(
        config: DirsearchJobConfig,
        last_progress: ParsedProgress | None,
        stdout_tail: str,
        failures: RequestErrorSummary | None = None,
    ) -> str | None:
        """Why a dirsearch that exited 0 did not reach the end of its wordlist, or None.

        Two independent signals: the message dirsearch prints when it gives up on a
        target, and its own last progress being clearly short of its own total.
        """
        parts = []
        found = EARLY_END_MESSAGE_RE.findall(stdout_tail)
        if found:
            parts.append(found[-1].strip())
        elif (aborted_by := JobManager._error_before_task_completed(stdout_tail)):
            parts.append(aborted_by)
        if last_progress is not None and last_progress.length > 0:
            tolerance = max(config.threads * 2, int(last_progress.length * EARLY_END_TOLERANCE_FRACTION))
            if last_progress.index < last_progress.length - tolerance and not parts:
                parts.append(f"it ended at {last_progress.index} of {last_progress.length} without saying why")
        if not parts:
            return None
        if failures:
            parts.append(failures.text)
        elif last_progress is not None and last_progress.errors:
            parts.append(f"{last_progress.errors} request errors")
        return "; ".join(parts)

    def _emit_parsed_results(
        self,
        job: FuzzJob,
        results: list[ParsedResult],
        batcher: ResultBatcher[ParsedResult],
        pause_statuses: set[int],
        proc,
    ) -> None:
        """Shared by _read_stdout/_run_chunk_pty's own read loop AND their
        end-of-stream `parser.flush()` call -- exactly the same handling
        either way, a held-back trailing result released at real EOF is
        just as real as one seen mid-stream."""
        for parsed in results:
            self._event_bus.publish(
                "job.output",
                {"job_id": job.id, "line": f"[{parsed.time}] {parsed.status} - {parsed.size} - {parsed.url}"},
            )
            batcher.add(parsed)
            self._maybe_trigger_auto_pause(job, parsed.status, pause_statuses, proc)

    def _read_stdout(
        self,
        job: FuzzJob,
        proc: subprocess.Popen,
        batcher: ResultBatcher[ParsedResult],
        progress_state: dict[str, Any],
        lines: list[str],
        config: DirsearchJobConfig,
        base_lines: int,
        base_requests: int,
    ) -> None:
        """Not line-based (`for line in proc.stdout`) anymore, since 2026-09-24:
        dirsearch's own live progress-bar redraws share the exact same
        no-reliable-newline-boundary shape on this plain pipe that ConPTY's
        raw stream always had (redraws glued directly onto whatever text
        follows, no `\\n` between them -- see PROGRESS_ENV_COLUMNS's own
        comment for why dirsearch emits that bar here at all now). Reusing
        `LiveOutputParser` (built for that exact shape, just previously only
        ever fed from a ConPTY read loop) instead of a second, parallel
        implementation -- its regexes search raw accumulated text either way
        and don't know or care whether the bytes came from a pseudo-console
        or an ordinary pipe.

        Only `job.output`-publishes a reconstructed line per actual result
        (not the raw banner/progress-bar text now mixed into this stream) --
        same choice `_run_chunk_pty` already made for the same reason: the
        live "output" panel showing verbose noise instead of clean result
        lines would be a real regression, not the point of any of this.

        `progress_state["last"]` is a mutable box (not a return value -- this
        runs as a background thread's target) so `_run_segment` can read the
        very last progress update seen, after this thread joins, and use it
        to compute a safe pause-resume checkpoint (`_checkpoint_pause`) --
        dirsearch's own live index is the only signal that exists for that,
        there being no separate "requests actually completed" counter.

        Also periodically PERSISTS that same checkpoint to disk during a
        live run (`PERIODIC_CHECKPOINT_INTERVAL_SECONDS`), independent of any
        Pause -- bounds how much a genuine crash can cost (see that
        constant's own comment).
        """
        assert proc.stdout is not None
        pause_statuses = set(job.pause_on_status)
        parser = LiveOutputParser()
        last_progress_published_at = 0.0
        last_checkpoint_saved_at = time.monotonic()

        while True:
            data = proc.stdout.read(STDOUT_READ_CHUNK_SIZE)
            if not data:
                break

            progress_state["tail"] = (progress_state["tail"] + data)[-STDOUT_TAIL_CHARS:]
            results, progress = parser.feed(data)
            self._emit_parsed_results(job, results, batcher, pause_statuses, proc)

            if progress is not None:
                progress_state["last"] = progress
                now = time.monotonic()
                if now - last_progress_published_at >= PROGRESS_PUBLISH_MIN_INTERVAL_SECONDS:
                    self._publish_progress(job, progress, base_requests)
                    last_progress_published_at = now
                if now - last_checkpoint_saved_at >= PERIODIC_CHECKPOINT_INTERVAL_SECONDS:
                    self._save_periodic_checkpoint(job, lines, config, progress, base_lines, base_requests)
                    last_checkpoint_saved_at = now

        # Real EOF -- release any trailing result LiveOutputParser withheld
        # because a chunk boundary happened to land inside its URL (see
        # LiveOutputParser._extract_new_results's own docstring). Nothing
        # else is ever coming now, so it's safe to trust it's complete.
        self._emit_parsed_results(job, parser.flush(), batcher, pause_statuses, proc)
        if progress_state["last"] is not None:
            self._publish_progress(job, progress_state["last"], base_requests)  # make sure the final state always lands
        proc.stdout.close()

    def _compute_checkpoint(
        self,
        lines: list[str],
        config: DirsearchJobConfig,
        progress: ParsedProgress | None,
        base_lines: int,
        base_requests: int,
    ) -> tuple[int, int]:
        """The safe (completed_lines, completed_requests) for this run
        segment's progress so far, relative to a FIXED `base_lines`/
        `base_requests` (this segment's own starting point, captured once in
        `_run_segment` -- never an already-updated `job.completed_lines`, so
        repeated calls during a live run stay consistent instead of
        double-counting or drifting).

        `progress.index` counts requests DISPATCHED to a worker thread, not
        COMPLETED -- confirmed directly against dirsearch's own source
        (`Dictionary.__next__`, `lib/core/dictionary.py`: `_index` increments
        on handout, never on response). Up to `config.threads` requests could
        be dispatched but never actually finished the instant this is read --
        subtracting `threads` before mapping the index back to a wordlist
        line is what keeps this a genuine "definitely done" boundary rather
        than an optimistic guess that could silently skip an untested path.
        """
        if progress is None or progress.index <= 0:
            return base_lines, base_requests
        safe_index = max(progress.index - config.threads, 0)
        if safe_index <= 0:
            return base_lines, base_requests

        remainder = lines[base_lines:]
        segment_lines_done, segment_requests_done = find_resume_point(
            remainder, safe_index, list(config.extensions), config.force_extensions,
        )
        return base_lines + segment_lines_done, base_requests + segment_requests_done

    def _save_periodic_checkpoint(
        self,
        job: FuzzJob,
        lines: list[str],
        config: DirsearchJobConfig,
        progress: ParsedProgress | None,
        base_lines: int,
        base_requests: int,
    ) -> None:
        new_lines, new_requests = self._compute_checkpoint(lines, config, progress, base_lines, base_requests)
        if new_lines == job.completed_lines and new_requests == job.completed_requests:
            return  # nothing new to persist -- skip the disk write
        with self._lock:
            job.completed_lines = new_lines
            job.completed_requests = new_requests
        self._save(job)

    def _read_stderr(self, proc: subprocess.Popen, collected: list[str]) -> None:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            collected.append(raw_line.rstrip("\r\n"))
        proc.stderr.close()

    def _run_chunk_pty(
        self,
        job: FuzzJob,
        wordlist_file: Path,
        output_base: Path,
        config: DirsearchJobConfig,
        batcher: ResultBatcher[ParsedResult],
        lines: list[str],
    ) -> tuple[int, str | None]:
        """Same contract as `_run_segment`, but attaches the child to a
        Windows pseudo-console instead of a plain pipe. Dead code in
        practice today (no engine sets `uses_pty = True`, see
        `DirsearchEngine`'s own comment for why it never will again) --
        kept only so a hypothetical future engine wiring this up doesn't
        also have to reinvent the checkpoint logic `_run_segment` uses.
        """
        from backend.job_manager.pty_runner import PtyChunkProcess  # lazy: Windows-only dep

        base_lines, base_requests = job.completed_lines, job.completed_requests

        argv = self._engine.build_command(config, wordlist_file=wordlist_file, output_base=output_base)
        proc = PtyChunkProcess(argv)

        with self._lock:
            self._pty_processes[job.id] = proc
            job.pid = proc.pid
            terminate_immediately = job.stop_requested or job.pause_requested
        self._save(job)
        if terminate_immediately:
            proc.terminate(force=True)

        parser = LiveOutputParser()
        pause_statuses = set(job.pause_on_status)
        last_progress: ParsedProgress | None = None
        last_progress_published_at = 0.0
        # A PTY merges stdout+stderr into one stream (exactly like a real
        # terminal does) -- there is no separate stderr channel to inspect
        # for a FAILED job's reason here, so keep a bounded raw tail instead.
        raw_tail = ""

        while True:
            data = proc.read(4096)
            if data is None:
                break
            raw_tail = (raw_tail + data)[-RAW_TAIL_CHARS_FOR_ERRORS:]

            results, progress = parser.feed(data)
            self._emit_parsed_results(job, results, batcher, pause_statuses, proc)

            if progress is not None:
                last_progress = progress
                now = time.monotonic()
                if now - last_progress_published_at >= PROGRESS_PUBLISH_MIN_INTERVAL_SECONDS:
                    self._publish_progress(job, progress, base_requests)
                    last_progress_published_at = now

        # See _read_stdout's own comment -- same trailing-result release at EOF.
        self._emit_parsed_results(job, parser.flush(), batcher, pause_statuses, proc)
        if last_progress is not None:
            self._publish_progress(job, last_progress, base_requests)  # make sure the final state always lands

        exit_code = proc.wait()
        with self._lock:
            self._pty_processes.pop(job.id, None)
            job.pid = None

        if job.pause_requested and not job.stop_requested:
            self._checkpoint_pause(job, lines, config, last_progress, base_lines, base_requests)

        error_reason = None
        if exit_code != 0:
            from backend.job_manager.pty_output import CONTROL_SEQUENCE_RE

            error_reason = CONTROL_SEQUENCE_RE.sub("", raw_tail).strip()[-2000:] or None
        return exit_code, error_reason

    def _maybe_trigger_auto_pause(self, job: FuzzJob, status: int, pause_statuses: set[int], proc) -> None:
        if not (pause_statuses and status in pause_statuses):
            return
        if job.pause_requested or job.stop_requested:
            return
        # e.g. the target started answering 429 (rate limited / down): cut
        # the current chunk short now rather than burning through the rest
        # of it against a dead target. A human resumes manually once the
        # target is back.
        with self._lock:
            job.pause_requested = True
            job.auto_paused = True
            job.auto_pause_status = status
        if isinstance(proc, subprocess.Popen):
            self._terminate_with_escalation(proc)
        else:
            proc.terminate(force=True)

    def _checkpoint_pause(
        self,
        job: FuzzJob,
        lines: list[str],
        config: DirsearchJobConfig,
        last_progress: ParsedProgress | None,
        base_lines: int,
        base_requests: int,
    ) -> None:
        """Folds however much of THIS run segment is safely done into
        job.completed_lines/completed_requests, via the same
        `_compute_checkpoint` a periodic mid-run save already uses (see its
        own comment for the "dispatched, not completed" subtlety) -- this is
        just the final call, once the segment has actually stopped, using
        whatever progress `_read_stdout` last saw. See this module's own
        comment above DEFAULT_MAX_CONCURRENT_JOBS for why this exists at all
        instead of dirsearch's own session save/resume (a confirmed,
        Windows-specific dead end).
        """
        job.completed_lines, job.completed_requests = self._compute_checkpoint(
            lines, config, last_progress, base_lines, base_requests,
        )

    def _publish_progress(self, job: FuzzJob, progress: ParsedProgress, base_requests: int) -> None:
        # total: dirsearch's OWN live-reported total for this run segment's
        # wordlist (progress.length -- parsed straight off its own progress
        # bar, e.g. the "823" in "[412/823]"), not our own upfront estimate
        # (job.total_wordlist_requests, wordlist_resolver.resolve_wordlist).
        # Real, reported bug this replaces (2026-09-25): that estimate
        # deliberately doesn't replicate every one of dirsearch's own
        # dictionary-expansion rules (prefixes/suffixes/overwrite-extensions,
        # only reachable via a raw --extra-args flag -- see
        # wordlist_resolver._estimate_requests's own docstring) -- for a
        # wordlist that actually uses one of those, the estimate undercounts,
        # `min(done, total)` below then clamps the displayed progress at
        # "100% (N/N)" the moment dirsearch's real live count passes that
        # too-low N, even though dirsearch keeps genuinely working (still
        # finding new results, rate still changing) for a while after --
        # confirmed directly: the user reported exactly that live symptom,
        # explicitly contrasted with plain dirsearch from a terminal (which
        # only ever displays its own real numbers, nothing external to
        # second-guess them). dirsearch's own number is definitionally
        # correct for whatever it's actually doing -- using it instead
        # removes the whole class of estimate-accuracy bugs at once, not
        # just this one reported case. `base_requests` -- prior segments'
        # already-folded-in share -- is passed in explicitly (this
        # segment's own fixed starting point, captured once in
        # `_run_segment`/`_run_chunk_pty`) rather than read live off
        # `job.completed_requests`: real, reported bug this ALSO fixes,
        # found tracing the one above -- `_save_periodic_checkpoint` (added
        # 2026-09-24) mutates that same field mid-segment, so reading it
        # live here double-counted this segment's own progress on top of
        # itself once at least one periodic checkpoint had landed, inflating
        # `overall_done` past the real total even faster than the estimate
        # mismatch alone would have. Falls back to the old estimate only if
        # dirsearch hasn't printed a usable length yet (shouldn't normally
        # happen once any progress line has been parsed at all).
        total = base_requests + progress.length if progress.length > 0 else (
            job.total_wordlist_requests or job.total_wordlist_lines or 1
        )
        overall_done = min(base_requests + progress.index, total)
        # In-memory only here (no dedicated disk write per tick) -- rides
        # along on whatever the next natural self._save(job) already is
        # (periodic checkpoint, pause, completion). JOBS' own live Rate/
        # Errors columns (2026-09-24) need this to survive a REST re-fetch
        # after the job goes terminal, not just the transient SSE payload.
        job.errors = progress.errors
        # This only ever fires while the segment's subprocess is still
        # running -- the job is never actually COMPLETED yet at this point
        # (there's still the process exit, report flush, and job.completed
        # event to come), so never claim 100% here: it visibly lied ("100%"
        # while the status was still RUNNING for however long that trailing
        # work took). Real 100% only ever comes from the terminal
        # job.completed event once the job truly is done.
        overall_percent = min(round(overall_done / total * 100), 99)
        self._event_bus.publish(
            "job.progress",
            {
                "job_id": job.id,
                "overall_done": overall_done,
                "overall_total": total,
                "overall_percent": overall_percent,
                "rate": progress.rate,
                "errors": progress.errors,
            },
        )

    def _flush_results(self, job_id: str, items: list[ParsedResult]) -> None:
        results = [_parsed_to_result(job_id, item) for item in items]
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # dirsearch can report the exact same finding more than once (a
            # wordlist with repeated words, say) and Results shows each finding
            # once, so count them once too: "5 results" on Jobs with 4 rows in
            # Results was a real, reported mismatch.
            counted = self._counted_findings.setdefault(job_id, set())
            for result in results:
                key = _finding_key(result)
                if key not in counted:
                    counted.add(key)
                    job.result_count += 1
        self._save(job)
        with self._lock:
            live = self._live_results.get(job_id)
            if live is not None:
                live.extend(results)
        # Keeps an already-tracked TREE node's status/size honest once a
        # later fuzz actually confirms it -- see record_fuzz_discoveries's
        # own docstring for the full story. Best-effort: a path this batch
        # never touches (the overwhelming majority of a real job's results)
        # costs nothing beyond the one shared node lookup below.
        tree_service.record_fuzz_discoveries(
            job.project_id,
            job.target_id,
            job_id,
            [(r.path, r.status, r.size) for r in results],
        )
        self._event_bus.publish(
            "job.result",
            {
                "job_id": job_id,
                "results": [r.to_dict() for r in results],
            },
        )

    # -- persistence / events ------------------------------------------------

    def _save(self, job: FuzzJob) -> None:
        write_json_atomic(layout.job_file(job.project_id, job.target_id, job.id), job.to_dict())

    def _set_status_and_save(self, job: FuzzJob, status: JobStatus) -> None:
        # Persist BEFORE the new status becomes visible in memory. Otherwise a
        # reader of job.json in that window (a fresh JobManager, a restart) sees
        # the previous state -- e.g. RUNNING, which reloads as PAUSED -- even
        # though this instance already reports the job as finished.
        ended_at = now_iso()
        data = job.to_dict()
        data["status"] = status.value
        data["ended_at"] = ended_at
        write_json_atomic(layout.job_file(job.project_id, job.target_id, job.id), data)
        job.status = status
        job.ended_at = ended_at

    def _publish(self, name: str, job: FuzzJob) -> None:
        self._event_bus.publish(name, {"job": job.to_dict()})


def _finding_key(result: Result) -> tuple:
    # No size: a live line reports it humanised ("21KB") and the report on disk in
    # bytes ("21997B"), so a finding read back after a resume would never match.
    return (result.url, result.status, result.redirect or "")


def _parsed_to_result(job_id: str, parsed: ParsedResult) -> Result:
    return Result(
        job_id=job_id,
        time=parsed.time,
        status=parsed.status,
        size=parsed.size,
        url=parsed.url,
        path=urlparse(parsed.url).path or "/",
        redirect=parsed.redirect,
    )
