#!/usr/bin/env python3
"""Owned one-connection worker. JSON-lines whitelist; never a general RPC/eval host."""
from __future__ import annotations

import base64
from importlib.metadata import version
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import sys
import time
import threading

# -I deliberately excludes cwd and ambient PYTHONPATH; admit only our own helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CANONICAL_KEYS, DEPENDENCIES, HEIGHT, MAX_MESSAGE, MAX_REQUEST, WIDTH,
                    TYPE_CHUNK, point, private_endpoint, text_symbols)

def dependencies():
    if not (3, 11) <= sys.version_info[:2] < (3, 15):
        raise RuntimeError("worker requires Python 3.11 through 3.14")
    found = {package: version(package) for package in DEPENDENCIES}
    if found != DEPENDENCIES:
        raise RuntimeError(f"worker dependency mismatch: expected {DEPENDENCIES}; found {found}")
    from vncdotool import api
    from vncdotool.client import VNCDoToolClient, VNCDoToolFactory
    from vncdotool.keys import KEYMAP
    return api, VNCDoToolClient, VNCDoToolFactory, KEYMAP


def main():
    api, base_client, base_factory, keymap = dependencies()
    check_only = sys.argv[1:] == ["--check"]
    if len(sys.argv) != 1 and not check_only:
        raise ValueError("only --check is supported")
    connection = None
    authority = None
    profile = endpoint = None
    endpoint_id = None
    current_epoch = None
    capture_epoch = None
    first_capture = True
    retiring = False
    identity = None
    activity = None
    lost = threading.Event()
    participant = None
    bound_socket = None
    retire_lock = threading.RLock()  # maintained write/close callbacks may re-enter on the reactor
    retirement_thread = None
    retirement_settled = threading.Event()
    retirement_failure = None
    retirement_deadline = None

    class FenceError(RuntimeError):
        code = "stale"

    def fence(epoch):
        if retiring:
            raise RuntimeError("worker is retiring; no new input is admitted")
        if authority is None or identity is None or activity is None:
            raise FenceError("external authority/activity admission missing")
        identity.validate()
        activity.check()
        from tools.bot_desktop import retirement
        retirement.capability(identity)
        if private_endpoint(endpoint) != endpoint_id:
            raise FenceError("endpoint identity changed")
        state = authority.assert_agent_may_act(profile_key=profile)
        if state.epoch != current_epoch or epoch != current_epoch:
            raise FenceError("human-control lease epoch changed; capture again")

    class Client(base_client):
        """Small hooks on the maintained client; it still owns all RFB framing."""
        last_frame = None
        _closed = False

        def connectionMade(self):
            self.factory.wire_protocol = self
            self._closed = False
            if retiring:
                self.transport.abortConnection()
                return
            super().connectionMade()

        def _fail_refresh(self, reason):
            self.last_frame = None
            self.coverage = None
            pending = getattr(self, "deferred", None)
            if pending is not None and not pending.called:
                pending.errback(reason)

        def connectionLost(self, reason):
            self._closed = True
            self._fail_refresh(reason)
            try:
                super().connectionLost(reason)
            finally:
                lost.set()  # Actual maintained acknowledgement, not native proof.
                start_retirement()
                settle_activity()


        def updateDesktopSize(self, width, height):
            if (width, height) != (WIDTH, HEIGHT):
                self._closed = True
                self._fail_refresh(RuntimeError("RFB desktop resized outside pinned geometry"))
                self.transport.loseConnection()
                return
            self.width, self.height = width, height
            super().updateDesktopSize(width, height)

        def check_connection(self):
            transport = self.transport
            if (self._closed or not getattr(transport, "connected", False)
                    or getattr(transport, "disconnecting", False)):
                raise RuntimeError("RFB transport disconnected or closing")

        def vncConnectionMade(self):
            self.held_keys = []
            self.held_buttons = 0
            self.pointer_xy = (0, 0)
            self.coverage = None
            self.rectangles_seen = 0
            self.check_geometry()
            super().vncConnectionMade()

        def check_geometry(self):
            if (self.width, self.height) != (WIDTH, HEIGHT):
                self.transport.loseConnection()
                raise RuntimeError("RFB geometry changed; only 1280x800 is supported")
            self.check_connection()

        def full_frame(self):
            self.check_geometry()
            from PIL import Image
            # A fresh canvas handles non-origin first rectangles correctly. Coverage
            # must paint every pixel before this buffer can ever be encoded/returned.
            self.screen = Image.new("RGB", (WIDTH, HEIGHT))
            self.coverage = bytearray(WIDTH * HEIGHT)
            self.rectangles_seen = 0
            pending = self.refreshScreen(incremental=False)
            def encode(_):
                self.check_geometry()
                fence(current_epoch)
                if self.screen is None or self.screen.size != (WIDTH, HEIGHT):
                    raise RuntimeError("incomplete framebuffer")
                output = BytesIO()
                self.screen.save(output, format="PNG")
                self.last_frame = output.getvalue()
                # api's factory Deferred chains require every proxied operation
                # to return the protocol, not bytes/None (as captureScreen does).
                return self
            pending.addCallback(encode)
            return pending

        def updateRectangle(self, x, y, width, height, data):
            self.check_geometry()
            if (x < 0 or y < 0 or width < 0 or height < 0 or
                    x + width > WIDTH or y + height > HEIGHT):
                self.transport.loseConnection()
                raise RuntimeError("frame rectangle is outside pinned output")
            super().updateRectangle(x, y, width, height, data)
            if self.coverage is not None and data:
                self.rectangles_seen += 1
                if self.rectangles_seen > 4096:
                    self.transport.loseConnection()
                    raise RuntimeError("frame rectangle resource limit")
                for row in range(y, y + height):
                    offset = row * WIDTH + x
                    self.coverage[offset:offset + width] = b"\x01" * width

        def commitUpdate(self, rectangles=None):
            self.check_geometry()
            if self.coverage is not None:
                if self.coverage.count(1) != WIDTH * HEIGHT:
                    # Bounded by proxy + parent deadline. Never call a partial/cached frame fresh.
                    self.framebufferUpdateRequest(incremental=False)
                    return
                self.coverage = None
            super().commitUpdate(rectangles)

        def input_key(self, symbol, down, epoch):
            self.check_geometry()
            fence(epoch)
            participant.admit(epoch)
            return self._write_key(symbol, down)

        def input_text(self, text, epoch):
            symbols = text_symbols(text)
            if len(symbols) > TYPE_CHUNK:
                raise ValueError("typing chunk exceeds limit")
            self.check_geometry()
            fence(epoch)
            participant.admit(epoch)
            # One short reactor turn on the already bound connection. Full
            # authority admission repeats for the next chunk; revocation polling
            # resumes between turns. Every individual write still checks local
            # transport/geometry/retirement and retains held-key cleanup custody.
            for symbol in symbols:
                self._write_key(symbol, True)
                self._write_key(symbol, False)
            return self

        def _write_key(self, symbol, down):
            self.check_geometry()
            if type(symbol) is int and 32 <= symbol <= 126:
                keysym = symbol
            elif isinstance(symbol, str) and symbol in CANONICAL_KEYS:
                keysym = int(keymap[symbol])
            else:
                raise ValueError("unsupported keysym")
            if type(down) is not bool:
                raise ValueError("key down must be boolean")
            if down:
                if keysym in self.held_keys:
                    raise ValueError("duplicate key-down")
                self.held_keys.append(keysym)  # Track before send: an exception can mean uncertain delivery.
            with retire_lock:
                if retiring:
                    raise FenceError("worker retired before input write")
                self.keyEvent(keysym, down=down)
            if not down and keysym in self.held_keys:
                self.held_keys.remove(keysym)
            return self

        def input_pointer(self, x, y, mask, epoch):
            self.check_geometry()
            fence(epoch)
            participant.admit(epoch)
            xy = point(x, y)
            if type(mask) is not int or mask not in (0, 1, 2, 4, 8, 16, 32, 64):
                raise ValueError("unsupported pointer mask")
            self.pointer_xy = xy
            if mask:
                self.held_buttons = mask
            with retire_lock:
                if retiring:
                    raise FenceError("worker retired before input write")
                self.pointerEvent(x, y, mask)
            self.held_buttons = mask
            return self

        def release_owned(self):
            # No new down, focus, clipboard or unrelated pointer movement after revocation.
            # No lease check here: withholding cleanup can leave a key held.
            self.check_connection()
            failures = []
            for keysym in list(reversed(self.held_keys)):
                try:
                    self.keyEvent(keysym, down=False)
                    self.held_keys.remove(keysym)
                except Exception as exc:
                    failures.append(type(exc).__name__)
            if self.held_buttons:
                try:
                    self.pointerEvent(*self.pointer_xy, 0)
                    self.held_buttons = 0
                except Exception as exc:
                    failures.append(type(exc).__name__)
            if failures:
                raise RuntimeError("held-input release could not be guaranteed on this transport")
            self.check_connection()
            return self

    class Factory(base_factory):
        wire_protocol = None
        protocol = Client
        shared = True
        pseudodesktop = True
        pseudocursor = False
        nocursor = False
        qemu_extended_key = False

    class Proxy(api.ThreadedVNCClientProxy):
        pending_connect = None

        def connect(self, host, port=5900, family=None):
            import socket
            from twisted.internet import reactor
            if family != socket.AF_UNIX:
                raise ValueError("only the canonical private UNIX endpoint is supported")
            def capture_protocol(protocol):
                self.protocol = protocol
                return protocol
            def begin():
                from twisted.python.failure import Failure
                try:
                    if retiring or bound_socket is None:
                        raise FenceError("owned native socket retired before adoption")
                    self.factory.deferred.addCallback(capture_protocol)
                    # Public API duplicates this connected nonblocking descriptor;
                    # vncdotool still owns RFB, with no second dial.
                    reactor.adoptStreamConnection(bound_socket.fileno(), socket.AF_UNIX, self.factory)
                except Exception:
                    if not self.factory.deferred.called:
                        self.factory.clientConnectionFailed(None, Failure())
                finally:
                    if bound_socket is not None:
                        bound_socket.close()
            reactor.callFromThread(begin)

    if check_only:
        print(json.dumps({"dependencies": DEPENDENCIES, "protocol": Factory.protocol.__name__,
                          "network_attempted": False}))
        return

    def settle_activity():
        # A cleanup result cannot stand in for connectionLost, nor vice versa.
        with retire_lock:
            if lost.is_set() and retirement_settled.is_set() and activity is not None:
                activity.close()

    def certify_retirement():
        nonlocal retirement_failure
        from tools.bot_desktop import lease_external, retirement
        try:
            try:
                lease_external.invalidate(identity, current_epoch,
                                          "owned RFB link retired; durable native cleanup pending")
            finally:
                if participant is not None:
                    participant.retire(deadline=time.monotonic() + retirement.BARRIER_SECONDS)
        except BaseException as exc:
            retirement_failure = exc  # Unknown never becomes a receipt.
        finally:
            retirement_settled.set()
            settle_activity()

    def start_retirement():
        nonlocal retiring, retirement_thread, retirement_failure, retirement_deadline
        from tools.bot_desktop import external, retirement
        with retire_lock:
            retiring = True
            if retirement_thread is not None or retirement_settled.is_set():
                return
            # Strong thread custody, independent of reactor/proxy result delivery.
            retirement_thread = threading.Thread(target=certify_retirement,
                                                 name="hermes-rfb-retirement", daemon=False)
            retirement_deadline = time.monotonic() + external.LOCK_SECONDS + retirement.BARRIER_SECONDS
            try:
                retirement_thread.start()
            except RuntimeError as exc:
                retirement_thread = None
                retirement_failure = exc
                retirement_settled.set()

    def retire_transport():
        nonlocal retiring
        start_retirement()
        if connection is not None:
            from twisted.python.failure import Failure
            connection.queue.put(Failure(FenceError("stream revoked; pending proxy work failed")))
        protocol = connection.factory.wire_protocol if connection is not None else None
        if protocol is not None:
            protocol.last_frame = None
            protocol._fail_refresh(FenceError("stream revoked; pending work failed"))
            # Abort actual transport independently of a browser/stdin event.
            protocol.transport.abortConnection()
        elif connection is not None and connection.pending_connect is not None:
            connection.pending_connect.cancel()  # maintained cancellable endpoint Deferred

    def poll_revocation():
        from twisted.internet import reactor
        try:
            fence(current_epoch)
        except Exception:
            retire_transport()
            return
        if not lost.is_set():
            reactor.callLater(0.1, poll_revocation)

    def interrupted(signum, _):
        nonlocal retiring
        retiring = True
        raise SystemExit(f"worker interrupted ({signum})")

    def expired(_signum, _):
        nonlocal retiring
        retiring = True
        raise TimeoutError("worker operation/idle deadline exceeded")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGALRM, expired)
    try:
        while True:
            # A model may spend over a minute reasoning between screen actions.
            # Connected revocation is watched by the reactor, not this idle
            # alarm. EOF still retires the worker and each operation stays timed.
            signal.alarm(0 if connection is not None else 5)
            line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
            signal.alarm(0)
            if not line:
                break
            request_id = None
            try:
                if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                    raise ValueError("oversized or unterminated worker request")
                request = json.loads(line)
                request_id = request["id"]
                op = request["op"]
                signal.alarm(30 if op in {"init", "capture", "geometry"} else 8)
                if op == "init" and connection is None:
                    if set(request) != {"id", "op", "endpoint", "profile", "hermes_root", "epoch", "identity"}:
                        raise ValueError("invalid init request")
                    endpoint, profile = request["endpoint"], request["profile"]
                    root = Path(request["hermes_root"])
                    if not root.is_absolute() or not Path(profile).is_absolute():
                        raise ValueError("absolute Hermes root and captured profile required")
                    # Same actual module as the parent, not a second lease/permission implementation.
                    sys.path.insert(0, str(root))
                    from tools.bot_desktop import lease as actual_lease
                    authority = actual_lease
                    if Path(authority.__file__).resolve() != (root / "tools/bot_desktop/lease.py").resolve():
                        raise RuntimeError("unexpected Hermes lease module")
                    from tools.bot_desktop import external
                    identity = external.resolve(profile)
                    if identity.as_dict() != request["identity"] or identity.endpoint != endpoint:
                        raise FenceError("worker endpoint/mode/generation differs from parent admission")
                    endpoint_id = private_endpoint(endpoint)
                    current_epoch = request["epoch"]
                    activity = identity.activity()  # worker-owned SH BEFORE lease admission/dial
                    fence(current_epoch)
                    from tools.bot_desktop.participants import Participant
                    participant = Participant(identity, role="agent")
                    bound_socket = participant.connect()
                    # This timeout covers full-frame proxy operations too, not
                    # only dialing. Keep it inside the outer 30-second budget.
                    connection = api.connect(endpoint, factory_class=Factory, proxy=Proxy, timeout=8)
                    fence(current_epoch)
                    from twisted.internet import reactor
                    reactor.callFromThread(poll_revocation)
                    # Initial actual full frame is discarded: startup gray was observed in feasibility.
                    connection.full_frame()
                    participant.identify()  # Exact native ID after maintained handshake.
                    fence(current_epoch)
                    response = {"ok": True}
                elif connection is None:
                    raise ValueError("init is required")
                elif op == "release" and set(request) == {"id", "op"}:
                    connection.release_owned()
                    response = {"ok": True, "release": "enqueued_not_delivery_verified"}
                elif op in {"capture", "geometry"} and set(request) == {"id", "op", "epoch"}:
                    fence(request["epoch"])
                    if first_capture:
                        time.sleep(0.5)
                        fence(current_epoch)
                    connection.full_frame()
                    raw = connection.last_frame
                    fence(current_epoch)
                    first_capture = False
                    if op == "capture":
                        capture_epoch = current_epoch
                        response = {"ok": True, "png": base64.b64encode(raw).decode("ascii"),
                                    "width": WIDTH, "height": HEIGHT}
                    else:
                        if capture_epoch != current_epoch:
                            raise RuntimeError("fresh capture required before input")
                        response = {"ok": True, "width": WIDTH, "height": HEIGHT}
                elif op == "key" and set(request) == {"id", "op", "epoch", "symbol", "down"}:
                    if capture_epoch != request["epoch"]:
                        raise RuntimeError("fresh capture required")
                    connection.input_key(request["symbol"], request["down"], request["epoch"])
                    response = {"ok": True}
                elif op == "type" and set(request) == {"id", "op", "epoch", "text"}:
                    if capture_epoch != request["epoch"]:
                        raise RuntimeError("fresh capture required")
                    connection.input_text(request["text"], request["epoch"])
                    response = {"ok": True}
                elif op == "pointer" and set(request) == {"id", "op", "epoch", "x", "y", "mask"}:
                    if capture_epoch != request["epoch"]:
                        raise RuntimeError("fresh capture required")
                    connection.input_pointer(request["x"], request["y"], request["mask"], request["epoch"])
                    response = {"ok": True}
                else:
                    raise ValueError("unsupported worker operation or fields")
                if retiring or lost.is_set():
                    raise FenceError("stream retired before response; queued success discarded")
                signal.alarm(0)
                response["id"] = request_id
                encoded = json.dumps(response, separators=(",", ":"))
                if len(encoded) > MAX_MESSAGE:
                    raise ValueError("worker response exceeds limit")
                sys.stdout.write(encoded + "\n")
                sys.stdout.flush()
            except Exception as exc:
                retiring = True
                signal.alarm(0)
                # Any uncertain failure is terminal; do not reuse the proxy's response queue.
                print(json.dumps({"id": request_id, "ok": False, "message": f"{type(exc).__name__}: {exc}",
                                  "meta": {"delivery": "partial_or_unknown", "release_guaranteed": False,
                                           "no_replay": True,
                                           "code": "human_has_control" if authority is not None and isinstance(exc, authority.HumanHasControl)
                                           else getattr(exc, "code", "transport_uncertain")}}), flush=True)
                break
    finally:
        with retire_lock:
            retiring = True
        signal.alarm(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if identity is not None:
            start_retirement()
        if connection is not None:
            from twisted.internet import reactor
            def cleanup():
                protocol = connection.factory.wire_protocol
                if protocol is None:
                    if connection.pending_connect is not None:
                        connection.pending_connect.cancel()
                    return
                try:
                    protocol.release_owned()  # maintained, release-only, no ordinary admission
                except Exception:
                    protocol.last_frame = None
                protocol.transport.loseConnection()
                # Abort is a bounded fallback, NOT proof of remote releases.
                reactor.callLater(0.5, protocol.transport.abortConnection)
            reactor.callFromThread(cleanup)
            if not lost.wait(timeout=2):
                reactor.callFromThread(retire_transport)
                lost.wait(timeout=1)
        elif activity is not None:
            # No connection was ever created; nothing can write on this SH.
            activity.close()
        if bound_socket is not None:
            bound_socket.close()
        if retirement_thread is not None:
            retirement_thread.join(timeout=max(0, retirement_deadline - time.monotonic()))
        if retirement_failure is not None:
            print(f"Native retirement remains unknown: {type(retirement_failure).__name__}: {retirement_failure}",
                  file=sys.stderr)
        # With an unacknowledged close, retain SH until process exit. The lease
        # transition still cannot publish active ownership after that crash.



if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
        sys.stderr.flush()
    except BaseException as exc:
        print(f"WayVNC worker stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        os._exit(1)
    os._exit(0)
