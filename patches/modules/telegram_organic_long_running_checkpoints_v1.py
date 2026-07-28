#!/usr/bin/env python3
"""Build Telegram checkpoints from the model's own interim commentary."""

from __future__ import annotations

import shutil
from pathlib import Path

MARKER = "HERMES_TELEGRAM_MODEL_COMMENTARY_CHECKPOINTS_v1"
HELPER_ANCHOR = "\ndef _last_transcript_timestamp(history: Optional[List[Dict[str, Any]]]) -> Any:\n"
QUEUE_ANCHOR = """        # Queue for progress messages (thread-safe)
        progress_queue = queue.Queue() if needs_progress_queue else None
        last_tool = [None]  # Mutable container for tracking in closure
"""
QUEUE_REPLACEMENT = """        # Queue for progress messages (thread-safe)
        progress_queue = queue.Queue() if needs_progress_queue else None
        # Periodic Telegram summaries reuse real model commentary from this run.
        # They do not infer progress from tool names, arguments, or results.
        model_checkpoint_lock = threading.Lock()
        model_checkpoint_updates: List[str] = []
        model_checkpoint_cursor = [0]
        last_tool = [None]  # Mutable container for tracking in closure
"""
INTERIM_ANCHOR = """            def _interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
                if not _run_still_current():
                    return
                display_text = text
                if _stream_consumer is not None:
"""
INTERIM_REPLACEMENT = """            def _interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
                if not _run_still_current():
                    return
                display_text = text
                if source.platform == Platform.TELEGRAM:
                    checkpoint_text = _redact_gateway_user_facing_secrets(
                        str(display_text or "")
                    ).strip()
                    if checkpoint_text:
                        checkpoint_text = checkpoint_text[:1200]
                        with model_checkpoint_lock:
                            if (
                                not model_checkpoint_updates
                                or model_checkpoint_updates[-1] != checkpoint_text
                            ):
                                model_checkpoint_updates.append(checkpoint_text)
                # Capture is internal.  Respect the configured surface policy
                # instead of turning each interim model message into a chat post.
                if not _want_interim_messages:
                    return
                if _stream_consumer is not None:
"""
CALLBACK_ASSIGNMENT_ANCHOR = """            agent.interim_assistant_callback = _interim_assistant_cb if _want_interim_messages else None
"""
CALLBACK_ASSIGNMENT_REPLACEMENT = """            # Telegram checkpoints need commentary capture even when immediate
            # interim-message display is disabled.
            agent.interim_assistant_callback = (
                _interim_assistant_cb
                if (_want_interim_messages or source.platform == Platform.TELEGRAM)
                else None
            )
"""
HEARTBEAT_ANCHOR = """                # Include agent activity context if available. Default
                # heartbeat is terse: elapsed + current tool. Verbose
                # iteration counter is gated on busy_ack_detail so users
                # who want it can opt in per platform.
                _agent_ref = agent_holder[0]
                _status_detail = ""
                _want_iteration_detail = bool(
                    resolve_display_setting(
                        user_config,
                        platform_key,
                        "busy_ack_detail",
                        True,
                    )
                )
                if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                    try:
                        _a = _agent_ref.get_activity_summary()
                        _parts = []
                        if _want_iteration_detail:
                            _parts.append(
                                f"iteration {_a['api_call_count']}/{_a['max_iterations']}"
                            )
                        _action = _a.get("current_tool") or _a.get("last_activity_desc")
                        if _action:
                            _parts.append(str(_action))
                        if _parts:
                            _status_detail = " — " + ", ".join(_parts)
                    except Exception:
                        pass
                _heartbeat_text = (
                    _generic_status_phrase("status")
                    if _long_running_mode == "generic"
                    else f"⏳ Working — {_elapsed_mins} min{_status_detail}"
                )
"""
HEARTBEAT_REPLACEMENT = """                if source.platform == Platform.TELEGRAM:
                    with model_checkpoint_lock:
                        checkpoint_stop = len(model_checkpoint_updates)
                        checkpoint_pending = list(
                            model_checkpoint_updates[
                                model_checkpoint_cursor[0]:checkpoint_stop
                            ]
                        )
                    _heartbeat_text = _format_telegram_model_checkpoint(
                        _elapsed_mins,
                        checkpoint_pending,
                    )
                    if not _heartbeat_text:
                        # Never replace missing model commentary with invented
                        # progress. Try again after the next interval.
                        continue
                    with model_checkpoint_lock:
                        model_checkpoint_cursor[0] = max(
                            model_checkpoint_cursor[0],
                            checkpoint_stop,
                        )
                else:
                    # Preserve native Hermes behavior on every other surface.
                    _agent_ref = agent_holder[0]
                    _status_detail = ""
                    _want_iteration_detail = bool(
                        resolve_display_setting(
                            user_config,
                            platform_key,
                            "busy_ack_detail",
                            True,
                        )
                    )
                    if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                        try:
                            _a = _agent_ref.get_activity_summary()
                            _parts = []
                            if _want_iteration_detail:
                                _parts.append(
                                    f"iteration {_a['api_call_count']}/{_a['max_iterations']}"
                                )
                            _action = _a.get("current_tool") or _a.get("last_activity_desc")
                            if _action:
                                _parts.append(str(_action))
                            if _parts:
                                _status_detail = " — " + ", ".join(_parts)
                        except Exception:
                            pass
                    _heartbeat_text = (
                        _generic_status_phrase("status")
                        if _long_running_mode == "generic"
                        else f"⏳ Working — {_elapsed_mins} min{_status_detail}"
                    )
"""
HELPERS = f'''
# {MARKER}
def _format_telegram_model_checkpoint(elapsed_mins, updates):
    """Render recent model-authored commentary as a short periodic summary."""
    import re as _re

    candidates = []
    for update in updates or []:
        text = str(update or "").strip()
        if not text:
            continue
        pieces = [piece for piece in _re.split(r"\\n+|(?<=[.!?;])\\s+", text) if piece]
        for piece in pieces:
            clean = " ".join(piece.split())
            clean = _re.sub(r"^(?:[-*•>]\\s*|\\d+[.)]\\s*)+", "", clean).strip()
            if not clean or clean.startswith("```"):
                continue
            clean = clean[:280].rstrip()
            if clean and clean not in candidates:
                candidates.append(clean)

    bullets = candidates[-4:]
    if not bullets:
        return ""
    minutes = max(0, int(elapsed_mins or 0))
    lines = [f"{{minutes}} minutes in — quick update:"]
    lines.extend(f"• {{bullet}}" for bullet in bullets)
    return "\\n".join(lines)[:1200]
'''


def patch_telegram_organic_long_running_checkpoints_v1(hermes_dir: Path) -> bool:
    """Patch only the Telegram periodic-notification seam."""
    run_py = Path(hermes_dir) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    if MARKER in original:
        return False

    replacements = (
        (HELPER_ANCHOR, HELPERS + HELPER_ANCHOR, "helper"),
        (QUEUE_ANCHOR, QUEUE_REPLACEMENT, "queue"),
        (INTERIM_ANCHOR, INTERIM_REPLACEMENT, "commentary capture"),
        (
            CALLBACK_ASSIGNMENT_ANCHOR,
            CALLBACK_ASSIGNMENT_REPLACEMENT,
            "commentary callback assignment",
        ),
        (HEARTBEAT_ANCHOR, HEARTBEAT_REPLACEMENT, "heartbeat"),
    )
    patched = original
    for anchor, replacement, label in replacements:
        if patched.count(anchor) != 1:
            raise RuntimeError(f"telegram model checkpoint {label} anchor drift")
        patched = patched.replace(anchor, replacement, 1)

    backup = Path(str(run_py) + ".bak-pre-telegram-model-checkpoints-v1")
    shutil.copy2(run_py, backup)
    try:
        run_py.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, run_py)
        backup.unlink(missing_ok=True)
        raise
    return True
