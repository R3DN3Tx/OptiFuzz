from __future__ import annotations

import re
from dataclasses import dataclass

# Matches dirsearch 0.5.0's quiet-mode (`-q --no-color`) result line, verified
# against real output in docs/ARCHITECTURE.md §2.3, e.g.:
#   [20:11:30] 200 -    15B - http://127.0.0.1:8766/index.html
#   [20:11:30] 301 -     0B - http://127.0.0.1:8766/old  ->  http://127.0.0.1:8766/new
# No trailing anchor: a future `-v` addition would append "  (123ms, type)"
# after the URL/redirect, which this intentionally ignores rather than fails.
RESULT_LINE_RE = re.compile(
    r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]\s+(?P<status>\d{3})\s+-\s*(?P<size>\S+)"
    r"\s+-\s+(?P<url>\S+)(?:\s+->\s+(?P<redirect>\S+))?"
)

# Same shape, without the start-of-string anchor: used with finditer() to pull
# result lines out of a PTY stream where they aren't reliably isolated on
# their own line (docs/ARCHITECTURE.md §2.3b) -- the quiet-mode reader above
# still uses the anchored version against real newline-delimited lines.
RESULT_LINE_FINDITER_RE = re.compile(
    r"\[(?P<time>\d{2}:\d{2}:\d{2})\]\s+(?P<status>\d{3})\s+-\s*(?P<size>\S+)"
    r"\s+-\s+(?P<url>\S+)(?:\s+->\s+(?P<redirect>\S+))?"
)

# dirsearch's live progress bar, verified against a real ConPTY-attached run
# (docs/ARCHITECTURE.md §2.3b), e.g.:
#   [####################]  8%       1/12         4/s       job:1/1           errors:0
PROGRESS_RE = re.compile(
    r"(?P<percent>\d+)%\s+(?P<index>\d+)/(?P<length>\d+)\s+(?P<rate>\d+)/s"
    r"\s+job:(?P<job_current>\d+)/(?P<job_total>\d+)\s+errors:(?P<errors>\d+)"
)


@dataclass(frozen=True)
class ParsedResult:
    time: str
    status: int
    size: str
    url: str
    redirect: str | None = None


@dataclass(frozen=True)
class ParsedProgress:
    percent: int
    index: int
    length: int
    rate: int
    errors: int


def parse_result_line(line: str) -> ParsedResult | None:
    """Parse one line of dirsearch quiet-mode stdout into a ParsedResult.

    Returns None for anything that isn't a result line (blank lines, and any
    error/warning text QuietCLI doesn't suppress, per §2.3/§2.5) rather than
    raising, since a stdout reader thread must never crash on unexpected input.
    """
    match = RESULT_LINE_RE.match(line.strip())
    if not match:
        return None

    return ParsedResult(
        time=match.group("time"),
        status=int(match.group("status")),
        size=match.group("size"),
        url=match.group("url"),
        redirect=match.group("redirect"),
    )
