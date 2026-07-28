"""Side-effect-free registration contract for MCP control tools."""

from __future__ import annotations

from .control import (
    RESTART_SCHEMA,
    STATUS_SCHEMA,
    restart_handler,
    start_activation_heartbeat,
    status_handler,
)

TOOLSET = "mcp-on-demand-control"


def register(ctx):
    start_activation_heartbeat()
    ctx.register_tool(
        name="mcp_server_status",
        toolset=TOOLSET,
        schema=STATUS_SCHEMA,
        handler=status_handler,
        description="Inspect policy-allowed MCP backend state",
        emoji="🔌",
    )
    ctx.register_tool(
        name="restart_mcp_server",
        toolset=TOOLSET,
        schema=RESTART_SCHEMA,
        handler=restart_handler,
        description="Activate or reconnect a policy-allowed MCP backend",
        emoji="🔄",
    )
