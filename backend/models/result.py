from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Result:
    """A single fuzz result row (docs/ARCHITECTURE.md §4).

    Derived from a job's raw output / live stream, never stored one-file-per-
    result -- this is just the shape the API and frontend work with.
    """

    job_id: str
    time: str
    status: int
    size: str
    url: str
    path: str
    redirect: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "time": self.time,
            "status": self.status,
            "size": self.size,
            "url": self.url,
            "path": self.path,
            "redirect": self.redirect,
        }
