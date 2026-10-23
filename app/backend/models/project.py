from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Project:
    id: str
    name: str
    created_at: str
    notes: str = ""
    # Absolute filesystem path where a COPY of every target's own .txt
    # outputs also gets saved, mirrored under a per-target subfolder --
    # "" means no mirroring configured (the normal case). The primary
    # outputs/ storage under the workspace is never affected either way;
    # this is purely an additional, optional copy for the pentester's own
    # convenience (services/output_mirror_service.py).
    outputs_mirror_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "notes": self.notes,
            "outputs_mirror_dir": self.outputs_mirror_dir,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            id=data["id"],
            name=data["name"],
            created_at=data["created_at"],
            notes=data.get("notes", ""),
            outputs_mirror_dir=data.get("outputs_mirror_dir", ""),
        )
