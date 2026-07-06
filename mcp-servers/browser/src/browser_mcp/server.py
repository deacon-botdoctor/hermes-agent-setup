from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - keeps tests runnable without mcp installed.
    FastMCP = None  # type: ignore[assignment]


BROWSER_CDP_URL = os.environ.get("BROWSER_CDP_URL", "http://127.0.0.1:9230").rstrip("/")
DEFAULT_TIMEOUT = float(os.environ.get("BROWSER_MCP_TIMEOUT", "10"))

mcp = FastMCP("browser") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _json_get(path: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    url = f"{BROWSER_CDP_URL}{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body else {}


@_tool
def browser_status() -> dict[str, Any]:
    try:
        version = _json_get("/json/version")
    except Exception as exc:
        return {"ok": False, "cdp_url": BROWSER_CDP_URL, "error": str(exc)}
    return {"ok": True, "cdp_url": BROWSER_CDP_URL, "version": version}


@_tool
def list_targets() -> dict[str, Any]:
    try:
        targets = _json_get("/json/list")
    except Exception as exc:
        return {"ok": False, "cdp_url": BROWSER_CDP_URL, "error": str(exc), "targets": []}
    return {"ok": True, "cdp_url": BROWSER_CDP_URL, "targets": targets if isinstance(targets, list) else []}


@_tool
def cdp_json(path: str) -> dict[str, Any]:
    safe_path = path if path.startswith("/") else f"/{path}"
    try:
        data = _json_get(safe_path)
    except Exception as exc:
        return {"ok": False, "cdp_url": BROWSER_CDP_URL, "path": safe_path, "error": str(exc)}
    return {"ok": True, "cdp_url": BROWSER_CDP_URL, "path": safe_path, "data": data}


def new_page(url: str = "about:blank") -> dict[str, Any]:
    encoded = urllib.parse.quote(url, safe="")
    return cdp_json(f"/json/new?{encoded}")


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the browser MCP server.")
    mcp.run()


if __name__ == "__main__":
    main()
