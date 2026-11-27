"""One task-specific Telegram progress message, using native delivery and run ownership."""
from collections import OrderedDict
from dataclasses import dataclass, field
import asyncio
import logging
from threading import Lock

log = logging.getLogger(__name__)


@dataclass
class Progress:
    session_key: str
    session_id: str
    turn_id: str
    accepting: bool = True
    message_id: str = ""
    text: str = ""
    turn: object = None
    mutex: asyncio.Lock = field(default_factory=asyncio.Lock)


def register(ctx):
    turns = OrderedDict()
    lock = Lock()
    host = None
    loop = None

    async def bind(*, gateway: object, **_):
        nonlocal host, loop
        # This hook only captures the host. Only an admitted foreground turn may send.
        host = gateway
        loop = asyncio.get_running_loop()

    def submit(coro):
        if loop is None or loop.is_closed():
            coro.close()
            return
        future = asyncio.run_coroutine_threadsafe(coro, loop)

        def report(done):
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                log.warning("Telegram progress delivery failed", exc_info=(type(error), error, error.__traceback__))
        future.add_done_callback(report)

    def begin(*, session_id="", turn_id="", platform="", parent_session_id="", **_):
        from gateway.session_context import get_session_env
        if (platform != "telegram" or parent_session_id or not session_id or not turn_id
                or get_session_env("HERMES_CRON_SESSION") == "1"):
            return
        session_key = get_session_env("HERMES_SESSION_KEY")
        if not session_key or host is None:
            return
        with lock:
            turns[(session_id, turn_id)] = Progress(session_key, session_id, turn_id)
            while len(turns) > 128:
                turns.popitem(last=False)
        return {"context": (
            "For this Telegram task, give a short task-specific update before starting tools. "
            "Use commentary for meaningful progress updates as work proceeds. "
            "State observed progress; do not invent milestones or send a generic acknowledgment."
        )}

    def current(progress):
        state = host._peek_session_state(progress.session_key)
        turn = state.turn.ctx if state else None
        if turn is None or not turn._run_still_current():
            return None
        agent = turn.agent_holder[0]
        if (agent is None or agent.session_id != progress.session_id
                or agent._current_turn_id != progress.turn_id
                or getattr(agent, "_hold_stream", False)
                or turn.scheduled_heartbeat or turn.mute_notification_reply
                or turn.source.platform.value != "telegram"):
            return None
        return turn

    async def cleanup(progress):
        with lock:
            progress.accepting = False
            turns.pop((progress.session_id, progress.turn_id), None)

    async def update(progress, text):
        async with progress.mutex:
            with lock:
                if not progress.accepting:
                    return
            turn = current(progress)
            if turn is None or text == progress.text:
                return
            adapter = turn._status_adapter
            if progress.turn is None:
                progress.turn = turn
                adapter.register_post_delivery_callback(
                    progress.session_key, lambda: cleanup(progress), generation=turn.run_generation,
                )
            operation = "edit" if progress.message_id else "send"
            try:
                if progress.message_id:
                    result = await asyncio.wait_for(
                        adapter.edit_message(turn.source.chat_id, progress.message_id, text), 15,
                    )
                else:
                    from gateway.run import _interim_metadata
                    result = await asyncio.wait_for(adapter.send(
                        turn.source.chat_id, text, reply_to=turn.event_message_id,
                        metadata=_interim_metadata(turn._status_thread_metadata),
                    ), 15)
                    if result.success and result.message_id:
                        progress.message_id = str(result.message_id)
                        if turn._cleanup_progress:
                            turn._cleanup_msg_ids.append(progress.message_id)
            except Exception:
                if not progress.message_id:
                    with lock:
                        progress.accepting = False
                raise
            if result.success and progress.message_id:
                progress.text = text
                log.info("Telegram progress %s session=%s turn=%s message=%s",
                         operation,
                         progress.session_id, progress.turn_id, progress.message_id)
            else:
                # An uncertain send is never retried as another send for this turn.
                if not progress.message_id:
                    with lock:
                        progress.accepting = False
                log.warning("Telegram progress operation failed session=%s turn=%s", progress.session_id, progress.turn_id)

    def commentary(*, session_id="", turn_id="", surface="", text="", **_):
        if surface != "telegram":
            return
        from gateway.run import _redact_gateway_user_facing_secrets
        from agent.history_commentary import visible_commentary
        clean = _redact_gateway_user_facing_secrets(visible_commentary(str(text))).strip()[:3500]
        if not clean:
            return
        with lock:
            progress = turns.get((session_id, turn_id))
            if progress is None or not progress.accepting:
                return
        submit(update(progress, clean))

    async def settled(progress):
        async with progress.mutex:
            pass

    def finished(*, session_id="", turn_id="", **_):
        with lock:
            progress = turns.get((session_id, turn_id))
            if progress:
                progress.accepting = False
        if progress is not None and loop is not None and not loop.is_closed():
            # post_llm_call runs on the turn worker. Finish any in-flight edit
            # before native final delivery; queued observer updates now do nothing.
            asyncio.run_coroutine_threadsafe(settled(progress), loop).result(timeout=20)

    def stopped(*, session_key="", **_):
        with lock:
            for progress in turns.values():
                if progress.session_key == session_key:
                    progress.accepting = False

    ctx.register_hook("agent_loop_stopped", stopped)
    ctx.register_hook("pre_gateway_dispatch", bind)
    ctx.register_hook("pre_llm_call", begin)
    ctx.register_hook("on_interim_message", commentary)
    ctx.register_hook("post_llm_call", finished)
