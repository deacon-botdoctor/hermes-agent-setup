"""Composio onboarding plugin."""

from __future__ import annotations

import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)

_COMMAND_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "composio_command_context",
    default={},
)


def _platform_name(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


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
