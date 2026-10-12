#!/usr/bin/env python3
# ruff: noqa: E501 -- embedded gateway source preserves patch anchors and indentation.
"""Render Telegram direction cards from model commentary without extra bubbles."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

V1_MARKER = "HERMES_TELEGRAM_MODEL_COMMENTARY_CHECKPOINTS_v1"
V2_MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2"
MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v3"
REVISION_MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v3_r1"

_CONTEXT_ANCHOR = "            turn_ctx.model_checkpoint_cursor = [0]\n"
_CONTEXT_REPLACEMENT = """            turn_ctx.model_checkpoint_cursor = [0]
            # The callback runs on the agent worker thread. This event only wakes
            # the Telegram notifier; it never turns commentary into an interim
            # message.
            turn_ctx.model_checkpoint_event = threading.Event()
"""

_RUNNER_ANCHOR = """                    if not ctx.model_checkpoint_updates or ctx.model_checkpoint_updates[-1] != checkpoint_text:
                        ctx.model_checkpoint_updates.append(checkpoint_text)
"""
_RUNNER_REPLACEMENT = """                    if not ctx.model_checkpoint_updates or ctx.model_checkpoint_updates[-1] != checkpoint_text:
                        ctx.model_checkpoint_updates.append(checkpoint_text)
                        checkpoint_event = getattr(ctx, "model_checkpoint_event", None)
                        if checkpoint_event is not None:
                            checkpoint_event.set()
"""

_HELPERS = r'''
def _telegram_checkpoint_safe_lines_v3(value):
    """Return independently sanitized commentary lines, preserving list marks."""
    import re as _re

    lines = []
    for raw_line in _re.split(r"\r?\n+", str(value or "")):
        clean = _sanitize_telegram_checkpoint_commentary_v2(raw_line)
        if clean:
            lines.append(clean)
    return lines


def _telegram_checkpoint_phase_lines_v3(value):
    """Return safe prose sentences for the changing `Now:` card line."""
    import re as _re

    phases = []
    for line in _telegram_checkpoint_safe_lines_v3(value):
        for piece in _re.split(r"(?<=[.!?;])\s+", line):
            clean = _telegram_checkpoint_commentary_bullet_v2(piece)
            if clean:
                phases.append(clean)
    return phases


def _format_telegram_model_checkpoint(
    elapsed_mins,
    updates,
    *,
    task=None,
    completed=None,
    current=None,
    activity=None,
    plan=None,
):
    """Render a bounded direction card from model-authored commentary only."""
    phases = []
    for update in updates or []:
        phases.extend(_telegram_checkpoint_phase_lines_v3(update))
    if not phases:
        return ""

    safe_plan = []
    for line in (plan or [])[:6]:
        clean = _sanitize_telegram_checkpoint_commentary_v2(line)
        if clean:
            safe_plan.append(clean)
    minutes = max(0, int(elapsed_mins or 0))
    task_label = " ".join(str(task or "").split())[:160]
    header = f"{task_label} — {minutes} min" if task_label else f"{minutes} min"
    phase = phases[-1]

    # A one-line first segment is deliberately the former concise card. A
    # direction block starts only when the model actually supplied a plan.
    if len(safe_plan) < 2:
        return f"{header}\n\n{phase}"[:1200]

    lines = [header]
    for line in safe_plan:
        candidate = "\n".join(lines + [line, "", f"Now: {phase}"])
        if len(candidate) > 1200:
            break
        lines.append(line)
    return "\n".join(lines + ["", f"Now: {phase}"])[:1200]
'''

_D363_V3_NOTIFIER = r'''    async def _run_agent_notify_long_running(
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
        _edit_floor = 60.0
        _long_running_mode = disp._display_surface_mode("long_running_notifications", default=True, allow_generic=True)
        if _long_running_mode == "off":
            return
        source, session_key, agent_holder = turn_ctx.source, turn_ctx.session_key, turn_ctx.agent_holder
        _notify_adapter = self._delivery_adapter_for(source)
        if not _notify_adapter:
            return
        _checkpoint_event = getattr(turn_ctx, "model_checkpoint_event", None)
        if _checkpoint_event is None:
            _checkpoint_event = threading.Event()
            turn_ctx.model_checkpoint_event = _checkpoint_event
        _heartbeat_msg_id = None
        _next_fallback = _notify_start + _notify_interval
        _last_delivery_at = None
        _plan = []
        _plan_decided = False
        _last_phase = ""
        while True:
            if not self._should_emit_long_running_notification(session_key, agent_holder[0], _executor_task_holder[0]):
                break
            _now = time.monotonic()
            _fallback_due = _now >= _next_fallback
            if not _fallback_due and not _checkpoint_event.is_set():
                # The producer is a worker thread. Polling its Event keeps this
                # coroutine cancellable while waking within one second.
                await asyncio.sleep(min(1.0, max(0.0, _next_fallback - _now)))
                continue
            _checkpoint_event.clear()
            _now = time.monotonic()
            _fallback_due = _now >= _next_fallback
            if _fallback_due:
                while _next_fallback <= _now:
                    _next_fallback += _notify_interval
            with turn_ctx.model_checkpoint_lock:
                _start = turn_ctx.model_checkpoint_cursor[0]
                _stop = len(turn_ctx.model_checkpoint_updates)
                _pending = list(turn_ctx.model_checkpoint_updates[_start:_stop])
            _new_phase = ""
            _had_clean_commentary = False
            for _update in _pending:
                _plan_lines = _telegram_checkpoint_safe_lines_v3(_update)
                _phase_lines = _telegram_checkpoint_phase_lines_v3(_update)
                if not _phase_lines:
                    continue
                _had_clean_commentary = True
                if not _plan_decided:
                    _plan_decided = True
                    if len(_plan_lines) >= 2:
                        _plan = _plan_lines[:6]
                _new_phase = _phase_lines[-1]
            if _new_phase:
                _last_phase = _new_phase
            if not _last_phase:
                if _stop > _start and not _had_clean_commentary:
                    with turn_ctx.model_checkpoint_lock:
                        turn_ctx.model_checkpoint_cursor[0] = max(turn_ctx.model_checkpoint_cursor[0], _stop)
                continue
            if _heartbeat_msg_id and _new_phase and _last_delivery_at is not None:
                if _now - _last_delivery_at < _edit_floor:
                    await asyncio.sleep(max(0.0, _edit_floor - (_now - _last_delivery_at)))
                    _now = time.monotonic()
            _elapsed_mins = int(max(0.0, _now - _notify_start) // 60)
            _heartbeat_text = _format_telegram_model_checkpoint(
                _elapsed_mins,
                [_last_phase],
                task=turn_ctx.model_checkpoint_task,
                plan=_plan,
            )
            if not _heartbeat_text:
                continue
            try:
                _notify_res = None
                if _heartbeat_msg_id:
                    with suppress(Exception):
                        _notify_res = await _notify_adapter.edit_message(source.chat_id, _heartbeat_msg_id, _heartbeat_text)
                    if not getattr(_notify_res, "success", False):
                        # A failed Telegram edit retains this one card for the
                        # next allowed attempt. Never fan out another message.
                        continue
                if not (_notify_res and getattr(_notify_res, "success", False)):
                    _notify_res = await _notify_adapter.send(
                        source.chat_id,
                        _heartbeat_text,
                        metadata=_interim_metadata(_non_conversational_metadata(turn_ctx._status_thread_metadata, platform=source.platform)),
                    )
                    if getattr(_notify_res, "success", False) and getattr(_notify_res, "message_id", None):
                        _heartbeat_msg_id = str(_notify_res.message_id)
                        if turn_ctx._cleanup_progress:
                            turn_ctx._cleanup_msg_ids.append(_heartbeat_msg_id)
                if getattr(_notify_res, "success", False):
                    _last_delivery_at = _now
                    with turn_ctx.model_checkpoint_lock:
                        turn_ctx.model_checkpoint_cursor[0] = max(turn_ctx.model_checkpoint_cursor[0], _stop)
            except Exception as _ne:
                logger.debug("Long-running notification error: %s", _ne)

'''


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    if source.count(anchor) != 1:
        raise RuntimeError(f"Telegram organic checkpoints v3 {label} anchor drift")
    return source.replace(anchor, replacement, 1)


def _replace_formatter(source: str) -> str:
    start = source.find("def _format_telegram_model_checkpoint(\n")
    end = source.find("\n\n_tool_call_logger_lock", start)
    if start < 0 or end < 0:
        raise RuntimeError("Telegram organic checkpoints v3 formatter anchor drift")
    return source[:start] + _HELPERS + source[end:]


def _load_v2_module():
    path = Path(__file__).with_name("telegram_organic_long_running_checkpoints_v2.py")
    spec = importlib.util.spec_from_file_location("telegram_organic_checkpoints_v2_predecessor", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Telegram organic checkpoints v3 predecessor unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_split_d363(root: Path) -> bool:
    run_py = root / "gateway/run_turn.py"
    runner_py = root / "gateway/run_turn_runner.py"
    run = run_py.read_text(encoding="utf-8")
    runner = runner_py.read_text(encoding="utf-8")
    run_has_marker = MARKER in run
    runner_has_marker = MARKER in runner
    if run_has_marker and runner_has_marker:
        if REVISION_MARKER not in run:
            raise RuntimeError("Telegram organic checkpoints stale v3 revision requires a clean candidate rebuild")
        return False
    if run_has_marker or runner_has_marker:
        raise RuntimeError("Telegram organic checkpoints v3 found a partial prior apply")
    if V2_MARKER not in run:
        raise RuntimeError("Telegram organic checkpoints v3 requires the v2 base")

    notifier_start = run.find("    async def _run_agent_notify_long_running(")
    notifier_end = run.find("    async def _run_agent_inner(", notifier_start)
    if notifier_start < 0 or notifier_end < 0:
        raise RuntimeError("Telegram organic checkpoints v3 notifier anchor drift")
    run = run[:notifier_start] + _D363_V3_NOTIFIER + run[notifier_end:]
    run = _replace_once(run, _CONTEXT_ANCHOR, _CONTEXT_REPLACEMENT, "turn context")
    run = _replace_formatter(run)
    run = run.replace(
        f"# {V2_MARKER}\n",
        f"# {V2_MARKER}\n# {MARKER}\n# {REVISION_MARKER}\n",
        1,
    )
    runner = _replace_once(runner, _RUNNER_ANCHOR, _RUNNER_REPLACEMENT, "commentary wake-up")
    runner += f"\n# {MARKER}\n"

    backups = {
        run_py: Path(str(run_py) + ".bak-pre-telegram-organic-checkpoints-v3"),
        runner_py: Path(str(runner_py) + ".bak-pre-telegram-organic-checkpoints-v3"),
    }
    for path, backup in backups.items():
        shutil.copy2(path, backup)
    try:
        run_py.write_text(run, encoding="utf-8")
        runner_py.write_text(runner, encoding="utf-8")
    except Exception:
        for path, backup in backups.items():
            if backup.exists():
                shutil.copy2(backup, path)
                backup.unlink(missing_ok=True)
        raise
    return True


def patch_telegram_organic_long_running_checkpoints_v3(hermes_dir: Path) -> bool:
    """Build v3 only from a clean v1 candidate; never mutate an r6 candidate."""
    root = Path(hermes_dir)
    run_py = root / "gateway/run_turn.py"
    if not run_py.exists():
        raise RuntimeError("Telegram organic checkpoints v3 requires the split gateway runtime")
    original_run = run_py.read_text(encoding="utf-8")
    if MARKER in original_run:
        if REVISION_MARKER not in original_run:
            raise RuntimeError("Telegram organic checkpoints stale v3 revision requires a clean candidate rebuild")
        return False
    if V2_MARKER in original_run:
        raise RuntimeError("Telegram organic checkpoints v3 requires a clean candidate rebuild")
    if V1_MARKER not in original_run:
        raise RuntimeError("Telegram organic checkpoints v3 requires the v1 base")

    predecessor = _load_v2_module()
    if not predecessor.patch_telegram_organic_long_running_checkpoints_v2(root):
        raise RuntimeError("Telegram organic checkpoints v3 could not build the v2 base")
    try:
        return _patch_split_d363(root)
    except Exception:
        run_py.write_text(original_run, encoding="utf-8")
        raise
