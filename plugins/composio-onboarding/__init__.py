"""Composio onboarding plugin."""

from __future__ import annotations

import contextvars
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_COMMAND_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "composio_command_context",
    default={},
)

_AUTH_FAILURE_MARKERS = (
    "invalid_grant",
    "expired state",
    "expired or revoked",
    "oauth refresh token expired",
    "access revoked",
    "reauthentication required",
)
_SERVICE_NAMES = (
    (("googlecalendar", "google_calendar"), ("googlecalendar", "Google Calendar")),
    (("googledrive", "google_drive"), ("googledrive", "Google Drive")),
    (("googledocs", "google_docs"), ("googledocs", "Google Docs")),
    (("googlesheets", "google_sheets"), ("googlesheets", "Google Sheets")),
    (("googletasks", "google_tasks"), ("googletasks", "Google Tasks")),
    (("zohomail", "zoho_mail", "zoho-mail"), ("zohomail", "Zoho Mail")),
    (("sharepoint", "share_point", "share-point"), ("share_point", "SharePoint")),
    (("linkedin",), ("linkedin", "LinkedIn")),
    (("gmail",), ("gmail", "Gmail")),
)


def _platform_name(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _composio_service(tool_name: str) -> tuple[str, str] | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(tool_name or "").lower())
    if "composio" not in normalized:
        return None
    compact = normalized.replace("_", "")
    for aliases, service in _SERVICE_NAMES:
        if any(alias.replace("_", "").replace("-", "") in compact for alias in aliases):
            return service
    return "connected_app", "connected app"


def _auth_failure_detail(result: str) -> str:
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if "error" not in parsed and parsed.get("isError") is not True and parsed.get("is_error") is not True:
        return ""
    detail = json.dumps(parsed, sort_keys=True).lower()
    return detail if any(marker in detail for marker in _AUTH_FAILURE_MARKERS) else ""


def _transform_composio_auth_failure(
    tool_name: str = "",
    result: str = "",
    **_: Any,
) -> str | None:
    service = _composio_service(tool_name)
    if service is None or not _auth_failure_detail(result):
        return None
    service_slug, service_label = service
    return json.dumps(
        {
            "error": f"{service_label} needs to be reconnected before this action can continue.",
            "incident_key": f"composio_auth:{service_slug}",
            "requires_reauthorization": True,
            "retry": "after_reauthorization_only",
            "agent_action": (
                f"Do not retry this tool. Tell the user once, in plain language, that {service_label} "
                "needs to be reconnected using the existing connection process. Do not mention "
                "Composio, MCP, account IDs, tokens, or raw provider errors. Continue any work that "
                f"does not require {service_label}."
            ),
        },
        sort_keys=True,
    )


def _capture_gateway_context(*, event=None, **_):
    source = getattr(event, "source", None)
    _COMMAND_CONTEXT.set(
        {
            "platform": _platform_name(getattr(source, "platform", "")),
            "user_id": str(getattr(source, "user_id", "") or ""),
            "chat_id": str(getattr(source, "chat_id", "") or ""),
        }
    )
    return {"action": "allow"}


def _command_handler(handler):
    def wrapped(raw_args: str):
        context = _COMMAND_CONTEXT.get()
        _COMMAND_CONTEXT.set({})
        try:
            return handler(raw_args, context)
        except Exception as exc:
            if type(exc).__name__ == "OnboardingError":
                return f"Blocked: {exc}"
            logger.exception("Composio onboarding command failed safely")
            return "Composio onboarding failed safely. Ask the operator to inspect the runtime error."

    return wrapped


def register(ctx):
    from .command import handle_connect_command, handle_connections_command, handle_disconnect_command
    from .redaction import install_composio_key_redaction

    install_composio_key_redaction()

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_gateway_dispatch", _capture_gateway_context)
        register_hook("transform_tool_result", _transform_composio_auth_failure)

    register_command = getattr(ctx, "register_command", None)
    if not callable(register_command):
        logger.info("Composio onboarding command registration unavailable")
        return

    commands = (
        (
            "connect",
            _command_handler(handle_connect_command),
            "Connect an app through this client's Composio project",
        ),
        (
            "connections",
            _command_handler(handle_connections_command),
            "List verified app connections and their canonical provider",
        ),
        (
            "disconnect",
            _command_handler(handle_disconnect_command),
            "Stop using a verified Composio connection as the canonical route",
        ),
    )
    for name, handler, description in commands:
        try:
            register_command(name, handler, description=description, args_hint="[service] [alias] [account]")
        except TypeError:
            register_command(name, handler, description=description)
