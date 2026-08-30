from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._json(200, {"status": "healthy"})
            return
        if self.path == "/":
            self._json(200, {"message": "Coding Agent DevOps demo is running"})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        print(f"demo-http: {format % args}", flush=True)

    def _json(self, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), DemoHandler).serve_forever()
