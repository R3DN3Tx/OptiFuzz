from __future__ import annotations

import sys
from pathlib import Path

from backend.engine.base import FuzzingEngine
from backend.models.job_config import DirsearchJobConfig
from backend.parser.stdout_parser import ParsedResult, parse_result_line

MOCK_ENGINE_VERSION = "mock-dirsearch v0.0.0-test"


class MockDirsearchEngine(FuzzingEngine):
    """Stands in for DirsearchEngine in tests: spawns backend.engine.mock_runner
    instead of real dirsearch, so JobManager/parser tests are fast, deterministic
    and never touch the network.
    """

    def __init__(
        self,
        python_executable: str | None = None,
        *,
        delay: float = 0.0,
        every: int = 4,
        fail: bool = False,
        force_status: int | None = None,
    ) -> None:
        self._python_executable = python_executable or sys.executable
        self._delay = delay
        self._every = every
        self._fail = fail
        self._force_status = force_status

    def build_command(
        self,
        config: DirsearchJobConfig,
        *,
        wordlist_file: Path,
        output_base: Path,
        log_file: Path | None = None,
    ) -> list[str]:
        del output_base, log_file  # the mock runner doesn't write report or log files (yet)
        argv = [
            self._python_executable,
            # A script path, not `-m backend...`: the child process does not need the package on its path.
            str(Path(__file__).with_name("mock_runner.py")),
            "--url",
            config.url,
            "--wordlist",
            str(wordlist_file),
            "--delay",
            str(self._delay),
            "--every",
            str(self._every),
        ]
        if self._fail:
            argv.append("--fail")
        if self._force_status is not None:
            argv += ["--force-status", str(self._force_status)]
        return argv

    def parse_line(self, line: str) -> ParsedResult | None:
        return parse_result_line(line)

    def detect_version(self) -> str | None:
        return MOCK_ENGINE_VERSION

    def is_target_reachable(self, url: str) -> bool:
        # Mock target URLs are never real sockets -- the base class's real TCP-connect check would fail every
        # single mock-engine test otherwise.
        del url
        return True
