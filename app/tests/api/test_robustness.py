"""Input validation, damaged-data tolerance and recovery, seen through the API."""
import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.persistence import layout
from backend.persistence.atomic import backup_path
from backend.services import notice_service

HEBREW = chr(0x5e9) + chr(0x5dc) + chr(0x5d5) + chr(0x5dd)


def _project(client: TestClient, name: str = "Client A") -> dict:
    return client.post("/api/projects", json={"name": name}).json()


def _target(client: TestClient, project_id: str, name: str = "api", url: str = "https://api.example.com/") -> dict:
    return client.post(f"/api/projects/{project_id}/targets", json={"name": name, "base_url": url}).json()


def _wordlist(tmp_path: Path) -> str:
    path = tmp_path / "words.txt"
    path.write_text("admin\napi\n", encoding="utf-8")
    return str(path)


# ---- names -------------------------------------------------------------------------------------------------

def test_names_in_any_language_are_accepted(client: TestClient) -> None:
    project = _project(client, HEBREW)
    assert project["name"] == HEBREW
    assert project["id"].startswith("x-")
    target = _target(client, project["id"], name=chr(0x4e2d) + chr(0x6587))
    assert target["name"] == chr(0x4e2d) + chr(0x6587)
    assert [p["name"] for p in client.get("/api/projects").json()] == [HEBREW]


def test_two_projects_with_different_non_latin_names_get_different_ids(client: TestClient) -> None:
    first = _project(client, HEBREW)
    second = _project(client, chr(0x4e2d) + chr(0x6587))
    assert first["id"] != second["id"]


@pytest.mark.parametrize("name", ["", "   ", "a" * 81, "a" + chr(0) + "b", "a" + chr(10) + "b"])
def test_bad_project_names_are_refused_with_a_message(client: TestClient, name: str) -> None:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 400
    assert "name" in response.json()["detail"].lower()


def test_the_longest_allowed_names_work_end_to_end(client: TestClient, tmp_path: Path) -> None:
    project = _project(client, "p" * 80)
    target = _target(client, project["id"], name="t" * 80)
    response = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/" + "seg/" * 20, "wordlist_files": [_wordlist(tmp_path)]},
    )
    assert response.status_code == 200
    # Long names must not make the folder path grow with them.
    assert len(str(layout.target_dir(project["id"], target["id"]))) - len(str(layout.workspace_root())) < 120


# ---- target URL --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "example.com", "ftp://example.com", "http://", "javascript:alert(1)", "http://x:99999", "http://x:0",
    "http://ex ample.com", "http://a" + chr(0) + "b", "https://" + "a" * 2000,
])
def test_a_bad_base_url_is_refused_when_the_target_is_added(client: TestClient, url: str) -> None:
    project = _project(client)
    response = client.post(f"/api/projects/{project['id']}/targets", json={"name": "t", "base_url": url})
    assert response.status_code == 400
    assert client.get(f"/api/projects/{project['id']}/targets").json() == []


@pytest.mark.parametrize("url", ["https://a.com", "http://127.0.0.1:8910/", "http://[::1]:80/x", "HTTP://UPPER.com/", "http://u:p@h/"])
def test_valid_base_urls_are_accepted(client: TestClient, url: str) -> None:
    project = _project(client)
    assert client.post(f"/api/projects/{project['id']}/targets", json={"name": "t", "base_url": url}).status_code == 200


# ---- job options -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("options", [
    {"threads": 501}, {"threads": 0}, {"timeout": -1}, {"timeout": 0}, {"delay": -1}, {"max_rate": -1},
    {"include_status_codes": ["abc"]}, {"exclude_status_codes": ["99"]}, {"proxy": "not a proxy"},
    {"cookie": "a=b\r\nX-Evil: 1"}, {"user_agent": "a\nb"}, {"headers": {"X-A": "a\r\nX-B: b"}},
    {"headers": {"": "v"}}, {"http_method": "po st"}, {"extensions": ["a b"]}, {"pause_on_status": [999]},
    {"extra_args": "a" + chr(0)},
])
def test_invalid_job_options_are_refused_before_a_job_exists(client: TestClient, tmp_path: Path, options: dict) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    response = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"wordlist_files": [_wordlist(tmp_path)], **options},
    )
    assert response.status_code == 422
    assert client.get(f"/api/projects/{project['id']}/targets/{target['id']}/jobs").json() == []


@pytest.mark.parametrize("options", [
    {"threads": 500}, {"proxy": "127.0.0.1:8080"}, {"proxy": "socks5://u:p@host:1080"}, {"http_method": "post"},
    {"include_status_codes": ["200-399", "404"]}, {"max_rate": 0}, {"cookie": "a=b; c=d"},
])
def test_valid_job_options_are_accepted(client: TestClient, tmp_path: Path, options: dict) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    response = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"wordlist_files": [_wordlist(tmp_path)], **options},
    )
    assert response.status_code == 200, response.text


def test_a_wordlist_with_only_blank_lines_and_comments_is_refused(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "blank.txt"
    path.write_text("\n  \n# only a comment\n\n", encoding="utf-8")
    project = _project(client)
    target = _target(client, project["id"])
    response = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs", json={"wordlist_files": [str(path)]},
    )
    assert response.status_code == 400
    assert "no words" in response.json()["detail"]


# ---- target settings that used to accept anything ---------------------------------------------------------

def test_fuzz_defaults_and_concurrency_need_a_real_target(client: TestClient) -> None:
    project = _project(client)
    base = f"/api/projects/{project['id']}/targets/nope"
    assert client.get(f"{base}/fuzz-defaults").status_code == 404
    assert client.put(f"{base}/fuzz-defaults", json={}).status_code == 404
    assert client.put(f"{base}/concurrency", json={"max_concurrent_jobs": 3}).status_code == 404
    assert client.get(f"{base}/concurrency").status_code == 404
    assert client.put(f"/api/projects/nope/targets/x/fuzz-defaults", json={}).status_code == 404


def test_concurrency_has_a_ceiling(client: TestClient) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    url = f"/api/projects/{project['id']}/targets/{target['id']}/concurrency"
    assert client.put(url, json={"max_concurrent_jobs": 10}).status_code == 200
    assert client.put(url, json={"max_concurrent_jobs": 11}).status_code == 422
    assert client.put(url, json={"max_concurrent_jobs": 0}).status_code == 422


def test_saved_fuzz_defaults_are_validated_like_a_fuzz(client: TestClient) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    url = f"/api/projects/{project['id']}/targets/{target['id']}/fuzz-defaults"
    assert client.put(url, json={"threads": -1}).status_code == 422
    assert client.put(url, json={"cookie": "a\nb"}).status_code == 422
    assert client.put(url, json={"threads": 20}).status_code == 200


def test_ui_prefs_are_bounded(client: TestClient) -> None:
    assert client.put("/api/settings/ui-prefs", json={"key": "", "value": 1}).status_code == 400
    assert client.put("/api/settings/ui-prefs", json={"key": "k" * 101, "value": 1}).status_code == 400
    assert client.put("/api/settings/ui-prefs", json={"key": "big", "value": "x" * 70000}).status_code == 400
    assert client.put("/api/settings/ui-prefs", json={"key": "ok", "value": {"a": [1]}}).status_code == 200


# ---- tree paths --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed, stored", [
    ("a", "/a"), ("/a/../b", "/b"), ("//", "/"), ("http://evil/x", "/x"), ("/a//b/./c", "/a/b/c"),
    ("/a/b/", "/a/b/"), ("/../..", "/"), ("/a?b=1#c", "/a?b=1#c"),
])
def test_tree_paths_are_normalised(client: TestClient, typed: str, stored: str) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    response = client.post(f"/api/projects/{project['id']}/targets/{target['id']}/tree", json={"path": typed})
    assert response.status_code == 200
    assert response.json()["path"] == stored


# ---- outputs mirror folder ---------------------------------------------------------------------------------

@pytest.mark.parametrize("folder", ["relative/folder", "a" + chr(0) + "b", "x" * 300])
def test_a_bad_mirror_folder_is_refused(client: TestClient, folder: str) -> None:
    project = _project(client)
    response = client.put(f"/api/projects/{project['id']}/outputs-mirror", json={"dir": folder})
    assert response.status_code == 400
    assert client.get(f"/api/projects/{project['id']}").json()["outputs_mirror_dir"] == ""


def test_a_good_mirror_folder_is_accepted_and_can_be_cleared(client: TestClient, tmp_path: Path) -> None:
    project = _project(client)
    url = f"/api/projects/{project['id']}/outputs-mirror"
    assert client.put(url, json={"dir": str(tmp_path / "mirror")}).status_code == 200
    assert client.put(url, json={"dir": ""}).json()["dir"] == ""


# ---- damaged data ------------------------------------------------------------------------------------------

def test_one_damaged_project_does_not_hide_the_others(client: TestClient) -> None:
    good = _project(client, "Good")
    bad = _project(client, "Bad")
    layout.project_file(bad["id"]).write_text("{ not json", encoding="utf-8")

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [good["id"]]
    assert _project(client, "Another").get("id")  # creating still works

    damaged = client.get("/api/damaged").json()
    assert [(d["kind"], d["project_id"]) for d in damaged] == [("project", bad["id"])]
    assert damaged[0]["file"] == "project.json" and "not valid JSON" in damaged[0]["reason"]


def test_reading_a_damaged_project_answers_409_with_what_is_needed_to_repair_it(client: TestClient) -> None:
    project = _project(client)
    layout.project_file(project["id"]).write_text("", encoding="utf-8")
    response = client.get(f"/api/projects/{project['id']}")
    assert response.status_code == 409
    assert response.json()["damaged"]["kind"] == "project"


def test_a_project_file_missing_fields_is_damaged_not_a_server_error(client: TestClient) -> None:
    project = _project(client)
    layout.project_file(project["id"]).write_text('{"id": "x"}', encoding="utf-8")
    assert client.get(f"/api/projects/{project['id']}").status_code == 409
    assert client.get("/api/projects").status_code == 200


def test_restoring_the_backup_brings_the_project_back(client: TestClient) -> None:
    project = _project(client, "Keep me")
    client.patch(f"/api/projects/{project['id']}", json={"name": "Renamed"})  # a second save makes the backup
    assert backup_path(layout.project_file(project["id"])).exists()
    layout.project_file(project["id"]).write_text("{ not json", encoding="utf-8")

    item = client.get("/api/damaged").json()[0]
    assert item["can_restore"] is True
    answer = client.post("/api/damaged/repair", json={**{k: item[k] for k in ("kind", "project_id")}, "action": "restore"})
    assert answer.status_code == 200
    assert client.get("/api/damaged").json() == []
    assert client.get(f"/api/projects/{project['id']}").json()["name"] == "Keep me"  # the last good save


def test_a_project_without_a_backup_can_be_rebuilt_from_its_folder(client: TestClient) -> None:
    project = _project(client, "Client A")
    target = _target(client, project["id"])
    layout.project_file(project["id"]).write_text("{ not json", encoding="utf-8")
    backup_path(layout.project_file(project["id"])).unlink(missing_ok=True)

    item = client.get("/api/damaged").json()[0]
    assert item["can_restore"] is False and item["can_rebuild"] is True
    assert client.post("/api/damaged/repair", json={"kind": "project", "project_id": project["id"], "action": "restore"}).status_code == 400
    assert client.post("/api/damaged/repair", json={"kind": "project", "project_id": project["id"], "action": "rebuild"}).status_code == 200

    rebuilt = client.get(f"/api/projects/{project['id']}").json()
    assert rebuilt["name"] == "client a"  # taken from the folder name
    assert [t["id"] for t in client.get(f"/api/projects/{project['id']}/targets").json()] == [target["id"]]  # nothing lost


def test_a_damaged_target_is_rebuilt_with_the_url_typed_again(client: TestClient) -> None:
    project = _project(client)
    target = _target(client, project["id"], name="api")
    layout.target_file(project["id"], target["id"]).write_text("[", encoding="utf-8")
    backup_path(layout.target_file(project["id"], target["id"])).unlink(missing_ok=True)

    assert client.get(f"/api/projects/{project['id']}/targets").json() == []
    item = client.get("/api/damaged", params={"project_id": project["id"]}).json()[0]
    assert item["kind"] == "target" and item["needs_base_url"] is True
    body = {"kind": "target", "project_id": project["id"], "target_id": target["id"], "action": "rebuild"}
    assert client.post("/api/damaged/repair", json={**body, "base_url": "example.com"}).status_code == 400  # URL is validated
    assert client.post("/api/damaged/repair", json={**body, "base_url": "https://api.example.com/"}).status_code == 200
    assert client.get(f"/api/projects/{project['id']}/targets/{target['id']}").json()["base_url"] == "https://api.example.com/"


def test_a_damaged_tree_can_be_restarted_empty(client: TestClient) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    client.post(f"/api/projects/{project['id']}/targets/{target['id']}/tree", json={"path": "/admin"})
    layout.tree_file(project["id"], target["id"]).write_text("[1,", encoding="utf-8")
    backup_path(layout.tree_file(project["id"], target["id"])).unlink(missing_ok=True)

    assert client.get(f"/api/projects/{project['id']}/targets/{target['id']}/tree").status_code == 409
    item = client.get("/api/damaged", params={"project_id": project["id"], "target_id": target["id"]}).json()[0]
    assert item["kind"] == "tree"
    body = {"kind": "tree", "project_id": project["id"], "target_id": target["id"], "action": "rebuild"}
    assert client.post("/api/damaged/repair", json=body).status_code == 200
    assert client.get(f"/api/projects/{project['id']}/targets/{target['id']}/tree").status_code == 200


def test_a_damaged_job_file_is_reported_and_can_be_taken_off_the_list(client: TestClient, tmp_path: Path) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs", json={"wordlist_files": [_wordlist(tmp_path)]},
    ).json()
    for _ in range(200):  # let the job finish: a running one would rewrite its own file over the damage
        if client.get(f"/api/jobs/{job['id']}").json()["status"] in ("COMPLETED", "FAILED", "STOPPED"):
            break
        time.sleep(0.02)
    job_file = layout.job_file(project["id"], target["id"], job["id"])
    job_file.write_text("{ broken", encoding="utf-8")
    backup_path(job_file).unlink(missing_ok=True)

    item = client.get("/api/damaged", params={"project_id": project["id"], "target_id": target["id"]}).json()[0]
    assert item["kind"] == "job" and item["job_id"] == job["id"]
    body = {"kind": "job", "project_id": project["id"], "target_id": target["id"], "job_id": job["id"], "action": "remove"}
    assert client.post("/api/damaged/repair", json=body).status_code == 200
    assert client.get("/api/damaged").json() == []
    assert not job_file.exists() and [p for p in job_file.parent.iterdir() if ".damaged-" in p.name]  # set aside, not deleted


def test_repair_refuses_ids_that_could_leave_the_workspace(client: TestClient) -> None:
    response = client.post("/api/damaged/repair", json={"kind": "project", "project_id": "../../x", "action": "restore"})
    assert response.status_code == 400


# ---- damaged settings, notices, ports -----------------------------------------------------------------------

def test_a_damaged_settings_file_is_restored_from_its_backup_and_the_user_is_told(client: TestClient, workspace_root: Path) -> None:
    notice_service.clear_notice("settings_file")
    client.put("/api/settings/ui-prefs", json={"key": "a", "value": 1})
    client.put("/api/settings/ui-prefs", json={"key": "b", "value": 2})  # second save: the backup holds a only
    (workspace_root / "app_settings.json").write_text("\xff\xfe garbage", encoding="utf-8")

    assert client.get("/api/settings/ui-prefs").json() == {"a": 1}
    notices = client.get("/api/notices").json()
    assert [n["id"] for n in notices] == ["settings_file"] and "restored" in notices[0]["message"]
    assert [p for p in workspace_root.iterdir() if p.name.startswith("app_settings.json.damaged-")]  # kept, not overwritten
    notice_service.clear_notice("settings_file")


def test_a_damaged_settings_file_without_backup_resets_to_defaults_and_keeps_the_file(client: TestClient, workspace_root: Path) -> None:
    notice_service.clear_notice("settings_file")
    (workspace_root / "app_settings.json").write_text("{ nope", encoding="utf-8")

    assert client.get("/api/settings/ui-prefs").json() == {}
    assert "reset" in client.get("/api/notices").json()[0]["message"]
    assert [p for p in workspace_root.iterdir() if p.name.startswith("app_settings.json.damaged-")]
    notice_service.clear_notice("settings_file")


def test_port_check_says_whether_a_port_is_free_and_what_would_replace_it(client: TestClient) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        answer = client.get("/api/settings/port/check", params={"port": port}).json()
    assert answer["free"] is False
    assert answer["alternative"] and answer["alternative"] != port

    assert client.get("/api/settings/port/check", params={"port": port}).json()["free"] is True  # released
    assert client.get("/api/settings/port/check", params={"port": 70000}).status_code == 400


# ---- second round: notices seen, running port, exports with damaged files --------------------------------

def test_a_notice_stops_being_listed_once_it_was_seen(client: TestClient) -> None:
    notice_service.set_notice("port", "busy")
    notice_service.set_notice("settings_file", "restored")
    assert {n["id"] for n in client.get("/api/notices").json()} == {"port", "settings_file"}

    client.post("/api/notices/settings_file/seen")
    assert [n["id"] for n in client.get("/api/notices").json()] == ["port"]
    client.post("/api/notices/port/seen")
    assert client.get("/api/notices").json() == []
    client.post("/api/notices/nope/seen")  # unknown ids are ignored
    notice_service.clear_notice("port")
    notice_service.clear_notice("settings_file")


def test_the_port_page_gets_the_port_that_is_really_in_use(client: TestClient) -> None:
    data = client.get("/api/settings/port").json()
    assert data["running_port"] == 8765  # what this client connected to, not the saved or default one
    assert data["default_port"] != data["running_port"]


def test_a_damaged_entry_carries_the_full_path_of_the_file(client: TestClient) -> None:
    project = _project(client)
    layout.project_file(project["id"]).write_text("{ nope", encoding="utf-8")
    item = client.get("/api/damaged").json()[0]
    assert item["file"] == "project.json"
    assert item["path"] == str(layout.project_file(project["id"]))


def test_an_export_still_works_when_the_tree_or_a_job_is_damaged_and_says_what_is_missing(client: TestClient, tmp_path: Path) -> None:
    project = _project(client)
    target = _target(client, project["id"])
    base = f"/api/projects/{project['id']}/targets/{target['id']}"
    job = client.post(f"{base}/jobs", json={"wordlist_files": [_wordlist(tmp_path)]}).json()
    for _ in range(200):
        if client.get(f"/api/jobs/{job['id']}").json()["status"] in ("COMPLETED", "FAILED", "STOPPED"):
            break
        time.sleep(0.02)
    client.post(f"{base}/tree", json={"path": "/admin"})
    layout.tree_file(project["id"], target["id"]).write_text("[", encoding="utf-8")

    page = client.get(f"{base}/export")
    assert page.status_code == 200
    embedded = page.text.split('id="export-data">', 1)[1].split("</script>", 1)[0]
    damaged = json.loads(embedded)["damaged"]
    assert [d["kind"] for d in damaged] == ["tree"] and damaged[0]["file"] == "tree.json" and "path" not in damaged[0]
    # The damaged-file notice names the file, never where it is on this computer. (Compared on the data as it is
    # embedded, not on the raw page: on Windows backslashes are escaped there and a plain search would prove nothing.)
    assert str(layout.tree_file(project["id"], target["id"])) not in json.dumps(json.loads(embedded)["damaged"])

    combined = client.get(f"/api/projects/{project['id']}/export-all-combined")
    assert combined.status_code == 200
    assert client.get(f"/api/projects/{project['id']}/export-all").status_code == 200


def _every_spelling(path: Path) -> list[str]:
    """The path as plain text, as JSON-escaped (backslashes doubled) and with forward slashes."""
    plain = str(path)
    return [plain, json.dumps(plain)[1:-1], plain.replace("\\", "/")]


def test_no_export_carries_a_path_of_this_computer(client: TestClient, tmp_path: Path) -> None:
    import io
    import zipfile

    project = _project(client)
    target = _target(client, project["id"])
    base = f"/api/projects/{project['id']}/targets/{target['id']}"
    job = client.post(f"{base}/jobs", json={"wordlist_files": [_wordlist(tmp_path)]}).json()
    for _ in range(200):
        if client.get(f"/api/jobs/{job['id']}").json()["status"] in ("COMPLETED", "FAILED", "STOPPED"):
            break
        time.sleep(0.02)
    client.post(f"{base}/tree", json={"path": "/"})

    zipped = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{project['id']}/export-all").content))
    pages = {
        "target": client.get(f"{base}/export").text,
        "combined": client.get(f"/api/projects/{project['id']}/export-all-combined").text,
        "zip": "".join(zipped.read(name).decode("utf-8") for name in zipped.namelist()),
    }
    folder = tmp_path.parent  # the folder holding the wordlist's own folder: any path of this machine goes through it
    for label, page in pages.items():
        assert "words.txt" in page, label  # the file NAME is still there
        for spelling in _every_spelling(tmp_path) + _every_spelling(folder):
            assert spelling not in page, f"{label} export carries {spelling}"


def test_local_paths_are_cut_down_to_file_names() -> None:
    from backend.services.export_service import _scrub_job_paths, _without_local_paths

    assert _without_local_paths(r"cannot read D:\lists\big.txt") == "cannot read big.txt"
    assert _without_local_paths("failed: /mnt/lists/common.txt now") == "failed: common.txt now"
    assert _without_local_paths("GET /admin/login returned 404") == "GET /admin/login returned 404"  # a URL path stays
    entry = {
        "wordlist_sources": [r"C:\data\w\a.txt", "/mnt/lists/b.txt"],
        "error_reason": r"no such file \\server\share\c.txt",
        "config": {"extra_args": ["--cert", "/etc/ssl/my.pem"], "threads": 5},
    }
    _scrub_job_paths(entry)
    assert entry["wordlist_sources"] == ["a.txt", "b.txt"]
    assert entry["error_reason"] == "no such file c.txt"
    assert entry["config"] == {"extra_args": ["--cert", "my.pem"], "threads": 5}
