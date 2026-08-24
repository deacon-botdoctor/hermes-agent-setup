from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STATE: dict[str, str] = {}
_OBSERVED_TARGETS: dict[str, dict[str, str]] = {}
_CAPTURE_CALLS: dict[str, str] = {}
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
_BROWSER_APP_PATTERN = re.compile(
    r"(?:^|\b)(?:arc|brave(?: browser)?|chrom(?:e|ium)|dia|duckduckgo|firefox|"
    r"google chrome|microsoft edge|opera|orion|safari|vivaldi|zen(?: browser)?)(?:\b|$)",
    re.IGNORECASE,
)
_EXECUTABLE_APP_PATTERN = re.compile(
    r"(?:^|\b)(?:alacritty|android studio|command prompt|cursor|eclipse|"
    r"intellij idea|iterm2?|kitty|konsole|powershell(?: ise)?|pycharm|rio|"
    r"script editor|terminal|visual studio(?: code)?|vs code|vscode|warp|"
    r"wezterm|windows terminal|windsurf|xcode|xterm)(?:\b|$)",
    re.IGNORECASE,
)
_NON_BROWSER_APP_PATTERN = re.compile(
    r"(?:^|\b)(?:calendar|finder|mail|messages|notes|preview|slack|textedit|zoom)"
    r"(?:\b|$)",
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
    return ((config.get("plugins") or {}).get("entries") or {}).get("semantic-computer-control-guard") or {}


def _device_posture(settings: dict[str, Any] | None = None) -> str:
    settings = settings if settings is not None else _plugin_settings()
    return str(settings.get("device_posture") or "standard").strip().lower()


def _principal_machine_control(settings: dict[str, Any] | None = None) -> bool:
    settings = settings if settings is not None else _plugin_settings()
    return (
        _device_posture(settings) == "dedicated_principal"
        and str(settings.get("control_scope") or "").strip().lower()
        == "principal_machine"
    )


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
    observed_agent = _canonical_identity(os.getenv("HERMES_AGENT_ID") or os.getenv("HERMES_PROFILE"))
    observed_device = _canonical_identity(socket.gethostname(), hostname=True)
    observed = {
        "principal_id": observed_principal,
        "agent_id": observed_agent,
        "device_id": observed_device,
    }
    mismatched = sorted(name for name in required if required[name] != observed[name])
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
    if settings.get("enforce") is True:
        return True
    # The former fleet-wide semantic_control_only flag turned a tool preference
    # into a hard deny on standard devices.  Keep enforcement only for an
    # explicit non-standard device posture (or the operator env opt-in above);
    # ordinary agents remain free to use the best available fallback. Unknown
    # non-standard postures still enter the guard and fail closed below.
    return _device_posture(settings) != "standard"


def _foreground_escalation_allowed() -> bool:
    return bool(_plugin_settings().get("allow_foreground_escalation"))


def _session_platform(kwargs: dict[str, Any]) -> str:
    explicit = kwargs.get("source_platform") or kwargs.get("platform")
    if explicit:
        return str(explicit).strip().casefold()
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().casefold()
    except Exception:
        return str(os.getenv("HERMES_SESSION_PLATFORM", "")).strip().casefold()


def _call_app(call: dict[str, Any]) -> str:
    for key in ("app", "app_name", "application"):
        if call.get(key):
            return str(call[key]).strip().casefold()
    return ""


def _call_urls(value: Any, key: str = "") -> list[str]:
    url_keys = {
        "url",
        "uri",
        "href",
        "origin",
        "target_url",
        "page_url",
        "current_url",
        "document_url",
    }
    if isinstance(value, dict):
        urls: list[str] = []
        for nested_key, nested_value in value.items():
            urls.extend(_call_urls(nested_value, str(nested_key).casefold()))
        return urls
    if isinstance(value, list):
        return [url for item in value for url in _call_urls(item, key)]
    if key in url_keys and isinstance(value, str):
        return [value.strip()]
    return []


def _is_telegram_url(value: str) -> bool:
    direct = urlparse(value)
    if direct.scheme.casefold() == "tg":
        return True
    candidate = value if "://" in value else f"https://{value}"
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return (
        hostname == "t.me"
        or hostname.endswith(".t.me")
        or (hostname == "telegram.org" or hostname.endswith(".telegram.org"))
    )


def _result_payload(result: Any) -> dict[str, Any]:
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _result_text(payload: dict[str, Any]) -> str:
    summaries = [payload.get("summary"), payload.get("text_summary")]
    content = payload.get("content")
    if isinstance(content, list):
        summaries.extend(item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text")
    return "\n".join(str(value) for value in summaries if value)


def _observed_native_target(result: Any, call: dict[str, Any] | None = None) -> str:
    call = call or {}
    if call.get("pid") is not None or call.get("window_id") is not None:
        return "unknown"
    payload = _result_payload(result)
    app = str(payload.get("app") or "").strip().casefold()
    if not app:
        match = re.search(
            r"\bcapture\s+mode=.*?\sapp=(.*?)(?:\swindow=|\n|$)",
            _result_text(payload),
            re.IGNORECASE,
        )
        app = (match.group(1) if match else "").strip().casefold()
    if not app:
        return "unknown"
    identities = [
        app,
        *(
            str(payload.get(key) or "").strip().casefold()
            for key in (
                "bundle_id",
                "bundle_identifier",
                "process_name",
                "application_id",
            )
        ),
    ]
    identity = " ".join(value for value in identities if value)
    if "telegram" in identity:
        return "telegram"
    browser_kind = payload.get("is_browser")
    app_kind = str(payload.get("app_type") or payload.get("application_type") or "").strip().casefold()
    if browser_kind is True or app_kind in {"browser", "web_browser"}:
        return "browser"
    if _BROWSER_APP_PATTERN.search(identity):
        return "browser"
    if _EXECUTABLE_APP_PATTERN.search(identity):
        return "executable"
    if browser_kind is False or app_kind in {"desktop", "native", "native_app"}:
        return "other"
    return "other" if _NON_BROWSER_APP_PATTERN.search(identity) else "unknown"


def _observed_browser_target(result: Any, call: dict[str, Any]) -> str:
    payload = _result_payload(result)
    url_keys = (
        "url",
        "current_url",
        "page_url",
        "document_url",
        "origin",
        "target_url",
    )
    urls = [
        str(payload[key]).strip() for key in url_keys if isinstance(payload.get(key), str) and str(payload[key]).strip()
    ]
    for container_key in ("page", "document", "tab", "snapshot"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            urls.extend(
                str(container[key]).strip()
                for key in url_keys
                if isinstance(container.get(key), str) and str(container[key]).strip()
            )
    tabs = payload.get("tabs")
    if isinstance(tabs, list):
        selected_tab = str(call.get("tab_id") or payload.get("tab_id") or "")
        candidates = [tab for tab in tabs if isinstance(tab, dict)]
        if selected_tab:
            candidates = [tab for tab in candidates if str(tab.get("tab_id") or tab.get("id") or "") == selected_tab]
        elif len(candidates) != 1:
            candidates = []
        for tab in candidates:
            urls.extend(
                str(tab[key]).strip() for key in url_keys if isinstance(tab.get(key), str) and str(tab[key]).strip()
            )
    if not urls:
        return "unknown"
    return "telegram" if any(_is_telegram_url(url) for url in urls) else "other"


def _source_surface_reentry_reason(
    session: str, action: str, call: dict[str, Any], kwargs: dict[str, Any]
) -> str | None:
    if _session_platform(kwargs) != "telegram":
        return None
    if action in _NATIVE_ACTIONS:
        app = _call_app(call)
        observed = _OBSERVED_TARGETS.get(session, {}).get("native", "unknown")
        if "telegram" in app or observed == "telegram":
            return (
                "a Telegram-originated turn cannot operate Telegram through "
                "desktop input; use telegram_topic_post instead"
            )
        if observed == "browser":
            return (
                "Telegram-originated native input cannot trust a browser app "
                "capture; use URL-bound cua_browser_state before browser mutation"
            )
        if _EXECUTABLE_APP_PATTERN.search(app) or observed == "executable":
            return (
                "Telegram-originated native input cannot mutate an executable or "
                "REPL app; use the guarded terminal tool for command work"
            )
        if observed != "other":
            return (
                "Telegram-originated native input requires a fresh capture that "
                "authoritatively identifies a non-Telegram target app; use "
                "telegram_topic_post for authorized topic updates"
            )
    if action in _BROWSER_ACTIONS:
        if any(_is_telegram_url(url) for url in _call_urls(call)):
            return (
                "a Telegram-originated turn cannot operate Telegram Web through "
                "browser input; use telegram_topic_post instead"
            )
        observed = _OBSERVED_TARGETS.get(session, {}).get("browser", "unknown")
        if observed == "telegram":
            return (
                "a Telegram-originated turn cannot operate the observed Telegram "
                "Web target; use telegram_topic_post instead"
            )
        if observed != "other":
            return (
                "Telegram-originated browser input requires fresh state that "
                "authoritatively identifies a non-Telegram URL"
            )
    return None


def _block(message: str) -> dict[str, str]:
    return {
        "action": "block",
        "message": (
            f"Semantic computer-control policy blocked this call: {message}. "
            "Use computer_use with fresh semantic state for other desktop work. "
            "Background delivery is preferred; foreground requires the profile's "
            "explicit escalation opt-in. No direct UI fallback is permitted."
        ),
    }


def _serialized(args: Any) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(args)


def _result_has_fresh_capture(result: Any) -> bool:
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("mode") in {"ax", "som", "vision"}:
        return True
    if parsed.get("_multimodal") is not True:
        return False
    content = parsed.get("content")
    return bool(
        isinstance(content, list)
        and any(isinstance(item, dict) and item.get("type") == "image_url" for item in content)
    )


def _direct_ui_block(tool_name: str, args: Any) -> dict[str, str] | None:
    if tool_name not in _DIRECT_UI_TOOL_NAMES:
        return None
    payload = _serialized(args)
    for pattern in _DIRECT_UI_PATTERNS:
        if pattern.search(payload):
            logger.warning(
                "semantic-computer-control: blocked direct UI primitive tool=%s pattern=%s",
                tool_name,
                pattern.pattern,
            )
            return _block("direct desktop scripting or input injection is forbidden")
    return None


def _is_direct_ui_call(tool_name: str, args: Any) -> bool:
    if tool_name != "computer_use" and _DIRECT_UI_TOOL_NAME_PATTERN.search(str(tool_name)):
        return True
    if tool_name not in _DIRECT_UI_TOOL_NAMES:
        return False
    payload = _serialized(args)
    return any(pattern.search(payload) for pattern in _DIRECT_UI_PATTERNS)


def _session_key(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or kwargs.get("turn_id") or "")


def _call_key(session: str, kwargs: dict[str, Any]) -> tuple[str, str] | None:
    tool_call_id = str(kwargs.get("tool_call_id") or "")
    return (session, tool_call_id) if tool_call_id else None


def _block_computer_call(session: str, kwargs: dict[str, Any], message: str) -> dict[str, str]:
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
    config = _runtime_config()
    settings = _plugin_settings(config)
    posture = _device_posture(settings)
    binding_error = _dedicated_binding_error(config, settings)

    # An explicitly opted-in principal machine is an outcome-scoped control
    # surface, not a semantic-control sandbox. Once the exact principal,
    # agent, and device binding is proven, the agent may use any installed
    # local control lane needed for the requested outcome. Consequential-action
    # authorization remains owned by the runtime's normal policy layer.
    if _principal_machine_control(settings):
        if binding_error is not None:
            if tool_name == "computer_use" or _is_direct_ui_call(str(tool_name), args):
                return _block(binding_error)
            return None
        if tool_name != "computer_use":
            return None
        try:
            _pin_permission_mode("unrestricted")
        except Exception as exc:
            logger.error("semantic-computer-control: permission-mode pin failed: %s", exc)
            return _block("the configured permission-mode pin is unavailable")
        call = args if isinstance(args, dict) else {}
        requested_mode = call.get("permission_mode")
        if requested_mode is not None and requested_mode != "unrestricted":
            return _block("permission_mode is bound to 'unrestricted' by device posture")
        return None

    if tool_name != "computer_use" and _DIRECT_UI_TOOL_NAME_PATTERN.search(str(tool_name)):
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

    if binding_error is not None:
        return deny(binding_error)
    permission_mode = "unrestricted" if posture == "dedicated_principal" else "standard"
    try:
        _pin_permission_mode(permission_mode)
    except Exception as exc:
        logger.error("semantic-computer-control: permission-mode pin failed: %s", exc)
        return deny("the configured permission-mode pin is unavailable")

    requested_mode = call.get("permission_mode")
    if requested_mode is not None and requested_mode != permission_mode:
        return deny(f"permission_mode is bound to {permission_mode!r} by device posture")
    foreground_requested = call.get("delivery_mode") == "foreground" or any(
        call.get(key) is True for key in ("raise_window", "bring_to_front")
    )
    if foreground_requested and not _foreground_escalation_allowed():
        return deny("foreground delivery requires allow_foreground_escalation: true")
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
            call_key = _call_key(session, kwargs)
            if call_key is None:
                return deny("capture requires a unique tool_call_id")
            if _STATE.get(session) in {"action_inflight", "capture_inflight"}:
                return deny("parallel capture/action batches are forbidden")
            _STATE[session] = "capture_inflight"
            _CAPTURE_CALLS[session] = call_key[1]
            target_key = "native" if action == "capture" else "browser"
            _OBSERVED_TARGETS.get(session, {}).pop(target_key, None)
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
        not isinstance(call.get("from_element"), int) or not isinstance(call.get("to_element"), int)
    ):
        return deny("drag requires from_element and to_element from the latest capture")
    if action in _BROWSER_ACTIONS - {
        "cua_browser_prepare",
        "cua_browser_navigate",
    } and not any(call.get(key) for key in ("ref", "tab_id", "dialog_id", "continuation")):
        return deny(f"{action} requires a current typed-browser capability or ref")

    with _LOCK:
        source_surface_reentry = _source_surface_reentry_reason(session, action, call, kwargs)
        if source_surface_reentry is not None:
            return deny(source_surface_reentry)
        required_state = "native_ready" if action in _NATIVE_ACTIONS else "browser_ready"
        if _STATE.get(session) != required_state:
            return deny("a successful fresh capture/state is required before each UI action")
        _STATE[session] = "action_inflight"
    return None


def _on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
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
            expected_call_id = _CAPTURE_CALLS.get(session)
            observed_call_id = str(kwargs.get("tool_call_id") or "")
            if not expected_call_id or observed_call_id != expected_call_id:
                return
            _CAPTURE_CALLS.pop(session, None)
            if status == "ok":
                targets = _OBSERVED_TARGETS.setdefault(session, {})
                if action == "capture":
                    targets["native"] = _observed_native_target(result, call)
                    _STATE[session] = "native_ready"
                else:
                    targets["browser"] = _observed_browser_target(result, call)
                    _STATE[session] = "browser_ready"
            else:
                _STATE[session] = "capture_required"
                target_key = "native" if action == "capture" else "browser"
                _OBSERVED_TARGETS.get(session, {}).pop(target_key, None)
        elif action in _NATIVE_ACTIONS | _BROWSER_ACTIONS:
            capture_after_ready = bool(
                status == "ok" and call.get("capture_after") is True and _result_has_fresh_capture(result)
            )
            if capture_after_ready:
                targets = _OBSERVED_TARGETS.setdefault(session, {})
                if action in _NATIVE_ACTIONS:
                    targets["native"] = _observed_native_target(result, call)
                else:
                    targets["browser"] = _observed_browser_target(result, call)
            _STATE[session] = (
                ("native_ready" if action in _NATIVE_ACTIONS else "browser_ready")
                if capture_after_ready
                else "capture_required"
            )


def _on_session_end(**kwargs: Any) -> None:
    session = _session_key(kwargs)
    if not session:
        return
    with _LOCK:
        _STATE.pop(session, None)
        _OBSERVED_TARGETS.pop(session, None)
        _CAPTURE_CALLS.pop(session, None)
        _BLOCKED_CALLS.difference_update(call_key for call_key in _BLOCKED_CALLS if call_key[0] == session)


def register(ctx) -> None:
    # The plugin is staged fleet-wide but registers no hot-path hooks until an
    # explicitly opted-in profile is restarted.  This keeps non-eligible and
    # headless agents behaviorally and computationally unchanged.
    if not _policy_enabled():
        return
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
