"""External-only authenticated noVNC bridge. Native bridge remains unchanged.

A watcher owns no activity SH. Each connection first sending admitted input gets
its own nonblocking SH before reading the lease, pins that epoch irreversibly,
and retains SH until the actual upstream transport's close waiter completes.
"""
from __future__ import annotations

import asyncio
import threading
import time


from fastapi import WebSocketDisconnect
from tools.bot_desktop import external, lease, lease_external, participants, retirement
from tools.bot_desktop.rfb_filter import RfbClientFilter


# Connection/failed-close custody only, never an input registry or authority.
# Keep the writer, close waiter and integer-FD activity owner through loop shutdown
# or a failed cleanup. Only acknowledged close plus settled pumps removes an owner.
_upstreams = set()


def _observe(task):
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            task.get_loop().call_exception_handler({
                "message": "External Screen connection task failed", "exception": error,
                "future": task})


class _Upstream:
    """One connection's FD custody, including results arriving after cancellation."""

    def __init__(self):
        self.guard = threading.Lock()
        self.closing = False
        self.socket = None
        self.writer = None
        self.dial = None
        self.adoption = None
        self.close_waiter = None
        self.activity = None
        self.cleanup = None
        self.clean_close = False
        self.handoff_canceled = lease_external.Cancellation()
        self.input_guard = threading.Lock()
        self.retirement_thread = None
        self.retirement_done = threading.Event()
        self.retirement_error = None
        self.retirement_deadline = None
        _upstreams.add(self)

    def retire(self, participant, identity, epoch):
        """Retain native-proof work independently of canceled asyncio results."""
        self.retirement_deadline = time.monotonic() + retirement.BARRIER_SECONDS

        def certify():
            try:
                # A canceled to_thread frame may still be persisting admission.
                # Serialize that final row write before looking for cleanup.
                if not self.input_guard.acquire(timeout=retirement._remaining(self.retirement_deadline)):
                    raise retirement.RetirementUnknown("closing input admission did not settle")
                try:
                    participant.retire(deadline=self.retirement_deadline)
                finally:
                    self.input_guard.release()
            except BaseException as exc:
                self.retirement_error = exc
                self.clean_close = False
            finally:
                if epoch is not None and not self.clean_close:
                    try:
                        lease_external.invalidate(identity, epoch,
                                                  "input-bearing viewer retired; native cleanup/handback unconfirmed")
                    except BaseException as exc:
                        self.retirement_error = exc
                self.retirement_done.set()

        self.retirement_thread = threading.Thread(target=certify,
                                                  name="hermes-viewer-retirement", daemon=False)
        try:
            self.retirement_thread.start()
        except RuntimeError as exc:
            self.retirement_thread = None
            self.retirement_error = exc
            self.clean_close = False
            self.retirement_done.set()

    def _dial(self, participant):
        # Never return an FD through to_thread: cancelling its asyncio task can
        # discard a result while the executor thread is still running. This
        # worker itself retains the owner and disposes any late socket, even if
        # no asyncio done callback can run after loop shutdown.
        transport = participant.connect()
        with self.guard:
            if self.closing:
                transport.close()
            else:
                self.socket = transport

    async def _adopt(self):
        transport, self.socket = self.socket, None
        try:
            reader, self.writer = await asyncio.open_unix_connection(sock=transport)
            return reader
        except BaseException:
            # asyncio closes a constructed transport on cancellation; before
            # construction, the original socket is still ours to close.
            transport.close()
            raise

    async def connect(self, participant):
        self.dial = asyncio.create_task(asyncio.to_thread(self._dial, participant))
        self.dial.add_done_callback(_observe)
        await asyncio.shield(self.dial)
        self.adoption = asyncio.create_task(self._adopt())
        self.adoption.add_done_callback(_observe)
        return await asyncio.wait_for(asyncio.shield(self.adoption), external.DIAL_SECONDS)

    def stop(self):
        with self.guard:
            self.closing = True
            if self.socket is not None and (self.adoption is None or self.adoption.done()):
                self.socket.close()
                self.socket = None
        if self.adoption is not None and not self.adoption.done():
            self.adoption.cancel()
        if self.writer is not None:
            self.writer.close()


async def _close_upstream(upstream):
    writer = upstream.writer
    writer.close()
    upstream.close_waiter = asyncio.create_task(writer.wait_closed())
    upstream.close_waiter.add_done_callback(_observe)
    try:
        try:
            await asyncio.wait_for(asyncio.shield(upstream.close_waiter), external.CLOSE_SECONDS)
        except asyncio.TimeoutError:
            writer.transport.abort()
            await asyncio.wait_for(asyncio.shield(upstream.close_waiter), external.CLOSE_SECONDS)
    except (OSError, ConnectionError):
        # Only the maintained waiter's completed connection-lost result can
        # settle custody, never close()/abort() or a timer by themselves.
        if not upstream.close_waiter.done() or upstream.close_waiter.cancelled():
            raise


async def _close_ws(ws, code=4000, reason="control-taken"):
    try:
        await asyncio.wait_for(ws.close(code=code, reason=reason), external.CLOSE_SECONDS)
    except (RuntimeError, OSError, asyncio.TimeoutError, WebSocketDisconnect):
        # A viewer that already closed (clean 1000 handback) makes Starlette raise
        # WebSocketDisconnect here; the close is moot and cleanup must still finish.
        return


async def bridge(ws, info):
    home = str(info["hermes_home"])
    viewer_id = info.get("viewer_id")
    activity = None
    admitted_epoch = None
    retired = False
    clean_close = False
    writer = None
    participant = None
    upstream = _Upstream()
    tasks = []
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()
    validation = asyncio.Lock()
    frame_state = None
    frame_deadline = 0.0
    try:
        identity = await asyncio.to_thread(external.resolve, home)
        if info.get("external_identity") != identity.as_dict():
            raise external.ExternalScreenError("display ticket endpoint/mode/authority generation changed")
        external.short_string(viewer_id, "viewer_id")
        identity.validate()
        participant = await asyncio.to_thread(participants.Participant, identity, role="human", viewer_id=viewer_id)
        reader = await upstream.connect(participant)
        writer = upstream.writer
        identity.validate()  # path replacement during dial is terminal

        def still_admitted():
            identity.validate()
            if upstream.activity is not None:
                upstream.activity.check()
            state = lease_external.get(home)
            return lease_external.matches(state, identity, holder=lease.HUMAN,
                                           viewer_id=viewer_id, epoch=admitted_epoch)

        def may_input():
            nonlocal activity, admitted_epoch, retired
            if retired or upstream.closing:
                return False
            if participant.client_id is None:
                return False
            try:
                if time.monotonic() >= frame_deadline:
                    raise external.ExternalScreenError("external input frame deadline exceeded")
                if activity is None:
                    candidate = identity.activity(seconds=0)
                    with upstream.guard:
                        if upstream.closing:
                            candidate.close()
                            return False
                        # Store SH in its surviving owner before any further I/O.
                        upstream.activity = candidate
                    try:
                        identity.validate()
                        if not lease_external.matches(frame_state, identity, holder=lease.HUMAN,
                                                       viewer_id=viewer_id):
                            candidate.close()
                            return False
                        participant.admit(frame_state.epoch, deadline=frame_deadline)
                        with upstream.guard:
                            if upstream.closing:
                                candidate.close()
                                return False
                            admitted_epoch = frame_state.epoch
                            activity = candidate
                    except BaseException:
                        candidate.close()
                        raise
                activity.check()
                if not lease_external.matches(frame_state, identity, holder=lease.HUMAN,
                                               viewer_id=viewer_id, epoch=admitted_epoch):
                    raise external.ExternalScreenError("input stream lost its admitted epoch")
                return True
            except (OSError, ValueError, TypeError, external.ExternalScreenError):
                retired = True
                try:
                    loop.call_soon_threadsafe(wake.set)
                except RuntimeError:
                    pass  # Closing loop; the surviving upstream still owns SH.
                return False

        def feed_frame(raw):
            nonlocal frame_state, frame_deadline
            with upstream.input_guard:
                if upstream.closing:
                    raise external.ExternalScreenError("viewer retired before frame admission")
                frame_deadline = time.monotonic() + retirement.BARRIER_SECONDS
                frame_state = lease_external.get(home, deadline=frame_deadline)
                retirement.capability(identity, deadline=frame_deadline)
                data = gate.feed(raw)
                if time.monotonic() >= frame_deadline:
                    raise external.ExternalScreenError("external input frame deadline exceeded")
                return data, frame_state

        # Clipboard and resize are unavailable regardless of current holder.
        gate = RfbClientFilter(may_input, blocked_types=frozenset({6, 251}), strict_handshake=True)

        async def to_viewer():
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                # Watchers retain frames even while lease excludes the agent.
                await ws.send_bytes(data)

        async def to_server():
            nonlocal clean_close
            while not retired:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    clean_close = message.get("code") in (1000, 1001)
                    return
                raw = message.get("bytes")
                if raw is None or len(raw) > 262144:
                    raise ValueError("RFB requires bounded binary frames")
                # One worker owns this frame's parser/admission state. Full
                # native authority checks never run on the shared event loop.
                async with validation:
                    data, validated = await asyncio.to_thread(feed_frame, raw)
                    if retired or upstream.closing:
                        return
                    identity.validate()
                    if data:
                        if activity is not None:
                            activity.check()
                            with lease_external.write_fence(identity, validated):
                                if retired:
                                    return
                                writer.write(data)  # No await inside the final fence.
                        else:
                            writer.write(data)  # Strictly framed watcher/handshake bytes only.
                if data:
                    await asyncio.wait_for(writer.drain(), external.DIAL_SECONDS)
                    await asyncio.to_thread(retirement.capability, identity)

        async def revocation():
            nonlocal retired
            # Pumps already run: exact native binding waits for the maintained
            # handshake without a lease grant or an activity SH.
            await asyncio.to_thread(participant.identify)
            while not retired:
                try:
                    async with validation:
                        identity.validate()
                        await asyncio.to_thread(retirement.capability, identity)
                        if activity is not None and not await asyncio.to_thread(still_admitted):
                            break
                except (OSError, ValueError, TypeError, external.ExternalScreenError):
                    break
                try:
                    await asyncio.wait_for(wake.wait(), external.POLL_SECONDS)
                except asyncio.TimeoutError:
                    continue
            retired = True
            # End task first. Browser close must not delay upstream retirement.

        tasks = [asyncio.create_task(to_viewer()), asyncio.create_task(to_server()),
                 asyncio.create_task(revocation())]
        for task in tasks:
            task.add_done_callback(_observe)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if tasks[0] in done or tasks[2] in done:
            clean_close = False  # upstream loss/revocation never grants handback
        for task in done:
            if not task.cancelled():
                error = task.exception()
                if error and not isinstance(error, (WebSocketDisconnect, OSError, ConnectionError)):
                    clean_close = False
    except asyncio.CancelledError:
        clean_close = False
        raise
    except (OSError, ValueError, TypeError, external.ExternalScreenError, asyncio.TimeoutError):
        clean_close = False
    finally:
        # Capture epoch before awaits. A closed same-ID link cannot release a
        # later acquisition, and release cannot wait on this link's own SH.
        release_epoch = admitted_epoch
        if release_epoch is not None:
            # Before any teardown await, cancellation also owns the actual old
            # input grant. _commit_grant replaces this with its new epoch only
            # under the same guard; set-before-commit still forbids publication.
            with upstream.handoff_canceled.guard:
                upstream.handoff_canceled.committed = (identity, release_epoch)
        retired = True
        # Install independent strong custody BEFORE any cancellable teardown
        # wait, including pump gathering. A repeated request cancellation cannot
        # cancel this owner or erase its failed-close SH/writer/waiter custody.
        upstream.clean_close = clean_close
        try:
            upstream.stop()
        except OSError:
            upstream.clean_close = False  # Owner is already retained; cleanup still runs.
        for task in tasks:
            if not task.done():
                task.cancel()
        if participant is not None:
            upstream.retire(participant, identity, release_epoch)
        else:
            upstream.retirement_done.set()  # No admitted participant could write.

        async def finish():
            closed = False
            try:
                settling = [*tasks]
                if upstream.adoption is not None:
                    settling.append(upstream.adoption)
                pending = set()
                if settling:
                    done, pending = await asyncio.wait(settling, timeout=external.CLOSE_SECONDS)
                    for task in done:
                        if not task.cancelled():
                            task.exception()
                upstream.stop()  # Also disposes a never-started adoption's raw socket.
                if upstream.writer is not None:
                    await _close_upstream(upstream)
                if upstream.retirement_thread is not None:
                    # Join a retained thread, never await a discardable proof
                    # result. It durably writes even if this task is canceled.
                    await asyncio.to_thread(upstream.retirement_thread.join,
                                            retirement.BARRIER_SECONDS + external.LOCK_SECONDS)
                closed = not pending and upstream.retirement_done.is_set()
                if upstream.retirement_error is not None:
                    upstream.clean_close = False
            except (OSError, asyncio.TimeoutError):
                upstream.clean_close = False
            finally:
                if closed:
                    if upstream.activity is not None:
                        upstream.activity.close()
                        upstream.activity = None
                # Otherwise the entire owner stays in _upstreams through process
                # exit, even if finish itself is cancelled during loop shutdown.
            if not closed:
                upstream.clean_close = False
            if upstream.clean_close and release_epoch is not None:
                try:
                    await asyncio.to_thread(
                        lease_external.transition, home, viewer_id=viewer_id, release=True,
                        epoch=release_epoch, cancellation=upstream.handoff_canceled)
                    if not upstream.clean_close:
                        upstream.handoff_canceled.set()
                except asyncio.CancelledError:
                    upstream.handoff_canceled.set()
                    upstream.clean_close = False
                    raise
                except (OSError, external.ExternalScreenError):
                    pass  # Native reconciliation/baseline failure remains durably excluded.
            elif upstream.clean_close and release_epoch is None and participant is not None:
                # A viewer that took control but closed cleanly before any input was
                # admitted still hands back, as the native bridge does. No input from this
                # link reached the screen, so there is no admitted epoch to drain; release
                # only the exact human lease this viewer holds right now.
                try:
                    held = await asyncio.to_thread(lease_external.get, home)
                    if lease_external.matches(held, identity, holder=lease.HUMAN, viewer_id=viewer_id):
                        await asyncio.to_thread(
                            lease_external.transition, home, viewer_id=viewer_id, release=True,
                            epoch=held.epoch, cancellation=upstream.handoff_canceled)
                except asyncio.CancelledError:
                    upstream.handoff_canceled.set()
                    raise
                except (OSError, ValueError, TypeError, external.ExternalScreenError):
                    pass  # The human lease stays held; reconciliation or explicit release settles it.
            if release_epoch is not None and not upstream.clean_close:
                try:
                    await asyncio.to_thread(lease_external.invalidate, identity, release_epoch,
                                            "input-bearing viewer lost; native retirement required")
                except (OSError, external.ExternalScreenError):
                    upstream.clean_close = False
            await _close_ws(ws, code=4000 if release_epoch is not None else 4001)
            if closed:
                _upstreams.discard(upstream)

        upstream.cleanup = asyncio.create_task(finish())
        upstream.cleanup.add_done_callback(_observe)
        try:
            await asyncio.shield(upstream.cleanup)
        except asyncio.CancelledError:
            upstream.handoff_canceled.set()
            upstream.clean_close = False
            raise
