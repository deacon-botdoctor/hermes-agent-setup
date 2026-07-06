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


SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8080").rstrip("/")
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "http://127.0.0.1:3002").rstrip("/")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")
DEFAULT_TIMEOUT = float(os.environ.get("WEB_SEARCH_MCP_TIMEOUT", "20"))

mcp = FastMCP("web-search") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _json_request(url: str, payload: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        if FIRECRAWL_API_KEY:
            headers["Authorization"] = f"Bearer {FIRECRAWL_API_KEY}"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body else {}


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title") or item.get("name") or "",
        "url": item.get("url") or item.get("link") or "",
        "snippet": item.get("content") or item.get("snippet") or item.get("description") or "",
        "source": item.get("engine") or item.get("source") or "searxng",
    }


@_tool
def web_search(query: str, max_results: int = 8) -> dict[str, Any]:
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    try:
        data = _json_request(f"{SEARXNG_URL}/search?{params}")
    except Exception as exc:
        return {"ok": False, "backend": "searxng", "error": str(exc), "results": []}
    raw_results = data.get("results", []) if isinstance(data, dict) else []
    return {
        "ok": True,
        "backend": "searxng",
        "query": query,
        "results": [_normalize(item) for item in raw_results[: max(1, int(max_results))]],
    }


@_tool
def scrape(url: str, only_main_content: bool = True) -> dict[str, Any]:
    payload = {"url": url, "formats": ["markdown"], "onlyMainContent": only_main_content}
    try:
        data = _json_request(f"{FIRECRAWL_URL}/v1/scrape", payload)
    except Exception as exc:
        return {"ok": False, "backend": "firecrawl", "url": url, "error": str(exc)}
    page = data.get("data", data) if isinstance(data, dict) else {}
    return {
        "ok": True,
        "backend": "firecrawl",
        "url": url,
        "title": page.get("metadata", {}).get("title") if isinstance(page, dict) else None,
        "markdown": page.get("markdown", "") if isinstance(page, dict) else "",
        "metadata": page.get("metadata", {}) if isinstance(page, dict) else {},
    }


@_tool
def web_search_status() -> dict[str, Any]:
    return {
        "ok": True,
        "searxng_url": SEARXNG_URL,
        "firecrawl_url": FIRECRAWL_URL,
        "firecrawl_auth": "configured" if FIRECRAWL_API_KEY else "not_configured",
    }


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the web-search MCP server.")
    mcp.run()


if __name__ == "__main__":
    main()
