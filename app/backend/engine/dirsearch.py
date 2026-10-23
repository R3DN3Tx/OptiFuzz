from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from backend import platform_compat
from backend.engine.base import FuzzingEngine
from backend.models.job_config import DirsearchJobConfig
from backend.parser.stdout_parser import ParsedResult, parse_result_line
from backend.persistence import app_settings

# dirsearch's own options.py does `opt.extensions = opt.extensions or
# config.safe_get("dictionary", "default-extensions", "")` -- its shipped
# config.ini sets that to "php,asp,aspx,jsp,html,htm". Whenever a fuzz's own
# `-e` is never passed at all, dirsearch silently falls back to those six
# instead of genuinely "no extensions" (real, reported bug, 2026-09-25:
# multiplied every eligible wordlist line into 8 real requests instead of
# the 2 a user expects with Force extensions and no extensions chosen).
# `-e ""` does NOT fix this -- an empty string is just as falsy to Python's
# `or` as never passing the flag, so the same fallback still fires.
#
# app_settings.get_default_extensions_fallback() controls what
# build_command does about it below: `None` (not configured, the default)
# leaves dirsearch's own real behavior untouched, matching a plain terminal
# launch; `""` forces genuinely none via this config file (the one case
# needing it, since a CLI flag can't express "empty" past the `or`); a
# non-empty string is passed straight through as a normal `-e` value
# instead (a real, non-empty string has no falsy-fallback problem, so no
# config file is needed for that case).
#
# A verbatim copy of the vendored dirsearch/config.ini, with ONLY
# `default-extensions` blanked out -- never editing the vendored package's
# own file (which a dirsearch upgrade could overwrite/reset).
_NO_DEFAULT_EXTENSIONS_CONFIG_PATH = Path(__file__).resolve().parent / "optifuzz_dirsearch_config.ini"
# The same file with dirsearch's own default-extensions kept. Both blank `skip-on-status`
# (dirsearch ships 429): at the first 429 dirsearch silently ends the whole scan, before it
# prints that response and with exit code 0, so OptiFuzz's auto-pause on 429/503 never saw it.
_DEFAULT_EXTENSIONS_CONFIG_PATH = Path(__file__).resolve().parent / "optifuzz_dirsearch_config_default_ext.ini"


def _default_python_executable() -> str:
    """The interpreter used to spawn dirsearch's subprocess.

    Deliberately never a GUI-subsystem copy of the interpreter, even when
    *this* process (the FastAPI app itself) is running under one -- e.g. the
    desktop app, launched via `OptiFuzz.exe run_desktop.py` (a renamed copy
    of `pythonw.exe`, so Task Manager shows "OptiFuzz" instead of the
    generic "Python") for a genuinely
    console-free start. Originally needed because `uses_pty = True` meant
    every chunk was spawned through a Windows pseudo-console (ConPTY), and a
    *child* process that's itself GUI-subsystem breaks under ConPTY --
    confirmed by direct reproduction: the identical job, same
    `pythonw.exe`-launched parent process and zero console ever created,
    COMPLETED with real results when the child was forced onto `python.exe`,
    FAILED (exit code 1, zero output captured) when left as `sys.executable`
    (`pythonw.exe`). `uses_pty` is `False` now (a different, later bug),
    but this rewrite is kept regardless: it's
    still the more conservative, unambiguously-correct choice for a
    console-subsystem tool's own subprocess, and costs nothing extra.

    Checks "am I already the real console-subsystem python.exe" rather than
    "am I literally named pythonw.exe" -- a renamed copy (`OptiFuzz.exe`,
    still a GUI-subsystem binary internally; a plain file rename doesn't
    change a PE file's own subsystem field) needs the exact same rewrite,
    and this generalizes to it without needing to know every possible name
    in advance.
    """
    if not platform_compat.IS_WINDOWS:
        return sys.executable  # no GUI-subsystem interpreter variant exists off Windows
    exe = Path(sys.executable)
    if exe.name.lower() != "python.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


class DirsearchEngine(FuzzingEngine):
    """Builds argv for the real dirsearch CLI, run as a subprocess.

    Never used through dirsearch's lightweight in-process API
    (`lib.core.api.DirsearchFuzzer`) -- that surface has no threading,
    filters, or report formats.
    """

    # Was True (ConPTY, for dirsearch's live per-request progress bar)
    # until a confirmed, unfixable-from-here bug: every chunk's
    # ConPTY session creates a real Windows console window that steals real
    # OS foreground focus for the chunk's whole duration (verified directly
    # against a live instance -- LockSetForegroundWindow, a reactive
    # SetForegroundWindow restore, and a reactive ShowWindow(SW_HIDE) were
    # all tried and none reliably won against it). Chosen given
    # the tradeoff: no more ConPTY at all -- guarantees the focus theft is
    # gone.
    #
    # **Live progress came back anyway, later (2026-09-24), without ConPTY.**
    # Root cause of "no live progress on a plain pipe" was never actually the
    # missing console -- it was dirsearch's own progress bar silently
    # dropping itself when `shutil.get_terminal_size()` reports too narrow a
    # width (confirmed by reading `lib/view/terminal.py`'s own width guard,
    # then verified empirically: a real dirsearch run with stdout redirected
    # to a plain file, no PTY, no console at all, produces zero progress
    # lines with a default 80-column fallback, and real, correctly-formatted
    # ones the instant COLUMNS/LINES are set wide in the child's own
    # environment). `JobManager._run_chunk` sets those env vars and parses
    # the result with the same non-line-based `LiveOutputParser` the old
    # ConPTY path used (its regexes don't care how the bytes arrived) -- see
    # its own comment for the full story. `uses_pty` stays `False`: this
    # never needed a pseudo-console, real or fake.
    uses_pty = False

    def __init__(self, python_executable: str | None = None, module: str = "dirsearch") -> None:
        self._python_executable = python_executable or _default_python_executable()
        self._module = module

    def _launch_args(self, config: DirsearchJobConfig) -> list[str]:
        """`-m dirsearch`, or a tiny `-c` launcher when default headers are switched off.

        dirsearch merges its DEFAULT_HEADERS under the user's, so the only way
        to stop it sending one is to pop it from that dict before main() runs.
        """
        disabled = list(config.disabled_default_headers)
        if not disabled:
            return ["-m", self._module]
        code = (
            f"import {self._module}\n"
            "from lib.core.settings import DEFAULT_HEADERS\n"
            f"for name in {disabled!r}: DEFAULT_HEADERS.pop(name, None)\n"
            f"from {self._module}.dirsearch import main\n"
            "main()"
        )
        return ["-c", code]

    def build_command(
        self,
        config: DirsearchJobConfig,
        *,
        wordlist_file: Path,
        output_base: Path,
        log_file: Path | None = None,
    ) -> list[str]:
        wordlist_file = Path(wordlist_file)
        output_base = Path(output_base)
        argv = [
            self._python_executable,
            *self._launch_args(config),
            "-u",
            config.url,
            "-w",
            wordlist_file.as_posix(),
            "-t",
            str(config.threads),
            # NOT -q anymore (2026-09-24, see JobManager._run_chunk's own
            # comment for the full story): dirsearch's progress bar doesn't
            # actually need a real console -- it only needs
            # shutil.get_terminal_size() to report a wide enough width, which
            # JobManager satisfies via COLUMNS/LINES env vars on this plain
            # pipe subprocess, no ConPTY involved. Verbose (non-quiet) mode
            # is required for dirsearch to emit that progress bar at all
            # (QuietCLI.last_path is a hardcoded no-op, lib/view/terminal.py
            # -- confirmed by reading dirsearch's own source, not assumed).
            "--no-color",
            # Without this, dirsearch prints bare paths ("/api") instead of
            # absolute URLs -- verified against real output -- which breaks
            # "open in browser" (needs a full http(s) URL) for every result.
            "--full-url",
            "-O",
            "plain,json",
            "-o",
            f"{output_base.as_posix()}.{{extension}}",
        ]

        if config.extensions:
            argv += ["-e", ",".join(config.extensions)]
        elif config.default_extensions:
            argv += ["-e", ",".join(config.default_extensions)]
        else:
            # See this module's own top-of-file comment for the full story.
            fallback = app_settings.get_default_extensions_fallback()
            if fallback:
                argv += ["-e", fallback]
        # Always an OptiFuzz config (see _DEFAULT_EXTENSIONS_CONFIG_PATH): the one with no default
        # extensions when "use none" is chosen, otherwise the one that keeps dirsearch's own
        # default-extensions fallback untouched (fallback None) or is overridden by -e above.
        if not config.extensions and app_settings.get_default_extensions_fallback() == "":
            argv += ["--config", str(_NO_DEFAULT_EXTENSIONS_CONFIG_PATH)]
        else:
            argv += ["--config", str(_DEFAULT_EXTENSIONS_CONFIG_PATH)]
        if log_file is not None:
            # dirsearch writes the reason of every failed request here (timeout, proxy error,
            # connection reset, ...) and nowhere else; JobManager reads it back when a job stops early.
            argv += ["--log", Path(log_file).as_posix()]
        if config.force_extensions:
            argv.append("-f")
        if config.http_method != "GET":
            argv += ["-X", config.http_method]
        for name, value in config.headers.items():
            argv += ["-H", f"{name}: {value}"]
        if config.cookie:
            argv += ["--cookie", config.cookie]
        if config.user_agent:
            argv += ["--user-agent", config.user_agent]
        if config.follow_redirects:
            argv.append("--follow-redirects")
        if config.include_status_codes:
            argv += ["-i", ",".join(config.include_status_codes)]
        if config.exclude_status_codes:
            argv += ["-x", ",".join(config.exclude_status_codes)]
        if config.timeout is not None:
            argv += ["--timeout", str(config.timeout)]
        if config.proxy:
            argv += ["--proxies", config.proxy]
        if config.max_rate is not None:
            argv += ["--max-rate", str(config.max_rate)]
        if config.delay is not None:
            argv += ["--delay", str(config.delay)]
        # Always last, after every flag this form builds itself.
        argv += list(config.extra_args)

        return argv

    def parse_line(self, line: str) -> ParsedResult | None:
        return parse_result_line(line)

    def detect_version(self) -> str | None:
        try:
            proc = subprocess.run(
                [self._python_executable, "-m", self._module, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                # Same fix as JobManager._run_chunk's Popen call, same
                # reason: a console-subsystem python.exe spawned from a
                # console-less pythonw.exe parent otherwise gets a real,
                # briefly-visible console window auto-allocated regardless
                # of output redirection.
                **platform_compat.no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        output = (proc.stdout or proc.stderr or "").strip()
        return output or None
