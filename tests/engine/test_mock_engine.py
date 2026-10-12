import subprocess
from pathlib import Path

from backend.engine.mock import MOCK_ENGINE_VERSION, MockDirsearchEngine
from backend.models.job_config import DirsearchJobConfig
from backend.parser.stdout_parser import parse_result_line

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dummy_config(url: str = "https://target.local/") -> DirsearchJobConfig:
    return DirsearchJobConfig(url=url)


def test_build_command_shape() -> None:
    engine = MockDirsearchEngine(python_executable="python.exe", delay=0.01, every=2)

    argv = engine.build_command(
        _dummy_config(),
        wordlist_file=Path("wordlist.txt"),
        output_base=Path("outputs/job_001"),
    )

    assert argv == [
        "python.exe", "-m", "backend.engine.mock_runner",
        "--url", "https://target.local/",
        "--wordlist", "wordlist.txt",
        "--delay", "0.01",
        "--every", "2",
    ]


def test_is_target_reachable_is_always_true_even_for_a_fake_url() -> None:
    # Mock target URLs are never real sockets (docs/OPTIFUZZ_SPEC.md §58) --
    # the base class's real TCP-connect check would fail every mock-engine
    # test otherwise.
    engine = MockDirsearchEngine()

    assert engine.is_target_reachable("https://this-host-does-not-exist.invalid/") is True


def test_detect_version_is_clearly_labeled_as_mock() -> None:
    engine = MockDirsearchEngine()

    version = engine.detect_version()

    assert version == MOCK_ENGINE_VERSION
    assert "mock" in version.lower()


def test_spawned_mock_runner_output_parses_with_the_shared_parser(tmp_path: Path) -> None:
    wordlist = tmp_path / "wordlist.txt"
    wordlist.write_text(
        "admin\napi\nlogin\nconfig\nbackup\nsecret\n", encoding="utf-8"
    )

    engine = MockDirsearchEngine(every=2)
    argv = engine.build_command(
        _dummy_config(),
        wordlist_file=wordlist,
        output_base=tmp_path / "job_001",
    )

    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT)
    )

    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, "mock runner produced no output"

    parsed = [parse_result_line(line) for line in lines]
    assert all(result is not None for result in parsed)
    assert all(result.url.startswith("https://target.local/") for result in parsed)
