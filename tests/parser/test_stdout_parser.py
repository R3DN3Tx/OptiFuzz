from backend.parser.stdout_parser import ParsedResult, parse_result_line


def test_parses_real_captured_result_line() -> None:
    # Captured verbatim from a real dirsearch 0.5.0 run against a local test
    # server (docs/ARCHITECTURE.md §2.3).
    line = "[20:11:30] 200 -    15B - http://127.0.0.1:8766/index.html"

    result = parse_result_line(line)

    assert result == ParsedResult(
        time="20:11:30",
        status=200,
        size="15B",
        url="http://127.0.0.1:8766/index.html",
        redirect=None,
    )


def test_parses_result_line_with_redirect() -> None:
    line = "[09:00:01] 301 -     0B - http://target/old  ->  http://target/new"

    result = parse_result_line(line)

    assert result is not None
    assert result.status == 301
    assert result.url == "http://target/old"
    assert result.redirect == "http://target/new"


def test_handles_crlf_line_ending() -> None:
    line = "[20:11:30] 200 -    15B - http://127.0.0.1:8766/index.html\r\n"

    result = parse_result_line(line)

    assert result is not None
    assert result.status == 200


def test_returns_none_for_blank_line() -> None:
    assert parse_result_line("") is None
    assert parse_result_line("   \n") is None


def test_returns_none_for_non_result_line() -> None:
    # QuietCLI mostly suppresses these, but interface.error() can still emit
    # text on stdout (docs/ARCHITECTURE.md §2.3) -- the parser must not crash.
    assert parse_result_line("Cannot connect to the target") is None
    assert parse_result_line("Task Completed") is None


def test_returns_none_for_partial_line_missing_url() -> None:
    assert parse_result_line("[20:11:30] 200 -    15B -") is None


def test_returns_none_for_line_with_unexpected_status_format() -> None:
    assert parse_result_line("[20:11:30] OK -    15B - http://target/x") is None
