from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - keeps tests runnable without mcp installed.
    FastMCP = None  # type: ignore[assignment]


DIRECTORY_PATH = Path(os.environ.get("TELEGRAM_DIRECTORY", "telegram-directory.json")).expanduser()

mcp = FastMCP("telegram-admin") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _load_directory() -> dict[str, Any]:
    if not DIRECTORY_PATH.exists():
        return {"channels": []}
    data = json.loads(DIRECTORY_PATH.read_text())
    if isinstance(data, list):
        return {"channels": data}
    return data if isinstance(data, dict) else {"channels": []}


def _scrub(entry: dict[str, Any]) -> dict[str, Any]:
    allowed = ["id", "name", "purpose", "thread_key", "message_thread_id", "boundary_class", "client_lock"]
    return {key: entry.get(key) for key in allowed if key in entry}


@_tool
def telegram_admin_lookup(query: str, limit: int = 8) -> dict[str, Any]:
    terms = {term.lower() for term in query.split() if term.strip()}
    directory = _load_directory()
    hits: list[dict[str, Any]] = []
    for entry in directory.get("channels", []):
        if not isinstance(entry, dict):
            continue
        text = " ".join(str(value) for value in entry.values()).lower()
        if not terms or any(term in text for term in terms):
            hits.append(_scrub(entry))
    return {"ok": True, "query": query, "matches": hits[: max(1, int(limit))]}


@_tool
def telegram_admin_status() -> dict[str, Any]:
    directory = _load_directory()
    return {"ok": True, "directory": str(DIRECTORY_PATH), "channels_total": len(directory.get("channels", []))}


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the telegram-admin MCP server.")
    mcp.run()


if __name__ == "__main__":
    main()
