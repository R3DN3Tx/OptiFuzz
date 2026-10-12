import functools
import http.server
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend.job_manager.wordlist_resolver import _estimate_requests
from backend.platform_compat import no_window_kwargs

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def request_logging_site(tmp_path: Path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    requested_paths: list[str] = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            requested_paths.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):
            pass

    handler = functools.partial(Handler, directory=str(site_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", requested_paths
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_estimate_requests_matches_real_dirsearch_with_force_extensions(
    tmp_path: Path, request_logging_site
) -> None:
    # _estimate_requests replicates dirsearch's own dictionary-generation
    # rule (lib/core/wordlist_backend.py) well enough to predict its real
    # request count -- verified here against an actual dirsearch run, not
    # just against a reading of its source (a previous version of this
    # function had a real bug -- missing that dirsearch always keeps the
    # original line *in addition to* the -f variants, not instead of them --
    # that only a real run caught, since it matched the docstring's own
    # wording exactly). dirsearch also makes its own small, roughly-fixed
    # set of wildcard-calibration probes per invocation, independent of the
    # wordlist -- not predicted here (see _estimate_requests' own
    # docstring for why), so this asserts the predicted paths are a subset
    # of what was actually requested, not an exact total match.
    url, requested_paths = request_logging_site
    words = ["admin", "api", "config.bak", "static/"]
    extensions = ["php", "html"]

    wordlist = tmp_path / "words.txt"
    wordlist.write_text("\n".join(words) + "\n", encoding="utf-8")

    predicted = _estimate_requests(words, extensions, force_extensions=True)

    argv = [
        sys.executable, "-m", "dirsearch", "-u", url, "-w", str(wordlist),
        "-t", "1", "-q", "--no-color", "-e", ",".join(extensions), "-f",
    ]
    subprocess.run(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=30, **no_window_kwargs(),
    )

    # Every path our own prediction expects to exist should have actually
    # been requested (this is the part that was previously wrong).
    expected_paths = {
        "/admin", "/admin/", "/admin.php", "/admin.html",
        "/api", "/api/", "/api.php", "/api.html",
        "/config.bak",  # has a "." -- never gets -f variants
        "/static/",  # already ends with "/" -- never gets -f variants
    }
    assert expected_paths.issubset(set(requested_paths))
    assert predicted == len(expected_paths) == 10
