#!/usr/bin/env python3
# ruff: noqa: E501
"""Guard Telegram DM topic recovery from hijacking explicit root messages.

Hermes DM-topic recovery intentionally maps lobby-shaped Telegram replies
(thread_id absent or General topic 1) back to the user's latest bound managed
topic. Telegram root/lobby prompts can have the same shape, so recovery must
only run for stripped replies with reply_to_message_id.

Idempotent via marker: HERMES_TELEGRAM_DM_TOPIC_RECOVERY_ROOT_GUARD_v1
Target: gateway/platforms/base.py, tests/gateway/test_base_topic_sessions.py,
        tests/gateway/test_telegram_text_batching.py,
        tests/gateway/test_drain_inbox.py

Usage:
  python3 -m patches.modules.telegram_dm_topic_recovery_root_guard_v1 --hermes-dir /path/to/hermes-agent
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

MARKER = "HERMES_TELEGRAM_DM_TOPIC_RECOVERY_ROOT_GUARD_v1"
DRAIN_REPLAY_TEST = "test_replay_awaits_task_returned_after_topic_recovery"


def _write_if_changed(path: Path, content: str) -> bool:
    original = path.read_text(encoding="utf-8")
    if original == content:
        return False
    backup = path.with_suffix(path.suffix + ".bak-telegram-dm-topic-root-guard")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(content, encoding="utf-8")
    return True


def patch_base_source(content: str) -> str | None:
    """Return the exact guarded base source, or None when its seam is absent."""
    if MARKER in content:
        return content

    start = content.find("    def _apply_topic_recovery(self, event: MessageEvent) -> None:\n")
    if start < 0:
        return None
    end = content.find("    def set_busy_session_handler(", start)
    if end < 0:
        return None

    old_method = content[start:end]
    if "recovered = recover(source)" not in old_method:
        return None
    if "reply_to_message_id" in old_method and "plain root/lobby message" in old_method:
        return content

    new_method = '''    def _apply_topic_recovery(self, event: MessageEvent) -> None:\n        """Rewrite event.source.thread_id in place if the hook returns one.\n\n        HERMES_TELEGRAM_DM_TOPIC_RECOVERY_ROOT_GUARD_v1: Telegram DM topic\n        recovery is only a fallback for stripped replies. A plain root/lobby\n        message is an explicit user action in the main chat, not permission to\n        route the turn into the globally most-recent topic.\n        """\n        recover = getattr(self, "_topic_recovery_fn", None)\n        if recover is None:\n            return\n        source = getattr(event, "source", None)\n        if source is None:\n            return\n        if (\n            _platform_name(getattr(source, "platform", None)) == "telegram"\n            and getattr(source, "chat_type", None) == "dm"\n            and str(getattr(source, "thread_id", None) or "") in {"", "1"}\n            and not getattr(event, "reply_to_message_id", None)\n        ):\n            return\n        try:\n            recovered = recover(source)\n        except Exception:\n            logger.debug("topic recovery hook failed", exc_info=True)\n            return\n        if recovered is None or str(recovered) == str(source.thread_id or ""):\n            return\n        try:\n            event.source = dataclasses.replace(source, thread_id=str(recovered))\n        except Exception:\n            logger.debug("topic recovery rewrite failed", exc_info=True)\n\n'''
    patched = content[:start] + new_method + content[end:]
    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    return patched


def _patch_base_adapter(hermes_dir: Path) -> bool:
    path = hermes_dir / "gateway" / "platforms" / "base.py"
    if not path.exists():
        print("[telegram_dm_topic_recovery_root_guard_v1] gateway/platforms/base.py not found")
        return False

    content = path.read_text(encoding="utf-8")
    patched = patch_base_source(content)
    if patched is None:
        print("[telegram_dm_topic_recovery_root_guard_v1] _apply_topic_recovery anchor missing")
        return False
    if patched == content:
        print("[telegram_dm_topic_recovery_root_guard_v1] base adapter already patched")
        return False
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        print(f"[telegram_dm_topic_recovery_root_guard_v1] ABORT: base.py parse failed: {exc}")
        return False

    changed = _write_if_changed(path, patched)
    if changed:
        print(f"[telegram_dm_topic_recovery_root_guard_v1] PATCHED {path}")
    return changed


def _patch_base_topic_tests(hermes_dir: Path) -> bool:
    path = hermes_dir / "tests" / "gateway" / "test_base_topic_sessions.py"
    if not path.exists():
        print("[telegram_dm_topic_recovery_root_guard_v1] test_base_topic_sessions.py not found, skip")
        return False
    content = path.read_text(encoding="utf-8")
    if "test_topic_recovery_does_not_rewrite_plain_root_message_to_latest_topic" in content:
        print("[telegram_dm_topic_recovery_root_guard_v1] base topic tests already patched")
        return False
    anchor = "    @pytest.mark.asyncio\n    async def test_process_message_background_replies_in_same_topic(self):\n"
    if anchor not in content:
        print("[telegram_dm_topic_recovery_root_guard_v1] base topic test anchor missing")
        return False
    insert = """    def test_topic_recovery_does_not_rewrite_plain_root_message_to_latest_topic(self):\n        adapter = DummyTelegramAdapter()\n        adapter.set_topic_recovery_fn(lambda _source: "99")\n        event = MessageEvent(\n            text="explicit root prompt",\n            source=SessionSource(\n                platform=Platform.TELEGRAM,\n                chat_id="208214988",\n                chat_type="dm",\n                user_id="208214988",\n                thread_id=None,\n            ),\n            message_id="10",\n        )\n\n        adapter._apply_topic_recovery(event)\n\n        assert event.source.thread_id is None\n\n    def test_topic_recovery_preserves_stripped_reply_routing(self):\n        adapter = DummyTelegramAdapter()\n        adapter.set_topic_recovery_fn(lambda _source: "99")\n        event = MessageEvent(\n            text="reply whose Telegram update lost its topic id",\n            source=SessionSource(\n                platform=Platform.TELEGRAM,\n                chat_id="208214988",\n                chat_type="dm",\n                user_id="208214988",\n                thread_id=None,\n            ),\n            message_id="11",\n            reply_to_message_id="10",\n        )\n\n        adapter._apply_topic_recovery(event)\n\n        assert event.source.thread_id == "99"\n\n"""
    patched = content.replace(anchor, insert + anchor, 1)
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        print(f"[telegram_dm_topic_recovery_root_guard_v1] ABORT: test_base_topic_sessions.py parse failed: {exc}")
        return False
    changed = _write_if_changed(path, patched)
    if changed:
        print(f"[telegram_dm_topic_recovery_root_guard_v1] PATCHED {path}")
    return changed


def _patch_text_batching_test(hermes_dir: Path) -> bool:
    path = hermes_dir / "tests" / "gateway" / "test_telegram_text_batching.py"
    if not path.exists():
        print("[telegram_dm_topic_recovery_root_guard_v1] test_telegram_text_batching.py not found, skip")
        return False
    content = path.read_text(encoding="utf-8")
    needle = """            ),\n        )\n\n        adapter._enqueue_text_event(event)\n"""
    replacement = """            ),\n            reply_to_message_id="10",\n        )\n\n        adapter._enqueue_text_event(event)\n"""
    if replacement in content:
        print("[telegram_dm_topic_recovery_root_guard_v1] text batching test already patched")
        return False
    test_name = "test_dm_topic_batching_recovers_thread_before_keying"
    idx = content.find(test_name)
    if idx < 0:
        print("[telegram_dm_topic_recovery_root_guard_v1] batching recovery test missing, skip")
        return False
    local = content[idx : idx + 1500]
    if needle not in local:
        print("[telegram_dm_topic_recovery_root_guard_v1] batching test anchor missing")
        return False
    local_patched = local.replace(needle, replacement, 1)
    patched = content[:idx] + local_patched + content[idx + len(local) :]
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        print(f"[telegram_dm_topic_recovery_root_guard_v1] ABORT: test_telegram_text_batching.py parse failed: {exc}")
        return False
    changed = _write_if_changed(path, patched)
    if changed:
        print(f"[telegram_dm_topic_recovery_root_guard_v1] PATCHED {path}")
    return changed


def _drain_inbox_replay_test_parts(hermes_dir: Path) -> tuple[Path, str, list[str], int, int, str]:
    path = hermes_dir / "tests" / "gateway" / "test_drain_inbox.py"
    if not path.exists():
        raise RuntimeError("paired durable-inbox test source is required")
    content = path.read_text(encoding="utf-8")
    tree = ast.parse(content)
    node = next(
        (item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == DRAIN_REPLAY_TEST),
        None,
    )
    if node is None or node.end_lineno is None:
        raise RuntimeError(f"paired durable-inbox test seam is missing: {DRAIN_REPLAY_TEST}")
    lines = content.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    local = "".join(lines[start:end])
    reply_anchor = '    event.reply_to_message_id = "reply-anchor"\n'
    needle = "    event = _event()\n"
    if reply_anchor not in local and needle not in local:
        raise RuntimeError(f"paired durable-inbox test anchor drifted: {DRAIN_REPLAY_TEST}")
    return path, content, lines, start, end, local


def _patch_drain_inbox_replay_test(hermes_dir: Path) -> bool:
    """Keep the replay test shaped like the stripped reply it exercises."""
    path, content, lines, start, end, local = _drain_inbox_replay_test_parts(hermes_dir)
    reply_anchor = '    event.reply_to_message_id = "reply-anchor"\n'
    if reply_anchor in local:
        print("[telegram_dm_topic_recovery_root_guard_v1] drain replay test already patched")
        return False
    needle = "    event = _event()\n"
    local = local.replace(needle, needle + reply_anchor, 1)
    patched = "".join(lines[:start]) + local + "".join(lines[end:])
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        raise RuntimeError(f"paired durable-inbox test parse failed: {exc}") from exc
    changed = _write_if_changed(path, patched)
    if changed:
        print(f"[telegram_dm_topic_recovery_root_guard_v1] PATCHED {path}")
    return changed


def patch_telegram_dm_topic_recovery_root_guard_v1(hermes_dir: Path) -> bool:
    _drain_inbox_replay_test_parts(hermes_dir)
    changed = False
    changed |= _patch_base_adapter(hermes_dir)
    changed |= _patch_base_topic_tests(hermes_dir)
    changed |= _patch_text_batching_test(hermes_dir)
    changed |= _patch_drain_inbox_replay_test(hermes_dir)
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description="Guard Telegram DM topic recovery for explicit root messages")
    ap.add_argument("--hermes-dir", required=True, type=Path)
    args = ap.parse_args()
    if not patch_telegram_dm_topic_recovery_root_guard_v1(args.hermes_dir):
        sys.exit(0)


if __name__ == "__main__":
    main()
