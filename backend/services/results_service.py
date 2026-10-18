from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from backend.models.job import FuzzJob
from backend.models.result import Result
from backend.persistence import layout
from backend.persistence.atomic import read_json, write_json_atomic
from backend.persistence.paths import path_is_file


def read_job_results(project_id: str, target_id: str, job: FuzzJob) -> list[Result]:
    """One job's own results, read from its raw JSON report on disk. Only
    safe to call when no dirsearch process is writing that report -- see
    list_results' own docstring for why a live read can't be done."""
    output_base = layout.job_output_base_current(project_id, target_id, job)
    # NOT .with_suffix(".json"): the stem can itself contain dots (an IP
    # address in the target name, e.g. "127.0.0.1"), and with_suffix()
    # treats everything after the LAST dot as an existing extension to
    # replace -- silently truncating the path (verified: turned
    # ..._8910_admin into ..._127.json, a file that never exists).
    output_json = output_base.parent / f"{output_base.name}.json"
    # path_is_file, not a plain Path.is_file() -- output_json can be a
    # job's full, human-readable final name, which can genuinely exceed
    # MAX_PATH (see persistence/paths.py::long_path's own docstring).
    if not path_is_file(output_json):
        return []
    try:
        data = read_json(output_json)
    except (OSError, ValueError):
        return []
    results: list[Result] = []
    for entry in data.get("results", []):
        url = entry.get("url", "")
        results.append(
            Result(
                job_id=job.id,
                time="",
                status=entry.get("status", 0),
                size=_format_size(entry.get("contentLength")),
                url=url,
                path=urlparse(url).path or "/",
                redirect=entry.get("redirect") or None,
            )
        )
    return results


def list_results(
    project_id: str,
    target_id: str,
    jobs: list[FuzzJob],
    live_results: dict[str, list[Result]] | None = None,
) -> list[Result]:
    """Historical Results for a target, derived from each job's own raw JSON
    report -- never a separate one-file-per-
    result store. This is the "already on disk" view used on first load /
    page refresh.

    `live_results`, when a job has an entry in it, is used INSTEAD of that
    job's file -- a RUNNING job's dirsearch process rewrites its report on
    every single result (atomic os.replace), and on Windows a reader holding
    that file open at that exact instant makes dirsearch itself crash with
    PermissionError [WinError 5] (reproduced: ~95% of replaces fail while a
    reader is polling in a tight loop; FILE_SHARE_DELETE does not help).
    The file is therefore never read while its process might be writing it:
    JobManager keeps the in-memory copy for the live segment instead.
    """
    results: list[Result] = []
    for job in jobs:
        if live_results is not None and job.id in live_results:
            results.extend(live_results[job.id])
        else:
            results.extend(read_job_results(project_id, target_id, job))
    return results


def _format_size(length: Any) -> str:
    if length is None:
        return ""
    return f"{length}B"


def get_seen_keys(project_id: str, target_id: str) -> set[str]:
    """Every individual result the pentester has actually had on screen at
    some point, persisted so it survives an app restart. Each key is
    "{job_id}:{index}" -- a result's stable position within its own job's
    append-only list (dirsearch's report carries no per-result time, see
    Result.time, always "") -- never a per-job COUNT: a real, reported
    requirement is that a result scrolled past, or hidden behind a collapsed
    parent, must stay NEW even after leaving Results, while ones actually
    seen do not -- a single watermark count can only ever say "the first N
    are seen", never "these specific ones, with a gap after"."""
    path = layout.results_seen_file(project_id, target_id)
    if not path.is_file():
        return set()
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return set()
    # Pre-existing files from before this was a set of keys (a per-job COUNT
    # dict) are simply dropped -- the only consequence is every already-seen
    # result shows NEW one more time, self-correcting the moment it's
    # actually looked at again.
    return set(data) if isinstance(data, list) else set()


def mark_seen(project_id: str, target_id: str, keys: list[str]) -> None:
    """Adds `keys` (each a result's own "seen_key", see annotate_new) to the
    persisted seen set -- called with exactly the results that were actually
    painted on screen at some point during this Results visit (the frontend
    tracks this; see paintResultsWindow()'s own accumulation), not
    "everything that exists right now" (the original "opening Results clears
    every NEW" was superseded by this same explicit requirement)."""
    seen = get_seen_keys(project_id, target_id)
    seen.update(keys)
    write_json_atomic(layout.results_seen_file(project_id, target_id), sorted(seen))


def _sanitize_rule(rule: Any) -> dict[str, Any] | None:
    if not isinstance(rule, dict):
        return None
    depth = rule.get("depth", 1)
    return {
        "status": str(rule.get("status", "") or ""),
        "size": str(rule.get("size", "") or ""),
        "parentPath": str(rule.get("parentPath", "") or ""),
        "depth": depth if isinstance(depth, int) else 1,
    }


def get_hide_rules(project_id: str, target_id: str) -> list[dict[str, Any]]:
    path = layout.hide_rules_file(project_id, target_id)
    if not path_is_file(path):
        return []
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return []
    return [r for r in (_sanitize_rule(x) for x in data) if r] if isinstance(data, list) else []


def set_hide_rules(project_id: str, target_id: str, rules: list[Any]) -> list[dict[str, Any]]:
    cleaned = [r for r in (_sanitize_rule(x) for x in rules) if r]
    write_json_atomic(layout.hide_rules_file(project_id, target_id), cleaned)
    return cleaned


def annotate_new(results: list[Result], seen_keys: set[str]) -> list[dict[str, Any]]:
    """Attach `is_new` and the result's own `seen_key` (its identity for
    mark_seen, above) to each result's dict form. Not a Result field: "new"
    is a per-viewing-session annotation, not an intrinsic property of the
    result itself."""
    seen_so_far: dict[str, int] = {}
    annotated = []
    for result in results:
        index = seen_so_far.get(result.job_id, 0)
        seen_so_far[result.job_id] = index + 1
        key = f"{result.job_id}:{index}"
        entry = result.to_dict()
        entry["seen_key"] = key
        entry["is_new"] = key not in seen_keys
        annotated.append(entry)
    return annotated
