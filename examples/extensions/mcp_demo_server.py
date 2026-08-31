"""Tiny dependency-free MCP stdio server used by the local demo."""

from __future__ import annotations

import json
import sys
from typing import Any


def respond(identifier: Any, result: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}) + "\n"
    )
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        respond(
            message["id"],
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "coding-agent-demo", "version": "1.0"},
            },
        )
    elif method == "tools/list":
        respond(
            message["id"],
            {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Return the supplied text from a separate MCP process.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            },
        )
    elif method == "tools/call":
        arguments = message.get("params", {}).get("arguments", {})
        respond(
            message["id"],
            {
                "content": [{"type": "text", "text": str(arguments.get("text", ""))}],
                "isError": False,
            },
        )
    elif method == "resources/list":
        respond(
            message["id"],
            {
                "resources": [
                    {
                        "uri": "demo://project-guidance",
                        "name": "project-guidance",
                        "description": "Small read-only resource for the MCP demo.",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
    elif method == "resources/read":
        respond(
            message["id"],
            {
                "contents": [
                    {
                        "uri": message.get("params", {}).get("uri", ""),
                        "mimeType": "text/plain",
                        "text": "Inspect existing code before proposing a change.",
                    }
                ]
            },
        )
    elif method == "prompts/list":
        respond(
            message["id"],
            {
                "prompts": [
                    {
                        "name": "review-change",
                        "description": "Build a short review request.",
                        "arguments": [{"name": "path", "required": True}],
                    }
                ]
            },
        )
    elif method == "prompts/get":
        path = message.get("params", {}).get("arguments", {}).get("path", "the change")
        respond(
            message["id"],
            {
                "description": "Generated review request",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": f"Review {path} for correctness."},
                    }
                ],
            },
        )
    else:
        sys.stdout.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
            + "\n"
        )
        sys.stdout.flush()
