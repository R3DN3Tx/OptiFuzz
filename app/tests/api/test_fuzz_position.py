import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.job_manager.wordlist_resolver import _around_word
from backend.persistence import layout
from backend.services import target_service


def _target(client: TestClient) -> tuple[str, str]:
    project = client.post("/api/projects", json={"name": f"Client {uuid.uuid4().hex[:6]}"}).json()
    target = client.post(
        f"/api/projects/{project['id']}/targets", json={"name": "t1", "base_url": "https://t1.local/"},
    ).json()
    return project["id"], target["id"]


def _words(tmp_path: Path) -> str:
    path = tmp_path / "words.txt"
    path.write_text("admin\n# comment\n\n/backup/\nstatic\n", encoding="utf-8")
    return str(path)


def _create(client: TestClient, tmp_path: Path, path: str, **extra):
    pid, tid = _target(client)
    response = client.post(
        f"/api/projects/{pid}/targets/{tid}/jobs",
        json={"path": path, "wordlist_files": [_words(tmp_path)], **extra},
    )
    return pid, tid, response


def test_a_fuzz_word_in_the_middle_of_the_path_puts_the_text_after_it_on_every_wordlist_line(
    client: TestClient, tmp_path: Path,
) -> None:
    pid, tid, response = _create(client, tmp_path, "/api/FUZZ/users")

    job = response.json()
    assert job["path"] == "/api/FUZZ/users"  # the template as typed: no "/" added
    assert job["fuzz_keyword"] == "FUZZ"
    assert job["config"]["url"] == "https://t1.local/api/"
    full = (layout.job_wordlist_dir(pid, tid, job["id"]) / "full.txt").read_text(encoding="utf-8").splitlines()
    # blank lines and comments stay (the line numbers must keep lining up for a resume)
    assert full == ["admin/users", "# comment", "", "backup/users", "static/users"]
    assert job["total_wordlist_requests"] == 3


def test_text_glued_to_the_fuzz_word_goes_before_each_word(client: TestClient, tmp_path: Path) -> None:
    pid, tid, response = _create(client, tmp_path, "/api/v1-FUZZ")

    job = response.json()
    assert job["config"]["url"] == "https://t1.local/api/"
    full = (layout.job_wordlist_dir(pid, tid, job["id"]) / "full.txt").read_text(encoding="utf-8").splitlines()
    assert full[0] == "v1-admin"


def test_force_extensions_is_ignored_when_the_path_has_a_fuzz_word(client: TestClient, tmp_path: Path) -> None:
    _pid, _tid, response = _create(client, tmp_path, "/FUZZ/index", force_extensions=True, extensions=["php"])

    assert response.json()["config"]["force_extensions"] is False


def test_a_path_without_the_fuzz_word_is_unchanged(client: TestClient, tmp_path: Path) -> None:
    _pid, _tid, response = _create(client, tmp_path, "/api", force_extensions=True)

    job = response.json()
    assert job["path"] == "/api/"
    assert job["fuzz_keyword"] == ""
    assert job["config"]["force_extensions"] is True


@pytest.mark.parametrize("path", ["/FUZZ/a/FUZZ", "/a?x=FUZZ&y=FUZZ", "/a?x=FUZZ#top"])
def test_a_fuzz_word_used_twice_or_with_a_fragment_is_refused(client: TestClient, tmp_path: Path, path: str) -> None:
    _pid, _tid, response = _create(client, tmp_path, path)

    assert response.status_code == 400


def test_the_fuzz_word_can_be_changed_in_settings_and_a_job_keeps_the_one_it_was_made_with(
    client: TestClient, tmp_path: Path,
) -> None:
    assert client.get("/api/settings/fuzz-keyword").json() == {"keyword": "FUZZ", "default": "FUZZ"}

    assert client.put("/api/settings/fuzz-keyword", json={"keyword": "XXX"}).json()["keyword"] == "XXX"
    _pid, _tid, response = _create(client, tmp_path, "/a/XXX/b")
    job = response.json()
    assert job["fuzz_keyword"] == "XXX"
    # the old word is now just text
    assert _create(client, tmp_path, "/a/FUZZ/b")[2].json()["fuzz_keyword"] == ""

    client.put("/api/settings/fuzz-keyword", json={"keyword": "OTHER"})
    stored = client.get(f"/api/jobs/{job['id']}").json()
    assert stored["fuzz_keyword"] == "XXX"

    assert client.put("/api/settings/fuzz-keyword", json={"keyword": ""}).json()["keyword"] == "FUZZ"


@pytest.mark.parametrize("word", ["ab", "a b", "a/b", "x" * 33, "FUZ?"])
def test_a_bad_fuzz_word_is_refused(client: TestClient, word: str) -> None:
    assert client.put("/api/settings/fuzz-keyword", json={"keyword": word}).status_code == 400


def test_a_fuzz_word_in_the_query_string_goes_after_the_question_mark_of_every_word(
    client: TestClient, tmp_path: Path,
) -> None:
    pid, tid, response = _create(client, tmp_path, "/search/?q=FUZZ&lang=en")

    job = response.json()
    assert job["path"] == "/search/?q=FUZZ&lang=en"
    assert job["config"]["url"] == "https://t1.local/search/"
    full = (layout.job_wordlist_dir(pid, tid, job["id"]) / "full.txt").read_text(encoding="utf-8").splitlines()
    # a value goes in exactly as it is (no slash stripped)
    assert full == ["?q=admin&lang=en", "# comment", "", "?q=/backup/&lang=en", "?q=static&lang=en"]


def test_a_fuzz_word_in_the_path_may_be_followed_by_a_query(client: TestClient, tmp_path: Path) -> None:
    pid, tid, response = _create(client, tmp_path, "/api/FUZZ?v=1")

    assert response.status_code == 200
    job = response.json()
    assert job["config"]["url"] == "https://t1.local/api/"
    full = (layout.job_wordlist_dir(pid, tid, job["id"]) / "full.txt").read_text(encoding="utf-8").splitlines()
    assert full[0] == "admin?v=1"


def test_results_of_a_query_fuzz_keep_the_query_in_their_path() -> None:
    from backend.models.job import FuzzJob, JobStatus
    from backend.services import results_service

    assert results_service.result_path("https://t1.local/search/?q=admin", True) == "/search/?q=admin"
    assert results_service.result_path("https://t1.local/search/?q=admin", False) == "/search/"
    job = FuzzJob(id="j", target_id="t", project_id="p", path="/search/?q=FUZZ", config={}, status=JobStatus.COMPLETED,
                  created_at="", fuzz_keyword="FUZZ")
    assert job.fuzz_in_query
    assert not FuzzJob(id="j", target_id="t", project_id="p", path="/a/FUZZ?q=1", config={}, status=JobStatus.COMPLETED,
                       created_at="", fuzz_keyword="FUZZ").fuzz_in_query


def test_the_fuzzed_place_of_a_job_with_the_fuzz_word_is_the_directory_the_word_sits_in() -> None:
    from backend.models.job import FuzzJob, JobStatus

    def place(path: str, keyword: str = "FUZZ") -> str:
        return FuzzJob(id="j", target_id="t", project_id="p", path=path, config={}, status=JobStatus.COMPLETED,
                       created_at="", fuzz_keyword=keyword).fuzzed_path

    assert place("/api/FUZZ/crash") == "/api/"
    assert place("/api/v1-FUZZ") == "/api/"
    assert place("/search/?q=FUZZ&lang=en") == "/search/"
    assert place("/search/?next=/a/FUZZ") == "/search/"
    assert place("/FUZZ") == "/"
    assert place("/admin/?lang=en", keyword="") == "/admin/"  # a normal job: its path without the query


def test_the_tree_node_of_the_directory_lists_the_jobs_that_fuzz_a_pattern_in_it(
    client: TestClient, tmp_path: Path,
) -> None:
    pid, tid, response = _create(client, tmp_path, "/api/FUZZ/users")
    assert response.status_code == 200
    client.post(f"/api/projects/{pid}/targets/{tid}/tree", json={"path": "/api/"})
    client.post(f"/api/projects/{pid}/targets/{tid}/tree", json={"path": "/other/"})

    nodes = {n["path"]: n for n in client.get(f"/api/projects/{pid}/targets/{tid}/tree").json()}

    history = nodes["/api/"]["fuzz_history"]
    assert [(h["path"], h["fuzz_keyword"]) for h in history] == [("/api/FUZZ/users", "FUZZ")]
    assert nodes["/other/"]["fuzz_history"] == []


def test_split_fuzz_template() -> None:
    assert target_service.split_fuzz_template("/a/FUZZ/b", "FUZZ") == ("/a/FUZZ/b", "/a/", "", "/b")
    assert target_service.split_fuzz_template("a/pre-FUZZ", "FUZZ") == ("/a/pre-FUZZ", "/a/", "pre-", "")
    assert target_service.split_fuzz_template("/FUZZ", "FUZZ") == ("/FUZZ", "/", "", "")
    assert target_service.split_fuzz_template("/a/b/", "FUZZ") is None
    assert target_service.split_fuzz_template("/s/?q=FUZZ&x=1", "FUZZ") == ("/s/?q=FUZZ&x=1", "/s/", "?q=", "&x=1")
    # a "/" inside the query belongs to the text before each word, not to the directory
    assert target_service.split_fuzz_template("/s/?next=/a/FUZZ", "FUZZ") == ("/s/?next=/a/FUZZ", "/s/", "?next=/a/", "")
    assert target_service.split_fuzz_template("/a/FUZZ?x=1", "FUZZ") == ("/a/FUZZ?x=1", "/a/", "", "?x=1")


def test_around_word_keeps_blank_lines_and_comments() -> None:
    assert _around_word("admin", "", "/x") == "admin/x"
    assert _around_word("/admin/", "p-", "/x") == "p-admin/x"
    assert _around_word("admin/", "", "") == "admin/"
    assert _around_word("# note", "", "/x") == "# note"
    assert _around_word("", "", "/x") == ""
    assert _around_word("/", "", "/x") == ""
    assert _around_word("/etc/passwd", "?f=", "&x=1") == "?f=/etc/passwd&x=1"
