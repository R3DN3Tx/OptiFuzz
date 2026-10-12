from backend.models.discovery import Discovery
from backend.models.job import FuzzJob, JobStatus
from backend.models.job_config import DirsearchJobConfig, config_from_dict, config_to_dict
from backend.models.project import Project
from backend.models.target import Target
from backend.models.tree_node import TreeNode


def test_project_round_trip() -> None:
    project = Project(id="client-a-aaaaaaaa", name="Client A", created_at="2026-01-01T00:00:00+00:00")

    assert Project.from_dict(project.to_dict()) == project


def test_target_round_trip() -> None:
    target = Target(
        id="www-example-com-bbbbbbbb",
        project_id="client-a-aaaaaaaa",
        name="www.example.com",
        base_url="https://www.example.com/",
        created_at="2026-01-01T00:00:00+00:00",
    )

    assert Target.from_dict(target.to_dict()) == target


def test_fuzz_job_round_trip_preserves_status_enum() -> None:
    job = FuzzJob(
        id="a1b2c3d4e5f6",
        target_id="www-example-com-bbbbbbbb",
        project_id="client-a-aaaaaaaa",
        path="/api/",
        config={"url": "https://www.example.com/api/", "threads": 10, "extensions": ["php"]},
        status=JobStatus.RUNNING,
        created_at="2026-01-01T00:00:00+00:00",
        completed_lines=2,
        completed_requests=2,
        wordlist_sources=["common.txt"],
    )

    restored = FuzzJob.from_dict(job.to_dict())

    assert restored == job
    assert isinstance(restored.status, JobStatus)


def test_fuzz_job_from_dict_fills_defaults_for_missing_optional_fields() -> None:
    minimal = {
        "id": "a1b2c3d4e5f6",
        "target_id": "t1",
        "project_id": "p1",
        "path": "/",
        "config": {"url": "https://target/"},
        "status": "QUEUED",
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    job = FuzzJob.from_dict(minimal)

    assert job.status == JobStatus.QUEUED
    assert job.completed_lines == 0
    assert job.stop_requested is False
    assert job.wordlist_sources == []


def test_tree_node_round_trip_preserves_discoveries() -> None:
    node = TreeNode(
        id="api-admin-aaaaaaaa",
        project_id="client-a-aaaaaaaa",
        target_id="www-example-com-bbbbbbbb",
        path="/api/admin",
        created_at="2026-01-01T00:00:00+00:00",
        interesting=True,
        note="Revisar bypass de 403.",
        discoveries=[
            Discovery(source="manual", at="2026-01-01T00:00:00+00:00"),
            Discovery(source="result", at="2026-01-01T00:01:00+00:00", job_id="job1", status=403, size="120B"),
        ],
    )

    restored = TreeNode.from_dict(node.to_dict())

    assert restored == node


def test_tree_node_from_dict_fills_defaults_for_missing_optional_fields() -> None:
    minimal = {
        "id": "api-aaaaaaaa",
        "project_id": "p1",
        "target_id": "t1",
        "path": "/api",
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    node = TreeNode.from_dict(minimal)

    assert node.interesting is False
    assert node.note == ""
    assert node.discoveries == []


def test_dirsearch_job_config_round_trip_preserves_extra_args() -> None:
    config = DirsearchJobConfig(
        url="https://target.local/",
        extensions=("php",),
        extra_args=("--recursive", "--exclude-subdirs", "/api/"),
    )

    restored = config_from_dict(config_to_dict(config))

    assert restored == config


def test_dirsearch_job_config_from_dict_defaults_extra_args_to_empty() -> None:
    config = config_from_dict({"url": "https://target.local/"})

    assert config.extra_args == ()
