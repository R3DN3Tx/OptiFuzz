"""The API only answers requests that really come from OptiFuzz's own page on this machine
(DNS rebinding and cross-site requests from a web page must not reach it)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app

LOCAL = "http://127.0.0.1:8765"


@pytest.fixture()
def local(workspace_root) -> TestClient:
    return TestClient(app, base_url=LOCAL)


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "localhost", "[::1]:8765", "LOCALHOST:1"])
def test_loopback_hosts_are_served(workspace_root, host: str) -> None:
    client = TestClient(app, base_url=LOCAL)
    assert client.get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:8765", "127.0.0.1.evil.example", "localhost.evil.example:8765",
    "10.0.0.5:8765", "0.0.0.0:8765", "testserver", "127.0.0.1@evil.example", "",
])
def test_foreign_host_is_refused_even_for_health_and_static(workspace_root, host: str) -> None:
    client = TestClient(app, base_url=LOCAL)
    for path in ("/health", "/api/projects", "/", "/static/app.js"):
        assert client.get(path, headers={"Host": host}).status_code == 403, (host, path)


def test_dns_rebinding_cannot_read_or_write(local: TestClient) -> None:
    rebound = {"Host": "attacker.example:8765", "Origin": "http://attacker.example:8765"}
    assert local.get("/api/projects", headers=rebound).status_code == 403
    assert local.post("/api/projects", json={"name": "x"}, headers=rebound).status_code == 403
    assert local.put("/api/settings/browser", json={"browser_path": "C:/Windows/System32/mshta.exe"},
                     headers=rebound).status_code == 403
    assert local.get("/api/projects").json() == []  # nothing was created


@pytest.mark.parametrize("origin", ["http://evil.example", "https://127.0.0.1:8765", "null",
                                    "http://127.0.0.1:9999", "http://localhost:3000", "file://"])
def test_foreign_origin_is_refused(local: TestClient, origin: str) -> None:
    assert local.post("/api/projects", json={"name": "csrf"}, headers={"Origin": origin}).status_code == 403
    assert local.get("/api/projects").json() == []


def test_own_origin_is_accepted(local: TestClient) -> None:
    assert local.post("/api/projects", json={"name": "mine"}, headers={"Origin": LOCAL}).status_code == 200
    assert local.get("/api/projects", headers={"Origin": "http://localhost:8765", "Host": "localhost:8765"}).status_code == 200


@pytest.mark.parametrize("site,expected", [("same-origin", 200), ("none", 200), ("cross-site", 403), ("same-site", 403)])
def test_sec_fetch_site_on_api(local: TestClient, site: str, expected: int) -> None:
    assert local.get("/api/projects", headers={"Sec-Fetch-Site": site}).status_code == expected


def test_a_cross_site_link_may_still_open_the_page(local: TestClient) -> None:
    assert local.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
