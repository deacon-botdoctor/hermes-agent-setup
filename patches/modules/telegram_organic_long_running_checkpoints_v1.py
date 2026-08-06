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
        # Tool lifecycle previews are projected to bounded, client-safe labels
        # immediately. Raw commands, arguments, paths, and results are never
        # retained in checkpoint state.
        turn_ctx.model_checkpoint_tool_active = {}
        turn_ctx.model_checkpoint_tool_current = []
        turn_ctx.model_checkpoint_tool_completed = []
        turn_ctx.model_checkpoint_tool_cursor = [0]
        turn_runner = TurnRunner(self, turn_ctx)
"""
PROGRESS_CALLBACK_ANCHOR = """        ctx = self._ctx
        # Live status line (Slack's assistant status): stash the current
"""
PROGRESS_CALLBACK_REPLACEMENT = """        ctx = self._ctx
        if ctx.source.platform == Platform.TELEGRAM:
            _capture_telegram_tool_checkpoint(
                ctx,
                event_type,
                tool_name,
                preview,
                kwargs,
            )
        # Live status line (Slack's assistant status): stash the current
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
CALLBACK_ASSIGNMENT_ANCHOR = (
    "        agent.interim_assistant_callback = _interim_assistant_cb "
    "if _want_interim_messages else None\n"
)
CALLBACK_ASSIGNMENT_REPLACEMENT = """        # Telegram checkpoints need commentary capture even when immediate
        # interim-message display is disabled.
        agent.interim_assistant_callback = (
            _interim_assistant_cb
            if (_want_interim_messages or ctx.source.platform == Platform.TELEGRAM)
            else None
        )
"""
TOOL_PROGRESS_ASSIGNMENT_ANCHOR = """                ctx.needs_progress_queue
                or ctx.log_mode_enabled
                or ctx._live_status_adapter is not None
"""
TOOL_PROGRESS_ASSIGNMENT_REPLACEMENT = """                ctx.needs_progress_queue
                or ctx.log_mode_enabled
                or ctx._live_status_adapter is not None
                or ctx.source.platform == Platform.TELEGRAM
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
                    _agent_ref = agent_holder[0]
                    _activity = None
                    if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                        try:
                            _activity = _agent_ref.get_activity_summary()
                        except Exception:
                            _activity = None
                    _heartbeat_text = _format_telegram_model_checkpoint(
                        _elapsed_mins,
                        checkpoint_pending,
                        task=turn_ctx.model_checkpoint_task,
                        completed=tool_checkpoint_pending,
                        current=tool_checkpoint_current,
                        activity=_activity,
                    )
                    if not _heartbeat_text:
                        # A malformed task label is a fail-closed condition.
                        continue
                    with turn_ctx.model_checkpoint_lock:
                        turn_ctx.model_checkpoint_cursor[0] = max(
                            turn_ctx.model_checkpoint_cursor[0],
                            checkpoint_stop,
                        )
                        turn_ctx.model_checkpoint_tool_cursor[0] = max(
                            turn_ctx.model_checkpoint_tool_cursor[0],
                            tool_checkpoint_stop,
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
    subject = next((label for terms, label in subjects if words.intersection(terms)), "")
    return subject


def _telegram_checkpoint_preview_subject(preview):
    """Return an allowlisted subject without preserving raw tool input."""
    import re as _re

    text = _redact_gateway_user_facing_secrets(str(preview or ""))
    text = " ".join(text.split())
    if not text:
        return ""
    lowered = text.casefold()
    known_sources = (
        ("github", "GitHub"),
        ("youtube", "YouTube"),
        ("reddit", "Reddit"),
        ("linkedin", "LinkedIn"),
        ("facebook", "Facebook"),
        ("instagram", "Instagram"),
        ("twitter", "X/Twitter"),
        ("x.com", "X/Twitter"),
    )
    for needle, label in known_sources:
        if needle in lowered:
            return label

    path = _re.search(r"[A-Za-z0-9_.-]+\\.[A-Za-z0-9]{{1,12}}(?:[: ,)]|$)", text)
    if not path:
        return ""
    suffix = path.group(0).rstrip(": ,)").rsplit(".", 1)[-1].casefold()
    file_kinds = {{
        "py": "a Python file",
        "js": "a JavaScript file",
        "ts": "a TypeScript file",
        "tsx": "a TypeScript file",
        "json": "a JSON file",
        "yaml": "a configuration file",
        "yml": "a configuration file",
        "md": "a documentation file",
        "toml": "a configuration file",
        "sh": "a shell script",
    }}
    return file_kinds.get(suffix, "a project file")


def _telegram_checkpoint_tool_labels(tool_name, preview=None):
    """Project only specific, observable tool work into safe descriptions.

    Generic tool names are not useful evidence. Returning empty labels makes
    the periodic notifier wait for model commentary or a concrete lifecycle
    milestone instead of inventing canned progress.
    """
    name = str(tool_name or "").strip().lower().replace("-", "_")
    if not name or name in {{"_thinking", "todo", "task_update", "task_done"}}:
        return "", ""
    subject = _telegram_checkpoint_preview_subject(preview)

    if name in {{"web_search", "web_extract"}} or "search" in name or "query" in name:
        if not subject:
            return "", ""
        return f"Searching {{subject}}", f"Reviewed results from {{subject}}"
    if "browser" in name or "navigate" in name:
        if not subject:
            return "", ""
        return f"Reviewing {{subject}}", f"Reviewed {{subject}}"
    if any(word in name for word in ("read", "inspect", "memory", "session", "list")):
        if not subject:
            return "", ""
        return f"Reviewing {{subject}}", f"Reviewed {{subject}}"
    if any(word in name for word in ("patch", "edit", "write", "update", "create")):
        if not subject:
            return "", ""
        return f"Updating {{subject}}", f"Updated {{subject}}"
    if name in {{"terminal", "exec_command", "execute_code", "process"}}:
        lowered = str(preview or "").lower()
        if any(token in lowered for token in ("pytest", " test", "npm test", "pnpm test")):
            return "Running the focused tests", "Ran the focused tests"
        if any(token in lowered for token in ("git diff", "git status")):
            return "Reviewing the pending changes", "Reviewed the pending changes"
        if "ssh " in lowered:
            return "Checking the remote runtime", "Checked the remote runtime"
        return "", ""
    return "", ""


def _capture_telegram_tool_checkpoint(ctx, event_type, tool_name, preview, kwargs):
    """Retain only safe lifecycle labels for the active Telegram turn."""
    if not ctx._run_still_current() or tool_name == "_thinking":
        return
    try:
        key = str(tool_name or "work")
        if event_type == "tool.started":
            active, done = _telegram_checkpoint_tool_labels(tool_name, preview)
            if not active and not done:
                return
            with ctx.model_checkpoint_lock:
                ctx.model_checkpoint_tool_active.setdefault(key, []).append((active, done))
                if active:
                    ctx.model_checkpoint_tool_current.append(active)
                    del ctx.model_checkpoint_tool_current[:-8]
            return
        if event_type != "tool.completed":
            return
        with ctx.model_checkpoint_lock:
            entries = ctx.model_checkpoint_tool_active.get(key) or []
            entry = entries.pop(0) if entries else None
            if not entries:
                ctx.model_checkpoint_tool_active.pop(key, None)
            active, done = entry if entry else _telegram_checkpoint_tool_labels(tool_name)
            if active:
                try:
                    ctx.model_checkpoint_tool_current.remove(active)
                except ValueError:
                    pass
            if kwargs.get("is_error"):
                done = "A work step hit a problem; I’m resolving it"
            if done:
                ctx.model_checkpoint_tool_completed.append(done)
                del ctx.model_checkpoint_tool_completed[:-16]
    except Exception:
        return


def _telegram_checkpoint_activity_label(activity):
    """Translate runtime activity without exposing its internal identifier."""
    if not isinstance(activity, dict):
        return ""
    raw = activity.get("current_tool") or activity.get("last_activity_desc")
    normalized = str(raw or "").lower().replace("_", " ").replace("-", " ")
    if "receiv" in normalized and "stream" in normalized:
        return "Drafting the response"
    if "waiting" in normalized and ("stream" in normalized or "response" in normalized):
        return "Waiting for the current response step to finish"
    if raw:
        return _telegram_checkpoint_tool_labels(raw)[0]
    return ""


def _format_telegram_model_checkpoint(
    elapsed_mins,
    updates,
    *,
    task=None,
    completed=None,
    current=None,
    activity=None,
):
    """Merge real commentary with specific factual lifecycle progress.

    An empty string is intentional: the caller skips that scheduled update and
    preserves the cursor until real commentary or an observable milestone is
    available. A silent interval is preferable to fake specificity.
    """
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
    for item in completed or []:
        clean = " ".join(str(item or "").split()).rstrip(".")
        if clean and clean not in bullets and len(bullets) < 4:
            bullets.append(clean)
    current_label = ""
    for item in reversed(current or []):
        current_label = " ".join(str(item or "").split()).rstrip(".")
        if current_label:
            break
    if not current_label:
        current_label = _telegram_checkpoint_activity_label(activity).rstrip(".")
    if current_label and current_label not in bullets:
        if len(bullets) >= 4:
            bullets = bullets[-3:]
        bullets.append(f"Now: {{current_label}}")
    minutes = max(0, int(elapsed_mins or 0))
    task_label = " ".join(str(task or "").split())[:160]
    if task_label:
        lines = [f"{{minutes}} minutes in on {{task_label}} — quick update:"]
    else:
        lines = [f"{{minutes}} minutes in — quick update:"]
    if not bullets:
        return ""
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
        (TURN_CONTEXT_ANCHOR, TURN_CONTEXT_REPLACEMENT, "turn context"),
        (
            PROGRESS_CALLBACK_ANCHOR,
            PROGRESS_CALLBACK_REPLACEMENT,
            "tool checkpoint capture",
        ),
        (INTERIM_ANCHOR, INTERIM_REPLACEMENT, "commentary capture"),
        (
            CALLBACK_ASSIGNMENT_ANCHOR,
            CALLBACK_ASSIGNMENT_REPLACEMENT,
            "commentary callback assignment",
        ),
        (
            TOOL_PROGRESS_ASSIGNMENT_ANCHOR,
            TOOL_PROGRESS_ASSIGNMENT_REPLACEMENT,
            "tool progress callback assignment",
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
