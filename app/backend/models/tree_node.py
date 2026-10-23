from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.models.discovery import Discovery


@dataclass
class TreeNode:
    """A path the pentester has chosen to keep in the FUZZ TREE.
    Distinct from Results, which is
    whatever dirsearch happened to find. Never created
    automatically from a Result; only via an explicit "Add path"/"Add to
    tree" action -- except the root ("/"), which
    `tree_service.list_nodes` auto-creates so it's always present and
    undeletable, per explicit user request.

    `note` and `interesting` are properties of the node itself, independent
    of how many times it's been fuzzed or discovered.
    `discoveries` is the append-only history of every "add" action, which
    doubles as the status history -- the most recent entry with a
    non-null status is the status shown in the compact row.
    """

    id: str
    project_id: str
    target_id: str
    path: str
    created_at: str
    interesting: bool = False
    note: str = ""
    discoveries: list[Discovery] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "target_id": self.target_id,
            "path": self.path,
            "created_at": self.created_at,
            "interesting": self.interesting,
            "note": self.note,
            "discoveries": [d.to_dict() for d in self.discoveries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TreeNode":
        return cls(
            id=data["id"],
            project_id=data["project_id"],
            target_id=data["target_id"],
            path=data["path"],
            created_at=data["created_at"],
            interesting=data.get("interesting", False),
            note=data.get("note", ""),
            discoveries=[Discovery.from_dict(d) for d in data.get("discoveries", [])],
        )
