#!/usr/bin/env python3
"""Build honest, privacy-safe Telegram checkpoints for long-running turns."""

from __future__ import annotations

import shutil
from pathlib import Path

MARKER = "HERMES_TELEGRAM_MODEL_COMMENTARY_CHECKPOINTS_v1"
HELPER_ANCHOR = "\ndef _last_transcript_timestamp(history: Optional[List[Dict[str, Any]]]) -> Any:\n"
TURN_CONTEXT_ANCHOR = """        )
        turn_runner = TurnRunner(self, turn_ctx)
"""
TURN_CONTEXT_REPLACEMENT = """        )
        # Periodic Telegram summaries prefer real model commentary from this
        # run. TurnContext is intentionally open (not slotted), so this
        # surface-only state can ride the native per-turn seam without adding
        # another runtime context type.
        turn_ctx.model_checkpoint_lock = threading.Lock()
        turn_ctx.model_checkpoint_updates = []
        turn_ctx.model_checkpoint_cursor = [0]
        turn_ctx.model_checkpoint_task = _telegram_checkpoint_task_label(message)
        turn_runner = TurnRunner(self, turn_ctx)
"""
INTERIM_ANCHOR = """        def _interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
            if not ctx._run_still_current():
                return
            display_text = text
            if _stream_consumer is not None:
"""
INTERIM_REPLACEMENT = """        def _interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
            if not ctx._run_still_current():
                return
            display_text = text
            if ctx.source.platform == Platform.TELEGRAM:
                checkpoint_text = _redact_gateway_user_facing_secrets(
                    str(display_text or "")
                ).strip()
                if checkpoint_text:
                    checkpoint_text = checkpoint_text[:1200]
                    with ctx.model_checkpoint_lock:
                        if (
                            not ctx.model_checkpoint_updates
                            or ctx.model_checkpoint_updates[-1] != checkpoint_text
                        ):
                            ctx.model_checkpoint_updates.append(checkpoint_text)
            # Capture is internal. Respect the configured surface policy
            # instead of turning each interim model message into a chat post.
            if not _want_interim_messages:
                return
            if _stream_consumer is not None:
"""
CALLBACK_ASSIGNMENT_ANCHOR = """        agent.interim_assistant_callback = _interim_assistant_cb if _want_interim_messages else None
"""
CALLBACK_ASSIGNMENT_REPLACEMENT = """        # Telegram checkpoints need commentary capture even when immediate
        # interim-message display is disabled.
        agent.interim_assistant_callback = (
            _interim_assistant_cb
            if (_want_interim_messages or ctx.source.platform == Platform.TELEGRAM)
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
                    with turn_ctx.model_checkpoint_lock:
                        checkpoint_stop = len(turn_ctx.model_checkpoint_updates)
                        checkpoint_pending = list(
                            turn_ctx.model_checkpoint_updates[
                                turn_ctx.model_checkpoint_cursor[0]:checkpoint_stop
                            ]
                        )
                    _heartbeat_text = _format_telegram_model_checkpoint(
                        _elapsed_mins,
                        checkpoint_pending,
                        task=turn_ctx.model_checkpoint_task,
                    )
                    if not _heartbeat_text:
                        # A malformed task label is a fail-closed condition.
                        continue
                    with turn_ctx.model_checkpoint_lock:
                        turn_ctx.model_checkpoint_cursor[0] = max(
                            turn_ctx.model_checkpoint_cursor[0],
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
def _telegram_checkpoint_task_label(raw_message):
    """Return a fixed-vocabulary task label without retaining request content."""
    import re as _re
    import unicodedata as _unicodedata

    text = _redact_gateway_user_facing_secrets(str(raw_message or ""))
    text = _re.sub(
        r"<(?:system|developer)[^>]*>.*?</(?:system|developer)>",
        " ",
        text,
        flags=_re.DOTALL | _re.IGNORECASE,
    )
    normalized = " ".join(_unicodedata.normalize("NFKC", text).casefold().split())
    words = set(_re.findall(r"[a-z]+", normalized))

    subjects = (
        (("ledger", "usage", "receipt"), "the usage ledger"),
        (("telegram", "checkpoint"), "the Telegram checkpoint"),
        (("test", "tests", "verification"), "the verification"),
        (("deploy", "rollout", "runtime", "gateway", "fleet"), "the runtime rollout"),
        (("build", "artifact", "package"), "the build"),
        (("config", "configuration", "setting"), "the configuration"),
        (("code", "bug", "patch", "change", "implementation"), "the code change"),
    )
    subject = next((label for terms, label in subjects if words.intersection(terms)), "your request")
    return subject


def _format_telegram_model_checkpoint(elapsed_mins, updates, *, task=None):
    """Render commentary, or a bounded task-tied liveness update when absent."""
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
    minutes = max(0, int(elapsed_mins or 0))
    lines = [f"{{minutes}} minutes in — quick update:"]
    if bullets:
        lines.extend(f"• {{bullet}}" for bullet in bullets)
    else:
        task_label = " ".join(str(task or "").split())[:160]
        if not task_label:
            return ""
        lines.extend((
            f"• Still working on: {{task_label}}.",
            "• I’ll send the verified outcome when this run completes.",
        ))
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
        (TURN_CONTEXT_ANCHOR, TURN_CONTEXT_REPLACEMENT, "turn context"),
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
