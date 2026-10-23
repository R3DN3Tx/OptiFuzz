"""Standalone script spawned by MockDirsearchEngine.

Run as `python -m backend.engine.mock_runner --url ... --wordlist ...`. Prints
canned result lines in dirsearch's real quiet-mode stdout format
at a small delay, then exits 0 -- lets
JobManager/parser tests exercise a real subprocess and a real stdout stream
without ever touching the network or spawning real dirsearch.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

_STATUS_CYCLE = (200, 301, 403, 404)


def _iter_words(wordlist_file: Path) -> list[str]:
    words = []
    for raw_line in wordlist_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        word = raw_line.strip()
        if word and not word.startswith("#"):
            words.append(word)
    return words


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--wordlist", required=True, type=Path)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--every", type=int, default=4)
    parser.add_argument(
        "--fail",
        action="store_true",
        help="Exit non-zero immediately, for exercising JobManager's FAILED path in tests",
    )
    parser.add_argument(
        "--force-status",
        type=int,
        default=None,
        help="Emit this status for every result instead of cycling, for exercising "
        "JobManager's auto-pause-on-status path in tests",
    )
    args = parser.parse_args(argv)

    if args.fail:
        print("simulated engine failure", file=sys.stderr, flush=True)
        return 1

    base_url = args.url if args.url.endswith("/") else args.url + "/"
    words = _iter_words(args.wordlist)

    for index, word in enumerate(words):
        if index % args.every != 0:
            continue

        status = args.force_status if args.force_status is not None else _STATUS_CYCLE[index % len(_STATUS_CYCLE)]
        size = f"{(index * 37) % 4096}B"
        timestamp = datetime.now().strftime("%H:%M:%S")
        url = urljoin(base_url, word)
        print(f"[{timestamp}] {status} - {size.rjust(6)} - {url}", flush=True)

        if args.delay:
            time.sleep(args.delay)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
