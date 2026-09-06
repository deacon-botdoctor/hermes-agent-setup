#!/usr/bin/env python3
"""Harden Telegram checkpoint cadence, privacy, and message reuse."""

from __future__ import annotations

import shutil
from pathlib import Path

V1_MARKER = "HERMES_TELEGRAM_MODEL_COMMENTARY_CHECKPOINTS_v1"
MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2"
REVISION_MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2_r6"

MARKER_ANCHOR = f"# {V1_MARKER}\n"
MARKER_REPLACEMENT = f"# {V1_MARKER}\n# {MARKER}\n# {REVISION_MARKER}\n"

START_ANCHOR = "        _notify_start = time.time()\n"
START_REPLACEMENT = """        # Schedule checkpoints from a monotonic origin. Wall-clock movement and
        # early wake-ups cannot turn a configured milestone into a near miss.
        _notify_start = time.monotonic()
"""

SLEEP_ANCHOR = """            while True:
                await asyncio.sleep(_NOTIFY_INTERVAL)
"""
SLEEP_REPLACEMENT = """            # The counter belongs to this coroutine. Keeping it local avoids a
            # closure write that would fail before the first agent turn completes.
            _notify_tick = 0
            while True:
                _notify_tick += 1
                _notify_deadline = _notify_start + (_notify_tick * _NOTIFY_INTERVAL)
                await asyncio.sleep(max(0.0, _notify_deadline - time.monotonic()))
"""

IMMEDIATE_SLEEP_ANCHOR = """            while True:
                _is_immediate_heartbeat = _first_heartbeat and _progress_on_typing
                if not _is_immediate_heartbeat:
                    if _NOTIFY_INTERVAL is None:
                        break
                    await asyncio.sleep(_NOTIFY_INTERVAL)
"""
IMMEDIATE_TICK_ANCHOR = "                _is_immediate_heartbeat = _first_heartbeat and _progress_on_typing\n"
IMMEDIATE_TICK_REPLACEMENT = "                _is_immediate_heartbeat = _first_heartbeat and _progress_on_typing and source.platform != Platform.TELEGRAM\n"

IMMEDIATE_SLEEP_REPLACEMENT = """            # Telegram waits for the first custom checkpoint; typing remains a
            # separate indicator. Other platforms retain their typing-start bubble.
            _notify_tick = 0
            while True:
                _is_immediate_heartbeat = _first_heartbeat and _progress_on_typing and source.platform != Platform.TELEGRAM
                if not _is_immediate_heartbeat:
                    if _NOTIFY_INTERVAL is None:
                        break
                    _notify_tick += 1
                    _notify_deadline = _notify_start + (_notify_tick * _NOTIFY_INTERVAL)
                    await asyncio.sleep(max(0.0, _notify_deadline - time.monotonic()))
"""

ELAPSED_ANCHOR = "                _elapsed_mins = int((time.time() - _notify_start) // 60)\n"
ELAPSED_REPLACEMENT = """                _elapsed_mins = _telegram_checkpoint_minutes_v2(
                    _notify_tick,
                    _NOTIFY_INTERVAL,
                )
"""

COMMENTARY_ANCHOR = """            clean = " ".join(piece.split())
            clean = _re.sub(r"^(?:[-*•>]\\s*|\\d+[.)]\\s*)+", "", clean).strip()
"""
COMMENTARY_REPLACEMENT = """            clean = _telegram_checkpoint_commentary_bullet_v2(piece)
            if not clean:
                continue
"""

PIECES_ANCHOR = """        pieces = [piece for piece in _re.split(r"\\n+|(?<=[.!?;])\\s+", text) if piece]
"""
PIECES_REPLACEMENT = """        pieces = [text]
"""

PENDING_ANCHOR = """                    with turn_ctx.model_checkpoint_lock:
                        checkpoint_stop = len(turn_ctx.model_checkpoint_updates)
                        tool_checkpoint_stop = len(
                            turn_ctx.model_checkpoint_tool_completed
                        )
                        checkpoint_pending = list(
                            turn_ctx.model_checkpoint_updates[
                                turn_ctx.model_checkpoint_cursor[0]:checkpoint_stop
                            ]
                        )
                        tool_checkpoint_pending = list(
                            turn_ctx.model_checkpoint_tool_completed[
                                turn_ctx.model_checkpoint_tool_cursor[0]:tool_checkpoint_stop
                            ]
                        )
                        tool_checkpoint_current = list(
                            turn_ctx.model_checkpoint_tool_current
                        )
"""
PENDING_REPLACEMENT = """                    with turn_ctx.model_checkpoint_lock:
                        checkpoint_start = turn_ctx.model_checkpoint_cursor[0]
                        tool_checkpoint_start = turn_ctx.model_checkpoint_tool_cursor[0]
                        checkpoint_stop = checkpoint_start
                        checkpoint_pending = []
                        while (
                            checkpoint_stop < len(turn_ctx.model_checkpoint_updates)
                            and len(checkpoint_pending) < 3
                        ):
                            checkpoint_value = _telegram_checkpoint_commentary_bullet_v2(
                                turn_ctx.model_checkpoint_updates[checkpoint_stop]
                            )
                            checkpoint_stop += 1
                            if checkpoint_value:
                                checkpoint_pending.append(checkpoint_value)
                        checkpoint_rejected_stop = (
                            checkpoint_stop
                            if not checkpoint_pending
                            else checkpoint_start
                        )
                        tool_checkpoint_stop = min(
                            len(turn_ctx.model_checkpoint_tool_completed),
                            tool_checkpoint_start + max(0, 3 - len(checkpoint_pending)),
                        )
                        tool_checkpoint_pending = list(
                            turn_ctx.model_checkpoint_tool_completed[
                                tool_checkpoint_start:tool_checkpoint_stop
                            ]
                        )
                        tool_checkpoint_current = list(
                            turn_ctx.model_checkpoint_tool_current
                        )
"""

EMPTY_HEARTBEAT_ANCHOR = """                    if not _heartbeat_text:
                        # A malformed task label is a fail-closed condition.
                        continue
"""
EMPTY_HEARTBEAT_REPLACEMENT = """                    if not _heartbeat_text:
                        # A malformed task label is a fail-closed condition.
                        with turn_ctx.model_checkpoint_lock:
                            turn_ctx.model_checkpoint_cursor[0] = max(
                                turn_ctx.model_checkpoint_cursor[0],
                                checkpoint_rejected_stop,
                            )
                            # Tool lifecycle telemetry is never client copy.
                            # Consume it instead of reconsidering the same
                            # synthetic fallback at every scheduled interval.
                            turn_ctx.model_checkpoint_tool_cursor[0] = max(
                                turn_ctx.model_checkpoint_tool_cursor[0],
                                tool_checkpoint_stop,
                            )
                        continue
"""

HELPER_ANCHOR = "\ndef _format_telegram_model_checkpoint(\n"
HELPER = r'''
def _telegram_checkpoint_minutes_v2(tick, interval_seconds):
    """Return the scheduled milestone, never a wall-clock-derived near miss."""
    try:
        tick_value = max(0, int(tick))
        interval_value = max(0.0, float(interval_seconds))
    except (TypeError, ValueError):
        return 0
    return max(0, int(round((tick_value * interval_value) / 60.0)))


def _sanitize_telegram_checkpoint_commentary_v2(value):
    """Keep factual prose while rejecting internal or sensitive surfaces."""
    import re as _re

    text = _redact_gateway_user_facing_secrets(str(value or ""))
    text = " ".join(text.split()).strip()
    if not text or len(text) > 600:
        return ""
    unsafe = (
        r"```|`[^`]+`",
        r"(?:^|\s)(?:~?/|\.{1,2}/|[A-Za-z]:\\)\S+",
        r"(?:^|\s)(?:[A-Za-z0-9_.-]+/){2,}[A-Za-z0-9_.-]+",
        r"\b(?:exec_command|tool[_ -]?(?:call|name|output|result)|stdout|stderr|"
        r"toolset|tooling|arguments?|argv|system prompt|developer message|chain[- ]of[- ]thought|"
        r"hidden reasoning|token budget)\b",
        r"\b(?:gbrain|mcp|capability[- ]router)\b",
        r"\b(?:pulling|loading|opening|reading|using)\b.{0,80}\bskills?\b",
        r"\b(?:identifying|checking|working)\b.{0,80}\blocally\b",
        r"\b(?:python3?|bash|zsh|pwsh|powershell|curl|wget|ssh)\s+[-\w]",
        r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}\b",
        r"^(?:analysis|reasoning|thought process)\s*:",
        r"\b(?:I think|I suspect|my reasoning)\b",
    )
    if any(_re.search(pattern, text, flags=_re.IGNORECASE) for pattern in unsafe):
        return ""
    return text[:280].rstrip()


def _telegram_checkpoint_commentary_bullet_v2(value):
    import re as _re

    clean = _sanitize_telegram_checkpoint_commentary_v2(value)
    if not clean:
        return ""
    return _re.sub(r"^(?:[-*•>]\s*|\d+[.)]\s*)+", "", clean).strip()
'''

SEND_ANCHOR = """                    if not (_notify_res and getattr(_notify_res, "success", False)):
                        _notify_res = await _notify_adapter.send(
"""
SEND_REPLACEMENT = """                    if (
                        _heartbeat_msg_id
                        and source.platform == Platform.TELEGRAM
                        and not (_notify_res and getattr(_notify_res, "success", False))
                    ):
                        # One foreground turn owns one checkpoint bubble. A failed
                        # edit is retained for the next scheduled edit, never fanned
                        # out into another client-visible Telegram message.
                        continue
                    if not (_notify_res and getattr(_notify_res, "success", False)):
                        _notify_res = await _notify_adapter.send(
"""

CURSOR_ADVANCE_ANCHOR = """                    with turn_ctx.model_checkpoint_lock:
                        turn_ctx.model_checkpoint_cursor[0] = max(
                            turn_ctx.model_checkpoint_cursor[0],
                            checkpoint_stop,
                        )
                        turn_ctx.model_checkpoint_tool_cursor[0] = max(
                            turn_ctx.model_checkpoint_tool_cursor[0],
                            tool_checkpoint_stop,
                        )
"""

DELIVERY_ACK_ANCHOR = """                except Exception as _ne:
                    logger.debug("Long-running notification error: %s", _ne)
"""
DELIVERY_ACK_REPLACEMENT = """                    if (
                        source.platform == Platform.TELEGRAM
                        and _notify_res
                        and getattr(_notify_res, "success", False)
                    ):
                        with turn_ctx.model_checkpoint_lock:
                            turn_ctx.model_checkpoint_cursor[0] = max(
                                turn_ctx.model_checkpoint_cursor[0],
                                checkpoint_stop,
                            )
                            turn_ctx.model_checkpoint_tool_cursor[0] = max(
                                turn_ctx.model_checkpoint_tool_cursor[0],
                                tool_checkpoint_stop,
                            )
                except Exception as _ne:
                    logger.debug("Long-running notification error: %s", _ne)
"""


def _patch_legacy_telegram_organic_long_running_checkpoints_v2(hermes_dir: Path) -> bool:
    """Upgrade both clean and already-v1-patched runtimes to the v2 contract."""
    run_py = Path(hermes_dir) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    if MARKER in original:
        if REVISION_MARKER not in original:
            raise RuntimeError("Telegram organic checkpoints stale v2 revision requires a clean candidate rebuild")
        if IMMEDIATE_TICK_REPLACEMENT in original or IMMEDIATE_TICK_ANCHOR not in original:
            return False
        if original.count(IMMEDIATE_TICK_ANCHOR) != 1:
            raise RuntimeError("Telegram immediate checkpoint anchor drift")
        return _write_checkpoint_source(
            run_py, original.replace(IMMEDIATE_TICK_ANCHOR, IMMEDIATE_TICK_REPLACEMENT, 1)
        )
    if V1_MARKER not in original:
        raise RuntimeError("Telegram organic checkpoints v2 requires the v1 base")

    immediate_first = IMMEDIATE_SLEEP_ANCHOR in original
    sleep_anchor = IMMEDIATE_SLEEP_ANCHOR if immediate_first else SLEEP_ANCHOR
    sleep_replacement = (
        IMMEDIATE_SLEEP_REPLACEMENT if immediate_first else SLEEP_REPLACEMENT
    )
    replacements = (
        (MARKER_ANCHOR, MARKER_REPLACEMENT, "version marker"),
        (START_ANCHOR, START_REPLACEMENT, "monotonic origin"),
        (sleep_anchor, sleep_replacement, "scheduled cadence"),
        (ELAPSED_ANCHOR, ELAPSED_REPLACEMENT, "scheduled milestone"),
        (PENDING_ANCHOR, PENDING_REPLACEMENT, "bounded pending milestones"),
        (
            EMPTY_HEARTBEAT_ANCHOR,
            EMPTY_HEARTBEAT_REPLACEMENT,
            "rejected commentary cursor",
        ),
        (PIECES_ANCHOR, PIECES_REPLACEMENT, "atomic commentary updates"),
        (COMMENTARY_ANCHOR, COMMENTARY_REPLACEMENT, "commentary privacy"),
        (HELPER_ANCHOR, HELPER + HELPER_ANCHOR, "v2 helpers"),
        (CURSOR_ADVANCE_ANCHOR, "", "delivery cursor ownership"),
        (SEND_ANCHOR, SEND_REPLACEMENT, "Telegram message reuse"),
        (DELIVERY_ACK_ANCHOR, DELIVERY_ACK_REPLACEMENT, "delivery acknowledgement"),
    )
    patched = original
    for anchor, replacement, label in replacements:
        if patched.count(anchor) != 1:
            raise RuntimeError(f"Telegram organic checkpoints v2 {label} anchor drift")
        patched = patched.replace(anchor, replacement, 1)

    return _write_checkpoint_source(run_py, patched)


def _write_checkpoint_source(run_py: Path, patched: str) -> bool:
    backup = Path(str(run_py) + ".bak-pre-telegram-organic-checkpoints-v2")
    shutil.copy2(run_py, backup)
    try:
        run_py.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, run_py)
        backup.unlink(missing_ok=True)
        raise
    return True


# Split d363 dispatch. The recovered legacy implementation above remains available for cb sources.
_D363_V2_NOTIFIER = r'''    async def _run_agent_notify_long_running(
        self, disp: "GatewayRunner._RunAgentDisplay", turn_ctx: TurnContext, _executor_task_holder: list,
    ) -> None:
        if turn_ctx.source.platform != Platform.TELEGRAM:
            return await self._run_agent_native_notify_long_running(disp, turn_ctx, _executor_task_holder)
        from gateway.run import _float_env, _interim_metadata, _non_conversational_metadata
        _notify_start = time.monotonic()
        _configured_interval = _float_env("HERMES_AGENT_NOTIFY_INTERVAL", 180)
        if _configured_interval <= 0:
            return
        _notify_interval = max(300.0, _configured_interval)
        _long_running_mode = disp._display_surface_mode("long_running_notifications", default=True, allow_generic=True)
        if _notify_interval <= 0 or _long_running_mode == "off":
            return
        source, session_key, agent_holder = turn_ctx.source, turn_ctx.session_key, turn_ctx.agent_holder
        _notify_adapter = self._adapter_for_source(source)
        if not _notify_adapter:
            return
        _heartbeat_msg_id = None
        _notify_tick = 0
        while True:
            _notify_tick += 1
            _deadline = _notify_start + (_notify_tick * _notify_interval)
            await asyncio.sleep(max(0.0, _deadline - time.monotonic()))
            if not self._should_emit_long_running_notification(session_key, agent_holder[0], _executor_task_holder[0]):
                break
            _elapsed_mins = _telegram_checkpoint_minutes_v2(_notify_tick, _notify_interval)
            if source.platform == Platform.TELEGRAM:
                with turn_ctx.model_checkpoint_lock:
                    _start = turn_ctx.model_checkpoint_cursor[0]
                    _stop = _start
                    _pending = []
                    while _stop < len(turn_ctx.model_checkpoint_updates) and len(_pending) < 3:
                        _value = _telegram_checkpoint_commentary_bullet_v2(turn_ctx.model_checkpoint_updates[_stop])
                        _stop += 1
                        if _value:
                            _pending.append(_value)
                    _tool_start = turn_ctx.model_checkpoint_tool_cursor[0]
                    _tool_stop = min(len(turn_ctx.model_checkpoint_tool_completed), _tool_start + max(0, 3 - len(_pending)))
                    _tool_pending = list(turn_ctx.model_checkpoint_tool_completed[_tool_start:_tool_stop])
                    _current = list(turn_ctx.model_checkpoint_tool_current)
                _heartbeat_text = _format_telegram_model_checkpoint(_elapsed_mins, _pending, task=turn_ctx.model_checkpoint_task, completed=_tool_pending, current=_current)
                if not _heartbeat_text:
                    with turn_ctx.model_checkpoint_lock:
                        turn_ctx.model_checkpoint_cursor[0] = max(turn_ctx.model_checkpoint_cursor[0], _stop)
                        turn_ctx.model_checkpoint_tool_cursor[0] = max(turn_ctx.model_checkpoint_tool_cursor[0], _tool_stop)
                    continue
            else:
                _heartbeat_text = disp._generic_status_phrase("status") if _long_running_mode == "generic" else f"⏳ Working — {_elapsed_mins} min"
            try:
                _notify_res = None
                if _heartbeat_msg_id:
                    with suppress(Exception):
                        _notify_res = await _notify_adapter.edit_message(source.chat_id, _heartbeat_msg_id, _heartbeat_text)
                    if source.platform == Platform.TELEGRAM and not getattr(_notify_res, "success", False):
                        continue
                if not (_notify_res and getattr(_notify_res, "success", False)):
                    _notify_res = await _notify_adapter.send(source.chat_id, _heartbeat_text, metadata=_interim_metadata(_non_conversational_metadata(turn_ctx._status_thread_metadata, platform=source.platform)))
                    if getattr(_notify_res, "success", False) and getattr(_notify_res, "message_id", None):
                        _heartbeat_msg_id = str(_notify_res.message_id)
                        if turn_ctx._cleanup_progress:
                            turn_ctx._cleanup_msg_ids.append(_heartbeat_msg_id)
                if source.platform == Platform.TELEGRAM and getattr(_notify_res, "success", False):
                    with turn_ctx.model_checkpoint_lock:
                        turn_ctx.model_checkpoint_cursor[0] = max(turn_ctx.model_checkpoint_cursor[0], _stop)
                        turn_ctx.model_checkpoint_tool_cursor[0] = max(turn_ctx.model_checkpoint_tool_cursor[0], _tool_stop)
            except Exception as _ne:
                logger.debug("Long-running notification error: %s", _ne)

'''

def _patch_split_d363(root: Path) -> bool:
    run_py = root / 'gateway/run_turn.py'
    run = run_py.read_text(encoding='utf-8')
    if MARKER in run:
        return False
    if V1_MARKER not in run:
        raise RuntimeError('Telegram organic checkpoints v2 requires the d363 v1 base')
    start = run.index('    async def _run_agent_notify_long_running(')
    end = run.index('    async def _run_agent_inner(', start)
    run = run[:start] + _D363_V2_NOTIFIER + run[end:]
    helper_anchor = '\ndef _format_telegram_model_checkpoint(\n'
    if run.count(helper_anchor) != 1:
        raise RuntimeError('Telegram organic checkpoints v2 d363 helper anchor drift')
    run = run.replace(helper_anchor, '\n' + HELPER + helper_anchor, 1)
    run = run.replace(f'# {V1_MARKER}\n', f'# {V1_MARKER}\n# {MARKER}\n# {REVISION_MARKER}\n', 1)
    backup = Path(str(run_py) + '.bak-pre-telegram-organic-checkpoints-v2')
    shutil.copy2(run_py, backup)
    try:
        run_py.write_text(run, encoding='utf-8')
    except Exception:
        shutil.copy2(backup, run_py); backup.unlink(missing_ok=True)
        raise
    return True

def patch_telegram_organic_long_running_checkpoints_v2(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    if (root / 'gateway/run_turn.py').exists():
        return _patch_split_d363(root)
    return _patch_legacy_telegram_organic_long_running_checkpoints_v2(root)
