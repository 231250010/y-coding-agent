from __future__ import annotations

import http.client
import json
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from coding_agent.local_settings import LocalSettings
from coding_agent.web import create_server
from coding_agent.web_runtime import WebRuntime


class UnusedModel:
    def complete(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("model should not be called")


@contextmanager
def running_server(
    tmp_path: Path,
    *,
    directory_picker: Callable[[str | None], str | None] | None = None,
) -> Iterator[tuple[WebRuntime, int]]:
    settings = LocalSettings(
        api_key="unit-test-secret",
        model="test-model",
        base_url="https://example.invalid",
        workspace=str(tmp_path),
    )
    runtime = WebRuntime(settings, tmp_path, model_factory=UnusedModel)
    server = create_server(runtime, port=0, directory_picker=directory_picker)
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


def test_devops_overview_route_is_safe_when_project_has_no_compose_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with running_server(tmp_path) as (_runtime, port):
        _, _, task_raw = request(port, "POST", "/api/conversations", {})
        task = json.loads(task_raw)["task"]
        request(
            port,
            "POST",
            f"/api/conversations/{task['id']}/workspace",
            {"path": str(workspace)},
        )
        status, content_type, raw = request(
            port,
            "GET",
            f"/api/conversations/{task['id']}/devops-overview",
        )

    payload = json.loads(raw)["overview"]
    assert status == 200
    assert content_type.startswith("application/json")
    assert payload["workspace"] == str(workspace.resolve())
    assert payload["compose_file"] is None
    assert payload["environments"][0]["error"]["code"] == "compose_not_found"
    assert "unit-test-secret" not in json.dumps(payload)


def test_task_worktree_route_switches_only_that_conversation_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repository"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Coding Agent Tests"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=workspace,
        check=True,
    )
    (workspace / "app.txt").write_text("main\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace, check=True)

    with running_server(tmp_path) as (_runtime, port):
        _, _, project_raw = request(port, "POST", "/api/projects", {"path": str(workspace)})
        project = json.loads(project_raw)["project"]
        _, _, first_raw = request(
            port, "POST", "/api/conversations", {"project_id": project["id"]}
        )
        _, _, second_raw = request(
            port, "POST", "/api/conversations", {"project_id": project["id"]}
        )
        first = json.loads(first_raw)["task"]
        second = json.loads(second_raw)["task"]
        status, _, isolated_raw = request(
            port, "POST", f"/api/conversations/{first['id']}/worktree", {}
        )
        _, _, state_raw = request(port, "GET", "/api/state")

    isolated = json.loads(isolated_raw)["task"]
    state = json.loads(state_raw)
    untouched = next(item for item in state["tasks"] if item["id"] == second["id"])
    assert status == 201
    assert isolated["worktree"]["branch"].startswith("coding-agent/task-")
    assert isolated["workspace"] != str(workspace.resolve())
    assert Path(isolated["workspace"]).joinpath("app.txt").is_file()
    assert untouched["workspace"] == str(workspace.resolve())
    assert untouched["worktree"] is None


def test_conversation_permission_mode_route_updates_only_selected_task(tmp_path: Path) -> None:
    with running_server(tmp_path) as (_runtime, port):
        _, _, first_raw = request(port, "POST", "/api/conversations", {})
        _, _, second_raw = request(port, "POST", "/api/conversations", {})
        first = json.loads(first_raw)["task"]
        second = json.loads(second_raw)["task"]
        status, _, updated_raw = request(
            port,
            "POST",
            f"/api/conversations/{first['id']}/permission",
            {"mode": "full"},
        )
        _, _, state_raw = request(port, "GET", "/api/state")

    updated = json.loads(updated_raw)["task"]
    state = json.loads(state_raw)
    untouched = next(item for item in state["tasks"] if item["id"] == second["id"])
    assert status == 200
    assert updated["permission_mode"] == "full"
    assert untouched["permission_mode"] == "risk"


def test_native_picker_atomically_binds_selected_workspace(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent-root"
    selected = tmp_path / "selected-project"
    agent_root.mkdir()
    selected.mkdir()
    received: list[str | None] = []

    def picker(initial: str | None) -> str | None:
        received.append(initial)
        return str(selected)

    with running_server(agent_root, directory_picker=picker) as (_runtime, port):
        _, _, task_raw = request(port, "POST", "/api/conversations", {})
        task = json.loads(task_raw)["task"]
        status, _, raw = request(
            port,
            "POST",
            f"/api/conversations/{task['id']}/pick-workspace",
            {"initial": str(agent_root)},
        )

    result = json.loads(raw)
    assert status == 200
    assert received == [str(agent_root)]
    assert result["cancelled"] is False
    assert result["task"]["workspace"] == str(selected.resolve())


def test_native_picker_can_add_project_and_cancel_without_mutating_state(tmp_path: Path) -> None:
    selected = tmp_path / "selected-project"
    selected.mkdir()
    choices = iter((str(selected), None))

    with running_server(tmp_path, directory_picker=lambda _initial: next(choices)) as (_runtime, port):
        created_status, _, created_raw = request(port, "POST", "/api/projects/pick", {})
        _, _, before_raw = request(port, "GET", "/api/state")
        cancelled_status, _, cancelled_raw = request(port, "POST", "/api/projects/pick", {})
        _, _, state_raw = request(port, "GET", "/api/state")

    created = json.loads(created_raw)
    cancelled = json.loads(cancelled_raw)
    before = json.loads(before_raw)
    state = json.loads(state_raw)
    assert created_status == 201
    assert created["project"]["path"] == str(selected.resolve())
    assert created["task"]["workspace"] == str(selected.resolve())
    assert cancelled_status == 200
    assert cancelled == {"cancelled": True}
    assert state == before


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


def test_delete_routes_return_204_and_reflect_state(tmp_path: Path) -> None:
    with running_server(tmp_path) as (_runtime, port):
        _, _, project_raw = request(port, "POST", "/api/projects", {"path": str(tmp_path)})
        project = json.loads(project_raw)["project"]
        _, _, task_raw = request(port, "POST", "/api/conversations", {"project_id": project["id"]})
        task = json.loads(task_raw)["task"]

        delete_project_status, _, _ = request(port, "DELETE", f"/api/projects/{project['id']}")
        _, _, state_raw = request(port, "GET", "/api/state")
        state = json.loads(state_raw)

        delete_task_status, _, _ = request(port, "DELETE", f"/api/conversations/{task['id']}")
        _, _, final_raw = request(port, "GET", "/api/state")
        final = json.loads(final_raw)

    assert delete_project_status == 204
    assert state["projects"] == []
    assert any(item["id"] == task["id"] and item["project_id"] is None for item in state["tasks"])
    assert delete_task_status == 204
    assert final["tasks"] == []
