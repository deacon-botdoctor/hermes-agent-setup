"""Optional external RFB identity and local stream-drain locks (stdlib only).

This is not server/compositor cleanup certification. The existing lease is the
only admission authority. No default external owner is inferred from a file's
absence. Lock descriptors are connection-owned and must outlive transport close.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
import weakref
from dataclasses import asdict, dataclass

# HERMES_NATIVE_EXTERNAL_SCREEN_v1: exact source carrier generation, not runtime state.
NATIVE = "native"
EXTERNAL = "external_rfb"
DIAL_SECONDS = 2.0
CLOSE_SECONDS = 2.0
LOCK_SECONDS = 3.0
POLL_SECONDS = 0.1
CLEANUP_BLOCKER = (
    "External input handoff is excluded: exact native participant retirement "
    "and a proven generation-origin managed baseline are required. Local drain, "
    "file deletion, force and historical receipts are not active-owner grants."
)


class ExternalScreenError(RuntimeError):
    pass


def _config(home: str | Path | None):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config_backend import ConfigBackendUnavailable, read_config_doc
    from hermes_cli.managed_scope import apply_managed_overlay
    # Keep native's historic home semantics. Strict external paths are validated
    # only after selection, so Windows/native do not require POSIX or flock.
    path = Path(home) if home is not None else get_hermes_home()
    token = set_hermes_home_override(path)
    try:
        # Do not consume last-known-good/default recovery on malformed config.
        # Backend owns the read; no raw YAML reader or direct config file writes.
        try:
            raw = read_config_doc(path / "config.yaml")
            if raw is not None and not isinstance(raw, dict):
                raise ExternalScreenError("config root must be a mapping")
        except FileNotFoundError:
            raw = None  # native fresh-profile semantics; external authority still excludes
        # Authority is live, not the tool worker's frozen policy snapshot or
        # last-known-good target. These nonsecret keys require literal values.
        cfg = apply_managed_overlay(raw or {}, fail_closed=True)
    except ConfigBackendUnavailable as exc:
        raise ExternalScreenError("Screen config backend unavailable") from exc
    except Exception as exc:
        raise ExternalScreenError("Screen configuration unreadable or malformed") from exc
    finally:
        reset_hermes_home_override(token)
    section = cfg.get("bot_desktop", {})
    if not isinstance(section, dict):
        raise ExternalScreenError("bot_desktop must be a mapping")
    return path, section


def mode(home: str | Path | None = None) -> str:
    path, section = _config(home)
    value = section.get("mode", NATIVE)
    if value not in (NATIVE, EXTERNAL):
        raise ExternalScreenError("bot_desktop.mode must be native or external_rfb; no fallback")
    # An external incarnation must be explicitly retired, never silently hidden
    # by changing mode back to native. Its activity inode is never deleted.
    external_paths = (path / "bot-desktop" / "external-authority.json", path / "bot-desktop" / "activity.lock")
    if value == NATIVE and any(p.exists() or p.is_symlink() for p in external_paths):
        raise ExternalScreenError("external authority exists; mixed native/external lifecycle refused")
    return value


def _literal(raw: str) -> Path:
    if not isinstance(raw, str) or not raw or ":" in raw:
        raise ExternalScreenError("expected a literal absolute UNIX path without display syntax")
    path = Path(raw)
    if not path.is_absolute() or str(path) != raw or ".." in path.parts:
        raise ExternalScreenError("path must be normalized and absolute")
    return path


def private_directory(path: Path, *, owner_only: bool = True) -> None:
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise ExternalScreenError(f"unsafe/symlink/writable directory: {part}")
    info = path.lstat()
    if owner_only and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ExternalScreenError("external state/endpoint parent must be private and owned by this uid")


def canonical_home(raw: str | Path) -> Path:
    path = _literal(str(raw))
    private_directory(path, owner_only=False)
    if path.resolve(strict=True) != path or path.stat().st_uid != os.geteuid():
        raise ExternalScreenError("profile must be a canonical owner-controlled directory")
    return path


def private_endpoint(raw: str) -> tuple[int, int]:
    path = _literal(raw)
    private_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ExternalScreenError("endpoint must be a private, non-symlink, single-link UNIX socket")
    return info.st_dev, info.st_ino


def short_string(value, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ExternalScreenError(f"invalid {name}")
    return value


def _regular(info) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ExternalScreenError("state/lock must be a private, single-link, owner regular file")


def read_record(path: Path, *, maximum: int = 16384) -> dict:
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _regular(os.fstat(fd))
        data = os.read(fd, maximum + 1)
        if len(data) > maximum or os.fstat(fd).st_ino != path.lstat().st_ino:
            raise ExternalScreenError("state size/identity changed")
        try:
            value = json.loads(data)
        except RecursionError as exc:
            raise ExternalScreenError("durable state JSON nesting exceeds decoder limit") from exc
        if not isinstance(value, dict):
            raise ExternalScreenError("state must be an object")
        return value
    finally:
        os.close(fd)


def write_record(path: Path, value: dict) -> None:
    # Caller owns lease.lock. Random exclusive temp avoids following/replacing
    # another writer's temp. Never replace a lock inode.
    import secrets
    private_directory(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(16)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


_open_locks = weakref.WeakSet()


class PrivateLock:
    """One open-file description per link; bounded flock, stable inode, no FD inheritance."""

    def __init__(self, path: Path, *, create: bool = False):
        if os.name != "posix":
            raise ExternalScreenError("external input requires qualified POSIX flock")
        try:
            import fcntl
        except ImportError as exc:
            raise ExternalScreenError("external input requires qualified POSIX flock") from exc
        self.fcntl = fcntl
        self.fd = None
        self.path = path
        self.owner_pid = os.getpid()
        private_directory(path.parent)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
        try:
            os.set_inheritable(fd, False)
            _regular(os.fstat(fd))
            self.identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            self.fd = fd
            self.check()
            _open_locks.add(self)
        except BaseException:
            os.close(fd)
            self.fd = None
            raise

    def check(self):
        if self.fd is None or self.owner_pid != os.getpid():
            raise ExternalScreenError("lock not owned by this process")
        info = self.path.lstat()
        _regular(info)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ExternalScreenError("stable activity/lease lock inode was replaced")

    def take(self, *, exclusive: bool, seconds: float = LOCK_SECONDS):
        deadline = time.monotonic() + seconds
        how = self.fcntl.LOCK_EX if exclusive else self.fcntl.LOCK_SH
        while True:
            self.check()
            try:
                self.fcntl.flock(self.fd, how | self.fcntl.LOCK_NB)
                self.check()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ExternalScreenError("bounded stream-drain/lease lock deadline exceeded")
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            # close, not LOCK_UN: a fork child must never unlock the parent's OFD.
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _fork_child():
    for lock in tuple(_open_locks):
        lock.close()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_fork_child)


@dataclass(frozen=True)
class Identity:
    home: str
    mode: str
    endpoint: str
    generation: str
    authority: str
    device: int
    inode: int
    activity_device: int
    activity_inode: int
    control_endpoint: str
    control_device: int
    control_inode: int
    native_incarnation: str
    lease_device: int
    lease_inode: int
    participants_device: int
    participants_inode: int
    state_device: int
    state_inode: int

    def as_dict(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    def activity(self, *, exclusive=False, seconds=LOCK_SECONDS):
        lock = PrivateLock(Path(self.home) / "bot-desktop/activity.lock")
        try:
            if lock.identity != (self.activity_device, self.activity_inode):
                raise ExternalScreenError("activity identity changed")
            lock.take(exclusive=exclusive, seconds=seconds)
            return lock
        except BaseException:
            lock.close()
            raise

    def validate(self):
        if resolve(self.home) != self:
            raise ExternalScreenError("external mode/endpoint/generation/authority changed")


def _target(home=None):
    raw_home, section = _config(home)
    if mode(raw_home) != EXTERNAL:
        raise ExternalScreenError("external_rfb mode must be explicitly selected")
    home = canonical_home(raw_home)
    target = section.get("external_rfb")
    if not isinstance(target, dict) or set(target) != {
            "endpoint", "generation", "control_endpoint", "native_incarnation"}:
        raise ExternalScreenError("external_rfb requires endpoint, generation, control_endpoint and native_incarnation")
    endpoint = str(_literal(target["endpoint"]))
    generation = short_string(target["generation"], "workstation/session/relay generation")
    control = str(_literal(target["control_endpoint"]))
    import re
    incarnation = target["native_incarnation"]
    if not isinstance(incarnation, str) or re.fullmatch(r"[0-9a-f]{64}", incarnation) is None:
        raise ExternalScreenError("expected explicit native retirement incarnation")
    import sys
    if sys.platform != "linux" or control == endpoint or Path(control).parent != Path(endpoint).parent:
        raise ExternalScreenError("local Linux RFB/control custody in one private directory required; remote relay unsupported")
    # Conservatively reject recorded native ownership without sandbox probes,
    # mutation, reattach, process scanning, or hiding launchers.
    state = home / "bot-desktop"
    for name in ("launcher.pid", "sandbox.json", "env"):
        if (state / name).exists() or (state / name).is_symlink():
            raise ExternalScreenError("recorded native lifecycle conflicts with external_rfb")
    return home, endpoint, generation, control, incarnation


def resolve(home=None) -> Identity:
    home, endpoint, generation, control, incarnation = _target(home)
    from . import generation as generation_lifecycle
    generation_lifecycle.require_current(home)
    device, inode = private_endpoint(endpoint)
    control_device, control_inode = private_endpoint(control)
    record = read_record(home / "bot-desktop/external-authority.json")
    if set(record) != {"schema", "home", "endpoint", "generation", "authority", "activity",
                       "control_endpoint", "native_incarnation", "lease", "participants", "state"} or record["schema"] != 2:
        raise ExternalScreenError("external authority is missing/incomplete")
    authority = short_string(record["authority"], "authority incarnation")
    if (record["home"], record["endpoint"], record["generation"], record["control_endpoint"], record["native_incarnation"]) != (
            str(home), endpoint, generation, control, incarnation):
        raise ExternalScreenError("external authority/config generation mismatch")
    with PrivateLock(home / "bot-desktop/activity.lock") as lock:
        activity = lock.identity
    if record["activity"] != list(activity):
        raise ExternalScreenError("activity inode binding changed")
    with PrivateLock(home / "bot-desktop/lease.lock") as lock:
        lease_id = lock.identity
    with PrivateLock(home / "bot-desktop/participants.lock") as lock:
        participants = lock.identity
    state = (home / "bot-desktop").lstat()
    state_id = (state.st_dev, state.st_ino)
    if any(record[key] != list(value) for key, value in
           (("lease", lease_id), ("participants", participants), ("state", state_id))):
        raise ExternalScreenError("stable lease/participant lock or state directory changed")
    expected = {**record, "device": device, "inode": inode,
                "control_device": control_device, "control_inode": control_inode}
    # Shared endpoint binding for both provider and human bridge, never a
    # plugin-only lock. Read-only consumers never create or repair it.
    binding = read_record(Path(endpoint).parent / (".hermes-rfb-" + hashlib.sha256(Path(endpoint).name.encode()).hexdigest()[:24] + ".json"))
    if binding != expected:
        raise ExternalScreenError("endpoint is not bound to this profile authority/incarnation")
    return Identity(str(home), EXTERNAL, endpoint, generation, authority, device, inode, *activity,
                    control, control_device, control_inode, incarnation, *lease_id, *participants, *state_id)


def identity_mark(home=None) -> tuple:
    try:
        selected = mode(home)
        if selected == NATIVE:
            return (NATIVE,)
        return (EXTERNAL, resolve(home).fingerprint)
    except (OSError, ValueError, TypeError, ExternalScreenError) as exc:
        return ("excluded", type(exc).__name__, str(exc))


def refuse_lifecycle(action: str, home=None) -> None:
    if mode(home) == EXTERNAL:
        raise ExternalScreenError(f"{action} unavailable: RFB service is externally managed; pane close only closes transport")


def require_provider(home=None) -> None:
    if mode(home) != EXTERNAL:
        return
    from plugins.computer_use import configured_backend_name
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(Path(home)) if home is not None else None
    try:
        if configured_backend_name() != "wayvnc":
            raise ExternalScreenError("external_rfb requires its activity-fenced wayvnc backend; alternate input paths refused")
    finally:
        if token is not None:
            reset_hermes_home_override(token)


def initialize_excluded(home=None):
    """Explicit local initialization only; NEVER grants input or repairs a live authority.

    Missing lease is not initialization. Existing authority, lock or binding is
    not removed/replaced. Recovery after unqualified teardown remains blocked.
    """
    import secrets
    home, endpoint, generation, control, incarnation = _target(home)
    state = home / "bot-desktop"
    state.mkdir(mode=0o700, exist_ok=True)
    private_directory(state)
    if any((state / name).exists() or (state / name).is_symlink() for name in
           ("external-authority.json", "lease.json", "participants.json", "participants.lock")):
        raise ExternalScreenError(CLEANUP_BLOCKER)
    with PrivateLock(state / "lease.lock", create=True) as gate:
        gate.take(exclusive=True)
        with PrivateLock(state / "activity.lock", create=True) as activity:
            # No lease lock during drain. New admissions cannot resolve yet.
            gate.close()
            activity.take(exclusive=True)
            with PrivateLock(state / "lease.lock") as finalize:
                finalize.take(exclusive=True)
                if (state / "external-authority.json").exists():
                    raise ExternalScreenError("initialization superseded")
                authority = secrets.token_hex(32)
                device, inode = private_endpoint(endpoint)
                control_device, control_inode = private_endpoint(control)
                with PrivateLock(state / "participants.lock", create=True) as registry:
                    participants = registry.identity
                state_info = state.lstat()
                record = {"schema": 2, "home": str(home), "endpoint": endpoint, "generation": generation,
                          "authority": authority, "activity": list(activity.identity),
                          "control_endpoint": control, "native_incarnation": incarnation,
                          "lease": list(finalize.identity), "participants": list(participants),
                          "state": [state_info.st_dev, state_info.st_ino]}
                binding_path = Path(endpoint).parent / (".hermes-rfb-" + hashlib.sha256(Path(endpoint).name.encode()).hexdigest()[:24] + ".json")
                binding = {**record, "device": device, "inode": inode,
                           "control_device": control_device, "control_inode": control_inode}
                # Exclusive create prevents rebinding another profile, including
                # simultaneous initializers on distinct profile lease locks.
                fd = os.open(binding_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(binding, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                write_record(state / "participants.json", {"schema": 1, "authority": authority, "participants": {}})
                write_record(state / "external-authority.json", record)
                from tools.bot_desktop import lease
                excluded = lease.Lease(holder="excluded", phase="excluded", authority=authority,
                                       endpoint_generation=generation, endpoint_identity=f"{device}:{inode}",
                                       reason="managed-baseline-unproved; preparatory excluded authority only", epoch=1)
                write_record(state / "lease.json", excluded.as_dict())
    return excluded
