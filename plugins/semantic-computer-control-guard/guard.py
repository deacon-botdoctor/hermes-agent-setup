from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STATE: dict[str, str] = {}
_BLOCKED_CALLS: set[tuple[str, str]] = set()

_READ_ACTIONS = {"list_apps", "list_windows", "wait"}
_CAPTURE_ACTIONS = {"capture", "cua_browser_state"}
_BROWSER_ACTIONS = {
    "cua_browser_prepare",
    "cua_browser_navigate",
    "cua_browser_click",
    "cua_browser_type",
    "cua_browser_pointer",
    "cua_browser_dialog",
    "cua_browser_set_input_files",
    "cua_browser_download",
}
_NATIVE_ACTIONS = {
    "click",
    "double_click",
    "right_click",
    "middle_click",
    "drag",
    "scroll",
    "type",
    "key",
    "set_value",
    "focus_app",
}
_DIRECT_UI_TOOL_NAMES = {
    "terminal",
    "shell",
    "bash",
    "powershell",
    "code_execution",
    "execute_code",
    "write_file",
    "patch",
    "apply_patch",
    "edit_file",
}
_DIRECT_UI_TOOL_NAME_PATTERN = re.compile(
    r"(?:^|[_-])(?:apple[_-]?script|mac[_-]?control|cua[_-]?driver|"
    r"desktop[_-]?(?:control|input)|pyautogui|xdotool)(?:$|[_-])",
    re.IGNORECASE,
)
_DIRECT_UI_PATTERNS = (
    re.compile(r"\bosascript\b|\bNSAppleScript\b|\bScriptingBridge\b", re.IGNORECASE),
    re.compile(
        r"tell\s+application\s+[\"'](?:System Events|Finder|Safari|Google Chrome|Brave Browser)",
        re.IGNORECASE,
    ),
    re.compile(r"\bAXUIElement\b|\bCGEvent(?:Create|Post)\b|\bQuartz\.CGEvent", re.IGNORECASE),
    re.compile(
        r"\b(?:cliclick|pyautogui|pynput|xdotool|ydotool|robotjs|nut\.js|AutoHotkey)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:SendKeys|WScript\.Shell)\b", re.IGNORECASE),
    re.compile(r"\bcua-driver\b", re.IGNORECASE),
    re.compile(r"\bmac-control\b", re.IGNORECASE),
    re.compile(
        r"(?:^|[/\\])(?:bulk[_-](?:upload|ui|input)|ui[_-]automation)\."
        r"(?:sh|py|js)\b",
        re.IGNORECASE,
    ),
)


def _runtime_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def _plugin_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else _runtime_config()
    return (
        ((config.get("plugins") or {}).get("entries") or {}).get(
            "semantic-computer-control-guard"
        )
        or {}
    )


def _device_posture(settings: dict[str, Any] | None = None) -> str:
    settings = settings if settings is not None else _plugin_settings()
    return str(settings.get("device_posture") or "standard").strip().lower()


def _canonical_identity(value: Any, *, hostname: bool = False) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized.rstrip(".") if hostname else normalized


def _dedicated_binding_error(
    config: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> str | None:
    config = config if config is not None else _runtime_config()
    settings = settings if settings is not None else _plugin_settings(config)
    posture = _device_posture(settings)
    if posture == "standard":
        return None
    if posture != "dedicated_principal":
        return f"unsupported device_posture {posture!r}"

    required = {
        "principal_id": _canonical_identity(settings.get("principal_id")),
        "agent_id": _canonical_identity(settings.get("agent_id")),
        "device_id": _canonical_identity(settings.get("device_id"), hostname=True),
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        return "dedicated-principal binding is missing " + ", ".join(missing)
    if str(settings.get("tool_authority") or "").strip() != "outcome_scoped":
        return "dedicated-principal tool_authority must be outcome_scoped"

    observed_principal = _canonical_identity(config.get("client_identity"))
    observed_agent = _canonical_identity(
        os.getenv("HERMES_AGENT_ID") or os.getenv("HERMES_PROFILE")
    )
    observed_device = _canonical_identity(socket.gethostname(), hostname=True)
    observed = {
        "principal_id": observed_principal,
        "agent_id": observed_agent,
        "device_id": observed_device,
    }
    mismatched = sorted(
        name for name in required if required[name] != observed[name]
    )
    if mismatched:
        return "dedicated-principal binding mismatch: " + ", ".join(mismatched)
    return None


def _policy_enabled() -> bool:
    if os.getenv("HERMES_SEMANTIC_COMPUTER_CONTROL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    settings = _plugin_settings()
    return bool(settings.get("semantic_control_only")) or _device_posture(
        settings
    ) != "standard"


def _foreground_escalation_allowed() -> bool:
    return bool(_plugin_settings().get("allow_foreground_escalation"))


def _block(message: str) -> dict[str, str]:
    return {
        "action": "block",
        "message": (
            f"Semantic computer-control policy blocked this call: {message}. "
            "Use computer_use with fresh semantic state. Background delivery is "
            "preferred; foreground requires the profile's explicit escalation opt-in. "
            "No direct UI fallback is permitted."
        ),
    }


def _serialized(args: Any) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(args)


def _direct_ui_block(tool_name: str, args: Any) -> dict[str, str] | None:
    if tool_name not in _DIRECT_UI_TOOL_NAMES:
        return None
    payload = _serialized(args)
    for pattern in _DIRECT_UI_PATTERNS:
        if pattern.search(payload):
            logger.warning(
                "semantic-computer-control: blocked direct UI primitive "
                "tool=%s pattern=%s",
                tool_name,
                pattern.pattern,
            )
            return _block("direct desktop scripting or input injection is forbidden")
    return None


def _session_key(kwargs: dict[str, Any]) -> str:
    return str(
        kwargs.get("session_id")
        or kwargs.get("task_id")
        or kwargs.get("turn_id")
        or ""
    )


def _call_key(
    session: str, kwargs: dict[str, Any]
) -> tuple[str, str] | None:
    tool_call_id = str(kwargs.get("tool_call_id") or "")
    return (session, tool_call_id) if tool_call_id else None


def _block_computer_call(
    session: str, kwargs: dict[str, Any], message: str
) -> dict[str, str]:
    call_key = _call_key(session, kwargs)
    with _LOCK:
        if call_key is not None:
            _BLOCKED_CALLS.add(call_key)
    return _block(message)


def _pin_permission_mode(mode: str) -> None:
    if mode not in {"standard", "unrestricted"}:
        raise ValueError(f"unsupported computer-use permission mode {mode!r}")
    from tools.computer_use import tool as computer_use_tool

    if not hasattr(computer_use_tool, "_cua_permission_mode"):
        raise RuntimeError(
            "Hermes computer_use permission resolver is unavailable; refusing "
            "semantic control without a configured permission-mode pin"
        )

    def _configured_mode(_session_id: str) -> str:
        return mode

    computer_use_tool._cua_permission_mode = _configured_mode


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **kwargs: Any):
    if tool_name != "computer_use" and _DIRECT_UI_TOOL_NAME_PATTERN.search(
        str(tool_name)
    ):
        return _block("legacy or direct desktop-control tools are forbidden")
    direct = _direct_ui_block(str(tool_name), args)
    if direct is not None:
        return direct
    if tool_name != "computer_use":
        return None

    call = args if isinstance(args, dict) else {}
    action = str(call.get("action") or "")
    session = _session_key(kwargs)

    def deny(message: str) -> dict[str, str]:
        return _block_computer_call(session, kwargs, message)

    if not session:
        return deny("a session, task, or turn identity is required")

    config = _runtime_config()
    settings = _plugin_settings(config)
    posture = _device_posture(settings)
    binding_error = _dedicated_binding_error(config, settings)
    if binding_error is not None:
        return deny(binding_error)
    permission_mode = (
        "unrestricted" if posture == "dedicated_principal" else "standard"
    )
    try:
        _pin_permission_mode(permission_mode)
    except Exception as exc:
        logger.error(
            "semantic-computer-control: permission-mode pin failed: %s", exc
        )
        return deny("the configured permission-mode pin is unavailable")

    requested_mode = call.get("permission_mode")
    if requested_mode is not None and requested_mode != permission_mode:
        return deny(
            f"permission_mode is bound to {permission_mode!r} by device posture"
        )
    foreground_requested = call.get("delivery_mode") == "foreground" or any(
        call.get(key) is True for key in ("raise_window", "bring_to_front")
    )
    if foreground_requested and not _foreground_escalation_allowed():
        return deny(
            "foreground delivery requires allow_foreground_escalation: true"
        )
    if any(
        key in call
        for key in (
            "coordinate",
            "from_coordinate",
            "to_coordinate",
            "x",
            "y",
            "to_x",
            "to_y",
        )
    ):
        return deny("raw coordinates are forbidden; use current elements or semantic refs")

    if action in _READ_ACTIONS or action == "cua_browser_prepare":
        return None
    if action in _CAPTURE_ACTIONS:
        with _LOCK:
            if _STATE.get(session) == "action_inflight":
                return deny("parallel capture/action batches are forbidden")
        return None
    if action not in _NATIVE_ACTIONS | _BROWSER_ACTIONS:
        return deny(f"unknown or unsupported computer_use action {action!r}")

    if action in {
        "click",
        "double_click",
        "right_click",
        "middle_click",
        "set_value",
    } and not isinstance(call.get("element"), int):
        return deny(f"{action} requires an element index from the latest capture")
    if action == "drag" and (
        not isinstance(call.get("from_element"), int)
        or not isinstance(call.get("to_element"), int)
    ):
        return deny("drag requires from_element and to_element from the latest capture")
    if action in _BROWSER_ACTIONS - {
        "cua_browser_prepare",
        "cua_browser_navigate",
    } and not any(
        call.get(key) for key in ("ref", "tab_id", "dialog_id", "continuation")
    ):
        return deny(f"{action} requires a current typed-browser capability or ref")

    with _LOCK:
        if _STATE.get(session) != "ready":
            return deny("a successful fresh capture/state is required before each UI action")
        _STATE[session] = "action_inflight"
    return None


def _on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    status: str = "",
    **kwargs: Any,
) -> None:
    if tool_name != "computer_use":
        return
    call = args if isinstance(args, dict) else {}
    action = str(call.get("action") or "")
    session = _session_key(kwargs)
    if not session:
        return
    with _LOCK:
        call_key = _call_key(session, kwargs)
        if call_key is not None and call_key in _BLOCKED_CALLS:
            _BLOCKED_CALLS.discard(call_key)
            return
        if action in _CAPTURE_ACTIONS:
            _STATE[session] = "ready" if status == "ok" else "capture_required"
        elif action in _NATIVE_ACTIONS | _BROWSER_ACTIONS:
            _STATE[session] = "capture_required"


def _on_session_end(**kwargs: Any) -> None:
    session = _session_key(kwargs)
    if not session:
        return
    with _LOCK:
        _STATE.pop(session, None)
        _BLOCKED_CALLS.difference_update(
            call_key for call_key in _BLOCKED_CALLS if call_key[0] == session
        )


def register(ctx) -> None:
    # The plugin is staged fleet-wide but registers no hot-path hooks until an
    # explicitly opted-in profile is restarted.  This keeps non-eligible and
    # headless agents behaviorally and computationally unchanged.
    if not _policy_enabled():
        return
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
