"""Tiny whitelist static server for Alert Vision (Phase D1).

Serves ONLY the dashboard files (index.html + script.js) from the project root,
bound to 127.0.0.1, and nothing else. Uses only the Python standard library so no
extra dependencies are required.
"""

import os
import posixpath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8000
ROOT = os.path.dirname(os.path.abspath(__file__))
WHITELIST = {"/", "/index.html", "/script.js", "/alert_vision.py", "/analytics.py"}
MAX_BYTES = 512 * 1024


class WhitelistHandler(BaseHTTPRequestHandler):
    server_version = "AlertVisionStatic/1.0"

    def _safe_path(self, path: str) -> str | None:
        cleaned = posixpath.normpath(path.split("?", 1)[0].split("#", 1)[0])
        if cleaned == ".":
            cleaned = "/"
        if not cleaned.startswith("/"):
            cleaned = "/" + cleaned
        candidate = os.path.normpath(os.path.join(ROOT, cleaned.lstrip("/")))
        if not candidate.startswith(ROOT):
            return None
        if not os.path.isfile(candidate):
            return None
        return candidate

    def do_GET(self) -> None:  # noqa: N802 - http server API
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        allowed = {"/", "/index.html", "/script.js"}
        if path not in allowed:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        target = self._safe_path(path) or os.path.join(ROOT, "index.html")
        if not os.path.isfile(target):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        ext = os.path.splitext(target)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".py": "text/plain; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(target, "rb") as handle:
            body = handle.read(MAX_BYTES)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # silence http logs
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), WhitelistHandler)
    server.daemon_threads = True
    print(f":: [Alert Vision] Static dashboard server on http://{HOST}:{PORT}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
