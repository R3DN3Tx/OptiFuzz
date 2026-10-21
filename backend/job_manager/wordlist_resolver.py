from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Replaces wordlist_chunker.py (2026-09-24): a job no longer runs as many
# small fixed-size chunk subprocesses (~1.8s of pure spawn overhead paid
# every DEFAULT_CHUNK_SIZE lines, confirmed by direct benchmark 2026-09-23) --
# it runs as ONE continuous dirsearch process against the full wordlist, and
# only re-slices a "remainder" wordlist on an actual pause/resume (see
# JobManager._run_job/_run_segment). This module resolves the job's source
# wordlist(s) into one on-disk file once, at job creation, and provides the
# extension-aware request-counting primitives both progress reporting and
# pause-checkpointing need.


@dataclass(frozen=True)
class ResolvedWordlist:
    total_lines: int
    # Estimated REAL request count (raw lines expanded by -e/-f, same rules
    # dirsearch's own dictionary generation uses -- see _estimate_requests).
    # Deduplicated across the WHOLE wordlist (a single shared `seen` set),
    # matching dirsearch's own single OrderedSet for one continuous
    # invocation -- more accurate than the old per-chunk-reset dedup this
    # replaces (two chunks could each count the same final path once before,
    # continuous execution no longer draws that boundary at all).
    total_requests: int


def resolve_wordlist(
    source_files: list[Path],
    output_path: Path,
    *,
    extensions: list[str] | None = None,
    force_extensions: bool = False,
) -> ResolvedWordlist:
    """Concatenates every source file's lines (in order) into one file at
    output_path -- this becomes the job's permanent on-disk wordlist, read
    back later (`load_wordlist_lines`) to slice a remainder on resume, so it
    must survive an app restart, not just live in memory for this call.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = list(_iter_source_lines(source_files))
    if not lines:
        raise ValueError("resolved wordlist is empty")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    extensions = list(extensions or [])
    total_requests = _estimate_requests(lines, extensions, force_extensions)
    return ResolvedWordlist(total_lines=len(lines), total_requests=total_requests)


def estimate_requests(lines: list[str], extensions: list[str], force_extensions: bool) -> int:
    """Real request count dirsearch makes for `lines` run on their own (fresh dedup)."""
    return _estimate_requests(lines, list(extensions or []), force_extensions)


def load_wordlist_lines(path: Path) -> list[str]:
    return list(_iter_source_lines([path]))


def write_remainder_wordlist(lines: list[str], start_line: int, output_path: Path) -> int:
    """Writes lines[start_line:] to output_path (a fresh "this run's
    wordlist" for a resumed segment). Returns how many lines were written."""
    remainder = lines[start_line:]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(remainder) + "\n" if remainder else "", encoding="utf-8")
    return len(remainder)


def find_resume_point(
    lines: list[str], target_index: int, extensions: list[str], force_extensions: bool,
) -> tuple[int, int]:
    """The safe boundary to resume from after a segment's wordlist (`lines`)
    was cut short at `target_index` real requests already DISPATCHED (not
    necessarily completed -- see JobManager._checkpoint_pause's own comment
    for why the caller must pass an already-margin-adjusted target_index,
    not dirsearch's raw live index).

    Returns (line_count, request_count): the longest prefix of `lines` whose
    own real-request total does not exceed target_index. Walking once with a
    single shared dedup set (matching resolve_wordlist's own global-dedup
    total) rather than calling _estimate_requests per-prefix -- O(n) instead
    of O(n^2), and guarantees a value consistent with total_wordlist_requests.
    """
    extensions = list(extensions or [])
    seen: set[str] = set()
    cumulative = 0
    for index, raw_line in enumerate(lines):
        line_count = _count_line_requests(raw_line, extensions, force_extensions, seen)
        if cumulative + line_count > target_index:
            return index, cumulative
        cumulative += line_count
    return len(lines), cumulative


def _iter_source_lines(source_files: list[Path]) -> Iterator[str]:
    for source in source_files:
        with open(source, "r", encoding="utf-8", errors="ignore") as file_handle:
            for line in file_handle:
                yield line.rstrip("\r\n")


def _estimate_requests(lines: list[str], extensions: list[str], force_extensions: bool) -> int:
    """How many real HTTP requests dirsearch will make for these raw lines,
    replicating `PythonWordlistBackend.generate()`
    (.venv/Lib/site-packages/dirsearch/lib/core/wordlist_backend.py) closely
    enough for the options OptiFuzz's own New Fuzz form actually exposes:

    - A line containing the literal `%ext%` token: one request per
      extension, with the token replaced (dirsearch's "classic" template
      wordlist support) -- or the line as-is if no extensions are set.
    - Force-extensions (-f) on a line with no `.` and no trailing `/`: the
      line *itself*, unmodified, PLUS the bare directory (line + "/") PLUS
      one per extension (line.ext) -- verified against real dirsearch
      output: an earlier version of this function wrongly treated "keep the
      original line" and "add the -f variants" as alternatives, when
      dirsearch's own code (`PythonWordlistBackend.generate()`) always adds
      the line first, unconditionally, before the -f branch even runs.
    - Everything else: exactly one request, unmodified -- **not** one per
      extension. This is the easy-to-miss part of dirsearch's own real
      behavior: without -f (or a literal %ext% in the wordlist), `-e`
      doesn't multiply anything at all.

    Deliberately NOT a byte-for-byte port of dirsearch's full dictionary
    pipeline -- prefixes/suffixes/overwrite-extensions/case transforms
    aren't exposed in OptiFuzz's own config model (only reachable via a raw
    --extra-args flag, an edge case), so a wordlist relying on those via
    extra_args will undercount slightly here. De-duplicates across the whole
    given list (an OrderedSet-like shared `seen` set) -- matches dirsearch's
    own behavior for one continuous invocation.

    Also NOT counted: dirsearch's own fixed, roughly-constant "wildcard
    response" calibration probes it makes once per invocation (a handful of
    random-string paths, independent of the wordlist) -- verified against
    real dirsearch output to exist (varies ~7-12 requests per run in
    testing) but deliberately not replicated: it's a per-invocation constant,
    not something that scales with (or needs to track) the wordlist itself.
    """
    seen: set[str] = set()
    count = 0
    for raw_line in lines:
        count += _count_line_requests(raw_line, extensions, force_extensions, seen)
    return count


def _line_variants(raw_line: str, extensions: list[str], force_extensions: bool) -> list[str]:
    """The paths dirsearch requests for one raw wordlist line (before dedup against earlier lines)."""
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return []
    line = line.lstrip("/")
    lower = line.lower()

    if "%ext%" in lower:
        return [lower.replace("%ext%", ext) for ext in extensions] or [line]
    if force_extensions and "." not in line and not line.endswith("/"):
        # dirsearch always keeps the line itself too (verified against real
        # dirsearch output -- an earlier version of this function missed
        # that, wrongly treating "add the line" and "add the -f variants" as
        # alternatives instead of both happening): {line, line/, line.ext1,
        # line.ext2, ...}.
        return [line, line + "/"] + [f"{line}.{ext}" for ext in extensions]
    return [line]


def _count_line_requests(
    raw_line: str, extensions: list[str], force_extensions: bool, seen: set[str],
) -> int:
    """One raw wordlist line's contribution to a real-request count, against
    a `seen` set shared across the whole walk (a duplicate expanded path --
    from this line or an earlier one -- only ever counts once, matching
    dirsearch's own OrderedSet-based dictionary generation)."""
    count = 0
    for variant in _line_variants(raw_line, extensions, force_extensions):
        if variant not in seen:
            seen.add(variant)
            count += 1
    return count


def first_line_requesting(
    lines: list[str], path: str, extensions: list[str], force_extensions: bool,
) -> int | None:
    """Index in `lines` of the first one that makes dirsearch request `path` (relative to the
    job's base URL, e.g. "cgi-files/"), or None when no line produces it."""
    wanted = path.lstrip("/")
    extensions = list(extensions or [])
    for index, raw_line in enumerate(lines):
        if wanted in _line_variants(raw_line, extensions, force_extensions):
            return index
    return None
