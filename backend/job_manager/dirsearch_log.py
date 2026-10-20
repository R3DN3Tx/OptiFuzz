"""Reads the log dirsearch writes with --log, to say WHY requests failed.

dirsearch prints no reason for a failed request (timeout, proxy error, connection reset, ...):
it only bumps a counter and, when --log is given, logs the exception. JobManager starts every job
with --log and reads it back here when the job stops early.

Format (checked against a real run): each failure is one record,
    2026-10-20 14:56:55,265 [ERROR] All connection attempts failed
    Traceback (most recent call last):
      ...
    httpx.ConnectError: All connection attempts failed
and successful requests are [INFO] records.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Enough to hold the last few hundred failures (a traceback is ~1-2 KB each) without reading a 20 MB log.
TAIL_BYTES = 600_000
TOP_REASONS = 3

_RECORD_START_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \[(?P<level>\w+)\] ?(?P<msg>.*)$")
# A top-level exception line closing a traceback (httpx.ReadTimeout: ..., ssl.SSLError: ..., ...).
_EXCEPTION_LINE_RE = re.compile(r"^(?:[\w.]+\.)?(?P<cls>\w*(?:Error|Exception|Timeout)\w*)(?:: (?P<msg>.*))?$")
_URL_RE = re.compile(r"https?://\S+")
_MAX_TEXT = 140


@dataclass(frozen=True)
class RequestErrorSummary:
    count: int
    text: str


def _tidy(text: str) -> str:
    text = _URL_RE.sub("<url>", text.strip())
    text = re.sub(r"0x[0-9a-fA-F]+", "0x…", text)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _reason(record: list[str]) -> str:
    """One short sentence for an [ERROR] record: the exception class that ended its traceback
    plus the message dirsearch logged, e.g. "ReadTimeout: Read timed out"."""
    first = _RECORD_START_RE.match(record[0])
    message = first.group("msg") if first else ""
    cls = None
    for line in record[1:]:
        match = None if line[:1].isspace() else _EXCEPTION_LINE_RE.match(line.strip())
        if match and match.group("cls") != "RequestException":
            cls = match.group("cls")
        elif match and not message:
            message = match.group("msg") or ""
    return _tidy(f"{cls}: {message}" if cls and message else cls or message or "unknown error")


def summarize_request_errors(log_file: Path) -> RequestErrorSummary | None:
    """What went wrong in the (tail of the) log, or None when it holds no request failure."""
    try:
        with open(log_file, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - TAIL_BYTES, 0))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    records: list[list[str]] = []
    for line in text.splitlines():
        start = _RECORD_START_RE.match(line)
        if start:
            records.append([line] if start.group("level") == "ERROR" else [])
        elif records and records[-1]:
            records[-1].append(line)

    reasons = Counter(_reason(record) for record in records if record)
    count = sum(reasons.values())
    if not count:
        return None
    top = "; ".join(f"{n}x {reason}" for reason, n in reasons.most_common(TOP_REASONS))
    return RequestErrorSummary(count=count, text=f"{count} failed requests logged (latest part of the run); most common: {top}")
