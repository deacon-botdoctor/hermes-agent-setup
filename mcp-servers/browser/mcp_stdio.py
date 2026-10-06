#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable


ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str, data: Any | None = None) -> None:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    if data is not None:
        payload["error"]["data"] = data
    _write(payload)


def _tool_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, ensure_ascii=False),
            }
        ]
    }


def serve(name: str, version: str, tools: list[Tool]) -> None:
    tool_map = {tool.name: tool for tool in tools}

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue

        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            _error(None, -32700, "Parse error", str(exc))
            continue

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if request_id is None:
            continue

        try:
            if method == "initialize":
                _result(
                    request_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": name, "version": version},
                    },
                )
            elif method == "tools/list":
                _result(
                    request_id,
                    {
                        "tools": [
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "inputSchema": tool.input_schema,
                            }
                            for tool in tools
                        ]
                    },
                )
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                tool = tool_map.get(tool_name)
                if tool is None:
                    _error(request_id, -32602, f"Unknown tool: {tool_name}")
                    continue
                _result(request_id, _tool_result(tool.handler(arguments)))
            else:
                _error(request_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            _error(request_id, -32000, str(exc), traceback.format_exc(limit=4))
