#!/usr/bin/env python3
"""Install candidate-bound, content-free performance event carriers."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

MARKER = "HERMES_RUNTIME_PERFORMANCE_EVENTS_v2"
V1_MARKER = "HERMES_RUNTIME_PERFORMANCE_EVENTS_v1"
PAYLOAD = (
    Path(__file__).resolve().parent.parent
    / "payloads/runtime-performance-events-v1/agent/runtime_performance_events.py"
)


class PatchError(RuntimeError):
    pass


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if new in source:
        return source
    if source.count(old) != 1:
        raise PatchError(f"required unique anchor missing: {label}")
    return source.replace(old, new, 1)


def _patch_method(source: str, name: str, replacements: list[tuple[str, str]]) -> str:
    """Keep lifecycle edits inside their exact native owner, fail on drift."""
    matches = [node for node in ast.walk(ast.parse(source))
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(matches) != 1:
        raise PatchError(f"required unique method missing: {name}")
    node = matches[0]
    lines = source.splitlines(keepends=True)
    body = "".join(lines[node.lineno - 1:node.end_lineno])
    for old, new in replacements:
        # This later private component observation may sit between the existing
        # queued send and its original performance receipts. Prove that exact
        # prior receipt block after removing only our known interposed statement.
        if name == "_deliver_queued_first_response" and new in body.replace(
                "                    _record_response_delivery(_queued_send, trace=_queued_delivery_trace)\n", ""):
            continue
        body = _replace_once(body, old, new, f"{name}: {old[:80]!r}")
    cleanup_import = (
        "from agent.runtime_performance_events import retire_gateway_event as _retire_gateway_event\n"
    )
    if "_retire_gateway_event(" in body and cleanup_import not in body:
        first = node.body[0]
        is_docstring = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
        offset = (first.end_lineno + 1 if is_docstring else first.lineno) - node.lineno
        body_lines = body.splitlines(keepends=True)
        body_lines.insert(offset, " " * (node.col_offset + 4) + cleanup_import)
        body = "".join(body_lines)
    return "".join(lines[:node.lineno - 1]) + body + "".join(lines[node.end_lineno:])


def patch_event_lifecycle_base(source: str) -> str:
    source = _patch_method(source, "handle_message", [
        ("        if not self._message_handler:\n            return\n",
         "        if not self._message_handler:\n"
         "            _retire_gateway_event(event, discard=True)\n            return\n"),
        ("            return\n\n        # On-entry self-heal:",
         "            _retire_gateway_event(event, discard=True)\n            return\n\n        # On-entry self-heal:"),
        ("            if should_bypass_active_session(cmd):\n",
         "            if should_bypass_active_session(cmd):\n"
         "                _retire_gateway_event(event, discard=True)\n"),
        ("                if _has_text_clarify:\n",
         "                if _has_text_clarify:\n                    _retire_gateway_event(event, discard=True)\n"),
    ])
    source = _patch_method(source, "merge_pending_message_event", [
        ("    pending_messages[session_key] = event\n",
         "    if existing is not event:\n        _retire_gateway_event(existing, discard=True)\n"
         "    pending_messages[session_key] = event\n"),
    ])
    source = _patch_method(source, "_discard_text_debounce", [
        ("        if state is not None and state.task is not None and not state.task.done():\n",
         "        if state is not None:\n            _retire_gateway_event(state.event, discard=True)\n"
         "        if state is not None and state.task is not None and not state.task.done():\n"),
    ])
    for name, indent in (("_heal_stale_session_lock", "        "), ("cancel_session_processing", "            ")):
        source = _patch_method(source, name, [
            (indent + "self._pending_messages.pop(session_key, None)\n",
             indent + "_retire_gateway_event(self._pending_messages.pop(session_key, None), discard=True)\n"),
        ])
    source = _patch_method(source, "cancel_background_tasks", [
        ("        self._pending_messages.clear()\n",
         "        for pending_event in self._pending_messages.values():\n"
         "            _retire_gateway_event(pending_event, discard=True)\n        self._pending_messages.clear()\n"),
        ("        for state in list(self._text_debounce_store().values()):\n",
         "        for state in list(self._text_debounce_store().values()):\n"
         "            _retire_gateway_event(state.event, discard=True)\n"),
    ])
    return source


def patch_event_lifecycle_gateway(source: str) -> str:
    source = _patch_method(source, "_enqueue_fifo", [
        ("        if adapter is None:\n            return\n",
         "        if adapter is None:\n"
         "            _retire_gateway_event(queued_event, discard=True)\n            return\n"),
        ("        if pending_slot is None:\n            return\n",
         "        if pending_slot is None:\n"
         "            _retire_gateway_event(queued_event, discard=True)\n            return\n"),
    ])
    source = _patch_method(source, "_queue_or_replace_pending_event", [
        ("        if not adapter:\n            return\n",
         "        if not adapter:\n            _retire_gateway_event(event, discard=True)\n            return\n"),
        ("            return\n\n        self._enqueue_fifo(session_key, event, adapter)\n",
         "            _retire_gateway_event(event, discard=True)\n            return\n\n"
         "        self._enqueue_fifo(session_key, event, adapter)\n"),
    ])
    source = _patch_method(source, "_handle_active_session_busy_message", [
        ("            return True  # handled (silently dropped); do not fall through\n",
         "            _retire_gateway_event(event, discard=True)\n"
         "            return True  # handled (silently dropped); do not fall through\n"),
        ("            if not adapter:\n                return True\n",
         "            if not adapter:\n"
         "                _retire_gateway_event(event, discard=True)\n                return True\n"),
        ('            else:\n                message = f"⏳ Gateway is',
         '            else:\n                _retire_gateway_event(event, discard=True)\n'
         '                message = f"⏳ Gateway is'),
        ("                    _reply = await _approval_handler(event)\n",
         "                    _retire_gateway_event(event, discard=True)\n"
         "                    _reply = await _approval_handler(event)\n"),
        ("        if not steered and not redirected:\n",
         "        if steered or redirected:\n            _retire_gateway_event(event, discard=True)\n"
         "        if not steered and not redirected:\n"),
    ])
    source = _patch_method(source, "_clear_conversation_scope", [
        ("        if state is not None:\n            state.conversation.clear()\n",
         "        if state is not None:\n            for queued_event in state.conversation.queued_events:\n"
         "                _retire_gateway_event(queued_event, discard=True)\n            state.conversation.clear()\n"),
    ])
    source = _patch_method(source, "_interrupt_and_clear_session", [
        ("            adapter.get_pending_message(session_key)  # consume and discard\n",
         "            _retire_gateway_event(adapter.get_pending_message(session_key), discard=True)\n"),
    ])
    return source


def patch_base(source: str) -> str:
    background = '''    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        """Background task that actually processes the message."""
'''
    legacy_ingress = tuple(
        f'''        # [{marker}] One task/context owns one opaque turn identity. No
        # message, chat, user, or response body is passed to the journal.
        from agent.runtime_performance_events import begin_gateway_turn
        begin_gateway_turn(
            str(getattr(event.source.platform, "value", event.source.platform))
        )
'''
        for marker in (V1_MARKER, MARKER)
    )
    ingress_new = f'''        # [{MARKER}] One task/context owns one opaque turn identity. No
        # message, chat, user, or response body is passed to the journal.
        from agent.runtime_performance_events import (
            adopt_gateway_trace,
            begin_gateway_turn,
            current_gateway_trace,
        )
        _runtime_performance_platform = str(
            getattr(event.source.platform, "value", event.source.platform)
        )
        _runtime_performance_trace = getattr(
            event, "_hermes_runtime_performance_trace", None
        )
        if not adopt_gateway_trace(_runtime_performance_trace):
            begin_gateway_turn(_runtime_performance_platform)
            _runtime_performance_trace = current_gateway_trace()
            event._hermes_runtime_performance_trace = _runtime_performance_trace
            event._hermes_runtime_performance_turn_id = (
                _runtime_performance_trace.turn_id
            )
            event._hermes_runtime_performance_platform = _runtime_performance_platform
'''
    background_new = background.rstrip("\n") + "\n" + ingress_new
    legacy_matches = [item for item in legacy_ingress if source.count(item) == 1]
    if len(legacy_matches) > 1:
        raise PatchError("required unique anchor missing: legacy gateway turn ingress")
    if legacy_matches:
        source = source.replace(legacy_matches[0], ingress_new, 1)
    elif MARKER not in source:
        source = _replace_once(
            source, background, background_new, "gateway turn ingress"
        )

    typing = '''                        await asyncio.wait_for(
                            self.send_typing(chat_id, metadata=metadata),
                            timeout=_send_typing_timeout,
                        )
'''
    typing_shipped_v1 = '''                        await asyncio.wait_for(
                            self.send_typing(chat_id, metadata=metadata),
                            timeout=_send_typing_timeout,
                        )
                        from agent.runtime_performance_events import record_turn_event
                        record_turn_event(
                            "typing_indicator_started",
                            timing_semantics="platform_typing_call_returned",
                        )
'''
    typing_v1 = '''                        _typing_acknowledged = await asyncio.wait_for(
                            self.send_typing(chat_id, metadata=metadata),
                            timeout=_send_typing_timeout,
                        )
                        if _typing_acknowledged is not False:
                            from agent.runtime_performance_events import record_turn_event
                            record_turn_event(
                                "typing_indicator_started",
                                timing_semantics="platform_typing_call_returned",
                            )
'''
    typing_new = '''                        _typing_acknowledged = await asyncio.wait_for(
                            self.send_typing(chat_id, metadata=metadata),
                            timeout=_send_typing_timeout,
                        )
                        if _typing_acknowledged is True:
                            from agent.runtime_performance_events import record_turn_event
                            record_turn_event(
                                "typing_indicator_started",
                                timing_semantics="platform_typing_call_returned",
                            )
'''
    if typing_shipped_v1 in source:
        return source.replace(typing_shipped_v1, typing_new, 1)
    if typing_v1 in source:
        return source.replace(typing_v1, typing_new, 1)
    return _replace_once(source, typing, typing_new, "typing receipt")


def patch_turn_context(source: str) -> str:
    source = source.replace(V1_MARKER, MARKER)
    old = '''    source: Any = None
    _run_still_current: Callable[[], bool] = None  # type: ignore[assignment]
'''
    new = f'''    source: Any = None
    # [{MARKER}] Explicit per-turn carrier; never serialized or shared across
    # concurrent chats. The executor worker adopts it before model execution.
    runtime_performance_trace: Any = None
    runtime_performance_turn_id: str = ""
    runtime_performance_platform: str = ""
    _run_still_current: Callable[[], bool] = None  # type: ignore[assignment]
'''
    return _replace_once(source, old, new, "TurnContext performance carrier")


def patch_gateway_regression_test(source: str) -> str:
    marker = "test_runtime_performance_turn_survives_real_executor_seam"
    if marker in source:
        return source
    addition = f'''

def {marker}(monkeypatch, tmp_path):
    """A real _run_agent/TurnRunner handoff keeps one opaque turn identity."""
    import contextvars

    _setup_monkeypatches(monkeypatch, tmp_path)
    runner = _make_runner()
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_adapter_for_source",
        lambda self, source: None,
    )

    from agent import runtime_performance_events as performance

    rows = []
    monkeypatch.setattr(
        performance,
        "_append",
        lambda event: rows.append(dict(event)) or True,
    )
    payload = {{
        "baseline_system": {{"bytes": 0}},
        "context_injections": {{}},
        "estimated_request": {{"estimated_tokens": 1}},
        "mcp_schemas": {{"bytes": 0, "count": 0}},
        "selector_controls": {{
            "mcp_unclassified_count": 0,
            "skills_index_allowlist_configured": False,
        }},
        "skills": {{
            "duplicate_injections": 0,
            "index_bytes": 0,
            "selected_count": 0,
            "selected_names": [],
        }},
        "tool_results": {{"bytes": 0, "duplicate_count": 0, "max_bytes": 0}},
        "tool_schemas": {{"bytes": 0}},
    }}
    turn = [""]

    class _ReceiptAgent(_NoopAgent):
        def run_conversation(self, user_message, **kwargs):
            from agent.llm_attempt_receipts import (
                execute_main_attempt,
                finish_main_attempt,
                record_main_first_byte,
            )

            assert performance.current_turn_id() == turn[0]
            self._relay_pending_turn_id = "relay-overwrote-the-gateway-value"
            response = SimpleNamespace(
                id="provider-1",
                model="gpt-5.6-terra",
                usage=SimpleNamespace(
                    input_tokens=179,
                    output_tokens=1,
                    cache_read_tokens=17920,
                    cache_write_tokens=0,
                ),
            )
            assert execute_main_attempt(
                lambda: record_main_first_byte("logical-1") and response,
                task="conversation",
                provider="openai-codex",
                model="gpt-5.6-terra",
                base_url="https://example.invalid/v1",
                api_mode="codex_responses",
                logical_request_id="logical-1",
                session_id="session-1",
                turn_id=self._relay_pending_turn_id,
                platform="telegram",
                api_key=None,
                retry_count=0,
                is_fallback=False,
                fallback_cause=None,
                payload_breakdown=payload,
                defer_success=True,
            ) is response
            assert finish_main_attempt("logical-1", "success", response=response)
            return super().run_conversation(user_message, **kwargs)

    sys.modules["run_agent"].AIAgent = _ReceiptAgent

    async def _empty_context_executor(func, *args):
        loop = asyncio.get_running_loop()
        empty = contextvars.Context()
        return await loop.run_in_executor(None, empty.run, func, *args)

    runner._run_in_executor_with_context = _empty_context_executor

    async def _run():
        turn[0] = performance.begin_gateway_turn("telegram")
        return await runner._run_agent(
            message="private-body",
            context_prompt="",
            history=[],
            source=_make_voice_source(),
            session_id="session-1",
            session_key="agent:main:telegram:dm:12345",
            message_type=MessageType.TEXT,
        )

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result["final_response"] == "Hello from the agent."
    assert [row["event_name"] for row in rows] == [
        "inbound_received",
        "model_request_started",
        "first_model_byte",
        "model_request_complete",
        "response_complete",
    ]
    assert {{row["turn_id"] for row in rows}} == {{turn[0]}}
    assert "private-body" not in str(rows)
'''
    return source.rstrip() + addition


def patch_stream_consumer(source: str) -> str:
    source = source.replace(V1_MARKER, MARKER)
    track = '''    def _track_preview_id(self, message_id: Optional[str]) -> None:
        """Record a real preview message id for finalization cleanup."""
        if message_id and message_id != "__no_edit__":
            message_id = str(message_id)
'''
    track_new = f'''    def _track_preview_id(self, message_id: Optional[str]) -> None:
        """Record a real preview message id for finalization cleanup."""
        if message_id and message_id != "__no_edit__":
            # [{MARKER}] The first acknowledged preview is the first visible
            # response chunk. The helper de-duplicates subsequent edits.
            from agent.runtime_performance_events import record_turn_event
            record_turn_event(
                "first_visible_response_chunk",
                timing_semantics="platform_stream_ack_exact",
            )
            message_id = str(message_id)
'''
    source = _replace_once(source, track, track_new, "stream first-visible receipt")
    final = '''        self._delivered_final_text = ensure_closed_code_fences(
            self._clean_for_display(source)
        ).strip()
'''
    final_new = '''        self._delivered_final_text = ensure_closed_code_fences(
            self._clean_for_display(source)
        ).strip()
        from agent.runtime_performance_events import record_turn_event
        record_turn_event(
            "first_visible_response_chunk",
            timing_semantics="platform_stream_final_upper_bound",
        )
        record_turn_event(
            "response_sent",
            timing_semantics="platform_stream_final_ack_exact",
        )
'''
    return _replace_once(source, final, final_new, "stream final delivery receipt")


def patch_gateway_turn_context(source: str) -> str:
    agent_call = '''            result = agent.run_conversation(_api_run_message, **_conversation_kwargs)
'''
    agent_call_legacy = tuple(
        f'''            # [{marker}] Adopt the gateway task's opaque UUID at the existing
            # relay turn seam so provider receipts and delivery share one ID.
            from agent.runtime_performance_events import current_turn_id
            _runtime_performance_turn_id = current_turn_id()
            if _runtime_performance_turn_id:
                agent._relay_pending_turn_id = _runtime_performance_turn_id
            result = agent.run_conversation(_api_run_message, **_conversation_kwargs)
'''
        for marker in (V1_MARKER, MARKER)
    )
    legacy_matches = [item for item in agent_call_legacy if source.count(item) == 1]
    if len(legacy_matches) > 1:
        raise PatchError("required unique anchor missing: legacy agent turn carrier")
    if legacy_matches:
        source = source.replace(legacy_matches[0], agent_call, 1)
    source = source.replace(V1_MARKER, MARKER)
    run_sync = '''    def run_sync(self):
        ctx = self._ctx
'''
    run_sync_new = f'''    def run_sync(self):
        ctx = self._ctx
        # [{MARKER}] ContextVars are not the release contract across the real
        # gateway executor seam. Adopt the explicit per-turn carrier first.
        from agent.runtime_performance_events import adopt_gateway_trace
        adopt_gateway_trace(ctx.runtime_performance_trace)
'''
    source = _replace_once(
        source, run_sync, run_sync_new, "TurnRunner worker trace adoption"
    )

    turn_context = '''        turn_ctx = TurnContext(
            source=source,
'''
    turn_context_new = '''        from agent.runtime_performance_events import (
            current_gateway_trace,
            current_turn_id,
        )
        _runtime_performance_platform = str(
            getattr(source.platform, "value", source.platform)
        )
        turn_ctx = TurnContext(
            source=source,
            runtime_performance_trace=current_gateway_trace(),
            runtime_performance_turn_id=current_turn_id(),
            runtime_performance_platform=_runtime_performance_platform,
'''
    source = _replace_once(
        source, turn_context, turn_context_new, "TurnContext trace capture"
    )

    ready = '''            logger.info(
                "response ready: platform=%s chat=%s time=%.1fs api_calls=%d response=%d chars",
                _platform_name, source.chat_id or "unknown",
                _response_time, _api_calls, _resp_len,
            )
'''
    ready_new = '''            logger.info(
                "response ready: platform=%s chat=%s time=%.1fs api_calls=%d response=%d chars",
                _platform_name, source.chat_id or "unknown",
                _response_time, _api_calls, _resp_len,
            )
            if (
                not agent_result.get("failed")
                and not agent_result.get("interrupted")
                and agent_result.get("completed") is not False
            ):
                from agent.runtime_performance_events import record_turn_event
                record_turn_event(
                    "response_complete",
                    timing_semantics="gateway_response_complete_exact",
                )
'''
    return _replace_once(source, ready, ready_new, "gateway response completion")


def patch_gateway_run(source: str) -> str:
    source = patch_gateway_turn_context(patch_event_lifecycle_gateway(source))
    recursive = '''                followup_result = await self._run_agent(
                    message=next_message,
                    context_prompt=context_prompt,
                    history=updated_history,
                    source=next_source,
                    session_id=session_id,
                    session_key=next_session_key,
                    run_generation=run_generation,
                    _interrupt_depth=_interrupt_depth + 1,
                    event_message_id=next_message_id,
                    channel_prompt=next_channel_prompt,
                    message_type=next_message_type,
                )
'''
    source = _replace_once(
        source, recursive,
        "                try:\n" + "".join("    " + line for line in recursive.splitlines(keepends=True))
        + "                finally:\n                    _retire_gateway_event(pending_event)\n",
        "recursive turn carrier cleanup",
    )
    followup_ack = '''                _followup_adapter = self._adapter_for_source(source)
                if _followup_adapter:
                    try:
                        await _followup_adapter.send_typing(
                            source.chat_id,
                            metadata=_status_thread_metadata,
                        )
                    except Exception:
                        pass
'''
    followup_ack_new = '''                from agent.runtime_performance_events import (
                    adopt_gateway_trace, record_turn_event,
                )
                # Native FIFO recursion bypasses adapter ingress. Adopt the
                # actual pending event, never the previous turn's ambient ID.
                # Text-only synthetic continuations have no measured carrier.
                adopt_gateway_trace(getattr(pending_event, "_hermes_runtime_performance_trace", None))
                _followup_adapter = self._adapter_for_source(next_source)
                if _followup_adapter:
                    try:
                        _followup_ack = await _followup_adapter.send_typing(
                            next_source.chat_id,
                            metadata=_status_thread_metadata,
                        )
                        if _followup_ack is True:
                            record_turn_event(
                                "typing_indicator_started", timing_semantics="platform_typing_call_returned"
                            )
                    except Exception:
                        pass
'''
    composed_ack = followup_ack_new.replace(
        '                adopt_gateway_trace(getattr(pending_event, "_hermes_runtime_performance_trace", None))\n',
        '                from agent.runtime_performance_events import _begin_response_delivery, _finish_response_delivery\n'
        '                _finish_response_delivery(trace=getattr(turn_ctx, "runtime_performance_trace", None), aborted=bool(result.get("interrupted")))\n'
        '                adopt_gateway_trace(getattr(pending_event, "_hermes_runtime_performance_trace", None))\n'
        '                _begin_response_delivery()\n', 1)
    if composed_ack not in source:
        source = _replace_once(source, followup_ack, followup_ack_new, "recursive turn trace adoption")
    source = _patch_method(source, "_run_agent_inner", [
        ("                        merge_pending_message_event(adapter._pending_messages, session_key, pending_event)\n",
         "                        merge_pending_message_event(adapter._pending_messages, session_key, pending_event)\n"
         "                        _runtime_performance_pending_requeued = True\n"),
        ("        finally:\n            # Stop progress sender, interrupt monitor, and notification task\n",
         "        finally:\n            if not locals().get(\"_runtime_performance_pending_requeued\", False):\n"
         "                _retire_gateway_event(locals().get(\"pending_event\"))\n"
         "            # Stop progress sender, interrupt monitor, and notification task\n"),
        ("            result = result_holder[0]\n            adapter = self._adapter_for_source(source)\n",
         "            result = result_holder[0]\n"
         "            if (isinstance(response, dict) and not response.get(\"failed\")\n"
         "                    and not response.get(\"interrupted\") and response.get(\"completed\") is not False):\n"
         "                from agent.runtime_performance_events import record_turn_event\n"
         '                record_turn_event("response_complete", timing_semantics="gateway_response_complete_exact")\n'
         "            adapter = self._adapter_for_source(source)\n"),
        ("                            pending_event = None\n                            pending = None\n",
         "                            _retire_gateway_event(pending_event, discard=True)\n"
         "                            pending_event = None\n                            pending = None\n"),
        ("                    if next_message is None:\n                        return result\n",
         "                    if next_message is None:\n"
         "                        _retire_gateway_event(pending_event, discard=True)\n"
         "                        return result\n"),
        ('                            "Discarding stale goal continuation for session %s — goal is no longer active",\n'
         '                            session_key or "?",\n                        )\n'
         '                        return result\n',
         '                            "Discarding stale goal continuation for session %s — goal is no longer active",\n'
         '                            session_key or "?",\n                        )\n'
         '                        _retire_gateway_event(pending_event, discard=True)\n'
         '                        return result\n'),
    ])
    drain = '''            if self._draining and (pending_event or pending):
                logger.info(
                    "Discarding pending follow-up for session %s during gateway %s",
                    session_key or "?",
                    self._status_action_label(),
                )
'''
    persist = '''            pending_event, pending = await self._persist_pending_followup_for_drain(
                pending_event,
                pending,
                source,
                session_key,
            )
'''
    persist_021 = '''            pending_event, pending = await self._persist_pending_followup_for_drain(
                pending_event,
                pending,
                source,
                session_key,
                run_generation=run_generation,
            )
'''
    if drain in source:
        source = _replace_once(source, drain, drain +
                               "                _retire_gateway_event(pending_event, discard=True)\n",
                               "native drain carrier cleanup")
    elif persist in source:
        source = _replace_once(
            source, persist,
            "            _drain_carrier = pending_event\n" + persist
            + "            if _drain_carrier is not None and pending_event is None:\n"
            "                _retire_gateway_event(_drain_carrier, discard=True)\n",
            "persisted drain carrier cleanup",
        )
    else:
        source = _replace_once(
            source,
            persist_021,
            "            _drain_carrier = pending_event\n" + persist_021
            + "            if _drain_carrier is not None and pending_event is None:\n"
            "                _retire_gateway_event(_drain_carrier, discard=True)\n",
            "persisted drain carrier cleanup",
        )
    # Direct queued delivery bypasses BasePlatformAdapter's final-response
    # callback. Emit only after the real send/edit acknowledgement succeeds.
    source = _patch_method(source, "_deliver_queued_first_response", [
        ("                            _reconciled = True\n", "                            _reconciled = True\n"
         "                            from agent.runtime_performance_events import record_turn_event\n"
         '                            record_turn_event("first_visible_response_chunk", '
         'timing_semantics="platform_delivery_ack_exact")\n'
         '                            record_turn_event("response_sent", '
         'timing_semantics="platform_delivery_ack_exact")'
         '\n'),
        ("                    await adapter.send(\n", "                    _queued_send = await adapter.send(\n"),
        ("                        metadata=metadata,\n                    )\n",
         "                        metadata=metadata,\n                    )\n"
         '                    if getattr(_queued_send, "success", False):\n'
         "                        from agent.runtime_performance_events import record_turn_event\n"
         '                        record_turn_event("first_visible_response_chunk", '
         'timing_semantics="platform_delivery_ack_exact")\n'
         '                        record_turn_event("response_sent", timing_semantics="platform_delivery_ack_exact")'
         '\n'),
    ])
    return source


def patch_gateway_fifo_regression_test(source: str) -> str:
    marker = "test_runtime_performance_native_fifo_executor_lifecycle"
    if marker in source:
        return source
    return source.rstrip() + '''


def test_runtime_performance_native_fifo_executor_lifecycle(monkeypatch, tmp_path):
    """Two real native runner turns, not manual per-event trace adoption."""
    import contextvars
    from agent import runtime_performance_events as performance
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    _setup_monkeypatches(monkeypatch, tmp_path)
    rows, calls, ids = [], [], []
    monkeypatch.setattr(performance, "_append", lambda row: rows.append(dict(row)) or True)
    payload = {
        "baseline_system": {"bytes": 0}, "context_injections": {},
        "estimated_request": {"estimated_tokens": 1},
        "mcp_schemas": {"bytes": 0, "count": 0},
        "selector_controls": {"mcp_unclassified_count": 0, "skills_index_allowlist_configured": False},
        "skills": {"duplicate_injections": 0, "index_bytes": 0, "selected_count": 0, "selected_names": []},
        "tool_results": {"bytes": 0, "duplicate_count": 0, "max_bytes": 0},
        "tool_schemas": {"bytes": 0},
    }

    class Adapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

        async def connect(self):
            return True

        async def disconnect(self):
            return None

        async def send_typing(self, chat_id, metadata=None):
            return True

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True, message_id="local-ack")

        async def get_chat_info(self, chat_id):
            return {"id": chat_id, "type": "dm"}

    class ReceiptAgent(_NoopAgent):
        def run_conversation(self, user_message, **kwargs):
            from agent.llm_attempt_receipts import execute_main_attempt, finish_main_attempt, record_main_first_byte

            index = len(calls)
            turn_id = performance.current_turn_id()
            assert turn_id == ids[index]
            calls.append(turn_id)
            logical = "logical-" + str(index)
            response = SimpleNamespace(id="provider-" + str(index), model="gpt-5.6-terra",
                                       usage=SimpleNamespace(input_tokens=1, output_tokens=1))
            execute_main_attempt(
                lambda: record_main_first_byte(logical) and response,
                task="conversation", provider="openai-codex", model="gpt-5.6-terra",
                base_url="https://example.invalid/v1", api_mode="codex_responses",
                logical_request_id=logical, session_id="session-1", turn_id=turn_id,
                platform="telegram", api_key=None, retry_count=0, is_fallback=False,
                fallback_cause=None, payload_breakdown=payload, defer_success=True,
            )
            assert finish_main_attempt(logical, "success", response=response)
            return super().run_conversation(user_message, **kwargs)

    sys.modules["run_agent"].AIAgent = ReceiptAgent
    adapter = Adapter()
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    key = "agent:main:telegram:dm:12345"
    runner._session_key_for_source = lambda source: key
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="private-second")
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._deliver_media_from_response = AsyncMock()

    async def empty_context_executor(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, contextvars.Context().run, func, *args)

    runner._run_in_executor_with_context = empty_context_executor

    async def handle(event):
        result = await runner._run_agent(
            message=event.text, context_prompt="", history=[], source=event.source,
            session_id="session-1", session_key=key, message_type=MessageType.TEXT,
        )
        return result["final_response"]

    adapter._message_handler = handle

    async def exercise():
        source = _make_voice_source()
        first, second = (MessageEvent(text=text, source=source, message_type=MessageType.TEXT)
                         for text in ("private-first", "private-second"))
        for event in (first, second):
            ids.append(performance.begin_gateway_turn("telegram", defer_publication=True))
            event._hermes_runtime_performance_trace = performance.current_gateway_trace()
        second._typing_receipt_task = asyncio.create_task(asyncio.Event().wait())
        runner._queue_or_replace_pending_event(key, second)
        await adapter._process_message_background(first, key)
        await asyncio.sleep(0)
        assert second._typing_receipt_task.cancelled()
        assert not adapter._pending_messages

    asyncio.run(exercise())
    assert calls == ids and len(set(ids)) == 2
    for index, turn_id in enumerate(ids):
        own = [row for row in rows if row["turn_id"] == turn_id]
        assert {row["event_name"] for row in own} == performance._EVENT_NAMES
        assert len(own) == 8
        assert {row["logical_request_id"] for row in own if "logical_request_id" in row} == {"logical-" + str(index)}
    assert "private-first" not in str(rows) and "private-second" not in str(rows)
'''


def patch_complete_response_delivery(source: str, *, native: bool) -> str:
    """Instrument existing component results; never change platform return contracts."""
    imports = "from agent.runtime_performance_events import _record_response_delivery, _response_delivery_acks, current_gateway_trace\n"
    def edit(owner, pairs):
        nonlocal source
        source = _patch_method(source, owner, pairs)
    def import_owner(owner):
        nonlocal source
        node = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == owner)
        body = ast.get_source_segment(source, node)
        if imports.strip() in body:
            return
        first = node.body[0]
        position = first.end_lineno if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) else first.lineno - 1
        if owner == "_process_message_background" and len(node.body) > 1:
            entry = node.body[1]
            if isinstance(entry, ast.Expr) and isinstance(entry.value, ast.Call) and ast.unparse(entry.value.func) == "_telegram_tx.begin":
                position = entry.end_lineno
        lines = source.splitlines(keepends=True)
        lines.insert(position, " " * (node.col_offset + 4) + imports + ("        _component_delivery_trace = current_gateway_trace()\n" if owner != "_process_message_background" else ""))
        source = "".join(lines)
    begin = "        delivery_attempted = delivery_succeeded = False" if native else "        delivery_attempted = False"
    edit("_process_message_background", [
        (begin, "        from agent.runtime_performance_events import _begin_response_delivery, _finish_response_delivery, current_gateway_trace\n        _runtime_delivery_trace = _begin_response_delivery()\n" + begin),
        ("            nonlocal delivery_attempted, delivery_succeeded\n", "            nonlocal delivery_attempted, delivery_succeeded\n            _record_response_delivery(result, trace=_runtime_delivery_trace)\n"),
        ("            response = await self._message_handler(event)\n", "            response = await self._message_handler(event)\n            _runtime_delivery_trace = current_gateway_trace()\n"),
        (("            expected = asyncio.current_task() in self._expected_cancelled_tasks\n" if native else "\n            current_task = asyncio.current_task()\n"),
         ("            expected = asyncio.current_task() in self._expected_cancelled_tasks\n" if native else "\n            current_task = asyncio.current_task()\n") + "            _runtime_delivery_trace = current_gateway_trace()\n"),
        ('            await self._run_processing_hook("on_processing_complete", event, ProcessingOutcome.FAILURE)\n',
         '            await self._run_processing_hook("on_processing_complete", event, ProcessingOutcome.FAILURE)\n            _runtime_delivery_trace = current_gateway_trace()\n'),
        ("            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)\n", "            _finish_response_delivery(trace=_runtime_delivery_trace)\n            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)\n"),
    ])
    proc = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_message_background")
    finalizers = [n for n in proc.body if isinstance(n, ast.Try) and n.finalbody]
    if len(finalizers) != 1:
        raise PatchError("response completion finally owner drift")
    final_calls = [n for item in finalizers[0].finalbody for n in ast.walk(item)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_finish_response_delivery"]
    if not final_calls:
        if "\n        finally:\n            try:\n" in ast.get_source_segment(source, proc):
            before = "\n        finally:\n            try:\n"
            after = before + "                _finish_response_delivery(trace=_runtime_delivery_trace, aborted=True)\n"
        else:
            before = "\n        finally:\n"
            after = before + "            _finish_response_delivery(trace=_runtime_delivery_trace, aborted=True)\n"
        edit("_process_message_background", [(before, after)])
    elif len(final_calls) != 1 or ast.unparse(final_calls[0]) != "_finish_response_delivery(trace=_runtime_delivery_trace, aborted=True)":
        raise PatchError("response completion abort hook drift")
    import_owner("_process_message_background")
    # The base image sender has a concrete SendResult per image. Overrides that
    # return None without exposing acknowledgements cannot prove full delivery.
    import_owner("send_multiple_images")
    edit("send_multiple_images", [
        ("                if not img_result.success:\n", "                _record_response_delivery(img_result, trace=_component_delivery_trace)\n                if not img_result.success:\n"),
        ("            except Exception as img_err:\n", "            except Exception as img_err:\n                _record_response_delivery(None, trace=_component_delivery_trace)\n"),
    ])
    owners = ("_send_image_batch", "_deliver_media_attachments") if native else ("_process_message_background",)
    for owner in owners:
        import_owner(owner)
    if native:
        tts_owner = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_play_tts_file")
        # Pristine d363 lets errors reach the outer abort boundary. The voice
        # carrier adds a caught fallback, which must expose its failed attempt.
        if any(isinstance(n, ast.Try) for n in ast.walk(tts_owner)):
            import_owner("_play_tts_file")
            edit("_play_tts_file", [
                ("        except Exception:\n", "        except Exception:\n            _record_response_delivery(None, trace=_component_delivery_trace)\n"),
            ])
        edit("_send_image_batch", [
            ("        try:\n            await self.send_multiple_images(\n", "        try:\n            _image_ack_start = _response_delivery_acks(trace=_component_delivery_trace)\n            await self.send_multiple_images(\n"),
            ("images=images, metadata=metadata, human_delay=human_delay)\n", "images=images, metadata=metadata, human_delay=human_delay)\n            if _response_delivery_acks(trace=_component_delivery_trace) - _image_ack_start < len(images):\n                _record_response_delivery(None, trace=_component_delivery_trace)\n"),
            ("        except Exception as batch_err:\n", "        except Exception as batch_err:\n            _record_response_delivery(None, trace=_component_delivery_trace)\n"),
        ])
        edit("_deliver_media_attachments", [
            ("            if not result.success:\n", "            _record_response_delivery(result, trace=_component_delivery_trace)\n            if not result.success:\n"),
            ("            except Exception as err:\n", "            except Exception as err:\n                _record_response_delivery(None, trace=_component_delivery_trace)\n"),
        ])
    else:
        edit("_process_message_background", [
            ("                    except Exception as tts_send_err:\n", "                    except Exception as tts_send_err:\n                        _record_response_delivery(None, trace=_runtime_delivery_trace)\n"),
            ("                        if not media_result.success:\n", "                        _record_response_delivery(media_result, trace=_runtime_delivery_trace)\n                        if not media_result.success:\n"),
            ("                        if not file_result.success:\n", "                        _record_response_delivery(file_result, trace=_runtime_delivery_trace)\n                        if not file_result.success:\n"),
            ("                    except Exception as media_err:\n", "                    except Exception as media_err:\n                        _record_response_delivery(None, trace=_runtime_delivery_trace)\n"),
            ("                    except Exception as file_err:\n", "                    except Exception as file_err:\n                        _record_response_delivery(None, trace=_runtime_delivery_trace)\n"),
        ])
        # Two exact existing image-batch statements (URL images and local images).
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_message_background")
        body = ast.get_source_segment(source, node)
        calls = [n for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
                 and isinstance(n.value.value, ast.Call) and isinstance(n.value.value.func, ast.Attribute)
                 and n.value.value.func.attr == "send_multiple_images"]
        if len(calls) != 2:
            raise PatchError("legacy response image owners drifted")
        for call in calls:
            fragment = ast.get_source_segment(body, call)
            prefix = " " * call.col_offset
            keyword = next(k.value for k in call.value.value.keywords if k.arg == "images")
            images = ast.get_source_segment(body, keyword)
            old = prefix + fragment + "\n"
            new = prefix + "_image_ack_start = _response_delivery_acks(trace=_runtime_delivery_trace)\n" + old + prefix + "if _response_delivery_acks(trace=_runtime_delivery_trace) - _image_ack_start < len(" + images + "):\n" + prefix + "    _record_response_delivery(None, trace=_runtime_delivery_trace)\n"
            edit("_process_message_background", [(old, new)])
        # Both batches preserve their existing best-effort error handling.
        source = source.replace("                    except Exception as batch_err:\n                        logger.warning", "                    except Exception as batch_err:\n                        _record_response_delivery(None, trace=_runtime_delivery_trace)\n                        logger.warning")
    return source


def patch_fifo_response_delivery(source: str, *, native: bool) -> str:
    owner = "_run_agent_queued_followup" if native else "_run_agent_inner"
    indent = "            " if native else "                "
    adoption = indent + 'adopt_gateway_trace(getattr(pending_event, "_hermes_runtime_performance_trace", None))\n'
    replacement = (indent + "from agent.runtime_performance_events import _begin_response_delivery, _finish_response_delivery\n"
                   + indent + '_finish_response_delivery(trace=getattr(turn_ctx, "runtime_performance_trace", None), aborted=bool(result.get("interrupted")))\n'
                   + adoption + indent + "_begin_response_delivery()\n")
    return _patch_method(source, owner, [(adoption, replacement)])


def patch_poststream_delivery(source: str, native: bool) -> str:
    """Observe required explicit post-stream media without changing its delivery API."""
    marker = "# HERMES_RUNTIME_PERFORMANCE_POSTSTREAM_DELIVERY_v1"
    owner = "_deliver_media_from_response"
    nodes = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.AsyncFunctionDef) and n.name == owner]
    if len(nodes) != 1:
        raise RuntimeError("post-stream media owner drift")
    body = ast.get_source_segment(source, nodes[0])
    if marker in body:
        tree = ast.parse(body)
        notes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_record_response_delivery"]
        sends = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in {"send_multiple_images", "send_voice", "send_video", "send_document"}]
        counts = {"_media_delivery_trace = current_gateway_trace()": 1,
                  "_response_delivery_acks(trace=_media_delivery_trace)": 2,
                  "_record_response_delivery(_media_send_result, trace=_media_delivery_trace)": 3,
                  "_record_response_delivery(None, trace=_media_delivery_trace)": 4}
        if (body.count(marker) != 1 or len(notes) != 7
                or sorted(sends) != ["send_document", "send_multiple_images", "send_video", "send_voice"]
                or any(body.count(fragment) != count for fragment, count in counts.items())):
            raise PatchError("post-stream delivery proof drift")
        return source
    replacements = []
    anchor = "        from urllib.parse import quote as _quote\n"
    replacements.append((anchor, anchor + "        " + marker + "\n"
        "        from agent.runtime_performance_events import _record_response_delivery, _response_delivery_acks, current_gateway_trace\n"
        "        _media_delivery_trace = current_gateway_trace()\n"))
    calls = [n for n in ast.walk(ast.parse(body))
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
             and isinstance(n.value.value, ast.Call)
             and isinstance(n.value.value.func, ast.Attribute)
             and n.value.value.func.attr in {"send_multiple_images", "send_voice", "send_video", "send_document"}]
    if sorted(n.value.value.func.attr for n in calls) != ["send_document", "send_multiple_images", "send_video", "send_voice"]:
        raise RuntimeError("post-stream media send owner drift")
    for call in calls:
        fragment = ast.get_source_segment(body, call)
        indent = " " * call.col_offset
        old = indent + fragment + "\n"
        if call.value.value.func.attr == "send_multiple_images":
            new = indent + "_media_image_ack_start = _response_delivery_acks(trace=_media_delivery_trace)\n" + old
            new += indent + "if _response_delivery_acks(trace=_media_delivery_trace) - _media_image_ack_start < len(images):\n"
            new += indent + "    _record_response_delivery(None, trace=_media_delivery_trace)\n"
        else:
            new = indent + "_media_send_result = " + fragment + "\n"
            new += indent + "_record_response_delivery(_media_send_result, trace=_media_delivery_trace)\n"
        replacements.append((old, new))
    # Inner batch and per-file failures are intentionally best-effort native paths.
    for log_text in ("Post-stream image batch delivery failed", "Post-stream media delivery failed"):
        anchor = '                except Exception as e:\n                    logger.warning("[%s] ' + log_text + ': %s", adapter.name, e)\n'
        replacements.append((anchor, anchor.replace("                    logger.warning", "                    _record_response_delivery(None, trace=_media_delivery_trace)\n                    logger.warning", 1)))
    if native:
        anchor = '        with _log_suppressed(logging.WARNING, "Post-stream media extraction failed: %s"):\n'
        replacements.append((anchor, "        try:  # Observe suppressed extraction failures.\n"))
        # AST extraction omits trailing whitespace; append the equivalent logging catch
        # after the existing final per-file exception handler.
        last = '                    logger.warning("[%s] Post-stream media delivery failed: %s", adapter.name, e)'
        replacements.append((last, last + '\n        except Exception as e:\n            _record_response_delivery(None, trace=_media_delivery_trace)\n            logger.warning("Post-stream media extraction failed: %s", e)'))
    else:
        anchor = '        except Exception as e:\n            logger.warning("Post-stream media extraction failed: %s", e)'
        replacements.append((anchor, anchor.replace("            logger.warning", "            _record_response_delivery(None, trace=_media_delivery_trace)\n            logger.warning", 1)))
    return _patch_method(source, owner, replacements)


def patch_queued_response_delivery(source: str, *, native: bool) -> str:
    owner = "_deliver_queued_first_response"
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == owner)
    body = ast.get_source_segment(source, node)
    if "_queued_delivery_trace = current_gateway_trace()" not in body:
        first = node.body[0]
        lines = source.splitlines(keepends=True)
        position = first.end_lineno if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) else first.lineno - 1
        lines.insert(position, "        from agent.runtime_performance_events import _record_response_delivery, current_gateway_trace\n        _queued_delivery_trace = current_gateway_trace()\n")
        source = "".join(lines)
    result = "sent" if native else "_queued_send"
    source = _patch_method(source, owner, [
        ('                    if getattr(' + result + ', "success", False):\n',
         '                    _record_response_delivery(' + result + ', trace=_queued_delivery_trace)\n                    if getattr(' + result + ', "success", False):\n'),
        ('                        if getattr(_edit_res, "success", False):\n',
         '                        if getattr(_edit_res, "success", False):\n                            _record_response_delivery(_edit_res, trace=_queued_delivery_trace)\n'),
    ])
    return source


def patch_queued_response_failure(source: str, *, native: bool) -> str:
    owner = "_run_agent_deliver_first_response" if native else "_run_agent_inner"
    indent = "                " if native else "                            "
    call = indent + "await self._deliver_queued_first_response(\n"
    replacement = (indent + "from agent.runtime_performance_events import _record_response_delivery, current_gateway_trace\n"
                   + indent + "_prior_delivery_trace = current_gateway_trace()\n" + call)
    log = indent + 'logger.warning("Failed to send first response before queued message: %s", e)'
    return _patch_method(source, owner, [(call, replacement),
        (log, indent + "_record_response_delivery(None, trace=_prior_delivery_trace)\n" + log)])


def _patch_native_performance(root: Path) -> bool:
    """Attach reviewed carriers to d363's decomposed lifecycle owners."""
    paths = {name: root / ('gateway/' + name) for name in (
        'platforms/base.py', 'run_busy.py', 'run_agent_cache.py', 'run_turn.py',
        'run_turn_runner.py', 'run_notifications.py', 'stream_consumer.py',
        'stream_consumer_transport.py', 'turn_context.py')}
    sources = {name: path.read_text() for name, path in paths.items()}
    def method(name, owner, pairs):
        # Another carrier may insert its receipt between our existing lines or
        # wrap native cleanup in a try/finally. Verify our concrete ordered
        # statements, not a marker or one contiguous indentation-sensitive blob.
        tree = ast.parse(sources[name])
        nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == owner]
        if len(nodes) != 1:
            raise PatchError(f"native performance owner drift: {owner}")
        body = ast.get_source_segment(sources[name], nodes[0]) or ""
        def installed(fragment):
            available = iter(line.strip() for line in body.splitlines() if line.strip())
            return all(any(line.strip() in candidate for candidate in available)
                       for line in fragment.splitlines() if line.strip())
        pending = [(old, new) for old, new in pairs if not installed(new)]
        if pending:
            sources[name] = _patch_method(sources[name], owner, pending)
    def replace(name, old, new, label):
        sources[name] = _replace_once(sources[name], old, new, label)
    method('platforms/base.py', 'handle_message', [
        ('        if not self._message_handler:\n            return\n', '        if not self._message_handler:\n            _retire_gateway_event(event, discard=True)\n            return\n'),
        ('            return\n        # On-entry self-heal:', '            _retire_gateway_event(event, discard=True)\n            return\n        # On-entry self-heal:')])
    method('platforms/base.py', '_handle_message_while_active', [
        ("                # /stop, /new, /reset: cancel + response + drain; other bypasses don't cancel.\n", "                _retire_gateway_event(event, discard=True)\n                # /stop, /new, /reset: cancel + response + drain; other bypasses don't cancel.\n"),
        ('            if _has_text_clarify:\n', '            if _has_text_clarify:\n                _retire_gateway_event(event, discard=True)\n')])
    method('platforms/base.py', 'merge_pending_message_event', [
        ('    pending_messages[session_key] = event\n', '    if existing is not event:\n        _retire_gateway_event(existing, discard=True)\n    pending_messages[session_key] = event\n')])
    method('platforms/base.py', '_discard_text_debounce', [
        ('        if state is not None:\n', '        if state is not None:\n            _retire_gateway_event(state.event, discard=True)\n')])
    for owner, indent in [('_heal_stale_session_lock','        '),('cancel_session_processing','            ')]:
        method('platforms/base.py', owner, [(indent+'self._pending_messages.pop(session_key, None)\n',indent+'_retire_gateway_event(self._pending_messages.pop(session_key, None), discard=True)\n')])
    method('platforms/base.py','cancel_background_tasks',[
        ('        for state in self._text_debounce_store().values():\n','        for pending_event in self._pending_messages.values():\n            _retire_gateway_event(pending_event, discard=True)\n        for state in self._text_debounce_store().values():\n            _retire_gateway_event(state.event, discard=True)\n')])
    ingress = '''        # HERMES_RUNTIME_PERFORMANCE_EVENTS_v2 native ingress
        from agent.runtime_performance_events import adopt_gateway_trace, begin_gateway_turn, current_gateway_trace
        platform = str(getattr(event.source.platform, "value", event.source.platform))
        if not adopt_gateway_trace(getattr(event, "_hermes_runtime_performance_trace", None)):
            begin_gateway_turn(platform)
            event._hermes_runtime_performance_trace = current_gateway_trace()
            event._hermes_runtime_performance_turn_id = current_gateway_trace().turn_id
            event._hermes_runtime_performance_platform = platform
'''
    cleanup_anchor = '        finally:\n            # Stop typing BEFORE'
    if '        finally:\n            try:\n                # Stop typing BEFORE' in sources['platforms/base.py']:
        cleanup_anchor = '        finally:\n            try:\n                # Stop typing BEFORE'
    method('platforms/base.py','_process_message_background',[
        ('        delivery_attempted = delivery_succeeded = False',ingress+'        delivery_attempted = delivery_succeeded = False'),
        ('                delivery_succeeded = delivery_succeeded or bool(getattr(result, "success", False))\n','                delivery_succeeded = delivery_succeeded or bool(getattr(result, "success", False))\n                if getattr(result, "success", False):\n                    from agent.runtime_performance_events import record_turn_event\n                    record_turn_event("first_visible_response_chunk", timing_semantics="platform_delivery_ack_exact")\n                    record_turn_event("response_sent", timing_semantics="platform_delivery_ack_exact")\n'),
        (cleanup_anchor, cleanup_anchor.replace('        finally:\n', '        finally:\n            _retire_gateway_event(event)\n', 1))])
    method('platforms/base.py','_keep_typing',[
        ('                        await asyncio.wait_for(self.send_typing(chat_id, metadata=metadata),\n                                               timeout=_send_typing_timeout)\n',
         '                        acknowledged = await asyncio.wait_for(self.send_typing(chat_id, metadata=metadata),\n                                               timeout=_send_typing_timeout)\n                        if acknowledged is True:\n                            from agent.runtime_performance_events import record_turn_event\n                            record_turn_event("typing_indicator_started", timing_semantics="platform_typing_call_returned")\n')])
    method('run_busy.py','_enqueue_fifo',[(
        '        if pending_slot is None:\n            return\n','        if pending_slot is None:\n            _retire_gateway_event(queued_event, discard=True)\n            return\n')])
    method('run_busy.py','_queue_or_replace_pending_event',[
        ('        if not adapter:\n            return\n','        if not adapter:\n            _retire_gateway_event(event, discard=True)\n            return\n'),
        ('            return\n\n        self._enqueue_fifo(session_key, event, adapter)\n','            _retire_gateway_event(event, discard=True)\n            return\n\n        self._enqueue_fifo(session_key, event, adapter)\n')])
    method('run_busy.py','_handle_active_session_busy_message',[
        ('            return True  # handled (silently dropped); do not fall through\n','            _retire_gateway_event(event, discard=True)\n            return True  # handled (silently dropped); do not fall through\n'),
        ('        if not _steer.steered and not redirected:\n','        if _steer.steered or redirected:\n            _retire_gateway_event(event, discard=True)\n        if not _steer.steered and not redirected:\n')])
    method('run_busy.py','_send_busy_drain_notice',[
        ('        if not adapter:\n            return\n','        if not adapter:\n            _retire_gateway_event(event, discard=True)\n            return\n'),
        ('        else:\n            message = f"⏳ Gateway is','        else:\n            _retire_gateway_event(event, discard=True)\n            message = f"⏳ Gateway is')])
    method('run_busy.py','_route_plaintext_approval_while_busy',[
        ('                    _reply = await _approval_handler(event)\n','                    _retire_gateway_event(event, discard=True)\n                    _reply = await _approval_handler(event)\n')])
    method('run_agent_cache.py','_clear_conversation_scope',[
        ('        if state is not None:\n            state.conversation.clear()\n','        if state is not None:\n            for queued_event in state.conversation.queued_events:\n                _retire_gateway_event(queued_event, discard=True)\n            state.conversation.clear()\n')])
    method('run_agent_cache.py','_interrupt_and_clear_session',[
        ('            adapter.get_pending_message(session_key)  # consume and discard\n','            _retire_gateway_event(adapter.get_pending_message(session_key), discard=True)\n')])
    sources['turn_context.py'] = patch_turn_context(sources['turn_context.py'])
    method('run_turn_runner.py','run_sync',[
        ('        ctx = self._ctx\n','        ctx = self._ctx\n        from agent.runtime_performance_events import adopt_gateway_trace\n        adopt_gateway_trace(ctx.runtime_performance_trace)\n')])
    replace('run_turn.py','        turn_ctx = TurnContext(\n',
        '        from agent.runtime_performance_events import current_gateway_trace, current_turn_id\n        turn_ctx = TurnContext(\n            runtime_performance_trace=current_gateway_trace(),\n            runtime_performance_turn_id=current_turn_id(),\n            runtime_performance_platform=str(getattr(source.platform, "value", source.platform)),\n','native turn context capture')
    # Record completion before a queued continuation switches ambient trace.
    replace('run_turn.py','            result = turn_ctx.result_holder[0]\n            adapter = self._adapter_for_source(source)\n',
        '            result = turn_ctx.result_holder[0]\n            if isinstance(response, dict) and not response.get("failed") and not response.get("interrupted") and response.get("completed") is not False:\n                from agent.runtime_performance_events import record_turn_event\n                record_turn_event("response_complete", timing_semantics="gateway_response_complete_exact")\n            adapter = self._adapter_for_source(source)\n','native model completion')
    method('run_turn.py','_run_agent_drain_pending',[
        ('                        pending_event = None\n','                        _retire_gateway_event(pending_event, discard=True)\n                        pending_event = None\n'),
        ('            pending_event = None\n            pending = None\n        return pending_event, pending\n','            _retire_gateway_event(pending_event, discard=True)\n            pending_event = None\n            pending = None\n        return pending_event, pending\n')])
    if '# HERMES_RUNTIME_PERFORMANCE_EVENTS_v2 queued cleanup' not in sources['run_turn.py']:
        method('run_turn.py','_run_agent_queued_followup',[
            ('                merge_pending_message_event(adapter._pending_messages, session_key, pending_event)\n','                merge_pending_message_event(adapter._pending_messages, session_key, pending_event)\n                _performance_requeued = True\n'),
            ('        if _clear_adapter:\n            with suppress(Exception):\n                await _clear_adapter.send_typing(source.chat_id, metadata=_status_thread_metadata)\n',
             '        from agent.runtime_performance_events import adopt_gateway_trace, record_turn_event\n        adopt_gateway_trace(getattr(pending_event, "_hermes_runtime_performance_trace", None))\n        _followup_adapter = self._adapter_for_source(next_source)\n        if _followup_adapter:\n            with suppress(Exception):\n                if await _followup_adapter.send_typing(next_source.chat_id, metadata=_status_thread_metadata) is True:\n                    record_turn_event("typing_indicator_started", timing_semantics="platform_typing_call_returned")\n')])
    # A finally around the native owner handles every early return/exception,
    # except its explicit recursion-cap requeue, whose event must remain live.
    source=sources['run_turn.py']
    if '# HERMES_RUNTIME_PERFORMANCE_EVENTS_v2 queued cleanup' not in source:
        node=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_agent_queued_followup')
        lines=source.splitlines(keepends=True);start=node.body[0].end_lineno
        body=''.join(lines[start:node.end_lineno])
        wrapped=('        # HERMES_RUNTIME_PERFORMANCE_EVENTS_v2 queued cleanup\n'
                 '        from agent.runtime_performance_events import retire_gateway_event as _retire_gateway_event\n'
                 '        _performance_requeued = False\n        try:\n'
                 + ''.join('    '+line if line.strip() else line for line in body.splitlines(keepends=True))
                 + '        finally:\n            if not _performance_requeued:\n                _retire_gateway_event(pending_event)\n')
        sources['run_turn.py']=''.join(lines[:start])+wrapped+''.join(lines[node.end_lineno:])
    queued_node = next(n for n in ast.walk(ast.parse(sources['run_turn.py']))
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run_agent_queued_followup')
    queued_body = ast.get_source_segment(sources['run_turn.py'], queued_node)
    for required in (
        '_performance_requeued = False', '_performance_requeued = True',
        'if not _performance_requeued:', '_retire_gateway_event(pending_event)',
        'adopt_gateway_trace(getattr(pending_event, "_hermes_runtime_performance_trace", None))',
        'await _followup_adapter.send_typing(next_source.chat_id, metadata=_status_thread_metadata) is True',
    ):
        if required not in queued_body:
            raise PatchError("native queued performance lifecycle drift: " + required)
    method('stream_consumer_transport.py','_track_preview_id',[
        ('        if message_id and message_id != "__no_edit__":\n','        if message_id and message_id != "__no_edit__":\n            from agent.runtime_performance_events import record_turn_event\n            record_turn_event("first_visible_response_chunk", timing_semantics="platform_stream_ack_exact")\n')])
    method('stream_consumer.py','_record_turn_final_payload',[
        ('        self._delivered_final_text = self._display_payload(text)\n','        self._delivered_final_text = self._display_payload(text)\n        from agent.runtime_performance_events import record_turn_event\n        record_turn_event("first_visible_response_chunk", timing_semantics="platform_stream_final_upper_bound")\n        record_turn_event("response_sent", timing_semantics="platform_stream_final_ack_exact")\n')])
    method('run_notifications.py','_deliver_queued_first_response',[
        ('                            _reconciled = True\n','                            _reconciled = True\n                            from agent.runtime_performance_events import record_turn_event\n                            record_turn_event("first_visible_response_chunk", timing_semantics="platform_delivery_ack_exact")\n                            record_turn_event("response_sent", timing_semantics="platform_delivery_ack_exact")\n'),
        ('                    await adapter.send(source.chat_id, text_content, metadata=metadata)\n','                    sent = await adapter.send(source.chat_id, text_content, metadata=metadata)\n                    if getattr(sent, "success", False):\n                        from agent.runtime_performance_events import record_turn_event\n                        record_turn_event("first_visible_response_chunk", timing_semantics="platform_delivery_ack_exact")\n                        record_turn_event("response_sent", timing_semantics="platform_delivery_ack_exact")\n')])
    sources["run_notifications.py"] = patch_queued_response_delivery(patch_poststream_delivery(sources["run_notifications.py"], native=True), native=True)
    sources["run_turn.py"] = patch_queued_response_failure(sources["run_turn.py"], native=True)
    sources["run_turn.py"] = patch_fifo_response_delivery(sources["run_turn.py"], native=True)
    sources["platforms/base.py"] = patch_complete_response_delivery(sources["platforms/base.py"], native=True)
    proposed={paths[name]:source for name,source in sources.items()}
    proposed[root/'agent/runtime_performance_events.py']=PAYLOAD.read_text()
    regression = root/'tests/gateway/test_streaming_tts_gateway_regression.py'
    proposed[regression] = patch_gateway_fifo_regression_test(patch_gateway_regression_test(regression.read_text()))
    for path,source in proposed.items():compile(source,str(path),'exec')
    changed=False
    for path,source in proposed.items():
        if not path.is_file() or path.read_text()!=source:
            path.write_text(source);changed=True
    return changed


def patch_runtime_performance_events_v1(hermes_dir: Path) -> bool:
    if (Path(hermes_dir) / "gateway/run_turn.py").is_file():
        return _patch_native_performance(Path(hermes_dir))
    root = Path(hermes_dir)
    helper = root / "agent/runtime_performance_events.py"
    targets = {
        root / "gateway/platforms/base.py": lambda source: patch_complete_response_delivery(patch_event_lifecycle_base(patch_base(source)), native=False),
        root / "gateway/run.py": lambda source: patch_queued_response_failure(patch_queued_response_delivery(patch_poststream_delivery(patch_fifo_response_delivery(patch_gateway_run(source), native=False), native=False), native=False), native=False),
        root / "gateway/stream_consumer.py": patch_stream_consumer,
        root / "gateway/turn_context.py": patch_turn_context,
        root
        / "tests/gateway/test_streaming_tts_gateway_regression.py": (
            lambda source: patch_gateway_fifo_regression_test(patch_gateway_regression_test(source))
        ),
    }
    if not PAYLOAD.is_file() or not all(path.is_file() for path in targets):
        raise PatchError("required runtime performance source is missing")
    changed = False
    helper_source = PAYLOAD.read_text(encoding="utf-8")
    if not helper.is_file() or helper.read_text(encoding="utf-8") != helper_source:
        helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PAYLOAD, helper)
        changed = True
    for path, patcher in targets.items():
        before = path.read_text(encoding="utf-8")
        after = patcher(before)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed = True
    return changed
