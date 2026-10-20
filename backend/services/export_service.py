from __future__ import annotations

import base64
import html as html_module
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from backend.job_manager.manager import JobManager
from backend.models.timestamps import now_iso
from backend.persistence import app_settings
from backend.persistence.slug import slugify
from backend.services import project_service, results_service, target_service, tree_service

# A single, self-contained HTML snapshot of one
# Target -- Tree (interactive expand/collapse), Results, fuzz
# history, statuses, notes, interesting markers, job information,
# openable from file:/// with no backend running and no external
# requests. Project-level export is deliberately out of
# scope for V1 (the Target export is the priority).
#
# Secret-redaction default (a deliberate choice): headers/cookies are embedded exactly as stored, no
# redaction -- the export is the same trust boundary as the local
# workspace, not a separate one.
#
# Pixel-parity pass (2026-09-23/24, two rounds of explicit follow-up: "no
# me gusta nada... quiero que sea exactamente igual en todos los sentidos a
# la app"). The page's own <style> embeds frontend/styles.css verbatim
# (read from disk at export time) -- every color, font, and layout rule
# (including the header/#scroll-area/#app structure and the .workspace-tabs
# sticky-gap trick) is the SAME CSS the live app uses, not a lookalike.
# This round's DOM was restructured to match the live app's own nesting
# exactly (<header> sibling to <div id="scroll-area"><main id="app">...),
# so the outer layout rules apply with ZERO local override needed anymore
# (round 1 had gotten this wrong -- its own hand-written `main`/
# `.workspace-tabs` override block was silently fighting the embedded real
# CSS's own rules for the same selectors). The logo is embedded as a base64
# data: URI (frontend/logo.png, ~80KB) -- still zero external requests
# -- but the actual real icon instead of leaving it out.
_STYLES_CSS_PATH = Path(__file__).resolve().parents[2] / "frontend" / "styles.css"
_LOGO_PATH = Path(__file__).resolve().parents[2] / "frontend" / "logo.png"


def export_filename(project_name: str, target_name: str) -> str:
    # {project}_{target}_OptiFuzz_Report.html (2026-09-24, explicit request --
    # supersedes an earlier same-day {hostname}_OptiFuzz_report.html attempt).
    # Capital "Report" is deliberate, matching the exact format asked for.
    return f"{slugify(project_name)}_{slugify(target_name)}_OptiFuzz_Report.html"


def build_target_export_data(project_id: str, target_id: str, job_manager: JobManager) -> dict[str, Any]:
    project = project_service.get_project(project_id)
    target = target_service.get_target(project_id, target_id)
    jobs = job_manager.list_jobs(target_id)

    results = results_service.list_results(project_id, target_id, jobs, live_results=job_manager.live_results())
    tree_view = tree_service.tree_views(project_id, target_id, jobs, results)
    results_view = [r.to_dict() for r in results]

    jobs_view = [job.to_dict() for job in sorted(jobs, key=lambda j: j.created_at, reverse=True)]

    return {
        "project": {"id": project.id, "name": project.name},
        "target": {"id": target.id, "name": target.name, "base_url": target.base_url},
        "generated_at": now_iso(),
        "tree": tree_view,
        "results": results_view,
        "jobs": jobs_view,
        "view": _saved_results_view(project_id, target_id),
    }


def _saved_results_view(project_id: str, target_id: str) -> dict[str, Any]:
    """The Results filters/hide rules saved for this target in the live app
    (hide_rules.json + the `results/{project}/{target}` ui-pref). Embedded in
    EVERY export so a downloaded report always opens exactly as the user left
    the app -- never depends on what the browser happens to send."""
    memo = app_settings.get_ui_prefs().get(f"results/{project_id}/{target_id}")
    memo = memo if isinstance(memo, dict) else {}
    return {
        "hide_rules": results_service.get_hide_rules(project_id, target_id),
        "search": str(memo.get("search") or ""),
        "fuzzedOnly": bool(memo.get("fuzzedOnly")),
        "statuses": str(memo.get("statuses") or ""),
    }


_HIDE_RULE_KEYS = {"status", "size", "parentPath", "depth"}


def _sanitize_hide_rules(raw: str | None) -> list[dict[str, Any]]:
    """Parses the `hide_rules` query param (see export_target's own
    docstring) -- untrusted client input, so malformed/unexpected JSON is
    silently dropped (empty list, export still opens with everything
    visible) rather than ever failing the whole export over it. Only the
    same fields the live app's own hide-rule shape ever has survive; an
    unexpected key or type is quietly stripped, not passed through."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        {k: v for k, v in item.items() if k in _HIDE_RULE_KEYS}
        for item in parsed
        if isinstance(item, dict)
    ]


def export_target_html(
    project_id: str, target_id: str, job_manager: JobManager, *, hide_rules_json: str | None = None
) -> str:
    data = build_target_export_data(project_id, target_id, job_manager)
    # </script> or </style> inside embedded JSON/CSS (a fuzzed path/note/
    # header value could legitimately contain either literal substring)
    # would otherwise close that block early and break the page -- the
    # standard escape, applied to both embedded blobs.
    embedded_json = json.dumps(data).replace("</", "<\\/")
    embedded_hide_rules = json.dumps(_sanitize_hide_rules(hide_rules_json)).replace("</", "<\\/")
    try:
        live_css = _STYLES_CSS_PATH.read_text(encoding="utf-8").replace("</style>", "<\\/style>")
    except OSError:
        live_css = ""  # best-effort -- the page still renders, just unstyled, rather than failing the export
    try:
        logo_b64 = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    except OSError:
        logo_b64 = ""
    title = html_module.escape(f"{data['project']['name']} / {data['target']['name']} - OptiFuzz export")
    return (
        _TEMPLATE.replace("__TITLE__", title)
        .replace("/*__LIVE_CSS__*/", live_css)
        .replace("__LOGO_B64__", logo_b64)
        .replace("__DATA_JSON__", embedded_json)
        .replace("__HIDE_RULES_JSON__", embedded_hide_rules)
    )


def export_all_targets_zip(
    project_id: str, job_manager: JobManager, *, hide_rules_json: str | None = None
) -> bytes:
    """Every target in this Project, each as the exact same self-contained
    HTML export_target_html() produces for one target on its own, bundled
    into a single .zip (2026-09-24, explicit request -- "export all targets
    at once" instead of one at a time). `hide_rules_json`, if given, is
    applied the same way to every target's own export in the archive --
    still accepted here for a caller that genuinely wants one shared rule
    set, but the live app's own "Export all targets" button no longer sends
    one: Results hide rules became a per-TARGET concept (2026-10-01, a real
    reported bug -- setting one on a target was silently applying it to
    every other target/project too), so there is no longer a single rule
    set that could correctly represent "all of them" here.

    Two targets with the same display NAME (never the same id -- ids are
    always unique) would otherwise collide on export_filename()'s own
    output and silently overwrite each other inside the zip -- an
    incrementing "-2"/"-3" suffix (before the .html extension) disambiguates
    instead, so every target's report always survives into the archive."""
    project = project_service.get_project(project_id)
    targets = target_service.list_targets(project_id)

    buffer = io.BytesIO()
    used_filenames: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for target in targets:
            html = export_target_html(project_id, target.id, job_manager, hide_rules_json=hide_rules_json)
            filename = export_filename(project.name, target.name)
            if filename in used_filenames:
                used_filenames[filename] += 1
                stem, _, ext = filename.rpartition(".")
                filename = f"{stem}-{used_filenames[filename]}.{ext}"
            else:
                used_filenames[filename] = 1
            archive.writestr(filename, html)
    return buffer.getvalue()


def export_all_filename(project_name: str) -> str:
    # {project}_OptiFuzz_Combined_Report.html -- same naming convention as
    # export_filename()'s own single-target "_OptiFuzz_Report.html".
    return f"{slugify(project_name)}_OptiFuzz_Combined_Report.html"


def build_project_export_data(project_id: str, job_manager: JobManager) -> dict[str, Any]:
    """Every target in this Project, each with its own tree/results/jobs,
    for the single COMBINED HTML export (explicit request, 2026-10-11) --
    as opposed to export_all_targets_zip's one-file-per-target .zip, this
    is one self-contained page that reproduces the live app's own
    Project -> Targets -> Target-workspace navigation, so the whole
    project can be browsed (search, open a target, go back) without ever
    extracting a .zip first. Same "no single hide-rule set could correctly
    represent every target" reasoning as export_all_targets_zip's own
    docstring -- no hide_rules_json parameter here at all; every target
    starts unfiltered, exactly like its own entry in that zip already does."""
    project = project_service.get_project(project_id)
    targets = target_service.list_targets(project_id)
    targets_data = []
    for target in targets:
        data = build_target_export_data(project_id, target.id, job_manager)
        targets_data.append({
            "target": data["target"],
            "tree": data["tree"],
            "results": data["results"],
            "jobs": data["jobs"],
            "view": data["view"],
        })
    return {
        "project": {"id": project.id, "name": project.name},
        "generated_at": now_iso(),
        "targets": targets_data,
    }


def export_project_html(project_id: str, job_manager: JobManager) -> str:
    data = build_project_export_data(project_id, job_manager)
    embedded_json = json.dumps(data).replace("</", "<\\/")
    try:
        live_css = _STYLES_CSS_PATH.read_text(encoding="utf-8").replace("</style>", "<\\/style>")
    except OSError:
        live_css = ""
    try:
        logo_b64 = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    except OSError:
        logo_b64 = ""
    title = html_module.escape(f"{data['project']['name']} - OptiFuzz export")
    return (
        _PROJECT_TEMPLATE.replace("__TITLE__", title)
        .replace("/*__LIVE_CSS__*/", live_css)
        .replace("__LOGO_B64__", logo_b64)
        .replace("__PROJECT_DATA_JSON__", embedded_json)
    )


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
/*__LIVE_CSS__*/
</style>
<style>
/* ---- Export-only additions -------------------------------------------
   Everything above this block is frontend/styles.css, embedded verbatim
   (see export_service.py's own docstring) -- the DOM below mirrors the
   live app's own structure closely enough that no override of a real
   selector (header/.workspace-tabs/main/#app/section) is needed anymore.
   This block only adds things that genuinely don't exist live: tab-panel
   show/hide, the export's own reduced-column tables (an action/delete
   column was dropped throughout -- no live backend to act against), and a
   couple of small pieces (job error text, read-only note) with no exact
   live equivalent. */
.tab-panel.hidden { display: none; }
.toolbar { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; position: relative; }
/* overflow-wrap/word-break/min-width -- real, reported bug: a note longer
   than its column's share of space overflowed past the Notes column
   instead of wrapping (the live app's own note field is an editable
   <textarea>, which never had this problem -- this plain read-only span is
   export-only, see .tree-note-cell-inner's own min-width: 0 comment in
   styles.css for the other half of this fix). */
.export-note { color: var(--muted); font-style: italic; overflow-wrap: anywhere; word-break: break-word; min-width: 0; }
.job-error { color: var(--red); white-space: pre-wrap; font-family: monospace; font-size: 11.5px; }
.job-row { cursor: pointer; }
.hidden { display: none; }
.exp-table { table-layout: fixed; }
/* Real, reported bug (2026-09-24): every one of these used to be a plain
   descendant selector (".exp-jobs td:nth-child(N)"), which doesn't stop at
   this table's own rows -- it matches ANY <td> anywhere inside a .exp-jobs
   table, at ANY nesting depth, including a .fuzz-history-table's own cells
   sitting inside one of THIS table's own <td>s (the "Fuzzed"/info-button
   detail panel, which is a genuinely separate <table> with its own real
   column count and its own real width rules from the embedded live CSS
   above). Whichever rule wins the cascade for a given nth-child position
   (same specificity either way; a coin-flip decided by which one happens
   to be declared later) is not the one that should EVER be in contention
   here -- ">" scopes every one of these to cells that are direct
   grandchildren of THIS table's own <thead>/<tbody>, which a nested
   table's cells structurally never are, no matter how the cascade would
   otherwise resolve it. Confirmed empirically, not just reasoned about:
   inspecting a real export's DOM showed the "Wordlist"/"Extensions"
   columns of a multi-row fuzz-history table rendered at 64px/340px --
   exactly .exp-tree's OWN 2nd/3rd-column widths, not that table's real
   18%/12% -- before this fix. */
.exp-results > tbody > tr > td:nth-child(1), .exp-results > thead > tr > th:nth-child(1) { width: 7%; padding-left: 6px; }
.exp-results > tbody > tr > td:nth-child(2), .exp-results > thead > tr > th:nth-child(2) { width: 7%; }
.exp-results > tbody > tr > td:nth-child(3), .exp-results > thead > tr > th:nth-child(3) { width: 45%; padding-left: 6px; overflow-wrap: anywhere; word-break: break-word; }
.exp-results > tbody > tr > td:nth-child(4), .exp-results > thead > tr > th:nth-child(4) { width: 41%; overflow-wrap: anywhere; word-break: break-word; }
/* Same 7%/7% as .exp-results' own Status/Size above (2026-09-24, explicit
   request), not their own previous fixed 64px each -- Note (nth-child(4)
   below) has no width of its own, so in this fixed-layout table it
   auto-fills whatever percentage this frees up, same "leftover flows to
   the unsized column for free" mechanism as Path's own 35% narrowing just
   below. */
/* padding-left: 6px on Status, explicitly matching .exp-results' own --
   real, reported bug: without it this stayed at the base th/td rule's 8px
   (frontend/styles.css: "th, td { padding: 4px 8px }"), 2px further right
   than Results' own Status column at the exact same width/position,
   visible as a small jump switching quickly between the two tabs.
   Size ALSO needs its own equivalent override, same bug -- an earlier
   session's claim that "both tables already leave it at that same base
   8px" was wrong (not re-verified carefully enough at the time): the
   embedded live CSS's own `.results-table` rule for this exact position
   sets `padding-left: 1px`, but `.tree-table`'s own rule for the SAME
   position never overrides padding-left at all, leaving it at the base
   8px -- since `.exp-results`/`.exp-tree` each only carry ONE of those two
   real classes, each inherits a DIFFERENT, unopposed padding-left here
   (1px vs 8px), a real 7px text-position mismatch confirmed with
   `getBoundingClientRect()` on a real export switching tabs. */
.exp-tree > tbody > tr > td:nth-child(1), .exp-tree > thead > tr > th:nth-child(1) { width: 7%; padding-left: 6px; }
.exp-tree > tbody > tr > td:nth-child(2), .exp-tree > thead > tr > th:nth-child(2) { width: 7%; padding-left: 1px; padding-right: 6px; }
/* text-align: left explicitly -- real, reported bug: the embedded live
   CSS's own .tree-table td:nth-child(3) rule centers that column, because
   in the LIVE app it's the star/lightning-icons column (this export has no
   such column at all -- no backend to act against, per this file's own
   docstring) -- Path just happens to land on the same nth-child(3) index
   in this table's own, shorter column set, and silently inherited that
   unrelated rule's centering instead of the left-align every other column
   already has. */
/* Path narrowed 35% (340px -> 221px), 2026-09-24 explicit request -- Note
   (nth-child(4) below) has no width of its own, so in this fixed-layout
   table it auto-fills whatever Path gives up: the same 35% moves straight
   over to it without needing its own rule. */
/* 221px -> 410px (2026-09-26, same request/ratio as the live app's own
   .tree-path-cell -- see that rule's own comment): Path had far more empty
   space than its content used, Note (unsized, right after it) started too
   close to it. */
.exp-tree > tbody > tr > td:nth-child(3), .exp-tree > thead > tr > th:nth-child(3) { width: 410px; padding-left: 6px; text-align: left; }
.exp-tree > tbody > tr > td:nth-child(4), .exp-tree > thead > tr > th:nth-child(4) { padding-left: 8px; }
/* Real, reported bug (2026-09-24): leaving Path (nth-child(3)) with NO
   width rule here at all, on the assumption it would just get "whatever's
   left" the way an unsized column does in .exp-results/.exp-tree, was
   wrong for THIS table specifically -- this <table> also carries the
   live app's own real `jobs-table` class (for its other, non-layout
   styling), and the embedded live CSS's own real
   ".jobs-table > tbody > tr > td:nth-child(3)" rule (the live app's own
   Actions/Stop-button column, a real, different column that happens to
   sit at this same position in the LIVE app's 10-column layout) has equal
   specificity and nothing here to lose to -- confirmed empirically: Path
   rendered at a hardcoded 150px regardless of actual available space
   (that rule's own value), and instead of the space taken away from the
   live app's actions/delete columns, that mismatched fixed sum. Fixed
   with EXPLICIT percentage widths on every single column (2026-09-24,
   explicit request), never an implicit/unsized one -- every position now
   has its OWN declared value here, so nothing from the embedded rule ever
   has a gap to win by default. Percentages (of this export's own real 7
   columns, summing to 100%, Path getting the visually largest, most
   useful share) rather than px, so they resolve against this table's own
   real 100%-of-container width instead of racing the live app's px values
   at the same nth-child positions the way the previous, partial px-based
   attempt did. */
.exp-jobs > tbody > tr > td:nth-child(1), .exp-jobs > thead > tr > th:nth-child(1) { width: 12.2%; }
.exp-jobs > tbody > tr > td:nth-child(2), .exp-jobs > thead > tr > th:nth-child(2) { width: 3.4%; text-align: center; }
.exp-jobs > tbody > tr > td:nth-child(3), .exp-jobs > thead > tr > th:nth-child(3) { width: 41.3%; overflow-wrap: anywhere; word-break: break-word; }
.exp-jobs > tbody > tr > td:nth-child(4), .exp-jobs > thead > tr > th:nth-child(4) { width: 12.2%; }
.exp-jobs > tbody > tr > td:nth-child(5), .exp-jobs > thead > tr > th:nth-child(5) { width: 6.6%; text-align: center; }
/* Rate/Errors (2026-09-24) took over Date's old slot -- Date itself moved
   into the fuzz-options detail panel below, matching the live app's own
   JOBS table restructure (frontend/app.js's own comment above
   formatRateCellText has the full story). */
.exp-jobs > tbody > tr > td:nth-child(6), .exp-jobs > thead > tr > th:nth-child(6) { width: 11.2%; white-space: nowrap; }
.exp-jobs > tbody > tr > td:nth-child(7), .exp-jobs > thead > tr > th:nth-child(7) { width: 5.6%; }
.exp-jobs > tbody > tr > td:nth-child(8), .exp-jobs > thead > tr > th:nth-child(8) { width: 7.5%; }
/* .fuzz-history-table(.no-status) itself needs no override here at all --
   it's a real, shared live class, already correctly styled by the embedded
   live CSS above (frontend/styles.css, verbatim). A duplicate, stale copy
   of its column widths used to live here too, silently fighting the real
   one (and drifting further out of sync every time the live one changed,
   e.g. its 2026-09-24 Date-column addition) since both applied to the exact
   same selector at equal specificity -- removed outright, not re-copied. */
</style>
</head>
<body>
<header>
  <img class="app-logo" src="data:image/png;base64,__LOGO_B64__" alt="">
  <h1>OptiFuzz</h1>
  <span class="breadcrumb" id="export-breadcrumb"></span>
  <div class="header-spacer"></div>
  <span class="app-version" id="export-meta"></span>
</header>
<div id="scroll-area">
<main id="app">
  <div class="workspace-tabs">
    <a href="#" class="workspace-tab" data-tab="jobs">JOBS</a>
    <a href="#" class="workspace-tab" data-tab="results">RESULTS</a>
    <a href="#" class="workspace-tab active" data-tab="tree">TREE</a>
  </div>
  <section class="tab-panel hidden" id="panel-jobs">
    <table class="jobs-table exp-table exp-jobs">
      <thead><tr>
        <th>Status</th><th></th><th>Path</th><th>Progress</th>
        <th class="jobs-col-center">Results</th><th>Rate</th><th class="jobs-col-center">Errors</th><th>Elapsed</th>
      </tr></thead>
      <tbody id="jobs-body"></tbody>
    </table>
  </section>
  <section class="tab-panel hidden" id="panel-results">
    <div class="toolbar">
      <div class="search-field"><input type="search" id="results-search" class="search-field-input" placeholder="Search path…"></div>
      <div class="filters-dropdown">
        <button type="button" class="filter-chip" id="results-filters-btn">Filters ▾</button>
        <div class="filters-popup" id="results-filters-popup" hidden>
          <button type="button" class="filter-chip" id="results-fuzzed-only">Fuzzed</button>
          <label class="filter-status-row"><span>Status:</span><input class="filter-status-input" placeholder="e.g. 200,301" title="Only show these status codes (comma-separated)" id="results-status-input"></label>
        </div>
      </div>
      <div class="filters-dropdown">
        <button type="button" class="filter-chip" id="results-hide-btn">Hide rules ▾</button>
        <div class="filters-popup hide-rules-popup" id="results-hide-popup" hidden>
          <form class="filter-add-form" id="results-hide-form">
            <input placeholder="Status" title="e.g. 404" id="results-hide-status">
            <input placeholder="Size" title="e.g. 3KB" id="results-hide-size">
            <input class="filter-parent-input" placeholder="Parent path" title="e.g. /admin/" id="results-hide-parent">
            <input type="number" min="0" class="filter-depth-input" placeholder="1" id="results-hide-depth">
            <button type="submit">+ Hide</button>
          </form>
          <div class="hide-rules-chips" id="results-hide-chips"></div>
        </div>
      </div>
    </div>
    <table class="results-table exp-table exp-results">
      <thead><tr><th>Status</th><th>Size</th><th>Path</th><th>Redirect</th></tr></thead>
      <tbody id="results-body"></tbody>
    </table>
  </section>
  <section class="tab-panel" id="panel-tree">
    <div class="toolbar">
      <div class="search-field"><input type="search" id="tree-search" class="search-field-input" placeholder="Search path…"></div>
      <div class="filters-dropdown">
        <button type="button" class="filter-chip" id="tree-filters-btn">Filters ▾</button>
        <div class="filters-popup" id="tree-filters-popup" hidden>
          <button type="button" class="filter-chip icon-text-btn" id="tree-interesting-only"><span class="btn-icon"><svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><polygon points="8 2.3 9.6 5.9 13.5 6.4 10.6 9.1 11.3 13 8 11.1 4.7 13 5.4 9.1 2.5 6.4 6.4 5.9 8 2.3"/></svg></span>Interesting</button>
          <button type="button" class="filter-chip" id="tree-fuzzed-only">Fuzzed</button>
        </div>
      </div>
    </div>
    <table class="tree-table exp-table exp-tree">
      <thead><tr><th>Status</th><th>Size</th><th>Path</th><th>Note</th></tr></thead>
      <tbody id="tree-body"></tbody>
    </table>
  </section>
</main>
</div>
<script type="application/json" id="export-data">__DATA_JSON__</script>
<script>
"use strict";
var DATA = JSON.parse(document.getElementById("export-data").textContent);
var scrollAreaEl = document.getElementById("scroll-area");

function h(tag, attrs, children) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    for (var k in attrs) {
        if (k === "class") el.className = attrs[k];
        else if (k === "text") el.textContent = attrs[k];
        else el.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) el.appendChild(c); });
    return el;
}

// Same icon set as the live app (frontend/app.js's own STANDING RULE:
// every icon is one of these named inline SVGs, never a raw emoji) --
// only the ones actually used read-only here.
var STAR_POINTS = "8 2.3 9.6 5.9 13.5 6.4 10.6 9.1 11.3 13 8 11.1 4.7 13 5.4 9.1 2.5 6.4 6.4 5.9 8 2.3";
var STAR_FILLED_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linejoin="round"><polygon points="' + STAR_POINTS + '"/></svg>';
var INFO_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="8" cy="8" r="6.3"/><line x1="8" y1="7.3" x2="8" y2="11.3"/><circle cx="8" cy="4.9" r="0.15" fill="currentColor"/></svg>';

function statusClass(status) {
    if (status == null) return "hint";
    if (status === 401 || status === 403 || status === 429) return "status status-" + status;
    var family = Math.floor(status / 100);
    return family >= 1 && family <= 5 ? "status status-" + family : "status status-other";
}
function wordlistBaseName(path) {
    if (!path) return "?";
    var parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
}
// Ported from the live app's own positionFloatingBox (frontend/app.js) --
// same algorithm, same behavior: flips to whichever side actually has room,
// caps height to that side's available space.
function positionFloatingBox(anchor, box, maxWidth) {
    var margin = 8;
    var rect = anchor.getBoundingClientRect();
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var boxMaxWidth = Math.min(maxWidth, vw - margin * 2);
    box.style.maxWidth = boxMaxWidth + "px";
    var left = rect.left;
    if (left + boxMaxWidth > vw - margin) left = Math.max(margin, vw - margin - boxMaxWidth);
    var spaceBelow = vh - rect.bottom - margin;
    var spaceAbove = rect.top - margin;
    var top, maxHeight;
    if (spaceBelow >= 80 || spaceBelow >= spaceAbove) {
        top = rect.bottom + 4;
        maxHeight = Math.max(60, spaceBelow - 4);
    } else {
        maxHeight = Math.max(60, spaceAbove - 4);
        top = Math.max(margin, rect.top - 4 - maxHeight);
    }
    box.style.left = left + "px";
    box.style.top = top + "px";
    box.style.maxHeight = maxHeight + "px";
}
// Ported from the live app's own buildHeaderNameChip: a header shows only
// its NAME by default (real, reported bug this fixes: the export used to
// dump every "Name: value" pair in plain, always-visible text, unlike the
// live app's name-only chip), hover reveals "Name: value" in a floating
// box, click copies the same text to the clipboard.
function buildHeaderNameChip(name, value, fullText) {
    if (fullText === undefined) fullText = name + ": " + value;
    var chip = h("span", { class: "fuzz-history-header-chip", text: name });
    var hoverBox = h("div", { class: "fuzz-history-header-hover", text: fullText });
    chip.appendChild(hoverBox);
    chip.addEventListener("mouseenter", function () {
        hoverBox.style.display = "block";
        positionFloatingBox(chip, hoverBox, 420);
    });
    chip.addEventListener("mouseleave", function () { hoverBox.style.display = "none"; });
    var copyResetTimer = null;
    chip.addEventListener("click", function () {
        function afterCopy() {
            var label = chip.firstChild;
            // Same fix as the live app's own click handler: restore to
            // `name` itself (never re-read from the label, which would be
            // "Copied!" on a second click before the first timer fired) and
            // clear any still-pending timer before scheduling a new one.
            if (copyResetTimer !== null) clearTimeout(copyResetTimer);
            label.textContent = "Copied!";
            copyResetTimer = setTimeout(function () { label.textContent = name; copyResetTimer = null; }, 1000);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(fullText).then(afterCopy, function () {});
        }
    });
    return chip;
}
// Same format/columns as the live app's own formatJobDate.
function formatJobDate(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    function pad(n) { return String(n).padStart(2, "0"); }
    return pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + "/" + d.getFullYear() + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function formatElapsedMs(ms) {
    if (ms == null) return "—";
    var totalSeconds = Math.floor(ms / 1000);
    function pad(n) { return String(n).padStart(2, "0"); }
    return pad(Math.floor(totalSeconds / 3600)) + ":" + pad(Math.floor((totalSeconds % 3600) / 60)) + ":" + pad(totalSeconds % 60);
}
// Elapsed/progress are both computed from the job's own static fields
// (started_at/ended_at, completed_lines/completed_requests) -- a fixed
// final value, not a live per-second ticker (nothing to tick against in a
// static snapshot).
function computeElapsedMs(job) {
    if (!job.started_at) return null;
    var end = job.ended_at ? new Date(job.ended_at).getTime() : new Date(DATA.generated_at).getTime();
    return Math.max(0, end - new Date(job.started_at).getTime());
}
function formatProgress(job) {
    var total = job.total_wordlist_requests || job.total_wordlist_lines;
    if (!total) return { pct: "0%", detail: "" };
    var done = job.status === "COMPLETED" ? total : Math.min(job.completed_requests, total);
    var pct = Math.round((done / total) * 100);
    return { pct: pct + "%", detail: " (" + done + "/" + total + ")" };
}
function renderProgressCell(cell, progress) {
    cell.appendChild(document.createTextNode(progress.pct));
    cell.appendChild(h("span", { class: "jobs-progress-detail", text: progress.detail }));
}
// Ported from the live app's own formatRateCellText/formatErrorsCellText
// (frontend/app.js) -- a static export has no live SSE ticks at all, so
// RUNNING here always means "captured mid-run in this snapshot", same "—"
// treatment the live app gives a RUNNING job with no live tick yet.
function formatRateCellText(job) {
    if (job.status === "RUNNING") return "—";
    var elapsedMs = computeElapsedMs(job);
    if (!elapsedMs || !job.completed_requests) return "—";
    var avg = job.completed_requests / (elapsedMs / 1000);
    return "Avg. Rate: " + avg.toFixed(1) + "/s";
}
function formatErrorsCellText(job) {
    return String(job.errors || 0);
}

document.getElementById("export-breadcrumb").textContent =
    DATA.project.name + " / " + DATA.target.name + " (" + DATA.target.base_url + ")";
document.getElementById("export-meta").textContent = "Exported at: " + formatJobDate(DATA.generated_at);

// ---- Tabs: same JOBS/RESULTS/TREE navigation as the live app's own
// .workspace-tabs/.workspace-tab. Nothing here ever launches/pauses/stops
// a fuzz -- there's no backend to do that against -- read-only navigation
// and client-side filtering only.
function switchToExportTab(tabName) {
    Array.prototype.forEach.call(document.querySelectorAll(".workspace-tab"), function (b) {
        b.classList.toggle("active", b.dataset.tab === tabName);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab-panel"), function (panel) {
        panel.classList.toggle("hidden", panel.id !== "panel-" + tabName);
    });
}
Array.prototype.forEach.call(document.querySelectorAll(".workspace-tab"), function (btn) {
    btn.addEventListener("click", function (e) {
        e.preventDefault();
        switchToExportTab(btn.dataset.tab);
    });
});
// Generic "Filters ▾" dropdown open/close, shared by Results/TREE -- click
// the toggle button, click anywhere else closes it. The real CSS's own
// `.filters-popup:not([hidden]) { display: flex; }` (styles.css, embedded
// verbatim above) keys off the actual HTML `hidden` ATTRIBUTE/DOM property
// (a higher-specificity rule than any plain `.hidden { display: none }`
// class could ever win against) -- toggling `.hidden` as a property here,
// exactly like the live app's own `popup.hidden = ...`, not a CSS class.
Array.prototype.forEach.call(document.querySelectorAll(".filters-dropdown"), function (dropdown) {
    var btn = dropdown.querySelector(".filter-chip");
    var popup = dropdown.querySelector(".filters-popup");
    btn.addEventListener("click", function (e) {
        e.stopPropagation();
        popup.hidden = !popup.hidden;
    });
});
document.addEventListener("click", function (e) {
    if (e.target.closest(".filters-dropdown")) return;
    Array.prototype.forEach.call(document.querySelectorAll(".filters-popup"), function (p) { p.hidden = true; });
});

// ---- Fuzz Tree / Results: same normalize/hierarchy/connector logic as
// the live app (frontend/app.js's normalizeKey/computeDisplaySegment/
// treeConnectorFor), operating on the embedded JSON instead of a live
// API -- must stay a real interactive tree here too, not
// flattened into a list.
function normalizeKey(path) {
    var k = path === "/" ? "/" : path.replace(/\/+$/, "");
    return k === "" ? "/" : k;
}
function parentKeyOf(key) {
    if (key === "/") return null;
    var idx = key.lastIndexOf("/");
    return idx <= 0 ? "/" : key.slice(0, idx);
}
function computeDisplaySegment(rawPath, parentKey, isDirectory) {
    var fullKey = normalizeKey(rawPath);
    var segment;
    if (parentKey !== null) {
        var prefix = parentKey === "/" ? "/" : parentKey + "/";
        segment = fullKey.indexOf(prefix) === 0 ? fullKey.slice(prefix.length) : fullKey.replace(/^\/+/, "");
    } else {
        segment = fullKey === "/" ? "" : fullKey.replace(/^\/+/, "");
    }
    if (isDirectory && segment && segment.charAt(segment.length - 1) !== "/") segment += "/";
    return segment || "/";
}
// ---- Right-click "Copy full path" on a clickable path link -- same helper
// as the live app's own attachCopyPathContextMenu, same markup/CSS classes
// (styles.css above is embedded verbatim, so no export-local CSS needed).
var openPathContextMenu = null;
function closePathContextMenu() {
    if (openPathContextMenu) {
        openPathContextMenu.remove();
        openPathContextMenu = null;
    }
}
document.addEventListener("click", closePathContextMenu);
document.addEventListener("scroll", closePathContextMenu, true);
function attachCopyPathContextMenu(element, fullPath) {
    element.addEventListener("contextmenu", function (e) {
        e.preventDefault();
        e.stopPropagation();
        closePathContextMenu();
        var item = h("button", { type: "button", class: "path-context-menu-item", text: "Copy full path" });
        item.addEventListener("click", function (ev) {
            ev.stopPropagation();
            navigator.clipboard.writeText(fullPath).catch(function () { /* denied/unavailable -- fail silently */ });
            closePathContextMenu();
        });
        var menu = h("div", { class: "path-context-menu" }, [item]);
        menu.style.left = e.clientX + "px";
        menu.style.top = e.clientY + "px";
        document.body.appendChild(menu);
        openPathContextMenu = menu;
        var rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = Math.max(8, window.innerWidth - rect.width - 8) + "px";
        if (rect.bottom > window.innerHeight) menu.style.top = Math.max(8, window.innerHeight - rect.height - 8) + "px";
    });
}
function treeConnectorFor(depth, prefix, isLast) {
    return depth === 0 ? "" : prefix + (isLast ? "└── " : "├── ");
}
function treeChildPrefixFor(depth, prefix, isLast) {
    return depth === 0 ? "" : prefix + (isLast ? "    " : "│   ");
}
// Same fix as the live app's own appendConnectorUnits (frontend/app.js,
// see its comment) -- splits the connector string into one span per 4-char
// unit instead of one span for the whole string, so an ancestor's "│" line
// can keep going (.tree-connector-line, CSS embedded verbatim above)
// through a row's own wrapped 2nd/3rd display lines instead of stopping
// after line 1 (docs/identacion.png).
function appendConnectorUnits(pathPrefix, connector) {
    if (!connector) return;
    var units = connector.match(/.{1,4}/g) || [];
    units.forEach(function (unit, i) {
        var continues = i < units.length - 1 && unit.charAt(0) === "│";
        pathPrefix.appendChild(h("span", {
            class: continues ? "tree-connector tree-connector-line" : "tree-connector",
            text: unit,
        }));
    });
}
// Real bug found and fixed here: linking parents in a SEPARATE pass over
// Object.keys(byKey) only ever saw the keys that existed at the moment
// that snapshot was taken -- any synthetic ancestor `ensure()` itself
// created *during* that same pass (e.g. "/api/v1", implied only by a
// real "/api/v1/users" with no "/api/v1" of its own) was never itself
// visited, so IT never got linked to ITS OWN parent ("/api") in turn.
// Any real item more than one synthetic level deep (a genuinely common
// shape -- Results in particular has no explicit "add this intermediate
// segment" step the way TREE does) silently vanished from the rendered
// hierarchy entirely, orphaned under an unreachable ancestor. Fixed by
// linking a node to its parent recursively, AT THE MOMENT it's first
// created, walking the full chain back to root immediately instead of a
// second pass over a stale snapshot.
// Real, reported data-loss bug fixed here (2026-09-25, mirroring the same
// fix in the live app's own buildPathHierarchy, frontend/app.js): two raw
// items can normalize to the same key -- the classic dirsearch redirect
// pair, "/admin" (301 stub) and "/admin/" (the real 200 hit), are separate,
// genuinely distinct results. This used to overwrite `node.item` with
// whichever item was seen LAST, silently discarding the other -- "la tool
// necesita mostrar TODOS los resultados". `node.items` (plural, every
// distinct item at that key, in encounter order) replaces it; both row
// builders below render one row per item sharing a position instead of
// picking a single winner.
function buildHierarchy(items) {
    var byKey = {};
    function ensure(key) {
        if (byKey[key]) return byKey[key];
        var node = { key: key, items: [], children: [] };
        byKey[key] = node;
        if (key !== "/") ensure(parentKeyOf(key)).children.push(node);
        return node;
    }
    ensure("/");
    items.forEach(function (item) { ensure(normalizeKey(item.path)).items.push(item); });
    Object.keys(byKey).forEach(function (key) {
        byKey[key].children.sort(function (a, b) { return a.key < b.key ? -1 : a.key > b.key ? 1 : 0; });
    });
    return byKey["/"];
}

// ---- Fuzz history: the exact same read-only .fuzz-history-table the live
// app uses (TREE/Results' own "Fuzzed" tag toggle, and a job's own history)
// -- same classes, same columns (minus Status for a single-job case, see
// the .no-status modifier above), so it looks and behaves identically.
function buildFuzzHistoryTable(history, hideStatus, path) {
    if (!history || !history.length) {
        return h("div", { class: "hint", text: "Never fuzzed directly." });
    }
    var headCells = [];
    if (!hideStatus) headCells.push(h("th", { text: "Status" }));
    headCells.push(
        h("th", { text: "Wordlist" }), h("th", { text: "Extensions" }), h("th", { text: "Headers" }), h("th", { text: "Options" }),
    );
    // Never shown in JOBS' own single-job panel (hideStatus) -- same as the
    // live app, it overflowed that narrower panel; the main JOBS row's own
    // Results cell is the one to look at there instead.
    if (!hideStatus) headCells.push(h("th", { text: "Results", class: "jobs-col-center" }));
    // Date now shows in every one of these panels (2026-09-24, explicit
    // request) -- every entry here (JOBS', TREE's, and Results' own, all
    // ultimately from tree_service.fuzz_history's own dict shape server-side)
    // always carries created_at, so this is effectively always true in
    // practice; the .some() guard is just defensive.
    var showDate = history.some(function (entry) { return entry.date || entry.created_at; });
    if (showDate) headCells.push(h("th", { text: "Date" }));
    var anyQuery = false;
    var rows = history.map(function (entry) {
        var headerEntries = Object.entries(entry.config && entry.config.headers || {});
        var headersCell = h("td", {});
        // A query string in the job's URL is not a header: shown first as
        // "query*" (hover = the full query), explained by the footnote below.
        var queryText = String(entry.path || (entry.config && entry.config.url) || "").split("#")[0];
        var queryAt = queryText.indexOf("?");
        var query = queryAt >= 0 && queryAt < queryText.length - 1 ? queryText.slice(queryAt) : "";
        if (query) {
            headersCell.appendChild(buildHeaderNameChip("query*", query, query));
            anyQuery = true;
        }
        headerEntries.forEach(function (pair, idx) {
            if (idx > 0 || query) headersCell.appendChild(document.createTextNode(", "));
            headersCell.appendChild(buildHeaderNameChip(pair[0], String(pair[1])));
        });
        if (!headerEntries.length && !query) headersCell.appendChild(document.createTextNode("—"));
        var other = [];
        var cfg = entry.config || {};
        // Same explicit order as the live app: threads, force-ext, AUTH, auto
        // pause, then whatever else was actually set.
        if (cfg.threads) other.push("threads " + cfg.threads);
        if (cfg.force_extensions) other.push("force-ext");
        if (entry.output_stem && /__AUTH$/.test(entry.output_stem)) other.push("AUTH");
        if (entry.pause_on_status && entry.pause_on_status.length) other.push("auto pause " + entry.pause_on_status.join(","));
        if (cfg.http_method && cfg.http_method !== "GET") other.push(cfg.http_method);
        if (cfg.proxy) other.push("proxy " + cfg.proxy);
        if (cfg.disabled_default_headers && cfg.disabled_default_headers.length) other.push("no " + cfg.disabled_default_headers.join(", "));
        if (cfg.extra_args && cfg.extra_args.length) other.push(String(cfg.extra_args));
        var cells = [];
        if (!hideStatus) cells.push(h("td", {}, [h("span", { class: "job-status job-" + entry.status, text: entry.status })]));
        cells.push(
            h("td", { text: (entry.wordlist_sources || []).map(wordlistBaseName).join(", ") || "?" }),
            h("td", { text: (entry.extensions || []).join(",") || "—" }),
            headersCell,
            h("td", { text: other.join(" · ") || "—" }),
        );
        if (!hideStatus) {
            var resultsCell = h("td", { class: "jobs-col-center" });
            if (entry.result_count) {
                var resultsLink = h("button", {
                    type: "button", class: "results-count-link", title: "Go to these results",
                    text: String(entry.result_count),
                });
                resultsLink.addEventListener("click", function (e) {
                    e.stopPropagation();
                    goToResultsAndReveal(path);
                });
                resultsCell.appendChild(resultsLink);
            } else {
                resultsCell.textContent = entry.result_count == null ? "—" : String(entry.result_count);
            }
            cells.push(resultsCell);
        }
        if (showDate) {
            var dateValue = entry.date || entry.created_at;
            cells.push(h("td", { class: "jobs-date-cell", text: dateValue ? formatJobDate(dateValue) : "—" }));
        }
        return h("tr", {}, cells);
    });
    var sections = [h("thead", {}, [h("tr", {}, headCells)]), h("tbody", {}, rows)];
    if (anyQuery) {
        sections.push(h("tfoot", {}, [h("tr", {}, [
            h("td", { class: "fuzz-history-note", colspan: String(headCells.length), text: "* query = query string appended to every request (not a header)" }),
        ])]));
    }
    return h("table", { class: "fuzz-history-table" + (hideStatus ? " no-status" : "") }, sections);
}
function buildFuzzHistoryRow(colspan, history, hideStatus, path) {
    return h("tr", { class: "fuzz-history-row hidden" }, [
        h("td", { colspan: String(colspan) }, [h("div", { class: "fuzz-history-panel" }, [buildFuzzHistoryTable(history, hideStatus, path)])]),
    ]);
}
function buildFuzzedTagButton(detail, onToggle) {
    var btn = h("button", {
        type: "button", class: "fuzzed-tag fuzz-history-toggle", title: "Already fuzzed — click for history",
        text: detail ? "Fuzzed: " + detail : "Fuzzed",
    });
    // .active toggled here, self-contained -- matches the live app's own
    // ".fuzzed-tag.fuzz-history-toggle.active" color change while its
    // detail panel is open (frontend/app.js re-renders the whole row with
    // an isOpen flag to get the same effect; this static export has no
    // equivalent re-render step, so the class is flipped directly on click
    // instead). The embedded live CSS above already has the matching rule,
    // no export-local style needed.
    btn.addEventListener("click", function (e) { e.stopPropagation(); btn.classList.toggle("active"); onToggle(btn); });
    return btn;
}
function formatFuzzedTag(history) {
    if (!history || !history.length) return null;
    // Same as the live app's own formatFuzzedTag -- every distinct
    // wordlist(+extensions) combo this path was fuzzed with, not just the
    // latest.
    var seen = {};
    var parts = [];
    history.forEach(function (entry) {
        var wordlists = (entry.wordlist_sources || []).map(wordlistBaseName).join(", ") || "?";
        var exts = (entry.extensions || []).join(",");
        var part = exts ? wordlists + " (" + exts + ")" : wordlists;
        if (!seen[part]) {
            seen[part] = true;
            parts.push(part);
        }
    });
    return parts.join(", ");
}

// ---- Fuzz Tree: TREE's own row builder -- every node fully EXPANDED by
// default (explicit request), independent toggles for children (arrow) vs
// the "Fuzzed" tag's own history detail (unlike Results, TREE always shows
// its note text inline too, never behind a toggle).
function appendTreeRows(tbody, node, depth, parentKey, prefix, isLast, forceVisible) {
    var childCount = node.children.length;
    // Every distinct item at this position gets its own row (2026-09-25 --
    // see buildHierarchy's own comment); an empty `items` (a synthetic
    // ancestor -- fuzzing straight at /a/b/c never independently discovers
    // /a or /a/b) still renders exactly one placeholder row, same as
    // before. Only the LAST row in the group carries the expand
    // arrow/children -- they render (and collapse) exactly once per
    // position, not once per item sharing it.
    var itemsToRender = node.items.length ? node.items : [null];
    var handles = itemsToRender.map(function (item, idx) {
        var isLastItem = idx === itemsToRender.length - 1;
        // Only the bottom item of a shared position closes the branch -- see the live app's own note in appendHierarchyRows.
        var connector = treeConnectorFor(depth, prefix, isLastItem ? isLast : false);
        var thisChildCount = isLastItem ? childCount : 0;
        var isDirectory = thisChildCount > 0 || (item && item.path && item.path.charAt(item.path.length - 1) === "/");
        var displaySegment = computeDisplaySegment(item ? item.path : node.key, parentKey, isDirectory);
        var statusInfo = item && item.status ? item.status.status : null;

        // Two flex siblings, same as the live app's own buildNodeRow (see its
        // comment) -- a path long enough to wrap onto a 2nd line stays
        // indented under its own content instead of cutting back across the
        // connector/tree-lines below it.
        var pathCell = h("td", { class: "tree-path-cell" });
        var pathCellInner = h("div", { class: "tree-path-cell-inner" });
        pathCell.appendChild(pathCellInner);
        var pathPrefix = h("span", { class: "tree-path-prefix" });
        pathCellInner.appendChild(pathPrefix);
        appendConnectorUnits(pathPrefix, connector);
        var expandBtn = null;
        if (thisChildCount > 0) {
            expandBtn = h("button", { type: "button", class: "result-expand", title: "Expand/collapse", text: "▾" });
            pathPrefix.appendChild(expandBtn);
        }
        var pathContent = h("span", { class: "tree-path-content" });
        pathCellInner.appendChild(pathContent);
        // Clickable like Results/JOBS already are in this export (explicit
        // request -- TREE paths were still plain, non-clickable text here).
        var treeHref = "#";
        try { treeHref = new URL(item ? item.path : node.key, DATA.target.base_url).href; } catch (e) { /* malformed -- fall back to "#" */ }
        var treeLink = h("a", { class: "result-path-link", href: treeHref, target: "_blank", rel: "noopener", text: displaySegment });
        attachCopyPathContextMenu(treeLink, item ? item.path : node.key);
        pathContent.appendChild(treeLink);
        if (item && item.interesting) {
            var star = h("span", { title: "Marked interesting" });
            star.innerHTML = STAR_FILLED_ICON_SVG;
            star.style.color = "var(--yellow)";
            star.style.marginLeft = "4px";
            star.style.verticalAlign = "middle";
            pathContent.appendChild(star);
        }

        var history = item ? (item.fuzz_history || []) : [];
        var fuzzedTagText = formatFuzzedTag(history);
        var noteInner = h("div", { class: "tree-note-cell-inner" });
        var detailRow = null;
        if (fuzzedTagText) {
            detailRow = buildFuzzHistoryRow(4, history, false, item ? item.path : node.key);
            noteInner.appendChild(buildFuzzedTagButton(fuzzedTagText, function () { detailRow.classList.toggle("hidden"); }));
        }
        if (item && item.note) noteInner.appendChild(h("span", { class: "export-note", text: item.note }));
        var noteCell = h("td", {}, [noteInner]);

        var row = h("tr", {}, [
            h("td", { class: statusClass(statusInfo), text: statusInfo != null ? String(statusInfo) : "—" }),
            h("td", { text: (item && item.status && item.status.size) || "" }),
            pathCell,
            noteCell,
        ]);
        tbody.appendChild(row);
        if (detailRow) tbody.appendChild(detailRow);
        if (!forceVisible) row.classList.add("hidden"); // never true for TREE's own default -- see renderTree()

        return { row: row, detailRow: detailRow, expandBtn: expandBtn, childHandles: [] };
    });

    var lastHandle = handles[handles.length - 1];
    var childHandles = [];
    var childPrefix = treeChildPrefixFor(depth, prefix, isLast);
    node.children.forEach(function (child, i) {
        childHandles = childHandles.concat(
            appendTreeRows(tbody, child, depth + 1, node.key, childPrefix, i === node.children.length - 1, forceVisible)
        );
    });
    lastHandle.childHandles = childHandles;

    function setVisible(hs, visible) {
        hs.forEach(function (handle) {
            handle.row.classList.toggle("hidden", !visible);
            if (!visible && handle.detailRow) handle.detailRow.classList.add("hidden");
            if (handle.expandBtn) handle.expandBtn.textContent = visible ? "▾" : "▸";
            setVisible(handle.childHandles, visible);
        });
    }
    if (lastHandle.expandBtn) {
        lastHandle.expandBtn.textContent = "▾"; // TREE starts fully expanded (explicit request)
        lastHandle.expandBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            var expanding = lastHandle.expandBtn.textContent !== "▾";
            lastHandle.expandBtn.textContent = expanding ? "▾" : "▸";
            setVisible(childHandles, expanding);
        });
    }

    return handles;
}

var treeBody = document.getElementById("tree-body");
var treeSearchInput = document.getElementById("tree-search");
var treeInterestingBtn = document.getElementById("tree-interesting-only");
var treeFuzzedBtn = document.getElementById("tree-fuzzed-only");

function passesTreeFilters(item) {
    var query = treeSearchInput.value.trim().toLowerCase();
    if (query && item.path.toLowerCase().indexOf(query) === -1) return false;
    if (treeInterestingBtn.classList.contains("active") && !item.interesting) return false;
    if (treeFuzzedBtn.classList.contains("active") && !(item.fuzz_history && item.fuzz_history.length)) return false;
    return true;
}
function renderTree() {
    treeBody.innerHTML = "";
    if (!DATA.tree.length) {
        treeBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No paths in the tree." })]));
        return;
    }
    var filtered = DATA.tree.filter(passesTreeFilters);
    if (!filtered.length) {
        treeBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No paths match your filters." })]));
        return;
    }
    // TREE keeps its own root ("/" is a real, always-present node, unlike
    // Results below) and is always fully expanded (explicit request) --
    // search/filters never collapse anything here, there's nothing to
    // revert on clear.
    appendTreeRows(treeBody, buildHierarchy(filtered), 0, null, "", true, true);
}
treeSearchInput.addEventListener("input", renderTree);
[treeInterestingBtn, treeFuzzedBtn].forEach(function (btn) {
    btn.addEventListener("click", function () { btn.classList.toggle("active"); renderTree(); });
});
renderTree();

// ---- Results: same hierarchy/connector rendering as Tree, but its own 4
// columns (Status/Size/Path/Redirect -- no note/interesting, TREE-only
// concepts), collapsed by default (unlike TREE), its own root NEVER shown
// as its own row (explicit request -- only real results matter here), and
// full Hide rules support (status/size/parent-path/depth), same as live.
var fuzzedPaths = {};
// Exact job paths, same rule as the live app: "path/" and "path" are separate.
DATA.jobs.forEach(function (j) { fuzzedPaths[String(j.path).split(/[?#]/)[0]] = true; });
function fuzzHistoryForPath(path) {
    return DATA.jobs.filter(function (j) { return String(j.path).split(/[?#]/)[0] === path; }).map(function (j) {
        return {
            job_id: j.id, status: j.status, wordlist_sources: j.wordlist_sources,
            extensions: (j.config && j.config.extensions) || [], config: j.config || {},
            created_at: j.created_at, started_at: j.started_at,
        };
    });
}

// Seeded from whatever hide rules were active in the live app AT EXPORT
// TIME (see export_target's own docstring, backend/api/targets.py) --
// still fully editable from here on, exactly like the live app.
var resultsHideFilters = __HIDE_RULES_JSON__;
function matchesHideFilter(result, f) {
    if (f.status && String(result.status) !== String(f.status)) return false;
    if (f.size && (result.size || "").trim().toLowerCase() !== f.size.trim().toLowerCase()) return false;
    if (f.parentPath) {
        var parentKey = normalizeKey(f.parentPath);
        var resultKey = normalizeKey(result.path);
        var prefix = parentKey === "/" ? "/" : parentKey + "/";
        if (resultKey.indexOf(prefix) !== 0 || resultKey === parentKey) return false;
        var depth = f.depth === undefined ? 0 : f.depth;
        if (depth > 0) {
            var segments = resultKey.slice(prefix.length).split("/").length;
            if (segments > depth) return false;
        }
    }
    return true;
}

var resultsBody = document.getElementById("results-body");
var resultsSearchInput = document.getElementById("results-search");
var resultsFuzzedBtn = document.getElementById("results-fuzzed-only");
var resultsStatusInput = document.getElementById("results-status-input");
var resultsStatusSet = [];
// Shared by the status input AND the Fuzzed toggle -- each status code counts
// on its own ("200,302" = 2, not 1); the live app does the same.
function updateResultsFiltersCount() {
    var n = (resultsFuzzedBtn.classList.contains("active") ? 1 : 0) + resultsStatusSet.length;
    document.getElementById("results-filters-btn").textContent = "Filters ▾" + (n ? " (" + n + ")" : "");
    document.getElementById("results-filters-btn").classList.toggle("active", n > 0);
}
resultsStatusInput.addEventListener("input", function () {
    resultsStatusSet = resultsStatusInput.value.split(",").map(function (s) { return s.trim(); }).filter(function (s) { return /^\d+$/.test(s); }).map(Number);
    resultsStatusInput.classList.toggle("active", resultsStatusInput.value.trim().length > 0);
    updateResultsFiltersCount();
    renderResults();
});

function passesResultsFilters(r) {
    var query = resultsSearchInput.value.trim().toLowerCase();
    if (query && r.path.toLowerCase().indexOf(query) === -1) return false;
    if (resultsFuzzedBtn.classList.contains("active") && !fuzzedPaths[r.path]) return false;
    if (resultsStatusSet.length && resultsStatusSet.indexOf(r.status) === -1) return false;
    if (resultsHideFilters.some(function (f) { return matchesHideFilter(r, f); })) return false;
    return true;
}

function appendResultRows(tbody, node, depth, parentKey, prefix, isLast, forceVisible) {
    var childCount = node.children.length;
    // Every distinct item at this position gets its own row (2026-09-25 --
    // see buildHierarchy's own comment); an empty `items` (a synthetic
    // ancestor) still renders exactly one placeholder row, same as before.
    // Only the LAST row in the group carries the expand arrow/children.
    var itemsToRender = node.items.length ? node.items : [null];
    var handles = itemsToRender.map(function (item, idx) {
        var isLastItem = idx === itemsToRender.length - 1;
        var connector = treeConnectorFor(depth, prefix, isLastItem ? isLast : false);
        var thisChildCount = isLastItem ? childCount : 0;
        var isDirectory = thisChildCount > 0 || (item && item.path && item.path.charAt(item.path.length - 1) === "/");
        var displaySegment = computeDisplaySegment(item ? item.path : node.key, parentKey, isDirectory);

        // Two flex siblings, same as the live app's own buildResultRow (see
        // its comment) -- a path long enough to wrap onto a 2nd line stays
        // indented under its own content instead of cutting back across the
        // connector/tree-lines below it.
        var pathCell = h("td", {});
        var pathCellInner = h("div", { class: "tree-path-cell-inner" });
        pathCell.appendChild(pathCellInner);
        var pathPrefix = h("span", { class: "tree-path-prefix" });
        pathCellInner.appendChild(pathPrefix);
        appendConnectorUnits(pathPrefix, connector);
        var expandBtn = null;
        if (thisChildCount > 0) {
            expandBtn = h("button", {
                type: "button", class: "result-expand", title: "Expand/collapse",
                text: forceVisible ? "▾" : "▸",
            });
            pathPrefix.appendChild(expandBtn);
        }
        var pathContent = h("span", { class: "tree-path-content" });
        pathCellInner.appendChild(pathContent);
        var link = h("a", { class: "result-path-link", href: item ? item.url : "#", target: "_blank", rel: "noopener", text: displaySegment });
        attachCopyPathContextMenu(link, item ? item.path : node.key);
        pathContent.appendChild(link);
        if (thisChildCount > 0) pathContent.appendChild(h("span", { class: "hint", text: " (" + thisChildCount + ")" }));

        var history = item ? fuzzHistoryForPath(item.path) : [];
        var detailRow = null;
        if (history.length) {
            detailRow = buildFuzzHistoryRow(4, history, false, item ? item.path : node.key);
            pathContent.appendChild(buildFuzzedTagButton(null, function () { detailRow.classList.toggle("hidden"); }));
        }

        var redirectCell = h("td", {});
        if (item && item.redirect) {
            var crossOrigin = false;
            var label = item.redirect;
            try {
                var resolved = new URL(item.redirect, item.url);
                var sourceOrigin = new URL(item.url).origin;
                crossOrigin = resolved.origin !== sourceOrigin;
                label = crossOrigin ? resolved.href : resolved.pathname + resolved.search + resolved.hash;
            } catch (e) { /* malformed Location header -- show the raw string as-is */ }
            redirectCell.appendChild(h("a", {
                class: "result-redirect-link" + (crossOrigin ? " redirect-cross-origin" : ""),
                href: item.redirect, target: "_blank", rel: "noopener", text: label,
            }));
        }

        var row = h("tr", {}, [
            h("td", { class: statusClass(item ? item.status : null), text: item ? String(item.status) : "—" }),
            h("td", { text: (item && item.size) || "" }),
            pathCell,
            redirectCell,
        ]);
        tbody.appendChild(row);
        if (detailRow) tbody.appendChild(detailRow);
        // depth > 0 -- a top-level (depth 0) row is always visible regardless
        // of forceVisible/collapse state; only its DESCENDANTS start hidden
        // when collapsed by default. Real bug fixed here: applying this
        // unconditionally hid every row, including top-level ones, leaving
        // Results completely empty whenever it wasn't in "searching" mode.
        if (depth > 0 && !forceVisible) row.classList.add("hidden");

        return { row: row, detailRow: detailRow, expandBtn: expandBtn, childHandles: [] };
    });

    var lastHandle = handles[handles.length - 1];
    // Registered by exact key so goToResultsAndReveal (below) can find this
    // node's own row(s) and children again later, from a click made in a
    // totally different tab/panel -- same lookup the live app's own
    // resultsPlanKeys.indexOf(key) does against its virtualized plan.
    resultNodesByKey[node.key] = { handles: handles, lastHandle: lastHandle };
    var childHandles = [];
    var childPrefix = treeChildPrefixFor(depth, prefix, isLast);
    node.children.forEach(function (child, i) {
        childHandles = childHandles.concat(
            appendResultRows(tbody, child, depth + 1, node.key, childPrefix, i === node.children.length - 1, forceVisible)
        );
    });
    lastHandle.childHandles = childHandles;

    function setVisible(hs, visible) {
        hs.forEach(function (handle) {
            handle.row.classList.toggle("hidden", !visible);
            if (!visible && handle.detailRow) handle.detailRow.classList.add("hidden");
            if (handle.expandBtn) handle.expandBtn.textContent = visible ? "▾" : "▸";
            setVisible(handle.childHandles, visible);
        });
    }
    if (lastHandle.expandBtn) {
        // Real, reported bug: this used to listen on the whole ROW and
        // exclude clicks on any <button> (so the Fuzzed-tag button and the
        // path link wouldn't ALSO toggle expand) -- but the expand arrow
        // itself is also a <button>, so that same guard silently ate its
        // own clicks too, and it could never be expanded at all. Matches
        // TREE's own (already correct) pattern above: listen on the arrow
        // itself, not the row.
        lastHandle.expandBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            var expanding = lastHandle.expandBtn.textContent !== "▾";
            lastHandle.expandBtn.textContent = expanding ? "▾" : "▸";
            setVisible(childHandles, expanding);
        });
    }

    return handles;
}

var resultNodesByKey = {};
function renderResults() {
    resultsBody.innerHTML = "";
    resultNodesByKey = {};
    if (!DATA.results.length) {
        resultsBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No results." })]));
        return;
    }
    var filtered = DATA.results.filter(passesResultsFilters);
    if (!filtered.length) {
        resultsBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No results match your filters." })]));
        return;
    }
    // Never the root's own row (explicit request) -- its real children
    // just become top-level rows directly. While a search is active,
    // everything renders fully expanded (so a deep match is actually
    // visible) -- reverts to the normal collapsed-by-default state the
    // instant the search box is cleared, since this is a full rebuild
    // either way, not a remembered per-node toggle.
    var searching = !!resultsSearchInput.value.trim();
    var hierarchy = buildHierarchy(filtered);
    // Root itself is never its own row (see the comment above) -- it still
    // needs an entry in resultNodesByKey, with no row of its own, so
    // goToResultsAndReveal(path) can be given the exact root path ("/", a
    // job fuzzed right at the target's base URL) and still correctly
    // re-collapse its own grandchildren, same contract as every other path.
    var rootTopHandles;
    if (hierarchy.items.length) {
        rootTopHandles = appendResultRows(resultsBody, hierarchy, 0, null, "", true, searching);
    } else {
        rootTopHandles = [];
        hierarchy.children.forEach(function (child, i) {
            rootTopHandles = rootTopHandles.concat(
                appendResultRows(resultsBody, child, 0, null, "", i === hierarchy.children.length - 1, searching)
            );
        });
    }
    resultNodesByKey["/"] = { handles: [], lastHandle: { row: null, expandBtn: null, childHandles: rootTopHandles } };
}
resultsSearchInput.addEventListener("input", renderResults);
resultsFuzzedBtn.addEventListener("click", function () {
    resultsFuzzedBtn.classList.toggle("active");
    updateResultsFiltersCount();
    renderResults();
});

// Hide rules -- same composable status/size/parent-path/depth rules as the
// live app, same add-form + chip-list UI, operating on the embedded JSON.
var hideForm = document.getElementById("results-hide-form");
var hideChips = document.getElementById("results-hide-chips");
var hideBtn = document.getElementById("results-hide-btn");
function renderHideChips() {
    hideChips.innerHTML = "";
    hideBtn.textContent = "Hide rules ▾" + (resultsHideFilters.length ? " (" + resultsHideFilters.length + ")" : "");
    hideBtn.classList.toggle("active", resultsHideFilters.length > 0);
    resultsHideFilters.forEach(function (f) {
        var depthSuffix = !f.parentPath ? null
            : (f.depth === undefined || f.depth === 0) ? "all descendants"
            : "depth " + f.depth; // same as the live app -- depth 1 reads as "depth 1", not "direct children"
        var label = [
            f.status && ("status " + f.status), f.size,
            f.parentPath && (depthSuffix ? f.parentPath + " (" + depthSuffix + ")" : f.parentPath),
        ].filter(Boolean).join(" · ");
        var removeBtn = h("button", { type: "button", text: "✕", title: "Remove filter" });
        removeBtn.addEventListener("click", function () {
            var idx = resultsHideFilters.indexOf(f);
            if (idx !== -1) resultsHideFilters.splice(idx, 1);
            renderHideChips();
            renderResults();
        });
        hideChips.appendChild(h("span", { class: "filter-chip-remove" }, [document.createTextNode(label), removeBtn]));
    });
}
hideForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var statusInput = document.getElementById("results-hide-status");
    var sizeInput = document.getElementById("results-hide-size");
    var parentInput = document.getElementById("results-hide-parent");
    var depthInput = document.getElementById("results-hide-depth");
    var depthRaw = depthInput.value.trim();
    var f = {
        status: statusInput.value.trim(), size: sizeInput.value.trim(), parentPath: parentInput.value.trim(),
        depth: depthRaw ? Number(depthRaw) : 1,
    };
    if (!f.status && !f.size && !f.parentPath) return;
    resultsHideFilters.push(f);
    statusInput.value = ""; sizeInput.value = ""; parentInput.value = ""; depthInput.value = "";
    renderHideChips();
    renderResults();
});
function applyResultsView(view) {
    view = view || {};
    resultsSearchInput.value = view.search || "";
    resultsFuzzedBtn.classList.toggle("active", !!view.fuzzedOnly);
    resultsStatusInput.value = view.statuses || "";
    resultsStatusInput.classList.toggle("active", resultsStatusInput.value.trim().length > 0);
    resultsStatusSet = resultsStatusInput.value.split(",").map(function (s) { return s.trim(); }).filter(function (s) { return /^\d+$/.test(s); }).map(Number);
    updateResultsFiltersCount();
}
if (!resultsHideFilters.length) resultsHideFilters = (DATA.view && DATA.view.hide_rules) || [];
applyResultsView(DATA.view);
renderHideChips();
renderResults();

// Fully collapses a subtree (used right below, to force grandchildren
// closed no matter what was expanded/collapsed before).
function collapseSubtree(handles) {
    handles.forEach(function (handle) {
        handle.row.classList.add("hidden");
        if (handle.detailRow) handle.detailRow.classList.add("hidden");
        if (handle.expandBtn) handle.expandBtn.textContent = "▸";
        collapseSubtree(handle.childHandles);
    });
}
// ---- Click a Results count (in the JOBS main table, or any Fuzzed-history
// panel's own Results cell in TREE/Results) -- same explicit request as
// the live app's own goToResultsAndReveal/revealResultsPath: switch to
// RESULTS, expand exactly `path`'s own direct children (grandchildren stay
// collapsed regardless of whatever was expanded before), and scroll so the
// path's own row -- not just its children -- actually ends up visible.
function goToResultsAndReveal(path) {
    switchToExportTab("results");
    var key = normalizeKey(path);
    var chain = [];
    var k = key;
    while (k) { chain.unshift(k); k = parentKeyOf(k); }
    chain.forEach(function (ancKey) {
        var entry = resultNodesByKey[ancKey];
        if (!entry) return;
        entry.handles.forEach(function (handle) { handle.row.classList.remove("hidden"); });
        if (entry.lastHandle.expandBtn) entry.lastHandle.expandBtn.textContent = "▾";
    });
    var targetEntry = resultNodesByKey[key];
    if (!targetEntry) return; // filtered/hidden out of the current view -- nothing to reveal
    targetEntry.lastHandle.childHandles.forEach(function (child) {
        child.row.classList.remove("hidden");
        if (child.expandBtn) child.expandBtn.textContent = "▸";
        collapseSubtree(child.childHandles);
    });
    var targetRow = targetEntry.lastHandle.row;
    if (!targetRow) return; // the root path ("/") has no row of its own -- nothing further to scroll to
    // Wait a frame so the tab switch's own layout/visibility change has
    // actually taken effect before reading getBoundingClientRect -- reading
    // it in the same tick as switchToExportTab can still reflect the
    // now-stale "hidden" layout.
    requestAnimationFrame(function () {
        var tabsBar = document.querySelector(".workspace-tabs");
        var tabsHeight = tabsBar ? tabsBar.offsetHeight : 0;
        var rect = targetRow.getBoundingClientRect();
        var areaRect = scrollAreaEl.getBoundingClientRect();
        var delta = (rect.top - areaRect.top) - tabsHeight - 12;
        scrollAreaEl.scrollTop = Math.max(0, scrollAreaEl.scrollTop + delta);
    });
}

// ---- Jobs: the live JOBS tab's own .jobs-table/.job-status pill/info-btn
// toggle, minus the Stop/Resume/Pause/delete columns (no backend to act
// against). The normal "i" reveals the same .fuzz-history-table detail
// TREE/Results use (Status column dropped there since the row right above
// it already IS this exact job) -- a FAILED row's own red icon is
// independent of it and shows ONLY the error message, matching the live
// app's own two separate controls (a plain info toggle vs. a
// showModal("Job failed", ...) call) instead of merging both into one
// shared panel.
var jobsBody = document.getElementById("jobs-body");
if (!DATA.jobs.length) {
    jobsBody.appendChild(h("tr", {}, [h("td", { colspan: "8", class: "empty", text: "No jobs." })]));
} else {
    DATA.jobs.forEach(function (job) {
        var infoBtn = h("button", { type: "button", class: "icon-btn job-info-btn", title: "Fuzz options this job ran with" });
        infoBtn.innerHTML = INFO_ICON_SVG;
        var progressCell = h("td", {});
        renderProgressCell(progressCell, formatProgress(job));

        var statusCell = h("td", {}, [h("span", { class: "job-status job-" + job.status, text: job.status })]);
        var errorRow = null;
        if (job.status === "FAILED" && job.error_reason) {
            errorRow = h("tr", { class: "fuzz-history-row hidden" }, [
                h("td", { colspan: "8" }, [h("div", { class: "fuzz-history-panel" }, [h("div", { class: "job-error", text: job.error_reason })])]),
            ]);
            var errBtn = h("button", { type: "button", class: "icon-btn job-error-info", title: "Why did this fail?", text: "ⓘ" });
            errBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                errorRow.classList.toggle("hidden");
            });
            statusCell.appendChild(errBtn);
        }

        var absoluteUrl;
        try { absoluteUrl = new URL(job.path, DATA.target.base_url).href; } catch (e) { absoluteUrl = DATA.target.base_url; }
        var jobPathLink = h("a", { class: "result-path-link", href: absoluteUrl, target: "_blank", rel: "noopener", text: String(job.path).split(/[?#]/)[0] });
        attachCopyPathContextMenu(jobPathLink, job.path);
        var pathCell = h("td", {}, [jobPathLink]);

        var resultsCell = h("td", { class: "jobs-col-center" });
        if (job.result_count) {
            var resultsLink = h("button", {
                type: "button", class: "results-count-link", title: "Go to these results",
                text: String(job.result_count),
            });
            resultsLink.addEventListener("click", function (e) {
                e.stopPropagation();
                goToResultsAndReveal(String(job.path).split(/[?#]/)[0]);
            });
            resultsCell.appendChild(resultsLink);
        } else {
            resultsCell.textContent = String(job.result_count != null ? job.result_count : "");
        }

        var row = h("tr", {}, [
            statusCell,
            h("td", { class: "job-info-cell" }, [infoBtn]),
            pathCell,
            progressCell,
            resultsCell,
            h("td", { class: "jobs-rate-cell", text: formatRateCellText(job) }),
            h("td", { class: "jobs-errors-cell", text: formatErrorsCellText(job) }),
            h("td", { class: "jobs-elapsed-cell", text: formatElapsedMs(computeElapsedMs(job)) }),
        ]);
        jobsBody.appendChild(row);
        if (errorRow) jobsBody.appendChild(errorRow);

        var detailRow = buildFuzzHistoryRow(8, [{
            status: job.status, wordlist_sources: job.wordlist_sources,
            extensions: job.config && job.config.extensions, config: job.config,
            date: job.started_at || job.created_at, result_count: job.result_count, output_stem: job.output_stem,
            pause_on_status: job.pause_on_status || [],
        }], true);
        jobsBody.appendChild(detailRow);
        infoBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            infoBtn.classList.toggle("job-info-btn-active");
            detailRow.classList.toggle("hidden");
        });
    });
}
</script>
</body>
</html>
"""



_PROJECT_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
/*__LIVE_CSS__*/
</style>
<style>
/* ---- Export-only additions -------------------------------------------
   Everything above this block is frontend/styles.css, embedded verbatim
   (see export_service.py's own docstring) -- the DOM below mirrors the
   live app's own structure closely enough that no override of a real
   selector (header/.workspace-tabs/main/#app/section) is needed anymore.
   This block only adds things that genuinely don't exist live: tab-panel
   show/hide, the export's own reduced-column tables (an action/delete
   column was dropped throughout -- no live backend to act against), and a
   couple of small pieces (job error text, read-only note) with no exact
   live equivalent. */
.tab-panel.hidden { display: none; }
.toolbar { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; position: relative; }
/* overflow-wrap/word-break/min-width -- real, reported bug: a note longer
   than its column's share of space overflowed past the Notes column
   instead of wrapping (the live app's own note field is an editable
   <textarea>, which never had this problem -- this plain read-only span is
   export-only, see .tree-note-cell-inner's own min-width: 0 comment in
   styles.css for the other half of this fix). */
.export-note { color: var(--muted); font-style: italic; overflow-wrap: anywhere; word-break: break-word; min-width: 0; }
.job-error { color: var(--red); white-space: pre-wrap; font-family: monospace; font-size: 11.5px; }
.job-row { cursor: pointer; }
.hidden { display: none; }
.exp-table { table-layout: fixed; }
/* Real, reported bug (2026-09-24): every one of these used to be a plain
   descendant selector (".exp-jobs td:nth-child(N)"), which doesn't stop at
   this table's own rows -- it matches ANY <td> anywhere inside a .exp-jobs
   table, at ANY nesting depth, including a .fuzz-history-table's own cells
   sitting inside one of THIS table's own <td>s (the "Fuzzed"/info-button
   detail panel, which is a genuinely separate <table> with its own real
   column count and its own real width rules from the embedded live CSS
   above). Whichever rule wins the cascade for a given nth-child position
   (same specificity either way; a coin-flip decided by which one happens
   to be declared later) is not the one that should EVER be in contention
   here -- ">" scopes every one of these to cells that are direct
   grandchildren of THIS table's own <thead>/<tbody>, which a nested
   table's cells structurally never are, no matter how the cascade would
   otherwise resolve it. Confirmed empirically, not just reasoned about:
   inspecting a real export's DOM showed the "Wordlist"/"Extensions"
   columns of a multi-row fuzz-history table rendered at 64px/340px --
   exactly .exp-tree's OWN 2nd/3rd-column widths, not that table's real
   18%/12% -- before this fix. */
.exp-results > tbody > tr > td:nth-child(1), .exp-results > thead > tr > th:nth-child(1) { width: 7%; padding-left: 6px; }
.exp-results > tbody > tr > td:nth-child(2), .exp-results > thead > tr > th:nth-child(2) { width: 7%; }
.exp-results > tbody > tr > td:nth-child(3), .exp-results > thead > tr > th:nth-child(3) { width: 45%; padding-left: 6px; overflow-wrap: anywhere; word-break: break-word; }
.exp-results > tbody > tr > td:nth-child(4), .exp-results > thead > tr > th:nth-child(4) { width: 41%; overflow-wrap: anywhere; word-break: break-word; }
/* Same 7%/7% as .exp-results' own Status/Size above (2026-09-24, explicit
   request), not their own previous fixed 64px each -- Note (nth-child(4)
   below) has no width of its own, so in this fixed-layout table it
   auto-fills whatever percentage this frees up, same "leftover flows to
   the unsized column for free" mechanism as Path's own 35% narrowing just
   below. */
/* padding-left: 6px on Status, explicitly matching .exp-results' own --
   real, reported bug: without it this stayed at the base th/td rule's 8px
   (frontend/styles.css: "th, td { padding: 4px 8px }"), 2px further right
   than Results' own Status column at the exact same width/position,
   visible as a small jump switching quickly between the two tabs.
   Size ALSO needs its own equivalent override, same bug -- an earlier
   session's claim that "both tables already leave it at that same base
   8px" was wrong (not re-verified carefully enough at the time): the
   embedded live CSS's own `.results-table` rule for this exact position
   sets `padding-left: 1px`, but `.tree-table`'s own rule for the SAME
   position never overrides padding-left at all, leaving it at the base
   8px -- since `.exp-results`/`.exp-tree` each only carry ONE of those two
   real classes, each inherits a DIFFERENT, unopposed padding-left here
   (1px vs 8px), a real 7px text-position mismatch confirmed with
   `getBoundingClientRect()` on a real export switching tabs. */
.exp-tree > tbody > tr > td:nth-child(1), .exp-tree > thead > tr > th:nth-child(1) { width: 7%; padding-left: 6px; }
.exp-tree > tbody > tr > td:nth-child(2), .exp-tree > thead > tr > th:nth-child(2) { width: 7%; padding-left: 1px; padding-right: 6px; }
/* text-align: left explicitly -- real, reported bug: the embedded live
   CSS's own .tree-table td:nth-child(3) rule centers that column, because
   in the LIVE app it's the star/lightning-icons column (this export has no
   such column at all -- no backend to act against, per this file's own
   docstring) -- Path just happens to land on the same nth-child(3) index
   in this table's own, shorter column set, and silently inherited that
   unrelated rule's centering instead of the left-align every other column
   already has. */
/* Path narrowed 35% (340px -> 221px), 2026-09-24 explicit request -- Note
   (nth-child(4) below) has no width of its own, so in this fixed-layout
   table it auto-fills whatever Path gives up: the same 35% moves straight
   over to it without needing its own rule. */
/* 221px -> 410px (2026-09-26, same request/ratio as the live app's own
   .tree-path-cell -- see that rule's own comment): Path had far more empty
   space than its content used, Note (unsized, right after it) started too
   close to it. */
.exp-tree > tbody > tr > td:nth-child(3), .exp-tree > thead > tr > th:nth-child(3) { width: 410px; padding-left: 6px; text-align: left; }
.exp-tree > tbody > tr > td:nth-child(4), .exp-tree > thead > tr > th:nth-child(4) { padding-left: 8px; }
/* Real, reported bug (2026-09-24): leaving Path (nth-child(3)) with NO
   width rule here at all, on the assumption it would just get "whatever's
   left" the way an unsized column does in .exp-results/.exp-tree, was
   wrong for THIS table specifically -- this <table> also carries the
   live app's own real `jobs-table` class (for its other, non-layout
   styling), and the embedded live CSS's own real
   ".jobs-table > tbody > tr > td:nth-child(3)" rule (the live app's own
   Actions/Stop-button column, a real, different column that happens to
   sit at this same position in the LIVE app's 10-column layout) has equal
   specificity and nothing here to lose to -- confirmed empirically: Path
   rendered at a hardcoded 150px regardless of actual available space
   (that rule's own value), and instead of the space taken away from the
   live app's actions/delete columns, that mismatched fixed sum. Fixed
   with EXPLICIT percentage widths on every single column (2026-09-24,
   explicit request), never an implicit/unsized one -- every position now
   has its OWN declared value here, so nothing from the embedded rule ever
   has a gap to win by default. Percentages (of this export's own real 7
   columns, summing to 100%, Path getting the visually largest, most
   useful share) rather than px, so they resolve against this table's own
   real 100%-of-container width instead of racing the live app's px values
   at the same nth-child positions the way the previous, partial px-based
   attempt did. */
.exp-jobs > tbody > tr > td:nth-child(1), .exp-jobs > thead > tr > th:nth-child(1) { width: 12.2%; }
.exp-jobs > tbody > tr > td:nth-child(2), .exp-jobs > thead > tr > th:nth-child(2) { width: 3.4%; text-align: center; }
.exp-jobs > tbody > tr > td:nth-child(3), .exp-jobs > thead > tr > th:nth-child(3) { width: 41.3%; overflow-wrap: anywhere; word-break: break-word; }
.exp-jobs > tbody > tr > td:nth-child(4), .exp-jobs > thead > tr > th:nth-child(4) { width: 12.2%; }
.exp-jobs > tbody > tr > td:nth-child(5), .exp-jobs > thead > tr > th:nth-child(5) { width: 6.6%; text-align: center; }
/* Rate/Errors (2026-09-24) took over Date's old slot -- Date itself moved
   into the fuzz-options detail panel below, matching the live app's own
   JOBS table restructure (frontend/app.js's own comment above
   formatRateCellText has the full story). */
.exp-jobs > tbody > tr > td:nth-child(6), .exp-jobs > thead > tr > th:nth-child(6) { width: 11.2%; white-space: nowrap; }
.exp-jobs > tbody > tr > td:nth-child(7), .exp-jobs > thead > tr > th:nth-child(7) { width: 5.6%; }
.exp-jobs > tbody > tr > td:nth-child(8), .exp-jobs > thead > tr > th:nth-child(8) { width: 7.5%; }
/* .fuzz-history-table(.no-status) itself needs no override here at all --
   it's a real, shared live class, already correctly styled by the embedded
   live CSS above (frontend/styles.css, verbatim). A duplicate, stale copy
   of its column widths used to live here too, silently fighting the real
   one (and drifting further out of sync every time the live one changed,
   e.g. its 2026-09-24 Date-column addition) since both applied to the exact
   same selector at equal specificity -- removed outright, not re-copied. */
</style>
</head>
<body>
<header>
  <img class="app-logo" src="data:image/png;base64,__LOGO_B64__" alt="">
  <h1>OptiFuzz</h1>
  <span class="breadcrumb" id="export-breadcrumb"></span>
  <div class="header-spacer"></div>
  <span class="app-version" id="export-meta"></span>
</header>
<div id="scroll-area">
<main id="app">
  <section id="targets-list-view">
    <div class="section-header-row"><h2>Targets</h2></div>
    <div class="toolbar">
      <div class="search-field"><input type="search" id="targets-search" class="search-field-input" placeholder="Search targets (name or URL)…"></div>
    </div>
    <div class="card-grid" id="targets-grid"></div>
  </section>
  <div id="target-workspace" class="hidden">
  <div class="workspace-tabs">
    <a href="#" class="workspace-tab" data-tab="jobs">JOBS</a>
    <a href="#" class="workspace-tab" data-tab="results">RESULTS</a>
    <a href="#" class="workspace-tab active" data-tab="tree">TREE</a>
  </div>
  <section class="tab-panel hidden" id="panel-jobs">
    <table class="jobs-table exp-table exp-jobs">
      <thead><tr>
        <th>Status</th><th></th><th>Path</th><th>Progress</th>
        <th class="jobs-col-center">Results</th><th>Rate</th><th class="jobs-col-center">Errors</th><th>Elapsed</th>
      </tr></thead>
      <tbody id="jobs-body"></tbody>
    </table>
  </section>
  <section class="tab-panel hidden" id="panel-results">
    <div class="toolbar">
      <div class="search-field"><input type="search" id="results-search" class="search-field-input" placeholder="Search path…"></div>
      <div class="filters-dropdown">
        <button type="button" class="filter-chip" id="results-filters-btn">Filters ▾</button>
        <div class="filters-popup" id="results-filters-popup" hidden>
          <button type="button" class="filter-chip" id="results-fuzzed-only">Fuzzed</button>
          <label class="filter-status-row"><span>Status:</span><input class="filter-status-input" placeholder="e.g. 200,301" title="Only show these status codes (comma-separated)" id="results-status-input"></label>
        </div>
      </div>
      <div class="filters-dropdown">
        <button type="button" class="filter-chip" id="results-hide-btn">Hide rules ▾</button>
        <div class="filters-popup hide-rules-popup" id="results-hide-popup" hidden>
          <form class="filter-add-form" id="results-hide-form">
            <input placeholder="Status" title="e.g. 404" id="results-hide-status">
            <input placeholder="Size" title="e.g. 3KB" id="results-hide-size">
            <input class="filter-parent-input" placeholder="Parent path" title="e.g. /admin/" id="results-hide-parent">
            <input type="number" min="0" class="filter-depth-input" placeholder="1" id="results-hide-depth">
            <button type="submit">+ Hide</button>
          </form>
          <div class="hide-rules-chips" id="results-hide-chips"></div>
        </div>
      </div>
    </div>
    <table class="results-table exp-table exp-results">
      <thead><tr><th>Status</th><th>Size</th><th>Path</th><th>Redirect</th></tr></thead>
      <tbody id="results-body"></tbody>
    </table>
  </section>
  <section class="tab-panel" id="panel-tree">
    <div class="toolbar">
      <div class="search-field"><input type="search" id="tree-search" class="search-field-input" placeholder="Search path…"></div>
      <div class="filters-dropdown">
        <button type="button" class="filter-chip" id="tree-filters-btn">Filters ▾</button>
        <div class="filters-popup" id="tree-filters-popup" hidden>
          <button type="button" class="filter-chip icon-text-btn" id="tree-interesting-only"><span class="btn-icon"><svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><polygon points="8 2.3 9.6 5.9 13.5 6.4 10.6 9.1 11.3 13 8 11.1 4.7 13 5.4 9.1 2.5 6.4 6.4 5.9 8 2.3"/></svg></span>Interesting</button>
          <button type="button" class="filter-chip" id="tree-fuzzed-only">Fuzzed</button>
        </div>
      </div>
    </div>
    <table class="tree-table exp-table exp-tree">
      <thead><tr><th>Status</th><th>Size</th><th>Path</th><th>Note</th></tr></thead>
      <tbody id="tree-body"></tbody>
    </table>
  </section>
  </div>
</main>
</div>
<script type="application/json" id="export-data">__PROJECT_DATA_JSON__</script>
<script>
"use strict";
var PROJECT_DATA = JSON.parse(document.getElementById("export-data").textContent);
var DATA = null;
var scrollAreaEl = document.getElementById("scroll-area");

function h(tag, attrs, children) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    for (var k in attrs) {
        if (k === "class") el.className = attrs[k];
        else if (k === "text") el.textContent = attrs[k];
        else el.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) el.appendChild(c); });
    return el;
}

// Same icon set as the live app (frontend/app.js's own STANDING RULE:
// every icon is one of these named inline SVGs, never a raw emoji) --
// only the ones actually used read-only here.
var STAR_POINTS = "8 2.3 9.6 5.9 13.5 6.4 10.6 9.1 11.3 13 8 11.1 4.7 13 5.4 9.1 2.5 6.4 6.4 5.9 8 2.3";
var STAR_FILLED_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linejoin="round"><polygon points="' + STAR_POINTS + '"/></svg>';
var INFO_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="8" cy="8" r="6.3"/><line x1="8" y1="7.3" x2="8" y2="11.3"/><circle cx="8" cy="4.9" r="0.15" fill="currentColor"/></svg>';

function statusClass(status) {
    if (status == null) return "hint";
    if (status === 401 || status === 403 || status === 429) return "status status-" + status;
    var family = Math.floor(status / 100);
    return family >= 1 && family <= 5 ? "status status-" + family : "status status-other";
}
function wordlistBaseName(path) {
    if (!path) return "?";
    var parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
}
// Ported from the live app's own positionFloatingBox (frontend/app.js) --
// same algorithm, same behavior: flips to whichever side actually has room,
// caps height to that side's available space.
function positionFloatingBox(anchor, box, maxWidth) {
    var margin = 8;
    var rect = anchor.getBoundingClientRect();
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var boxMaxWidth = Math.min(maxWidth, vw - margin * 2);
    box.style.maxWidth = boxMaxWidth + "px";
    var left = rect.left;
    if (left + boxMaxWidth > vw - margin) left = Math.max(margin, vw - margin - boxMaxWidth);
    var spaceBelow = vh - rect.bottom - margin;
    var spaceAbove = rect.top - margin;
    var top, maxHeight;
    if (spaceBelow >= 80 || spaceBelow >= spaceAbove) {
        top = rect.bottom + 4;
        maxHeight = Math.max(60, spaceBelow - 4);
    } else {
        maxHeight = Math.max(60, spaceAbove - 4);
        top = Math.max(margin, rect.top - 4 - maxHeight);
    }
    box.style.left = left + "px";
    box.style.top = top + "px";
    box.style.maxHeight = maxHeight + "px";
}
// Ported from the live app's own buildHeaderNameChip: a header shows only
// its NAME by default (real, reported bug this fixes: the export used to
// dump every "Name: value" pair in plain, always-visible text, unlike the
// live app's name-only chip), hover reveals "Name: value" in a floating
// box, click copies the same text to the clipboard.
function buildHeaderNameChip(name, value, fullText) {
    if (fullText === undefined) fullText = name + ": " + value;
    var chip = h("span", { class: "fuzz-history-header-chip", text: name });
    var hoverBox = h("div", { class: "fuzz-history-header-hover", text: fullText });
    chip.appendChild(hoverBox);
    chip.addEventListener("mouseenter", function () {
        hoverBox.style.display = "block";
        positionFloatingBox(chip, hoverBox, 420);
    });
    chip.addEventListener("mouseleave", function () { hoverBox.style.display = "none"; });
    var copyResetTimer = null;
    chip.addEventListener("click", function () {
        function afterCopy() {
            var label = chip.firstChild;
            // Same fix as the live app's own click handler: restore to
            // `name` itself (never re-read from the label, which would be
            // "Copied!" on a second click before the first timer fired) and
            // clear any still-pending timer before scheduling a new one.
            if (copyResetTimer !== null) clearTimeout(copyResetTimer);
            label.textContent = "Copied!";
            copyResetTimer = setTimeout(function () { label.textContent = name; copyResetTimer = null; }, 1000);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(fullText).then(afterCopy, function () {});
        }
    });
    return chip;
}
// Same format/columns as the live app's own formatJobDate.
function formatJobDate(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    function pad(n) { return String(n).padStart(2, "0"); }
    return pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + "/" + d.getFullYear() + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function formatElapsedMs(ms) {
    if (ms == null) return "—";
    var totalSeconds = Math.floor(ms / 1000);
    function pad(n) { return String(n).padStart(2, "0"); }
    return pad(Math.floor(totalSeconds / 3600)) + ":" + pad(Math.floor((totalSeconds % 3600) / 60)) + ":" + pad(totalSeconds % 60);
}
// Elapsed/progress are both computed from the job's own static fields
// (started_at/ended_at, completed_lines/completed_requests) -- a fixed
// final value, not a live per-second ticker (nothing to tick against in a
// static snapshot).
function computeElapsedMs(job) {
    if (!job.started_at) return null;
    var end = job.ended_at ? new Date(job.ended_at).getTime() : new Date(DATA.generated_at).getTime();
    return Math.max(0, end - new Date(job.started_at).getTime());
}
function formatProgress(job) {
    var total = job.total_wordlist_requests || job.total_wordlist_lines;
    if (!total) return { pct: "0%", detail: "" };
    var done = job.status === "COMPLETED" ? total : Math.min(job.completed_requests, total);
    var pct = Math.round((done / total) * 100);
    return { pct: pct + "%", detail: " (" + done + "/" + total + ")" };
}
function renderProgressCell(cell, progress) {
    cell.appendChild(document.createTextNode(progress.pct));
    cell.appendChild(h("span", { class: "jobs-progress-detail", text: progress.detail }));
}
// Ported from the live app's own formatRateCellText/formatErrorsCellText
// (frontend/app.js) -- a static export has no live SSE ticks at all, so
// RUNNING here always means "captured mid-run in this snapshot", same "—"
// treatment the live app gives a RUNNING job with no live tick yet.
function formatRateCellText(job) {
    if (job.status === "RUNNING") return "—";
    var elapsedMs = computeElapsedMs(job);
    if (!elapsedMs || !job.completed_requests) return "—";
    var avg = job.completed_requests / (elapsedMs / 1000);
    return "Avg. Rate: " + avg.toFixed(1) + "/s";
}
function formatErrorsCellText(job) {
    return String(job.errors || 0);
}

document.getElementById("export-meta").textContent = "Exported at: " + formatJobDate(PROJECT_DATA.generated_at);
var breadcrumbEl = document.getElementById("export-breadcrumb");
// Plain project name at the Targets list (nothing above it to link to in a
// single-project export); once a target is open, the project name becomes
// a link back to that list -- explicit request, matching the live app's
// own breadcrumb ("Projects" link / ProjectName link / Target • tab).
function renderBreadcrumb() {
    breadcrumbEl.innerHTML = "";
    if (!DATA) {
        breadcrumbEl.textContent = PROJECT_DATA.project.name;
        return;
    }
    var projectLink = h("a", { href: "#", text: PROJECT_DATA.project.name });
    projectLink.addEventListener("click", function (e) {
        e.preventDefault();
        showTargetsList();
    });
    breadcrumbEl.appendChild(projectLink);
    breadcrumbEl.append(" / " + DATA.target.name + " (" + DATA.target.base_url + ")");
}

// ---- Tabs: same JOBS/RESULTS/TREE navigation as the live app's own
// .workspace-tabs/.workspace-tab. Nothing here ever launches/pauses/stops
// a fuzz -- there's no backend to do that against -- read-only navigation
// and client-side filtering only.
function switchToExportTab(tabName) {
    Array.prototype.forEach.call(document.querySelectorAll(".workspace-tab"), function (b) {
        b.classList.toggle("active", b.dataset.tab === tabName);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab-panel"), function (panel) {
        panel.classList.toggle("hidden", panel.id !== "panel-" + tabName);
    });
}
Array.prototype.forEach.call(document.querySelectorAll(".workspace-tab"), function (btn) {
    btn.addEventListener("click", function (e) {
        e.preventDefault();
        switchToExportTab(btn.dataset.tab);
    });
});
// Generic "Filters ▾" dropdown open/close, shared by Results/TREE -- click
// the toggle button, click anywhere else closes it. The real CSS's own
// `.filters-popup:not([hidden]) { display: flex; }` (styles.css, embedded
// verbatim above) keys off the actual HTML `hidden` ATTRIBUTE/DOM property
// (a higher-specificity rule than any plain `.hidden { display: none }`
// class could ever win against) -- toggling `.hidden` as a property here,
// exactly like the live app's own `popup.hidden = ...`, not a CSS class.
Array.prototype.forEach.call(document.querySelectorAll(".filters-dropdown"), function (dropdown) {
    var btn = dropdown.querySelector(".filter-chip");
    var popup = dropdown.querySelector(".filters-popup");
    btn.addEventListener("click", function (e) {
        e.stopPropagation();
        popup.hidden = !popup.hidden;
    });
});
document.addEventListener("click", function (e) {
    if (e.target.closest(".filters-dropdown")) return;
    Array.prototype.forEach.call(document.querySelectorAll(".filters-popup"), function (p) { p.hidden = true; });
});

// ---- Fuzz Tree / Results: same normalize/hierarchy/connector logic as
// the live app (frontend/app.js's normalizeKey/computeDisplaySegment/
// treeConnectorFor), operating on the embedded JSON instead of a live
// API -- must stay a real interactive tree here too, not
// flattened into a list.
function normalizeKey(path) {
    var k = path === "/" ? "/" : path.replace(/\/+$/, "");
    return k === "" ? "/" : k;
}
function parentKeyOf(key) {
    if (key === "/") return null;
    var idx = key.lastIndexOf("/");
    return idx <= 0 ? "/" : key.slice(0, idx);
}
function computeDisplaySegment(rawPath, parentKey, isDirectory) {
    var fullKey = normalizeKey(rawPath);
    var segment;
    if (parentKey !== null) {
        var prefix = parentKey === "/" ? "/" : parentKey + "/";
        segment = fullKey.indexOf(prefix) === 0 ? fullKey.slice(prefix.length) : fullKey.replace(/^\/+/, "");
    } else {
        segment = fullKey === "/" ? "" : fullKey.replace(/^\/+/, "");
    }
    if (isDirectory && segment && segment.charAt(segment.length - 1) !== "/") segment += "/";
    return segment || "/";
}
// ---- Right-click "Copy full path" on a clickable path link -- same helper
// as the live app's own attachCopyPathContextMenu, same markup/CSS classes
// (styles.css above is embedded verbatim, so no export-local CSS needed).
var openPathContextMenu = null;
function closePathContextMenu() {
    if (openPathContextMenu) {
        openPathContextMenu.remove();
        openPathContextMenu = null;
    }
}
document.addEventListener("click", closePathContextMenu);
document.addEventListener("scroll", closePathContextMenu, true);
function attachCopyPathContextMenu(element, fullPath) {
    element.addEventListener("contextmenu", function (e) {
        e.preventDefault();
        e.stopPropagation();
        closePathContextMenu();
        var item = h("button", { type: "button", class: "path-context-menu-item", text: "Copy full path" });
        item.addEventListener("click", function (ev) {
            ev.stopPropagation();
            navigator.clipboard.writeText(fullPath).catch(function () { /* denied/unavailable -- fail silently */ });
            closePathContextMenu();
        });
        var menu = h("div", { class: "path-context-menu" }, [item]);
        menu.style.left = e.clientX + "px";
        menu.style.top = e.clientY + "px";
        document.body.appendChild(menu);
        openPathContextMenu = menu;
        var rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = Math.max(8, window.innerWidth - rect.width - 8) + "px";
        if (rect.bottom > window.innerHeight) menu.style.top = Math.max(8, window.innerHeight - rect.height - 8) + "px";
    });
}
function treeConnectorFor(depth, prefix, isLast) {
    return depth === 0 ? "" : prefix + (isLast ? "└── " : "├── ");
}
function treeChildPrefixFor(depth, prefix, isLast) {
    return depth === 0 ? "" : prefix + (isLast ? "    " : "│   ");
}
// Same fix as the live app's own appendConnectorUnits (frontend/app.js,
// see its comment) -- splits the connector string into one span per 4-char
// unit instead of one span for the whole string, so an ancestor's "│" line
// can keep going (.tree-connector-line, CSS embedded verbatim above)
// through a row's own wrapped 2nd/3rd display lines instead of stopping
// after line 1 (docs/identacion.png).
function appendConnectorUnits(pathPrefix, connector) {
    if (!connector) return;
    var units = connector.match(/.{1,4}/g) || [];
    units.forEach(function (unit, i) {
        var continues = i < units.length - 1 && unit.charAt(0) === "│";
        pathPrefix.appendChild(h("span", {
            class: continues ? "tree-connector tree-connector-line" : "tree-connector",
            text: unit,
        }));
    });
}
// Real bug found and fixed here: linking parents in a SEPARATE pass over
// Object.keys(byKey) only ever saw the keys that existed at the moment
// that snapshot was taken -- any synthetic ancestor `ensure()` itself
// created *during* that same pass (e.g. "/api/v1", implied only by a
// real "/api/v1/users" with no "/api/v1" of its own) was never itself
// visited, so IT never got linked to ITS OWN parent ("/api") in turn.
// Any real item more than one synthetic level deep (a genuinely common
// shape -- Results in particular has no explicit "add this intermediate
// segment" step the way TREE does) silently vanished from the rendered
// hierarchy entirely, orphaned under an unreachable ancestor. Fixed by
// linking a node to its parent recursively, AT THE MOMENT it's first
// created, walking the full chain back to root immediately instead of a
// second pass over a stale snapshot.
// Real, reported data-loss bug fixed here (2026-09-25, mirroring the same
// fix in the live app's own buildPathHierarchy, frontend/app.js): two raw
// items can normalize to the same key -- the classic dirsearch redirect
// pair, "/admin" (301 stub) and "/admin/" (the real 200 hit), are separate,
// genuinely distinct results. This used to overwrite `node.item` with
// whichever item was seen LAST, silently discarding the other -- "la tool
// necesita mostrar TODOS los resultados". `node.items` (plural, every
// distinct item at that key, in encounter order) replaces it; both row
// builders below render one row per item sharing a position instead of
// picking a single winner.
function buildHierarchy(items) {
    var byKey = {};
    function ensure(key) {
        if (byKey[key]) return byKey[key];
        var node = { key: key, items: [], children: [] };
        byKey[key] = node;
        if (key !== "/") ensure(parentKeyOf(key)).children.push(node);
        return node;
    }
    ensure("/");
    items.forEach(function (item) { ensure(normalizeKey(item.path)).items.push(item); });
    Object.keys(byKey).forEach(function (key) {
        byKey[key].children.sort(function (a, b) { return a.key < b.key ? -1 : a.key > b.key ? 1 : 0; });
    });
    return byKey["/"];
}

// ---- Fuzz history: the exact same read-only .fuzz-history-table the live
// app uses (TREE/Results' own "Fuzzed" tag toggle, and a job's own history)
// -- same classes, same columns (minus Status for a single-job case, see
// the .no-status modifier above), so it looks and behaves identically.
function buildFuzzHistoryTable(history, hideStatus, path) {
    if (!history || !history.length) {
        return h("div", { class: "hint", text: "Never fuzzed directly." });
    }
    var headCells = [];
    if (!hideStatus) headCells.push(h("th", { text: "Status" }));
    headCells.push(
        h("th", { text: "Wordlist" }), h("th", { text: "Extensions" }), h("th", { text: "Headers" }), h("th", { text: "Options" }),
    );
    // Never shown in JOBS' own single-job panel (hideStatus) -- same as the
    // live app, it overflowed that narrower panel; the main JOBS row's own
    // Results cell is the one to look at there instead.
    if (!hideStatus) headCells.push(h("th", { text: "Results", class: "jobs-col-center" }));
    // Date now shows in every one of these panels (2026-09-24, explicit
    // request) -- every entry here (JOBS', TREE's, and Results' own, all
    // ultimately from tree_service.fuzz_history's own dict shape server-side)
    // always carries created_at, so this is effectively always true in
    // practice; the .some() guard is just defensive.
    var showDate = history.some(function (entry) { return entry.date || entry.created_at; });
    if (showDate) headCells.push(h("th", { text: "Date" }));
    var anyQuery = false;
    var rows = history.map(function (entry) {
        var headerEntries = Object.entries(entry.config && entry.config.headers || {});
        var headersCell = h("td", {});
        // A query string in the job's URL is not a header: shown first as
        // "query*" (hover = the full query), explained by the footnote below.
        var queryText = String(entry.path || (entry.config && entry.config.url) || "").split("#")[0];
        var queryAt = queryText.indexOf("?");
        var query = queryAt >= 0 && queryAt < queryText.length - 1 ? queryText.slice(queryAt) : "";
        if (query) {
            headersCell.appendChild(buildHeaderNameChip("query*", query, query));
            anyQuery = true;
        }
        headerEntries.forEach(function (pair, idx) {
            if (idx > 0 || query) headersCell.appendChild(document.createTextNode(", "));
            headersCell.appendChild(buildHeaderNameChip(pair[0], String(pair[1])));
        });
        if (!headerEntries.length && !query) headersCell.appendChild(document.createTextNode("—"));
        var other = [];
        var cfg = entry.config || {};
        // Same explicit order as the live app: threads, force-ext, AUTH, auto
        // pause, then whatever else was actually set.
        if (cfg.threads) other.push("threads " + cfg.threads);
        if (cfg.force_extensions) other.push("force-ext");
        if (entry.output_stem && /__AUTH$/.test(entry.output_stem)) other.push("AUTH");
        if (entry.pause_on_status && entry.pause_on_status.length) other.push("auto pause " + entry.pause_on_status.join(","));
        if (cfg.http_method && cfg.http_method !== "GET") other.push(cfg.http_method);
        if (cfg.proxy) other.push("proxy " + cfg.proxy);
        if (cfg.disabled_default_headers && cfg.disabled_default_headers.length) other.push("no " + cfg.disabled_default_headers.join(", "));
        if (cfg.extra_args && cfg.extra_args.length) other.push(String(cfg.extra_args));
        var cells = [];
        if (!hideStatus) cells.push(h("td", {}, [h("span", { class: "job-status job-" + entry.status, text: entry.status })]));
        cells.push(
            h("td", { text: (entry.wordlist_sources || []).map(wordlistBaseName).join(", ") || "?" }),
            h("td", { text: (entry.extensions || []).join(",") || "—" }),
            headersCell,
            h("td", { text: other.join(" · ") || "—" }),
        );
        if (!hideStatus) {
            var resultsCell = h("td", { class: "jobs-col-center" });
            if (entry.result_count) {
                var resultsLink = h("button", {
                    type: "button", class: "results-count-link", title: "Go to these results",
                    text: String(entry.result_count),
                });
                resultsLink.addEventListener("click", function (e) {
                    e.stopPropagation();
                    goToResultsAndReveal(path);
                });
                resultsCell.appendChild(resultsLink);
            } else {
                resultsCell.textContent = entry.result_count == null ? "—" : String(entry.result_count);
            }
            cells.push(resultsCell);
        }
        if (showDate) {
            var dateValue = entry.date || entry.created_at;
            cells.push(h("td", { class: "jobs-date-cell", text: dateValue ? formatJobDate(dateValue) : "—" }));
        }
        return h("tr", {}, cells);
    });
    var sections = [h("thead", {}, [h("tr", {}, headCells)]), h("tbody", {}, rows)];
    if (anyQuery) {
        sections.push(h("tfoot", {}, [h("tr", {}, [
            h("td", { class: "fuzz-history-note", colspan: String(headCells.length), text: "* query = query string appended to every request (not a header)" }),
        ])]));
    }
    return h("table", { class: "fuzz-history-table" + (hideStatus ? " no-status" : "") }, sections);
}
function buildFuzzHistoryRow(colspan, history, hideStatus, path) {
    return h("tr", { class: "fuzz-history-row hidden" }, [
        h("td", { colspan: String(colspan) }, [h("div", { class: "fuzz-history-panel" }, [buildFuzzHistoryTable(history, hideStatus, path)])]),
    ]);
}
function buildFuzzedTagButton(detail, onToggle) {
    var btn = h("button", {
        type: "button", class: "fuzzed-tag fuzz-history-toggle", title: "Already fuzzed — click for history",
        text: detail ? "Fuzzed: " + detail : "Fuzzed",
    });
    // .active toggled here, self-contained -- matches the live app's own
    // ".fuzzed-tag.fuzz-history-toggle.active" color change while its
    // detail panel is open (frontend/app.js re-renders the whole row with
    // an isOpen flag to get the same effect; this static export has no
    // equivalent re-render step, so the class is flipped directly on click
    // instead). The embedded live CSS above already has the matching rule,
    // no export-local style needed.
    btn.addEventListener("click", function (e) { e.stopPropagation(); btn.classList.toggle("active"); onToggle(btn); });
    return btn;
}
function formatFuzzedTag(history) {
    if (!history || !history.length) return null;
    // Same as the live app's own formatFuzzedTag -- every distinct
    // wordlist(+extensions) combo this path was fuzzed with, not just the
    // latest.
    var seen = {};
    var parts = [];
    history.forEach(function (entry) {
        var wordlists = (entry.wordlist_sources || []).map(wordlistBaseName).join(", ") || "?";
        var exts = (entry.extensions || []).join(",");
        var part = exts ? wordlists + " (" + exts + ")" : wordlists;
        if (!seen[part]) {
            seen[part] = true;
            parts.push(part);
        }
    });
    return parts.join(", ");
}

// ---- Fuzz Tree: TREE's own row builder -- every node fully EXPANDED by
// default (explicit request), independent toggles for children (arrow) vs
// the "Fuzzed" tag's own history detail (unlike Results, TREE always shows
// its note text inline too, never behind a toggle).
function appendTreeRows(tbody, node, depth, parentKey, prefix, isLast, forceVisible) {
    var childCount = node.children.length;
    // Every distinct item at this position gets its own row (2026-09-25 --
    // see buildHierarchy's own comment); an empty `items` (a synthetic
    // ancestor -- fuzzing straight at /a/b/c never independently discovers
    // /a or /a/b) still renders exactly one placeholder row, same as
    // before. Only the LAST row in the group carries the expand
    // arrow/children -- they render (and collapse) exactly once per
    // position, not once per item sharing it.
    var itemsToRender = node.items.length ? node.items : [null];
    var handles = itemsToRender.map(function (item, idx) {
        var isLastItem = idx === itemsToRender.length - 1;
        // Only the bottom item of a shared position closes the branch -- see the live app's own note in appendHierarchyRows.
        var connector = treeConnectorFor(depth, prefix, isLastItem ? isLast : false);
        var thisChildCount = isLastItem ? childCount : 0;
        var isDirectory = thisChildCount > 0 || (item && item.path && item.path.charAt(item.path.length - 1) === "/");
        var displaySegment = computeDisplaySegment(item ? item.path : node.key, parentKey, isDirectory);
        var statusInfo = item && item.status ? item.status.status : null;

        // Two flex siblings, same as the live app's own buildNodeRow (see its
        // comment) -- a path long enough to wrap onto a 2nd line stays
        // indented under its own content instead of cutting back across the
        // connector/tree-lines below it.
        var pathCell = h("td", { class: "tree-path-cell" });
        var pathCellInner = h("div", { class: "tree-path-cell-inner" });
        pathCell.appendChild(pathCellInner);
        var pathPrefix = h("span", { class: "tree-path-prefix" });
        pathCellInner.appendChild(pathPrefix);
        appendConnectorUnits(pathPrefix, connector);
        var expandBtn = null;
        if (thisChildCount > 0) {
            expandBtn = h("button", { type: "button", class: "result-expand", title: "Expand/collapse", text: "▾" });
            pathPrefix.appendChild(expandBtn);
        }
        var pathContent = h("span", { class: "tree-path-content" });
        pathCellInner.appendChild(pathContent);
        // Clickable like Results/JOBS already are in this export (explicit
        // request -- TREE paths were still plain, non-clickable text here).
        var treeHref = "#";
        try { treeHref = new URL(item ? item.path : node.key, DATA.target.base_url).href; } catch (e) { /* malformed -- fall back to "#" */ }
        var treeLink = h("a", { class: "result-path-link", href: treeHref, target: "_blank", rel: "noopener", text: displaySegment });
        attachCopyPathContextMenu(treeLink, item ? item.path : node.key);
        pathContent.appendChild(treeLink);
        if (item && item.interesting) {
            var star = h("span", { title: "Marked interesting" });
            star.innerHTML = STAR_FILLED_ICON_SVG;
            star.style.color = "var(--yellow)";
            star.style.marginLeft = "4px";
            star.style.verticalAlign = "middle";
            pathContent.appendChild(star);
        }

        var history = item ? (item.fuzz_history || []) : [];
        var fuzzedTagText = formatFuzzedTag(history);
        var noteInner = h("div", { class: "tree-note-cell-inner" });
        var detailRow = null;
        if (fuzzedTagText) {
            detailRow = buildFuzzHistoryRow(4, history, false, item ? item.path : node.key);
            noteInner.appendChild(buildFuzzedTagButton(fuzzedTagText, function () { detailRow.classList.toggle("hidden"); }));
        }
        if (item && item.note) noteInner.appendChild(h("span", { class: "export-note", text: item.note }));
        var noteCell = h("td", {}, [noteInner]);

        var row = h("tr", {}, [
            h("td", { class: statusClass(statusInfo), text: statusInfo != null ? String(statusInfo) : "—" }),
            h("td", { text: (item && item.status && item.status.size) || "" }),
            pathCell,
            noteCell,
        ]);
        tbody.appendChild(row);
        if (detailRow) tbody.appendChild(detailRow);
        if (!forceVisible) row.classList.add("hidden"); // never true for TREE's own default -- see renderTree()

        return { row: row, detailRow: detailRow, expandBtn: expandBtn, childHandles: [] };
    });

    var lastHandle = handles[handles.length - 1];
    var childHandles = [];
    var childPrefix = treeChildPrefixFor(depth, prefix, isLast);
    node.children.forEach(function (child, i) {
        childHandles = childHandles.concat(
            appendTreeRows(tbody, child, depth + 1, node.key, childPrefix, i === node.children.length - 1, forceVisible)
        );
    });
    lastHandle.childHandles = childHandles;

    function setVisible(hs, visible) {
        hs.forEach(function (handle) {
            handle.row.classList.toggle("hidden", !visible);
            if (!visible && handle.detailRow) handle.detailRow.classList.add("hidden");
            if (handle.expandBtn) handle.expandBtn.textContent = visible ? "▾" : "▸";
            setVisible(handle.childHandles, visible);
        });
    }
    if (lastHandle.expandBtn) {
        lastHandle.expandBtn.textContent = "▾"; // TREE starts fully expanded (explicit request)
        lastHandle.expandBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            var expanding = lastHandle.expandBtn.textContent !== "▾";
            lastHandle.expandBtn.textContent = expanding ? "▾" : "▸";
            setVisible(childHandles, expanding);
        });
    }

    return handles;
}

var treeBody = document.getElementById("tree-body");
var treeSearchInput = document.getElementById("tree-search");
var treeInterestingBtn = document.getElementById("tree-interesting-only");
var treeFuzzedBtn = document.getElementById("tree-fuzzed-only");

function passesTreeFilters(item) {
    var query = treeSearchInput.value.trim().toLowerCase();
    if (query && item.path.toLowerCase().indexOf(query) === -1) return false;
    if (treeInterestingBtn.classList.contains("active") && !item.interesting) return false;
    if (treeFuzzedBtn.classList.contains("active") && !(item.fuzz_history && item.fuzz_history.length)) return false;
    return true;
}
function renderTree() {
    treeBody.innerHTML = "";
    if (!DATA.tree.length) {
        treeBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No paths in the tree." })]));
        return;
    }
    var filtered = DATA.tree.filter(passesTreeFilters);
    if (!filtered.length) {
        treeBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No paths match your filters." })]));
        return;
    }
    // TREE keeps its own root ("/" is a real, always-present node, unlike
    // Results below) and is always fully expanded (explicit request) --
    // search/filters never collapse anything here, there's nothing to
    // revert on clear.
    appendTreeRows(treeBody, buildHierarchy(filtered), 0, null, "", true, true);
}
treeSearchInput.addEventListener("input", renderTree);
[treeInterestingBtn, treeFuzzedBtn].forEach(function (btn) {
    btn.addEventListener("click", function () { btn.classList.toggle("active"); renderTree(); });
});

// ---- Results: same hierarchy/connector rendering as Tree, but its own 4
// columns (Status/Size/Path/Redirect -- no note/interesting, TREE-only
// concepts), collapsed by default (unlike TREE), its own root NEVER shown
// as its own row (explicit request -- only real results matter here), and
// full Hide rules support (status/size/parent-path/depth), same as live.
var fuzzedPaths = {};
function fuzzHistoryForPath(path) {
    return DATA.jobs.filter(function (j) { return String(j.path).split(/[?#]/)[0] === path; }).map(function (j) {
        return {
            job_id: j.id, status: j.status, wordlist_sources: j.wordlist_sources,
            extensions: (j.config && j.config.extensions) || [], config: j.config || {},
            created_at: j.created_at, started_at: j.started_at,
        };
    });
}

// Seeded per target from the hide rules/filters saved in the live app
// (t.view, see loadTarget) -- still fully editable from here on.
var resultsHideFilters = [];
function matchesHideFilter(result, f) {
    if (f.status && String(result.status) !== String(f.status)) return false;
    if (f.size && (result.size || "").trim().toLowerCase() !== f.size.trim().toLowerCase()) return false;
    if (f.parentPath) {
        var parentKey = normalizeKey(f.parentPath);
        var resultKey = normalizeKey(result.path);
        var prefix = parentKey === "/" ? "/" : parentKey + "/";
        if (resultKey.indexOf(prefix) !== 0 || resultKey === parentKey) return false;
        var depth = f.depth === undefined ? 0 : f.depth;
        if (depth > 0) {
            var segments = resultKey.slice(prefix.length).split("/").length;
            if (segments > depth) return false;
        }
    }
    return true;
}

var resultsBody = document.getElementById("results-body");
var resultsSearchInput = document.getElementById("results-search");
var resultsFuzzedBtn = document.getElementById("results-fuzzed-only");
var resultsStatusInput = document.getElementById("results-status-input");
var resultsStatusSet = [];
// Shared by the status input AND the Fuzzed toggle -- each status code counts
// on its own ("200,302" = 2, not 1); the live app does the same.
function updateResultsFiltersCount() {
    var n = (resultsFuzzedBtn.classList.contains("active") ? 1 : 0) + resultsStatusSet.length;
    document.getElementById("results-filters-btn").textContent = "Filters ▾" + (n ? " (" + n + ")" : "");
    document.getElementById("results-filters-btn").classList.toggle("active", n > 0);
}
resultsStatusInput.addEventListener("input", function () {
    resultsStatusSet = resultsStatusInput.value.split(",").map(function (s) { return s.trim(); }).filter(function (s) { return /^\d+$/.test(s); }).map(Number);
    resultsStatusInput.classList.toggle("active", resultsStatusInput.value.trim().length > 0);
    updateResultsFiltersCount();
    renderResults();
});

function passesResultsFilters(r) {
    var query = resultsSearchInput.value.trim().toLowerCase();
    if (query && r.path.toLowerCase().indexOf(query) === -1) return false;
    if (resultsFuzzedBtn.classList.contains("active") && !fuzzedPaths[r.path]) return false;
    if (resultsStatusSet.length && resultsStatusSet.indexOf(r.status) === -1) return false;
    if (resultsHideFilters.some(function (f) { return matchesHideFilter(r, f); })) return false;
    return true;
}

function appendResultRows(tbody, node, depth, parentKey, prefix, isLast, forceVisible) {
    var childCount = node.children.length;
    // Every distinct item at this position gets its own row (2026-09-25 --
    // see buildHierarchy's own comment); an empty `items` (a synthetic
    // ancestor) still renders exactly one placeholder row, same as before.
    // Only the LAST row in the group carries the expand arrow/children.
    var itemsToRender = node.items.length ? node.items : [null];
    var handles = itemsToRender.map(function (item, idx) {
        var isLastItem = idx === itemsToRender.length - 1;
        var connector = treeConnectorFor(depth, prefix, isLastItem ? isLast : false);
        var thisChildCount = isLastItem ? childCount : 0;
        var isDirectory = thisChildCount > 0 || (item && item.path && item.path.charAt(item.path.length - 1) === "/");
        var displaySegment = computeDisplaySegment(item ? item.path : node.key, parentKey, isDirectory);

        // Two flex siblings, same as the live app's own buildResultRow (see
        // its comment) -- a path long enough to wrap onto a 2nd line stays
        // indented under its own content instead of cutting back across the
        // connector/tree-lines below it.
        var pathCell = h("td", {});
        var pathCellInner = h("div", { class: "tree-path-cell-inner" });
        pathCell.appendChild(pathCellInner);
        var pathPrefix = h("span", { class: "tree-path-prefix" });
        pathCellInner.appendChild(pathPrefix);
        appendConnectorUnits(pathPrefix, connector);
        var expandBtn = null;
        if (thisChildCount > 0) {
            expandBtn = h("button", {
                type: "button", class: "result-expand", title: "Expand/collapse",
                text: forceVisible ? "▾" : "▸",
            });
            pathPrefix.appendChild(expandBtn);
        }
        var pathContent = h("span", { class: "tree-path-content" });
        pathCellInner.appendChild(pathContent);
        var link = h("a", { class: "result-path-link", href: item ? item.url : "#", target: "_blank", rel: "noopener", text: displaySegment });
        attachCopyPathContextMenu(link, item ? item.path : node.key);
        pathContent.appendChild(link);
        if (thisChildCount > 0) pathContent.appendChild(h("span", { class: "hint", text: " (" + thisChildCount + ")" }));

        var history = item ? fuzzHistoryForPath(item.path) : [];
        var detailRow = null;
        if (history.length) {
            detailRow = buildFuzzHistoryRow(4, history, false, item ? item.path : node.key);
            pathContent.appendChild(buildFuzzedTagButton(null, function () { detailRow.classList.toggle("hidden"); }));
        }

        var redirectCell = h("td", {});
        if (item && item.redirect) {
            var crossOrigin = false;
            var label = item.redirect;
            try {
                var resolved = new URL(item.redirect, item.url);
                var sourceOrigin = new URL(item.url).origin;
                crossOrigin = resolved.origin !== sourceOrigin;
                label = crossOrigin ? resolved.href : resolved.pathname + resolved.search + resolved.hash;
            } catch (e) { /* malformed Location header -- show the raw string as-is */ }
            redirectCell.appendChild(h("a", {
                class: "result-redirect-link" + (crossOrigin ? " redirect-cross-origin" : ""),
                href: item.redirect, target: "_blank", rel: "noopener", text: label,
            }));
        }

        var row = h("tr", {}, [
            h("td", { class: statusClass(item ? item.status : null), text: item ? String(item.status) : "—" }),
            h("td", { text: (item && item.size) || "" }),
            pathCell,
            redirectCell,
        ]);
        tbody.appendChild(row);
        if (detailRow) tbody.appendChild(detailRow);
        // depth > 0 -- a top-level (depth 0) row is always visible regardless
        // of forceVisible/collapse state; only its DESCENDANTS start hidden
        // when collapsed by default. Real bug fixed here: applying this
        // unconditionally hid every row, including top-level ones, leaving
        // Results completely empty whenever it wasn't in "searching" mode.
        if (depth > 0 && !forceVisible) row.classList.add("hidden");

        return { row: row, detailRow: detailRow, expandBtn: expandBtn, childHandles: [] };
    });

    var lastHandle = handles[handles.length - 1];
    // Registered by exact key so goToResultsAndReveal (below) can find this
    // node's own row(s) and children again later, from a click made in a
    // totally different tab/panel -- same lookup the live app's own
    // resultsPlanKeys.indexOf(key) does against its virtualized plan.
    resultNodesByKey[node.key] = { handles: handles, lastHandle: lastHandle };
    var childHandles = [];
    var childPrefix = treeChildPrefixFor(depth, prefix, isLast);
    node.children.forEach(function (child, i) {
        childHandles = childHandles.concat(
            appendResultRows(tbody, child, depth + 1, node.key, childPrefix, i === node.children.length - 1, forceVisible)
        );
    });
    lastHandle.childHandles = childHandles;

    function setVisible(hs, visible) {
        hs.forEach(function (handle) {
            handle.row.classList.toggle("hidden", !visible);
            if (!visible && handle.detailRow) handle.detailRow.classList.add("hidden");
            if (handle.expandBtn) handle.expandBtn.textContent = visible ? "▾" : "▸";
            setVisible(handle.childHandles, visible);
        });
    }
    if (lastHandle.expandBtn) {
        // Real, reported bug: this used to listen on the whole ROW and
        // exclude clicks on any <button> (so the Fuzzed-tag button and the
        // path link wouldn't ALSO toggle expand) -- but the expand arrow
        // itself is also a <button>, so that same guard silently ate its
        // own clicks too, and it could never be expanded at all. Matches
        // TREE's own (already correct) pattern above: listen on the arrow
        // itself, not the row.
        lastHandle.expandBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            var expanding = lastHandle.expandBtn.textContent !== "▾";
            lastHandle.expandBtn.textContent = expanding ? "▾" : "▸";
            setVisible(childHandles, expanding);
        });
    }

    return handles;
}

var resultNodesByKey = {};
function renderResults() {
    resultsBody.innerHTML = "";
    resultNodesByKey = {};
    if (!DATA.results.length) {
        resultsBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No results." })]));
        return;
    }
    var filtered = DATA.results.filter(passesResultsFilters);
    if (!filtered.length) {
        resultsBody.appendChild(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "No results match your filters." })]));
        return;
    }
    // Never the root's own row (explicit request) -- its real children
    // just become top-level rows directly. While a search is active,
    // everything renders fully expanded (so a deep match is actually
    // visible) -- reverts to the normal collapsed-by-default state the
    // instant the search box is cleared, since this is a full rebuild
    // either way, not a remembered per-node toggle.
    var searching = !!resultsSearchInput.value.trim();
    var hierarchy = buildHierarchy(filtered);
    // Root itself is never its own row (see the comment above) -- it still
    // needs an entry in resultNodesByKey, with no row of its own, so
    // goToResultsAndReveal(path) can be given the exact root path ("/", a
    // job fuzzed right at the target's base URL) and still correctly
    // re-collapse its own grandchildren, same contract as every other path.
    var rootTopHandles;
    if (hierarchy.items.length) {
        rootTopHandles = appendResultRows(resultsBody, hierarchy, 0, null, "", true, searching);
    } else {
        rootTopHandles = [];
        hierarchy.children.forEach(function (child, i) {
            rootTopHandles = rootTopHandles.concat(
                appendResultRows(resultsBody, child, 0, null, "", i === hierarchy.children.length - 1, searching)
            );
        });
    }
    resultNodesByKey["/"] = { handles: [], lastHandle: { row: null, expandBtn: null, childHandles: rootTopHandles } };
}
resultsSearchInput.addEventListener("input", renderResults);
resultsFuzzedBtn.addEventListener("click", function () {
    resultsFuzzedBtn.classList.toggle("active");
    updateResultsFiltersCount();
    renderResults();
});

// Hide rules -- same composable status/size/parent-path/depth rules as the
// live app, same add-form + chip-list UI, operating on the embedded JSON.
var hideForm = document.getElementById("results-hide-form");
var hideChips = document.getElementById("results-hide-chips");
var hideBtn = document.getElementById("results-hide-btn");
function renderHideChips() {
    hideChips.innerHTML = "";
    hideBtn.textContent = "Hide rules ▾" + (resultsHideFilters.length ? " (" + resultsHideFilters.length + ")" : "");
    hideBtn.classList.toggle("active", resultsHideFilters.length > 0);
    resultsHideFilters.forEach(function (f) {
        var depthSuffix = !f.parentPath ? null
            : (f.depth === undefined || f.depth === 0) ? "all descendants"
            : "depth " + f.depth; // same as the live app -- depth 1 reads as "depth 1", not "direct children"
        var label = [
            f.status && ("status " + f.status), f.size,
            f.parentPath && (depthSuffix ? f.parentPath + " (" + depthSuffix + ")" : f.parentPath),
        ].filter(Boolean).join(" · ");
        var removeBtn = h("button", { type: "button", text: "✕", title: "Remove filter" });
        removeBtn.addEventListener("click", function () {
            var idx = resultsHideFilters.indexOf(f);
            if (idx !== -1) resultsHideFilters.splice(idx, 1);
            renderHideChips();
            renderResults();
        });
        hideChips.appendChild(h("span", { class: "filter-chip-remove" }, [document.createTextNode(label), removeBtn]));
    });
}
hideForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var statusInput = document.getElementById("results-hide-status");
    var sizeInput = document.getElementById("results-hide-size");
    var parentInput = document.getElementById("results-hide-parent");
    var depthInput = document.getElementById("results-hide-depth");
    var depthRaw = depthInput.value.trim();
    var f = {
        status: statusInput.value.trim(), size: sizeInput.value.trim(), parentPath: parentInput.value.trim(),
        depth: depthRaw ? Number(depthRaw) : 1,
    };
    if (!f.status && !f.size && !f.parentPath) return;
    resultsHideFilters.push(f);
    statusInput.value = ""; sizeInput.value = ""; parentInput.value = ""; depthInput.value = "";
    renderHideChips();
    renderResults();
});
// Fully collapses a subtree (used right below, to force grandchildren
// closed no matter what was expanded/collapsed before).
function collapseSubtree(handles) {
    handles.forEach(function (handle) {
        handle.row.classList.add("hidden");
        if (handle.detailRow) handle.detailRow.classList.add("hidden");
        if (handle.expandBtn) handle.expandBtn.textContent = "▸";
        collapseSubtree(handle.childHandles);
    });
}
// ---- Click a Results count (in the JOBS main table, or any Fuzzed-history
// panel's own Results cell in TREE/Results) -- same explicit request as
// the live app's own goToResultsAndReveal/revealResultsPath: switch to
// RESULTS, expand exactly `path`'s own direct children (grandchildren stay
// collapsed regardless of whatever was expanded before), and scroll so the
// path's own row -- not just its children -- actually ends up visible.
function goToResultsAndReveal(path) {
    switchToExportTab("results");
    var key = normalizeKey(path);
    var chain = [];
    var k = key;
    while (k) { chain.unshift(k); k = parentKeyOf(k); }
    chain.forEach(function (ancKey) {
        var entry = resultNodesByKey[ancKey];
        if (!entry) return;
        entry.handles.forEach(function (handle) { handle.row.classList.remove("hidden"); });
        if (entry.lastHandle.expandBtn) entry.lastHandle.expandBtn.textContent = "▾";
    });
    var targetEntry = resultNodesByKey[key];
    if (!targetEntry) return; // filtered/hidden out of the current view -- nothing to reveal
    targetEntry.lastHandle.childHandles.forEach(function (child) {
        child.row.classList.remove("hidden");
        if (child.expandBtn) child.expandBtn.textContent = "▸";
        collapseSubtree(child.childHandles);
    });
    var targetRow = targetEntry.lastHandle.row;
    if (!targetRow) return; // the root path ("/") has no row of its own -- nothing further to scroll to
    // Wait a frame so the tab switch's own layout/visibility change has
    // actually taken effect before reading getBoundingClientRect -- reading
    // it in the same tick as switchToExportTab can still reflect the
    // now-stale "hidden" layout.
    requestAnimationFrame(function () {
        var tabsBar = document.querySelector(".workspace-tabs");
        var tabsHeight = tabsBar ? tabsBar.offsetHeight : 0;
        var rect = targetRow.getBoundingClientRect();
        var areaRect = scrollAreaEl.getBoundingClientRect();
        var delta = (rect.top - areaRect.top) - tabsHeight - 12;
        scrollAreaEl.scrollTop = Math.max(0, scrollAreaEl.scrollTop + delta);
    });
}

// ---- Jobs: the live JOBS tab's own .jobs-table/.job-status pill/info-btn
// toggle, minus the Stop/Resume/Pause/delete columns (no backend to act
// against). The normal "i" reveals the same .fuzz-history-table detail
// TREE/Results use (Status column dropped there since the row right above
// it already IS this exact job) -- a FAILED row's own red icon is
// independent of it and shows ONLY the error message, matching the live
// app's own two separate controls (a plain info toggle vs. a
// showModal("Job failed", ...) call) instead of merging both into one
// shared panel.
var jobsBody = document.getElementById("jobs-body");
function renderJobs() {
    jobsBody.innerHTML = "";
    if (!DATA.jobs.length) {
        jobsBody.appendChild(h("tr", {}, [h("td", { colspan: "8", class: "empty", text: "No jobs." })]));
        return;
    }
    DATA.jobs.forEach(function (job) {
        var infoBtn = h("button", { type: "button", class: "icon-btn job-info-btn", title: "Fuzz options this job ran with" });
        infoBtn.innerHTML = INFO_ICON_SVG;
        var progressCell = h("td", {});
        renderProgressCell(progressCell, formatProgress(job));

        var statusCell = h("td", {}, [h("span", { class: "job-status job-" + job.status, text: job.status })]);
        var errorRow = null;
        if (job.status === "FAILED" && job.error_reason) {
            errorRow = h("tr", { class: "fuzz-history-row hidden" }, [
                h("td", { colspan: "8" }, [h("div", { class: "fuzz-history-panel" }, [h("div", { class: "job-error", text: job.error_reason })])]),
            ]);
            var errBtn = h("button", { type: "button", class: "icon-btn job-error-info", title: "Why did this fail?", text: "ⓘ" });
            errBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                errorRow.classList.toggle("hidden");
            });
            statusCell.appendChild(errBtn);
        }

        var absoluteUrl;
        try { absoluteUrl = new URL(job.path, DATA.target.base_url).href; } catch (e) { absoluteUrl = DATA.target.base_url; }
        var jobPathLink = h("a", { class: "result-path-link", href: absoluteUrl, target: "_blank", rel: "noopener", text: String(job.path).split(/[?#]/)[0] });
        attachCopyPathContextMenu(jobPathLink, job.path);
        var pathCell = h("td", {}, [jobPathLink]);

        var resultsCell = h("td", { class: "jobs-col-center" });
        if (job.result_count) {
            var resultsLink = h("button", {
                type: "button", class: "results-count-link", title: "Go to these results",
                text: String(job.result_count),
            });
            resultsLink.addEventListener("click", function (e) {
                e.stopPropagation();
                goToResultsAndReveal(String(job.path).split(/[?#]/)[0]);
            });
            resultsCell.appendChild(resultsLink);
        } else {
            resultsCell.textContent = String(job.result_count != null ? job.result_count : "");
        }

        var row = h("tr", {}, [
            statusCell,
            h("td", { class: "job-info-cell" }, [infoBtn]),
            pathCell,
            progressCell,
            resultsCell,
            h("td", { class: "jobs-rate-cell", text: formatRateCellText(job) }),
            h("td", { class: "jobs-errors-cell", text: formatErrorsCellText(job) }),
            h("td", { class: "jobs-elapsed-cell", text: formatElapsedMs(computeElapsedMs(job)) }),
        ]);
        jobsBody.appendChild(row);
        if (errorRow) jobsBody.appendChild(errorRow);

        var detailRow = buildFuzzHistoryRow(8, [{
            status: job.status, wordlist_sources: job.wordlist_sources,
            extensions: job.config && job.config.extensions, config: job.config,
            date: job.started_at || job.created_at, result_count: job.result_count, output_stem: job.output_stem,
            pause_on_status: job.pause_on_status || [],
        }], true);
        jobsBody.appendChild(detailRow);
        infoBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            infoBtn.classList.toggle("job-info-btn-active");
            detailRow.classList.toggle("hidden");
        });
    });
}

// ---- Targets list (this project's own Targets page, read-only): search
// + card grid, same classes/markup the live app's own renderTargets()
// uses (frontend/app.js) so it looks identical -- minus anything that
// mutates data (no "+ New Target" form, no Outputs mirror section, no
// export buttons), explicit request. Clicking a card opens that target's
// own JOBS/RESULTS/TREE workspace in place, exactly like the live app.
var targetsGrid = document.getElementById("targets-grid");
var targetsSearchInput = document.getElementById("targets-search");
function renderTargetsGrid() {
    targetsGrid.innerHTML = "";
    if (!PROJECT_DATA.targets.length) {
        targetsGrid.appendChild(h("div", { class: "empty", text: "No targets in this project." }));
        return;
    }
    var query = targetsSearchInput.value.trim().toLowerCase();
    var visible = PROJECT_DATA.targets.filter(function (t) {
        return !query || t.target.name.toLowerCase().indexOf(query) !== -1 || t.target.base_url.toLowerCase().indexOf(query) !== -1;
    });
    if (!visible.length) {
        targetsGrid.appendChild(h("div", { class: "empty", text: "No targets match your search." }));
        return;
    }
    visible.forEach(function (t) {
        var card = h("div", { class: "card" }, [
            h("h3", { text: t.target.name }),
            h("p", { text: t.target.base_url }),
        ]);
        card.addEventListener("click", function () { loadTarget(t); });
        targetsGrid.appendChild(card);
    });
}
targetsSearchInput.addEventListener("input", renderTargetsGrid);

// ---- Switching between the Targets list and a target's own workspace --
// loadTarget(t) seeds every filter/search control from that target's saved
// live-app view (t.view), rebuilds TREE/Results/JOBS from that target's own data, and
// shows the workspace; showTargetsList() (the project-name breadcrumb
// link's own handler) reverses it.
function applyResultsView(view) {
    view = view || {};
    resultsSearchInput.value = view.search || "";
    resultsFuzzedBtn.classList.toggle("active", !!view.fuzzedOnly);
    resultsStatusInput.value = view.statuses || "";
    resultsStatusInput.classList.toggle("active", resultsStatusInput.value.trim().length > 0);
    resultsStatusSet = resultsStatusInput.value.split(",").map(function (s) { return s.trim(); }).filter(function (s) { return /^\d+$/.test(s); }).map(Number);
    updateResultsFiltersCount();
}
function loadTarget(t) {
    DATA = t;
    treeSearchInput.value = "";
    treeInterestingBtn.classList.remove("active");
    treeFuzzedBtn.classList.remove("active");
    applyResultsView(t.view);
    resultsHideFilters = ((t.view && t.view.hide_rules) || []).map(function (r) { return Object.assign({}, r); });
    fuzzedPaths = {};
    DATA.jobs.forEach(function (j) { fuzzedPaths[String(j.path).split(/[?#]/)[0]] = true; });
    renderBreadcrumb();
    renderTree();
    renderHideChips();
    updateResultsFiltersCount();
    renderResults();
    renderJobs();
    switchToExportTab("tree");
    document.getElementById("targets-list-view").classList.add("hidden");
    document.getElementById("target-workspace").classList.remove("hidden");
    scrollAreaEl.scrollTop = 0;
}
function showTargetsList() {
    DATA = null;
    renderBreadcrumb();
    document.getElementById("target-workspace").classList.add("hidden");
    document.getElementById("targets-list-view").classList.remove("hidden");
    scrollAreaEl.scrollTop = 0;
}

renderBreadcrumb();
renderTargetsGrid();
</script>
</body>
</html>
"""
