from __future__ import annotations

import re

from backend.parser.stdout_parser import (
    PROGRESS_RE,
    RESULT_LINE_FINDITER_RE,
    ParsedProgress,
    ParsedResult,
)

# Strips ConPTY control sequences so the underlying text (banner, result
# lines, progress bar numbers) can be found with plain regexes -- verified
# against a real captured ConPTY + dirsearch byte stream (docs/ARCHITECTURE.md
# §2.3b): CSI sequences (\x1b[...letter), lone save/restore cursor (\x1b7/\x1b8),
# and the backspace runs dirsearch uses to erase the previous progress bar
# before redrawing it in place.
CONTROL_SEQUENCE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[78]|\x08")

# Trim the buffer once it's grown well past anything a real result line or
# progress segment could span, so a chunk that (rare, but possible) finds
# zero results across many progress updates doesn't grow this unboundedly.
_BUFFER_TRIM_THRESHOLD = 16_384
_BUFFER_TRIM_KEEP = 2_048


class LiveOutputParser:
    """Incrementally turns a raw ConPTY text stream into ParsedResult and
    ParsedProgress events. One instance per chunk subprocess.

    Not line-based: dirsearch's progress bar redraws are cursor-position
    control sequences with no newline between updates (unlike result lines,
    which are real newline-terminated lines even here) -- so this strips
    control sequences from each fed chunk and regex-searches the resulting
    text directly, tracking how much has already been reported so nothing
    is ever emitted twice.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._processed_upto = 0
        self._last_progress: ParsedProgress | None = None

    def feed(self, raw_chunk: str) -> tuple[list[ParsedResult], ParsedProgress | None]:
        self._buffer += CONTROL_SEQUENCE_RE.sub("", raw_chunk)

        results = self._extract_new_results()
        progress = self._extract_latest_progress()
        self._trim_buffer()

        return results, progress

    def flush(self) -> list[ParsedResult]:
        """Call once the underlying stream has genuinely hit EOF (the read
        loop's own `if not data: break`) -- releases a trailing result
        `_extract_new_results` held back (see its own docstring) because no
        more bytes were going to prove it was actually complete. At real EOF
        that risk no longer exists: nothing else is ever coming."""
        return self._extract_new_results(allow_trailing=True)

    def _extract_new_results(self, *, allow_trailing: bool = False) -> list[ParsedResult]:
        # Real, reported bug (2026-09-25): a plain-pipe read() (or a PTY
        # read) can return in the middle of a URL, with nothing else in the
        # buffer after it yet. \S+ has no way to tell "the stream paused
        # here" apart from "the URL genuinely ends here", so finditer's own
        # LAST match can complete early on a truncated prefix -- one real
        # result then gets silently reported with a chopped-off URL, and the
        # actual rest of that line is never re-scanned once the remaining
        # bytes do arrive (_processed_upto already moved past it). Every
        # match except a trailing one flush against the current buffer's own
        # end is safe as-is: it's followed by more real text, which is only
        # possible if its own \S+ had already stopped for real.
        matches = list(RESULT_LINE_FINDITER_RE.finditer(self._buffer, self._processed_upto))
        if matches and not allow_trailing and matches[-1].end() == len(self._buffer):
            matches.pop()
        results: list[ParsedResult] = []
        last_end = self._processed_upto
        for match in matches:
            results.append(
                ParsedResult(
                    time=match.group("time"),
                    status=int(match.group("status")),
                    size=match.group("size"),
                    url=match.group("url"),
                    redirect=match.group("redirect"),
                )
            )
            last_end = match.end()
        self._processed_upto = last_end
        return results

    def _extract_latest_progress(self) -> ParsedProgress | None:
        latest_match = None
        for match in PROGRESS_RE.finditer(self._buffer):
            latest_match = match
        if latest_match is None:
            return None

        progress = ParsedProgress(
            percent=int(latest_match.group("percent")),
            index=int(latest_match.group("index")),
            length=int(latest_match.group("length")),
            rate=int(latest_match.group("rate")),
            errors=int(latest_match.group("errors")),
        )
        if progress == self._last_progress:
            return None
        self._last_progress = progress
        return progress

    def _trim_buffer(self) -> None:
        if len(self._buffer) <= _BUFFER_TRIM_THRESHOLD:
            return
        drop = self._processed_upto - _BUFFER_TRIM_KEEP
        if drop <= 0:
            return
        self._buffer = self._buffer[drop:]
        self._processed_upto -= drop
