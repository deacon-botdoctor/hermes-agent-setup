"""Narrow bot-identity posting for explicitly authorized Telegram topics."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

TOOLSET = "telegram-topic-post"
PLUGIN_ID = "telegram-topic-post"
logger = logging.getLogger(__name__)
_GATEWAY_SEND_TIMEOUT_MARGIN_SECONDS = 5.0
_MAX_GATEWAY_SEND_TIMEOUT_SECONDS = max(1.0, threading.TIMEOUT_MAX - 1.0)
_TELEGRAM_HTTP_TIMEOUTS = {
    "pool": ("HERMES_TELEGRAM_HTTP_POOL_TIMEOUT", 8.0),
    "connect": ("HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT", 10.0),
    "read": ("HERMES_TELEGRAM_HTTP_READ_TIMEOUT", 60.0),
    "write": ("HERMES_TELEGRAM_HTTP_WRITE_TIMEOUT", 20.0),
}

TELEGRAM_TOPIC_POST_SCHEMA = {
    "name": "telegram_topic_post",
    "description": (
        "Post an explicitly requested update into another known Telegram topic "
        "through this agent's bot identity. Use this instead of computer_use or "
        "Telegram Web. The destination is restricted by operator configuration, "
        "and a delivery receipt is returned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "Known destination Telegram topic/thread ID.",
            },
            "message": {
                "type": "string",
                "description": "Exact user-facing update to post as the agent bot.",
            },
            "chat_id": {
                "type": "string",
                "description": ("Destination chat ID. Omit when exactly one destination chat is configured."),
            },
        },
        "required": ["thread_id", "message"],
        "additionalProperties": False,
    },
}

def _runtime_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def _settings() -> dict[str, Any]:
    return ((_runtime_config().get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}


def _session_value(name: str, kwargs: dict[str, Any]) -> str:
    explicit_keys = {
        "HERMES_SESSION_PLATFORM": ("source_platform", "platform"),
        "HERMES_SESSION_USER_ID": ("source_user_id", "user_id"),
    }
    for key in explicit_keys.get(name, ()):
        if kwargs.get(key) not in (None, ""):
            return str(kwargs[key]).strip()
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "").strip()
    except Exception:
        return str(os.getenv(name, "") or "").strip()


def _configured_ids(settings: dict[str, Any], key: str) -> list[str]:
    values = settings.get(key) or []
    if isinstance(values, (str, int)):
        values = [values]
    return [str(value).strip() for value in values if str(value).strip()]


def _transcript_db_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return Path(home) / "data" / "telegram-transcript.db"


def _known_topic(chat_id: str, thread_id: str) -> bool:
    transcript_db = _transcript_db_path()
    if not transcript_db.exists():
        return False
    try:
        with sqlite3.connect(f"file:{transcript_db}?mode=ro", uri=True) as db:
            target_ids = (thread_id, chat_id, f"telegram:{chat_id}:{thread_id}")
            for table in ("telegram_topics", "telegram_messages"):
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                known = db.execute(
                    f"SELECT 1 FROM {table} WHERE thread_id = ? AND (chat_id = ? OR chat_id = ?) LIMIT 1",
                    target_ids,
                ).fetchone()
                if known:
                    return True
            return False
    except (OSError, sqlite3.Error):
        return False


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _finite_timeout(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else default


def _telegram_http_timeout(adapter: Any, field: str) -> float:
    env_name, default = _TELEGRAM_HTTP_TIMEOUTS[field]
    config = getattr(adapter, "config", None)
    extra = getattr(config, "extra", None) or {}
    if field == "read":
        default = _finite_timeout(extra.get("http_read_timeout", default), default)
    bot = getattr(adapter, "_bot", None)
    request = getattr(bot, "request", None)
    client = getattr(request, "_client", None)
    timeout = getattr(client, "timeout", None)
    effective = getattr(timeout, field, None)
    if effective is None and field == "read":
        effective = getattr(request, "read_timeout", None)
    if effective is not None:
        return _finite_timeout(effective, default)
    return _finite_timeout(os.environ.get(env_name, default), default)


def _telegram_connect_attempts(adapter: Any) -> int:
    bot = getattr(adapter, "_bot", None)
    request = getattr(bot, "request", None)
    client = getattr(request, "_client", None)
    transport = getattr(client, "_transport", None)
    if transport is not None:
        fallback_ips = getattr(transport, "_fallback_ips", None)
        return len(fallback_ips) + 1 if fallback_ips else 1
    try:
        fallback_ips = adapter._fallback_ips()
    except Exception:
        fallback_ips = []
    disable_fallback = str(os.environ.get("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "")).casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return 1 if disable_fallback else max(3, len(fallback_ips) + 1)


def _telegram_send_timeout_seconds(adapter: Any) -> float:
    connect_attempts = _telegram_connect_attempts(adapter)
    timeout = (
        connect_attempts * (_telegram_http_timeout(adapter, "pool") + _telegram_http_timeout(adapter, "connect"))
        + _telegram_http_timeout(adapter, "write")
        + _telegram_http_timeout(adapter, "read")
        + _GATEWAY_SEND_TIMEOUT_MARGIN_SECONDS
    )
    return min(timeout, _MAX_GATEWAY_SEND_TIMEOUT_SECONDS)


def _run_on_gateway_loop(runner: Any, coroutine: Any, timeout_seconds: float) -> Any:
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or loop.is_closed() or not loop.is_running():
        coroutine.close()
        raise RuntimeError("Telegram gateway loop is unavailable")
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        coroutine.close()
        raise RuntimeError("Telegram topic send cannot block the gateway loop")
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    except Exception:
        coroutine.close()
        raise
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError:
        future.cancel()
        raise


def _strict_topic_send(chat_id: str, thread_id: str, message: str) -> dict[str, Any]:
    try:
        from gateway.config import Platform, load_gateway_config
        from gateway.run import _gateway_runner_ref
        from plugins.platforms.telegram.telegram_ids import (
            normalize_telegram_chat_id,
        )

        config = load_gateway_config()
        platform = config.platforms.get(Platform.TELEGRAM)
        token = str(getattr(platform, "token", "") or "").strip()
        if not platform or not getattr(platform, "enabled", False) or not token:
            return {"success": False, "error": "Telegram bot identity is unavailable"}

        runner = _gateway_runner_ref()
        if runner is None:
            return {"success": False, "error": "Telegram bot identity is unavailable"}
        try:
            from gateway.session_context import get_session_env

            profile = str(get_session_env("HERMES_SESSION_PROFILE", "") or "").strip()
        except Exception:
            profile = ""
        adapter_maps = []
        if profile:
            profile_map = getattr(runner, "_profile_adapters", {}).get(profile)
            if isinstance(profile_map, dict):
                adapter_maps.append(profile_map)
        adapter_maps.append(getattr(runner, "adapters", {}))
        adapter_maps.extend(
            value
            for value in getattr(runner, "_profile_adapters", {}).values()
            if isinstance(value, dict) and value not in adapter_maps
        )
        adapter = next(
            (
                candidate
                for adapter_map in adapter_maps
                for candidate in [adapter_map.get(Platform.TELEGRAM)]
                if candidate is not None
                and str(getattr(getattr(candidate, "config", None), "token", "") or "").strip() == token
            ),
            None,
        )
        bot = getattr(adapter, "_bot", None) if adapter is not None else None
        if bot is None:
            return {"success": False, "error": "Telegram bot identity is unavailable"}

        effective_thread = None if thread_id == "1" else int(thread_id)

        async def send():
            kwargs: dict[str, Any] = {
                "chat_id": normalize_telegram_chat_id(chat_id),
                "text": message,
            }
            if effective_thread is not None:
                kwargs["message_thread_id"] = effective_thread
            delivered = await bot.send_message(**kwargs)
            return delivered, time.time()

        delivered, accepted_at = _run_on_gateway_loop(
            runner,
            send(),
            _telegram_send_timeout_seconds(adapter),
        )
        delivered_chat = str(getattr(getattr(delivered, "chat", None), "id", ""))
        transport_thread = getattr(delivered, "message_thread_id", None)
        delivered_thread = "1" if thread_id == "1" and transport_thread is None else str(transport_thread or "")
        if delivered_chat != chat_id or delivered_thread != thread_id:
            return {
                "success": False,
                "error": ("Telegram delivery receipt did not confirm the requested topic"),
            }
        return {
            "success": True,
            "chat_id": delivered_chat,
            "thread_id": delivered_thread,
            "transport_thread_id": (str(transport_thread) if transport_thread is not None else None),
            "message_id": str(getattr(delivered, "message_id", "")),
            "accepted_at": accepted_at,
        }
    except Exception as exc:
        logger.warning("Telegram topic delivery failed (%s)", type(exc).__name__)
        return {
            "success": False,
            "code": "telegram_transport_error",
            "error": "Telegram topic delivery failed",
        }


def _mirror_topic_post(chat_id: str, thread_id: str, message: str) -> bool:
    try:
        from gateway.mirror import mirror_to_session
        from gateway.session_context import get_session_env

        return bool(
            mirror_to_session(
                "telegram",
                chat_id,
                message,
                source_label=get_session_env("HERMES_SESSION_PLATFORM", "telegram"),
                thread_id=thread_id,
                user_id=get_session_env("HERMES_SESSION_USER_ID", "") or None,
            )
        )
    except Exception:
        return False


def _record_delivery_receipt(
    chat_id: str,
    thread_id: str,
    message_id: str,
    accepted_at: float,
) -> bool:
    try:
        from gateway.telegram_transaction_ledger import external_accepted

        for delay in (0.0, 0.05, 0.1):
            if delay:
                time.sleep(delay)
            if external_accepted(
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                source=PLUGIN_ID,
                occurred_at=accepted_at,
            ):
                return True
        return False
    except Exception:
        logger.exception("Telegram topic delivery receipt persistence failed")
        return False


def telegram_topic_post_handler(*, thread_id: Any, message: Any, chat_id: Any = "", **kwargs: Any) -> str:
    if _session_value("HERMES_SESSION_PLATFORM", kwargs).casefold() != "telegram":
        return _error("telegram_topic_post is only available from Telegram turns")

    settings = _settings()
    allowed_users = _configured_ids(settings, "allowed_user_ids")
    source_user = _session_value("HERMES_SESSION_USER_ID", kwargs)
    if not allowed_users or source_user not in allowed_users:
        return _error("the authenticated Telegram user is not authorized to post")

    allowed_chats = _configured_ids(settings, "allowed_target_chat_ids")
    if not allowed_chats:
        return _error("no destination Telegram chats are configured")
    destination_chat = str(chat_id or "").strip()
    if not destination_chat and len(allowed_chats) == 1:
        destination_chat = allowed_chats[0]
    if destination_chat not in allowed_chats:
        return _error("the destination Telegram chat is not authorized")

    destination_thread = str(thread_id or "").strip()
    body = str(message or "").strip()
    if not destination_thread.isdigit() or destination_thread == "0":
        return _error("thread_id must be a positive numeric Telegram topic ID")
    if not body:
        return _error("message must not be empty")
    if len(body.encode("utf-16-le")) // 2 > 4096:
        return _error("message exceeds Telegram's 4,096-unit message limit")
    if not _known_topic(destination_chat, destination_thread):
        return _error("destination topic is not present in the local topic ledger")

    receipt = _strict_topic_send(destination_chat, destination_thread, body)
    if not receipt.get("success"):
        return json.dumps(receipt)
    delivered_chat = str(receipt.get("chat_id") or "")
    delivered_thread = str(receipt.get("thread_id") or "")
    if delivered_chat != destination_chat or delivered_thread != destination_thread:
        return _error("Telegram delivery receipt did not confirm the requested topic")
    delivered_message_id = str(receipt.get("message_id") or "")
    try:
        accepted_at = float(receipt.get("accepted_at"))
    except (TypeError, ValueError):
        return _error("Telegram delivery receipt did not include provider acceptance time")
    if not math.isfinite(accepted_at) or accepted_at <= 0:
        return _error("Telegram delivery receipt did not include provider acceptance time")
    mirrored = _mirror_topic_post(delivered_chat, delivered_thread, body)
    if not _record_delivery_receipt(
        delivered_chat,
        delivered_thread,
        delivered_message_id,
        accepted_at,
    ):
        return json.dumps(
            {
                "success": False,
                "code": "delivery_receipt_persistence_failed",
                "error": "Telegram delivered the message but durable receipt persistence failed",
                "delivered": True,
                "identity": "configured_telegram_bot",
                "chat_id": delivered_chat,
                "thread_id": delivered_thread,
                "transport_thread_id": receipt.get("transport_thread_id"),
                "message_id": delivered_message_id,
                "mirrored": mirrored,
                "receipt_retry_attempted": True,
                "instruction": (
                    "Do not resend this message. Block task closeout because durable receipt "
                    "persistence failed after Telegram accepted delivery."
                ),
            }
        )
    return json.dumps(
        {
            "success": True,
            "identity": "configured_telegram_bot",
            "chat_id": delivered_chat,
            "thread_id": delivered_thread,
            "transport_thread_id": receipt.get("transport_thread_id"),
            "message_id": delivered_message_id,
            "mirrored": mirrored,
        }
    )


def register(ctx) -> None:
    settings = _settings()
    if not _configured_ids(settings, "allowed_user_ids") or not _configured_ids(settings, "allowed_target_chat_ids"):
        return
    ctx.register_tool(
        name="telegram_topic_post",
        toolset=TOOLSET,
        schema=TELEGRAM_TOPIC_POST_SCHEMA,
        handler=lambda args, **kwargs: telegram_topic_post_handler(
            thread_id=args.get("thread_id"),
            message=args.get("message"),
            chat_id=args.get("chat_id", ""),
            **kwargs,
        ),
        description="Post an authorized update as the configured Telegram bot",
        emoji="📨",
    )
