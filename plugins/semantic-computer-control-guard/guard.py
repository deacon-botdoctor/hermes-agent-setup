from __future__ import annotations

import json
import logging
import os
import re
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


def _policy_enabled() -> bool:
    if os.getenv("HERMES_SEMANTIC_COMPUTER_CONTROL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        return bool(
            (
                (
                    ((config.get("plugins") or {}).get("entries") or {}).get(
                        "semantic-computer-control-guard"
                    )
                    or {}
                ).get("semantic_control_only")
            )
        )
    except Exception:
        return False


def _block(message: str) -> dict[str, str]:
    return {
        "action": "block",
        "message": (
            f"Semantic computer-control policy blocked this call: {message}. "
            "Use computer_use in background mode. If it is unavailable, stop "
            "and report or repair that route; no direct UI fallback is permitted."
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


def _pin_standard_permission_mode() -> None:
    from tools.computer_use import tool as computer_use_tool

    if not hasattr(computer_use_tool, "_cua_permission_mode"):
        raise RuntimeError(
            "Hermes computer_use permission resolver is unavailable; refusing "
            "semantic control without a standard-mode pin"
        )

    def _standard_mode(_session_id: str) -> str:
        return "standard"

    computer_use_tool._cua_permission_mode = _standard_mode


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

    try:
        _pin_standard_permission_mode()
    except Exception as exc:
        logger.error(
            "semantic-computer-control: standard-mode pin failed: %s", exc
        )
        return deny("the standard-mode safety pin is unavailable")

    if call.get("permission_mode") == "unrestricted":
        return deny("unrestricted desktop-control mode is forbidden")
    if call.get("delivery_mode") == "foreground":
        return deny("foreground delivery is forbidden")
    if call.get("raise_window") is True or call.get("bring_to_front") is True:
        return deny("raising or bringing a window to the foreground is forbidden")
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
