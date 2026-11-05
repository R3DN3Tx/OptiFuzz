from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Folder:
    """A folder that groups projects (and other folders) in the Projects view. Only an organising layer:
    a project's own directory on disk never moves, the project just remembers which folder it is in."""

    id: str
    name: str
    created_at: str
    parent_id: str = ""  # "" = the top level (Projects itself)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "created_at": self.created_at, "parent_id": self.parent_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Folder":
        return cls(
            id=data["id"],
            name=data["name"],
            created_at=data["created_at"],
            parent_id=data.get("parent_id", ""),
        )
