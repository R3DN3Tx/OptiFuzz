"""End-to-end check against REAL dirsearch (CI, Windows and Linux): complete run, Pause, Resume, Stop.

Starts a throwaway local HTTP server, drives the FastAPI app through its own API with the real
engine, and exits non-zero on any deviation. Never touches anything outside a temp workspace.
"""
from __future__ import annotations

import functools
import http.server
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["OPTIFUZZ_WORKSPACE_ROOT"] = tempfile.mkdtemp(prefix="optifuzz-e2e-")
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

MAX_SECONDS_TO_END_A_RUN = 6.0  # Pause/Stop kill the child; this is a generous ceiling


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        pass  # dirsearch is killed mid-request on purpose (Pause/Stop); resets are expected


def fail(message: str) -> None:
    print("E2E FAILED:", message)
    sys.exit(1)


def main() -> None:
    web = Path(tempfile.mkdtemp(prefix="optifuzz-web-"))
    (web / "admin").mkdir()
    (web / "admin" / "index.html").write_text("admin")
    (web / "index.html").write_text("home")
    (web / "secret.txt").write_text("s")
    server = QuietServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(web)))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    small = web.parent / "optifuzz-small.txt"
    small.write_text("admin\nsecret.txt\nnope\nindex.html\n")
    big = web.parent / "optifuzz-big.txt"
    big.write_text("\n".join(f"w{i}" for i in range(60000)) + "\n")

    def wait_for(client: TestClient, job_id: str, wanted: tuple[str, ...], timeout: float = 90.0) -> dict:
        deadline = time.time() + timeout
        job: dict = {}
        while time.time() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"].lower() in wanted:
                return job
            time.sleep(0.2)
        fail(f"timed out waiting for {wanted}; last status {job.get('status')}")
        return job

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        project = client.post("/api/projects", json={"name": "E2E"}).json()
        target = client.post(
            f"/api/projects/{project['id']}/targets",
            json={"name": "local", "base_url": f"http://127.0.0.1:{port}"},
        ).json()
        base = f"/api/projects/{project['id']}/targets/{target['id']}/jobs"

        job = client.post(base, json={"wordlist_files": [str(small)], "threads": 2}).json()
        done = wait_for(client, job["id"], ("completed", "failed", "stopped"))
        print("complete run:", done["status"], "results:", done.get("result_count"))
        if done["status"].lower() != "completed" or done.get("result_count") != 3:
            fail(f"expected a COMPLETED run with 3 results, got {done['status']} / {done.get('result_count')}")

        job = client.post(base, json={"wordlist_files": [str(big)], "threads": 3, "delay": 0.01}).json()
        wait_for(client, job["id"], ("running",))
        time.sleep(4)
        started = time.time()
        client.post(f"/api/jobs/{job['id']}/pause")
        paused = wait_for(client, job["id"], ("paused",), timeout=20)
        pause_seconds = time.time() - started
        print(f"pause took {pause_seconds:.1f}s, completed_lines={paused.get('completed_lines')}")
        if pause_seconds > MAX_SECONDS_TO_END_A_RUN:
            fail(f"pause took {pause_seconds:.1f}s")
        if not paused.get("completed_lines"):
            fail("pause recorded no progress")

        client.post(f"/api/jobs/{job['id']}/resume")
        wait_for(client, job["id"], ("running",))
        time.sleep(3)
        started = time.time()
        client.post(f"/api/jobs/{job['id']}/stop")
        wait_for(client, job["id"], ("stopped",), timeout=20)
        stop_seconds = time.time() - started
        print(f"stop took {stop_seconds:.1f}s")
        if stop_seconds > MAX_SECONDS_TO_END_A_RUN:
            fail(f"stop took {stop_seconds:.1f}s")

        failed_job = client.post(
            base, json={"wordlist_files": [str(small)], "extra_args": "--this-flag-does-not-exist"}
        ).json()
        failed = wait_for(client, failed_job["id"], ("completed", "failed", "stopped"))
        print("bad-flag run:", failed["status"])
        if failed["status"].lower() != "failed":
            fail(f"a job with an invalid dirsearch flag should be FAILED, got {failed['status']}")

    server.shutdown()
    print("E2E OK")


if __name__ == "__main__":
    main()
