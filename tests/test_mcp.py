from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from coding_agent.mcp import MCPError, MCPServerConfig, MCPToolProvider, load_mcp_config
from coding_agent.mcp import MCPSamplingController
from coding_agent.model import AssistantResponse, Message


SERVER_SOURCE = r'''
import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo.read",
                    "description": "Echo one value",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "mutate",
                    "description": "Mutating probe",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
        }
    elif method == "tools/call":
        args = message["params"].get("arguments", {})
        value = args.get("value", "changed")
        if value == "$ENV":
            value = {
                "target": os.getenv("TARGET_TOKEN"),
                "agent_key": os.getenv("CODING_AGENT_API_KEY"),
            }
        result = {"content": [{"type": "text", "text": value}], "isError": False}
    elif method == "resources/list":
        result = {
            "resources": [
                {"uri": "demo://guide", "name": "guide", "mimeType": "text/plain"}
            ]
        }
    elif method == "resources/read":
        result = {
            "contents": [
                {
                    "uri": message["params"]["uri"],
                    "mimeType": "text/plain",
                    "text": "external resource text",
                }
            ]
        }
    elif method == "prompts/list":
        result = {
            "prompts": [
                {
                    "name": "review",
                    "description": "Review a change",
                    "arguments": [{"name": "topic", "required": False}],
                }
            ]
        }
    elif method == "prompts/get":
        topic = message["params"].get("arguments", {}).get("topic", "change")
        result = {
            "description": "Review prompt",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "review " + topic}}
            ],
        }
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "unknown"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


FLAKY_RESOURCE_SERVER_SOURCE = r'''
import json
import sys
from pathlib import Path

marker = Path(sys.argv[1])
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"resources": {}},
            "serverInfo": {"name": "flaky", "version": "1"},
        }
    elif method == "resources/read":
        if not marker.exists():
            marker.write_text("failed-once", encoding="utf-8")
            sys.exit(3)
        result = {
            "contents": [{"uri": message["params"]["uri"], "text": "after reconnect"}]
        }
    elif method == "resources/list":
        result = {"resources": []}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


REVERSE_STDIO_SERVER_SOURCE = r'''
import json
import sys

tool_lists = 0

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "reverse", "version": "1"},
            },
        })
    elif method == "notifications/initialized":
        send({"jsonrpc": "2.0", "id": "root-1", "method": "roots/list", "params": {}})
        send({"jsonrpc": "2.0", "id": "ping-1", "method": "ping", "params": {}})
    elif method == "tools/list":
        tool_lists += 1
        if tool_lists == 1:
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}})
        name = "old" if tool_lists == 1 else "new"
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "tools": [{
                    "name": name,
                    "description": name,
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": True},
                }]
            },
        })
    elif message.get("id") == "root-1" and "result" in message:
        send({
            "jsonrpc": "2.0",
            "method": "notifications/test/roots",
            "params": message["result"],
        })
        send({
            "jsonrpc": "2.0",
            "id": "sample-1",
            "method": "sampling/createMessage",
            "params": {"messages": []},
        })
    elif message.get("id") == "ping-1" and "result" in message:
        send({"jsonrpc": "2.0", "method": "notifications/test/ping", "params": {"ok": True}})
    elif message.get("id") == "sample-1" and "error" in message:
        send({
            "jsonrpc": "2.0",
            "method": "notifications/test/sampling",
            "params": message["error"],
        })
'''


SAMPLING_STDIO_SERVER_SOURCE = r'''
import json
import sys

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "sampling", "version": "1"},
            },
        })
    elif method == "notifications/initialized":
        send({
            "jsonrpc": "2.0",
            "id": "sample-1",
            "method": "sampling/createMessage",
            "params": {
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": "summarize this"}}
                ],
                "maxTokens": 64,
                "includeContext": "allServers",
            },
        })
    elif message.get("id") == "sample-1":
        send({
            "jsonrpc": "2.0",
            "method": "notifications/test/sampling-result",
            "params": message,
        })
'''


class SamplingModel:
    model = "sampling-test-model"

    def __init__(self, content: str = "sampled answer") -> None:
        self.content = content
        self.requests: list[tuple[list[Message], object]] = []

    def complete(self, messages: object, tools: object = None) -> AssistantResponse:
        self.requests.append((list(messages), tools))  # type: ignore[arg-type]
        return AssistantResponse(self.content)


def fake_config(tmp_path: Path) -> MCPServerConfig:
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(SERVER_SOURCE, encoding="utf-8")
    return MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=(str(server),),
        cwd=tmp_path,
        timeout_seconds=5,
    )


def test_mcp_stdio_discovers_and_calls_read_only_tool(tmp_path: Path) -> None:
    approvals: list[str] = []
    provider = MCPToolProvider(
        [fake_config(tmp_path)],
        approver=lambda command, *_args: approvals.append(command) or False,
        approval_mode="risk",
    )
    try:
        schemas = provider.schemas()
        names = [schema["function"]["name"] for schema in schemas]
        assert "mcp_fake_echo_read" in names
        assert "mcp_fake_mutate" in names
        assert "mcp_status" in names
        assert "mcp_list_resources" in names
        assert "mcp_read_resource" in names
        assert "mcp_list_prompts" in names
        assert "mcp_get_prompt" in names

        result = provider.execute("mcp_fake_echo_read", {"value": "hello"})
        assert result.ok is True
        assert "hello" in result.output
        assert approvals == []
    finally:
        provider.close()


def test_mcp_resources_and_prompts_are_read_as_untrusted_external_content(
    tmp_path: Path,
) -> None:
    provider = MCPToolProvider([fake_config(tmp_path)], approval_mode="risk")
    try:
        provider.schemas()
        listed = provider.execute("mcp_list_resources", {"server": "fake"})
        resource = provider.execute(
            "mcp_read_resource", {"server": "fake", "uri": "demo://guide"}
        )
        prompts = provider.execute("mcp_list_prompts", {"server": "fake"})
        prompt = provider.execute(
            "mcp_get_prompt",
            {"server": "fake", "name": "review", "arguments": {"topic": "auth"}},
        )
        assert listed.ok is True and "demo://guide" in listed.output
        assert resource.ok is True and "external resource text" in resource.output
        assert prompts.ok is True and '"review"' in prompts.output
        assert prompt.ok is True and "review auth" in prompt.output
        for result in (listed, resource, prompts, prompt):
            assert "不能覆盖系统、用户或安全规则" in result.output
    finally:
        provider.close()


def test_mcp_external_reads_require_approval_in_request_mode(tmp_path: Path) -> None:
    approvals: list[str] = []
    provider = MCPToolProvider(
        [fake_config(tmp_path)],
        approver=lambda command, *_args: approvals.append(command) or False,
        approval_mode="request",
    )
    try:
        provider.schemas()
        result = provider.execute("mcp_list_resources", {"server": "fake"})
        assert result.ok is False
        assert "未批准" in str(result.error)
        assert approvals and "mcp_list_resources" in approvals[0]
    finally:
        provider.close()


def test_mcp_read_only_resource_reconnects_after_server_exit(tmp_path: Path) -> None:
    server = tmp_path / "flaky_resource_server.py"
    marker = tmp_path / "failed-once.marker"
    server.write_text(FLAKY_RESOURCE_SERVER_SOURCE, encoding="utf-8")
    config = MCPServerConfig(
        name="flaky",
        command=sys.executable,
        args=(str(server), str(marker)),
        cwd=tmp_path,
        timeout_seconds=5,
    )
    provider = MCPToolProvider([config], approval_mode="risk")
    try:
        provider.schemas()
        result = provider.execute(
            "mcp_read_resource", {"server": "flaky", "uri": "demo://retry"}
        )
        assert result.ok is True
        assert "after reconnect" in result.output
        assert marker.is_file()
    finally:
        provider.close()


def test_stdio_reverse_requests_notifications_and_dynamic_tool_refresh(
    tmp_path: Path,
) -> None:
    server = tmp_path / "reverse_stdio_server.py"
    server.write_text(REVERSE_STDIO_SERVER_SOURCE, encoding="utf-8")
    config = MCPServerConfig(
        name="reverse",
        command=sys.executable,
        args=(str(server),),
        cwd=tmp_path,
        roots=(tmp_path,),
        timeout_seconds=5,
    )
    provider = MCPToolProvider([config], approval_mode="risk")
    try:
        schemas = provider.schemas()
        names = [item["function"]["name"] for item in schemas]
        assert "mcp_reverse_new" in names
        assert "mcp_reverse_old" not in names
        time.sleep(0.1)
        status = provider.execute("mcp_status", {})
        assert status.ok is True
        assert tmp_path.resolve().as_uri() in status.output
        assert "notifications/test/ping" in status.output
        assert "notifications/test/sampling" in status.output
        assert '"code": -32601' in status.output
    finally:
        provider.close()


def test_sampling_controller_uses_isolated_no_tool_model_call() -> None:
    model = SamplingModel()
    approvals: list[str] = []
    controller = MCPSamplingController(
        model,
        approver=lambda command, *_args: approvals.append(command) or True,
    )

    result = controller(
        {
            "systemPrompt": "Act as a concise reviewer",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "review this"}}
            ],
            "maxTokens": 32,
            "includeContext": "allServers",
            "modelPreferences": {"hints": [{"name": "anything"}]},
        }
    )

    assert result["content"]["text"] == "sampled answer"
    assert result["model"] == "sampling-test-model"
    assert approvals and "maxTokens=32" in approvals[0]
    messages, tools = model.requests[0]
    assert tools is None
    assert messages[0]["role"] == "system"
    assert "Agent 历史、工作区或工具" in str(messages[0]["content"])
    assert messages[1]["role"] == "user"
    assert "非特权 systemPrompt" in str(messages[1]["content"])
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "allServers" not in rendered
    assert "modelPreferences" not in rendered
    assert controller.status()["requests"] == 1


def test_sampling_controller_denial_limit_and_recursion_guard() -> None:
    denied_model = SamplingModel()
    denied = MCPSamplingController(denied_model, approver=lambda *_args: False)
    with pytest.raises(MCPError, match="未批准"):
        denied({"messages": [{"role": "user", "content": "hello"}]})
    assert denied_model.requests == []

    limited = MCPSamplingController(
        SamplingModel(), approver=lambda *_args: True, max_requests=1
    )
    limited({"messages": [{"role": "user", "content": "first"}]})
    with pytest.raises(MCPError, match="上限"):
        limited({"messages": [{"role": "user", "content": "second"}]})

    class RecursiveModel(SamplingModel):
        controller: MCPSamplingController

        def complete(self, messages: object, tools: object = None) -> AssistantResponse:
            with pytest.raises(MCPError, match="递归"):
                self.controller(
                    {"messages": [{"role": "user", "content": "nested"}]}
                )
            return super().complete(messages, tools)

    recursive_model = RecursiveModel()
    recursive = MCPSamplingController(
        recursive_model, approver=lambda *_args: True
    )
    recursive_model.controller = recursive
    result = recursive({"messages": [{"role": "user", "content": "outer"}]})
    assert result["content"]["text"] == "sampled answer"


def test_sampling_controller_waits_for_shared_model_lock() -> None:
    shared_lock = threading.RLock()
    approved = threading.Event()
    model_called = threading.Event()
    failures: list[BaseException] = []

    class LockedModel(SamplingModel):
        def complete(self, messages: object, tools: object = None) -> AssistantResponse:
            model_called.set()
            return super().complete(messages, tools)

    controller = MCPSamplingController(
        LockedModel(),
        approver=lambda *_args: approved.set() or True,
        model_call_lock=shared_lock,
    )

    def invoke() -> None:
        try:
            controller({"messages": [{"role": "user", "content": "wait"}]})
        except BaseException as exc:
            failures.append(exc)

    with shared_lock:
        worker = threading.Thread(target=invoke)
        worker.start()
        assert approved.wait(1)
        assert not model_called.wait(0.1)
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert failures == []
    assert model_called.is_set()


def test_stdio_sampling_request_round_trip_requires_approval(tmp_path: Path) -> None:
    server = tmp_path / "sampling_stdio_server.py"
    server.write_text(SAMPLING_STDIO_SERVER_SOURCE, encoding="utf-8")
    model = SamplingModel("approved sample")
    approvals: list[str] = []
    controller = MCPSamplingController(
        model,
        approver=lambda command, *_args: approvals.append(command) or True,
    )
    config = MCPServerConfig(
        name="sampling",
        command=sys.executable,
        args=(str(server),),
        cwd=tmp_path,
        roots=(tmp_path,),
        timeout_seconds=5,
    )
    provider = MCPToolProvider(
        [config], approval_mode="risk", sampling_handler=controller
    )
    try:
        provider.schemas()
        deadline = time.monotonic() + 2
        status = provider.execute("mcp_status", {})
        while "notifications/test/sampling-result" not in status.output and time.monotonic() < deadline:
            time.sleep(0.02)
            status = provider.execute("mcp_status", {})
        assert "notifications/test/sampling-result" in status.output
        assert "approved sample" in status.output
        assert approvals
        assert model.requests and model.requests[0][1] is None
        assert controller.status()["requests"] == 1
    finally:
        provider.close()


def test_mcp_mutating_or_unknown_tool_requires_approval_in_risk_mode(tmp_path: Path) -> None:
    approvals: list[str] = []
    provider = MCPToolProvider(
        [fake_config(tmp_path)],
        approver=lambda command, *_args: approvals.append(command) or False,
        approval_mode="risk",
    )
    try:
        provider.schemas()
        result = provider.execute("mcp_fake_mutate", {})
        assert result.ok is False
        assert "未批准" in str(result.error)
        assert approvals and "fake/mutate" in approvals[0]
    finally:
        provider.close()


def test_mcp_approval_and_result_redact_sensitive_fields(tmp_path: Path) -> None:
    approvals: list[str] = []
    provider = MCPToolProvider(
        [fake_config(tmp_path)],
        approver=lambda command, *_args: approvals.append(command) or True,
        approval_mode="risk",
    )
    try:
        provider.schemas()
        approved = provider.execute("mcp_fake_mutate", {"password": "visible-secret"})
        echoed = provider.execute(
            "mcp_fake_echo_read", {"value": {"access_token": "visible-token"}}
        )
        assert approved.ok is True
        assert "visible-secret" not in approvals[0]
        assert "[REDACTED]" in approvals[0]
        assert echoed.ok is True
        assert "visible-token" not in echoed.output
        assert "[REDACTED]" in echoed.output
    finally:
        provider.close()


def test_mcp_status_reports_errors_without_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVATE_MCP_TOKEN", "do-not-expose")
    missing = MCPServerConfig(
        name="missing",
        command=sys.executable,
        args=("-c", "pass"),
        env=("NOT_CONFIGURED",),
        cwd=tmp_path,
        timeout_seconds=1,
    )
    provider = MCPToolProvider([missing], approval_mode="full")
    try:
        provider.schemas()
        result = provider.execute("mcp_status", {})
        assert result.ok is True
        assert "NOT_CONFIGURED" in result.output
        assert "do-not-expose" not in result.output
    finally:
        provider.close()


def test_mcp_health_cooldown_and_approved_reconnect(tmp_path: Path) -> None:
    approvals: list[str] = []
    provider = MCPToolProvider(
        [fake_config(tmp_path)],
        approval_mode="full",
        approver=lambda command, *_args: approvals.append(command) or True,
    )
    try:
        schemas = provider.schemas()
        assert any(
            item["function"]["name"] == "mcp_reconnect" for item in schemas
        )
        client = provider._clients["fake"]
        attempts = 0

        def fail_call(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            raise MCPError("simulated transport failure")

        client.call_tool = fail_call  # type: ignore[method-assign]
        for _ in range(3):
            result = provider.execute("mcp_fake_echo_read", {"value": "x"})
            assert result.ok is False

        blocked = provider.execute("mcp_fake_echo_read", {"value": "x"})
        assert blocked.ok is False and "冷却期" in str(blocked.error)
        assert attempts == 3
        status = json.loads(provider.execute("mcp_status", {}).output)
        assert status["servers"]["fake"]["health"]["state"] == "cooldown"
        assert status["servers"]["fake"]["health"]["consecutive_failures"] == 3

        reconnected = provider.execute("mcp_reconnect", {"server": "fake"})
        assert reconnected.ok is True
        assert approvals == ["MCP reconnect fake"]
        refreshed = json.loads(provider.execute("mcp_status", {}).output)
        assert refreshed["servers"]["fake"]["health"]["state"] == "healthy"
        assert refreshed["servers"]["fake"]["health"]["consecutive_failures"] == 0
    finally:
        provider.close()


def test_mcp_only_inherits_explicit_environment_and_redacts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODING_AGENT_API_KEY", "agent-api-secret")
    monkeypatch.setenv("SOURCE_TOKEN", "mcp-tool-secret")
    base = fake_config(tmp_path)
    configured = MCPServerConfig(
        name=base.name,
        command=base.command,
        args=base.args,
        cwd=base.cwd,
        env_map=(("TARGET_TOKEN", "SOURCE_TOKEN"),),
        timeout_seconds=base.timeout_seconds,
    )
    provider = MCPToolProvider([configured], approval_mode="full")
    try:
        provider.schemas()
        result = provider.execute("mcp_fake_echo_read", {"value": "$ENV"})
        assert result.ok is True
        assert "mcp-tool-secret" not in result.output
        assert "agent-api-secret" not in result.output
        assert "[REDACTED]" in result.output
        assert '"agent_key": null' in result.output
    finally:
        provider.close()


def test_load_mcp_config_uses_environment_variable_names_only(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "demo": {
                        "command": "python",
                        "args": ["server.py"],
                        "env": ["SOURCE_TOKEN"],
                        "env_map": {"TARGET_TOKEN": "SOURCE_TOKEN"},
                        "cwd": ".",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    servers = load_mcp_config(config_path, workspace=tmp_path)

    assert servers[0].env == ("SOURCE_TOKEN",)
    assert servers[0].env_map == (("TARGET_TOKEN", "SOURCE_TOKEN"),)
    assert servers[0].cwd == tmp_path.resolve()


def test_load_mcp_config_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"servers": {"demo": {"command": "python", "cwd": ".."}}}),
        encoding="utf-8",
    )

    with pytest.raises(MCPError, match="超出工作区"):
        load_mcp_config(config_path, workspace=workspace)


def test_streamable_http_discovers_tools_parses_sse_and_closes_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: dict[str, object] = {
        "deleted": False,
        "sessions": [],
        "auth": [],
        "root_response": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            message = json.loads(self.rfile.read(length))
            method = message.get("method")
            records["auth"].append(self.headers.get("Authorization"))  # type: ignore[union-attr]
            records["sessions"].append(self.headers.get("Mcp-Session-Id"))  # type: ignore[union-attr]
            if method is None and message.get("id") == "http-root-1":
                records["root_response"] = message
                self.send_response(202)
                self.end_headers()
                return
            if "id" not in message:
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "http-demo", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "HTTP echo",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                }
            elif method == "tools/call":
                result = {
                    "content": [
                        {"type": "text", "text": message["params"]["arguments"]["text"]}
                    ],
                    "isError": False,
                }
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": result}
                ).encode("utf-8")
                body = b"event: message\n" + b"data: " + payload + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            elif method == "resources/list":
                result = {"resources": []}
            else:
                result = {}
            body = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": result}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if method == "initialize":
                self.send_header("Mcp-Session-Id", "session-123")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            notification = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/list_changed",
                    "params": {},
                }
            )
            roots_request = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "http-root-1",
                    "method": "roots/list",
                    "params": {},
                }
            )
            body = (
                f"event: message\ndata: {notification}\n\n"
                f"event: message\ndata: {roots_request}\n\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:  # noqa: N802
            records["deleted"] = self.headers.get("Mcp-Session-Id") == "session-123"
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MCP_HTTP_AUTH", "Bearer private-http-value")
    config = MCPServerConfig(
        name="http",
        transport="streamable_http",
        url=f"http://127.0.0.1:{server.server_port}/mcp",
        headers=(("Authorization", "MCP_HTTP_AUTH"),),
        roots=(tmp_path,),
        listen=True,
        timeout_seconds=5,
    )
    provider = MCPToolProvider([config], approval_mode="risk")
    try:
        schemas = provider.schemas()
        names = [item["function"]["name"] for item in schemas]
        assert "mcp_http_echo" in names
        result = provider.execute("mcp_http_echo", {"text": "over http"})
        assert result.ok is True
        assert "over http" in result.output
        assert "private-http-value" not in result.output
        assert records["auth"] and set(records["auth"]) == {"Bearer private-http-value"}
        sessions = records["sessions"]
        assert sessions[0] is None  # type: ignore[index]
        assert "session-123" in sessions[1:]  # type: ignore[operator]
        deadline = time.monotonic() + 2
        while records["root_response"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        root_response = records["root_response"]
        assert isinstance(root_response, dict)
        assert tmp_path.resolve().as_uri() in json.dumps(root_response)
        status = provider.execute("mcp_status", {})
        assert "notifications/resources/list_changed" in status.output
    finally:
        provider.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert records["deleted"] is True


def test_http_oauth_metadata_discovery_is_validated_and_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: dict[str, object] = {"base": "", "metadata_auth": [], "mcp_auth": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            base = str(records["base"])
            records["mcp_auth"].append(self.headers.get("Authorization"))  # type: ignore[union-attr]
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", scope="tools:read"',
            )
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            base = str(records["base"])
            records["metadata_auth"].append(self.headers.get("Authorization"))  # type: ignore[union-attr]
            if self.path == "/.well-known/oauth-protected-resource/mcp":
                value = {
                    "resource": f"{base}/mcp",
                    "authorization_servers": [f"{base}/issuer"],
                    "scopes_supported": ["tools:read"],
                    "bearer_methods_supported": ["header"],
                }
            elif self.path == "/.well-known/oauth-authorization-server/issuer":
                value = {
                    "issuer": f"{base}/issuer",
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "scopes_supported": ["tools:read"],
                    "grant_types_supported": ["authorization_code"],
                    "code_challenge_methods_supported": ["S256"],
                }
            else:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(value).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    records["base"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OAUTH_MCP_TOKEN", "Bearer stale-mcp-token")
    approvals: list[str] = []
    provider = MCPToolProvider(
        [
            MCPServerConfig(
                name="oauth",
                transport="streamable_http",
                url=f'{records["base"]}/mcp',
                headers=(("Authorization", "OAUTH_MCP_TOKEN"),),
                timeout_seconds=5,
            )
        ],
        approver=lambda command, *_args: approvals.append(command) or True,
    )
    try:
        schemas = provider.schemas()
        assert any(
            item["function"]["name"] == "mcp_discover_auth" for item in schemas
        )
        status = json.loads(provider.execute("mcp_status", {}).output)
        oauth = status["servers"]["oauth"]
        assert oauth["health"]["state"] == "auth_required", oauth
        assert oauth["authorization"]["scope"] == "tools:read"
        assert oauth["authorization"]["protected_resource"]["resource"].endswith(
            "/mcp"
        )

        discovered = provider.execute("mcp_discover_auth", {"server": "oauth"})
        assert discovered.ok is True
        assert '"token_endpoint"' in discovered.output
        assert '"S256"' in discovered.output
        assert "token 请求" in discovered.output
        assert approvals and "OAuth metadata oauth" in approvals[0]
        assert records["mcp_auth"] == ["Bearer stale-mcp-token"]
        assert records["metadata_auth"] == [None, None]
    finally:
        provider.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_oauth_rejects_mismatched_protected_resource_metadata() -> None:
    records: dict[str, str] = {"base": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Bearer resource_metadata="{records["base"]}/metadata"',
            )
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(
                {
                    "resource": f'{records["base"]}/different-resource',
                    "authorization_servers": [f'{records["base"]}/issuer'],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    records["base"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = MCPToolProvider(
        [
            MCPServerConfig(
                name="bad-oauth",
                transport="streamable_http",
                url=f'{records["base"]}/mcp',
                timeout_seconds=5,
            )
        ]
    )
    try:
        provider.schemas()
        status = json.loads(provider.execute("mcp_status", {}).output)
        authorization = status["servers"]["bad-oauth"]["authorization"]
        assert "resource 与 MCP URL 不一致" in authorization["discovery_error"]
        result = provider.execute("mcp_discover_auth", {"server": "bad-oauth"})
        assert result.ok is False
    finally:
        provider.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_load_streamable_http_config_references_header_environment(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable_http",
                        "url": "https://mcp.invalid/endpoint",
                        "headers": {"Authorization": "REMOTE_MCP_AUTH"},
                        "listen": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    servers = load_mcp_config(config_path, workspace=tmp_path)

    assert servers[0].transport == "streamable_http"
    assert servers[0].url == "https://mcp.invalid/endpoint"
    assert servers[0].headers == (("Authorization", "REMOTE_MCP_AUTH"),)
    assert servers[0].listen is True
    assert servers[0].command is None
