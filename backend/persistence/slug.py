from __future__ import annotations

import re
import uuid

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_SAFE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def slugify(name: str) -> str:
    slug = _NON_SLUG_CHARS.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("name must contain at least one alphanumeric character")
    return slug


def make_id(name: str) -> str:
    """A short, readable, filesystem-safe, unique id derived from a
    user-supplied name -- e.g. "Client A" -> "client-a-3f9c2a1b".

    The random suffix means renaming a Project/Target never has to move its
    directory or rewrite anything that references its id ("renombrar
    proyecto"); two entities with the same name never collide.
    """
    return f"{slugify(name)}-{uuid.uuid4().hex[:8]}"


def validate_safe_id(value: str, *, label: str = "id") -> str:
    """Defense in depth for any id that reaches path construction from a
    request (e.g. a URL path parameter), even though make_id()'s own output
    is already restricted to [a-z0-9-] and can't contain traversal sequences.
    """
    if not _SAFE_ID_RE.match(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value
