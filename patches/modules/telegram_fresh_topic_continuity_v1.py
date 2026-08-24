#!/usr/bin/env python3
"""Carry fresh Telegram conversation history from a trusted hook into the agent request."""

from __future__ import annotations

from pathlib import Path

OLD_MARKER = "HERMES_TELEGRAM_FRESH_TOPIC_CONTINUITY_v1"
MARKER = "HERMES_TELEGRAM_FRESH_TOPIC_CONTINUITY_v2"
NEW_SESSION_ANCHOR = """        # Emit session:start for new or auto-reset sessions
        _is_new_session = (
"""
HOOK_MESSAGE_ANCHOR = '                "message": message_text[:500],\n'
HOOK_EMIT_ANCHOR = '            await self.hooks.emit("agent:start", hook_ctx)\n'

RESET_CAPTURE = f"""        # [{MARKER}] Preserve the explicit-reset distinction before
        # the native one-shot flag is consumed below. Manual /new and /reset
        # intentionally stay clean; first-ever and automatic fresh sessions may
        # receive strictly same-conversation Telegram transcript history.
        _was_explicit_reset = bool(
            getattr(session_entry, "is_fresh_reset", False)
        )

"""

HOOK_FIELDS = """                "full_message": message_text,
                "fresh_telegram_rehydrate": bool(
                    _is_new_session
                    and not _was_explicit_reset
                    and source.platform == Platform.TELEGRAM
                    and (
                        getattr(source, "chat_type", "") == "dm"
                        or bool(getattr(source, "thread_id", None))
                    )
                ),
"""

CONSUME_OVERRIDE = f"""            # [{MARKER}] Only the explicit fresh-Telegram contract may
            # replace the model-visible message. Other hooks remain observational.
            _telegram_continuity_override = hook_ctx.get("model_message_override")
            if (
                hook_ctx.get("fresh_telegram_rehydrate") is True
                and hook_ctx.get("continuity_injected") is True
                and isinstance(_telegram_continuity_override, str)
                and _telegram_continuity_override.strip()
            ):
                message_text = _telegram_continuity_override
                logger.info(
                    "telegram conversation continuity injected: session=%s scope=telegram:%s:%s "
                    "rows=%s marker=%s",
                    session_entry.session_id,
                    source.chat_id,
                    getattr(source, "thread_id", None),
                    hook_ctx.get("continuity_history_rows", 0),
                    hook_ctx.get("continuity_marker", ""),
                )
"""

OLD_RESET_CAPTURE = f"""        # [{OLD_MARKER}] Preserve the explicit-reset distinction before
        # the native one-shot flag is consumed below. Manual /new and /reset
        # intentionally stay clean; first-ever and automatic fresh sessions may
        # receive strictly same-topic transcript history.
        _was_explicit_reset = bool(
            getattr(session_entry, "is_fresh_reset", False)
        )

"""

OLD_HOOK_FIELDS = """                "full_message": message_text,
                "fresh_topic_rehydrate": bool(
                    _is_new_session
                    and not _was_explicit_reset
                    and source.platform == Platform.TELEGRAM
                    and getattr(source, "thread_id", None)
                ),
"""

OLD_CONSUME_OVERRIDE = f"""            # [{OLD_MARKER}] Only the explicit fresh-topic contract may
            # replace the model-visible message. Other hooks remain observational.
            _topic_continuity_override = hook_ctx.get("model_message_override")
            if (
                hook_ctx.get("fresh_topic_rehydrate") is True
                and hook_ctx.get("continuity_injected") is True
                and isinstance(_topic_continuity_override, str)
                and _topic_continuity_override.strip()
            ):
                message_text = _topic_continuity_override
                logger.info(
                    "telegram topic continuity injected: session=%s topic=telegram:%s:%s "
                    "rows=%s marker=%s",
                    session_entry.session_id,
                    source.chat_id,
                    getattr(source, "thread_id", None),
                    hook_ctx.get("continuity_history_rows", 0),
                    hook_ctx.get("continuity_marker", ""),
                )
"""


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    if source.count(anchor) != 1:
        raise RuntimeError(f"{label} anchor drift: expected 1, found {source.count(anchor)}")
    return source.replace(anchor, replacement, 1)


def patch_run_text(source: str) -> str:
    if MARKER in source:
        return source
    if OLD_MARKER in source:
        source = _replace_once(source, OLD_RESET_CAPTURE, RESET_CAPTURE, "old reset capture")
        source = _replace_once(source, OLD_HOOK_FIELDS, HOOK_FIELDS, "old hook fields")
        return _replace_once(
            source,
            OLD_CONSUME_OVERRIDE,
            CONSUME_OVERRIDE,
            "old continuity override",
        )
    source = _replace_once(
        source,
        NEW_SESSION_ANCHOR,
        RESET_CAPTURE + NEW_SESSION_ANCHOR,
        "new-session",
    )
    source = _replace_once(
        source,
        HOOK_MESSAGE_ANCHOR,
        HOOK_MESSAGE_ANCHOR + HOOK_FIELDS,
        "agent-start hook context",
    )
    return _replace_once(
        source,
        HOOK_EMIT_ANCHOR,
        HOOK_EMIT_ANCHOR + CONSUME_OVERRIDE,
        "agent-start hook emit",
    )


def patch_telegram_fresh_topic_continuity_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "gateway" / "run.py"
    original = target.read_text(encoding="utf-8")
    patched = patch_run_text(original)
    if patched == original:
        return False
    target.write_text(patched, encoding="utf-8")
    return True
