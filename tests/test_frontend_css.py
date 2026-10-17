import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "frontend" / "styles.css").read_text(encoding="utf-8")


def _declarations(selector: str) -> str:
    """The declarations of the rule whose selector list is exactly `selector`."""
    match = re.search(r"(?:^|[}/])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, f"rule {selector!r} not found in styles.css"
    return match.group(1)


def test_fuzz_history_panel_resets_inherited_nowrap() -> None:
    # The cell wrapping the panel is the first column of Results' table, which is white-space:
    # nowrap; without this reset the nested table inherits it and a long Options value runs over
    # the neighbouring columns instead of wrapping inside its own.
    assert "white-space: normal" in _declarations(".fuzz-history-panel")


def test_fuzz_history_cells_can_wrap_except_the_short_status_column() -> None:
    for column in (2, 3, 4, 5, 6, 7):
        rule = _declarations(f".fuzz-history-table th:nth-child({column}), .fuzz-history-table td:nth-child({column})")
        assert "nowrap" not in rule, f"column {column} must be able to wrap"
    assert "overflow-wrap: anywhere" in _declarations(".fuzz-history-table th, .fuzz-history-table td")
