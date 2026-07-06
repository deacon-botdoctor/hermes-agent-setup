from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - keeps tests runnable without mcp installed.
    FastMCP = None  # type: ignore[assignment]


BROWSER_LANE_SOCKET = Path(os.environ.get("BROWSER_LANE_SOCKET", "~/.hermes/browser-lane/daemon.sock")).expanduser()
BROWSER_LANE_TIMEOUT = float(os.environ.get("BROWSER_LANE_TIMEOUT", "15"))
BROWSER_CDP_URL = os.environ.get("BROWSER_CDP_URL", "http://127.0.0.1:9230")

mcp = FastMCP("browser-lane") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _send(payload: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(BROWSER_LANE_TIMEOUT)
        client.connect(str(BROWSER_LANE_SOCKET))
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


@_tool
def browser_lane_status() -> dict[str, Any]:
    exists = BROWSER_LANE_SOCKET.exists()
    return {
        "ok": exists,
        "socket": str(BROWSER_LANE_SOCKET),
        "cdp_url": BROWSER_CDP_URL,
        "error": None if exists else "browser-lane socket not found",
    }


@_tool
def browser_lane_command(command: str, **params: Any) -> dict[str, Any]:
    payload = {"command": command, "params": params}
    try:
        result = _send(payload)
    except Exception as exc:
        return {"ok": False, "socket": str(BROWSER_LANE_SOCKET), "command": command, "error": str(exc)}
    if "ok" not in result:
        result = {"ok": True, "result": result}
    return result


@_tool
def browser_lane_open(url: str) -> dict[str, Any]:
    return browser_lane_command("open", url=url)


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the browser-lane MCP server.")
    mcp.run()


if __name__ == "__main__":
    main()
