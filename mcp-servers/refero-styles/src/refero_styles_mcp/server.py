"""Read-only MCP for styles.refero.design."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from refero_styles_mcp import client as refero

mcp = MCPServer("refero-styles")


@mcp.tool()
def refero_search(query: str, limit: int = 8) -> dict[str, Any]:
    """Search the public Refero style library. Returns 1-3 usable matches plus extras."""
    return refero.search_styles(query, limit=limit)


@mcp.tool()
def refero_get(style_id: str) -> dict[str, Any]:
    """Get one Refero style by id, site name, or hostname."""
    return refero.get_style(style_id)


@mcp.tool()
def refero_list(limit: int = 20) -> dict[str, Any]:
    """List cached Refero styles."""
    catalog = refero.list_catalog()
    cards = [refero._card(item) for item in catalog[: max(1, min(int(limit), 50))]]
    return {"ok": True, "count": len(cards), "total": len(catalog), "styles": cards}


@mcp.tool()
def refero_similar(style_id: str, limit: int = 8) -> dict[str, Any]:
    """List similar Refero styles."""
    return refero.similar_styles(style_id, limit=limit)


@mcp.tool()
def refero_design_md(style_id: str) -> dict[str, Any]:
    """Render DESIGN.md in-session. Never writes a file."""
    return refero.render_design_md(style_id)


@mcp.tool()
def refero_refresh() -> dict[str, Any]:
    """Drop the local Refero cache."""
    return refero.refresh_catalog()


if __name__ == "__main__":
    mcp.run()
