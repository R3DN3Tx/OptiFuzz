from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

# Builds the human-readable base filename dirsearch's plain/json reports are
# saved under (e.g. "dirsearch_api_example_com_8910_admin_subfolder"), so a
# user browsing the outputs/ folder directly can tell jobs apart without
# opening the app. Configurable via app_settings (settings gear -> Output
# filenames); this module only knows how to fill in one template.

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Real, reported bug: fuzzing a long discovered path (deeply nested routes,
# a long token as a path segment, ...) made the job fail outright on
# Windows -- the fuzzed *path* went straight into this output filename with
# no length cap, and once the full on-disk path crossed Windows' classic
# MAX_PATH (260 characters), dirsearch's own report-file open() failed and
# took the whole job down with it.
#
# The actual MAX_PATH fix now lives in job_manager/manager.py: dirsearch
# itself always writes to a short, job_id-based working name (guaranteed
# safe regardless of what this module builds), renamed to this module's
# full, human-readable name only once the job is done and nothing will ever
# write to it again -- using the \\?\ extended-length path prefix
# (persistence/layout.py::long_path), which bypasses MAX_PATH entirely for
# that rename, no registry opt-in needed. So the stem this module builds no
# longer has to stay under 260 -- explicit request: the pentester wants to
# actually SEE the whole fuzzed path in the filename, not a truncated one.
#
# It still can't be truly unbounded, though: NTFS itself caps a single path
# COMPONENT (a plain filename, independent of the *total* path length, and
# NOT lifted by \\?\ or the long-paths registry setting -- this is a real
# filesystem-level ceiling) at 255 UTF-16 characters. This module's own
# fixed suffix ("_<job_id>.json", job_id always 12 hex chars) eats 18 of
# those, so the cap below leaves a comfortable margin under the remaining
# ~237 -- generous enough that this only actually truncates a genuinely
# extreme path, not the realistic long ones (deep REST routes, long
# tokens/UUIDs as path segments) this was written for.
_MAX_FIELD_LENGTH = 150
_MAX_STEM_LENGTH = 200


def _sanitize(value: str) -> str:
    cleaned = _UNSAFE_RE.sub("_", value.strip()).strip("_")
    return cleaned or "x"


def _truncate_with_hash(value: str, max_length: int) -> str:
    """Keeps `value` under `max_length`, appending a short hash of the
    FULL original value when it has to cut anything -- a bare truncation
    would silently collide two different long paths that happen to share
    the same prefix onto the same output file (one overwriting the
    other's results), which the hash suffix prevents."""
    if len(value) <= max_length:
        return value
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:8]
    keep = max(1, max_length - len(digest) - 1)
    return f"{value[:keep]}-{digest}"


def _port_segment(url: str) -> str:
    parsed = urlparse(url)
    if parsed.port:
        return str(parsed.port)
    return "443" if parsed.scheme == "https" else "80"


def _path_segment(path: str) -> str:
    trimmed = path.strip("/")
    if not trimmed:
        return "root"
    return _truncate_with_hash(_sanitize(trimmed.replace("/", "_")), _MAX_FIELD_LENGTH)


def build_output_stem(template: str, *, target_name: str, url: str, path: str) -> str:
    """Fill in `template`'s {target}/{port}/{path} placeholders. Falls back
    to the default template (never raises) if the stored template is
    malformed, since a bad settings value must never block job creation."""
    fields = {
        "target": _truncate_with_hash(_sanitize(target_name), _MAX_FIELD_LENGTH),
        "port": _port_segment(url),
        "path": _path_segment(path),
    }
    try:
        stem = template.format(**fields)
    except (KeyError, IndexError, ValueError):
        stem = "dirsearch_{target}_{port}_{path}".format(**fields)
    # Individual fields are already capped above -- this is just the safety
    # net for a custom template (Settings -> Output filenames) that repeats
    # a field or adds its own long literal text, so the *combined* stem
    # still can't blow Windows' MAX_PATH budget even then.
    return _truncate_with_hash(_sanitize(stem), _MAX_STEM_LENGTH)
