from __future__ import annotations

import hashlib
import re
import uuid

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_SAFE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# A project or target name may be written in any language, but it is kept short: the id built from it
# becomes a folder name, and Windows paths are limited to 260 characters in total.
MAX_NAME_LENGTH = 80
# Only this much of the readable part of a name goes into its id (the random suffix keeps ids unique).
MAX_SLUG_LENGTH = 40


def clean_name(name: str, label: str) -> str:
    """The name as it is stored: trimmed, and checked. Any language is fine (the stored id never depends
    on it being Latin); only empty names, control characters and over-long names are refused."""
    name = name.strip()
    if not name:
        raise ValueError(f"{label} name is required")
    if _CONTROL_CHARS.search(name):
        raise ValueError(f"{label} name cannot contain control characters")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"{label} name must be {MAX_NAME_LENGTH} characters or fewer (it has {len(name)})")
    return name


def slugify(name: str) -> str:
    """Lowercase ASCII letters and digits joined by "-". A name with none (Hebrew, Chinese, only symbols...)
    gets a stable neutral slug built from a hash of the name, so it is never refused."""
    slug = _NON_SLUG_CHARS.sub("-", name.strip().lower()).strip("-")
    if not slug:
        return "x-" + hashlib.sha1(name.strip().encode("utf-8", errors="ignore")).hexdigest()[:8]
    return slug


def make_id(name: str) -> str:
    """A short, readable, filesystem-safe, unique id derived from a
    user-supplied name -- e.g. "Client A" -> "client-a-3f9c2a1b".

    The random suffix means renaming a Project/Target never has to move its
    directory or rewrite anything that references its id ("renombrar
    proyecto"); two entities with the same name never collide. The readable part is cut to
    MAX_SLUG_LENGTH so a long name cannot push a folder path past what Windows allows.
    """
    slug = slugify(name)[:MAX_SLUG_LENGTH].strip("-") or "x"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def validate_safe_id(value: str, *, label: str = "id") -> str:
    """Defense in depth for any id that reaches path construction from a
    request (e.g. a URL path parameter), even though make_id()'s own output
    is already restricted to [a-z0-9-] and can't contain traversal sequences.
    """
    if not _SAFE_ID_RE.match(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value
