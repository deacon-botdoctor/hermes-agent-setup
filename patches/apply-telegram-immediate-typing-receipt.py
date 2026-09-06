#!/usr/bin/env python3
"""Acknowledge accepted Telegram text immediately, then refresh during work."""

import argparse
import shutil
import sys
import time
from pathlib import Path

MARKER = "HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v5"
V4_MARKER = "HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v4"
V3_MARKER = "HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v3"
V2_MARKER = "HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v2"
V1_MARKER = "HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v1"

HELPER_ANCHOR = (
    "    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
)
V2_HELPER = '''    # [HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v2] helper
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

V3_HELPER = '''    # [HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v3] helper
    # HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v2 compatibility marker
    def _start_immediate_typing_receipt(self, event: MessageEvent, accepted_at: float) -> None:
        """Start or reuse one pre-turn typing loop for this Telegram session."""
        if not getattr(self.config, "typing_indicator", True):
            return
        key = self._text_batch_key(event)
        tasks = getattr(self, "_telegram_pre_turn_typing_tasks", None)
        if tasks is None:
            tasks = {}
            self._telegram_pre_turn_typing_tasks = tasks
        task = tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._send_initial_typing_receipt(event, accepted_at)
            )
            tasks[key] = task

            def _forget(done_task: asyncio.Task) -> None:
                if tasks.get(key) is done_task:
                    tasks.pop(key, None)

            task.add_done_callback(_forget)
        event._typing_receipt_task = task

    async def _send_initial_typing_receipt(self, event: MessageEvent, accepted_at: float) -> None:
        """Send the measured first action, then continue Hermes's native loop."""
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
        except asyncio.CancelledError:
            raise
        except Exception:
            result = "error"
            logger.debug("[Telegram] Initial typing receipt failed", exc_info=True)
        latency_ms = (asyncio.get_running_loop().time() - accepted_at) * 1000.0
        logger.info(
            "[Telegram] typing receipt: result=%s latency_ms=%.1f chat=%s thread=%s",
            result, latency_ms, event.source.chat_id, event.source.thread_id or "-",
        )
        elapsed = max(0.0, asyncio.get_running_loop().time() - accepted_at)
        await asyncio.sleep(max(0.0, 2.0 - elapsed))
        await self._keep_typing(
            event.source.chat_id,
            metadata=metadata or None,
        )

'''

V3_TRACE_HELPER = '''    # [HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v3] helper
    # HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v2 compatibility marker
    def _start_immediate_typing_receipt(self, event: MessageEvent, accepted_at: float) -> None:
        """Start or reuse one pre-turn typing loop for this Telegram session."""
        if not getattr(self.config, "typing_indicator", True):
            return
        from agent.runtime_performance_events import (
            adopt_gateway_trace,
            begin_gateway_turn,
            current_gateway_trace,
        )
        platform = str(getattr(event.source.platform, "value", event.source.platform))
        key = self._text_batch_key(event)
        tasks = getattr(self, "_telegram_pre_turn_typing_tasks", None)
        if tasks is None:
            tasks = {}
            self._telegram_pre_turn_typing_tasks = tasks
        task = tasks.get(key)
        pending_batches = getattr(self, "_pending_text_batches", {})
        if task is None or task.done() or key not in pending_batches:
            begin_gateway_turn(platform)
            trace = current_gateway_trace()
            event._hermes_runtime_performance_trace = trace
            event._hermes_runtime_performance_turn_id = trace.turn_id
            event._hermes_runtime_performance_platform = platform
            task = asyncio.create_task(
                self._send_initial_typing_receipt(event, accepted_at)
            )
            task._hermes_runtime_performance_trace = trace
            task._hermes_runtime_performance_turn_id = trace.turn_id
            task._hermes_runtime_performance_platform = platform
            tasks[key] = task

            def _forget(done_task: asyncio.Task) -> None:
                if tasks.get(key) is done_task:
                    tasks.pop(key, None)

            task.add_done_callback(_forget)
        else:
            trace = getattr(task, "_hermes_runtime_performance_trace", None)
            if adopt_gateway_trace(trace):
                event._hermes_runtime_performance_trace = trace
                event._hermes_runtime_performance_turn_id = trace.turn_id
                event._hermes_runtime_performance_platform = trace.platform
        event._typing_receipt_task = task

    async def _send_initial_typing_receipt(self, event: MessageEvent, accepted_at: float) -> None:
        """Send the measured first action, then continue Hermes's native loop."""
        from agent.runtime_performance_events import (
            adopt_gateway_trace,
            record_turn_event,
        )
        trace_adopted = adopt_gateway_trace(
            getattr(event, "_hermes_runtime_performance_trace", None)
        )
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
                if trace_adopted:
                    record_turn_event(
                        "typing_indicator_started",
                        timing_semantics="telegram_typing_ack_exact",
                    )
        except asyncio.TimeoutError:
            result = "timeout"
        except asyncio.CancelledError:
            raise
        except Exception:
            result = "error"
            logger.debug("[Telegram] Initial typing receipt failed", exc_info=True)
        latency_ms = (asyncio.get_running_loop().time() - accepted_at) * 1000.0
        logger.info(
            "[Telegram] typing receipt: result=%s latency_ms=%.1f chat=%s thread=%s",
            result, latency_ms, event.source.chat_id, event.source.thread_id or "-",
        )
        elapsed = max(0.0, asyncio.get_running_loop().time() - accepted_at)
        await asyncio.sleep(max(0.0, 2.0 - elapsed))
        await self._keep_typing(
            event.source.chat_id,
            metadata=metadata or None,
        )

'''

V4_HELPER = '''    # [HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v4] helper
    # HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v3 compatibility marker
    # HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v2 compatibility marker
    def _adopt_immediate_typing_carrier(
        self, event: MessageEvent, carrier: MessageEvent
    ) -> bool:
        from agent.runtime_performance_events import adopt_gateway_trace
        trace = getattr(carrier, "_hermes_runtime_performance_trace", None)
        if not adopt_gateway_trace(trace):
            return False
        event_task = getattr(event, "_typing_receipt_task", None)
        carrier_task = getattr(carrier, "_typing_receipt_task", None)
        if (
            event_task is not None
            and event_task is not carrier_task
            and not event_task.done()
        ):
            event_task.cancel()
        event._hermes_runtime_performance_trace = trace
        event._hermes_runtime_performance_turn_id = trace.turn_id
        event._hermes_runtime_performance_platform = trace.platform
        if carrier_task is not None:
            event._typing_receipt_task = carrier_task
        elif event_task is not None and event_task is not carrier_task:
            del event._typing_receipt_task
        return True

    def _start_immediate_typing_receipt(self, event: MessageEvent, accepted_at: float) -> None:
        """Start one trace-bound pre-turn typing loop for an admitted batch."""
        if not getattr(self.config, "typing_indicator", True):
            return
        existing_task = getattr(event, "_typing_receipt_task", None)
        if existing_task is not None and not existing_task.done():
            return
        from agent.runtime_performance_events import (
            adopt_gateway_trace,
            begin_gateway_turn,
            current_gateway_trace,
        )
        platform = str(getattr(event.source.platform, "value", event.source.platform))
        trace = getattr(event, "_hermes_runtime_performance_trace", None)
        if not adopt_gateway_trace(trace):
            begin_gateway_turn(platform, defer_publication=True)
            trace = current_gateway_trace()
            event._hermes_runtime_performance_trace = trace
            event._hermes_runtime_performance_turn_id = trace.turn_id
            event._hermes_runtime_performance_platform = platform
        task = asyncio.create_task(
            self._send_initial_typing_receipt(event, accepted_at)
        )
        task._hermes_runtime_performance_trace = trace
        task._hermes_runtime_performance_turn_id = trace.turn_id
        task._hermes_runtime_performance_platform = platform
        event._typing_receipt_task = task

    async def _send_initial_typing_receipt(self, event: MessageEvent, accepted_at: float) -> None:
        """Send the measured first action, then continue Hermes's native loop."""
        from agent.runtime_performance_events import (
            adopt_gateway_trace,
            record_turn_event,
        )
        trace_adopted = adopt_gateway_trace(
            getattr(event, "_hermes_runtime_performance_trace", None)
        )
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
            if sent is True:
                result = "success"
                event._typing_receipt_sent_at = asyncio.get_running_loop().time()
                trace_adopted = adopt_gateway_trace(
                    getattr(event, "_hermes_runtime_performance_trace", None)
                )
                if trace_adopted:
                    record_turn_event(
                        "typing_indicator_started",
                        timing_semantics="telegram_typing_ack_exact",
                    )
        except asyncio.TimeoutError:
            result = "timeout"
        except asyncio.CancelledError:
            raise
        except Exception:
            result = "error"
            logger.debug("[Telegram] Initial typing receipt failed", exc_info=True)
        latency_ms = (asyncio.get_running_loop().time() - accepted_at) * 1000.0
        logger.info(
            "[Telegram] typing receipt: result=%s latency_ms=%.1f chat=%s thread=%s",
            result, latency_ms, event.source.chat_id, event.source.thread_id or "-",
        )
        elapsed = max(0.0, asyncio.get_running_loop().time() - accepted_at)
        await asyncio.sleep(max(0.0, 2.0 - elapsed))
        await self._keep_typing(
            event.source.chat_id,
            metadata=metadata or None,
        )

'''

HELPER = (
    V4_HELPER.replace(
        "    # [HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v4] helper\n",
        "    # [HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v5] helper\n"
        "    # HERMES_TELEGRAM_IMMEDIATE_TYPING_RECEIPT_v4 compatibility marker\n",
        1,
    )
    .replace(
        '"""Start one trace-bound pre-turn typing loop for an admitted batch."""',
        '"""Start one trace-bound receipt for an admitted batch."""',
        1,
    )
    .replace(
        '"""Send the measured first action, then continue Hermes\'s native loop."""',
        '"""Send one measured, bounded typing action at accepted ingress."""',
        1,
    )
    .replace(
        '''        elapsed = max(0.0, asyncio.get_running_loop().time() - accepted_at)
        await asyncio.sleep(max(0.0, 2.0 - elapsed))
        await self._keep_typing(
            event.source.chat_id,
            metadata=metadata or None,
        )
''',
        "",
        1,
    )
)

OLD_TEXT_BLOCK = """        await self._ensure_forum_commands(update.message)

        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self._cache_replied_media(msg, event)
        event = self._apply_telegram_group_observe_attribution(event)
        self._enqueue_text_event(event)
"""

V3_TEXT_BLOCK = """        accepted_at = asyncio.get_running_loop().time()
        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        event = self._apply_telegram_group_observe_attribution(event)
        self._start_immediate_typing_receipt(event, accepted_at)
        await self._ensure_forum_commands(msg)
        await self._cache_replied_media(msg, event)
        self._enqueue_text_event(event)
"""

NEW_TEXT_BLOCK = """        accepted_at = asyncio.get_running_loop().time()
        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        event = self._apply_telegram_group_observe_attribution(event)
        event._hermes_typing_accepted_at = accepted_at
        await self._ensure_forum_commands(msg)
        await self._cache_replied_media(msg, event)
        self._enqueue_text_event(event)
"""

ENQUEUE_ANCHOR = """        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
"""

ENQUEUE_WITH_RECEIPT = """        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
            accepted_at = getattr(
                event, "_hermes_typing_accepted_at", asyncio.get_running_loop().time()
            )
            self._start_immediate_typing_receipt(event, accepted_at)
        else:
            if not self._adopt_immediate_typing_carrier(event, existing):
                accepted_at = getattr(
                    existing,
                    "_hermes_typing_accepted_at",
                    getattr(event, "_hermes_typing_accepted_at", asyncio.get_running_loop().time()),
                )
                self._start_immediate_typing_receipt(existing, accepted_at)
                self._adopt_immediate_typing_carrier(event, existing)
"""

MERGE_CARRIER_ANCHOR = """        if (
            merge_text
            and getattr(existing, "message_type", None) == MessageType.TEXT
            and event.message_type == MessageType.TEXT
        ):
            if event.text:
"""

MERGE_CARRIER_BLOCK = MERGE_CARRIER_ANCHOR.replace(
    "            if event.text:\n",
    "            from agent.runtime_performance_events import reconcile_gateway_events\n"
    "            reconcile_gateway_events(existing, event)\n"
    "            if event.text:\n",
)

DEBOUNCE_CARRIER_ANCHOR = '''        else:
            if event.text:
                state.event.text = (
'''
DEBOUNCE_CARRIER_BLOCK = '''        else:
            from agent.runtime_performance_events import reconcile_gateway_events
            reconcile_gateway_events(state.event, event)
            if event.text:
                state.event.text = (
'''

OLD_SEND_SIGNATURE = (
    "    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:\n"
)
NEW_SEND_SIGNATURE = (
    "    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:\n"
)

ORIGINAL_INITIAL_ACK = """            await self._bot.send_chat_action(chat_id=chat_id, action="typing")
            self._telegram_typing_cooldown_until.pop(str(chat_id), None)
"""
LEGACY_INITIAL_ACK = ORIGINAL_INITIAL_ACK + "            return True\n"
EXPLICIT_INITIAL_ACK = """            sent = await self._bot.send_chat_action(chat_id=chat_id, action="typing")
            if sent is True:
                self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                return True
            return False
"""

DM_TOPIC_INITIAL_ACK = """            await self._bot.send_chat_action(
                chat_id=normalize_telegram_chat_id(chat_id),
                action="typing",
                message_thread_id=message_thread_id,
            )
            self._telegram_typing_cooldown_until.pop(str(chat_id), None)
"""
DM_TOPIC_LEGACY_INITIAL_ACK = DM_TOPIC_INITIAL_ACK + "            return True\n"
DM_TOPIC_EXPLICIT_INITIAL_ACK = """            sent = await self._bot.send_chat_action(
                chat_id=normalize_telegram_chat_id(chat_id),
                action="typing",
                message_thread_id=message_thread_id,
            )
            if sent is True:
                self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                return True
            return False
"""

ORIGINAL_RETRY_ACK = """                    await self._bot.send_chat_action(chat_id=chat_id, action="typing")
                    self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                    return
"""
LEGACY_RETRY_ACK = """                    await self._bot.send_chat_action(chat_id=chat_id, action="typing")
                    self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                    return True
"""
EXPLICIT_RETRY_ACK = """                    retry_sent = await self._bot.send_chat_action(
                        chat_id=chat_id, action="typing"
                    )
                    if retry_sent is True:
                        self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                        return True
                    return False
"""

DM_TOPIC_FALLBACK_ACK = """                    await self._bot.send_chat_action(
                        chat_id=normalize_telegram_chat_id(chat_id),
                        action="typing",
                    )
                    self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                    return
"""
DM_TOPIC_LEGACY_FALLBACK_ACK = DM_TOPIC_FALLBACK_ACK.replace(
    "                    return\n", "                    return True\n"
)
DM_TOPIC_EXPLICIT_FALLBACK_ACK = """                    fallback_sent = await self._bot.send_chat_action(
                        chat_id=normalize_telegram_chat_id(chat_id),
                        action="typing",
                    )
                    if fallback_sent is True:
                        self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                        return True
                    return False
"""

FAILURE_TAILS = (
    """            logger.debug(
                "[%s] Failed to send Telegram typing indicator: %s",
                self.name,
                e,
                exc_info=True,
            )
""",
    """            logger.debug(
                "[%s] Failed to send Telegram typing indicator: %s",
                self.name,
                _redact_telegram_error_text(e),
                exc_info=True,
            )
""",
)

OLD_BASE_BLOCK = """            typing_task = asyncio.create_task(
                self._keep_typing(
                    event.source.chat_id,
                    **_keep_typing_kwargs,
                )
            )
"""

V1_BASE_BLOCK = """            async def _run_typing_refresh() -> None:
                early_sent_at = getattr(event, "_typing_receipt_sent_at", None)
                if early_sent_at is not None:
                    elapsed = asyncio.get_running_loop().time() - early_sent_at
                    await asyncio.sleep(max(0.0, 2.0 - elapsed))
                await self._keep_typing(
                    event.source.chat_id,
                    **_keep_typing_kwargs,
                )

            typing_task = asyncio.create_task(_run_typing_refresh())
"""

V2_BASE_BLOCK = """            async def _run_typing_refresh() -> None:
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
"""

REUSE_BASE_BLOCK = """            receipt_task = getattr(event, "_typing_receipt_task", None)
            if receipt_task is not None and not receipt_task.done():
                typing_task = receipt_task
            else:
                typing_task = asyncio.create_task(
                    self._keep_typing(
                        event.source.chat_id,
                        **_keep_typing_kwargs,
                    )
                )
"""

NEW_BASE_BLOCK = """            async def _run_typing_refresh() -> None:
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
"""


def _replace_transport_variant(source: str, variants: tuple[str, ...], replacement: str, label: str) -> str:
    if source.count(replacement) == 1:
        if any(source.count(variant) for variant in variants):
            raise ValueError(f"Telegram {label} anchors are ambiguous")
        return source
    for variant in variants:
        if source.count(variant) == 1:
            return source.replace(variant, replacement, 1)
    raise ValueError(f"Telegram {label} anchor is not unique")


def _patch_transport_acknowledgement(source: str) -> str:
    if source.count(NEW_SEND_SIGNATURE) != 1:
        if source.count(OLD_SEND_SIGNATURE) != 1:
            raise ValueError("Telegram send_typing signature anchor is not unique")
        source = source.replace(OLD_SEND_SIGNATURE, NEW_SEND_SIGNATURE, 1)
    guard = "        if not self._bot or self._typing_in_cooldown(chat_id):\n"
    if source.count(guard + "            return False\n") != 1:
        if source.count(guard + "            return\n") != 1:
            raise ValueError("Telegram send_typing guard anchor is not unique")
        source = source.replace(
            guard + "            return\n",
            guard + "            return False\n",
            1,
        )
    dm_topic_shape = any(
        source.count(anchor) == 1
        for anchor in (
            DM_TOPIC_LEGACY_INITIAL_ACK,
            DM_TOPIC_INITIAL_ACK,
            DM_TOPIC_EXPLICIT_INITIAL_ACK,
        )
    )
    if dm_topic_shape:
        source = _replace_transport_variant(
            source,
            (DM_TOPIC_LEGACY_INITIAL_ACK, DM_TOPIC_INITIAL_ACK),
            DM_TOPIC_EXPLICIT_INITIAL_ACK,
            "initial typing acknowledgment",
        )
        source = _replace_transport_variant(
            source,
            (DM_TOPIC_LEGACY_FALLBACK_ACK, DM_TOPIC_FALLBACK_ACK),
            DM_TOPIC_EXPLICIT_FALLBACK_ACK,
            "DM-topic fallback typing acknowledgment",
        )
    else:
        source = _replace_transport_variant(
            source,
            (LEGACY_INITIAL_ACK, ORIGINAL_INITIAL_ACK),
            EXPLICIT_INITIAL_ACK,
            "initial typing acknowledgment",
        )
        source = _replace_transport_variant(
            source,
            (LEGACY_RETRY_ACK, ORIGINAL_RETRY_ACK),
            EXPLICIT_RETRY_ACK,
            "retry typing acknowledgment",
        )
    matches = [tail for tail in FAILURE_TAILS if source.count(tail) == 1]
    completed = [tail for tail in FAILURE_TAILS if source.count(tail + "            return False\n") == 1]
    if len(completed) == 1:
        return source
    if len(matches) != 1:
        raise ValueError("Telegram send_typing failure anchor is not unique")
    return source.replace(matches[0], matches[0] + "            return False\n", 1)


def patch_adapter(source: str) -> str:
    if f"[{MARKER}] helper" in source:
        if source.count(HELPER) != 1 or source.count(ENQUEUE_WITH_RECEIPT) != 1:
            raise ValueError("unrecognized v5 receipt body; rebuild an exact clean Golden assembly")
        return _patch_transport_acknowledgement(source)
    if f"[{V4_MARKER}] helper" in source:
        if source.count(V4_HELPER) != 1 or source.count(ENQUEUE_WITH_RECEIPT) != 1:
            raise ValueError("unrecognized v4 receipt body; rebuild an exact clean Golden assembly")
        source = source.replace(V4_HELPER, HELPER, 1)
        return _patch_transport_acknowledgement(source)
    if f"[{V3_MARKER}] helper" in source:
        helpers = [helper for helper in (V3_HELPER, V3_TRACE_HELPER) if source.count(helper) == 1]
        if len(helpers) != 1:
            raise ValueError("Telegram v3 receipt helper anchor is not unique")
        if source.count(V3_TEXT_BLOCK) != 1:
            raise ValueError("Telegram v3 receipt handler anchor is not unique")
        source = source.replace(helpers[0], HELPER, 1)
        source = source.replace(V3_TEXT_BLOCK, NEW_TEXT_BLOCK, 1)
        if source.count(ENQUEUE_ANCHOR) != 1:
            raise ValueError("Telegram batch-admission anchor is not unique")
        source = source.replace(ENQUEUE_ANCHOR, ENQUEUE_WITH_RECEIPT, 1)
        return _patch_transport_acknowledgement(source)
    if f"[{V2_MARKER}] helper" in source:
        if source.count(V2_HELPER) != 1:
            raise ValueError("Telegram v2 receipt helper anchor is not unique")
        source = source.replace(V2_HELPER, HELPER, 1)
        old_task = """        event._typing_receipt_task = asyncio.create_task(
            self._send_initial_typing_receipt(event, accepted_at)
        )
"""
        new_task = "        self._start_immediate_typing_receipt(event, accepted_at)\n"
        if source.count(old_task) != 1:
            raise ValueError("Telegram v2 receipt task anchor is not unique")
        source = source.replace(old_task, new_task, 1)
        if source.count(V3_TEXT_BLOCK) != 1:
            raise ValueError("Telegram v2 receipt handler anchor is not unique")
        source = source.replace(V3_TEXT_BLOCK, NEW_TEXT_BLOCK, 1)
        if source.count(ENQUEUE_ANCHOR) != 1:
            raise ValueError("Telegram batch-admission anchor is not unique")
        source = source.replace(ENQUEUE_ANCHOR, ENQUEUE_WITH_RECEIPT, 1)
        return _patch_transport_acknowledgement(source)
    if f"[{V1_MARKER}] helper" in source:
        v1_helper = (
            V2_HELPER.replace(V2_MARKER, V1_MARKER)
            .replace("concurrent with agent preparation", "before batching or agent preparation")
            .replace("timeout=1.5", "timeout=0.5")
        )
        if source.count(v1_helper) != 1:
            raise ValueError("Telegram v1 receipt helper anchor is not unique")
        source = source.replace(v1_helper, HELPER, 1)
        old_await = "        await self._send_initial_typing_receipt(event, accepted_at)\n"
        if source.count(old_await) != 1:
            raise ValueError("Telegram v1 receipt call anchor is not unique")
        source = source.replace(
            old_await,
            "        self._start_immediate_typing_receipt(event, accepted_at)\n",
            1,
        )
        if source.count(V3_TEXT_BLOCK) != 1:
            raise ValueError("Telegram v1 receipt handler anchor is not unique")
        source = source.replace(V3_TEXT_BLOCK, NEW_TEXT_BLOCK, 1)
        if source.count(ENQUEUE_ANCHOR) != 1:
            raise ValueError("Telegram batch-admission anchor is not unique")
        source = source.replace(ENQUEUE_ANCHOR, ENQUEUE_WITH_RECEIPT, 1)
        return _patch_transport_acknowledgement(source)
    if source.count(HELPER_ANCHOR) != 1 or source.count(OLD_TEXT_BLOCK) != 1:
        raise ValueError("Telegram accepted-text anchors are not unique")
    source = source.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
    source = source.replace(OLD_TEXT_BLOCK, NEW_TEXT_BLOCK, 1)
    if source.count(ENQUEUE_ANCHOR) != 1:
        raise ValueError("Telegram batch-admission anchor is not unique")
    source = source.replace(ENQUEUE_ANCHOR, ENQUEUE_WITH_RECEIPT, 1)
    return _patch_transport_acknowledgement(source)


def patch_base(source: str) -> str:
    if source.count(NEW_BASE_BLOCK) != 1:
        if source.count(V2_BASE_BLOCK) == 1:
            source = source.replace(V2_BASE_BLOCK, NEW_BASE_BLOCK, 1)
        elif source.count(V1_BASE_BLOCK) == 1:
            source = source.replace(V1_BASE_BLOCK, NEW_BASE_BLOCK, 1)
        elif source.count(REUSE_BASE_BLOCK) == 1:
            source = source.replace(REUSE_BASE_BLOCK, NEW_BASE_BLOCK, 1)
        elif source.count(OLD_BASE_BLOCK) == 1:
            source = source.replace(OLD_BASE_BLOCK, NEW_BASE_BLOCK, 1)
        else:
            raise ValueError("base typing refresh anchor is not unique")
    replacements = [
        (MERGE_CARRIER_ANCHOR, MERGE_CARRIER_BLOCK),
        (DEBOUNCE_CARRIER_ANCHOR, DEBOUNCE_CARRIER_BLOCK),
    ]
    for branch in (
        "        if existing_is_photo and incoming_is_photo:\n",
        "        if existing_has_media or incoming_has_media:\n",
    ):
        replacements.append((
            branch,
            branch + "            from agent.runtime_performance_events import reconcile_gateway_events\n"
            "            reconcile_gateway_events(existing, event)\n",
        ))
    for old, new in replacements:
        if source.count(new) == 1:
            continue
        if source.count(old) != 1:
            raise ValueError("base coalescing carrier anchor is not unique")
        source = source.replace(old, new, 1)
    return source


def _patch_native_typing(hermes_dir: Path, *, check: bool = False) -> bool:
    """Port the receipt into native admission, refresh, and teardown owners."""
    adapter = hermes_dir / "plugins/platforms/telegram/adapter.py"
    base = hermes_dir / "gateway/platforms/base.py"
    a, b = adapter.read_text(), base.read_text()
    def replace(source, old, new):
        if source.count(old) != 1:
            raise ValueError("native typing owner anchor drift")
        return source.replace(old, new, 1)
    if MARKER not in a:
        a = replace(a, HELPER_ANCHOR, HELPER + HELPER_ANCHOR)
        old = '        await self._ensure_forum_commands(update.message)\n        self._enqueue_text_event(await self._build_triggered_event(msg, update, MessageType.TEXT))\n'
        new = '        accepted_at = asyncio.get_running_loop().time()\n        await self._ensure_forum_commands(update.message)\n        event = await self._build_triggered_event(msg, update, MessageType.TEXT)\n        event._hermes_typing_accepted_at = accepted_at\n        self._enqueue_text_event(event)\n'
        a = replace(a, old, new)
        a = replace(a, '        super()._enqueue_text_event(event)\n',
            '        key = self._text_batch_key(event)\n'
            '        carrier = self._pending_text_batches.get(key, event)\n'
            '        super()._enqueue_text_event(event)\n'
            '        if not self._adopt_immediate_typing_carrier(event, carrier):\n'
            '            accepted_at = getattr(carrier, "_hermes_typing_accepted_at", asyncio.get_running_loop().time())\n'
            '            self._start_immediate_typing_receipt(carrier, accepted_at)\n'
            '            self._adopt_immediate_typing_carrier(event, carrier)\n')
        a = replace(a,
            '                *self._media_group_tasks.values(), *self._pending_photo_batch_tasks.values(), *self._pending_text_batch_tasks.values(),\n',
            '                *self._media_group_tasks.values(), *self._pending_photo_batch_tasks.values(), *self._pending_text_batch_tasks.values(),\n'
            '                *(getattr(event, "_typing_receipt_task", None) for event in self._pending_text_batches.values()),\n')
        start = a.index(OLD_SEND_SIGNATURE)
        end = a.index('    async def get_chat_info(', start)
        send = a[start:end].replace(OLD_SEND_SIGNATURE, NEW_SEND_SIGNATURE, 1)
        send = replace(send, '\n            return\n', '\n            return False\n')
        send = replace(send,
            '        async def _action(**kw) -> None:\n            await self._bot.send_chat_action(chat_id=normalize_telegram_chat_id(chat_id), action="typing", **kw)\n            self._telegram_typing_cooldown_until.pop(str(chat_id), None)\n',
            '        async def _action(**kw) -> bool:\n            sent = await self._bot.send_chat_action(chat_id=normalize_telegram_chat_id(chat_id), action="typing", **kw)\n            if sent is True:\n                self._telegram_typing_cooldown_until.pop(str(chat_id), None)\n                return True\n            return False\n')
        send = replace(send, '            await _action(message_thread_id=message_thread_id)\n', '            return await _action(message_thread_id=message_thread_id)\n')
        send = replace(send, '                    await _action()\n                    return\n', '                    return await _action()\n')
        send = send.rstrip() + '\n            return False\n\n'
        a = a[:start] + send + a[end:]
    if '# HERMES_NATIVE_TYPING_REFRESH_v5' not in b:
        old = '        return asyncio.create_task(self._keep_typing(event.source.chat_id, **kwargs))\n'
        new = '        # HERMES_NATIVE_TYPING_REFRESH_v5\n        async def refresh():\n            receipt = getattr(event, "_typing_receipt_task", None)\n            if receipt is not None:\n                try:\n                    await asyncio.shield(receipt)\n                except asyncio.CancelledError:\n                    raise\n                except Exception:\n                    pass\n            sent_at = getattr(event, "_typing_receipt_sent_at", None)\n            if sent_at is not None:\n                elapsed = asyncio.get_running_loop().time() - sent_at\n                await asyncio.sleep(max(0.0, 2.0 - elapsed))\n            await self._keep_typing(event.source.chat_id, **kwargs)\n        return asyncio.create_task(refresh())\n'
        b = replace(b, old, new)
        for old in ('        if both_photo or existing.media_urls or incoming_has_media:\n',
                    '        if merge_text and both_text:\n'):
            b = replace(b, old, old + '            from agent.runtime_performance_events import reconcile_gateway_events\n            reconcile_gateway_events(existing, event)\n')
        old = '        else:\n            if event.text:\n                state.event.text = _append_text(state.event.text, event.text)\n'
        b = replace(b, old, '        else:\n            from agent.runtime_performance_events import reconcile_gateway_events\n            reconcile_gateway_events(state.event, event)\n            if event.text:\n                state.event.text = _append_text(state.event.text, event.text)\n')
    proposed = {adapter: a, base: b}
    for path, source in proposed.items():
        compile(source, str(path), "exec")
    if check:
        return any(path.read_text() != source for path, source in proposed.items())
    changed = False
    for path, source in proposed.items():
        if path.read_text() != source:
            path.write_text(source)
            changed = True
    return changed


def patch_telegram_immediate_typing_receipt_v5(hermes_dir: Path) -> bool:
    if "def _start_typing_refresh(" in (hermes_dir / "gateway/platforms/base.py").read_text():
        return _patch_native_typing(hermes_dir)
    """Apply the receipt upgrade and return whether either runtime file changed."""
    adapter = hermes_dir / "plugins" / "platforms" / "telegram" / "adapter.py"
    base = hermes_dir / "gateway" / "platforms" / "base.py"
    adapter_source = adapter.read_text(encoding="utf-8")
    base_source = base.read_text(encoding="utf-8")
    patched_adapter = patch_pending_typing_cleanup(patch_adapter(adapter_source))
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


def patch_pending_typing_cleanup(source: str) -> str:
    """Let the native teardown owner cancel/await admitted batch receipts too."""
    anchor = """        for task in list(self._pending_text_batch_tasks.values()):
            collect(task)
"""
    replacement = anchor + """        for event in list(self._pending_text_batches.values()):
            collect(getattr(event, "_typing_receipt_task", None))
"""
    if replacement in source:
        return source
    if source.count(anchor) != 1:
        raise ValueError("Telegram pending typing cleanup anchor is not unique")
    return source.replace(anchor, replacement, 1)


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
        if "def _start_typing_refresh(" in base_source:
            _patch_native_typing(root, check=True)
        else:
            patch_pending_typing_cleanup(patch_adapter(adapter_source))
            patch_base(base_source)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 3
    if args.dry_run:
        print("DRY_RUN OK: immediate typing receipt applies cleanly")
        return 0
    try:
        changed = patch_telegram_immediate_typing_receipt_v5(root)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 3
    if not changed:
        print("ALREADY_PATCHED: markers present, no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
