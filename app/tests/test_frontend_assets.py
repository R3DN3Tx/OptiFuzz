import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def test_every_bundled_font_referenced_by_the_css_exists() -> None:
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    urls = re.findall(r'url\("/static/(fonts/[^"]+)"\)', css)

    assert urls, "styles.css should declare the bundled @font-face fonts"
    for relative in urls:
        font = FRONTEND / relative
        assert font.is_file(), f"{relative} is referenced by styles.css but missing"
        assert font.stat().st_size > 10_000, f"{relative} looks truncated"


def test_bundled_fonts_ship_with_their_license_notice() -> None:
    notice = (FRONTEND / "fonts" / "README.txt").read_text(encoding="utf-8")

    assert "SIL Open Font License" in notice
    assert "Selawik" in notice and "Cascadia" in notice


def test_new_fuzz_form_follows_the_default_extensions_setting_without_a_tab_switch() -> None:
    # Reported: the "Default extensions activated" field only changed after leaving the tab. Settings announces
    # its new value with a window event and every New Fuzz form listens for it.
    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert source.count('"optifuzz:default-extensions"') == 3  # dispatch, add and remove of the listener


# ---- A "/" inside a query is not a folder separator (BUG-022) ------------------------------------

import json
import shutil
import subprocess

import pytest

from backend.services import export_service

_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"

_PARENT_CASES = {
    "/search/?file=../../etc/passwd": "/search",
    "/search/?file=a/b": "/search",
    "/a/b/c": "/a/b",
    "/a": "/",
    "/?x=a/b": "/",
    "/": None,
}


def _function_source(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += text[j] == "{"
        depth -= text[j] == "}"
        if depth == 0:
            return text[start : j + 1]
    raise AssertionError(name)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("where", ["app", "report"])
def test_parent_of_a_path_never_cuts_inside_the_query(where: str) -> None:
    if where == "app":
        text = (_FRONTEND / "app.js").read_text(encoding="utf-8")
        helpers = _function_source(text, "normalizeKey") + _function_source(text, "parentKeyOf")
        call = "parentKeyOf(%s)"
    else:
        text = export_service.__file__ and Path(export_service.__file__).read_text(encoding="utf-8")
        helpers = _function_source(text, "normalizeKey") + _function_source(text, "parentKeyOf")
        call = "parentKeyOf(normalizeKey(%s))"
    script = helpers + "\n" + "".join(
        f"console.log(JSON.stringify({call % json.dumps(path)}));\n" for path in _PARENT_CASES
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    assert [json.loads(line) for line in out.stdout.splitlines()] == list(_PARENT_CASES.values())


def test_exported_reports_filter_and_tag_fuzzed_places_by_normalised_key() -> None:
    """BUG-023: the Fuzzed chip compared raw result paths with the job's folder, so /admin never matched /admin/."""
    text = Path(export_service.__file__).read_text(encoding="utf-8")
    assert text.count("fuzzedPaths[normalizeKey(r.path)] && isPrimaryResult(r)") == 2
    assert "!fuzzedPaths[r.path]" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_hide_rule_depth_counts_path_segments_not_slashes_in_a_query() -> None:
    """BUG-030: 'parent /search, depth 1' must hide /search/?file=a/b too."""
    text = (_FRONTEND / "app.js").read_text(encoding="utf-8")
    helpers = _function_source(text, "normalizeKey") + _function_source(text, "matchesHideFilter")
    script = helpers + """
const f = { parentPath: "/search", depth: 1 };
const out = ["/search/?file=a/b", "/search/?file=x", "/search/deep/er", "/search/one"].map(
  (p) => matchesHideFilter({ path: p, status: 200, size: "1KB" }, f));
console.log(JSON.stringify(out));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout) == [True, True, False, True]


def test_exported_reports_show_how_many_children_the_hide_rules_hide() -> None:
    """The live app writes "(10 · 3 hidden)" next to a folder; both report templates must do the same."""
    text = Path(export_service.__file__).read_text(encoding="utf-8")
    assert text.count("updateHiddenByRules();") == 2
    assert text.count("hiddenChildCounts[node.key]") == 2


def test_fuzzed_filter_keeps_the_parents_of_a_fuzzed_place_in_the_app_and_in_reports() -> None:
    text = Path(export_service.__file__).read_text(encoding="utf-8")
    assert text.count("isFuzzAncestor(r.path)") == 2
    assert "underFuzzedFolder" not in text  # the children of a fuzzed folder are never kept
    assert "isFuzzAncestor(r.path)" in (_FRONTEND / "app.js").read_text(encoding="utf-8")
