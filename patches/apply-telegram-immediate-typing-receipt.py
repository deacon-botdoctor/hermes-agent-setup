#!/usr/bin/env python3
"""Acknowledge accepted Telegram text immediately and measure the receipt."""

import argparse
import shutil
import sys
import time
from pathlib import Path


MARKER = "HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v2"
V1_MARKER = "HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v1"

HELPER_ANCHOR = "    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
HELPER = '''    # [HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v2] helper
    async def _send_initial_typing_receipt(self, event: MessageEvent, accepted_at: float) -> None:
        """Bounded client acknowledgment concurrent with agent preparation."""
        if not getattr(self.config, "typing_indicator", True):
            return
        metadata: Dict[str, Any] = {}
        if event.source.thread_id:
            metadata["thread_id"] = event.source.thread_id
            if event.source.chat_type == "dm":
                metadata["telegram_dm_topic_reply_fallback"] = True
        result = "failed"
        try:
            sent = await asyncio.wait_for(
                self.send_typing(event.source.chat_id, metadata=metadata or None),
                timeout=1.5,
            )
            if sent:
                result = "success"
                event._typing_receipt_sent_at = asyncio.get_running_loop().time()
        except asyncio.TimeoutError:
            result = "timeout"
        except Exception:
            result = "error"
            logger.debug("[Telegram] Initial typing receipt failed", exc_info=True)
        latency_ms = (asyncio.get_running_loop().time() - accepted_at) * 1000.0
        logger.info(
            "[Telegram] typing receipt: result=%s latency_ms=%.1f chat=%s thread=%s",
            result, latency_ms, event.source.chat_id, event.source.thread_id or "-",
        )

'''

OLD_TEXT_BLOCK = '''        await self._ensure_forum_commands(update.message)

        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self._cache_replied_media(msg, event)
        event = self._apply_telegram_group_observe_attribution(event)
        self._enqueue_text_event(event)
'''

NEW_TEXT_BLOCK = '''        accepted_at = asyncio.get_running_loop().time()
        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        event = self._apply_telegram_group_observe_attribution(event)
        event._typing_receipt_task = asyncio.create_task(
            self._send_initial_typing_receipt(event, accepted_at)
        )
        await self._ensure_forum_commands(msg)
        await self._cache_replied_media(msg, event)
        self._enqueue_text_event(event)
'''

OLD_SEND_SIGNATURE = "    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:\n"
NEW_SEND_SIGNATURE = "    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:\n"

OLD_BASE_BLOCK = '''            typing_task = asyncio.create_task(
                self._keep_typing(
                    event.source.chat_id,
                    **_keep_typing_kwargs,
                )
            )
'''

V1_BASE_BLOCK = '''            async def _run_typing_refresh() -> None:
                early_sent_at = getattr(event, "_typing_receipt_sent_at", None)
                if early_sent_at is not None:
                    elapsed = asyncio.get_running_loop().time() - early_sent_at
                    await asyncio.sleep(max(0.0, 2.0 - elapsed))
                await self._keep_typing(
                    event.source.chat_id,
                    **_keep_typing_kwargs,
                )

            typing_task = asyncio.create_task(_run_typing_refresh())
'''

NEW_BASE_BLOCK = '''            async def _run_typing_refresh() -> None:
                receipt_task = getattr(event, "_typing_receipt_task", None)
                if receipt_task is not None:
                    try:
                        await asyncio.shield(receipt_task)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                early_sent_at = getattr(event, "_typing_receipt_sent_at", None)
                if early_sent_at is not None:
                    elapsed = asyncio.get_running_loop().time() - early_sent_at
                    await asyncio.sleep(max(0.0, 2.0 - elapsed))
                await self._keep_typing(
                    event.source.chat_id,
                    **_keep_typing_kwargs,
                )

            typing_task = asyncio.create_task(_run_typing_refresh())
'''


def patch_adapter(source: str) -> str:
    if f"[{MARKER}] helper" in source:
        return source
    if f"[{V1_MARKER}] helper" in source:
        v1_helper = HELPER.replace(MARKER, V1_MARKER).replace(
            "concurrent with agent preparation", "before batching or agent preparation"
        ).replace("timeout=1.5", "timeout=0.5")
        if source.count(v1_helper) != 1:
            raise ValueError("Telegram v1 receipt helper anchor is not unique")
        source = source.replace(v1_helper, HELPER, 1)
        old_await = "        await self._send_initial_typing_receipt(event, accepted_at)\n"
        task_start = "        event._typing_receipt_task = asyncio.create_task(\n"
        if task_start not in source:
            if source.count(old_await) != 1:
                raise ValueError("Telegram v1 receipt call anchor is not unique")
            source = source.replace(
                old_await,
                task_start
                + "            self._send_initial_typing_receipt(event, accepted_at)\n"
                + "        )\n",
                1,
            )
        return source
    if source.count(HELPER_ANCHOR) != 1 or source.count(OLD_TEXT_BLOCK) != 1:
        raise ValueError("Telegram accepted-text anchors are not unique")
    if source.count(OLD_SEND_SIGNATURE) != 1:
        raise ValueError("Telegram send_typing signature anchor is not unique")
    source = source.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
    source = source.replace(OLD_TEXT_BLOCK, NEW_TEXT_BLOCK, 1)
    source = source.replace(OLD_SEND_SIGNATURE, NEW_SEND_SIGNATURE, 1)
    source = source.replace(
        "        if not self._bot or self._typing_in_cooldown(chat_id):\n            return\n",
        "        if not self._bot or self._typing_in_cooldown(chat_id):\n            return False\n",
        1,
    )
    source = source.replace(
        "            self._telegram_typing_cooldown_until.pop(str(chat_id), None)\n        except Exception as e:\n",
        "            self._telegram_typing_cooldown_until.pop(str(chat_id), None)\n            return True\n        except Exception as e:\n",
        1,
    )
    source = source.replace(
        "                    self._telegram_typing_cooldown_until.pop(str(chat_id), None)\n                    return\n",
        "                    self._telegram_typing_cooldown_until.pop(str(chat_id), None)\n                    return True\n",
        1,
    )
    failure_tails = (
        '''            logger.debug(
                "[%s] Failed to send Telegram typing indicator: %s",
                self.name,
                e,
                exc_info=True,
            )
''',
        '''            logger.debug(
                "[%s] Failed to send Telegram typing indicator: %s",
                self.name,
                _redact_telegram_error_text(e),
                exc_info=True,
            )
''',
    )
    matches = [tail for tail in failure_tails if source.count(tail) == 1]
    if len(matches) != 1:
        raise ValueError("Telegram send_typing failure anchor is not unique")
    failure_tail = matches[0]
    return source.replace(failure_tail, failure_tail + "            return False\n", 1)


def patch_base(source: str) -> str:
    if 'receipt_task = getattr(event, "_typing_receipt_task", None)' in source:
        return source
    if source.count(V1_BASE_BLOCK) == 1:
        return source.replace(V1_BASE_BLOCK, NEW_BASE_BLOCK, 1)
    if source.count(OLD_BASE_BLOCK) != 1:
        raise ValueError("base typing refresh anchor is not unique")
    return source.replace(OLD_BASE_BLOCK, NEW_BASE_BLOCK, 1)


def patch_telegram_immediate_typing_receipt_v2(hermes_dir: Path) -> bool:
    """Apply the receipt upgrade and return whether either runtime file changed."""
    adapter = hermes_dir / "plugins" / "platforms" / "telegram" / "adapter.py"
    base = hermes_dir / "gateway" / "platforms" / "base.py"
    adapter_source = adapter.read_text(encoding="utf-8")
    base_source = base.read_text(encoding="utf-8")
    patched_adapter = patch_adapter(adapter_source)
    patched_base = patch_base(base_source)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    changed = False
    for path, old, new in ((adapter, adapter_source, patched_adapter), (base, base_source, patched_base)):
        if old == new:
            continue
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}-pre-typing-receipt"))
        path.write_text(new, encoding="utf-8")
        print(f"OK: patched {path}")
        changed = True
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(args.hermes_dir)
    adapter = root / "plugins" / "platforms" / "telegram" / "adapter.py"
    base = root / "gateway" / "platforms" / "base.py"
    try:
        adapter_source = adapter.read_text(encoding="utf-8")
        base_source = base.read_text(encoding="utf-8")
        patch_adapter(adapter_source)
        patch_base(base_source)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 3
    if args.dry_run:
        print("DRY_RUN OK: immediate typing receipt applies cleanly")
        return 0
    try:
        changed = patch_telegram_immediate_typing_receipt_v2(root)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 3
    if not changed:
        print("ALREADY_PATCHED: markers present, no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
