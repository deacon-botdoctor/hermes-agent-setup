"""Policy-gated MCP activation for long-lived Hermes gateway sessions.

Hermes intentionally skips ``mcp_servers`` entries with ``enabled: false``.
Bot Doctor runtimes use that state for cold, on-demand backends, so a model-
facing control path must activate the selected backend in the *current*
gateway process.  Starting it in a separate CLI process would register tools
in the wrong registry and leave the active conversation unchanged.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_POLICY_KEYS = ("on_demand", "active_enabled", "hot_path", "hot_path_enabled")
_DENY_POLICY_KEYS = ("disabled", "on_demand_disabled")
_CONTROL_LOCK = threading.RLock()
_STATE_DIR = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser() / "state" / "mcp-activation"
_HEARTBEAT_INTERVAL_S = 5.0
_CLEANUP_REGISTERED = False
_HEARTBEAT_THREAD: threading.Thread | None = None


def _state_path() -> Path:
    return _STATE_DIR / f"{os.getpid()}.json"


def _cleanup_activation_state() -> None:
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass


def _write_activation_state(
    config: dict[str, Any],
    allowed: set[str],
    rows: dict[str, dict[str, Any]],
) -> bool:
    """Publish connected backends for this Hermes host process."""
    global _CLEANUP_REGISTERED
    denied = _policy_names(config, _DENY_POLICY_KEYS)
    active_servers = sorted(
        name
        for name, server_config in _configured_servers(config).items()
        if name not in denied
        and (name in allowed or server_config.get("enabled") is True)
        and rows.get(name, {}).get("connected") is True
        and str(rows.get(name, {}).get("status")) == "connected"
        and int(rows.get(name, {}).get("tools") or 0) > 0
    )
    state_path = _state_path()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "host_pid": os.getpid(),
                    "verified_at": time.time(),
                    "active_servers": active_servers,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(state_path)
    except OSError:
        return False
    if not _CLEANUP_REGISTERED:
        atexit.register(_cleanup_activation_state)
        _CLEANUP_REGISTERED = True
    return True


def _refresh_activation_state(config: dict[str, Any], allowed: set[str]) -> bool:
    try:
        rows = _status_index()
    except Exception:
        rows = {}
    return _write_activation_state(config, allowed, rows)


def _activation_heartbeat(config: dict[str, Any], allowed: set[str]) -> None:
    while True:
        time.sleep(_HEARTBEAT_INTERVAL_S)
        _refresh_activation_state(config, allowed)


def _ensure_activation_heartbeat(config: dict[str, Any], allowed: set[str]) -> None:
    global _HEARTBEAT_THREAD
    if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
        return
    _HEARTBEAT_THREAD = threading.Thread(
        target=_activation_heartbeat,
        args=(config, allowed),
        name="mcp-activation-heartbeat",
        daemon=True,
    )
    _HEARTBEAT_THREAD.start()


def _load_config() -> dict[str, Any]:
    from hermes_cli.config import load_config

    config = load_config() or {}
    return config if isinstance(config, dict) else {}


def _get_status() -> list[dict[str, Any]]:
    from tools.mcp_tool import get_mcp_status

    return list(get_mcp_status() or [])


def _register_server(server_name: str, server_config: dict[str, Any]) -> None:
    from tools.mcp_tool import register_mcp_servers

    register_mcp_servers({server_name: server_config})


def _get_live_server(server_name: str) -> Any:
    from tools import mcp_tool

    with mcp_tool._lock:
        return mcp_tool._servers.get(server_name)


def _reconnect_live_server(server_name: str, server: Any) -> bool:
    from tools.mcp_tool import _signal_reconnect_and_wait

    return bool(
        _signal_reconnect_and_wait(
            server_name,
            server,
            op_description="operator-requested restart",
            timeout=30.0,
        )
    )


def _launch_identity(config: dict[str, Any]) -> tuple[Any, ...]:
    """Return only the process-shaping fields for an in-process MCP rebind."""
    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    env_identity = tuple(
        (key, str(env.get(key, "")))
        for key in ("HERMES_HOME", "PYTHONPATH", "VIRTUAL_ENV", "ANAMNESIS_DB", "PATH")
        if key in env
    )
    args = config.get("args")
    return (
        str(config.get("command", "")),
        tuple(str(item) for item in args) if isinstance(args, list) else (),
        str(config.get("url", "")),
        str(config.get("cwd", "")),
        env_identity,
    )


def _rebind_live_server(server_name: str, server: Any) -> None:
    """Replace one stale current-process MCP transport using current config."""
    from tools import mcp_tool

    mcp_tool._run_on_mcp_loop(lambda: server.shutdown(), timeout=30.0)
    with mcp_tool._lock:
        if mcp_tool._servers.get(server_name) is server:
            mcp_tool._servers.pop(server_name, None)


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _policy_names(config: dict[str, Any], keys: tuple[str, ...]) -> set[str]:
    raw = config.get("mcp_policy") or {}
    if not isinstance(raw, dict):
        return set()
    names: set[str] = set()
    for key in keys:
        names.update(_string_set(raw.get(key)))
    return names


def _policy(config: dict[str, Any]) -> tuple[set[str], set[str]]:
    raw = config.get("mcp_policy") or {}
    if not isinstance(raw, dict):
        return set(), set()
    denied = _policy_names(config, _DENY_POLICY_KEYS)
    on_demand = _string_set(raw.get("on_demand")) - denied
    allowed = _policy_names(config, _POLICY_KEYS) - denied
    return allowed, on_demand


def _configured_servers(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("mcp_servers") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): value for name, value in raw.items() if isinstance(value, dict)}


def _status_index() -> dict[str, dict[str, Any]]:
    return {str(row.get("name")): row for row in _get_status() if isinstance(row, dict) and row.get("name")}


def _public_state(
    server_name: str,
    row: dict[str, Any] | None,
    *,
    on_demand: set[str],
) -> dict[str, Any]:
    row = row or {}
    raw_status = str(row.get("status") or "configured")
    if server_name in on_demand and raw_status == "disabled":
        status = "cold"
    elif raw_status in {"connected", "connecting", "failed", "configured", "disabled"}:
        status = raw_status
    else:
        status = "unavailable"
    return {
        "server_name": server_name,
        "status": status,
        "connected": bool(row.get("connected")),
        "tools": max(0, int(row.get("tools") or 0)),
        "policy": "on_demand" if server_name in on_demand else "active",
    }


def mcp_server_status(server_name: str = "", **_: Any) -> str:
    """Return sanitized state for policy-allowed MCP servers."""
    config = _load_config()
    configured = _configured_servers(config)
    allowed, on_demand = _policy(config)
    requested = str(server_name or "").strip()
    rows = _status_index()
    _write_activation_state(config, allowed, rows)

    if requested:
        if requested not in configured:
            return json.dumps({"ok": False, "server_name": requested, "status": "missing"})
        if requested not in allowed:
            return json.dumps({"ok": False, "server_name": requested, "status": "not_allowed"})
        return json.dumps(
            {"ok": True, **_public_state(requested, rows.get(requested), on_demand=on_demand)},
            sort_keys=True,
        )

    states = [_public_state(name, rows.get(name), on_demand=on_demand) for name in sorted(allowed & set(configured))]
    return json.dumps({"ok": True, "servers": states}, sort_keys=True)


def _wait_for_terminal_state(server_name: str, *, timeout: float = 30.0) -> dict[str, Any] | None:
    iterations = max(1, int(timeout / 0.25))
    last: dict[str, Any] | None = None
    for index in range(iterations):
        last = _status_index().get(server_name)
        if last and str(last.get("status")) == "failed":
            return last
        if (
            last
            and str(last.get("status")) == "connected"
            and last.get("connected") is True
            and int(last.get("tools") or 0) > 0
        ):
            return last
        if index < iterations - 1:
            time.sleep(0.25)
    return last


def restart_mcp_server(
    server_name: str = "",
    *,
    ensure_active: bool = False,
    **_: Any,
) -> str:
    """Activate or reconnect one allowed MCP.

    When ``ensure_active`` is true, preserve a ready live session and report it
    as ``already_active`` instead of reconnecting it.
    """
    requested = str(server_name or "").strip()
    if not requested:
        return json.dumps({"ok": False, "status": "invalid", "message": "server_name is required"})

    with _CONTROL_LOCK:
        config = _load_config()
        configured = _configured_servers(config)
        allowed, on_demand = _policy(config)
        if requested not in configured:
            return json.dumps({"ok": False, "server_name": requested, "status": "missing"})
        if requested not in allowed:
            return json.dumps({"ok": False, "server_name": requested, "status": "not_allowed"})

        before_row = _status_index().get(requested)
        before = _public_state(requested, before_row, on_demand=on_demand)
        live_server = _get_live_server(requested)
        action = "activated" if before["status"] in {"cold", "disabled", "configured"} else "reconnected"

        try:
            launch_changed = (
                live_server is not None
                and getattr(live_server, "session", None) is not None
                and hasattr(live_server, "_config")
                and _launch_identity(live_server._config) != _launch_identity(configured[requested])
            )
            if launch_changed:
                action = "rebound_config"
                server_config = dict(configured[requested])
                _rebind_live_server(requested, live_server)
                server_config["enabled"] = True
                _register_server(requested, server_config)
            elif (
                ensure_active
                and before["status"] == "connected"
                and before["connected"] is True
                and before["tools"] > 0
                and live_server is not None
                and getattr(live_server, "session", None) is not None
            ):
                action = "already_active"
            elif live_server is not None and getattr(live_server, "session", None) is not None:
                action = "restarted"
                if not _reconnect_live_server(requested, live_server):
                    raise RuntimeError("reconnect did not become ready")
            else:
                server_config = dict(configured[requested])
                server_config["enabled"] = True
                _register_server(requested, server_config)
        except Exception:
            _refresh_activation_state(config, allowed)
            return json.dumps(
                {
                    "ok": False,
                    "server_name": requested,
                    "status": "failed",
                    "action": action,
                    "retryable": True,
                    "message": "MCP activation failed; inspect the gateway MCP log for the sanitized transport error.",
                },
                sort_keys=True,
            )

        after_row = _wait_for_terminal_state(requested)
        after = _public_state(requested, after_row, on_demand=on_demand)
        ok = after["status"] == "connected" and after["connected"] is True and after["tools"] > 0
        _refresh_activation_state(config, allowed)
        result = {
            "ok": ok,
            "server_name": requested,
            "status": after["status"],
            "connected": after["connected"],
            "tools": after["tools"],
            "action": action,
        }
        if ok:
            _ensure_activation_heartbeat(config, allowed)
            result["next"] = (
                "Run tool_search again for the requested capability, then invoke the returned tool with tool_call."
            )
        else:
            result["retryable"] = True
            result["message"] = "MCP activation did not reach connected state; inspect the gateway MCP log."
        return json.dumps(result, sort_keys=True)


STATUS_SCHEMA = {
    "name": "mcp_server_status",
    "description": (
        "Inspect whether an allowlisted MCP backend is connected, cold, or failed. "
        "Use the backend/source name returned by capability discovery (for example a "
        "Calendar, Gmail, Drive, Stripe, or other MCP server), not a tool function name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "server_name": {
                "type": "string",
                "description": "Exact configured MCP server/backend name. Omit to list all policy-allowed backends.",
            }
        },
    },
}

RESTART_SCHEMA = {
    "name": "restart_mcp_server",
    "description": (
        "Activate a cold allowlisted MCP backend inside the current gateway process, or "
        "reconnect a failed backend. Use the exact backend/source name from capability "
        "discovery. After success, search again for the actual capability tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "server_name": {
                "type": "string",
                "description": "Exact configured MCP server/backend name to activate or reconnect.",
            }
        },
        "required": ["server_name"],
    },
}


def start_activation_heartbeat() -> None:
    try:
        config = _load_config()
        allowed, _ = _policy(config)
        _ensure_activation_heartbeat(config, allowed)
    except Exception:
        pass


def status_handler(args: dict[str, Any], **kwargs: Any) -> str:
    return mcp_server_status(server_name=args.get("server_name", ""), **kwargs)


def restart_handler(
    args: dict[str, Any],
    _mcp_ensure_active: bool = False,
    **kwargs: Any,
) -> str:
    """Accept ensure-active only from trusted dispatch context, never ``args``."""
    return restart_mcp_server(
        server_name=args.get("server_name", ""),
        ensure_active=_mcp_ensure_active is True,
        **kwargs,
    )
