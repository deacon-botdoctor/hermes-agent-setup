#!/usr/bin/env python3
"""mac-control MCP — open apps, list running apps, send notifications on macOS."""

import subprocess
from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP("mac-control")


@mcp.tool()
def open_app(app_name: str) -> dict:
    """Open a macOS application by name (as shown in Finder or /Applications).
    Examples: Safari, Notes, Screenshot, System Preferences, Mail, Calculator.
    """
    result = subprocess.run(
        ["open", "-a", app_name],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or f"Could not open '{app_name}'"}
    return {"ok": True, "opened": app_name}


@mcp.tool()
def list_running_apps() -> dict:
    """List all currently running foreground applications on this Mac."""
    script = (
        'tell application "System Events" to get name of every application process '
        'whose background only is false'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    apps = sorted(a.strip() for a in result.stdout.strip().split(",") if a.strip())
    return {"ok": True, "apps": apps, "count": len(apps)}


@mcp.tool()
def get_frontmost_app() -> dict:
    """Get the name of the currently focused (frontmost) application."""
    script = (
        'tell application "System Events" to get name of first application process '
        'whose frontmost is true'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True, "app": result.stdout.strip()}


@mcp.tool()
def show_notification(title: str, message: str, subtitle: str = "") -> dict:
    """Send a macOS system notification to this machine's notification center."""
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv) "
        "subtitle (item 3 of argv)\n"
        "end run"
    )
    result = subprocess.run(
        ["osascript", "-e", script, "--", title, message, subtitle],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()}
    return {"ok": True}


if __name__ == "__main__":
    mcp.run()
