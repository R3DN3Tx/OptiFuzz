from __future__ import annotations

import os
import socket

from backend.services import notice_service

# Ports other programs commonly listen on (web servers, databases, dev servers, proxies, remote desktop...).
# When the port OptiFuzz wants is busy it never takes one of these as a fallback: a program that starts
# later on its usual port would then fail, and the app would be reachable on a port people associate with
# something else.
COMMON_PORTS = frozenset({
    1080, 1433, 1521, 1723, 2049, 2375, 2376, 3000, 3001, 3128, 3306, 3389, 4000, 4200, 4444, 4567, 5000,
    5001, 5173, 5432, 5500, 5601, 5672, 5900, 5985, 5986, 6000, 6379, 6443, 7000, 7001, 8000, 8001, 8008,
    8010, 8080, 8081, 8082, 8083, 8088, 8089, 8090, 8443, 8500, 8888, 8889, 8910, 9000, 9001, 9042, 9090,
    9091, 9200, 9300, 9418, 9999, 10000, 11211, 15672, 27017, 50000,
})
_FIRST_FALLBACK_OFFSET = 1
_FALLBACK_SPAN = 400


def is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            # Same option uvicorn binds with: without it a port still in TIME_WAIT (right after the previous
            # copy closed) looks taken. Not set on Windows, where it would let a port that another program is
            # listening on look free.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def alternative_for(preferred: int) -> int:
    """The closest free port above `preferred` that is not one of the commonly used ones; an
    operating-system-chosen port only if none of the nearby ones is free."""
    for candidate in range(preferred + _FIRST_FALLBACK_OFFSET, min(preferred + _FALLBACK_SPAN, 65535)):
        if candidate >= 1024 and candidate not in COMMON_PORTS and is_free(candidate):
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pick_port(preferred: int, *, chosen_by_user: bool) -> int:
    """The port to start on. When `preferred` is busy another one is used and a notice is raised, so
    Settings can show it (red dot) instead of the app silently moving."""
    if is_free(preferred):
        notice_service.clear_notice("port")
        return preferred
    port = alternative_for(preferred)
    which = "The port you chose" if chosen_by_user else "The default port"
    notice_service.set_notice(
        "port",
        f"{which} ({preferred}) is busy, so OptiFuzz is using {port} instead. "
        "Close the program that uses it, or choose another port in Settings.",
        wanted=preferred, using=port, chosen_by_user=chosen_by_user,
    )
    return port
