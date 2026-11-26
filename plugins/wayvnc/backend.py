"""Hermes ABC adapter. Approval/hard-block policy remains in the Hermes wrapper."""
from __future__ import annotations

import base64
import contextlib
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import selectors
import stat
import struct
import subprocess
import threading
import time
from dataclasses import dataclass

from hermes_constants import (get_hermes_home, set_hermes_home_override,
                              reset_hermes_home_override)
from hermes_cli.config import load_config_readonly
from tools.bot_desktop import lease, external, lease_external, retirement
from tools.computer_use.backend import ActionResult, CaptureResult, ComputerUseBackend

from .common import (BUTTONS, DEPENDENCIES, HEIGHT, MAX_MESSAGE, MAX_REQUEST, WHEELS, WIDTH,
                     TYPE_CHUNK, combo, modifiers, point, private_endpoint, text_symbols)


@dataclass(frozen=True)
class Settings:
    profile: str
    endpoint: str
    python: str
    identity: external.Identity
    generation: str

    @classmethod
    def load(cls, profile=None):
        identity = external.resolve(profile)
        home = identity.home
        token = set_hermes_home_override(home)
        try:
            cfg = (load_config_readonly().get("computer_use") or {}).get("wayvnc") or {}
            if not isinstance(cfg, dict):
                raise ValueError("computer_use.wayvnc must be a mapping")
            if set(cfg) - {"python"}:
                raise ValueError("endpoint/generation belong only to bot_desktop.external_rfb; output is not attested")
            return cls(home, identity.endpoint, cfg.get("python", ""), identity, identity.generation)
        finally:
            reset_hermes_home_override(token)

    def validate(self):
        self.identity.validate()
        if not isinstance(self.python, str) or not self.python:
            raise ValueError("WayVNC requires package-local python configuration")
        expected = Path(__file__).parent / ".venv" / "bin" / "python"
        if self.python != str(expected) or not os.access(expected, os.X_OK):
            raise ValueError(f"python must be the explicit package-local interpreter {expected}")
        if not (expected.parent.parent / "pyvenv.cfg").is_file():
            raise ValueError("package-local virtual environment is missing")
        sites = list((expected.parent.parent / "lib").glob("python*/site-packages"))
        installed = {d.metadata["Name"].lower(): d.version
                     for d in distributions(path=[str(p) for p in sites]) if d.metadata["Name"]}
        if any(installed.get(name.lower()) != pinned for name, pinned in DEPENDENCIES.items()):
            raise ValueError(f"package-local worker requires exact dependency versions: {DEPENDENCIES}")
        self.identity.validate()
        return self.identity.device, self.identity.inode


class Refusal(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class WorkerError(RuntimeError):
    def __init__(self, message, meta=None):
        super().__init__(message)
        self.meta = meta or {}


class WayVncBackend(ComputerUseBackend):
    IO_TIMEOUT = 3.0
    CAPTURE_TIMEOUT = 30.0
    ACTION_TIMEOUT = 20.0
    CAPTURE_TTL = 30.0

    def __init__(self, settings, *, permission_mode):
        self.settings = settings
        self.permission_mode = permission_mode
        self._mutex = threading.RLock()
        self._process = None
        self._buffer = b""
        self._sequence = 0
        self._started = False
        self._target = None
        self._endpoint_id = None
        self._pointer = None
        self._last_app = None
        self._last_target = None
        self._events = 0
        self._worker_epoch = None

    def _invalidate(self):
        self._target = None
        self._last_target = None
        # The wrapper's capture_after passes this back as app; None selects our
        # desktop viewport, whereas an empty string is an unsupported named target.
        self._last_app = None

    def is_available(self):
        try:
            self.settings.validate()
            return self.permission_mode in {"standard", "unrestricted"}
        except (OSError, ValueError, TypeError, external.ExternalScreenError):
            return False

    def start(self):
        with self._mutex:
            if self.permission_mode not in {"standard", "unrestricted"}:
                raise Refusal("unsupported_permission_mode", "bounded mode has no enforced RFB capability ceiling")
            self.settings.validate()
            self._started = True

    def stop(self):
        with self._mutex:
            self._retire()
            self._started = False

    def _retire(self):
        self._invalidate()
        self._pointer = None
        process, self._process = self._process, None
        worker_epoch, self._worker_epoch = self._worker_epoch, None
        self._buffer = b""
        if process is None:
            return
        # EOF asks the owned worker to release/close through its reactor. A
        # bounded forced process stop is last-resort custody, never safe handback.
        try:
            lease_external.invalidate(self.settings.identity, worker_epoch,
                                      "owned worker retired; native reconciliation required")
        finally:
            if process.stdin:
                process.stdin.close()
            if process.poll() is None:
                try:
                    # Let the owned worker settle its existing close, lease and
                    # native-proof budgets. Killing at 2s could erase a valid
                    # in-flight 4s barrier. This is not native retention/grace.
                    process.wait(timeout=external.CLOSE_SECONDS + external.LOCK_SECONDS
                                 + retirement.BARRIER_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def _lease(self, epoch=None):
        self.settings.identity.validate()
        retirement.capability(self.settings.identity)
        state = lease.assert_agent_may_act(profile_key=self.settings.profile)
        if epoch is not None and state.epoch != epoch:
            self._invalidate()
            raise Refusal("stale", "human-control lease changed; capture again after handback")
        return state.epoch

    def _admit(self, epoch=None):
        if not self._started:
            raise Refusal("not_started", "backend has not started")
        if self.permission_mode not in {"standard", "unrestricted"}:
            raise Refusal("unsupported_permission_mode", "bounded mode is unsupported")
        if Settings.load(self.settings.profile) != self.settings:
            self._invalidate()
            raise Refusal("stale", "provider configuration changed; release this Hermes session and start a new one")
        self.settings.identity.validate()
        identity = (self.settings.identity.device, self.settings.identity.inode)
        if self._endpoint_id is not None and identity != self._endpoint_id:
            self._retire()
            self._endpoint_id = identity
            raise Refusal("stale", "UNIX endpoint was replaced; fresh capture is required")
        self._endpoint_id = identity
        return self._lease(epoch)

    @contextlib.contextmanager
    def _endpoint_lock(self):
        # Serializes agent operations only; each worker owns its own separate
        # activity SH. Human authority/binding uses the canonical native resolver.
        identity = self.settings.identity
        identity.validate()
        endpoint = Path(identity.endpoint)
        path = endpoint.parent / (".hermes-rfb-operation-" + hashlib.sha256(endpoint.name.encode()).hexdigest()[:24] + ".lock")
        with external.PrivateLock(path, create=True) as lock:
            lock.take(exclusive=True)
            identity.validate()
            try:
                yield
            except BaseException:
                self._retire()
                raise

    def _rpc(self, operation, *, timeout=None, **payload):
        process = self._process
        if process is None or process.poll() is not None:
            raise WorkerError("worker disconnected; no automatic replay", {"delivery": "unknown"})
        self._sequence += 1
        raw = json.dumps({"id": self._sequence, "op": operation, **payload}, ensure_ascii=True).encode() + b"\n"
        if len(raw) > MAX_REQUEST:
            raise ValueError("worker request exceeds limit")
        deadline = time.monotonic() + (timeout or self.IO_TIMEOUT)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            sent = 0
            while sent < len(raw):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise WorkerError("worker write deadline exceeded", {"delivery": "unknown"})
                sent += os.write(process.stdin.fileno(), raw[sent:])
            selector.unregister(process.stdin)
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise WorkerError("worker response deadline exceeded", {"delivery": "unknown"})
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise WorkerError("worker EOF", {"delivery": "unknown"})
                self._buffer += chunk
                if len(self._buffer) > MAX_MESSAGE:
                    raise WorkerError("worker message exceeds limit")
        line, self._buffer = self._buffer.split(b"\n", 1)
        result = json.loads(line)
        if result.get("id") != self._sequence:
            raise WorkerError("worker response identity mismatch")
        if not result.get("ok"):
            raise WorkerError(result.get("message", "worker refused"), result.get("meta"))
        return result

    def _connect(self, epoch):
        if self._process is not None:
            if self._process.poll() is not None:
                self._retire()
                raise WorkerError("worker died; explicit native reconciliation required; no reconnect/replay")
            else:
                return
        self._invalidate()
        self.settings.validate()
        # Import only the actual running Hermes lease implementation in the child.
        root = str(Path(lease.__file__).resolve().parents[2])
        self._worker_epoch = epoch
        self._process = subprocess.Popen(
            [self.settings.python, "-I", "-B", str(Path(__file__).with_name("rfb_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, close_fds=True,
        )
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)
        self._rpc("init", timeout=self.CAPTURE_TIMEOUT, endpoint=self.settings.endpoint,
                  profile=self.settings.profile, hermes_root=root, epoch=epoch,
                  identity=self.settings.identity.as_dict())

    def capture(self, mode="som", app=None, pid=None, window_id=None):
        with self._mutex:
            try:
                if mode not in {"som", "vision", "ax"}:
                    raise Refusal("unsupported_action", "unknown capture mode")
                if app not in (None, "screen") or pid is not None or window_id is not None:
                    raise Refusal("unsupported_action", "only the dedicated desktop viewport/app=screen is supported; no named output/app/PID/window scope")
                self._admit()
                with self._endpoint_lock():
                    epoch = self._admit()
                    if mode == "ax":
                        self._invalidate()
                        return CaptureResult(mode="ax", width=WIDTH, height=HEIGHT,
                                             note="AX is unsupported by RFB; use vision capture of the dedicated desktop viewport.")
                    self._connect(epoch)
                    frame = self._rpc("capture", timeout=self.CAPTURE_TIMEOUT, epoch=epoch)
                    self._admit(epoch)
                    raw = base64.b64decode(frame["png"], validate=True)
                    if (not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 24
                            or struct.unpack(">II", raw[16:24]) != (WIDTH, HEIGHT)
                            or frame.get("width") != WIDTH or frame.get("height") != HEIGHT):
                        raise WorkerError("frame geometry/format does not match the required 1280x800 RFB viewport")
                    self._target = (epoch, time.monotonic())
                    self._last_app = "screen"
                    self._last_target = {"app": "screen"}
                    return CaptureResult(mode=mode, width=WIDTH, height=HEIGHT, png_b64=frame["png"],
                                         png_bytes_len=len(raw), image_mime_type="image/png", app="screen",
                                         note=("Current full RFB viewport of the dedicated workstation; no named-output/app attestation or AX/SOM overlays. "
                                               "Fresh full RFB update; first connection frame discarded and refreshed after 0.5s. "
                                               "Same-geometry source/focus changes are not authenticated. Pixels do not prove awake/unlocked/application readiness. Inspect before foreground input."))
            except BaseException:
                self._retire()
                raise

    def _event(self, op, epoch, deadline, *, event_count=1, **payload):
        if time.monotonic() >= deadline:
            raise WorkerError("bounded action deadline exceeded", {"delivery": "partial_or_unknown"})
        self._admit(epoch)
        self._rpc(op, timeout=min(self.IO_TIMEOUT, max(0.01, deadline - time.monotonic())),
                  epoch=epoch, **payload)
        self._events += event_count

    def _perform(self, action, delivery_mode, bring_to_front, build):
        with self._mutex:
            self._events = 0
            try:
                if delivery_mode != "foreground":
                    raise Refusal("background_unavailable", "RFB drives the guest seat; explicit foreground approval is required")
                if bring_to_front:
                    raise Refusal("unsupported_action", "RFB cannot select/raise an app or restore prior focus")
                execute = build()  # Fully preflight before any sends.
                self._admit()
                with self._endpoint_lock():
                    epoch = self._admit()
                    if (self._target is None or self._target[0] != epoch
                            or time.monotonic() - self._target[1] > self.CAPTURE_TTL):
                        raise Refusal("stale", "a fresh vision capture is required before every input action")
                    self._invalidate()  # Every action consumes the capture, even on partial failure.
                    # Geometry is checked through a complete refresh immediately before input.
                    # Full-frame validation has the capture budget, not the input
                    # budget. A slow frame must not consume the keys' deadline.
                    self._rpc("geometry", timeout=self.CAPTURE_TIMEOUT, epoch=epoch)
                    deadline = time.monotonic() + self.ACTION_TIMEOUT
                    try:
                        execute(epoch, deadline)
                    except BaseException:
                        # Do not enqueue on an uncertain proxy or overwrite its real error.
                        # SIGTERM triggers direct, release-only reactor cleanup under this lock.
                        self._retire()
                        raise
                    # Normal release also bypasses lease admission; no new downs are allowed.
                    self._rpc("release", timeout=1)
                    self._admit(epoch)
                return ActionResult(ok=True, action=action, effect="unverifiable", verified=None,
                                    path="wayvnc_rfb", delivery_mode=delivery_mode,
                                    message="RFB events enqueued; application delivery/effect is unverified. Capture again to inspect.",
                                    meta={"acknowledged_events": self._events, "receipt_scope": "worker_enqueue_only",
                                          "wire_delivery_verified": False, "requires_fresh_capture": True})
            except (Refusal, lease.HumanHasControl) as exc:
                self._retire()
                code = exc.code if isinstance(exc, Refusal) else "human_has_control"
                return ActionResult(ok=False, action=action, code=code, message=str(exc),
                                    path="wayvnc_rfb", delivery_mode=delivery_mode,
                                    escalation={"recommended": "foreground", "reason": str(exc)} if code == "background_unavailable" else None,
                                    meta={"acknowledged_events": self._events, "requires_fresh_capture": True,
                                          "delivery": "partial_or_unknown" if self._events else "not_sent"})
            except (OSError, ValueError, TypeError, KeyError, WorkerError, external.ExternalScreenError) as exc:
                self._retire()
                code = getattr(exc, "meta", {}).get("code") or ("transport_uncertain" if isinstance(exc, (OSError, WorkerError)) else "unsupported_action")
                return ActionResult(ok=False, action=action, code=code,
                                    message=str(exc), effect="unverifiable", path="wayvnc_rfb", delivery_mode=delivery_mode,
                                    meta={"acknowledged_events": self._events, "delivery": "partial_or_unknown" if self._events or isinstance(exc, (OSError, WorkerError)) else "not_sent",
                                          "requires_fresh_capture": True, "no_replay": True,
                                          "release_guaranteed": False, **getattr(exc, "meta", {})})
            except BaseException:
                self._retire()
                raise

    def _keys_down(self, symbols, epoch, deadline):
        for symbol in symbols:
            self._event("key", epoch, deadline, symbol=symbol, down=True)

    def _pointer_event(self, xy, mask, epoch, deadline):
        self._event("pointer", epoch, deadline, x=xy[0], y=xy[1], mask=mask)
        self._pointer = xy

    def click(self, *, element=None, x=None, y=None, button="left", click_count=1,
              modifiers=None, delivery_mode=None, bring_to_front=False):
        def build():
            if element is not None or button not in BUTTONS or type(click_count) is not int or not 1 <= click_count <= 3:
                raise ValueError("click supports coordinates, left/middle/right and 1..3 clicks only")
            xy, mods = point(x, y), self._modifiers(modifiers)
            def run(epoch, deadline):
                self._pointer_event(xy, 0, epoch, deadline)
                self._keys_down(mods, epoch, deadline)
                for index in range(click_count):
                    self._pointer_event(xy, BUTTONS[button], epoch, deadline)
                    self._pointer_event(xy, 0, epoch, deadline)
                    if index + 1 < click_count:
                        time.sleep(0.1)
            return run
        return self._perform("click", delivery_mode, bring_to_front, build)

    @staticmethod
    def _modifiers(values):
        return modifiers(values)

    def drag(self, *, from_element=None, to_element=None, from_xy=None, to_xy=None,
             button="left", modifiers=None, delivery_mode=None, bring_to_front=False):
        def build():
            if from_element is not None or to_element is not None or button not in BUTTONS:
                raise ValueError("drag supports coordinates and left/middle/right buttons only")
            if not isinstance(from_xy, (list, tuple)) or len(from_xy) != 2 or not isinstance(to_xy, (list, tuple)) or len(to_xy) != 2:
                raise ValueError("drag requires two complete coordinate pairs")
            start, end, mods = point(*from_xy), point(*to_xy), self._modifiers(modifiers)
            def run(epoch, deadline):
                self._pointer_event(start, 0, epoch, deadline)
                self._keys_down(mods, epoch, deadline)
                self._pointer_event(start, BUTTONS[button], epoch, deadline)
                for step in range(1, 21):
                    xy = tuple(round(a + (b - a) * step / 20) for a, b in zip(start, end))
                    self._pointer_event(xy, BUTTONS[button], epoch, deadline)
                    time.sleep(0.02)
                self._pointer_event(end, 0, epoch, deadline)
            return run
        return self._perform("drag", delivery_mode, bring_to_front, build)

    def scroll(self, *, direction, amount=3, element=None, x=None, y=None,
               modifiers=None, delivery_mode=None, bring_to_front=False):
        def build():
            if element is not None or direction not in WHEELS or type(amount) is not int or not 1 <= amount <= 20:
                raise ValueError("scroll requires a supported direction and 1..20 wheel ticks")
            xy = self._pointer if x is None and y is None else point(x, y)
            if xy is None:
                raise ValueError("scroll requires explicit coordinates until a pointer anchor has been delivered")
            mods = self._modifiers(modifiers)
            def run(epoch, deadline):
                self._keys_down(mods, epoch, deadline)
                for _ in range(amount):
                    self._pointer_event(xy, WHEELS[direction], epoch, deadline)
                    self._pointer_event(xy, 0, epoch, deadline)
                    time.sleep(0.03)
            return run
        return self._perform("scroll", delivery_mode, bring_to_front, build)

    def type_text(self, text, *, delivery_mode=None, bring_to_front=False):
        def build():
            text_symbols(text)  # Preflight the complete action before any chunk is sent.
            def run(epoch, deadline):
                for start in range(0, len(text), TYPE_CHUNK):
                    chunk = text[start:start + TYPE_CHUNK]
                    self._event("type", epoch, deadline, text=chunk, event_count=2 * len(chunk))
            return run
        return self._perform("type", delivery_mode, bring_to_front, build)

    def key(self, keys, *, delivery_mode=None, bring_to_front=False):
        def build():
            symbols = combo(keys)
            def run(epoch, deadline):
                self._keys_down(symbols, epoch, deadline)
                for symbol in reversed(symbols):
                    self._event("key", epoch, deadline, symbol=symbol, down=False)
            return run
        return self._perform("key", delivery_mode, bring_to_front, build)

    def list_apps(self):
        return []  # RFB has no application discovery.

    def list_windows(self):
        return []  # RFB has no native window identities.

    def focus_app(self, app, raise_window=False):
        self._invalidate()
        return ActionResult(ok=False, action="focus_app", code="unsupported_action",
                            message="RFB cannot identify/focus/raise an application", path="wayvnc_rfb")

    def set_value(self, value, element=None):
        self._invalidate()
        return ActionResult(ok=False, action="set_value", code="unsupported_action",
                            message="RFB has no AX element/value interface", path="wayvnc_rfb")
