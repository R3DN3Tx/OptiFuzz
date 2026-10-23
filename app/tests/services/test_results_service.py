from backend.models.job import FuzzJob, JobStatus
from backend.persistence import layout
from backend.persistence.atomic import write_json_atomic
from backend.services import results_service as rs


def _job(job_id: str, target_id: str, project_id: str, output_stem: str, output_flat: bool = False) -> FuzzJob:
    return FuzzJob(
        id=job_id,
        target_id=target_id,
        project_id=project_id,
        path="/",
        config={"url": "https://api.example.com/"},
        status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00+00:00",
        output_stem=output_stem,
        output_flat=output_flat,
    )


def _write_report(project_id: str, target_id: str, job: FuzzJob, entries: list[dict]) -> None:
    base = layout.job_output_base_for(project_id, target_id, job.id, job.output_stem, job.output_flat)
    write_json_atomic(base.parent / f"{base.name}.json", {"results": entries})


def test_list_results_reads_the_flat_naming_layout(workspace_root) -> None:
    # Current naming generation: outputs/<stem>_<job_id>.{txt,json}, no
    # per-job subfolder (explicit user request).
    job = _job("job1", "t1", "p1", "dirsearch_api-example-com_admin", output_flat=True)
    _write_report("p1", "t1", job, [{"url": "https://api.example.com/admin", "status": 403}])

    base = layout.job_output_base_for("p1", "t1", "job1", job.output_stem, job.output_flat)
    assert base == layout.outputs_dir("p1", "t1") / "dirsearch_api-example-com_admin_job1"
    assert base.parent == layout.outputs_dir("p1", "t1")  # no per-job subfolder

    results = rs.list_results("p1", "t1", [job])
    assert results[0].path == "/admin"


def test_get_seen_keys_defaults_to_empty(workspace_root) -> None:
    assert rs.get_seen_keys("p1", "t1") == set()


def test_mark_seen_persists_exactly_the_given_keys(workspace_root) -> None:
    job = _job("job1", "t1", "p1", "job1")
    _write_report(
        "p1", "t1", job,
        [{"url": "https://api.example.com/admin", "status": 403}, {"url": "https://api.example.com/api", "status": 200}],
    )
    results = rs.list_results("p1", "t1", [job])
    annotated = rs.annotate_new(results, set())

    rs.mark_seen("p1", "t1", [annotated[0]["seen_key"]])

    assert rs.get_seen_keys("p1", "t1") == {"job1:0"}


def test_annotate_new_only_clears_keys_actually_marked_seen(workspace_root) -> None:
    # Real, reported requirement: a result scrolled past, or hidden behind a
    # collapsed parent, must stay NEW even after leaving Results -- only
    # whatever was actually painted on screen gets marked.
    job = _job("job1", "t1", "p1", "job1")
    _write_report(
        "p1", "t1", job,
        [{"url": "https://api.example.com/admin", "status": 403}, {"url": "https://api.example.com/debug", "status": 200}],
    )
    results = rs.list_results("p1", "t1", [job])
    first_pass = rs.annotate_new(results, set())
    # Only the first result was ever actually on screen this visit.
    rs.mark_seen("p1", "t1", [first_pass[0]["seen_key"]])

    annotated = rs.annotate_new(results, rs.get_seen_keys("p1", "t1"))

    assert annotated[0]["is_new"] is False
    assert annotated[1]["is_new"] is True  # never actually seen -- still NEW


def test_annotate_new_treats_never_seen_job_as_all_new(workspace_root) -> None:
    job = _job("job1", "t1", "p1", "job1")
    _write_report("p1", "t1", job, [{"url": "https://api.example.com/x", "status": 200}])

    results = rs.list_results("p1", "t1", [job])
    annotated = rs.annotate_new(results, rs.get_seen_keys("p1", "t1"))

    assert annotated[0]["is_new"] is True


def test_list_results_never_reads_a_job_whose_live_copy_is_present(workspace_root, monkeypatch) -> None:
    # Real, reported bug: a RUNNING job's dirsearch process rewrites its own
    # report on every result, and a reader holding that file open at that
    # instant made dirsearch crash with PermissionError [WinError 5] on
    # Windows. While a live copy exists, the file must not be read at all.
    job = _job("job1", "t1", "p1", "dirsearch_api-example-com_admin", output_flat=True)
    _write_report("p1", "t1", job, [{"url": "https://api.example.com/from-disk", "status": 200}])

    def _forbidden(*args, **kwargs):
        raise AssertionError("report file read while its live copy is present")

    monkeypatch.setattr(rs, "read_job_results", _forbidden)
    live = {"job1": [rs.Result(job_id="job1", time="", status=403, size="1B", url="https://api.example.com/live", path="/live")]}

    results = rs.list_results("p1", "t1", [job], live_results=live)

    assert [r.path for r in results] == ["/live"]


def test_format_size_matches_dirsearchs_own_readable_sizes() -> None:
    # The live output says "579KB", so the report's byte count must read the same.
    from backend.services.results_service import _format_size

    assert _format_size(None) == ""
    assert _format_size(15) == "15B"
    assert _format_size(1023) == "1023B"
    assert _format_size(593287) == "579KB"
    assert _format_size(5 * 1024 * 1024) == "5MB"
