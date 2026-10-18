from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.STOPPED})


@dataclass
class FuzzJob:
    """A Fuzz Job's metadata.

    `config` is the DirsearchJobConfig snapshot as a plain dict (url,
    extensions, threads, headers, ...) -- kept as a dict rather than the
    dataclass itself so this model has no dependency on the engine layer and
    serializes trivially.

    A job runs as ONE continuous dirsearch process against its full resolved
    wordlist (2026-09-24 -- no more fixed-size chunk subprocesses, see
    `JobManager._run_job`/`_run_segment`). `completed_lines`/
    `completed_requests` are the pause checkpoint that makes a graceful
    pause durable across an app restart: how much of the original
    wordlist is safely known to be done, computed at pause time from
    dirsearch's own live progress index minus a thread-count safety margin
    (see `JobManager._checkpoint_pause`) -- NOT updated during a live run,
    so an ungraceful crash (as opposed to a deliberate pause) loses whatever
    the current run segment had done since its own start, not just one
    chunk's worth; a deliberate Pause/auto-pause is unaffected.
    """

    id: str
    target_id: str
    project_id: str
    path: str
    config: dict[str, Any]
    status: JobStatus
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    total_wordlist_lines: int = 0
    # Estimated real HTTP-request count, extension-aware (raw lines expanded
    # by -e/-f, same rules dirsearch's own dictionary generation uses -- see
    # wordlist_resolver._estimate_requests), deduplicated across the WHOLE
    # wordlist -- lets progress reporting be extension-aware without
    # depending on dirsearch's own console-dependent progress bar alone.
    total_wordlist_requests: int = 0
    # How much of the ORIGINAL wordlist is safe to skip on the next run
    # segment (see this class's own docstring, and
    # JobManager._checkpoint_pause for how these are computed).
    completed_lines: int = 0
    completed_requests: int = 0
    # dirsearch's own live error counter (its progress bar's "errors:N"),
    # mirrored here so it survives a REST re-fetch/page reload -- only ever
    # updated in-memory on every JobManager._publish_progress call (2026-09-24,
    # JOBS' own live Rate/Errors columns), persisted to disk at whatever the
    # next natural save point is (no dedicated extra disk write per tick).
    errors: int = 0
    pid: int | None = None
    stop_requested: bool = False
    pause_requested: bool = False
    # Distinct from pause_requested (set by BOTH a manual Pause and an
    # auto-pause-on-status-code -- same underlying "cut this run short"
    # mechanism either way): this is ONLY true when the auto-pause feature
    # itself was the trigger, so the frontend's "⚠ auto" tag can tell the
    # two apart. Real, reported bug this fixes: pause_requested alone made
    # every manual pause show the auto-pause tag too.
    auto_paused: bool = False
    error_reason: str | None = None
    result_count: int = 0
    wordlist_sources: list[str] = field(default_factory=list)
    pause_on_status: list[int] = field(default_factory=list)
    output_stem: str = ""  # "" => pre-existing job, use layout.job_output_base_legacy
    output_flat: bool = False  # True => outputs/<stem>_<id>.{ext}, no per-job subfolder

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_id": self.target_id,
            "project_id": self.project_id,
            "path": self.path,
            "config": self.config,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total_wordlist_lines": self.total_wordlist_lines,
            "total_wordlist_requests": self.total_wordlist_requests,
            "completed_lines": self.completed_lines,
            "completed_requests": self.completed_requests,
            "errors": self.errors,
            "pid": self.pid,
            "stop_requested": self.stop_requested,
            "pause_requested": self.pause_requested,
            "auto_paused": self.auto_paused,
            "error_reason": self.error_reason,
            "result_count": self.result_count,
            "wordlist_sources": self.wordlist_sources,
            "pause_on_status": self.pause_on_status,
            "output_stem": self.output_stem,
            "output_flat": self.output_flat,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FuzzJob":
        return cls(
            id=data["id"],
            target_id=data["target_id"],
            project_id=data["project_id"],
            path=data["path"],
            config=data["config"],
            status=JobStatus(data["status"]),
            created_at=data["created_at"],
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            total_wordlist_lines=data.get("total_wordlist_lines", 0),
            total_wordlist_requests=data.get("total_wordlist_requests", 0),
            completed_lines=data.get("completed_lines", 0),
            completed_requests=data.get("completed_requests", 0),
            errors=data.get("errors", 0),
            pid=data.get("pid"),
            stop_requested=data.get("stop_requested", False),
            pause_requested=data.get("pause_requested", False),
            auto_paused=data.get("auto_paused", False),
            error_reason=data.get("error_reason"),
            result_count=data.get("result_count", 0),
            wordlist_sources=data.get("wordlist_sources", []),
            pause_on_status=data.get("pause_on_status", []),
            output_stem=data.get("output_stem", ""),
            output_flat=data.get("output_flat", False),
        )
