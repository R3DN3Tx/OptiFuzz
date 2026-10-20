from __future__ import annotations

import re
from typing import Any

from backend.models.discovery import Discovery
from backend.models.job import FuzzJob
from backend.models.timestamps import now_iso
from backend.models.tree_node import TreeNode
from backend.persistence import layout
from backend.persistence.atomic import read_json, write_json_atomic
from backend.persistence.slug import make_id


class TreeNodeNotFoundError(Exception):
    pass


def normalize_path(path: str) -> str:
    """Only enforces a leading "/" -- deliberately does NOT touch a trailing
    slash. Used to strip it here (so "/api/admin/" and "/api/admin" always
    landed on one node), but that silently diverged from a job's own `path`
    (which keeps whatever the pentester actually fuzzed, slash or not),
    producing a real, reported bug: fuzzing "/path/" showed the "Fuzzed" tag
    on a node stripped to "/path", and results grouping (frontend's own
    `normalizeKey`, used for grouping only, never storage) could disagree
    with which node that tag landed on. Storing exactly what was sent keeps
    one single source of truth end-to-end -- grouping/matching still goes
    through the frontend's own `normalizeKey` where two forms need to be
    treated as "the same place" (e.g. Results grouping a child under its
    parent), but the stored/displayed path itself is never mutated."""
    path = path.strip()
    if not path:
        raise ValueError("path is required")
    if not path.startswith("/"):
        path = "/" + path
    return path


def _load_nodes(project_id: str, target_id: str) -> dict[str, TreeNode]:
    path = layout.tree_file(project_id, target_id)
    if not path.is_file():
        return {}
    data = read_json(path)
    return {n["id"]: TreeNode.from_dict(n) for n in data.get("nodes", [])}


def _save_nodes(project_id: str, target_id: str, nodes: dict[str, TreeNode]) -> None:
    write_json_atomic(
        layout.tree_file(project_id, target_id),
        {"nodes": [n.to_dict() for n in nodes.values()]},
    )


def _ensure_ancestors(nodes: dict[str, TreeNode], project_id: str, target_id: str, path: str) -> bool:
    """Adding "/a/b/c" also adds "/a" and "/a/b" as real nodes of their own --
    they're part of the tree now, with their own note, interesting flag, status
    and deletion, not just placeholders implied by a deeper path. Returns
    whether anything new was added."""
    segments = [s for s in _key_for_comparison(path).split("/") if s]
    existing = {_key_for_comparison(n.path) for n in nodes.values()}
    changed = False
    for depth in range(1, len(segments)):
        ancestor = "/" + "/".join(segments[:depth])
        if ancestor in existing:
            continue
        node = TreeNode(
            id=make_id(ancestor), project_id=project_id, target_id=target_id, path=ancestor,
            created_at=now_iso(), discoveries=[Discovery(source="ancestor", at=now_iso())],
        )
        nodes[node.id] = node
        existing.add(ancestor)
        changed = True
    return changed


def _find_by_path(nodes: dict[str, TreeNode], path: str) -> TreeNode | None:
    for node in nodes.values():
        if node.path == path:
            return node
    return None


def list_nodes(project_id: str, target_id: str) -> list[TreeNode]:
    nodes = _load_nodes(project_id, target_id)
    if not any(n.path == "/" for n in nodes.values()):
        # The root is always part of the tree, whether or not it's ever been
        # explicitly fuzzed or added to it -- undeletable (see delete_node
        # below), with its own note/interesting/fuzz-history like any other
        # node. String-sorts first on its own ("/" is a prefix of every
        # other path), so no separate "show root first" logic is needed.
        add_path(project_id, target_id, "/", source="root")
        nodes = _load_nodes(project_id, target_id)
    # Trees saved before ancestors became real nodes get their missing ones here, once.
    changed = False
    for node in list(nodes.values()):
        changed |= _ensure_ancestors(nodes, project_id, target_id, node.path)
    if changed:
        _save_nodes(project_id, target_id, nodes)
    return sorted(nodes.values(), key=lambda n: n.path)


def get_node(project_id: str, target_id: str, node_id: str) -> TreeNode:
    node = _load_nodes(project_id, target_id).get(node_id)
    if node is None:
        raise TreeNodeNotFoundError(node_id)
    return node


def add_path(
    project_id: str,
    target_id: str,
    path: str,
    *,
    source: str = "manual",
    job_id: str | None = None,
    status: int | None = None,
    size: str | None = None,
) -> TreeNode:
    """Add a path to the tree, or -- if it's already there -- record this as
    another Discovery on the existing node rather than duplicating it
   ."""
    normalized = normalize_path(path)
    nodes = _load_nodes(project_id, target_id)
    node = _find_by_path(nodes, normalized)
    _ensure_ancestors(nodes, project_id, target_id, normalized)
    discovery = Discovery(source=source, at=now_iso(), job_id=job_id, status=status, size=size)

    if node is None:
        # make_id() slugifies the path for the id, and "/" has no
        # alphanumeric characters to slugify -- give the root a fixed,
        # literal id instead (still passes validate_safe_id's [a-z0-9-]
        # pattern) rather than crashing on that one path.
        node_id = "root" if normalized == "/" else make_id(normalized)
        node = TreeNode(
            id=node_id,
            project_id=project_id,
            target_id=target_id,
            path=normalized,
            created_at=now_iso(),
            discoveries=[discovery],
        )
        nodes[node.id] = node
    else:
        node.discoveries.append(discovery)

    _save_nodes(project_id, target_id, nodes)
    return node


def ensure_fuzzed_directory_node(project_id: str, target_id: str, path: str) -> None:
    """A fuzz of "/es-eu/" where the tree only has the stub "/es-eu": add the
    "/es-eu/" row too, since that is the directory dirsearch fuzzes and the one
    that carries the Fuzzed tag. Does nothing when the tree does not track the
    place at all (it stays a curated list) or already has the slash form."""
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    if path == "/" or not path.endswith("/"):
        return
    nodes = _load_nodes(project_id, target_id)
    if _find_by_path(nodes, path) is not None:
        return
    if _find_by_path(nodes, _key_for_comparison(path)) is not None:
        add_path(project_id, target_id, path, source="fuzz")


def record_fuzz_discoveries(
    project_id: str,
    target_id: str,
    job_id: str,
    findings: list[tuple[str, int, str | None]],
) -> None:
    """Real, reported bug (2026-09-25): a path added to the tree manually
    (or via /from-result) never updated again once a later fuzz job
    actually discovered a real hit at that exact path -- its status/size
    stayed whatever (or nothing) it was first recorded with, even though
    Results clearly showed a fresh, real finding there. This is the
    server-side half of that fix: called once per results batch (see
    JobManager._flush_results), so a busy job doesn't re-load/re-save the
    tree file once per individual result.

    Deliberately only ever UPDATES an existing node -- never creates one.
    Auto-creating a tree node for every single discovered path here would
    duplicate what Results' own "Add to TREE" button/hierarchy is for; the
    tree is still an intentional, curated list of paths a pentester chose
    to track, this just keeps an already-tracked one honest."""
    if not findings:
        return
    nodes = _load_nodes(project_id, target_id)
    if not nodes:
        return
    by_path = {node.path: node for node in nodes.values()}
    changed = False
    for path, status, size in findings:
        node = by_path.get(normalize_path(path))
        if node is None:
            continue
        node.discoveries.append(
            Discovery(source="fuzz", at=now_iso(), job_id=job_id, status=status, size=size)
        )
        changed = True
    if changed:
        _save_nodes(project_id, target_id, nodes)


def set_interesting(project_id: str, target_id: str, node_id: str, interesting: bool) -> TreeNode:
    nodes = _load_nodes(project_id, target_id)
    node = nodes.get(node_id)
    if node is None:
        raise TreeNodeNotFoundError(node_id)
    node.interesting = interesting
    _save_nodes(project_id, target_id, nodes)
    return node


def set_note(project_id: str, target_id: str, node_id: str, note: str) -> TreeNode:
    nodes = _load_nodes(project_id, target_id)
    node = nodes.get(node_id)
    if node is None:
        raise TreeNodeNotFoundError(node_id)
    node.note = note
    _save_nodes(project_id, target_id, nodes)
    return node


def _key_for_comparison(path: str) -> str:
    """Same idea as the frontend's own normalizeKey() -- a trailing slash
    never changes which node a path refers to. Comparison-only: never used
    to mutate a stored path (see normalize_path's own docstring)."""
    return path[:-1] if path != "/" and path.endswith("/") else path


def _self_and_descendants(nodes: dict[str, TreeNode], path: str) -> list[str]:
    target_key = _key_for_comparison(path)
    prefix = f"{target_key}/"  # never "/" itself -- callers reject root before this runs
    return [
        node_id
        for node_id, node in nodes.items()
        if _key_for_comparison(node.path) == target_key or _key_for_comparison(node.path).startswith(prefix)
    ]


def delete_node(project_id: str, target_id: str, node_id: str) -> list[str]:
    """Deletes a node -- and explicit request: any other REAL node whose
    path is a descendant of it too, not just the one node_id. Before this,
    deleting a manually-added parent path (one WITH real children already
    in the tree, e.g. "/admin" when "/admin/panel" was separately added)
    only removed the parent's own entry -- the hierarchy walk in
    tree_service.list_nodes()/the frontend's own buildPathHierarchy()
    still implies "/admin" exists (any real descendant's own key walks
    back up through it), so it immediately reappeared as a synthetic
    placeholder, making the parent effectively undeletable in practice.

    Returns every node id actually removed (the requested one, first,
    followed by any cascaded descendants) -- purely informational for a
    caller that wants to know what happened; nothing in this app's own
    API currently uses it beyond a 204 response.
    """
    nodes = _load_nodes(project_id, target_id)
    node = nodes.get(node_id)
    if node is None:
        raise TreeNodeNotFoundError(node_id)
    if node.path == "/":
        raise ValueError('The tree root ("/") cannot be removed.')

    to_delete = _self_and_descendants(nodes, node.path)
    for did in to_delete:
        del nodes[did]
    _save_nodes(project_id, target_id, nodes)
    return to_delete


def delete_subtree(project_id: str, target_id: str, path: str) -> list[str]:
    """Same cascading delete as delete_node(), but starting from a PATH
    rather than an existing node's id -- for a SYNTHETIC ancestor (one
    implied purely by a deeper real path, e.g. deleting "/path1" when only
    "/path1/path2/test" was ever actually added) that has no real node of
    its own to call delete_node() with at all. Explicit follow-up report:
    delete_node()'s own cascading fix didn't cover this case, since a
    synthetic node was never delete-able in the first place (no id to
    delete). Removes every real node at exactly this path or under it;
    a no-op (returns an empty list, doesn't raise) if nothing real is
    actually there -- unlike delete_node(), there's no "node not found"
    error to raise, a synthetic path not existing as a real node is the
    expected, common case.
    """
    normalized = normalize_path(path)
    if _key_for_comparison(normalized) == "/":
        raise ValueError('The tree root ("/") cannot be removed.')

    nodes = _load_nodes(project_id, target_id)
    to_delete = _self_and_descendants(nodes, normalized)
    if not to_delete:
        return []
    for did in to_delete:
        del nodes[did]
    _save_nodes(project_id, target_id, nodes)
    return to_delete


def latest_status(node: TreeNode) -> dict[str, Any] | None:
    for discovery in reversed(node.discoveries):
        if discovery.status is not None:
            return {"status": discovery.status, "size": discovery.size, "at": discovery.at}
    return None


def fuzz_history(node: TreeNode, jobs: list[FuzzJob]) -> list[dict[str, Any]]:
    """Every Fuzz Job launched directly against this node's own path -- a
    node can be fuzzed multiple times, each an independent history entry,
    never overwriting the last."""
    return fuzz_history_for_path(node.path, jobs)


def fuzz_history_for_path(path: str, jobs: list[FuzzJob]) -> list[dict[str, Any]]:
    # A job's path may carry a query string ("/gs/htc/?a=a"); the node it
    # fuzzed is the path before it. dirsearch always fuzzes a path as a directory,
    # so the fuzz belongs to the slash form ("/es-eu/") only, never to a stub
    # "/es-eu"; jobs stored before that was enforced (no slash) count as the slash form.
    if path != "/" and not path.endswith("/"):
        return []

    def directory(value: str) -> str:
        return value.rstrip("/") or "/"

    wanted = directory(path)
    matching = [
        j for j in jobs
        if directory(normalize_path(re.split(r"[?#]", j.path, maxsplit=1)[0])) == wanted
    ]
    matching.sort(key=lambda j: j.created_at, reverse=True)
    return [
        {
            "job_id": j.id,
            "status": j.status.value,
            "wordlist_sources": j.wordlist_sources,
            "extensions": j.config.get("extensions", []),
            "created_at": j.created_at,
            "ended_at": j.ended_at,
            "result_count": j.result_count,
            "output_stem": j.output_stem,
            "pause_on_status": j.pause_on_status,
            "error_reason": j.error_reason,
            # Full config too (threads, headers, method, filters, ...) --
            # the UI's "already fuzzed" detail shows every option a fuzz
            # actually ran with, not just wordlist/extensions.
            "config": j.config,
        }
        for j in matching
    ]


def node_to_view(node: TreeNode, jobs: list[FuzzJob]) -> dict[str, Any]:
    data = node.to_dict()
    data["status"] = latest_status(node)
    data["fuzz_history"] = fuzz_history(node, jobs)
    return data


def tree_views(project_id: str, target_id: str, jobs: list[FuzzJob], results: list[Any]) -> list[dict[str, Any]]:
    """Every node, with its status and Fuzzed history. A node's own stored
    discovery wins; if it has none yet, the latest result found at its exact
    path fills it in -- exact, not normalized: "x/" and "x" never share a result."""
    latest: dict[str, dict[str, Any]] = {}
    for result in results:
        latest[result.path] = {"status": result.status, "size": result.size, "at": None}
    views = []
    for node in list_nodes(project_id, target_id):
        view = node_to_view(node, jobs)
        if view["status"] is None:
            view["status"] = latest.get(node.path)
        views.append(view)
    return views
