import io
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient


def _wordlist(tmp_path: Path, count: int = 5) -> str:
    path = tmp_path / "words.txt"
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


def _make_project_and_target(client: TestClient) -> tuple[dict, dict]:
    project = client.post("/api/projects", json={"name": "Client Export"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "export.example.com", "base_url": "https://export.example.com/"},
    ).json()
    return project, target


def test_export_target_returns_downloadable_html(client: TestClient, tmp_path: Path) -> None:
    project, target = _make_project_and_target(client)
    tree_url = f"/api/projects/{project['id']}/targets/{target['id']}/tree"

    node = client.post(tree_url, json={"path": "/admin"}).json()
    client.patch(f"{tree_url}/{node['id']}", json={"interesting": True, "note": "Check this."})

    job = client.post(
        f"/api/projects/{project['id']}/targets/{target['id']}/jobs",
        json={"path": "/", "wordlist_files": [_wordlist(tmp_path)]},
    ).json()
    _wait_for_terminal(client, job["id"])

    response = client.get(f"/api/projects/{project['id']}/targets/{target['id']}/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" in response.headers["content-disposition"]
    assert "client-export_export-example-com_OptiFuzz_Report.html" in response.headers["content-disposition"]

    body = response.text
    # No external requests/assets -- spec §61/§64 (must open from file:///
    # with no backend running).
    assert "http://" not in body.replace("https://export.example.com", "")
    assert "cdn." not in body
    assert "<script src=" not in body
    # The Target's own data made it into the embedded JSON, not just the
    # page shell.
    assert '"path": "/admin"' in body
    assert "Check this." in body
    assert project["id"] in body
    assert target["id"] in body


def test_export_all_targets_bundles_every_target_into_one_zip(client: TestClient) -> None:
    project, target_a = _make_project_and_target(client)
    target_b = client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "second.example.com", "base_url": "https://second.example.com/"},
    ).json()

    response = client.get(f"/api/projects/{project['id']}/export-all")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "client-export_Optifuzz_Reports.zip" in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert "client-export_export-example-com_OptiFuzz_Report.html" in names
    assert "client-export_second-example-com_OptiFuzz_Report.html" in names
    for name in names:
        assert archive.read(name).decode("utf-8").startswith("<!doctype html>")

    del target_a, target_b  # only their creation matters here


def test_export_all_targets_disambiguates_duplicate_target_names(client: TestClient) -> None:
    # Distinct names ("same name" vs "Same-Name") that create_target's own
    # uniqueness check allows (it compares .lower(), space != hyphen) but
    # that slugify() collapses to the identical slug -- the real scenario
    # export_all_targets_zip's own filename disambiguation guards against.
    project = client.post("/api/projects", json={"name": "Dup Project"}).json()
    client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "same name", "base_url": "https://one.example.com/"},
    )
    client.post(
        f"/api/projects/{project['id']}/targets",
        json={"name": "Same-Name", "base_url": "https://two.example.com/"},
    )

    response = client.get(f"/api/projects/{project['id']}/export-all")

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(archive.namelist())
    assert names == [
        "dup-project_same-name_OptiFuzz_Report-2.html",
        "dup-project_same-name_OptiFuzz_Report.html",
    ]


def test_export_has_tab_navigation_and_search_filter_controls(client: TestClient) -> None:
    # UI-parity follow-up (2026-09-23): the export used to be three
    # sections stacked on one page with no navigation/search/filter at
    # all -- now mirrors the live app's own JOBS/RESULTS/TREE tabs, plus
    # the search boxes and Interesting/Fuzzed filter toggles.
    project, target = _make_project_and_target(client)

    body = client.get(f"/api/projects/{project['id']}/targets/{target['id']}/export").text

    assert 'data-tab="jobs"' in body
    assert 'data-tab="results"' in body
    assert 'data-tab="tree"' in body
    assert 'id="results-search"' in body
    assert 'id="tree-search"' in body
    assert 'id="tree-interesting-only"' in body
    assert 'id="tree-fuzzed-only"' in body
    assert 'id="results-fuzzed-only"' in body
    # Hide rules (status/size/parent-path/depth) DOES ship in the export
    # (explicit request, 2026-09-23) -- its own "+ Hide" button is a
    # legitimate type="submit" (adding a client-side-only filter rule, not
    # launching anything), so the "no fuzz-launching affordance" check
    # below has to name the actual Fuzz button specifically rather than
    # ban every submit button in the page.
    assert 'id="results-hide-form"' in body
    assert 'id="results-hide-chips"' in body
    # No fuzz-launching affordance -- a static export has no backend to
    # launch/pause/stop a job against. Checked as a rendered tag boundary,
    # not a bare substring: the page's own embedded copy of frontend/
    # styles.css (see export_service.py's pixel-parity docstring) has a
    # source comment mentioning "New Fuzz" by name, which a plain substring
    # check would false-positive on.
    assert ">New Fuzz<" not in body
    assert ">Fuzz</button>" not in body


def test_export_target_with_empty_tree_and_no_jobs(client: TestClient) -> None:
    # Must not crash/500 on a brand-new target with nothing in it yet.
    project, target = _make_project_and_target(client)

    response = client.get(f"/api/projects/{project['id']}/targets/{target['id']}/export")

    assert response.status_code == 200
    assert "No paths in the tree." in response.text
    assert "No jobs." in response.text


def test_export_missing_target_404s(client: TestClient) -> None:
    project = client.post("/api/projects", json={"name": "Client X"}).json()

    response = client.get(f"/api/projects/{project['id']}/targets/does-not-exist/export")

    assert response.status_code == 404


def test_exports_embed_the_saved_hide_rules_and_results_filters(client: TestClient) -> None:
    # Whatever the app has saved for the target travels in EVERY export
    # (single, .zip, combined) -- never depends on what the browser sends.
    project, target = _make_project_and_target(client)
    pid, tid = project["id"], target["id"]
    client.put(
        f"/api/projects/{pid}/targets/{tid}/results/hide-rules",
        json={"rules": [{"status": "404", "size": "", "parentPath": "", "depth": 0}]},
    )
    client.put(
        "/api/settings/ui-prefs",
        json={"key": f"results/{pid}/{tid}", "value": {"search": "needle-q", "fuzzedOnly": True, "statuses": "200,301"}},
    )

    single = client.get(f"/api/projects/{pid}/targets/{tid}/export").text
    combined = client.get(f"/api/projects/{pid}/export-all-combined").text
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/export-all").content))
    zipped = archive.read(archive.namelist()[0]).decode("utf-8")

    for body in (single, combined, zipped):
        assert '"search": "needle-q"' in body
        assert '"statuses": "200,301"' in body
        assert '"status": "404"' in body
