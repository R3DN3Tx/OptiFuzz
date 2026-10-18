from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Discovery:
    """One record of how/when a Tree Node's path became known, and -- when it
    came from a fuzz result -- what status it had at that moment.
    A node accumulates one of these per
    "Add to tree"/"Add path" action rather than being overwritten, so the
    full status history survives even if a later fuzz sees a different
    status for the same path.
    """

    source: str  # "manual" | "result" | "fuzz" (fuzz: auto-synced onto an
    # already-tracked node once a job discovers a real hit there -- see
    # tree_service.record_fuzz_discoveries)
    at: str
    job_id: str | None = None
    status: int | None = None
    size: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "at": self.at,
            "job_id": self.job_id,
            "status": self.status,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Discovery":
        return cls(
            source=data["source"],
            at=data["at"],
            job_id=data.get("job_id"),
            status=data.get("status"),
            size=data.get("size"),
        )
