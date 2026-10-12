#!/usr/bin/env python3
"""Restore Golden session-reset continuity on the current upstream lifecycle."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_AUTO_RESUME_CONTEXTUAL_RESET_v1"
TIMEZONE_MARKER = "HERMES_SESSION_RESET_TIMEZONE_v1"

LIFECYCLE_IMPORT = "from datetime import datetime, timedelta\n"
LIFECYCLE_ROUTE = '''    def _route_reset_reason(self, entry: SessionEntry) -> Optional[str]:
        """Only explicit suspension replaces a routed conversation; time never does."""
        return "suspended" if entry.suspended else None
'''
LIFECYCLE_HELPERS = f"""
# [{TIMEZONE_MARKER}] Golden keeps the existing raw session_reset policy active
# after upstream 0.21 made its compatibility type intentionally inert.
from hermes_time import now as _hermes_now


def _golden_session_reset_config():
    try:
        from hermes_cli.config import read_raw_config
        value = (read_raw_config() or {{}}).get("session_reset") or {{}}
    except Exception:
        return {{}}
    return value if isinstance(value, dict) else {{}}


def _golden_daily_reset_boundary(at_hour: int, *, policy_now=None) -> datetime:
    current = policy_now or _hermes_now()
    if current.tzinfo is None:
        current = current.astimezone()
    boundary = current.replace(hour=at_hour, minute=0, second=0, microsecond=0)
    if current < boundary:
        boundary -= timedelta(days=1)
    return boundary.astimezone().replace(tzinfo=None)
"""
LIFECYCLE_REPLACEMENT = '''    def _route_reset_reason(self, entry: SessionEntry) -> Optional[str]:
        """Return the configured automatic boundary without changing upstream defaults."""
        if entry.suspended:
            return "suspended"
        if self._has_active_processes_safe(entry.session_key, context="reset"):
            return None
        policy = _golden_session_reset_config()
        mode = str(policy.get("mode") or "none").lower()
        if mode not in {"idle", "daily", "both"}:
            return None
        try:
            idle_minutes = int(policy.get("idle_minutes", 240))
            at_hour = int(policy.get("at_hour", 4))
        except (TypeError, ValueError):
            return None
        if idle_minutes <= 0 or not 0 <= at_hour <= 23:
            return None
        current = _hermes_now()
        if current.tzinfo is None:
            current = current.astimezone()
        host_now = current.astimezone().replace(tzinfo=None)
        if mode in {"idle", "both"} and entry.updated_at < host_now - timedelta(minutes=idle_minutes):
            return "idle"
        if mode in {"daily", "both"} and entry.updated_at < _golden_daily_reset_boundary(
            at_hour, policy_now=current
        ):
            return "daily"
        return None
'''

RUN_HELPERS = """    # [HERMES_AUTO_RESUME_CONTEXTUAL_RESET_v1] helpers
    @staticmethod
    def _auto_resume_contextual_reset_enabled() -> bool:
        try:
            from gateway.run import _load_gateway_config
            return bool(((_load_gateway_config().get("session_reset") or {}).get(
                "auto_resume_previous_if_contextual", False
            )))
        except Exception:
            return False

    @staticmethod
    def _looks_like_contextual_reset_followup(text: str) -> bool:
        import re
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized or normalized.startswith("/") or len(normalized.split()) > 80:
            return False
        if re.search(
            r"\\b(?:without (?:relying on|using)|do not (?:rely on|use)|"
            r"don't (?:rely on|use)|ignore) (?:any )?(?:earlier|previous|prior)\\b",
            normalized,
        ):
            return False
        if re.search(
            r"\\b(?:keep going|pick (?:it )?up|where were we|"
            r"as (?:(?:we|you|i) )?(?:discussed|said)|do that)\\b", normalized,
        ):
            return True
        if re.fullmatch(
            r"(?:(?:please|okay|ok),? |(?:can|could|would) you )?"
            r"(?:continue|try again|finish (?:it|that|this)|"
            r"(?:do )?(?:the )?same (?:one|thing))[?.!?]?", normalized,
        ):
            return True
        return bool(re.search(
            r"\\b(?:(?:earlier|previous|prior) (?:discussion|conversation|message|request|answer|session)|"
            r"(?:you|i|we) (?:said|discussed|explained) (?:earlier|previously)|"
            r"what we (?:said|discussed|decided)|the above (?:message|request|answer))\\b",
            normalized,
        ))
"""
RUN_DECISION = """        _auto_reset_pending = getattr(session_entry, "was_auto_reset", False)
        _auto_reset_reason = getattr(session_entry, "auto_reset_reason", None)
        _auto_reset_previous_session_id = getattr(session_entry, "prev_session_id", None)
        _is_internal_event = bool(
            getattr(event, "internal", False)
            or (getattr(event, "metadata", None) or {}).get("gateway_session_id")
        )
        _defer_contextual_reset_decision_for_internal_event = bool(
            _auto_reset_pending
            and _is_internal_event
            and _auto_reset_reason in {"idle", "daily"}
            and _auto_reset_previous_session_id
            and self._auto_resume_contextual_reset_enabled()
        )
        _auto_resumed_previous = False
        if (
            _auto_reset_pending
            and not _defer_contextual_reset_decision_for_internal_event
            and _auto_reset_reason in {"idle", "daily"}
            and _auto_reset_previous_session_id
            and self._auto_resume_contextual_reset_enabled()
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
                await asyncio.to_thread(
                    self._sync_telegram_topic_binding, source, session_entry,
                    reason="contextual-auto-resume",
                )
        if (
            _auto_reset_pending
            and not _defer_contextual_reset_decision_for_internal_event
            and not _auto_resumed_previous
        ):
            await asyncio.to_thread(
                self._sync_telegram_topic_binding, source, session_entry,
                reason="contextual-auto-reset",
            )
        _skip_telegram_topic_recovery = _auto_reset_pending
"""


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    if source.count(anchor) != 1:
        raise ValueError(f"{label} anchor is not unique")
    return source.replace(anchor, replacement, 1)


def patch_native_session_text(source: str) -> str:
    if TIMEZONE_MARKER in source:
        return source
    import_anchor = ("from datetime import datetime, timedelta, timezone\n"
                     if "from datetime import datetime, timedelta, timezone\n" in source else LIFECYCLE_IMPORT)
    source = _replace_once(
        source,
        import_anchor,
        import_anchor + LIFECYCLE_HELPERS,
        "lifecycle policy helpers",
    )
    return _replace_once(source, LIFECYCLE_ROUTE, LIFECYCLE_REPLACEMENT, "lifecycle reset route")


def patch_native_run_text(source: str) -> str:
    if MARKER in source:
        return source
    source = _replace_once(
        source,
        "    async def _hmwa_resolve_session(self, event, source):\n",
        RUN_HELPERS + "    async def _hmwa_resolve_session(self, event, source):\n",
        "turn helper owner",
    )
    source = _replace_once(
        source,
        "        self._cache_session_source(session_key, source)\n",
        "        self._cache_session_source(session_key, source)\n" + RUN_DECISION,
        "contextual decision",
    )
    source = _replace_once(
        source,
        "        if await asyncio.to_thread(self._is_telegram_topic_lane, source):\n",
        "        if not _skip_telegram_topic_recovery and await asyncio.to_thread(\n"
        "            self._is_telegram_topic_lane, source\n"
        "        ):\n",
        "topic recovery",
    )
    source = _replace_once(
        source,
        "        return source, session_entry, session_key\n",
        "        return source, session_entry, session_key, _defer_contextual_reset_decision_for_internal_event\n",
        "resolve result",
    )
    source = source.replace(
        "Returns ``(source, session_entry, session_key)``",
        "Returns ``(source, session_entry, session_key, preserve_reset_state)``",
        1,
    )
    source = _replace_once(
        source,
        "    async def _hmwa_open_session(self, session_entry, session_key, source):\n",
        "    async def _hmwa_open_session(self, session_entry, session_key, source, *, preserve_reset_state=False):\n",
        "open signature",
    )
    source = _replace_once(
        source,
        "        # Consume was_auto_reset immediately so it cannot re-fire and wipe overrides set between turns.\n",
        "        if preserve_reset_state:\n            return False, False\n"
        "        # Consume was_auto_reset immediately so it cannot re-fire and wipe overrides set between turns.\n",
        "deferred reset",
    )
    source = _replace_once(
        source,
        "    async def _hmwa_prepare_turn(self, event, source, session_entry, "
        "session_key, _quick_key, run_generation):\n",
        "    async def _hmwa_prepare_turn(self, event, source, session_entry, "
        "session_key, _quick_key, run_generation, *, preserve_reset_state=False):\n",
        "prepare signature",
    )
    source = _replace_once(
        source,
        "        _was_auto_reset, _is_new_session = await self._hmwa_open_session("
        "session_entry, session_key, source)\n",
        "        _was_auto_reset, _is_new_session = await self._hmwa_open_session("
        "session_entry, session_key, source, preserve_reset_state=preserve_reset_state)\n",
        "prepare reset",
    )
    source = _replace_once(
        source,
        "        source, session_entry, session_key = resolved\n",
        "        source, session_entry, session_key, preserve_reset_state = resolved\n",
        "resolve unpack",
    )
    return _replace_once(
        source,
        "            event, source, session_entry, session_key, _quick_key, "
        "run_generation,\n        )\n"
        "        if not isinstance(prepared, self._PreparedTurn):\n",
        "            event, source, session_entry, session_key, _quick_key, "
        "run_generation,\n"
        "            preserve_reset_state=preserve_reset_state,\n"
        "        )\n"
        "        if not isinstance(prepared, self._PreparedTurn):\n",
        "prepare caller",
    )


def patch_auto_resume_contextual_reset_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    targets = {
        root / "gateway/session_lifecycle.py": patch_native_session_text,
        root / "gateway/run_turn.py": patch_native_run_text,
    }
    if not all(path.is_file() for path in targets):
        return False
    changes = {}
    for path, transform in targets.items():
        original = path.read_text(encoding="utf-8")
        updated = transform(original)
        compile(updated, str(path), "exec")
        if updated != original:
            changes[path] = updated
    for path, content in changes.items():
        path.write_text(content, encoding="utf-8")
    return bool(changes)
