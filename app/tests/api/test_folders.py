from __future__ import annotations

from fastapi.testclient import TestClient

from backend.persistence import layout


def _folder(client: TestClient, name: str, parent_id: str = "") -> dict:
    response = client.post("/api/folders", json={"name": name, "parent_id": parent_id})
    assert response.status_code == 200, response.text
    return response.json()


def _project(client: TestClient, name: str, folder_id: str = "") -> dict:
    response = client.post("/api/projects", json={"name": name, "folder_id": folder_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_create_list_and_nest_folders(client: TestClient) -> None:
    clients = _folder(client, "Clients")
    acme = _folder(client, "Acme", clients["id"])

    listed = {f["id"]: f for f in client.get("/api/folders").json()}
    assert listed[clients["id"]]["parent_id"] == ""
    assert listed[acme["id"]]["parent_id"] == clients["id"]


def test_folder_names_are_unique_per_parent_only(client: TestClient) -> None:
    a = _folder(client, "A")
    _folder(client, "Same", a["id"])
    assert client.post("/api/folders", json={"name": "same", "parent_id": a["id"]}).status_code == 409
    _folder(client, "Same")  # another level: allowed


def test_a_folder_needs_an_existing_parent(client: TestClient) -> None:
    assert client.post("/api/folders", json={"name": "X", "parent_id": "nope-12345678"}).status_code == 404


def test_projects_remember_their_folder_and_can_be_moved(client: TestClient) -> None:
    one = _folder(client, "One")
    two = _folder(client, "Two")
    project = _project(client, "Proj", one["id"])
    assert project["folder_id"] == one["id"]

    moved = client.post(f"/api/projects/{project['id']}/move", json={"folder_id": two["id"]})
    assert moved.status_code == 200 and moved.json()["folder_id"] == two["id"]
    assert client.get(f"/api/projects/{project['id']}").json()["folder_id"] == two["id"]

    back = client.post(f"/api/projects/{project['id']}/move", json={"folder_id": ""})
    assert back.json()["folder_id"] == ""
    assert client.post(f"/api/projects/{project['id']}/move", json={"folder_id": "nope-12345678"}).status_code == 404


def test_moving_a_project_does_not_move_its_files(client: TestClient) -> None:
    folder = _folder(client, "F")
    project = _project(client, "Stays")
    before = layout.project_dir(project["id"])
    client.post(f"/api/projects/{project['id']}/move", json={"folder_id": folder["id"]})
    assert before.is_dir() and (before / "project.json").is_file()


def test_a_folder_cannot_be_moved_into_itself_or_its_own_subfolder(client: TestClient) -> None:
    top = _folder(client, "Top")
    child = _folder(client, "Child", top["id"])
    grandchild = _folder(client, "Grandchild", child["id"])

    assert client.post(f"/api/folders/{top['id']}/move", json={"folder_id": top["id"]}).status_code == 400
    assert client.post(f"/api/folders/{top['id']}/move", json={"folder_id": grandchild["id"]}).status_code == 400

    moved = client.post(f"/api/folders/{grandchild['id']}/move", json={"folder_id": ""})
    assert moved.status_code == 200 and moved.json()["parent_id"] == ""


def test_rename_a_folder(client: TestClient) -> None:
    folder = _folder(client, "Old")
    renamed = client.patch(f"/api/folders/{folder['id']}", json={"name": "New"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "New"


def test_only_an_empty_folder_can_be_deleted(client: TestClient) -> None:
    folder = _folder(client, "Full")
    project = _project(client, "Inside", folder["id"])
    assert client.delete(f"/api/folders/{folder['id']}").status_code == 409

    client.post(f"/api/projects/{project['id']}/move", json={"folder_id": ""})
    sub = _folder(client, "Sub", folder["id"])
    assert client.delete(f"/api/folders/{folder['id']}").status_code == 409

    assert client.delete(f"/api/folders/{sub['id']}").status_code == 204
    assert client.delete(f"/api/folders/{folder['id']}").status_code == 204
    assert client.get("/api/folders").json() == []


def test_a_damaged_folder_file_does_not_hide_the_others(client: TestClient) -> None:
    good = _folder(client, "Good")
    bad = _folder(client, "Bad")
    layout.folder_file(bad["id"]).write_text("{ not json", encoding="utf-8")
    assert [f["id"] for f in client.get("/api/folders").json()] == [good["id"]]


def test_the_project_list_carries_how_many_targets_each_has(client: TestClient) -> None:
    full = _project(client, "Full")
    _project(client, "Empty")
    for name in ("One", "Two"):
        response = client.post(f"/api/projects/{full['id']}/targets", json={"name": name, "base_url": f"http://127.0.0.1:9/{name}"})
        assert response.status_code == 200, response.text

    counts = {p["name"]: p["target_count"] for p in client.get("/api/projects").json()}

    assert counts == {"Full": 2, "Empty": 0}
