#!/usr/bin/env python3
"""Resume the exact prior transcript for contextual post-expiry follow-ups."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

MARKER = "HERMES_AUTO_RESUME_CONTEXTUAL_RESET_v1"

RUN_METHOD_ANCHOR = (
    "    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):\n"
)
RUN_DECISION_ANCHOR = "        self._cache_session_source(session_key, source)\n"
RUN_TOPIC_BINDING_ANCHOR = "        if await asyncio.to_thread(self._is_telegram_topic_lane, source):\n"
RUN_TOPIC_SWITCH_ANCHOR = "                if bound_session_id and bound_session_id != session_entry.session_id:\n"
RUN_RESET_CAPTURE_ANCHOR = '        _was_auto_reset = getattr(session_entry, "was_auto_reset", False)\n'
RUN_NEW_SESSION_ANCHOR = '            or getattr(session_entry, "is_fresh_reset", False)\n        )\n'


RUN_HELPER = '''    # [HERMES_AUTO_RESUME_CONTEXTUAL_RESET_v1] helpers
    @staticmethod
    def _auto_resume_contextual_reset_enabled() -> bool:
        """Return the opt-in post-expiry continuation policy."""
        try:
            cfg = _load_gateway_config()
            return bool(
                ((cfg.get("session_reset") or {}).get(
                    "auto_resume_previous_if_contextual", False
                ))
            )
        except Exception:
            return False

    @staticmethod
    def _looks_like_contextual_reset_followup(text: str) -> bool:
        """Conservatively detect a message that refers to prior dialogue."""
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized or normalized.startswith("/"):
            return False
        words = normalized.split()
        if len(words) > 80:
            return False
        if re.search(
            r"\\b(?:without (?:relying on|using)|do not (?:rely on|use)|"
            r"don't (?:rely on|use)|ignore) (?:any )?(?:earlier|previous|prior)\\b",
            normalized,
        ):
            return False
        strong = re.search(
            r"\\b(?:keep going|pick (?:it )?up|where were we|"
            r"as (?:(?:we|you|i) )?(?:discussed|said)|do that)\\b",
            normalized,
        )
        if strong:
            return True
        if re.fullmatch(
            r"(?:(?:please|okay|ok),? |(?:can|could|would) you )?"
            r"(?:continue|try again|finish (?:it|that|this)|"
            r"(?:do )?(?:the )?same (?:one|thing))[?.!]?",
            normalized,
        ):
            return True
        dialogue_reference = re.search(
            r"\\b(?:(?:earlier|previous|prior) "
            r"(?:discussion|conversation|message|request|answer|session)|"
            r"(?:discussion|conversation|message|request|answer|session) "
            r"(?:earlier|previously)|(?:you|i|we) (?:said|discussed|explained) "
            r"(?:earlier|previously)|what we (?:said|discussed|decided)|"
            r"the above (?:message|request|answer)|"
            r"now that (?:i've|i have|we've|we have|you've|you have) "
            r"explained (?:it|that|this))\\b",
            normalized,
        )
        if dialogue_reference:
            return True
        return bool(re.fullmatch(
            r"(?:what about )?(?:it|that|this|these|those|them|"
            r"that one|this one|those ones|these ones)[?.!]?",
            normalized,
        ))

'''

RUN_DECIDE = """        _auto_reset_pending = getattr(
            session_entry, "was_auto_reset", False
        )
        _is_internal_event = bool(
            getattr(event, "internal", False)
            or (getattr(event, "metadata", None) or {}).get("gateway_session_id")
        )
        _auto_reset_reason = getattr(session_entry, "auto_reset_reason", None)
        _auto_reset_previous_session_id = getattr(
            session_entry, "prev_session_id", None
        )
        _contextual_auto_resume_enabled = (
            self._auto_resume_contextual_reset_enabled()
        )
        # Completion/watch events re-enter through ``handle_message`` as
        # ``internal`` events. They still need their own cascading turn, but
        # must not make (or consume) the user's contextual-resume decision.
        # Returning here silently dropped those events.
        _defer_contextual_reset_decision_for_internal_event = bool(
            _auto_reset_pending
            and _is_internal_event
            and _auto_reset_reason in {"idle", "daily"}
            and _auto_reset_previous_session_id
            and _contextual_auto_resume_enabled
        )
        _was_auto_reset = (
            _auto_reset_pending
            and not _defer_contextual_reset_decision_for_internal_event
        )
        _auto_resumed_previous = False
        if _was_auto_reset:
            if (
                _auto_reset_reason in {"idle", "daily"}
                and _auto_reset_previous_session_id
                and _contextual_auto_resume_enabled
                and (
                    getattr(event, "reply_to_message_id", None)
                    or getattr(event, "reply_to_text", None)
                    or self._looks_like_contextual_reset_followup(event.text)
                )
            ):
                switched = await self.async_session_store.switch_session(
                    session_key, _auto_reset_previous_session_id
                )
                if switched is not None:
                    session_entry = switched
                    _auto_resumed_previous = True
                    _was_auto_reset = False
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source,
                        session_entry,
                        reason="contextual-auto-resume",
                    )
                    logger.info(
                        "contextual auto-resume: restored %s for routing key %s",
                        _auto_reset_previous_session_id,
                        session_key,
                    )
            if not _auto_resumed_previous:
                await asyncio.to_thread(
                    self._sync_telegram_topic_binding,
                    source,
                    session_entry,
                    reason="contextual-auto-reset",
                )
        _skip_telegram_topic_recovery = _auto_reset_pending
"""


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    if source.count(anchor) != 1:
        raise ValueError(f"{label} anchor is not unique")
    return source.replace(anchor, replacement, 1)


def patch_run_text(source: str) -> str:
    if f"# [{MARKER}] helpers" in source:
        return source
    source = _replace_once(source, RUN_METHOD_ANCHOR, RUN_HELPER + RUN_METHOD_ANCHOR, "runner method")
    source = _replace_once(
        source,
        RUN_DECISION_ANCHOR,
        RUN_DECISION_ANCHOR + RUN_DECIDE,
        "auto-reset decision",
    )
    source = _replace_once(
        source,
        RUN_TOPIC_BINDING_ANCHOR,
        "        if (\n"
        "            not _skip_telegram_topic_recovery\n"
        "            and await asyncio.to_thread(self._is_telegram_topic_lane, source)\n"
        "        ):\n",
        "topic binding recovery",
    )
    source = _replace_once(
        source,
        RUN_TOPIC_SWITCH_ANCHOR,
        "                if (\n"
        "                    bound_session_id\n"
        "                    and bound_session_id\n"
        "                    != getattr(\n"
        '                        session_entry, "prev_session_id", None\n'
        "                    )\n"
        "                    and bound_session_id != session_entry.session_id\n"
        "                ):\n",
        "stale topic binding",
    )
    source = _replace_once(
        source,
        RUN_RESET_CAPTURE_ANCHOR,
        "",
        "auto-reset capture",
    )
    return _replace_once(
        source,
        RUN_NEW_SESSION_ANCHOR,
        RUN_NEW_SESSION_ANCHOR[:-1]
        + " and not (\n"
        + "            _auto_resumed_previous\n"
        + "            or _defer_contextual_reset_decision_for_internal_event\n"
        + "        )\n",
        "new-session classification",
    )


def patch_auto_resume_contextual_reset_v1(hermes_dir: Path) -> bool:
    targets = {
        hermes_dir / "gateway" / "run.py": patch_run_text,
    }
    if not all(path.is_file() for path in targets):
        return False
    originals = {path: path.read_text(encoding="utf-8") for path in targets}
    patched = {path: fn(originals[path]) for path, fn in targets.items()}
    changed = [path for path in targets if patched[path] != originals[path]]
    if not changed:
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in changed:
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}-pre-contextual-auto-resume"))
        path.write_text(patched[path], encoding="utf-8")
    return True
