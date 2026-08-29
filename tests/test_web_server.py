from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from coding_agent.local_settings import LocalSettings
from coding_agent.web import create_server
from coding_agent.web_runtime import WebRuntime


class UnusedModel:
    def complete(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("model should not be called")


@contextmanager
def running_server(tmp_path: Path) -> Iterator[tuple[WebRuntime, int]]:
    settings = LocalSettings(
        api_key="unit-test-secret",
        model="test-model",
        base_url="https://example.invalid",
        workspace=str(tmp_path),
    )
    runtime = WebRuntime(settings, tmp_path, model_factory=UnusedModel)
    server = create_server(runtime, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield runtime, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, bytes]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Host": f"127.0.0.1:{port}"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type") or ""
    connection.close()
    return response.status, content_type, data


def test_state_and_project_routes_return_json_without_secret(tmp_path: Path) -> None:
    with running_server(tmp_path) as (_runtime, port):
        status, content_type, raw = request(port, "GET", "/api/state")
        created_status, _, created_raw = request(
            port,
            "POST",
            "/api/projects",
            {"path": str(tmp_path)},
        )

    state = json.loads(raw)
    created = json.loads(created_raw)
    assert status == 200
    assert content_type.startswith("application/json")
    assert created_status == 201
    assert created["project"]["path"] == str(tmp_path.resolve())
    assert "unit-test-secret" not in json.dumps(state)


def test_conversation_can_be_created_selected_and_bound(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with running_server(tmp_path) as (_runtime, port):
        status, _, raw = request(port, "POST", "/api/conversations", {})
        task = json.loads(raw)["task"]
        bind_status, _, bind_raw = request(
            port,
            "POST",
            f"/api/conversations/{task['id']}/workspace",
            {"path": str(workspace)},
        )
        select_status, _, _ = request(
            port,
            "POST",
            f"/api/conversations/{task['id']}/select",
            {},
        )

    assert status == 201
    assert bind_status == 200
    assert json.loads(bind_raw)["task"]["workspace"] == str(workspace.resolve())
    assert select_status == 204


def test_invalid_json_and_unknown_route_have_structured_errors(tmp_path: Path) -> None:
    with running_server(tmp_path) as (_runtime, port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(
            "POST",
            "/api/projects",
            body=b"{bad",
            headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        invalid_status = response.status
        invalid = json.loads(response.read())
        connection.close()
        missing_status, _, missing_raw = request(port, "GET", "/api/not-found")

    assert invalid_status == 400
    assert invalid["error"] == "请求内容不是合法 JSON"
    assert missing_status == 404
    assert json.loads(missing_raw)["error"] == "接口不存在"


def test_non_loopback_host_and_cross_origin_write_are_rejected(tmp_path: Path) -> None:
    with running_server(tmp_path) as (_runtime, port):
        host_status, _, _ = request(
            port,
            "GET",
            "/api/state",
            headers={"Host": "evil.example"},
        )
        origin_status, _, _ = request(
            port,
            "POST",
            "/api/conversations",
            {},
            headers={"Origin": "https://evil.example"},
        )

    assert host_status == 403
    assert origin_status == 403


def test_static_routes_do_not_allow_path_traversal(tmp_path: Path) -> None:
    with running_server(tmp_path) as (_runtime, port):
        page_status, page_type, page = request(port, "GET", "/")
        traversal_status, _, _ = request(port, "GET", "/../pyproject.toml")

    assert page_status == 200
    assert page_type.startswith("text/html")
    assert b'id="app"' in page
    assert traversal_status == 404
