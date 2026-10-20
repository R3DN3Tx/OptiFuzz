from __future__ import annotations

import socket
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlsplit

from backend.models.job_config import DirsearchJobConfig
from backend.parser.stdout_parser import ParsedResult

_REACHABILITY_CHECK_TIMEOUT_SECONDS = 3.0


class FuzzingEngine(ABC):
    """Interface a fuzzing engine backend must implement.

    dirsearch is the only V1 engine, but this exists so a future engine
    (e.g. FFUF) can be added without touching JobManager/API/frontend.
    """

    #: Whether JobManager should run this engine's subprocess attached to a
    #: pseudo-console instead of a plain pipe, so its live per-request
    #: progress signal is actually emitted rather than silently suppressed.
    #: False keeps the simpler, dependency-free
    #: plain-pipe path -- what MockDirsearchEngine uses for fast tests, and
    #: what DirsearchEngine uses too now (2026-09-24): live progress no
    #: longer needs a pseudo-console at all, see its own comment.
    uses_pty: bool = False

    @abstractmethod
    def build_command(
        self,
        config: DirsearchJobConfig,
        *,
        wordlist_file: Path,
        output_base: Path,
        log_file: Path | None = None,
    ) -> list[str]:
        """Return the argv for one run segment of this job (the full
        wordlist on a fresh run, a remainder on a resume). Never a shell
        string."""

    @abstractmethod
    def parse_line(self, line: str) -> ParsedResult | None:
        """Parse one line of the engine's stdout, or None if it's not a result."""

    @abstractmethod
    def detect_version(self) -> str | None:
        """Return the installed engine's version string, or None if undetectable."""

    def is_target_reachable(self, url: str) -> bool:
        """Quick pre-flight TCP connect check (2026-09-24, explicit request):
        without this, a job against a target with nothing listening on that
        port ran the ENTIRE wordlist against a dead socket and still ended
        as COMPLETED -- dirsearch itself treats every per-request connection
        failure as an ordinary "no match" outcome and exits 0 (verified
        directly against real dirsearch), so there is no signal in its own exit code or
        output to catch this instead. A single short TCP connect to the
        target's own host:port is enough to tell "nothing is listening
        here" apart from "something is listening, dirsearch just found
        nothing interesting" -- callers report `False` as a FAILED job with
        an "Unreachable host" reason, before ever spawning dirsearch (saving
        the whole wordlist's worth of wasted time on a target that can
        never respond).

        Default (real) implementation, shared by every engine that actually
        talks to a real HTTP(S) target -- `MockDirsearchEngine` overrides
        this to always return True, since its own target URLs are never
        real sockets to begin with (mock engine tests must never touch
        the network)."""
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            return True  # unparseable -- let dirsearch itself report the real error
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=_REACHABILITY_CHECK_TIMEOUT_SECONDS):
                return True
        except OSError:
            return False
