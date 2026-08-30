from __future__ import annotations

import argparse
import json
import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from .local_settings import LocalSettings
from .directory_picker import DirectoryPickerError, pick_directory
from .web_runtime import RuntimeConflict, RuntimeNotFound, WebRuntime


MAX_REQUEST_BYTES = 1_048_576
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        runtime: WebRuntime,
        directory_picker: Any,
    ) -> None:
        self.runtime = runtime
        self.directory_picker = directory_picker
        super().__init__(address, LocalRequestHandler)


class LocalRequestHandler(BaseHTTPRequestHandler):
    server: LocalWebServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _handle(self, method: str) -> None:
        if not self._loopback_host_allowed():
            self._json(403, {"error": "仅允许通过本机地址访问"})
            return
        if method in {"POST", "PATCH", "DELETE"} and not self._origin_allowed():
            self._json(403, {"error": "请求来源不是当前本机页面"})
            return
        path = urlsplit(self.path).path
        try:
            if method == "GET" and path in STATIC_ROUTES:
                self._static(path)
                return
            if method == "GET" and path == "/api/events":
                self._events()
                return
            payload = self._json_body() if method in {"POST", "PATCH", "DELETE"} else {}
            status, response = self._dispatch(method, path, payload)
            if status == 204:
                self._empty(204)
            else:
                self._json(status, response)
        except RuntimeNotFound as exc:
            self._json(404, {"error": str(exc)})
        except RuntimeConflict as exc:
            self._json(409, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except DirectoryPickerError as exc:
            self._json(503, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "本机服务处理请求失败"})

    def _dispatch(self, method: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        runtime = self.server.runtime
        if method == "GET" and path == "/api/health":
            return 200, {"ok": True}
        if method == "GET" and path == "/api/state":
            return 200, runtime.snapshot()
        if method == "POST" and path == "/api/projects":
            return 201, {"project": runtime.add_project(self._string(payload, "path"))}
        if method == "POST" and path == "/api/projects/pick":
            selected = self.server.directory_picker(self._optional_string(payload, "initial"))
            if selected is None:
                return 200, {"cancelled": True}
            project, task = runtime.add_project_with_conversation(selected)
            return 201, {"cancelled": False, "project": project, "task": task}
        if method == "POST" and path == "/api/conversations":
            project_id = payload.get("project_id")
            if project_id is not None and not isinstance(project_id, str):
                raise ValueError("project_id 必须是字符串或 null")
            return 201, {"task": runtime.new_conversation(project_id)}
        if method == "PATCH" and path.startswith("/api/projects/"):
            project_id = self._identifier(path, "/api/projects/")
            return 200, {"project": runtime.rename_project(project_id, self._string(payload, "title"))}
        if method == "DELETE" and path.startswith("/api/projects/"):
            project_id = self._identifier(path, "/api/projects/")
            runtime.remove_project(project_id)
            return 204, {}
        if method == "PATCH" and path.startswith("/api/conversations/"):
            task_id = self._identifier(path, "/api/conversations/")
            return 200, {"task": runtime.rename_conversation(task_id, self._string(payload, "title"))}
        if method == "DELETE" and path.startswith("/api/conversations/"):
            task_id = self._identifier(path, "/api/conversations/")
            runtime.remove_conversation(task_id)
            return 204, {}
        if method == "POST" and path == "/api/settings":
            return 200, {"settings": runtime.update_settings(payload)}

        prefix = "/api/conversations/"
        if path.startswith(prefix):
            remainder = path[len(prefix) :]
            task_id, separator, action = remainder.partition("/")
            if not task_id or not separator:
                raise RuntimeNotFound("接口不存在")
            if method == "POST" and action == "workspace":
                return 200, {"task": runtime.bind_workspace(task_id, self._string(payload, "path"))}
            if method == "POST" and action == "permission":
                return 200, {
                    "task": runtime.set_permission_mode(task_id, self._string(payload, "mode"))
                }
            if method == "POST" and action == "worktree":
                return 201, {"task": runtime.create_task_worktree(task_id)}
            if method == "POST" and action == "pick-workspace":
                runtime.ensure_workspace_change_allowed(task_id)
                selected = self.server.directory_picker(self._optional_string(payload, "initial"))
                if selected is None:
                    return 200, {"cancelled": True}
                return 200, {
                    "cancelled": False,
                    "task": runtime.bind_workspace(task_id, selected),
                }
            if method == "POST" and action == "select":
                runtime.select_conversation(task_id)
                return 204, {}
            if method == "POST" and action == "messages":
                return 202, {"task": runtime.send_message(task_id, self._string(payload, "content"))}
            if method == "POST" and action == "cancel":
                runtime.cancel(task_id)
                return 202, {"ok": True}
            if method == "GET" and action.startswith("changes/"):
                change_path = unquote(action[len("changes/") :])
                if not change_path:
                    raise RuntimeNotFound("文件改动不存在")
                return 200, {"change": runtime.diff(task_id, change_path)}
            if method == "GET" and action == "devops-overview":
                return 200, {"overview": runtime.devops_overview(task_id)}

        approval_prefix = "/api/approvals/"
        if method == "POST" and path.startswith(approval_prefix):
            approval_id = self._identifier(path, approval_prefix)
            approved = payload.get("approved")
            if not isinstance(approved, bool):
                raise ValueError("approved 必须是布尔值")
            runtime.resolve_approval(approval_id, approved)
            return 204, {}
        raise RuntimeNotFound("接口不存在")

    def _json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求内容不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return value

    def _static(self, route: str) -> None:
        name, content_type = STATIC_ROUTES[route]
        asset = files("coding_agent").joinpath("web_assets", name)
        try:
            content = asset.read_bytes()
        except OSError:
            self._json(404, {"error": "页面资源不存在"})
            return
        self.send_response(200)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        content = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _events(self) -> None:
        raw_event_id = self.headers.get("Last-Event-ID")
        try:
            after_revision = int(raw_event_id) if raw_event_id is not None else -1
        except ValueError:
            after_revision = -1
        self.send_response(200)
        self._common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        while True:
            revision, state = self.server.runtime.wait_for_state(
                after_revision, timeout=15.0
            )
            if revision <= after_revision:
                frame = b": keepalive\n\n"
            else:
                payload = json.dumps(
                    state, ensure_ascii=False, separators=(",", ":")
                )
                frame = (
                    f"id: {revision}\nevent: state\ndata: {payload}\n\n"
                ).encode("utf-8")
                after_revision = revision
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def _loopback_host_allowed(self) -> bool:
        raw = self.headers.get("Host", "")
        host = raw.rsplit(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost"}

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == self.server.server_port
        )

    @staticmethod
    def _identifier(path: str, prefix: str) -> str:
        identifier = path[len(prefix) :]
        if not identifier or "/" in identifier:
            raise RuntimeNotFound("接口不存在")
        return identifier

    @staticmethod
    def _string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串")
        return value

    @staticmethod
    def _optional_string(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串或 null")
        return value or None


def create_server(
    runtime: WebRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    directory_picker: Any = None,
) -> LocalWebServer:
    if host != "127.0.0.1":
        raise ValueError("本机网页版只能绑定 127.0.0.1")
    if port < 0 or port > 65535:
        raise ValueError("端口必须在 0 到 65535 之间")
    return LocalWebServer((host, port), runtime, directory_picker or pick_directory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent", description="小码本机网页版编程智能体")
    parser.add_argument("--workspace", type=Path, help="初始工作区")
    parser.add_argument("--port", type=int, default=8000, help="本机访问端口，默认 8000")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path.cwd().resolve()
    settings = LocalSettings.load(root)
    runtime = WebRuntime(settings, root)
    if args.workspace:
        project = runtime.add_project(str(args.workspace.expanduser().resolve()))
        runtime.new_conversation(project["id"])
    server = create_server(runtime, port=args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    if not args.no_browser:
        webbrowser.open(url)
    print(f"小码已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
