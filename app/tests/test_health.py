from fastapi.testclient import TestClient

from backend.main import app


def test_health_returns_ok() -> None:
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
