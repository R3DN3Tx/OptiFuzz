import sys
from pathlib import Path

import pytest

from backend.engine import dirsearch as dirsearch_module
from backend.engine.dirsearch import (
    _NO_DEFAULT_EXTENSIONS_CONFIG_PATH,
    DirsearchEngine,
    _default_python_executable,
)
from backend.models.job_config import DirsearchJobConfig
from backend.parser.stdout_parser import ParsedResult
from backend.persistence import app_settings


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="pythonw.exe/python.exe is Windows-only")


def test_default_python_executable_is_sys_executable_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dirsearch_module.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(dirsearch_module.sys, "executable", "/opt/venv/bin/python3")

    assert _default_python_executable() == "/opt/venv/bin/python3"


@windows_only
def test_default_python_executable_rewrites_pythonw_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real, previously-shipped bug: the
    # desktop app can run entirely under pythonw.exe with zero console ever
    # created, but the dirsearch CHILD process must never be pythonw.exe --
    # ConPTY silently breaks with a GUI-subsystem child, confirmed by direct
    # reproduction. Every fuzz job must be spawned via python.exe regardless
    # of what interpreter the app itself happens to be running under.
    monkeypatch.setattr(dirsearch_module.sys, "executable", r"C:\env\Scripts\pythonw.exe")
    monkeypatch.setattr(dirsearch_module.Path, "is_file", lambda self: True)

    assert _default_python_executable() == r"C:\env\Scripts\python.exe"


@windows_only
def test_default_python_executable_falls_back_when_python_exe_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dirsearch_module.sys, "executable", r"C:\env\Scripts\pythonw.exe")
    monkeypatch.setattr(dirsearch_module.Path, "is_file", lambda self: False)

    assert _default_python_executable() == r"C:\env\Scripts\pythonw.exe"


@windows_only
def test_default_python_executable_leaves_python_exe_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dirsearch_module.sys, "executable", r"C:\env\Scripts\python.exe")

    assert _default_python_executable() == r"C:\env\Scripts\python.exe"


@windows_only
def test_default_python_executable_rewrites_a_renamed_pythonw_copy_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # run_desktop.py launches via a renamed copy of pythonw.exe (OptiFuzz.exe
    # -- so Task Manager shows "OptiFuzz" instead of the generic "Python") -- still a GUI-subsystem binary internally
    # (renaming a file doesn't touch its PE subsystem field), so it needs
    # the exact same rewrite pythonw.exe itself needed, even though its name
    # doesn't literally contain "pythonw".
    monkeypatch.setattr(dirsearch_module.sys, "executable", r"C:\env\Scripts\OptiFuzz.exe")
    monkeypatch.setattr(dirsearch_module.Path, "is_file", lambda self: True)

    assert _default_python_executable() == r"C:\env\Scripts\python.exe"


def test_build_command_minimal_config(workspace_root) -> None:
    # workspace_root: build_command now reads app_settings.
    # get_default_extensions_fallback(), which without this fixture would
    # hit the REAL user workspace instead of an isolated tmp_path.
    engine = DirsearchEngine(python_executable="python.exe")
    config = DirsearchJobConfig(url="https://target.local/api/")

    argv = engine.build_command(
        config,
        wordlist_file=Path("wordlist.txt"),
        output_base=Path("outputs/job_001"),
    )

    assert argv == [
        "python.exe", "-m", "dirsearch",
        "-u", "https://target.local/api/",
        "-w", "wordlist.txt",
        "-t", "5",
        # No -q (2026-09-24): verbose mode is required for dirsearch to emit
        # its own progress bar at all -- JobManager makes it survive on a
        # plain pipe via COLUMNS/LINES env vars, not via this argv.
        "--no-color",
        "--full-url",
        "-O", "plain,json",
        "-o", "outputs/job_001.{extension}",
    ]
    # No --config either: the default_extensions_fallback app setting is
    # unconfigured (None) in this test, which deliberately leaves dirsearch's
    # own real default-extensions behavior untouched -- see the next tests.
    # Argv paths are always forward-slash, regardless of host OS -- makes the
    # Command Preview identical on Windows and a future Linux
    # build, and Windows accepts forward slashes natively anyway.


def test_build_command_full_config_is_never_a_shell_string(workspace_root) -> None:
    # Golden-file test: this list was hand-verified against every `dest=` in
    # dirsearch 0.5.0's lib/parse/cmdline.py. If
    # this breaks, either the flag names drifted or a real regression crept in.
    engine = DirsearchEngine(python_executable="python.exe")
    config = DirsearchJobConfig(
        url="https://target.local/",
        extensions=("php", "html"),
        force_extensions=True,
        threads=25,
        http_method="POST",
        headers={"Authorization": "Bearer secret; rm -rf /"},
        cookie="session=abc",
        user_agent="OptiFuzz/1.0",
        follow_redirects=True,
        include_status_codes=("200", "301-399"),
        exclude_status_codes=("404",),
        timeout=15.0,
        proxy="http://127.0.0.1:8080",
        max_rate=50,
        delay=0.5,
        extra_args=("--recursive", "--exclude-subdirs", "/api/"),
    )

    argv = engine.build_command(
        config,
        wordlist_file=Path("remainder.txt"),
        output_base=Path("outputs/job_002"),
    )

    assert argv == [
        "python.exe", "-m", "dirsearch",
        "-u", "https://target.local/",
        "-w", "remainder.txt",
        "-t", "25",
        "--no-color",
        "--full-url",
        "-O", "plain,json",
        "-o", "outputs/job_002.{extension}",
        "-e", "php,html",
        "-f",
        "-X", "POST",
        "-H", "Authorization: Bearer secret; rm -rf /",
        "--cookie", "session=abc",
        "--user-agent", "OptiFuzz/1.0",
        "--follow-redirects",
        "-i", "200,301-399",
        "-x", "404",
        "--timeout", "15.0",
        "--proxies", "http://127.0.0.1:8080",
        "--max-rate", "50",
        "--delay", "0.5",
        "--recursive", "--exclude-subdirs", "/api/",
    ]
    # A malicious/careless header value stays one argv element -- subprocess
    # never goes through a shell, so this can't become command injection.
    assert argv[argv.index("-H") + 1] == "Authorization: Bearer secret; rm -rf /"


def test_build_command_default_extensions_fallback_none_uses_the_override_config(
    workspace_root,
) -> None:
    # "" (explicitly configured, not just absent) means "use none, ever" --
    # the one mode that genuinely needs the config-file override, since a
    # plain `-e ""` can't express "empty" past dirsearch's own `or` fallback.
    app_settings.set_default_extensions_fallback("")
    engine = DirsearchEngine(python_executable="python.exe")
    config = DirsearchJobConfig(url="https://target.local/", force_extensions=True)

    argv = engine.build_command(
        config, wordlist_file=Path("wordlist.txt"), output_base=Path("outputs/job_003")
    )

    assert "-e" not in argv
    assert argv[argv.index("--config") + 1] == str(_NO_DEFAULT_EXTENSIONS_CONFIG_PATH)


def test_build_command_default_extensions_fallback_custom_list_uses_a_plain_e_flag(
    workspace_root,
) -> None:
    # A non-empty custom fallback is just a real -e value -- no falsy-`or`
    # problem, so no config-file override is needed for this mode.
    app_settings.set_default_extensions_fallback("bak,old")
    engine = DirsearchEngine(python_executable="python.exe")
    config = DirsearchJobConfig(url="https://target.local/", force_extensions=True)

    argv = engine.build_command(
        config, wordlist_file=Path("wordlist.txt"), output_base=Path("outputs/job_004")
    )

    assert "--config" not in argv
    assert argv[argv.index("-e") + 1] == "bak,old"


def test_build_command_default_extensions_fallback_never_overrides_a_jobs_own_extensions(
    workspace_root,
) -> None:
    # A job that DOES pick its own extensions is never touched by this
    # setting at all, regardless of its mode.
    app_settings.set_default_extensions_fallback("")
    engine = DirsearchEngine(python_executable="python.exe")
    config = DirsearchJobConfig(url="https://target.local/", extensions=("php",))

    argv = engine.build_command(
        config, wordlist_file=Path("wordlist.txt"), output_base=Path("outputs/job_005")
    )

    assert "--config" not in argv
    assert argv[argv.index("-e") + 1] == "php"


def test_parse_line_delegates_to_shared_parser() -> None:
    engine = DirsearchEngine()

    result = engine.parse_line("[20:11:30] 200 -    15B - http://target/x")

    assert isinstance(result, ParsedResult)
    assert result.status == 200


def test_parse_line_returns_none_for_non_result_text() -> None:
    engine = DirsearchEngine()

    assert engine.parse_line("Task Completed") is None


def test_detect_version_against_real_installed_dirsearch() -> None:
    # Not mocked: catches drift if the pinned dirsearch commit
    # (requirements.txt) is ever bumped and its --version output changes
    # shape.
    engine = DirsearchEngine()

    version = engine.detect_version()

    assert version is not None
    assert "dirsearch" in version.lower()
    assert "0.5.0" in version


def test_build_command_without_disabled_headers_launches_the_plain_module() -> None:
    engine = DirsearchEngine(python_executable="python.exe")
    config = DirsearchJobConfig(url="https://target.local/")

    argv = engine.build_command(
        config, wordlist_file=Path("wordlist.txt"), output_base=Path("outputs/job_006")
    )

    assert argv[1:3] == ["-m", "dirsearch"]


def test_build_command_with_disabled_headers_pops_them_before_dirsearch_runs() -> None:
    engine = DirsearchEngine(python_executable=sys.executable)
    config = DirsearchJobConfig(url="https://target.local/", disabled_default_headers=("keep-alive",))

    argv = engine.build_command(
        config, wordlist_file=Path("wordlist.txt"), output_base=Path("outputs/job_007")
    )

    assert argv[1] == "-c"
    assert "['keep-alive']" in argv[2]
    assert argv[3:5] == ["-u", "https://target.local/"]


def test_disabled_headers_launcher_really_starts_dirsearch_and_drops_the_header() -> None:
    import subprocess

    config = DirsearchJobConfig(url="https://target.local/", disabled_default_headers=("keep-alive",))
    args = DirsearchEngine(python_executable=sys.executable)._launch_args(config)
    code = args[1].replace("main()", "import sys; print(sorted(DEFAULT_HEADERS)); sys.exit(0)")

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert "keep-alive" not in result.stdout
    assert "cache-control" in result.stdout


def test_job_config_rejects_an_unknown_default_header_and_round_trips_the_known_ones() -> None:
    from backend.models.job_config import config_from_dict, config_to_dict

    with pytest.raises(ValueError):
        DirsearchJobConfig(url="https://t/", disabled_default_headers=("x-nope",))

    config = DirsearchJobConfig(url="https://t/", disabled_default_headers=("keep-alive",))
    assert config_from_dict(config_to_dict(config)) == config
