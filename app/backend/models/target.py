from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Target:
    id: str
    project_id: str
    name: str
    base_url: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "base_url": self.base_url,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Target":
        return cls(
            id=data["id"],
            project_id=data["project_id"],
            name=data["name"],
            base_url=data["base_url"],
            created_at=data["created_at"],
        )
