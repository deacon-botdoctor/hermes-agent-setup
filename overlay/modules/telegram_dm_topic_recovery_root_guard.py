#!/usr/bin/env python3
"""Guard Telegram DM topic recovery from hijacking explicit root messages."""
from __future__ import annotations

import ast
import shutil
import time
from pathlib import Path

MARKER = "HERMES_TELEGRAM_DM_TOPIC_RECOVERY_ROOT_GUARD_v1"
TARGET = "gateway/platforms/base.py"


def apply(target_path: Path, *, dry_run: bool = False) -> str:
    src = target_path.read_text(encoding="utf-8")
    if MARKER in src:
        return "already"

    start = src.find("    def _apply_topic_recovery(self, event: MessageEvent) -> None:\n")
    if start < 0:
        return "anchor-miss"
    end = src.find("    def set_busy_session_handler(", start)
    if end < 0:
        return "anchor-miss"

    old_method = src[start:end]
    if "recovered = recover(source)" not in old_method:
        return "anchor-miss"
    if "reply_to_message_id" in old_method and "plain root/lobby message" in old_method:
        return "already"

    new_method = '''    def _apply_topic_recovery(self, event: MessageEvent) -> None:
        """Rewrite event.source.thread_id in place if the hook returns one.

        HERMES_TELEGRAM_DM_TOPIC_RECOVERY_ROOT_GUARD_v1: Telegram DM topic
        recovery is only a fallback for stripped replies. A plain root/lobby
        message is an explicit user action in the main chat, not permission to
        route the turn into the globally most-recent topic.
        """
        recover = getattr(self, "_topic_recovery_fn", None)
        if recover is None:
            return
        source = getattr(event, "source", None)
        if source is None:
            return
        if (
            _platform_name(getattr(source, "platform", None)) == "telegram"
            and getattr(source, "chat_type", None) == "dm"
            and str(getattr(source, "thread_id", None) or "") in {"", "1"}
            and not getattr(event, "reply_to_message_id", None)
        ):
            return
        try:
            recovered = recover(source)
        except Exception:
            logger.debug("topic recovery hook failed", exc_info=True)
            return
        if recovered is None or str(recovered) == str(source.thread_id or ""):
            return
        try:
            event.source = dataclasses.replace(source, thread_id=str(recovered))
        except Exception:
            logger.debug("topic recovery rewrite failed", exc_info=True)

'''
    patched = src[:start] + new_method + src[end:]
    ast.parse(patched)

    if dry_run:
        return "applied"

    backup = target_path.with_suffix(target_path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target_path, backup)
    target_path.write_text(patched, encoding="utf-8")
    return "applied"
