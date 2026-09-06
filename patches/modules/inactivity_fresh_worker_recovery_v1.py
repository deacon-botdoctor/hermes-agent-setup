#!/usr/bin/env python3
"""Replace raw inactivity failures with one guarded fresh-worker recovery."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

MARKER = "HERMES_INACTIVITY_FRESH_WORKER_RECOVERY_v1"
METADATA_KEY = "inactivity_recovery_v1"
BACKUP_SUFFIX = ".bak-pre-inactivity-fresh-worker-recovery-v1"

SESSION_METHOD_ANCHOR = "    def set_model_override(\n"
SESSION_METHODS = f'''    # [{MARKER}] Small durable compare-and-set state for one timeout
    # recovery chain. SessionEntry.metadata is already persisted by the routing
    # store, so this deliberately adds no parallel ledger or schema.
    def record_inactivity_checkpoint(
        self, session_key: str, message_id: str
    ) -> bool:
        if not message_id:
            return False
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None:
                return False
            state = dict(entry.metadata.get("{METADATA_KEY}") or {{}})
            state["checkpoint_message_id"] = str(message_id)
            entry.metadata["{METADATA_KEY}"] = state
            entry.updated_at = _now()
            self._save()
            return True

    def claim_inactivity_recovery(self, session_key: str) -> Dict[str, Any]:
        """Atomically spend the sole recovery attempt for the active turn."""
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None or entry.suspended or not entry.active_turn_token:
                return {{"decision": "terminal", "reason": "turn_not_owned"}}
            state = dict(entry.metadata.get("{METADATA_KEY}") or {{}})
            if state.get("status") in {{"recovering", "terminal"}}:
                state["status"] = "terminal"
                state["reason"] = "attempt_exhausted"
                state["terminal_at"] = _now().isoformat()
                entry.metadata["{METADATA_KEY}"] = state
                entry.updated_at = _now()
                self._save()
                return {{"decision": "terminal", **state}}
            state.update(
                {{
                    "status": "recovering",
                    "attempt": 1,
                    "origin_turn_token": entry.active_turn_token,
                    "claimed_at": _now().isoformat(),
                }}
            )
            entry.metadata["{METADATA_KEY}"] = state
            entry.updated_at = _now()
            self._save()
            return {{"decision": "recover", **state}}

    def mark_inactivity_recovery_terminal(
        self, session_key: str, reason: str
    ) -> bool:
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None:
                return False
            state = dict(entry.metadata.get("{METADATA_KEY}") or {{}})
            state.update(
                {{
                    "status": "terminal",
                    "reason": str(reason or "recovery_stopped")[:120],
                    "terminal_at": _now().isoformat(),
                }}
            )
            entry.metadata["{METADATA_KEY}"] = state
            entry.updated_at = _now()
            self._save()
            return True

    def clear_inactivity_recovery(self, session_key: str) -> bool:
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None or "{METADATA_KEY}" not in entry.metadata:
                return False
            entry.metadata.pop("{METADATA_KEY}", None)
            entry.updated_at = _now()
            self._save()
            return True

'''

RUN_METHOD_ANCHOR = """    async def _run_startup_resume_event(
"""
RUN_METHODS = f'''    # [{MARKER}]
    async def _edit_inactivity_recovery_card(
        self, source, session_key: str, content: str
    ) -> bool:
        """Edit the existing Telegram checkpoint; never fan out after edit failure."""
        adapter = self._adapter_for_source(source)
        if adapter is None:
            return False
        try:
            state = await self.async_session_store.get_session_metadata(
                session_key, "{METADATA_KEY}", {{}}
            )
        except Exception:
            state = {{}}
        state = state if isinstance(state, dict) else {{}}
        message_id = str(state.get("checkpoint_message_id") or "")
        if message_id and source.platform == Platform.TELEGRAM:
            try:
                result = await adapter.edit_message(
                    source.chat_id, message_id, content
                )
                return bool(result and getattr(result, "success", False))
            except Exception as exc:
                logger.debug("Inactivity recovery card edit failed: %s", exc)
                return False
        try:
            result = await adapter.send(
                source.chat_id,
                content,
                metadata=_non_conversational_metadata(
                    self._thread_metadata_for_source(source),
                    platform=source.platform,
                ),
            )
        except Exception as exc:
            logger.debug("Inactivity recovery card send failed: %s", exc)
            return False
        accepted = bool(result and getattr(result, "success", False))
        new_message_id = getattr(result, "message_id", None) if result else None
        if accepted and new_message_id and source.platform == Platform.TELEGRAM:
            try:
                await self.async_session_store.record_inactivity_checkpoint(
                    session_key, str(new_message_id)
                )
            except Exception:
                logger.debug("Could not persist inactivity recovery card id", exc_info=True)
        return accepted

    async def _stop_inactivity_recovery(
        self, source, session_key: str, reason: str
    ) -> None:
        try:
            await self.async_session_store.mark_inactivity_recovery_terminal(
                session_key, reason
            )
        except Exception:
            logger.warning(
                "Could not persist terminal inactivity recovery state for %s",
                session_key,
                exc_info=True,
            )
        await self._edit_inactivity_recovery_card(
            source,
            session_key,
            "Update:\\n"
            "• I kept the confirmed work.\\n"
            "• I stopped before repeating an uncertain step.",
        )

    async def _run_inactivity_recovery_after_release(
        self, source, session_key: str, worker_done
    ) -> None:
        """Wait for the abandoned worker, then dispatch one fresh internal turn."""
        deadline = asyncio.get_running_loop().time() + 30.0
        while not worker_done.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        if not worker_done.is_set():
            await self._stop_inactivity_recovery(
                source, session_key, "abandoned_worker_still_running"
            )
            return

        # The outer handler owns the durable token, runner slot, and turn lease.
        # All three must unwind before the replacement is admitted.
        release_deadline = asyncio.get_running_loop().time() + 5.0
        while self._is_session_running(session_key):
            if asyncio.get_running_loop().time() >= release_deadline:
                await self._stop_inactivity_recovery(
                    source, session_key, "session_slot_not_released"
                )
                return
            await asyncio.sleep(0.05)

        try:
            state = await self.async_session_store.get_session_metadata(
                session_key, "{METADATA_KEY}", {{}}
            )
        except Exception:
            state = {{}}
        if not isinstance(state, dict) or state.get("status") != "recovering":
            return  # A real user message or lifecycle boundary superseded recovery.

        try:
            if not self._is_user_authorized(source):
                await self.async_session_store.mark_inactivity_recovery_terminal(
                    session_key, "owner_not_authorized"
                )
                return
        except Exception:
            await self.async_session_store.mark_inactivity_recovery_terminal(
                session_key, "authorization_check_failed"
            )
            return

        adapter = self._adapter_for_source(source)
        if adapter is None:
            await self._stop_inactivity_recovery(
                source, session_key, "adapter_unavailable"
            )
            return

        # The old worker is gone and no turn owns the slot. Remove its cached
        # AIAgent so the continuation constructs a genuinely fresh worker.
        self._evict_cached_agent(session_key)
        recovery_state = self._session_state(session_key)
        if self._is_session_running(session_key):
            return
        recovery_state.turn.agent = _AGENT_PENDING_SENTINEL
        recovery_state.turn.started_ts = time.time()
        self._persist_active_agents()

        event = MessageEvent(
            text=(
                "[System note: The prior foreground turn reached its inactivity "
                "safety boundary. Continue the same saved user request from the "
                "durable conversation history. Treat only persisted transcript "
                "rows and completed tool results as confirmed. An unconfirmed "
                "external action may already have happened: inspect current state "
                "or use a stable idempotency key before any write, and never blindly "
                "repeat the old tool call. Finish the request or report the exact "
                "remaining blocker.]"
            ),
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        setattr(event, "_inactivity_recovery_v1", True)
        try:
            await self._run_startup_resume_event(adapter, event, session_key)
        except Exception:
            logger.exception("Fresh-worker inactivity recovery failed for %s", session_key)
            await self._stop_inactivity_recovery(
                source, session_key, "fresh_worker_dispatch_failed"
            )

    def _schedule_inactivity_recovery(
        self, source, session_key: str, worker_done
    ) -> None:
        task = asyncio.create_task(
            self._run_inactivity_recovery_after_release(
                source, session_key, worker_done
            ),
            name=f"gateway-inactivity-recovery-{{session_key[:24]}}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

'''

REAL_INBOUND_ANCHOR = """        _quick_key = self._session_key_for_source(source)
        _up_state = self._peek_session_state(_quick_key)
"""
REAL_INBOUND_REPLACEMENT = (
    REAL_INBOUND_ANCHOR
    + f"""
        # [{MARKER}] A real inbound turn supersedes any completed or
        # pending timeout-recovery chain. Internal recovery events retain it.
        if not is_internal:
            try:
                await self.async_session_store.clear_inactivity_recovery(_quick_key)
            except Exception:
                logger.debug("Could not clear prior inactivity recovery state", exc_info=True)
"""
)
REAL_INBOUND_CURRENT_ANCHOR = """        _quick_key = self._session_key_for_source(source)
        allow_gateway_control = event.allow_gateway_control
        _up_state = self._peek_session_state(_quick_key)
"""
REAL_INBOUND_CURRENT_REPLACEMENT = (
    """        _quick_key = self._session_key_for_source(source)
"""
    + f"""        # [{MARKER}] A real inbound turn supersedes any completed or
        # pending timeout-recovery chain. Internal recovery events retain it.
        if not is_internal:
            try:
                await self.async_session_store.clear_inactivity_recovery(_quick_key)
            except Exception:
                logger.debug("Could not clear prior inactivity recovery state", exc_info=True)
        allow_gateway_control = event.allow_gateway_control
        _up_state = self._peek_session_state(_quick_key)
"""
)

HEARTBEAT_INIT_ANCHOR = "            _heartbeat_msg_id: Optional[str] = None\n"
HEARTBEAT_INIT_REPLACEMENT = f'''            # [{MARKER}] A recovery turn inherits the previous turn's
            # checkpoint bubble so every status remains one edit-in-place card.
            try:
                _recovery_card_state = await self.async_session_store.get_session_metadata(
                    session_key, "{METADATA_KEY}", {{}}
                )
            except Exception:
                _recovery_card_state = {{}}
            _heartbeat_msg_id: Optional[str] = str(
                (_recovery_card_state or {{}}).get("checkpoint_message_id") or ""
            ) or None
            if _heartbeat_msg_id and _cleanup_progress:
                _cleanup_msg_ids.append(_heartbeat_msg_id)
'''

HEARTBEAT_RECORD_ANCHOR = """                            _heartbeat_msg_id = str(_notify_res.message_id)
                            if _cleanup_progress:
                                _cleanup_msg_ids.append(_heartbeat_msg_id)
"""
HEARTBEAT_RECORD_REPLACEMENT = """                            _heartbeat_msg_id = str(_notify_res.message_id)
                            try:
                                await self.async_session_store.record_inactivity_checkpoint(
                                    session_key, _heartbeat_msg_id
                                )
                            except Exception:
                                logger.debug("Could not persist checkpoint card id", exc_info=True)
                            if _cleanup_progress:
                                _cleanup_msg_ids.append(_heartbeat_msg_id)
"""

TIMEOUT_RESPONSE_ANCHOR = """                response = {
                    "final_response": "\\n".join(_diag_lines),
                    "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                    "api_calls": _iter_n,
                    "tools": tools_holder[0] or [],
                    "history_offset": 0,
                    "failed": True,
                }
"""
TIMEOUT_RESPONSE_REPLACEMENT = f"""                # [{MARKER}] Spend one durable attempt and keep the raw
                # timeout diagnostic in logs only. The outer handler persists the
                # original user row, then either dispatches a fresh worker or stops.
                try:
                    _recovery_claim = await self.async_session_store.claim_inactivity_recovery(
                        session_key
                    )
                except Exception:
                    logger.exception("Could not claim inactivity recovery for %s", session_key)
                    _recovery_claim = {{"decision": "terminal", "reason": "claim_failed"}}
                _recovery_scheduled = _recovery_claim.get("decision") == "recover"
                if _recovery_scheduled:
                    await self._edit_inactivity_recovery_card(
                        source,
                        session_key,
                        "Quick update:\\n"
                        "• The last step stopped responding.\\n"
                        "• Now: Resuming from the last confirmed point.",
                    )
                else:
                    await self._stop_inactivity_recovery(
                        source,
                        session_key,
                        str(_recovery_claim.get("reason") or "attempt_exhausted"),
                    )

                response = {{
                    "final_response": "",
                    "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                    "api_calls": _iter_n,
                    "tools": tools_holder[0] or [],
                    "history_offset": 0,
                    "failed": True,
                    "inactivity_recovery_scheduled": _recovery_scheduled,
                    "inactivity_recovery_terminal": not _recovery_scheduled,
                    "_inactivity_recovery_worker_done": _turn_worker_done,
                }}
"""

OUTER_RESULT_ANCHOR = """            await self._refresh_agent_cache_message_count(
                session_key, session_entry.session_id
            )

            # Intentional silence is a delivery decision, not a transcript
"""
OUTER_RESULT_REPLACEMENT = f"""            await self._refresh_agent_cache_message_count(
                session_key, session_entry.session_id
            )

            # [{MARKER}] Persistence is complete. Suppress the raw timeout result
            # and let the outer finally release the token/slot before recovery runs.
            if agent_result.get("inactivity_recovery_scheduled"):
                self._schedule_inactivity_recovery(
                    source,
                    session_key,
                    agent_result.get("_inactivity_recovery_worker_done"),
                )
                return None
            if agent_result.get("inactivity_recovery_terminal"):
                return None
            try:
                await self.async_session_store.clear_inactivity_recovery(session_key)
            except Exception:
                logger.debug("Could not clear inactivity recovery state", exc_info=True)

            # Intentional silence is a delivery decision, not a transcript
"""


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    if source.count(anchor) != 1:
        raise RuntimeError(f"inactivity recovery {label} anchor drift")
    return source.replace(anchor, replacement, 1)


def _replace_real_inbound_reset(source: str) -> str:
    if source.count(REAL_INBOUND_ANCHOR) == 1:
        return source.replace(REAL_INBOUND_ANCHOR, REAL_INBOUND_REPLACEMENT, 1)
    if source.count(REAL_INBOUND_CURRENT_ANCHOR) == 1:
        return source.replace(
            REAL_INBOUND_CURRENT_ANCHOR,
            REAL_INBOUND_CURRENT_REPLACEMENT,
            1,
        )
    raise RuntimeError("inactivity recovery real inbound reset anchor drift")


def _patch_native_inactivity_recovery(root: Path) -> bool:
    """Attach the existing one-attempt policy to native timeout/persistence owners."""
    paths = [root / "gateway" / name for name in ("session.py", "run_turn.py", "run_inbound.py")]
    originals = {path: path.read_text(encoding="utf-8") for path in paths}
    if all(MARKER in value for value in originals.values()):
        return False
    if any(MARKER in value for value in originals.values()):
        raise RuntimeError("partial native inactivity recovery requires clean rebuild")
    session, turn, inbound = (originals[path] for path in paths)
    if "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2" not in turn:
        raise RuntimeError("native inactivity recovery requires Telegram organic checkpoints v2")
    # Metadata receipts must not refresh the native user-activity/reset clock.
    session_methods = "\n".join(line for line in SESSION_METHODS.split("\n") if "entry.updated_at = _now()" not in line)
    session = _replace_once(session, "    def set_model_override(", session_methods + "    def set_model_override(", "native session methods")
    methods = RUN_METHODS.replace(
        '        adapter = self._adapter_for_source(source)\n',
        '        from gateway.run import _non_conversational_metadata\n        adapter = self._adapter_for_source(source)\n',
    ).replace(
        '        deadline = asyncio.get_running_loop().time() + 30.0\n',
        '        from gateway.run import _AGENT_PENDING_SENTINEL\n        from gateway.platforms.base import MessageType\n        deadline = asyncio.get_running_loop().time() + 30.0\n',
    )
    turn = _replace_once(turn, '    async def _run_agent_inactivity_warning(', methods + '    async def _run_agent_inactivity_warning(', "native recovery methods")
    prepare = f'''    async def _prepare_inactivity_recovery(self, worker, turn_ctx, response):
        # [{MARKER}] Native timeout interruption and diagnostics already ran.
        # Persist/claim once, then defer replacement until outer persistence and
        # native worker/turn ownership have unwound.
        source, session_key = turn_ctx.source, turn_ctx.session_key
        try:
            claim = await self.async_session_store.claim_inactivity_recovery(session_key)
        except Exception:
            logger.exception("Could not claim inactivity recovery for %s", session_key)
            claim = {{"decision": "terminal", "reason": "claim_failed"}}
        scheduled = claim.get("decision") == "recover"
        if scheduled:
            await self._edit_inactivity_recovery_card(
                source, session_key,
                "Quick update:\\n• The last step stopped responding.\\n"
                "• Now: Resuming from the last confirmed point.",
            )
        else:
            await self._stop_inactivity_recovery(source, session_key, str(claim.get("reason") or "attempt_exhausted"))
        return {{**response, "final_response": "", "failed": True,
                "inactivity_recovery_scheduled": scheduled,
                "inactivity_recovery_terminal": not scheduled,
                "_inactivity_recovery_worker_done": worker.worker_done}}

'''
    turn = _replace_once(turn, '    def _run_agent_timeout_result(', prepare + '    def _run_agent_timeout_result(', "native prepare")
    turn = _replace_once(turn,
        '        return self._run_agent_timeout_result(worker, turn_ctx)\n',
        '        response = self._run_agent_timeout_result(worker, turn_ctx)\n        return await self._prepare_inactivity_recovery(worker, turn_ctx, response)\n', "native timeout handoff")
    deliver = '        # Intentional silence is a delivery decision: the [SILENT] turn stays persisted (alternation).\n'
    turn = _replace_once(turn, deliver, f'''        # [{MARKER}] This phase runs only after native transcript persistence.
        if agent_result.get("inactivity_recovery_scheduled"):
            self._schedule_inactivity_recovery(source, session_key, agent_result["_inactivity_recovery_worker_done"])
            return None
        if agent_result.get("inactivity_recovery_terminal"):
            return None
        try:
            await self.async_session_store.clear_inactivity_recovery(session_key)
        except Exception:
            logger.debug("Could not clear inactivity recovery state", exc_info=True)

''' + deliver, "native post-persistence delivery")
    init = '        _heartbeat_msg_id = None\n'
    turn = _replace_once(turn, init, f'''        # [{MARKER}] Reuse only an accepted checkpoint ID from this recovery chain.
        try:
            _recovery_card_state = await self.async_session_store.get_session_metadata(session_key, "{METADATA_KEY}", {{}})
        except Exception:
            _recovery_card_state = {{}}
        _heartbeat_msg_id = str((_recovery_card_state or {{}}).get("checkpoint_message_id") or "") or None
        if _heartbeat_msg_id and turn_ctx._cleanup_progress:
            turn_ctx._cleanup_msg_ids.append(_heartbeat_msg_id)
''', "native checkpoint inheritance")
    record = '                        _heartbeat_msg_id = str(_notify_res.message_id)\n'
    notifier_start = turn.index("    async def _run_agent_notify_long_running(")
    notifier_end = turn.index("    async def _run_agent_inner(", notifier_start)
    notifier = turn[notifier_start:notifier_end]
    notifier = _replace_once(notifier, record, record + '''                        try:
                            await self.async_session_store.record_inactivity_checkpoint(session_key, _heartbeat_msg_id)
                        except Exception:
                            logger.debug("Could not persist checkpoint card id", exc_info=True)
''', "native checkpoint receipt")
    turn = turn[:notifier_start] + notifier + turn[notifier_end:]
    reset = '        _quick_key = self._session_key_for_source(source)\n'
    inbound = _replace_once(inbound, reset, reset + f'''        # [{MARKER}] Real input supersedes the previous recovery chain.
        if not is_internal:
            try:
                await self.async_session_store.clear_inactivity_recovery(_quick_key)
            except Exception:
                logger.debug("Could not clear prior inactivity recovery state", exc_info=True)
''', "native inbound reset")
    changed = dict(zip(paths, (session, turn, inbound)))
    for value in changed.values():
        ast.parse(value)
    try:
        for path, value in changed.items():
            path.write_text(value, encoding="utf-8")
    except Exception:
        for path, value in originals.items():
            path.write_text(value, encoding="utf-8")
        raise
    return True


def patch_inactivity_fresh_worker_recovery_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    if (root / "gateway/run_turn.py").is_file():
        return _patch_native_inactivity_recovery(root)
    session_py = root / "gateway" / "session.py"
    run_py = root / "gateway" / "run.py"
    session_original = session_py.read_text(encoding="utf-8")
    run_original = run_py.read_text(encoding="utf-8")

    session_marked = MARKER in session_original
    run_marked = MARKER in run_original
    if session_marked or run_marked:
        if not (session_marked and run_marked):
            raise RuntimeError("partial inactivity recovery patch requires clean rebuild")
        return False
    if "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2" not in run_original:
        raise RuntimeError("inactivity recovery requires Telegram organic checkpoints v2")

    session_patched = _replace_once(
        session_original,
        SESSION_METHOD_ANCHOR,
        SESSION_METHODS + SESSION_METHOD_ANCHOR,
        "session methods",
    )
    run_patched = run_original
    for anchor, replacement, label in (
        (RUN_METHOD_ANCHOR, RUN_METHODS + RUN_METHOD_ANCHOR, "runner methods"),
        (HEARTBEAT_INIT_ANCHOR, HEARTBEAT_INIT_REPLACEMENT, "checkpoint initialization"),
        (HEARTBEAT_RECORD_ANCHOR, HEARTBEAT_RECORD_REPLACEMENT, "checkpoint persistence"),
        (TIMEOUT_RESPONSE_ANCHOR, TIMEOUT_RESPONSE_REPLACEMENT, "timeout result"),
        (OUTER_RESULT_ANCHOR, OUTER_RESULT_REPLACEMENT, "outer recovery dispatch"),
    ):
        run_patched = _replace_once(run_patched, anchor, replacement, label)
    run_patched = _replace_real_inbound_reset(run_patched)

    ast.parse(session_patched)
    ast.parse(run_patched)
    session_backup = Path(str(session_py) + BACKUP_SUFFIX)
    run_backup = Path(str(run_py) + BACKUP_SUFFIX)
    shutil.copy2(session_py, session_backup)
    shutil.copy2(run_py, run_backup)
    try:
        session_py.write_text(session_patched, encoding="utf-8")
        run_py.write_text(run_patched, encoding="utf-8")
    except Exception:
        shutil.copy2(session_backup, session_py)
        shutil.copy2(run_backup, run_py)
        session_backup.unlink(missing_ok=True)
        run_backup.unlink(missing_ok=True)
        raise
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_inactivity_fresh_worker_recovery_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
