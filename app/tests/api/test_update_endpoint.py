import pytest

from backend.services import update_service


def test_apply_endpoint_pauses_jobs_only_when_the_update_can_go_ahead(monkeypatch: pytest.MonkeyPatch, client) -> None:
    paused: list[int] = []
    monkeypatch.setattr(client.app.state.job_manager, "pause_all", lambda: paused.append(1) or 2)
    ok = {"current": "1.0.1", "updated": True, "restart_required": True}

    monkeypatch.setattr(update_service, "check", lambda: {"can_update": True})
    monkeypatch.setattr(update_service, "apply", lambda: ok)
    body = client.post("/api/update/apply").json()
    assert body["paused_jobs"] == 2 and body["updated"] is True

    paused.clear()
    monkeypatch.setattr(update_service, "check", lambda: {"can_update": False})

    def refuse() -> dict:
        raise update_service.UpdateError("local changes")

    monkeypatch.setattr(update_service, "apply", refuse)
    response = client.post("/api/update/apply")
    assert response.status_code == 400 and not paused


def test_restart_endpoint_refuses_outside_the_desktop_app(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setattr(update_service, "_restart_close_window", None)
    response = client.post("/api/update/restart")
    assert response.status_code == 400 and "desktop app" in response.json()["detail"]


def test_restart_endpoint_triggers_the_restart(monkeypatch: pytest.MonkeyPatch, client) -> None:
    called: list[int] = []
    monkeypatch.setattr(update_service, "restart", lambda: called.append(1))
    assert client.post("/api/update/restart").json() == {"restarting": True}
    assert called == [1]
