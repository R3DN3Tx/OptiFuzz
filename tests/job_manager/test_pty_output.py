from backend.job_manager.pty_output import CONTROL_SEQUENCE_RE, LiveOutputParser
from backend.parser.stdout_parser import ParsedProgress, ParsedResult


def _redraw(progress_text: str) -> str:
    """Shapes a progress-bar redraw exactly like a real captured ConPTY +
    dirsearch byte stream (docs/ARCHITECTURE.md §2.3b): save cursor, jump to
    a fixed row, blank it, restore cursor, backspace, then the new text --
    with stray `\\x1b[0m` resets colorama emits even under --no-color."""
    return "\x1b7\x1b[10;1H\x1b[0m" + " " * 40 + "\x1b8" + "\x08" * 40 + progress_text


def _progress_text(percent: int, index: int, length: int) -> str:
    return f"[{'#' * (percent // 5):<20}] {percent:>2}%  {index}/{length}  4/s  job\x1b[0m:1/1  errors\x1b[0m:0"


def test_control_sequence_re_strips_everything_a_real_capture_contains() -> None:
    raw = "\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h\r\n  banner\r\n\x1b[0mExtensions: \x1b[0mhtml\x1b[0m\r\n"

    clean = CONTROL_SEQUENCE_RE.sub("", raw)

    assert "\x1b" not in clean
    assert "\x08" not in clean
    assert clean == "\r\n  banner\r\nExtensions: html\r\n"


def test_extracts_a_single_progress_update() -> None:
    parser = LiveOutputParser()

    results, progress = parser.feed(_progress_text(8, 1, 12))

    assert results == []
    assert progress == ParsedProgress(percent=8, index=1, length=12, rate=4, errors=0)


def test_repeated_identical_progress_is_reported_only_once() -> None:
    parser = LiveOutputParser()
    parser.feed(_progress_text(8, 1, 12))

    _, progress = parser.feed(_redraw(_progress_text(8, 1, 12)))

    assert progress is None


def test_progress_changes_are_reported_each_time() -> None:
    parser = LiveOutputParser()
    seen = []
    for index in (1, 2, 3):
        _, progress = parser.feed(_redraw(_progress_text(index * 8, index, 12)))
        seen.append(progress)

    assert [p.index for p in seen] == [1, 2, 3]


def test_result_lines_survive_interleaved_with_progress_redraws() -> None:
    # Matches the real observed shape: a progress redraw, then a result line
    # (which *is* a genuine newline-terminated line, unlike the progress
    # bar), then more progress redraws.
    parser = LiveOutputParser()
    chunk = (
        _redraw(_progress_text(0, 5, 6717))
        + "\r\n[22:31:58] 301 -     0B - /admin  ->  /admin/\r\n"
        + _redraw(_progress_text(0, 6, 6717))
    )

    results, progress = parser.feed(chunk)

    assert results == [
        ParsedResult(time="22:31:58", status=301, size="0B", url="/admin", redirect="/admin/")
    ]
    assert progress.index == 6


def test_results_are_never_reported_twice_across_multiple_feeds() -> None:
    parser = LiveOutputParser()
    result_line = "[22:31:58] 200 -    38B - /admin/\r\n"

    first_results, _ = parser.feed(result_line)
    second_results, _ = parser.feed(_progress_text(10, 20, 100))

    assert len(first_results) == 1
    assert second_results == []


def test_result_line_split_across_two_feeds_is_found_once_complete() -> None:
    parser = LiveOutputParser()
    line = "[22:31:58] 200 -    38B - /admin/\r\n"
    midpoint = len(line) // 2

    results_a, _ = parser.feed(line[:midpoint])
    results_b, _ = parser.feed(line[midpoint:])

    assert results_a == []
    assert len(results_b) == 1
    assert results_b[0].url == "/admin/"


def test_a_url_split_mid_token_across_two_reads_is_not_truncated() -> None:
    # Real, reported bug (2026-09-25): a plain-pipe read() (STDOUT_READ_CHUNK_SIZE,
    # backend/job_manager/manager.py's _read_stdout) can return in the middle
    # of a URL, with nothing else buffered after it yet. \S+ has nothing to
    # stop it there except "no more text exists RIGHT NOW" -- which finditer
    # can't tell apart from "the URL genuinely ends here" -- so the match
    # completed early, one real result got silently reported with a
    # chopped-off URL, and the actual rest of that line was never re-scanned
    # (already past _processed_upto) once the remaining bytes did arrive.
    parser = LiveOutputParser()
    line = "[22:31:58] 404 -   137B - http://127.0.0.1:8910/legacy-api  (16ms)\r\n"
    split_at = line.index("/legacy-api") + 4  # inside the URL token itself

    results_a, _ = parser.feed(line[:split_at])
    results_b, _ = parser.feed(line[split_at:])

    assert results_a == []  # held back -- looked complete but might not be
    assert len(results_b) == 1
    assert results_b[0].url == "http://127.0.0.1:8910/legacy-api"


def test_flush_releases_a_trailing_result_still_held_back_at_real_eof() -> None:
    # The withholding above must never eat a genuine final result that has
    # no more text after it because the process actually exited right there
    # (no trailing newline/progress-bar redraw left to prove the URL was
    # already complete) -- flush() is what the read loop calls once EOF is
    # confirmed, to release it.
    parser = LiveOutputParser()
    parser.feed("[22:31:58] 200 -    38B - http://target.local/admin")

    results = parser.flush()

    assert results == [
        ParsedResult(time="22:31:58", status=200, size="38B", url="http://target.local/admin", redirect=None)
    ]


def test_buffer_trims_without_losing_or_duplicating_results() -> None:
    parser = LiveOutputParser()
    all_results = []

    for i in range(50):
        # Enough padding per iteration to blow well past the trim threshold.
        chunk = _redraw(_progress_text(i, i, 200)) * 20
        chunk += f"[22:31:{i % 60:02d}] 200 -    1B - /path{i}\r\n"
        results, _ = parser.feed(chunk)
        all_results.extend(results)

    assert [r.url for r in all_results] == [f"/path{i}" for i in range(50)]
