"""CI smoke test (Linux): start the real desktop window and check it comes up.

Run it through a virtual display, e.g. `xvfb-run -a .venv/bin/python app/dev/ci_smoke.py`. It starts
run_desktop.py, waits for the backend to answer /health, takes a screenshot (shot.png) and checks
the window process is still alive, then stops it. Writes shot.png and app.log in the current
directory. Meant for GitHub Actions only -- it opens a window.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 58947  # run_desktop.py's default (backend/persistence/app_settings.py::DEFAULT_PORT)
ROOT = Path(__file__).resolve().parents[2]  # repository root: <repo>/app/dev/ci_smoke.py


def fail(message: str) -> None:
    print("SMOKE TEST FAILED:", message)
    sys.exit(1)


def backend_answers() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def main() -> None:
    env = dict(os.environ, OPTIFUZZ_WORKSPACE_ROOT=tempfile.mkdtemp(prefix="optifuzz-smoke-"))
    with open("app.log", "w", encoding="utf-8") as log:
        app = subprocess.Popen(
            [sys.executable, str(ROOT / "run_desktop.py")],
            stdout=log, stderr=subprocess.STDOUT, env=env, cwd=ROOT,
        )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if app.poll() is not None:
                fail(f"the app exited before the backend came up (code {app.returncode}); see app.log")
            if backend_answers():
                print("backend up")
                break
            time.sleep(1)
        else:
            fail("the backend did not answer /health within 60s")

        # The backend answering does not mean the window has painted yet (WebKit starts its own
        # processes). Take screenshots until one shows a real UI: a blank, single-colour 1600x1000
        # screenshot compresses to ~5 KB, a painted UI to tens of KB.
        painted = False
        shot_bytes = 0
        for attempt in range(1, 21):
            if app.poll() is not None:
                fail("the window process died after starting; see app.log")
            time.sleep(2)
            subprocess.run(["scrot", "-o", "shot.png"], check=True)
            shot_bytes = Path("shot.png").stat().st_size
            print(f"screenshot {attempt}: {shot_bytes} bytes")
            if shot_bytes >= 15_000:
                painted = True
                break
        if not painted:
            print("--- last lines of app.log:")
            print("\n".join(Path("app.log").read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
            fail("the window never painted anything within 40s (the screenshot stayed blank)")
        print("SMOKE TEST OK")
    finally:
        app.terminate()
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()


if __name__ == "__main__":
    main()
