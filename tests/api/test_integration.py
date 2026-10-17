import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.engine.mock import MockDirsearchEngine
from backend.events.bus import EventBus
from backend.job_manager.manager import JobManager
from backend.main import app
from backend.persistence import layout
from backend.persistence.atomic import write_json_atomic


def _wordlist(tmp_path: Path, name: str = "words.txt", count: int = 10) -> str:
    path = tmp_path / name
    path.write_text("\n".join(f"word{i}" for i in range(count)) + "\n", encoding="utf-8")
    return str(path)


def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("COMPLETED", "FAILED", "STOPPED"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not reach a terminal state in time")


def _wait_for_status(client: TestClient, job_id: str, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == status:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not reach status {status} in time")


def test_full_project_target_job_flow(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "api.example.com", "base_url": "https://api.example.com/"},
    ).json()

    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path)], "extensions": ["php"]},
    ).json()
    # Dispatch is synchronous inside create_job, so with a free concurrency
    # slot the job can already be RUNNING by the time the response arrives.
    assert job["status"] in ("QUEUED", "RUNNING")
    assert job["config"]["url"] == "https://api.example.com/"

    final = _wait_for_terminal(client, job["id"])
    assert final["status"] == "COMPLETED"
    assert final["result_count"] > 0

    results = client.get(
        f"/api/projects/{project['id']}/targets/{target['id']}/results"
    ).json()
    # Mock engine doesn't write report files, so the historical-results
    # endpoint (backed by outputs/job_<id>.json, docs/ARCHITECTURE.md \u00a72.4)
    # is legitimately empty here -- live results arrive over SSE instead.
    assert results == []

    jobs = client.get(f"/api/projects/{project['id']}/targets/{target['id']}/jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["id"] == job["id"]


def test_get_missing_project_returns_404(client: TestClient) -> None:
    response = client.get("/api/projects/does-not-exist")

    assert response.status_code == 404


def test_duplicate_project_name_returns_409(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Client A"})

    response = client.post("/api/projects", json={"name": "Client A"})

    assert response.status_code == 409


def test_create_job_with_missing_wordlist_returns_400(client: TestClient) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()

    response = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [r"C:\does\not\exist.txt"]},
    )

    assert response.status_code == 400


def test_create_job_with_extra_args_appends_them_to_the_config(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()

    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={
            "path": "/",
            "wordlist_files": [_wordlist(tmp_path)],
            "extra_args": '--recursive --exclude-subdirs "/api/ admin/"',
        },
    ).json()

    assert job["config"]["extra_args"] == ["--recursive", "--exclude-subdirs", "/api/ admin/"]


def test_create_job_with_authenticated_tags_the_output_stem(client: TestClient, tmp_path: Path) -> None:
    # Purely a saved-output-filename tag -- explicit request: "__AUTH"
    # (double underscore) appended after the path, so an authenticated
    # fuzz's report is visually distinguishable from an unauthenticated
    # one when browsing outputs/ directly. Never touches the actual
    # dirsearch invocation/config.
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()

    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/admin", "wordlist_files": [_wordlist(tmp_path)], "authenticated": True},
    ).json()

    assert job["output_stem"].endswith("__AUTH")
    assert "config" not in job or "authenticated" not in job["config"]  # never leaks into the dirsearch config


def test_create_job_without_authenticated_leaves_the_output_stem_untagged(
    client: TestClient, tmp_path: Path
) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()

    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/admin", "wordlist_files": [_wordlist(tmp_path)]},
    ).json()

    assert not job["output_stem"].endswith("__AUTH")


def test_create_job_with_malformed_extra_args_returns_400(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()

    response = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path)], "extra_args": '--cookie "unterminated'},
    )

    assert response.status_code == 400


def test_jobs_and_results_are_isolated_between_targets(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    www = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "www", "base_url": "https://www.example.com/"},
    ).json()
    api = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "api", "base_url": "https://api.example.com/"},
    ).json()

    job_www = client.post(
        f"/api/projects/{project['id']}/targets/{www['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path, "a.txt")]},
    ).json()
    job_api = client.post(
        f"/api/projects/{project['id']}/targets/{api['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path, "b.txt")]},
    ).json()

    _wait_for_terminal(client, job_www["id"])
    _wait_for_terminal(client, job_api["id"])

    www_jobs = client.get(f"/api/projects/{project['id']}/targets/{www['id']}/jobs").json()
    api_jobs = client.get(f"/api/projects/{project['id']}/targets/{api['id']}/jobs").json()

    assert {j["id"] for j in www_jobs} == {job_www["id"]}
    assert {j["id"] for j in api_jobs} == {job_api["id"]}


def test_stop_job_endpoint(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path)]},
    ).json()

    client.post(f"/api/jobs/{job['id']}/stop")
    # stop_job() returns immediately after signalling -- the job may still
    # read RUNNING at that exact instant, until the background runner
    # thread notices stop_requested and finalizes it.
    final = _wait_for_terminal(client, job["id"])

    assert final["status"] in ("STOPPED", "COMPLETED")  # may finish before stop lands


def test_pause_job_endpoint_then_resume(client: TestClient, tmp_path: Path) -> None:
    # A manual Pause button (not the auto-pause-on-status feature), driven
    # end-to-end through the API: pause a RUNNING job, confirm it lands on
    # PAUSED, then resume it to completion.
    app.state.job_manager = JobManager(
        MockDirsearchEngine(every=1, delay=0.1), event_bus=EventBus()
    )

    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path, count=30)]},
    ).json()

    _wait_for_status(client, job["id"], "RUNNING", timeout=5)
    client.post(f"/api/jobs/{job['id']}/pause")
    paused = _wait_for_status(client, job["id"], "PAUSED", timeout=10)
    assert paused["pause_requested"] is True

    client.post(f"/api/jobs/{job['id']}/resume")

    final = _wait_for_terminal(client, job["id"], timeout=10)
    assert final["status"] == "COMPLETED"


def test_delete_target_blocked_while_job_active(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path, count=200)]},
    ).json()

    response = client.delete(f"/api/projects/{project['id']}/targets/{target['id']}")
    assert response.status_code == 409

    client.post(f"/api/jobs/{job['id']}/stop")
    _wait_for_terminal(client, job["id"])

    response = client.delete(f"/api/projects/{project['id']}/targets/{target['id']}")
    assert response.status_code == 204


def test_delete_project_blocked_while_a_targets_job_is_active(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path, count=200)]},
    ).json()

    response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 409

    client.post(f"/api/jobs/{job['id']}/stop")
    _wait_for_terminal(client, job["id"])

    response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 204


def test_wordlists_endpoint_lists_files_and_folders(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Independent of which example wordlists a given checkout ships: builds its own tree.
    from backend.services import wordlist_service

    (tmp_path / "top.txt").write_text("admin\n")
    (tmp_path / "extra").mkdir()
    (tmp_path / "extra" / "api_paths.txt").write_text("api\n")
    (tmp_path / ".hidden.txt").write_text("never listed\n")
    monkeypatch.setattr(wordlist_service, "WORDLISTS_DIR", tmp_path)

    tree = client.get("/api/wordlists").json()

    names = {child["name"] for child in tree["children"]}
    assert "top.txt" in names
    assert ".hidden.txt" not in names
    extra = next(c for c in tree["children"] if c["name"] == "extra")
    assert extra["type"] == "dir"
    assert any(f["name"] == "api_paths.txt" for f in extra["children"])


def test_pause_on_status_and_resume_via_api(client: TestClient, tmp_path: Path) -> None:
    # Swap in an engine that always reports 429, to exercise the "target
    # rate-limited / down" auto-pause behaviour end-to-end through the API.
    app.state.job_manager = JobManager(
        MockDirsearchEngine(every=1, delay=0.05, force_status=429), event_bus=EventBus()
    )

    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={
            "path": "/",
            "wordlist_files": [_wordlist(tmp_path, count=20)],
            "pause_on_status": [429],
        },
    ).json()

    paused = _wait_for_status(client, job["id"], "PAUSED", timeout=10)
    assert paused["pause_requested"] is True
    assert paused["completed_lines"] == 0

    resumed = client.post(f"/api/jobs/{job['id']}/resume").json()
    assert resumed["status"] in ("QUEUED", "RUNNING", "PAUSED")

    # Same dead-target condition still holds, so it pauses again -- proves
    # resume actually re-dispatched the job rather than being a no-op.
    _wait_for_status(client, job["id"], "PAUSED", timeout=10)


def test_resuming_a_non_paused_job_is_a_no_op(client: TestClient, tmp_path: Path) -> None:
    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path)]},
    ).json()
    final = _wait_for_terminal(client, job["id"])
    assert final["status"] == "COMPLETED"

    result = client.post(f"/api/jobs/{job['id']}/resume").json()

    assert result["status"] == "COMPLETED"


def test_engine_version_endpoint_reports_mock(client: TestClient) -> None:
    response = client.get("/api/engine/version")

    assert response.status_code == 200
    assert "mock" in response.json()["version"].lower()


def test_target_concurrency_round_trip_is_scoped_to_that_target(client: TestClient) -> None:
    project = client.post("/api/projects", json={"name": "Concurrency Co"}).json()
    target_a = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "a.example.com", "base_url": "https://a.example.com/"},
    ).json()
    target_b = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "b.example.com", "base_url": "https://b.example.com/"},
    ).json()

    got = client.get(f"/api/projects/{project['id']}/targets/{target_a['id']}/concurrency").json()
    assert got["max_concurrent_jobs"] >= 1

    updated = client.put(
        f"/api/projects/{project['id']}/targets/{target_a['id']}/concurrency",
        json={"max_concurrent_jobs": 4},
    ).json()
    assert updated["max_concurrent_jobs"] == 4
    assert (
        client.get(f"/api/projects/{project['id']}/targets/{target_a['id']}/concurrency").json()[
            "max_concurrent_jobs"
        ]
        == 4
    )
    # A different target is unaffected.
    assert (
        client.get(f"/api/projects/{project['id']}/targets/{target_b['id']}/concurrency").json()[
            "max_concurrent_jobs"
        ]
        != 4
    )


def test_target_concurrency_rejects_invalid_value(client: TestClient) -> None:
    project = client.post("/api/projects", json={"name": "Concurrency Co 2"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "a.example.com", "base_url": "https://a.example.com/"},
    ).json()

    response = client.put(
        f"/api/projects/{project['id']}/targets/{target['id']}/concurrency",
        json={"max_concurrent_jobs": 0},
    )

    assert response.status_code == 422  # Pydantic ge=1 validation


def test_target_fuzz_defaults_round_trip_is_scoped_to_that_target(client: TestClient) -> None:
    project, target_a = _make_project_and_target(client, name="Fuzz Defaults Co")
    target_b = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "b.example.com", "base_url": "https://b.example.com/"},
    ).json()
    defaults_url = f"/api/projects/{project['id']}/targets/{target_a['id']}/fuzz-defaults"

    # Nothing saved yet -- {}, not a 404/error, so the frontend never needs
    # a special case for "brand new target".
    assert client.get(defaults_url).json() == {}

    body = {
        "extensions": ["php", "html"], "threads": 20, "force_extensions": True,
        "headers": {"Authorization": "Bearer abc"}, "pause_on_status": [429],
        "extra_args": "--exclude-status 404", "authenticated": True,
    }
    saved = client.put(defaults_url, json=body).json()
    assert saved["extensions"] == ["php", "html"]
    assert saved["threads"] == 20

    got = client.get(defaults_url).json()
    assert got["extensions"] == ["php", "html"]
    assert got["headers"] == {"Authorization": "Bearer abc"}
    assert got["authenticated"] is True

    # A different target's own defaults are untouched.
    other_url = f"/api/projects/{project['id']}/targets/{target_b['id']}/fuzz-defaults"
    assert client.get(other_url).json() == {}


def test_output_filename_template_settings_round_trip(client: TestClient) -> None:
    got = client.get("/api/settings/output-filename-template").json()
    assert "{target}" in got["template"]
    assert got["example"].endswith(".txt")

    updated = client.put(
        "/api/settings/output-filename-template", json={"template": "{path}_{target}"}
    ).json()
    assert updated["template"] == "{path}_{target}"
    assert updated["example"].endswith(".txt")


def test_port_settings_round_trip(client: TestClient) -> None:
    got = client.get("/api/settings/port").json()
    assert got["configured_port"] is None
    assert got["default_port"] == 58947

    updated = client.put("/api/settings/port", json={"port": 9999}).json()
    assert updated["configured_port"] == 9999
    assert updated["default_port"] == 58947

    again = client.get("/api/settings/port").json()
    assert again["configured_port"] == 9999

    cleared = client.put("/api/settings/port", json={"port": None}).json()
    assert cleared["configured_port"] is None


def test_port_settings_rejects_an_out_of_range_port(client: TestClient) -> None:
    response = client.put("/api/settings/port", json={"port": 70000})

    assert response.status_code == 422


def test_default_extensions_fallback_settings_round_trip(client: TestClient) -> None:
    got = client.get("/api/settings/default-extensions-fallback").json()
    assert got["configured_value"] is None
    assert got["dirsearch_default"] == "php,asp,aspx,jsp,html,htm"

    none_mode = client.put("/api/settings/default-extensions-fallback", json={"value": ""}).json()
    assert none_mode["configured_value"] == ""

    custom = client.put(
        "/api/settings/default-extensions-fallback", json={"value": "bak,old"}
    ).json()
    assert custom["configured_value"] == "bak,old"

    again = client.get("/api/settings/default-extensions-fallback").json()
    assert again["configured_value"] == "bak,old"

    cleared = client.put("/api/settings/default-extensions-fallback", json={"value": None}).json()
    assert cleared["configured_value"] is None


def test_wordlists_dir_setting_round_trip_and_drives_the_picker(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import wordlist_service

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "bundled.txt").write_text("a\n")
    monkeypatch.setattr(wordlist_service, "WORDLISTS_DIR", bundled)
    mine = tmp_path / "my lists"
    (mine / "sub").mkdir(parents=True)
    (mine / "sub" / "mine.txt").write_text("b\n")

    got = client.get("/api/settings/wordlists-dir").json()
    assert got == {"configured_dir": None, "default_dir": str(bundled)}
    assert [c["name"] for c in client.get("/api/wordlists").json()["children"]] == ["bundled.txt"]

    updated = client.put("/api/settings/wordlists-dir", json={"dir": str(mine)}).json()
    assert updated["configured_dir"] == str(mine)
    tree = client.get("/api/wordlists").json()
    assert [c["name"] for c in tree["children"]] == ["sub"]
    assert tree["children"][0]["children"][0]["name"] == "mine.txt"

    cleared = client.put("/api/settings/wordlists-dir", json={"dir": ""}).json()
    assert cleared["configured_dir"] is None
    assert [c["name"] for c in client.get("/api/wordlists").json()["children"]] == ["bundled.txt"]


def test_wordlists_dir_setting_rejects_a_missing_folder(client: TestClient, tmp_path: Path) -> None:
    response = client.put("/api/settings/wordlists-dir", json={"dir": str(tmp_path / "nope")})

    assert response.status_code == 400
    assert client.get("/api/settings/wordlists-dir").json()["configured_dir"] is None


def test_wordlist_tree_survives_a_symlink_loop(tmp_path: Path) -> None:
    from backend.services import wordlist_service

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x\n")
    try:
        (tmp_path / "a" / "loop").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")

    tree = wordlist_service._build_tree(tmp_path, set())

    assert tree["children"][0]["name"] == "a"


def _fake_exe(tmp_path: Path, name: str = "firefox.exe") -> Path:
    # A real .exe starts with the "MZ" DOS header -- browser_service now
    # validates a custom path against that before accepting it, so a fake
    # browser fixture has to look the part, not just exist and be named right.
    if sys.platform != "win32":
        # POSIX validation wants the execute bit plus an ELF/shebang header.
        path = tmp_path / name.removesuffix(".exe")
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path
    path = tmp_path / name
    path.write_bytes(b"MZ" + b"\x00" * 62)
    return path


def test_browser_settings_round_trip(client: TestClient, tmp_path: Path) -> None:
    fake_browser = _fake_exe(tmp_path)

    got = client.get("/api/settings/browser").json()
    assert got["configured_path"] is None

    updated = client.put("/api/settings/browser", json={"browser_path": str(fake_browser)}).json()
    assert updated["configured_path"] == str(fake_browser)

    again = client.get("/api/settings/browser").json()
    assert again["configured_path"] == str(fake_browser)
    assert again["effective_path"] == str(fake_browser)


def test_browser_settings_rejects_a_path_that_is_not_a_real_executable(
    client: TestClient, tmp_path: Path
) -> None:
    not_an_exe = tmp_path / "notes.txt"
    not_an_exe.write_text("this is not a browser")

    response = client.put("/api/settings/browser", json={"browser_path": str(not_an_exe)})

    assert response.status_code == 400
    assert client.get("/api/settings/browser").json()["configured_path"] is None


def test_browser_settings_detected_list_only_has_real_finds(client: TestClient) -> None:
    detected = client.get("/api/settings/browser").json()["detected"]

    for browser in detected:
        assert set(browser.keys()) == {"name", "path"}
        assert browser["path"]


def test_open_url_rejects_non_http_schemes(client: TestClient) -> None:
    response = client.post("/api/open-url", json={"url": "file:///etc/passwd"})

    assert response.status_code == 400


def test_open_url_launches_the_configured_browser(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_browser = _fake_exe(tmp_path)
    client.put("/api/settings/browser", json={"browser_path": str(fake_browser)})

    calls = []
    from backend.services import browser_service

    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda argv: calls.append(argv))

    response = client.post("/api/open-url", json={"url": "https://example.com/admin"})

    assert response.status_code == 200
    assert calls == [[str(fake_browser), "https://example.com/admin"]]


def test_open_url_returns_a_clear_error_when_nothing_can_be_launched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.services import browser_service

    # This machine may genuinely have a real browser installed -- without
    # neutralizing detection too, browser_path would resolve to a real
    # firefox.exe and this test would actually launch it via subprocess.Popen.
    monkeypatch.setattr(browser_service, "default_browser_path", lambda: None)
    monkeypatch.setattr(browser_service.webbrowser, "open", lambda url: False)

    response = client.post("/api/open-url", json={"url": "https://example.com/admin"})

    assert response.status_code == 400
    assert "Settings" in response.json()["detail"]


def test_targets_list_reports_is_fuzzing(client: TestClient, tmp_path: Path) -> None:
    # Slow the engine down deliberately -- the default `client` fixture's
    # zero-delay mock can finish a job before this test gets to check the
    # "busy" state, which would make the assertion flaky.
    app.state.job_manager = JobManager(
        MockDirsearchEngine(every=1, delay=0.05), event_bus=EventBus()
    )

    project = client.post("/api/projects", json={"name": "Client A"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()

    idle = client.get(f"/api/projects/{project['id']}/targets").json()
    assert idle[0]["is_fuzzing"] is False

    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/admin", "wordlist_files": [_wordlist(tmp_path, count=200)]},
    ).json()

    busy = client.get(f"/api/projects/{project['id']}/targets").json()
    assert busy[0]["is_fuzzing"] is True
    # Targets grid's "currently fuzzing" dot hover: path + % per running job.
    assert len(busy[0]["running_jobs"]) == 1
    assert busy[0]["running_jobs"][0]["path"] == "/admin"
    assert 0 <= busy[0]["running_jobs"][0]["percent"] <= 100

    client.post(f"/api/jobs/{job['id']}/stop")
    _wait_for_terminal(client, job["id"])

    idle_again = client.get(f"/api/projects/{project['id']}/targets").json()
    assert idle_again[0]["is_fuzzing"] is False
    assert idle_again[0]["running_jobs"] == []


def _make_project_and_target(client: TestClient, name: str = "Client A") -> tuple[dict, dict]:
    project = client.post("/api/projects", json={"name": name}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "api.example.com", "base_url": "https://api.example.com/"},
    ).json()
    return project, target


def test_tree_add_path_and_dedupe(client: TestClient) -> None:
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"

    # A trailing slash is preserved exactly as sent (not stripped/merged
    # with the no-slash form -- see tree_service.normalize_path).
    node = client.post(tree_url, json={"path": "/api/admin/"}).json()
    assert node["path"] == "/api/admin/"

    # The exact same path again must not duplicate.
    client.post(tree_url, json={"path": "/api/admin/"})

    # "/api" is a real node too now -- it was implied by "/api/admin/" and is part of the tree.
    paths = {n["path"] for n in client.get(tree_url).json() if n["path"] != "/"}
    assert paths == {"/api", "/api/admin/"}
    admin = next(n for n in client.get(tree_url).json() if n["path"] == "/api/admin/")
    assert len(admin["discoveries"]) == 2


def test_tree_add_from_result_status_and_interesting_note(client: TestClient) -> None:
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"

    node = client.post(
        f"{tree_url}/from-result",
        json={"path": "/api/debug", "status": 403, "size": "120B", "job_id": "job1"},
    ).json()
    assert node["path"] == "/api/debug"

    updated = client.patch(
        f"{tree_url}/{node['id']}",
        json={"interesting": True, "note": "Revisar autenticacion."},
    ).json()
    assert updated["interesting"] is True
    assert updated["note"] == "Revisar autenticacion."
    assert updated["status"]["status"] == 403


def test_tree_add_from_result_accepts_no_status(client: TestClient) -> None:
    # A synthetic ancestor segment in Results (an implied path never itself
    # discovered by dirsearch) has no real status -- "Add to tree" on one
    # must not 422.
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"

    response = client.post(f"{tree_url}/from-result", json={"path": "/api/internal"})
    assert response.status_code == 200
    assert response.json()["path"] == "/api/internal"


def test_tree_delete_node(client: TestClient) -> None:
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"
    node = client.post(tree_url, json={"path": "/old"}).json()

    response = client.delete(f"{tree_url}/{node['id']}")
    assert response.status_code == 204
    # The root ("/") auto-creates and is never deletable -- only "/old" is gone.
    assert [n["path"] for n in client.get(tree_url).json()] == ["/"]


def test_tree_delete_subtree_cascades_a_synthetic_ancestor_by_path(client: TestClient) -> None:
    # A synthetic ancestor (e.g. "/path1" implied only by the deeper
    # "/path1/path2/test" actually being added) has no id of its own to hit
    # DELETE /{node_id} with -- this endpoint deletes by path instead,
    # cascading to every real node at or under it.
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"
    client.post(tree_url, json={"path": "/path1/path2/test"})
    client.post(tree_url, json={"path": "/path1-other"})

    response = client.delete(f"{tree_url}/by-path/subtree", params={"path": "/path1"})
    assert response.status_code == 204

    remaining = {n["path"] for n in client.get(tree_url).json()}
    assert remaining == {"/", "/path1-other"}


def test_tree_delete_subtree_is_a_noop_when_nothing_real_exists_there(client: TestClient) -> None:
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"

    response = client.delete(f"{tree_url}/by-path/subtree", params={"path": "/nothing/here"})
    assert response.status_code == 204
    assert [n["path"] for n in client.get(tree_url).json()] == ["/"]


def test_tree_root_is_always_present_and_undeletable(client: TestClient) -> None:
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"

    nodes = client.get(tree_url).json()
    assert nodes[0]["path"] == "/"

    response = client.delete(f"{tree_url}/{nodes[0]['id']}")
    assert response.status_code == 400


def test_tree_missing_node_returns_404(client: TestClient) -> None:
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"

    response = client.patch(f"{tree_url}/does-not-exist", json={"interesting": True})
    assert response.status_code == 404


def test_tree_isolated_between_targets(client: TestClient) -> None:
    project, target_a = _make_project_and_target(client)
    target_b = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "admin.example.com", "base_url": "https://admin.example.com/"},
    ).json()

    client.post(f"/api/projects/{project['id']}/targets/{target_a['id']}/tree", json={"path": "/a"})

    # 2: "/a" plus the always-present root.
    assert len(client.get(f"/api/projects/{project['id']}/targets/{target_a['id']}/tree").json()) == 2
    # target_b's tree is untouched by target_a's -- just its own auto-created root.
    assert [n["path"] for n in client.get(f"/api/projects/{project['id']}/targets/{target_b['id']}/tree").json()] == ["/"]


def test_results_new_marker_clears_after_mark_seen(client: TestClient, tmp_path: Path) -> None:
    project, target = _make_project_and_target(client)
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path)]},
    ).json()
    _wait_for_terminal(client, job["id"])

    # Mock engine writes no report file -- write one directly, as a real
    # dirsearch run would, to exercise the on-disk "already seen" endpoint.
    base = layout.job_output_base_for(
        project["id"], target["id"], job["id"], job["output_stem"], job["output_flat"]
    )
    write_json_atomic(
        base.parent / f"{base.name}.json",
        {"results": [{"url": "https://api.example.com/admin", "status": 403}]},
    )

    results_url = f"/api/projects/{project['id']}/targets/{target['id']}/results"
    results = client.get(results_url).json()
    assert results[0]["is_new"] is True

    mark_seen_response = client.post(f"{results_url}/mark-seen", json={"keys": [results[0]["seen_key"]]})
    assert mark_seen_response.status_code == 200

    results_after = client.get(results_url).json()
    assert results_after[0]["is_new"] is False


def test_mark_seen_only_clears_the_given_keys_not_every_result(client: TestClient, tmp_path: Path) -> None:
    # Real, reported requirement: a result scrolled past, or hidden behind a
    # collapsed parent, must stay NEW even after leaving Results -- opening
    # the tab (or the old mark-seen-everything behavior) must not clear it.
    project, target = _make_project_and_target(client)
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path)]},
    ).json()
    _wait_for_terminal(client, job["id"])
    base = layout.job_output_base_for(
        project["id"], target["id"], job["id"], job["output_stem"], job["output_flat"]
    )
    write_json_atomic(
        base.parent / f"{base.name}.json",
        {
            "results": [
                {"url": "https://api.example.com/admin", "status": 403},
                {"url": "https://api.example.com/debug", "status": 200},
            ]
        },
    )

    results_url = f"/api/projects/{project['id']}/targets/{target['id']}/results"
    results = client.get(results_url).json()
    # Only the first result was ever actually painted on screen this visit.
    client.post(f"{results_url}/mark-seen", json={"keys": [results[0]["seen_key"]]})

    results_after = client.get(results_url).json()
    assert results_after[0]["is_new"] is False
    assert results_after[1]["is_new"] is True


def test_hide_rules_survive_a_fresh_read_and_are_per_target(client: TestClient) -> None:
    # Real, reported bug: hide rules lived only in the browser, so a relaunch on
    # a different port (or a fresh profile) silently lost every rule.
    project, target = _make_project_and_target(client)
    other = client.post(
        f"/api/projects/{project['id']}/targets", json={"name": "other", "base_url": "https://other.example.com/"}
    ).json()
    rules_url = f"/api/projects/{project['id']}/targets/{target['id']}/results/hide-rules"

    saved = client.put(rules_url, json={"rules": [{"status": "404", "size": "", "parentPath": "/admin", "depth": 0}]})
    assert saved.status_code == 200
    assert client.get(rules_url).json() == [{"status": "404", "size": "", "parentPath": "/admin", "depth": 0}]

    other_url = f"/api/projects/{project['id']}/targets/{other['id']}/results/hide-rules"
    assert client.get(other_url).json() == []


def test_ui_prefs_round_trip_and_null_clears(client: TestClient) -> None:
    # UI preferences used to live in localStorage (per origin, so per port).
    assert client.get("/api/settings/ui-prefs").json() == {}
    client.put("/api/settings/ui-prefs", json={"key": "collapsed:tree/p/t", "value": ["/admin"]})
    client.put("/api/settings/ui-prefs", json={"key": "defaultWordlist", "value": {"path": "x.txt"}})
    assert client.get("/api/settings/ui-prefs").json() == {
        "collapsed:tree/p/t": ["/admin"],
        "defaultWordlist": {"path": "x.txt"},
    }
    client.put("/api/settings/ui-prefs", json={"key": "defaultWordlist", "value": None})
    assert "defaultWordlist" not in client.get("/api/settings/ui-prefs").json()
