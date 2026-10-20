from pathlib import Path

import pytest

from backend.job_manager.wordlist_resolver import (
    find_resume_point,
    load_wordlist_lines,
    resolve_wordlist,
    write_remainder_wordlist,
)


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_resolve_writes_one_concatenated_file(tmp_path: Path) -> None:
    source = _write(tmp_path / "words.txt", [f"word{i}" for i in range(10)])
    out = tmp_path / "resolved" / "full.txt"

    result = resolve_wordlist([source], out)

    assert result.total_lines == 10
    assert load_wordlist_lines(out) == [f"word{i}" for i in range(10)]


def test_multiple_source_files_are_concatenated_in_order(tmp_path: Path) -> None:
    source_a = _write(tmp_path / "a.txt", ["admin", "api"])
    source_b = _write(tmp_path / "b.txt", ["login", "config"])
    out = tmp_path / "full.txt"

    result = resolve_wordlist([source_a, source_b], out)

    assert result.total_lines == 4
    assert load_wordlist_lines(out) == ["admin", "api", "login", "config"]


def test_blank_lines_and_comments_are_preserved_not_filtered(tmp_path: Path) -> None:
    # dirsearch itself skips blanks/comments on expansion -- the resolver
    # must not pre-filter, or line numbers would stop lining up with the
    # raw wordlist a resume needs to slice.
    source = _write(tmp_path / "words.txt", ["admin", "", "# a comment", "api"])
    out = tmp_path / "full.txt"

    resolve_wordlist([source], out)

    assert load_wordlist_lines(out) == ["admin", "", "# a comment", "api"]


def test_empty_wordlist_raises(tmp_path: Path) -> None:
    source = tmp_path / "words.txt"
    source.write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        resolve_wordlist([source], tmp_path / "full.txt")


def test_request_count_without_extensions_is_one_per_line(tmp_path: Path) -> None:
    # dirsearch's own rule (lib/core/wordlist_backend.py): no -f and no
    # literal %ext% in the wordlist means -e doesn't multiply anything at
    # all -- easy to assume otherwise, but confirmed against dirsearch's own
    # source.
    source = _write(tmp_path / "words.txt", ["admin", "api", "config"])
    result = resolve_wordlist([source], tmp_path / "full.txt", extensions=["php", "html"])
    assert result.total_requests == 3


def test_request_count_with_force_extensions(tmp_path: Path) -> None:
    # -f on a line with no "." and no trailing "/": the line itself PLUS
    # the bare directory (line + "/") PLUS one per extension -- verified
    # directly against real dirsearch output (dirsearch always keeps the
    # original line too, not just the -f variants).
    source = _write(tmp_path / "words.txt", ["admin", "api"])
    result = resolve_wordlist(
        [source], tmp_path / "full.txt", extensions=["php", "html"], force_extensions=True,
    )
    # Each line -> {line, line/, line.php, line.html} = 4 requests.
    assert result.total_requests == 8


def test_request_count_force_extensions_skips_lines_with_a_dot_or_trailing_slash(tmp_path: Path) -> None:
    source = _write(tmp_path / "words.txt", ["config.bak", "admin/", "api"])
    result = resolve_wordlist(
        [source], tmp_path / "full.txt", extensions=["php"], force_extensions=True,
    )
    # "config.bak" (has a ".") and "admin/" (trailing slash) -> 1 request
    # each, unmodified; "api" -> {api, api/, api.php} = 3. Total 5.
    assert result.total_requests == 5


def test_request_count_percent_ext_token_expands_per_extension(tmp_path: Path) -> None:
    source = _write(tmp_path / "words.txt", ["backup.%EXT%"])
    result = resolve_wordlist([source], tmp_path / "full.txt", extensions=["zip", "tar", "sql"])
    assert result.total_requests == 3


def test_request_count_deduplicates_across_the_whole_wordlist(tmp_path: Path) -> None:
    # Two raw lines that expand to the same final path only count once,
    # regardless of how far apart they are -- matches dirsearch's own
    # OrderedSet-based dictionary generation for one continuous run (no
    # more per-chunk dedup reset now that a job is one continuous process).
    source = _write(tmp_path / "words.txt", ["admin"] + [f"filler{i}" for i in range(20)] + ["admin"])
    result = resolve_wordlist([source], tmp_path / "full.txt", extensions=["php"])
    assert result.total_requests == 21  # "admin" once + 20 fillers, not 22


def test_write_remainder_wordlist_slices_from_given_line(tmp_path: Path) -> None:
    lines = [f"word{i}" for i in range(10)]
    out = tmp_path / "remainder.txt"

    count = write_remainder_wordlist(lines, 4, out)

    assert count == 6
    assert load_wordlist_lines(out) == [f"word{i}" for i in range(4, 10)]


def test_write_remainder_wordlist_from_the_very_end_is_empty(tmp_path: Path) -> None:
    lines = ["a", "b", "c"]
    out = tmp_path / "remainder.txt"

    count = write_remainder_wordlist(lines, 3, out)

    assert count == 0
    assert load_wordlist_lines(out) == []


def test_find_resume_point_stays_within_target_index(tmp_path: Path) -> None:
    # 10 plain lines, no extensions -> 1 request each. Cutting at index 4
    # (4 requests already dispatched) should resume from line 4, not line 5
    # -- never claim MORE lines done than the target index actually covers.
    lines = [f"word{i}" for i in range(10)]

    line_count, request_count = find_resume_point(lines, target_index=4, extensions=[], force_extensions=False)

    assert line_count == 4
    assert request_count == 4


def test_find_resume_point_respects_multi_request_lines(tmp_path: Path) -> None:
    # Each line expands to 2 requests (%EXT% with 2 extensions). A target
    # index that lands mid-expansion of a line must NOT count that line as
    # done -- only whole lines are ever considered safe.
    lines = ["a.%EXT%", "b.%EXT%", "c.%EXT%"]

    # After line "a" (2 requests), target_index=3 is mid-way through "b"'s
    # own 2 requests -- must stop at line 1 (only "a" done), not line 2.
    line_count, request_count = find_resume_point(
        lines, target_index=3, extensions=["php", "html"], force_extensions=False,
    )

    assert line_count == 1
    assert request_count == 2


def test_find_resume_point_target_at_or_past_total_returns_everything(tmp_path: Path) -> None:
    lines = ["a", "b", "c"]

    line_count, request_count = find_resume_point(lines, target_index=999, extensions=[], force_extensions=False)

    assert line_count == 3
    assert request_count == 3


def test_find_resume_point_zero_target_resumes_from_the_start(tmp_path: Path) -> None:
    lines = ["a", "b", "c"]

    line_count, request_count = find_resume_point(lines, target_index=0, extensions=[], force_extensions=False)

    assert line_count == 0
    assert request_count == 0


def test_estimate_requests_of_a_remainder_counts_paths_already_requested_before_the_pause() -> None:
    # dirsearch dedups a resumed segment on its own, so "admin/" (already requested through the
    # first line) is requested again: the job's total after a resume is done + this estimate,
    # which is larger than the whole-list estimate minus what was done.
    from backend.job_manager.wordlist_resolver import estimate_requests, find_resume_point

    lines = ["admin", "login", "admin/", "login/"]
    whole = estimate_requests(lines, [], True)
    assert whole == 4  # admin, admin/, login, login/
    done_lines, done_requests = find_resume_point(lines, 3, [], True)
    assert (done_lines, done_requests) == (1, 2)
    assert done_requests + estimate_requests(lines[done_lines:], [], True) == 5  # not 4
