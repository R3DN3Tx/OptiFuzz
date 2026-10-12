"""Demo target server for OptiFuzz manual testing: serves the static
demo_site/ directory like before, plus a handful of fixed routes that
return non-2xx/3xx status codes with deliberately different body sizes, so
status-code handling (and dirsearch's own same-size-404 detection) can be
eyeballed directly in the app.
"""
from __future__ import annotations

import http.server
import os
import socketserver

DEMO_DIR = os.path.dirname(os.path.abspath(__file__)) + "/demo_site"

SPECIAL_ROUTES = {
    "/forbidden": (403, b"Forbidden: you do not have access to this resource.\n"),
    "/forbidden-secret": (403, b"Forbidden.\n"),
    "/forbidden-vault": (
        403,
        b"Forbidden: this vault is restricted to administrators only. Contact "
        b"support if you believe this is a mistake.\n",
    ),
    "/missing-page": (404, b"Custom 404: missing-page was not found here.\n"),
    "/old-report.pdf": (404, b"Custom 404: old-report.pdf is gone (different size than missing-page).\n"),
    "/legacy-api": (
        404,
        b"Custom 404: this legacy API endpoint has been retired and is no longer "
        b"available at this location. Please refer to the current API docs.\n",
    ),
    "/moved": (302, b"Redirecting...\n", "/index.html"),
    "/old-login": (302, b"Redirecting to the new login page...\n", "/login.html"),
    "/crash": (500, b"Internal Server Error: something went wrong while handling this request.\n"),
    "/broken-export": (500, b"500 Internal Server Error.\n"),
    # Cross-origin redirects (different host and/or port than this server) --
    # for eyeballing that Results/FUZZ TREE show the full absolute URL
    # instead of just a path when a redirect actually leaves this origin.
    "/old-port-service": (
        302,
        b"Redirecting to the service now running on a different port...\n",
        "http://127.0.0.1:9090/new-service",  # same host, different port
    ),
    # Status variety inside subfolders, so FUZZ TREE children / Results
    # filters have 403/404/302/500 entries below the root too.
    "/admin/secret": (403, b"Forbidden: admin secret area.\n"),
    "/admin/old": (302, b"Moved to the new admin panel...\n", "/admin/panel/index.html"),
    "/admin/export": (403, b"Forbidden.\n"),
    "/api/v1/internal": (403, b"Forbidden: internal API, not for external use.\n"),
    "/api/v1/crash": (500, b"Internal Server Error in /api/v1/crash.\n"),
    "/api/v2/beta": (404, b"Custom 404: the beta endpoint is not enabled for this account.\n"),
    "/api/v2/debug": (500, b"500: debug endpoint failed.\n"),
    "/static/private": (403, b"Forbidden: private static assets.\n"),
    "/backup/old": (302, b"Moved...\n", "/backup/db.sql.bak"),
    "/wp-admin/staging": (403, b"Forbidden: staging.\n"),
    "/external-docs": (
        302,
        b"Redirecting to externally-hosted docs...\n",
        "https://example.com/docs",  # different host and scheme entirely
    ),
}


class DemoHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DEMO_DIR, **kwargs)

    def do_GET(self):
        route = SPECIAL_ROUTES.get(self.path)
        if route:
            status, body = route[0], route[1]
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            if len(route) == 3:
                self.send_header("Location", route[2])
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, format, *args):
        pass  # keep stdout quiet -- same as the plain http.server before


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8910), DemoHandler)
    print("Demo server on http://127.0.0.1:8910/ (static files + status-code routes)")
    server.serve_forever()
