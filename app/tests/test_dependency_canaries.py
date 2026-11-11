"""Canaries for the places where OptiFuzz reaches into a dependency beyond its public interface.

When a version in requirements.txt changes and one of these fails, the failing test names what moved.
app/docs/DEPENDENCIES.md lists every one of them and what to check by hand.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

from backend.engine.dirsearch import DirsearchEngine
from backend.models.job_config import DirsearchJobConfig

# Every long option OptiFuzz puts on dirsearch's command line (backend/engine/dirsearch.py, build_command).
_DIRSEARCH_OPTIONS = [
    "--url", "--wordlists", "--threads", "--no-color", "--full-url", "--output-formats", "--output-file",
    "--extensions", "--config", "--log", "--force-extensions", "--http-method", "--header", "--cookie",
    "--user-agent", "--follow-redirects", "--include-status", "--exclude-status", "--timeout", "--proxy",
    "--max-rate", "--delay",
]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_dirsearch_still_accepts_every_option_optifuzz_sends() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dirsearch", "--help-all"], capture_output=True, text=True, timeout=60
    )
    text = result.stdout + result.stderr
    # whole option names: "--proxies" would otherwise match "--proxies-file" (that was BUG-031)
    missing = [o for o in _DIRSEARCH_OPTIONS if not re.search(re.escape(o) + r"(?![\w-])", text)]
    assert not missing, f"dirsearch no longer lists: {missing}"


def test_the_names_inside_dirsearch_that_the_launcher_patches_still_exist() -> None:
    """The launcher (DirsearchEngine._launch_args) changes these before dirsearch starts."""
    result = _run(
        "import dirsearch, re\n"
        "import lib.core.logger as log\n"
        "from lib.core.settings import DEFAULT_HEADERS\n"
        "from dirsearch.dirsearch import main\n"
        "assert isinstance(DEFAULT_HEADERS, dict)\n"
        "assert hasattr(log.QUERY_VALUE_PATTERN, 'sub') and hasattr(log.BARE_QUERY_COMPONENT_PATTERN, 'sub')\n"
        "assert callable(log.redact_log_text) and callable(main)\n"
        "print('ok')\n"
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr


def test_the_launcher_really_changes_what_dirsearch_logs_and_sends() -> None:
    """Runs the exact launcher code with main() swapped for a probe: queries stay readable in the log,
    credentials stay censored, and a disabled default header is gone."""
    config = DirsearchJobConfig(url="https://target.local/", disabled_default_headers=("keep-alive",))
    code = DirsearchEngine(python_executable=sys.executable)._launch_args(config)[1].replace(
        "main()",
        "from lib.core.logger import redact_log_text as r\n"
        "print(r('GET http://user:pw@h/a/?q=../x&p=1'))\n"
        "print(sorted(DEFAULT_HEADERS))",
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert "?q=../x&p=1" in result.stdout and "user:pw" not in result.stdout
    assert "keep-alive" not in result.stdout


def test_dirsearch_request_log_format_is_still_the_one_the_parsers_read() -> None:
    """parse_request_record and the error summary read lines like: "GET url" 403 - 34280B."""
    from backend.job_manager.dirsearch_log import parse_request_record

    source = (Path(importlib.util.find_spec("dirsearch").origin).parent / "lib" / "connection" / "requester.py").read_text(
        encoding="utf-8"
    )
    assert "log_msg = f'\"{options[\"http_method\"]} {response.url}\" {response.status} - {response.length}B'" in source
    assert parse_request_record('2026-11-11 10:00:00,100 [INFO] "GET http://h/a?q=1" 200 - 372B') == (200, "http://h/a?q=1")


def test_the_desktop_window_library_still_has_what_run_desktop_calls() -> None:
    """Looked up in the source, not imported: this test must never open or even prepare a window."""
    spec = importlib.util.find_spec("webview")
    assert spec is not None and spec.origin
    package = Path(spec.origin).parent
    init = (package / "__init__.py").read_text(encoding="utf-8")
    for name in ("def create_window", "def start", "class FileDialog", "class Window"):
        assert name in init or name in "".join(
            p.read_text(encoding="utf-8") for p in package.glob("*.py")
        ), f"pywebview no longer defines: {name}"
