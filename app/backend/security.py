from __future__ import annotations

from urllib.parse import urlsplit

# OptiFuzz only ever talks to its own window, which loads the page from 127.0.0.1. A web page in
# the user's browser must not be able to reach the API either, and there are two ways it could:
#   * a cross-site request sent to 127.0.0.1 (stopped by the Origin / Sec-Fetch-Site checks), and
#   * DNS rebinding: the attacker's own domain is made to resolve to 127.0.0.1, so the browser
#     treats the API as "same site" and lets the page read the answers (stopped by the Host check,
#     since the request still carries the attacker's hostname).
# Everything is refused with 403 before any route runs.

ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})  # "none": typed in the address bar


def _hostname(host_header: str) -> str | None:
    """Hostname part of a Host header or Origin netloc ('127.0.0.1:8765' -> '127.0.0.1',
    '[::1]:8765' -> '::1'); None when it cannot be parsed."""
    try:
        return urlsplit(f"//{host_header}").hostname
    except ValueError:
        return None


def _is_allowed_host(host_header: str) -> bool:
    name = _hostname(host_header)
    return name is not None and name.lower() in ALLOWED_HOSTNAMES


def _is_allowed_origin(origin: str, host_header: str) -> bool:
    """The page that sent the request must itself be served by this same host and port."""
    try:
        parts = urlsplit(origin)
        origin_port = parts.port
    except ValueError:
        return False
    if parts.scheme != "http" or not parts.hostname or parts.hostname.lower() not in ALLOWED_HOSTNAMES:
        return False
    try:
        host_port = urlsplit(f"//{host_header}").port
    except ValueError:
        return False
    return origin_port == host_port


class LocalOnlyMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware), so the server-sent-events stream is not buffered."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        reason = self._refusal(scope, headers)
        if reason is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = reason.encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _refusal(scope, headers: dict[str, str]) -> str | None:
        host = headers.get("host", "")
        if not _is_allowed_host(host):
            return "Forbidden: OptiFuzz only answers requests addressed to 127.0.0.1 or localhost."
        origin = headers.get("origin")
        if origin is not None and not _is_allowed_origin(origin, host):
            return "Forbidden: cross-origin request."
        fetch_site = headers.get("sec-fetch-site")
        if (fetch_site is not None and fetch_site.lower() not in _SAME_ORIGIN_FETCH_SITES
                and scope.get("path", "").startswith("/api/")):
            return "Forbidden: cross-site request."
        return None
