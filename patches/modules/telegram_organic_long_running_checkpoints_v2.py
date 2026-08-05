#!/usr/bin/env python3
"""Harden Telegram checkpoint cadence, privacy, and message reuse."""

from __future__ import annotations

import shutil
from pathlib import Path

V1_MARKER = "HERMES_TELEGRAM_MODEL_COMMENTARY_CHECKPOINTS_v1"
MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2"

MARKER_ANCHOR = f"# {V1_MARKER}\n"
MARKER_REPLACEMENT = f"# {V1_MARKER}\n# {MARKER}\n"

START_ANCHOR = "        _notify_start = time.time()\n"
START_REPLACEMENT = """        # Schedule checkpoints from a monotonic origin. Wall-clock movement and
        # early wake-ups cannot turn the ten-minute milestone into "9 minutes".
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

ELAPSED_ANCHOR = "                _elapsed_mins = int((time.time() - _notify_start) // 60)\n"
ELAPSED_REPLACEMENT = """                _elapsed_mins = _telegram_checkpoint_minutes_v2(
                    _notify_tick,
                    _NOTIFY_INTERVAL,
                )
"""

COMMENTARY_ANCHOR = """            clean = " ".join(piece.split())
            clean = _re.sub(r"^(?:[-*•>]\\s*|\\d+[.)]\\s*)+", "", clean).strip()
"""
COMMENTARY_REPLACEMENT = """            clean = _sanitize_telegram_checkpoint_commentary_v2(piece)
            if not clean:
                continue
            clean = _re.sub(r"^(?:[-*•>]\\s*|\\d+[.)]\\s*)+", "", clean).strip()
"""

HELPER_ANCHOR = "\ndef _format_telegram_model_checkpoint(\n"
HELPER = r'''
def _telegram_checkpoint_minutes_v2(tick, interval_seconds):
    """Return the scheduled milestone, never a wall-clock-derived near miss."""
    try:
        tick_value = max(1, int(tick))
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
        r"arguments?|argv|system prompt|developer message|chain[- ]of[- ]thought|"
        r"hidden reasoning|token budget)\b",
        r"\b(?:python3?|bash|zsh|pwsh|powershell|curl|wget|ssh)\s+[-\w]",
        r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}\b",
        r"^(?:analysis|reasoning|thought process)\s*:",
        r"\b(?:I think|I suspect|my reasoning)\b",
    )
    if any(_re.search(pattern, text, flags=_re.IGNORECASE) for pattern in unsafe):
        return ""
    return text[:280].rstrip()
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


def patch_telegram_organic_long_running_checkpoints_v2(hermes_dir: Path) -> bool:
    """Upgrade both clean and already-v1-patched runtimes to the v2 contract."""
    run_py = Path(hermes_dir) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    if MARKER in original:
        return False
    if V1_MARKER not in original:
        raise RuntimeError("Telegram organic checkpoints v2 requires the v1 base")

    replacements = (
        (MARKER_ANCHOR, MARKER_REPLACEMENT, "version marker"),
        (START_ANCHOR, START_REPLACEMENT, "monotonic origin"),
        (SLEEP_ANCHOR, SLEEP_REPLACEMENT, "scheduled cadence"),
        (ELAPSED_ANCHOR, ELAPSED_REPLACEMENT, "scheduled milestone"),
        (COMMENTARY_ANCHOR, COMMENTARY_REPLACEMENT, "commentary privacy"),
        (HELPER_ANCHOR, HELPER + HELPER_ANCHOR, "v2 helpers"),
        (SEND_ANCHOR, SEND_REPLACEMENT, "Telegram message reuse"),
    )
    patched = original
    for anchor, replacement, label in replacements:
        if patched.count(anchor) != 1:
            raise RuntimeError(f"Telegram organic checkpoints v2 {label} anchor drift")
        patched = patched.replace(anchor, replacement, 1)

    backup = Path(str(run_py) + ".bak-pre-telegram-organic-checkpoints-v2")
    shutil.copy2(run_py, backup)
    try:
        run_py.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, run_py)
        backup.unlink(missing_ok=True)
        raise
    return True
