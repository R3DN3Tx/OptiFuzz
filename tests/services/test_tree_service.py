import pytest

from backend.models.job import FuzzJob, JobStatus
from backend.services import project_service as ps
from backend.services import target_service as ts
from backend.services import tree_service as trs


def _setup(workspace_root):
    project = ps.create_project("Client A")
    target = ts.create_target(project.id, "api.example.com", "https://api.example.com/")
    return project.id, target.id


def test_normalize_path_variants() -> None:
    # Only enforces a leading "/" -- a trailing slash is preserved exactly
    # as given (spec change: was previously stripped, which diverged from
    # what a job actually fuzzed -- see normalize_path's own docstring).
    assert trs.normalize_path("api/admin/") == "/api/admin/"
    assert trs.normalize_path("/api/admin") == "/api/admin"
    assert trs.normalize_path("/api/admin/") == "/api/admin/"
    assert trs.normalize_path("/") == "/"


def test_normalize_path_rejects_empty() -> None:
    with pytest.raises(ValueError):
        trs.normalize_path("   ")


def test_add_path_manual(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)

    node = trs.add_path(project_id, target_id, "/api/admin/", source="manual")

    assert node.path == "/api/admin/"
    assert len(node.discoveries) == 1
    assert node.discoveries[0].source == "manual"
    assert node.interesting is False
    assert node.note == ""


def test_add_path_does_not_duplicate_existing_node(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)

    trs.add_path(project_id, target_id, "/api/admin")
    trs.add_path(project_id, target_id, "/api/admin", source="result", status=403, job_id="job1")

    nodes = [n for n in trs.list_nodes(project_id, target_id) if n.path == "/api/admin"]
    assert len(nodes) == 1
    assert len(nodes[0].discoveries) == 2


def test_add_path_with_and_without_trailing_slash_are_distinct_nodes(workspace_root) -> None:
    # Deliberately NOT merged (unlike the old behavior): a trailing slash is
    # now preserved exactly as sent, so these are two different nodes.
    project_id, target_id = _setup(workspace_root)

    trs.add_path(project_id, target_id, "/api/admin")
    trs.add_path(project_id, target_id, "/api/admin/")

    nodes = {n.path for n in trs.list_nodes(project_id, target_id) if n.path != "/"}
    assert nodes == {"/api", "/api/admin", "/api/admin/"}


def test_latest_status_reflects_most_recent_discovery(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)

    trs.add_path(project_id, target_id, "/api/debug", source="result", status=403, job_id="job1")
    trs.add_path(project_id, target_id, "/api/debug", source="result", status=200, job_id="job2")

    node = next(n for n in trs.list_nodes(project_id, target_id) if n.path == "/api/debug")
    status = trs.latest_status(node)
    assert status["status"] == 200


def test_set_interesting_and_note(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    node = trs.add_path(project_id, target_id, "/admin")

    trs.set_interesting(project_id, target_id, node.id, True)
    trs.set_note(project_id, target_id, node.id, "Forbidden. Revisar si existe bypass.")

    updated = trs.get_node(project_id, target_id, node.id)
    assert updated.interesting is True
    assert updated.note == "Forbidden. Revisar si existe bypass."


def test_note_survives_multiple_fuzzings_of_same_node(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    node = trs.add_path(project_id, target_id, "/api/debug")
    trs.set_note(project_id, target_id, node.id, "Dev endpoint.")

    trs.add_path(project_id, target_id, "/api/debug", source="result", status=200, job_id="job1")
    trs.add_path(project_id, target_id, "/api/debug", source="result", status=403, job_id="job2")

    updated = trs.get_node(project_id, target_id, node.id)
    assert updated.note == "Dev endpoint."
    assert len(updated.discoveries) == 3


def test_delete_node(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    node = trs.add_path(project_id, target_id, "/old")

    trs.delete_node(project_id, target_id, node.id)

    # The root ("/") auto-creates on list and is never itself removable --
    # only the "/old" node just deleted should be gone.
    assert [n.path for n in trs.list_nodes(project_id, target_id)] == ["/"]
    with pytest.raises(trs.TreeNodeNotFoundError):
        trs.get_node(project_id, target_id, node.id)


def test_delete_node_cascades_to_real_descendants(workspace_root) -> None:
    # Real, reported bug: deleting a manually-added parent path that
    # already had real children under it (e.g. "/admin" with "/admin/panel"
    # separately added) only removed the parent's own entry -- the
    # hierarchy walk still implied "/admin" existed (any real descendant's
    # own key walks back up through it), so it immediately reappeared as a
    # synthetic placeholder, making the parent effectively undeletable.
    project_id, target_id = _setup(workspace_root)
    parent = trs.add_path(project_id, target_id, "/admin")
    child = trs.add_path(project_id, target_id, "/admin/panel")
    grandchild = trs.add_path(project_id, target_id, "/admin/panel/settings")
    unrelated = trs.add_path(project_id, target_id, "/administrator")  # must NOT be swept up

    deleted_ids = trs.delete_node(project_id, target_id, parent.id)

    assert set(deleted_ids) == {parent.id, child.id, grandchild.id}
    remaining_paths = {n.path for n in trs.list_nodes(project_id, target_id)}
    assert remaining_paths == {"/", "/administrator"}
    with pytest.raises(trs.TreeNodeNotFoundError):
        trs.get_node(project_id, target_id, child.id)


def test_delete_node_with_a_trailing_slash_still_cascades_correctly(workspace_root) -> None:
    # The parent's own STORED path can carry a trailing slash
    # (normalize_path deliberately never strips one) -- comparison must
    # still treat "/admin/" and "/admin" as the same node for cascading.
    project_id, target_id = _setup(workspace_root)
    parent = trs.add_path(project_id, target_id, "/admin/")
    child = trs.add_path(project_id, target_id, "/admin/panel")

    deleted_ids = trs.delete_node(project_id, target_id, parent.id)

    assert set(deleted_ids) == {parent.id, child.id}


def test_delete_leaf_node_does_not_touch_unrelated_siblings(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    leaf = trs.add_path(project_id, target_id, "/admin/panel")
    sibling = trs.add_path(project_id, target_id, "/admin/other")

    deleted_ids = trs.delete_node(project_id, target_id, leaf.id)

    assert deleted_ids == [leaf.id]
    assert trs.get_node(project_id, target_id, sibling.id) is not None


def test_delete_subtree_cascades_a_synthetic_ancestor_by_path(workspace_root) -> None:
    # Real, reported follow-up: delete_node()'s own cascade only helps when
    # the parent itself has a real node/id -- a purely SYNTHETIC ancestor
    # (never itself explicitly added, only implied by a deeper real path
    # like "/path1/path2/test") has no id at all to call delete_node() with,
    # so it stayed undeletable even after that fix. delete_subtree() starts
    # from a PATH instead, for exactly this case.
    project_id, target_id = _setup(workspace_root)
    leaf = trs.add_path(project_id, target_id, "/path1/path2/test")
    unrelated = trs.add_path(project_id, target_id, "/path1-other")

    deleted_ids = trs.delete_subtree(project_id, target_id, "/path1")

    assert leaf.id in deleted_ids
    remaining_paths = {n.path for n in trs.list_nodes(project_id, target_id)}
    assert remaining_paths == {"/", "/path1-other"}


def test_delete_subtree_is_a_noop_when_nothing_real_exists_there(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    trs.add_path(project_id, target_id, "/admin")

    deleted_ids = trs.delete_subtree(project_id, target_id, "/nothing/here")

    assert deleted_ids == []
    assert {n.path for n in trs.list_nodes(project_id, target_id)} == {"/", "/admin"}


def test_delete_subtree_rejects_the_root(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)

    with pytest.raises(ValueError):
        trs.delete_subtree(project_id, target_id, "/")


def test_get_missing_node_raises(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)

    with pytest.raises(trs.TreeNodeNotFoundError):
        trs.get_node(project_id, target_id, "does-not-exist")


def test_root_is_auto_created_and_listed_first(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    trs.add_path(project_id, target_id, "/admin")

    nodes = trs.list_nodes(project_id, target_id)

    assert nodes[0].path == "/"
    assert [n.path for n in nodes] == ["/", "/admin"]

    # Listing again doesn't create a second root node.
    nodes_again = trs.list_nodes(project_id, target_id)
    assert len([n for n in nodes_again if n.path == "/"]) == 1


def test_root_cannot_be_deleted(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    root = next(n for n in trs.list_nodes(project_id, target_id) if n.path == "/")

    with pytest.raises(ValueError):
        trs.delete_node(project_id, target_id, root.id)

    assert trs.get_node(project_id, target_id, root.id) is not None


def _job(job_id: str, target_id: str, project_id: str, path: str, **overrides) -> FuzzJob:
    defaults = dict(
        id=job_id,
        target_id=target_id,
        project_id=project_id,
        path=path,
        config={"url": "https://api.example.com/api", "extensions": ["php"]},
        status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00+00:00",
        wordlist_sources=["common.txt"],
    )
    defaults.update(overrides)
    return FuzzJob(**defaults)


def test_fuzz_history_matches_jobs_by_normalized_path(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    node = trs.add_path(project_id, target_id, "/api/")

    matching = _job("job1", target_id, project_id, "/api/")
    other = _job("job2", target_id, project_id, "/admin/")

    history = trs.fuzz_history(node, [matching, other])

    assert len(history) == 1
    assert history[0]["job_id"] == "job1"


def test_record_fuzz_discoveries_updates_an_already_tracked_node(workspace_root) -> None:
    # Real, reported bug (2026-09-25): a path added to TREE manually, with
    # no status/size yet, never picked one up even once a later fuzz job
    # discovered a real hit at that exact path.
    project_id, target_id = _setup(workspace_root)
    node = trs.add_path(project_id, target_id, "/admin", source="manual")
    assert trs.latest_status(node) is None

    trs.record_fuzz_discoveries(
        project_id, target_id, "job1", [("/admin", 200, "1.2KB"), ("/unrelated", 404, "0B")]
    )

    updated = trs.get_node(project_id, target_id, node.id)
    status = trs.latest_status(updated)
    assert status == {"status": 200, "size": "1.2KB", "at": updated.discoveries[-1].at}
    assert updated.discoveries[-1].source == "fuzz"
    assert updated.discoveries[-1].job_id == "job1"
    # The unrelated path was never tracked -- no node was created for it.
    assert {n.path for n in trs.list_nodes(project_id, target_id)} == {"/", "/admin"}


def test_record_fuzz_discoveries_is_a_noop_when_nothing_is_tracked(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)

    trs.record_fuzz_discoveries(project_id, target_id, "job1", [("/anything", 200, "1B")])

    assert {n.path for n in trs.list_nodes(project_id, target_id)} == {"/"}


def test_node_to_view_includes_status_and_history(workspace_root) -> None:
    project_id, target_id = _setup(workspace_root)
    node = trs.add_path(project_id, target_id, "/api", source="result", status=200, job_id="parent-job")
    job = _job("job1", target_id, project_id, "/api")

    view = trs.node_to_view(node, [job])

    assert view["status"]["status"] == 200
    assert view["fuzz_history"][0]["job_id"] == "job1"


def test_adding_a_deep_path_makes_its_ancestors_real_nodes(workspace_root) -> None:
    # Real, reported bug: "/a/b/c" implied "/a" and "/a/b", but those stayed
    # placeholders -- no status, no Fuzzed tag, not deletable. They're real now.
    project_id, target_id = _setup(workspace_root)
    trs.add_path(project_id, target_id, "/a/b/c", source="manual")

    assert {n.path for n in trs.list_nodes(project_id, target_id)} == {"/", "/a", "/a/b", "/a/b/c"}


def test_tree_views_gives_an_ancestor_its_own_status_and_fuzz_history(workspace_root) -> None:
    from backend.models.result import Result

    project_id, target_id = _setup(workspace_root)
    trs.add_path(project_id, target_id, "/a/b/c", source="manual")
    jobs = [_job("job1", target_id, project_id, "/a/b")]
    results = [Result(job_id="job1", time="", status=301, size="0B", url="https://api.example.com/a/b", path="/a/b")]

    views = {v["path"]: v for v in trs.tree_views(project_id, target_id, jobs, results)}

    assert views["/a/b"]["id"] is not None
    assert views["/a/b"]["status"]["status"] == 301
    assert views["/a/b"]["fuzz_history"][0]["job_id"] == "job1"
    assert views["/a"]["status"] is None
    assert views["/a"]["fuzz_history"] == []


def test_result_status_is_matched_by_exact_path_only(workspace_root) -> None:
    from backend.models.result import Result

    project_id, target_id = _setup(workspace_root)
    trs.add_path(project_id, target_id, "/x/", source="manual")
    results = [Result(job_id="job1", time="", status=200, size="1B", url="https://api.example.com/x", path="/x")]

    views = {v["path"]: v for v in trs.tree_views(project_id, target_id, [], results)}

    assert views["/x/"]["status"] is None
