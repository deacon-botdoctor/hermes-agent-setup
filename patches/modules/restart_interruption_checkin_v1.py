#!/usr/bin/env python3
"""Replace synthetic restart auto-resume with one contextual check-in."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

LEGACY_MARKER = "HERMES_RESTART_INTERRUPTION_CHECKIN_v1"
PRIOR_MARKER = "HERMES_RESTART_INTERRUPTION_CHECKIN_v2"
MARKER = "HERMES_RESTART_INTERRUPTION_CHECKIN_v3"
BACKUP_SUFFIX = ".bak-pre-restart-interruption-checkin-v3"
METHOD_ANCHOR = "    def _schedule_resume_pending_sessions(self, platform=None) -> int:\n"
SCHEDULER_DOC_OLD = '''        """Auto-continue fresh restart-interrupted sessions after startup.

        ``resume_pending`` already preserves the transcript AND the existing
        ``_is_resume_pending`` branch in ``_handle_message_with_agent``
        injects a reason-aware recovery system note on the next turn.  This
        method closes the UX gap by synthesizing that next turn once
        adapters are back online — the event text is empty so the existing
        injection path owns the wording and we never double up.

'''
SCHEDULER_DOC_NEW = '''        """Send one contextual check-in for fresh restart-interrupted sessions.

        The client, not a synthetic agent turn, decides whether any unfinished
        work should continue.  This prevents an uncertain action from being
        repeated after a gateway replacement.

'''
SCHEDULE_BLOCK = """            # Claim the session slot *before* spawning the task so that an
            # inbound message arriving between task creation and the task's
            # first await (where _process_message_background sets the real
            # sentinel) sees the slot as occupied and queues behind it
            # instead of spinning up a duplicate AIAgent (#45456).
            self._running_agents[entry.session_key] = _AGENT_PENDING_SENTINEL
            self._running_agents_ts[entry.session_key] = time.time()
            self._persist_active_agents()

            # Empty-text internal event — the _is_resume_pending branch in
            # _handle_message_with_agent prepends the proper reason-aware
            # system note before the turn runs.
            event = MessageEvent(
                text="",
                message_type=MessageType.TEXT,
                source=source,
                internal=True,
            )
            task = asyncio.create_task(
                self._run_startup_resume_event(adapter, event, entry.session_key)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            if getattr(self, "_startup_restore_in_progress", False):
                tasks = getattr(self, "_startup_restore_tasks", None)
                if tasks is None:
                    tasks = []
                    self._startup_restore_tasks = tasks
                tasks.append(task)
            scheduled += 1
"""
SCHEDULE_BLOCK_SESSION_STATE = """            # Claim the session slot *before* spawning the task so that an
            # inbound message arriving between task creation and the task's
            # first await (where _process_message_background sets the real
            # sentinel) sees the slot as occupied and queues behind it
            # instead of spinning up a duplicate AIAgent (#45456).
            _resume_state = self._session_state(entry.session_key)
            _resume_state.turn.agent = _AGENT_PENDING_SENTINEL
            _resume_state.turn.started_ts = time.time()
            self._persist_active_agents()

            # Empty-text internal event — the _is_resume_pending branch in
            # _handle_message_with_agent prepends the proper reason-aware
            # system note before the turn runs.
            event = MessageEvent(
                text="",
                message_type=MessageType.TEXT,
                source=source,
                internal=True,
            )
            task = asyncio.create_task(
                self._run_startup_resume_event(adapter, event, entry.session_key)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            if getattr(self, "_startup_restore_in_progress", False):
                tasks = getattr(self, "_startup_restore_tasks", None)
                if tasks is None:
                    tasks = []
                    self._startup_restore_tasks = tasks
                tasks.append(task)
            scheduled += 1
"""
CHECKIN_BLOCK = """            # [HERMES_RESTART_INTERRUPTION_CHECKIN_v3] Claim this check-in
            # in memory so reconnect/startup scans cannot schedule duplicates
            # in one process.  The durable resume marker is cleared only after
            # accepted delivery; a send failure remains retryable.
            checkins = getattr(self, "_restart_interruption_checkins", None)
            if checkins is None:
                checkins = set()
                self._restart_interruption_checkins = checkins
            if entry.session_key in checkins:
                continue
            checkins.add(entry.session_key)

            task = asyncio.create_task(
                self._send_restart_interruption_checkin(adapter, source, entry)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(
                lambda _task, key=entry.session_key: checkins.discard(key)
            )
            if getattr(self, "_startup_restore_in_progress", False):
                tasks = getattr(self, "_startup_restore_tasks", None)
                if tasks is None:
                    tasks = []
                    self._startup_restore_tasks = tasks
                tasks.append(task)
            scheduled += 1
"""
RETAINING_V1_CHECKIN_BLOCK = CHECKIN_BLOCK.replace(MARKER, LEGACY_MARKER)
LEGACY_CHECKIN_BLOCK = """            # [HERMES_RESTART_INTERRUPTION_CHECKIN_v1] Clear the durable
            # marker before the send.  A crash during delivery can lose this
            # notice, but cannot create a second agent turn or duplicate an
            # uncertain external action after the replacement process starts.
            try:
                with self.session_store._lock:  # noqa: SLF001
                    if not entry.resume_pending:
                        continue
                    entry.resume_pending = False
                    entry.resume_reason = None
                    self.session_store._save()  # noqa: SLF001
            except Exception as exc:
                logger.warning("Failed to claim restart recovery check-in for %s: %s", entry.session_key, exc)
                continue

            task = asyncio.create_task(
                self._send_restart_interruption_checkin(adapter, source, entry)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            if getattr(self, "_startup_restore_in_progress", False):
                tasks = getattr(self, "_startup_restore_tasks", None)
                if tasks is None:
                    tasks = []
                    self._startup_restore_tasks = tasks
                tasks.append(task)
            scheduled += 1
"""
HELPER_PREFIX = '''    # [HERMES_RESTART_INTERRUPTION_CHECKIN_v3]
    def _restart_interruption_summary(self, session_id: str) -> str:
        """Read the last client request without adding another source of truth."""
        try:
            history = self.session_store.load_transcript(session_id)
        except Exception:
            history = []
        for item in reversed(history or []):
            if str(item.get("role") or "").lower() != "user":
                continue
            content = item.get("content") or item.get("text") or ""
            if not isinstance(content, str):
                continue
            summary = " ".join(content.split())
            if summary:
                summary = _redact_gateway_user_facing_secrets(summary)
                summary = re.sub(
                    r"(?i)\\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd|token)\\s*([=:])\\s*[^\\s,;]+",
                    lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
                    summary,
                )
                return summary[:180].rstrip() + ("…" if len(summary) > 180 else "")
        return "your earlier request"

    async def _send_restart_interruption_checkin(self, adapter, source, entry) -> None:
        """Ask for a decision; never resume the interrupted task itself."""
        summary = self._restart_interruption_summary(entry.session_id)
        task_text = f"“{summary}”" if summary != "your earlier request" else summary
        message = (
            f"I was working on {task_text} and got interrupted before I could "
            "confirm the last step. I didn’t repeat it. Do you still need me to finish it?"
        )
'''
LEGACY_SEND_TAIL = """        try:
            result = await adapter.send(
                source.chat_id,
                message,
                metadata=self._thread_metadata_for_source(source),
            )
            if result is not None and getattr(result, "success", True) is False:
                logger.warning("Restart recovery check-in was not accepted for %s", entry.session_key)
        except Exception as exc:
            logger.warning("Restart recovery check-in failed for %s: %s", entry.session_key, exc)

"""
RETAINING_V1_SEND_TAIL = """        try:
            result = await adapter.send(
                source.chat_id,
                message,
                metadata=self._thread_metadata_for_source(source),
            )
            if result is not None and getattr(result, "success", True) is False:
                logger.warning("Restart recovery check-in was not accepted for %s", entry.session_key)
                return
        except Exception as exc:
            logger.warning("Restart recovery check-in failed for %s: %s", entry.session_key, exc)
            return
        try:
            with self.session_store._lock:  # noqa: SLF001
                if not entry.resume_pending:
                    return
                entry.resume_pending = False
                entry.resume_reason = None
                self.session_store._save()  # noqa: SLF001
        except Exception as exc:
            logger.warning("Failed to commit restart recovery check-in for %s: %s", entry.session_key, exc)

"""
STALE_RETRY_SEND_TAIL = """        delay = 1.0
        while entry.resume_pending:
            try:
                result = await adapter.send(
                    source.chat_id,
                    message,
                    metadata=self._thread_metadata_for_source(source),
                )
                if result is None or getattr(result, "success", True):
                    break
                logger.warning("Restart recovery check-in was not accepted for %s", entry.session_key)
            except Exception as exc:
                logger.warning("Restart recovery check-in failed for %s: %s", entry.session_key, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        if not entry.resume_pending:
            return
        try:
            with self.session_store._lock:  # noqa: SLF001
                if not entry.resume_pending:
                    return
                entry.resume_pending = False
                entry.resume_reason = None
                self.session_store._save()  # noqa: SLF001
        except Exception as exc:
            logger.warning("Failed to commit restart recovery check-in for %s: %s", entry.session_key, exc)

"""
SYNC_RETRY_SEND_TAIL = """        delay = 1.0
        while entry.resume_pending:
            try:
                current_adapter = self._adapter_for_source(source)
                if current_adapter is None:
                    logger.warning(
                        "Restart recovery check-in adapter is unavailable for %s",
                        entry.session_key,
                    )
                else:
                    result = await current_adapter.send(
                        source.chat_id,
                        message,
                        metadata=self._thread_metadata_for_source(source),
                    )
                    if result is None or getattr(result, "success", True):
                        break
                    logger.warning("Restart recovery check-in was not accepted for %s", entry.session_key)
            except Exception as exc:
                logger.warning("Restart recovery check-in failed for %s: %s", entry.session_key, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        if not entry.resume_pending:
            return
        try:
            with self.session_store._lock:  # noqa: SLF001
                if not entry.resume_pending:
                    return
                entry.resume_pending = False
                entry.resume_reason = None
                self.session_store._save()  # noqa: SLF001
        except Exception as exc:
            logger.warning("Failed to commit restart recovery check-in for %s: %s", entry.session_key, exc)

"""
RETRY_SEND_TAIL = """        delay = 1.0
        while entry.resume_pending:
            try:
                current_adapter = self._adapter_for_source(source)
                if current_adapter is None:
                    logger.warning(
                        "Restart recovery check-in adapter is unavailable for %s",
                        entry.session_key,
                    )
                else:
                    result = await current_adapter.send(
                        source.chat_id,
                        message,
                        metadata=self._thread_metadata_for_source(source),
                    )
                    if result is None or getattr(result, "success", True):
                        break
                    logger.warning("Restart recovery check-in was not accepted for %s", entry.session_key)
            except Exception as exc:
                logger.warning("Restart recovery check-in failed for %s: %s", entry.session_key, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        if not entry.resume_pending:
            return
        try:
            cleared = await self.async_session_store.clear_resume_pending(entry.session_key)
            if cleared:
                # Keep this scheduler's captured entry coherent with the
                # durable store even when a test/double facade returns a
                # successful clear without mutating the same object.
                entry.resume_pending = False
                entry.resume_reason = None
                entry.last_resume_marked_at = None
        except Exception as exc:
            logger.warning("Failed to commit restart recovery check-in for %s: %s", entry.session_key, exc)

"""
FACADE_RETRY_SEND_TAIL = RETRY_SEND_TAIL.replace(
    """            cleared = await self.async_session_store.clear_resume_pending(entry.session_key)
            if cleared:
                # Keep this scheduler's captured entry coherent with the
                # durable store even when a test/double facade returns a
                # successful clear without mutating the same object.
                entry.resume_pending = False
                entry.resume_reason = None
                entry.last_resume_marked_at = None
""",
    """            await self.async_session_store.clear_resume_pending(entry.session_key)
""",
)
HELPER = HELPER_PREFIX + RETRY_SEND_TAIL

DURABLE_DRAIN_METHOD_ANCHOR = "    def _schedule_post_startup_drain(self) -> None:\n"
POST_STARTUP_DRAIN_BLOCK = """    def _schedule_post_startup_drain(self) -> None:
        current = getattr(self, "_post_startup_drain_task", None)
        if current is not None and not current.done():
            return

        async def _run() -> None:
            try:
                await self._drain_persisted_drain_inbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Post-startup durable inbox drain failed")
            finally:
                if (
                    getattr(self, "_post_startup_drain_task", None)
                    is asyncio.current_task()
                ):
                    self._post_startup_drain_task = None

        task = asyncio.create_task(_run(), name="gateway-post-startup-drain")
        self._post_startup_drain_task = task
        background_tasks = getattr(self, "_background_tasks", None)
        if not isinstance(background_tasks, set):
            background_tasks = set()
            self._background_tasks = background_tasks
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
"""
POST_STARTUP_DRAIN_BLOCK_WITH_RESCHEDULE = """    def _schedule_post_startup_drain(self) -> None:
        current = getattr(self, "_post_startup_drain_task", None)
        if current is not None and not current.done():
            self._post_startup_drain_reschedule = True
            return
        self._post_startup_drain_reschedule = False

        async def _run() -> None:
            cancelled = False
            try:
                await self._drain_persisted_drain_inbox()
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception:
                logger.exception("Post-startup durable inbox drain failed")
            finally:
                if (
                    getattr(self, "_post_startup_drain_task", None)
                    is asyncio.current_task()
                ):
                    self._post_startup_drain_task = None
                    if (
                        not cancelled
                        and getattr(self, "_post_startup_drain_reschedule", False)
                    ):
                        self._post_startup_drain_reschedule = False
                        self._schedule_post_startup_drain()

        task = asyncio.create_task(_run(), name="gateway-post-startup-drain")
        self._post_startup_drain_task = task
        background_tasks = getattr(self, "_background_tasks", None)
        if not isinstance(background_tasks, set):
            background_tasks = set()
            self._background_tasks = background_tasks
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
"""
PRIMARY_RECONNECT_SUCCESS_BLOCK = """\
                        logger.info("✓ %s reconnected successfully", platform.value)

                        # Rebuild channel directory with the new adapter
"""
PRIMARY_RECONNECT_SUCCESS_BLOCK_WITH_DRAIN = """\
                        logger.info("✓ %s reconnected successfully", platform.value)
                        self._schedule_post_startup_drain()

                        # Rebuild channel directory with the new adapter
"""
PROFILE_RECONNECT_SUCCESS_BLOCK = """                            logger.info(
                                "✓ %s reconnected (profile: %s)",
                                platform.value,
                                profile_name,
                            )
                            return
"""
PROFILE_RECONNECT_SUCCESS_BLOCK_WITH_DRAIN = """                            logger.info(
                                "✓ %s reconnected (profile: %s)",
                                platform.value,
                                profile_name,
                            )
                            self._schedule_post_startup_drain()
                            return
"""

STARTUP_WAIT_BLOCK_OLD = """        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
"""
STARTUP_WAIT_BLOCK_NEW = """        if tasks:
            try:
                results = await asyncio.wait_for(
                    asyncio.shield(
                        asyncio.gather(*tasks, return_exceptions=True)
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Restart recovery check-in is still pending; continuing startup"
                )
                results = []
"""
NATIVE_BOUNDED_STARTUP_WAIT = "                done, pending = await asyncio.wait(tasks, timeout=timeout)\n"

TEST_REPLACEMENTS = {
    "test_startup_auto_resume_schedules_fresh_pending_sessions": """@pytest.mark.asyncio
async def test_startup_recovery_sends_contextual_checkin_for_fresh_pending_session():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="resume-chat", thread_id="topic-1")
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:group:resume-chat:topic-1",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="group",
        resume_pending=True,
        resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {pending_entry.session_key: pending_entry}
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "finish the long instruction"}
    ]
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*list(runner._background_tasks))

    adapter.handle_message.assert_not_awaited()
    assert len(adapter.sent) == 1
    assert "I didn’t repeat it" in adapter.sent[0]
    assert pending_entry.resume_pending is False
""",
    "test_startup_auto_resume_includes_crash_recovery": """@pytest.mark.asyncio
async def test_startup_recovery_sends_checkin_for_crash_recovery():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="crash-chat")
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:dm:crash-chat",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {pending_entry.session_key: pending_entry}
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*list(runner._background_tasks))

    adapter.handle_message.assert_not_awaited()
    assert len(adapter.sent) == 1
    assert pending_entry.resume_pending is False
""",
    "test_reconnect_reschedules_pending_after_late_platform_connect": """@pytest.mark.asyncio
async def test_reconnect_retries_pending_checkin_after_late_platform_connect():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="late-chat")
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:dm:late-chat",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {pending_entry.session_key: pending_entry}
    runner.adapters = {}
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 0

    runner.adapters = {Platform.TELEGRAM: adapter}
    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 1
    await asyncio.gather(*list(runner._background_tasks))

    adapter.handle_message.assert_not_awaited()
    assert len(adapter.sent) == 1
    assert pending_entry.resume_pending is False
""",
    "test_reconnect_reschedule_is_platform_scoped": """@pytest.mark.asyncio
async def test_reconnect_checkin_retry_is_platform_scoped():
    runner, adapter = make_restart_runner()
    tg_source = make_restart_source(chat_id="tg-chat")
    discord_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dc-chat",
        chat_type="dm",
        user_id="u1",
    )
    tg_entry = SessionEntry(
        session_key="agent:main:telegram:dm:tg-chat",
        session_id="sid-tg",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=tg_source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    discord_entry = SessionEntry(
        session_key="agent:main:discord:dm:dc-chat",
        session_id="sid-dc",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=discord_source,
        platform=Platform.DISCORD,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {
        tg_entry.session_key: tg_entry,
        discord_entry.session_key: discord_entry,
    }

    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 1
    await asyncio.gather(*list(runner._background_tasks))

    assert len(adapter.sent) == 1
    assert tg_entry.resume_pending is False
    assert discord_entry.resume_pending is True
""",
    "test_startup_restore_waits_for_resume_before_final_durable_drain": """@pytest.mark.asyncio
async def test_startup_restore_waits_for_checkin_before_final_durable_drain():
    runner, adapter = make_restart_runner()
    runner._startup_restore_in_progress = True
    runner._startup_restore_tasks = []
    runner._drain_persisted_drain_inbox = AsyncMock(side_effect=[0, 0])

    source = make_restart_source(chat_id="restore-chat")
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:dm:restore-chat",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {pending_entry.session_key: pending_entry}

    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def delayed_send(*_args, **_kwargs):
        send_started.set()
        await release_send.wait()
        return SendResult(success=True, message_id="1")

    adapter.send = delayed_send
    finish_task = asyncio.create_task(runner._finish_startup_restore())
    await asyncio.wait_for(send_started.wait(), timeout=2)

    assert runner._drain_persisted_drain_inbox.await_count == 1
    assert runner._startup_restore_in_progress is True

    release_send.set()
    await asyncio.wait_for(finish_task, timeout=2)

    assert runner._drain_persisted_drain_inbox.await_count == 2
    assert runner._startup_restore_in_progress is False
    assert pending_entry.resume_pending is False
""",
    "test_startup_restore_waits_for_resume_before_draining_inbound": """@pytest.mark.asyncio
async def test_startup_restore_waits_for_checkin_before_draining_inbound():
    runner, adapter = make_restart_runner()
    runner._startup_restore_in_progress = True
    runner._startup_restore_queue = []
    runner._startup_restore_tasks = []

    source = make_restart_source(chat_id="restore-chat")
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:dm:restore-chat",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {pending_entry.session_key: pending_entry}
    adapter.handle_message = AsyncMock()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def delayed_send(*_args, **_kwargs):
        send_started.set()
        await release_send.wait()
        return SendResult(success=True, message_id="1")

    adapter.send = delayed_send
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.wait_for(send_started.wait(), timeout=2)

    inbound = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=source,
    )
    assert await runner._handle_message(inbound) is None
    assert runner._startup_restore_queue == [inbound]

    finish_task = asyncio.create_task(runner._finish_startup_restore())
    await asyncio.sleep(0)
    adapter.handle_message.assert_not_awaited()

    release_send.set()
    await asyncio.wait_for(finish_task, timeout=2)

    adapter.handle_message.assert_awaited_once_with(inbound)
    assert runner._startup_restore_queue == []
    assert runner._startup_restore_in_progress is False
    assert pending_entry.resume_pending is False
""",
    "test_auto_resume_sets_sentinel_before_task_execution": """@pytest.mark.asyncio
async def test_restart_checkin_claims_single_flight_before_delivery():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="race-chat")
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:dm:race-chat",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {pending_entry.session_key: pending_entry}
    release_send = asyncio.Event()

    async def delayed_send(*_args, **_kwargs):
        await release_send.wait()
        return SendResult(success=True, message_id="1")

    adapter.send = delayed_send

    assert runner._schedule_resume_pending_sessions() == 1
    assert pending_entry.session_key in runner._restart_interruption_checkins
    assert runner._schedule_resume_pending_sessions() == 0

    release_send.set()
    await asyncio.gather(*list(runner._background_tasks))
    await asyncio.sleep(0)

    assert pending_entry.session_key not in runner._restart_interruption_checkins
    assert pending_entry.resume_pending is False
""",
    "test_auto_resume_sentinel_cleaned_on_task_failure": """@pytest.mark.asyncio
async def test_restart_checkin_retries_rejection_until_accepted():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="fail-chat")
    pending_entry = SessionEntry(
        session_key="agent:main:telegram:dm:fail-chat",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {pending_entry.session_key: pending_entry}
    adapter.send = AsyncMock(
        side_effect=[
            SendResult(success=False, error="simulated delivery failure"),
            SendResult(success=True, message_id="1"),
        ]
    )
    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()

    async def hold_retry(_delay):
        retry_waiting.set()
        await release_retry.wait()

    assert runner._schedule_resume_pending_sessions() == 1
    tasks = list(runner._background_tasks)
    with patch("asyncio.sleep", side_effect=hold_retry):
        await asyncio.wait_for(retry_waiting.wait(), timeout=2)
        assert pending_entry.resume_pending is True
        assert pending_entry.session_key in runner._restart_interruption_checkins
        release_retry.set()
        await asyncio.gather(*tasks)
    await asyncio.sleep(0)

    assert adapter.send.await_count == 2
    assert pending_entry.session_key not in runner._restart_interruption_checkins
    assert pending_entry.resume_pending is False
    runner.session_store.clear_resume_pending.assert_called_once_with(pending_entry.session_key)
""",
    "test_auto_resume_runs_agent_exactly_once_through_full_path": """@pytest.mark.asyncio
async def test_restart_checkin_never_runs_interrupted_agent():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="full-path-chat")
    session_key = runner._session_key_for_source(source)
    pending_entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {session_key: pending_entry}
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*list(runner._background_tasks))

    adapter.handle_message.assert_not_awaited()
    assert len(adapter.sent) == 1
    assert "Do you still need me to finish it?" in adapter.sent[0]
    assert pending_entry.resume_pending is False
""",
}

OPTIONAL_COMPOSITION_TESTS = frozenset(
    {
        # Upstream's v0.19.1 test-pruning pass removed these direct scheduler
        # cases. Golden still proves the same policy through its patcher tests
        # and the assembled durable-drain composition suite; runtime patching
        # must not depend on upstream retaining our preferred test names.
        "test_startup_auto_resume_schedules_fresh_pending_sessions",
        "test_startup_auto_resume_includes_crash_recovery",
        "test_reconnect_reschedules_pending_after_late_platform_connect",
        "test_auto_resume_sentinel_cleaned_on_task_failure",
        "test_startup_restore_waits_for_resume_before_final_durable_drain",
    }
)

POLICY_TEST_ALTERNATIVES = {
    "test_startup_restore_waits_for_resume_before_draining_inbound": frozenset(
        {
            "test_startup_restore_gate_persists_real_inbound_messages",
            "test_startup_gate_waits_for_final_barrier_then_dispatches_normally",
            "test_startup_restore_has_no_in_memory_queue_path",
        }
    ),
}


def _replace_async_test(content: str, name: str, replacement: str) -> str | None:
    tree = ast.parse(content)
    node = next(
        (item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == name),
        None,
    )
    if node is None or node.end_lineno is None:
        return None
    start_lineno = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    lines = content.splitlines(keepends=True)
    replacement_text = replacement.rstrip() + "\n\n"
    return "".join(lines[: start_lineno - 1]) + replacement_text + "".join(lines[node.end_lineno :])


def _validate_behavior_tests(hermes_dir: Path) -> None:
    path = Path(hermes_dir) / "tests" / "gateway" / "test_restart_resume_pending.py"
    if not path.exists():
        raise RuntimeError("required restart interruption behavior tests are missing")
    content = path.read_text(encoding="utf-8")
    missing = []
    for old_name, replacement in TEST_REPLACEMENTS.items():
        new_name = ast.parse(replacement).body[0].name
        if f"async def {old_name}(" not in content and f"async def {new_name}(" not in content:
            if old_name in OPTIONAL_COMPOSITION_TESTS:
                continue
            alternatives = POLICY_TEST_ALTERNATIVES.get(old_name, ())
            if alternatives and all(f"def {name}(" in content for name in alternatives):
                continue
            missing.append(old_name)
    if missing:
        raise RuntimeError("required restart interruption behavior test seams are missing: " + ", ".join(missing))


def _patch_behavior_tests(hermes_dir: Path) -> bool:
    path = Path(hermes_dir) / "tests" / "gateway" / "test_restart_resume_pending.py"
    original = path.read_text(encoding="utf-8")
    patched = original.replace(
        "    runner.session_store._save.assert_called_once()\n",
        "    runner.session_store.clear_resume_pending.assert_called_once_with(pending_entry.session_key)\n",
        1,
    )
    for old_name, replacement in TEST_REPLACEMENTS.items():
        new_name = ast.parse(replacement).body[0].name
        if f"async def {new_name}(" in patched:
            continue
        alternatives = POLICY_TEST_ALTERNATIVES.get(old_name, ())
        if alternatives and all(f"def {name}(" in patched for name in alternatives):
            # Newer durable-runtime suites may retain a historical function
            # name for a different composition invariant. Prefer their full
            # policy proof over injecting Golden's older queue-based test.
            continue
        if f"async def {old_name}(" not in patched:
            if old_name in OPTIONAL_COMPOSITION_TESTS:
                continue
            raise RuntimeError(f"restart interruption check-in test anchor drifted: {old_name}")
        updated = _replace_async_test(patched, old_name, replacement)
        if updated is None:
            raise RuntimeError(f"restart interruption check-in test anchor drifted: {old_name}")
        patched = updated
    if patched == original:
        return False
    ast.parse(patched)
    backup = Path(str(path) + BACKUP_SUFFIX)
    created_backup = False
    try:
        if not backup.exists():
            shutil.copy2(path, backup)
            created_backup = True
        path.write_text(patched, encoding="utf-8")
    except Exception:
        path.write_text(original, encoding="utf-8")
        if created_backup:
            backup.unlink(missing_ok=True)
        raise
    return True


def patch_restart_interruption_checkin_v1(hermes_dir: Path) -> bool:
    """Patch the native scheduler without adding a parallel task system."""
    _validate_behavior_tests(Path(hermes_dir))
    run_py = Path(hermes_dir) / "gateway" / "run.py"
    original = run_py.read_text(encoding="utf-8")
    patched = original
    runtime_changed = False
    if MARKER in original:
        if STALE_RETRY_SEND_TAIL in patched:
            patched = patched.replace(
                STALE_RETRY_SEND_TAIL,
                RETRY_SEND_TAIL,
                1,
            )
        elif SYNC_RETRY_SEND_TAIL in patched:
            patched = patched.replace(
                SYNC_RETRY_SEND_TAIL,
                RETRY_SEND_TAIL,
                1,
            )
        elif FACADE_RETRY_SEND_TAIL in patched:
            patched = patched.replace(
                FACADE_RETRY_SEND_TAIL,
                RETRY_SEND_TAIL,
                1,
            )
        elif RETRY_SEND_TAIL not in patched:
            raise RuntimeError("current restart interruption check-in anchors drifted")
    else:
        if PRIOR_MARKER in original:
            prior_checkin_block = CHECKIN_BLOCK.replace(MARKER, PRIOR_MARKER)
            prior_helper_prefix = HELPER_PREFIX.replace(MARKER, PRIOR_MARKER)
            if (
                prior_checkin_block not in original
                or prior_helper_prefix not in original
                or (
                    STALE_RETRY_SEND_TAIL not in original
                    and SYNC_RETRY_SEND_TAIL not in original
                    and FACADE_RETRY_SEND_TAIL not in original
                    and RETRY_SEND_TAIL not in original
                )
            ):
                raise RuntimeError("prior restart interruption check-in anchors drifted")
            patched = original.replace(PRIOR_MARKER, MARKER)
            if STALE_RETRY_SEND_TAIL in patched:
                patched = patched.replace(
                    STALE_RETRY_SEND_TAIL,
                    RETRY_SEND_TAIL,
                    1,
                )
            elif SYNC_RETRY_SEND_TAIL in patched:
                patched = patched.replace(
                    SYNC_RETRY_SEND_TAIL,
                    RETRY_SEND_TAIL,
                    1,
                )
            elif FACADE_RETRY_SEND_TAIL in patched:
                patched = patched.replace(
                    FACADE_RETRY_SEND_TAIL,
                    RETRY_SEND_TAIL,
                    1,
                )
        elif LEGACY_MARKER in original:
            if LEGACY_CHECKIN_BLOCK in original and LEGACY_SEND_TAIL in original:
                patched = original.replace(LEGACY_CHECKIN_BLOCK, CHECKIN_BLOCK, 1)
                patched = patched.replace(LEGACY_SEND_TAIL, RETRY_SEND_TAIL, 1)
            elif RETAINING_V1_CHECKIN_BLOCK in original and RETAINING_V1_SEND_TAIL in original:
                patched = original.replace(RETAINING_V1_CHECKIN_BLOCK, CHECKIN_BLOCK, 1)
                patched = patched.replace(RETAINING_V1_SEND_TAIL, RETRY_SEND_TAIL, 1)
            else:
                raise RuntimeError("legacy restart interruption check-in anchors drifted")
            patched = patched.replace(LEGACY_MARKER, MARKER)
        else:
            schedule_blocks = [
                block
                for block in (SCHEDULE_BLOCK, SCHEDULE_BLOCK_SESSION_STATE)
                if block in original
            ]
            if METHOD_ANCHOR not in original or len(schedule_blocks) != 1:
                raise RuntimeError("restart interruption check-in anchors drifted")
            patched = original.replace(METHOD_ANCHOR, HELPER + METHOD_ANCHOR, 1)
            if SCHEDULER_DOC_OLD in patched:
                patched = patched.replace(SCHEDULER_DOC_OLD, SCHEDULER_DOC_NEW, 1)
            patched = patched.replace(schedule_blocks[0], CHECKIN_BLOCK, 1)
        if STARTUP_WAIT_BLOCK_NEW not in patched:
            if NATIVE_BOUNDED_STARTUP_WAIT in patched:
                pass
            elif STARTUP_WAIT_BLOCK_OLD not in patched:
                raise RuntimeError("restart interruption startup-wait anchor drifted")
            else:
                patched = patched.replace(
                    STARTUP_WAIT_BLOCK_OLD,
                    STARTUP_WAIT_BLOCK_NEW,
                    1,
                )

    if POST_STARTUP_DRAIN_BLOCK in patched:
        patched = patched.replace(
            POST_STARTUP_DRAIN_BLOCK,
            POST_STARTUP_DRAIN_BLOCK_WITH_RESCHEDULE,
            1,
        )
    elif DURABLE_DRAIN_METHOD_ANCHOR in patched and POST_STARTUP_DRAIN_BLOCK_WITH_RESCHEDULE not in patched:
        raise RuntimeError("post-startup durable drain anchor drifted")

    if POST_STARTUP_DRAIN_BLOCK_WITH_RESCHEDULE in patched:
        reconnect_blocks = (
            (
                PRIMARY_RECONNECT_SUCCESS_BLOCK,
                PRIMARY_RECONNECT_SUCCESS_BLOCK_WITH_DRAIN,
            ),
            (
                PROFILE_RECONNECT_SUCCESS_BLOCK,
                PROFILE_RECONNECT_SUCCESS_BLOCK_WITH_DRAIN,
            ),
        )
        for old_block, new_block in reconnect_blocks:
            if new_block in patched:
                continue
            if old_block not in patched:
                raise RuntimeError("durable drain reconnect anchor drifted")
            patched = patched.replace(old_block, new_block, 1)

    if patched != original:
        backup = Path(str(run_py) + BACKUP_SUFFIX)
        created_backup = False
        try:
            if not backup.exists():
                shutil.copy2(run_py, backup)
                created_backup = True
            run_py.write_text(patched, encoding="utf-8")
        except Exception:
            run_py.write_text(original, encoding="utf-8")
            if created_backup:
                backup.unlink(missing_ok=True)
            raise
        runtime_changed = True
    return _patch_behavior_tests(Path(hermes_dir)) or runtime_changed
