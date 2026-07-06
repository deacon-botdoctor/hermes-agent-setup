from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - keeps tests runnable without mcp installed.
    FastMCP = None  # type: ignore[assignment]


ASSET_MANIFEST = Path(os.environ.get("VISUAL_IDENTITY_MANIFEST", "visual-assets.json")).expanduser()
ASSET_ROOT = Path(os.environ.get("VISUAL_IDENTITY_ROOT", ".")).expanduser().resolve()

mcp = FastMCP("visual-identity") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _load_assets() -> list[dict[str, Any]]:
    if not ASSET_MANIFEST.exists():
        return []
    data = json.loads(ASSET_MANIFEST.read_text())
    if isinstance(data, dict):
        assets = data.get("assets", [])
    elif isinstance(data, list):
        assets = data
    else:
        assets = []
    return [asset for asset in assets if isinstance(asset, dict)]


def _safe_asset(asset: dict[str, Any]) -> dict[str, Any]:
    result = {key: asset.get(key) for key in ["id", "label", "kind", "tags", "notes"] if key in asset}
    path = asset.get("path")
    if path:
        resolved = (ASSET_ROOT / str(path)).resolve()
        try:
            resolved.relative_to(ASSET_ROOT)
            result["path"] = str(path)
        except ValueError:
            result["path"] = None
    return result


@_tool
def visual_identity_search(query: str, limit: int = 8) -> dict[str, Any]:
    terms = {term.lower() for term in query.split() if term.strip()}
    hits: list[dict[str, Any]] = []
    for asset in _load_assets():
        text = " ".join(str(asset.get(key, "")) for key in ["id", "label", "kind", "tags", "notes"]).lower()
        if not terms or any(term in text for term in terms):
            hits.append(_safe_asset(asset))
    return {"ok": True, "query": query, "assets": hits[: max(1, int(limit))]}


@_tool
def visual_identity_get(asset_id: str) -> dict[str, Any]:
    for asset in _load_assets():
        if str(asset.get("id")) == asset_id:
            return {"ok": True, "asset": _safe_asset(asset)}
    return {"ok": False, "error": "asset_not_found", "asset_id": asset_id}


@_tool
def visual_identity_status() -> dict[str, Any]:
    return {"ok": True, "manifest": str(ASSET_MANIFEST), "assets_total": len(_load_assets())}


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the visual-identity MCP server.")
    mcp.run()


if __name__ == "__main__":
    main()
