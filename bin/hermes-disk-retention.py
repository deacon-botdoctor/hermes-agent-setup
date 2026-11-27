#!/usr/bin/env python3
"""Fail-closed runtime and snapshot retention for one Hermes profile.

Only explicit runtime-candidate, rollback-backup, snapshot, completed-rollout
SQLite working copies, unverified superseded sqlite-families, and regenerable
cache roots are eligible. Current/pending/referenced sqlite-families, live
profile databases, memories, sessions, workspaces, credentials, and client
artifacts are never candidates.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import time
import shutil
import stat
import subprocess
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

UTC = timezone.utc
PROTECTED_HERMES_NAMES = {
    "config.yaml",
    "data",
    "kanban.db",
    "lcm.db",
    "memories",
    "profiles",
    "sessions",
    "state.db",
    "workspace",
    "workspaces",
}
CACHE_REL_PATHS = (
    ".npm",
    ".cache/qmd",
    ".cache/whisper",
    "Library/Caches/Google",
    "Library/Caches/BraveSoftware",
    "Library/Caches/camoufox",
    "Library/Caches/pnpm",
)
BACKUP_PATTERNS = ("*-swap-*", "*-migration-*", "*-upgrade-*")
SQLITE_FAMILY_GRACE_S = int(os.environ.get("HERMES_DISK_RETENTION_SQLITE_GRACE_S", str(6 * 3600)))


@dataclass(frozen=True)
class Candidate:
    path: Path
    kind: str
    size_bytes: int
    mtime: float
    reason: str
    evidence: tuple[tuple[Path, tuple[int, ...]], ...] = ()


class HostProtectionError(ValueError):
    """The host inventory could not establish that any deletion is safe."""


@dataclass(frozen=True)
class ProtectedPath:
    kind: str
    path: Path
    source: str

    def record(self) -> dict[str, str]:
        return {"kind": self.kind, "path": str(self.path), "source": self.source}


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_size(value: str) -> int:
    text = str(value).strip().lower()
    suffixes = {
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
    }
    for suffix, multiplier in suffixes.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)].strip()) * multiplier)
    return int(float(text))


def path_size(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum(
            child.stat().st_size
            for child in path.rglob("*")
            if child.is_file() and not child.is_symlink()
        )
    except OSError:
        return 0


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def read_json(path: Path, description: str) -> tuple[object, bytes]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"unsafe {description}: {path}")
    try:
        raw = path.read_bytes()
        return json.loads(raw), raw
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {description}: {path}") from exc


def runtime_root(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = Path(raw)
    return Path(os.path.normpath(raw)) if root.is_absolute() else None


def receipt_runtime_root(payload: object) -> Path | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("runtime_root")
    if payload.get("kind") != "botdoctor_runtime_binding":
        contract = payload.get("runtime_contract")
        raw = contract.get("runtime_root") if isinstance(contract, dict) else None
    return runtime_root(raw)


def candidate_roots(hermes_home: Path) -> tuple[Path, ...]:
    homes = [hermes_home]
    if hermes_home.parent.name.lower() == "profiles":
        homes.append(hermes_home.parent.parent)
    roots: list[Path] = []
    for home in homes:
        legacy = home / "state/runtime-candidates"
        canonical = home / "runtime-candidates"
        if legacy.is_symlink():
            # This exact redundant alias is not a second deletion root. Keep
            # traversals and runtime evidence on the real directory; arbitrary,
            # dangling, chained, or cyclic root links remain fail-closed.
            target = Path(os.path.normpath(legacy.parent / os.readlink(legacy)))
            if target != canonical or canonical.is_symlink() or not canonical.is_dir():
                raise ValueError(f"unsafe runtime candidate root: {legacy}")
        else:
            roots.append(legacy)
        if canonical.is_symlink():
            raise ValueError(f"unsafe runtime candidate root: {canonical}")
        roots.append(canonical)
    return tuple(dict.fromkeys(roots))


def rollout_roots(hermes_home: Path) -> tuple[Path, ...]:
    homes = [hermes_home]
    if hermes_home.parent.name.lower() == "profiles":
        homes.append(hermes_home.parent.parent)
    return tuple(dict.fromkeys(home / "state/fleet-rollouts" for home in homes))


def validate_generation(root: Path, allowed: tuple[Path, ...], role: str) -> Path:
    generation = root
    if root.is_absolute() and root.parent in allowed:
        allowed_root = root.parent
    elif (
        root.is_absolute()
        and root.name == "hermes-agent"
        and root.parent.parent in allowed
    ):
        # Older profile bindings identify the code root inside the generation,
        # while retention operates on the enclosing direct child.  Normalize
        # this one exact historical layout; never accept arbitrary descendants.
        generation = root.parent
        allowed_root = root.parent.parent
    else:
        raise ValueError(f"unsafe {role} runtime root: {root}")
    if (
        root.is_symlink()
        or generation.is_symlink()
        or allowed_root.is_symlink()
        or not root.is_dir()
        or not generation.is_dir()
    ):
        raise ValueError(f"unsafe {role} runtime root: {root}")
    resolved = generation.resolve(strict=True)
    if resolved.parent != allowed_root.resolve(strict=True):
        raise ValueError(f"unsafe {role} runtime root: {root}")
    return resolved


def active_runtime_root(hermes_home: Path) -> tuple[Path, list[Path]]:
    state = hermes_home / "state"
    binding = state / "runtime-binding.json"
    if not os.path.lexists(binding):
        raise ValueError("active runtime evidence missing")
    payload, _ = read_json(binding, "runtime binding receipt")
    if not isinstance(payload, dict) or payload.get("kind") != "botdoctor_runtime_binding":
        raise ValueError(f"invalid runtime binding receipt: {binding}")
    root = receipt_runtime_root(payload)
    if root is None:
        raise ValueError(f"invalid runtime binding receipt: {binding}")
    receipt_roots: set[Path] = set()
    receipt_paths: list[Path] = []
    for path in sorted((state / "host-receipts").glob("latest-*.json")):
        # Operators preserve superseded receipts beside the canonical latest
        # receipt as ``latest-<host>.before-<change>.json``.  Those historical
        # snapshots are rollback evidence, not current runtime evidence, and
        # may predate the binding-receipt schema.
        if ".before-" in path.name:
            continue
        receipt, _ = read_json(path, "runtime binding receipt")
        receipt_root = receipt_runtime_root(receipt)
        # The central operator host retains canonical receipts for every OS.
        # Foreign Windows paths are not absolute on POSIX (and vice versa), so
        # they cannot validate or threaten this host's local runtime binding.
        if receipt_root is None:
            continue
        receipt_roots.add(receipt_root)
        receipt_paths.append(path)
    stale_receipts: list[Path] = []
    if receipt_roots and root not in receipt_roots:
        try:
            binding_mtime = binding.stat().st_mtime
            conflicting = [path for path in receipt_paths if path.stat().st_mtime >= binding_mtime]
        except OSError as exc:
            raise ValueError("active runtime receipt freshness unreadable") from exc
        if conflicting:
            raise ValueError("active runtime binding conflicts with a newer host receipt")
        stale_receipts = receipt_paths
    return root, stale_receipts


def rollback_runtime_root(hermes_home: Path) -> tuple[Path, list[Path]]:
    state = hermes_home / "state"
    pointer = state / "current-rollback.json"
    payload, _ = read_json(pointer, "current rollback pointer")
    if not isinstance(payload, dict):
        raise ValueError(f"invalid current rollback pointer: {pointer}")
    source_raw = payload.get("source_path")
    source_digest = payload.get("source_sha256")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "botdoctor_current_rollback"
        or not isinstance(source_raw, str)
        or not isinstance(source_digest, str)
        or len(source_digest) != 64
    ):
        raise ValueError(f"invalid current rollback pointer: {pointer}")
    source = Path(source_raw)
    relative: Path | None = None
    for rollout_root in rollout_roots(hermes_home):
        try:
            relative = source.relative_to(rollout_root)
            break
        except ValueError:
            continue
    if relative is None:
        raise ValueError(f"unsafe current rollback source: {source}")
    if len(relative.parts) != 2 or relative.parts[1] not in {
        "receipt.before",
        "runtime-binding.before",
        "unmanaged-predecessor.json",
    }:
        raise ValueError(f"unsafe current rollback source: {source}")
    source_payload, source_bytes = read_json(source, "current rollback source")
    if hashlib.sha256(source_bytes).hexdigest() != source_digest.lower():
        raise ValueError(f"invalid current rollback source digest: {source}")
    if source.name == "unmanaged-predecessor.json":
        if (not isinstance(source_payload, dict)
                or source_payload.get("schema_version") != 1
                or source_payload.get("kind") != "unmanaged_systemd_predecessor"
                or source_payload.get("home") != str(hermes_home)):
            raise ValueError(f"invalid unmanaged rollback source: {source}")
        source_root = runtime_root(source_payload.get("runtime_root"))
    else:
        source_root = receipt_runtime_root(source_payload)
    pointer_root = runtime_root(payload.get("runtime_root"))
    if source_root is None or pointer_root != source_root:
        raise ValueError(f"invalid current rollback pointer: {pointer}")
    if (source_root == hermes_home / "hermes-agent"
            and source.name != "unmanaged-predecessor.json"):
        raise ValueError(f"invalid unmanaged rollback source: {source}")
    superseded_pending: list[Path] = []
    try:
        pointer_mtime = pointer.stat().st_mtime
    except OSError as exc:
        raise ValueError(f"current rollback pointer freshness unreadable: {pointer}") from exc
    pending_transactions = {
        pending
        for rollout_root in rollout_roots(hermes_home)
        for pending in rollout_root.glob("*/rollback-transaction.pending")
    }
    for pending in sorted(pending_transactions):
        try:
            superseded = pending.parent != source.parent and pending.stat().st_mtime < pointer_mtime
        except OSError as exc:
            raise ValueError(f"rollback transaction evidence unreadable: {pending}") from exc
        if not superseded:
            raise ValueError(f"rollback transaction pending: {pending}")
        superseded_pending.append(pending)
    return source_root, superseded_pending


def extract_candidate_roots(text: str) -> set[Path]:
    patterns = (
        (
            r"[A-Za-z]:[\\/]"
            r"(?:[^\\/\s\"']+[\\/])*"
            r"runtime-candidates[\\/][A-Za-z0-9._-]+",
        )
        if os.name == "nt"
        else (r"/(?:[^/:<>\s\"']+/)*runtime-candidates/[A-Za-z0-9._-]+",)
    )
    found: set[Path] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            found.add(Path(os.path.normpath(match.group(0).rstrip("\\/"))))
    return found


def nominal_runtime_references(hermes_home: Path) -> set[Path]:
    homes = [hermes_home]
    if hermes_home.parent.name.lower() == "profiles":
        homes.append(hermes_home.parent.parent)
    referenced: set[Path] = set()
    for home in homes:
        for name in ("hermes-agent", "venv", ".venv"):
            path = home / name
            if not os.path.lexists(path) or not path.is_symlink():
                continue
            try:
                target = path.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"nominal runtime link broken: {path}") from exc
            roots = extract_candidate_roots(str(target))
            if not roots:
                continue
            referenced.update(roots)
    return referenced


def reference_texts(hermes_home: Path, active: Path) -> list[str]:
    paths = [
        active / "venv/pyvenv.cfg",
        active / ".venv/pyvenv.cfg",
        hermes_home / "config.yaml",
        hermes_home / "config.yml",
        hermes_home / "gateway_state.json",
        hermes_home / "state/gateway.json",
        hermes_home / "state/gateway-state.json",
    ]
    texts: list[str] = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace")[:1_000_000])
        except OSError as exc:
            raise ValueError(f"runtime reference evidence unreadable: {path}") from exc
    return texts


def mapped_runtime_reference_paths() -> list[str]:
    """Include loaded dependencies of managed runtimes and their subprocesses."""
    import psutil

    processes = {
        process.pid: process
        for process in psutil.process_iter(["ppid", "exe", "cmdline", "create_time"])
    }
    related = {
        pid for pid, process in processes.items()
        if extract_candidate_roots(" ".join([
            process.info["exe"] or "", *(process.info["cmdline"] or [])
        ]))
    }
    while True:
        children: set[int] = set()
        for pid, process in processes.items():
            parent_pid = process.info["ppid"]
            if pid in related or parent_pid not in related:
                continue
            parent_birth = processes[parent_pid].info["create_time"]
            child_birth = process.info["create_time"]
            if parent_birth is None or child_birth is None:
                raise ValueError(f"runtime process ancestry unreadable: pid={pid}")
            # Windows retains a departed parent's PID, which may now belong to
            # a younger process. Such a link cannot establish ancestry.
            if parent_birth <= child_birth:
                children.add(pid)
        if not children:
            break
        related.update(children)
    paths: list[str] = []
    for pid in sorted(related):
        try:
            paths.extend(mapping.path for mapping in processes[pid].memory_maps())
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise ValueError(f"runtime module reference inventory unreadable: pid={pid}") from exc
    return paths


def live_reference_text() -> str:
    if os.name == "nt":
        script = (
            "$v=@();"
            "Get-CimInstance Win32_Process|%{$v+=[string]$_.ExecutablePath;$v+=[string]$_.CommandLine};"
            "Get-ScheduledTask|%{$_.Actions|%{$v+=[string]$_.Execute;$v+=[string]$_.Arguments}};"
            "$v|?{$_}|ConvertTo-Json -Compress"
        )
        probe = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if probe.returncode:
            raise ValueError("Windows runtime reference inventory failed")
        try:
            values = json.loads(probe.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Windows runtime reference inventory invalid") from exc
        if isinstance(values, str):
            values = [values]
        values.extend(mapped_runtime_reference_paths())
        return "\n".join(str(value) for value in values if value)
    probe = subprocess.run(
        ["ps", "-axo", "command="],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode:
        raise ValueError("process runtime reference inventory failed")
    chunks = [probe.stdout]
    cron = subprocess.run(
        ["crontab", "-l"], text=True, capture_output=True, timeout=10, check=False
    )
    if cron.returncode == 0:
        chunks.append(cron.stdout)
    for directory in (
        Path.home() / ".config/systemd/user",
        Path.home() / "Library/LaunchAgents",
    ):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink():
                try:
                    chunks.append(path.read_text(encoding="utf-8", errors="replace")[:500_000])
                except OSError:
                    continue
    return "\n".join(chunks)


def linux_process_paths() -> tuple[list[ProtectedPath], list[dict[str, object]], int]:
    def process_state(proc: Path) -> tuple[bytes, list[bytes]]:
        # comm is parenthesized and may itself contain spaces and parens.
        prefix, fields = (proc / "stat").read_bytes().rsplit(b") ", 1)
        return prefix.split(b" (", 1)[1], fields.split()

    def dead_or_kernel(fields: list[bytes]) -> bool:
        return fields[0] in {b"Z", b"X", b"x"} or bool(int(fields[6]) & 0x00200000)  # PF_KTHREAD

    items: list[ProtectedPath] = []
    unverified: list[dict[str, object]] = []
    unreadable = 0
    root = os.geteuid() == 0
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            comm, state = process_state(proc)
            if dead_or_kernel(state):
                continue
            values = []
            gaps = []
            for kind, name in (("process_cwd", "cwd"), ("process_exe", "exe")):
                try:
                    values.append((kind, os.readlink(proc / name)))
                except OSError as exc:
                    gaps.append(exc)
            hard_error = next((exc for exc in gaps if root or exc.errno not in {errno.EACCES, errno.EPERM}), None)
            fallback = bool(gaps) and hard_error is None
            cmdline_error = None
            try:
                cmdline = (proc / "cmdline").read_bytes()
            except OSError as exc:
                cmdline_error = exc
                cmdline = b""
            process_uid = None
            if fallback:
                # proc inode ownership is not necessarily the real process UID.
                try:
                    for line in (proc / "status").read_bytes().splitlines():
                        if line.startswith(b"Uid:"):
                            process_uid = int(line.split()[1])
                            break
                except OSError:
                    pass  # UID is diagnostic; the final stat read checks liveness.
            _, current = process_state(proc)
            if current[19] != state[19]:  # starttime, field 22
                raise HostProtectionError(f"process identity changed during lookup: pid={proc.name}")
            if dead_or_kernel(current):
                continue
            if fallback:
                unverified.append({"pid": int(proc.name), "uid": process_uid, "comm": os.fsdecode(comm)})
                if cmdline_error is not None:
                    unreadable += 1
                else:
                    values.extend(("process_cmdline_fallback", os.fsdecode(arg))
                                  for arg in cmdline.split(b"\0") if arg.startswith(b"/"))
            elif hard_error is not None or cmdline_error is not None:
                error = hard_error if hard_error is not None else cmdline_error
                raise HostProtectionError(f"cannot read process paths for pid={proc.name}: {error}")
            else:
                values.append(("process_argv0", os.fsdecode(cmdline.split(b"\0", 1)[0])))
        except (FileNotFoundError, ProcessLookupError):
            # The leader exited between enumerating /proc and reading stat.
            continue
        for kind, raw in values:
            path = Path(raw)
            if path.is_absolute():
                items.append(ProtectedPath(kind, path, f"pid={proc.name}"))
                if kind in {"process_cwd", "process_exe"} and raw.endswith(" (deleted)"):
                    items.append(ProtectedPath(kind, Path(raw[:-10]), f"pid={proc.name}"))
            elif kind != "process_argv0":
                raise HostProtectionError(f"non-absolute {kind}: pid={proc.name}")
    return items, unverified, unreadable


def macos_process_paths() -> tuple[list[ProtectedPath], int]:
    import ctypes

    def processes() -> dict[bytes, tuple[int, bytes]]:
        output = subprocess.run(
            ["/bin/ps", "-axo", "pid=,uid=,stat="], capture_output=True, timeout=30, check=True,
        ).stdout
        return {pid: (int(uid), state) for pid, uid, state in map(bytes.split, output.splitlines())}

    # ps args= flattens argv and cannot distinguish a space in argv[0] from
    # the start of a later argument. Use the same Darwin kernel interface as
    # ps, without displaying or retaining any subsequent arguments.
    sysctl = ctypes.CDLL(None, use_errno=True).sysctl
    sysctl.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
    limit = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.argmax"], timeout=10))
    buffer = ctypes.create_string_buffer(limit)

    def argv0(pid: bytes) -> bytes:
        # CTL_KERN, KERN_PROCARGS2, pid: argc (native int), executable path,
        # optional NUL padding, then argc NUL-terminated arguments.
        mib = (ctypes.c_int * 3)(1, 49, int(pid))
        size = ctypes.c_size_t(limit)
        if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        width = ctypes.sizeof(ctypes.c_int)
        argc = int.from_bytes(buffer[:width], sys.byteorder)
        _, sep, rest = bytes(buffer[width:size.value]).partition(b"\0")
        if not sep:
            raise ValueError("invalid kernel process argument record")
        return rest.lstrip(b"\0").split(b"\0", 1)[0] if argc else b""

    before = processes()
    probe = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-d", "cwd,txt", "-F0pfn"], capture_output=True, timeout=30, check=False,
    )
    if probe.returncode not in {0, 1} or not probe.stdout:
        raise HostProtectionError(f"lsof process inventory failed (exit {probe.returncode})")
    files: dict[bytes, dict[bytes, list[Path]]] = {}
    pid = fd = b""
    for field in probe.stdout.split(b"\0"):
        field = field.lstrip(b"\n")
        if field.startswith(b"p"):
            pid, fd = field[1:], b""
        elif field.startswith(b"f"):
            fd = field[1:]
        elif pid and fd in {b"cwd", b"txt"} and field.startswith(b"n/"):
            files.setdefault(pid, {}).setdefault(fd, []).append(Path(os.fsdecode(field[1:])))

    items: list[ProtectedPath] = []
    unreadable = 0
    uid = os.geteuid()
    gaps: dict[bytes, tuple[int, str]] = {}
    for pid in sorted(before.keys() | files.keys()):
        paths = files.get(pid, {})
        source = f"pid={pid.decode('ascii')}"
        # lsof's txt set includes the main executable and can also include
        # mapped images. Conservatively protect all of them without guessing
        # which txt row is the executable.
        items.extend(ProtectedPath("process_cwd", path, source) for path in paths.get(b"cwd", []))
        items.extend(ProtectedPath("process_exe_or_text", path, source) for path in paths.get(b"txt", []))
        if pid not in before or before[pid][1].startswith(b"Z") or pid == b"0":
            continue
        missing = [os.fsdecode(fd) for fd in (b"cwd", b"txt") if not paths.get(fd)]
        try:
            raw = argv0(pid)
        except (OSError, ValueError) as exc:
            missing.append(f"argv[0] ({exc})")
        else:
            if raw.startswith(b"/"):
                items.append(ProtectedPath("process_argv0", Path(os.fsdecode(raw)), source))
        if missing:
            gaps[pid] = (before[pid][0], ", ".join(missing))
    if gaps:
        after = processes()
        for pid, (original_uid, missing) in gaps.items():
            if pid not in after or after[pid][1].startswith(b"Z"):
                continue
            if uid and uid not in {original_uid, after[pid][0]}:
                unreadable += 1
            else:
                raise HostProtectionError(f"cannot read process {missing} for pid={pid.decode('ascii')}")
    return items, unreadable


def protection_path_forms(path: Path) -> set[Path]:
    forms = {Path(os.path.normpath(path)), Path(os.path.realpath(path))}
    if sys.platform == "darwin":
        # Default APFS aliases names by case and Unicode normalization. On
        # case-sensitive volumes the extra spelling is conservative.
        forms.update({Path(unicodedata.normalize("NFD", str(item)).casefold()) for item in forms})
    return forms


class HostProtection:
    def __init__(self, items: list[ProtectedPath]):
        self.paths: dict[Path, ProtectedPath] = {}
        self.roots: dict[Path, ProtectedPath] = {}
        self.containers: dict[Path, ProtectedPath] = {}
        for item in items:
            for form in protection_path_forms(item.path):
                self.paths.setdefault(form, item)
                if item.kind in {"live_runtime_root", "rollback_runtime_root"}:
                    self.roots.setdefault(form, item)
                for parent in form.parents:
                    self.containers.setdefault(parent, item)

    def refusal(self, target: Path) -> ProtectedPath | None:
        for form in sorted(protection_path_forms(target)):
            if match := self.paths.get(form) or self.containers.get(form):
                return match
            for parent in form.parents:
                if match := self.roots.get(parent):
                    return match
        return None


def host_protection_inventory(home: Path, hermes_home: Path, report: dict[str, object]) -> HostProtection:
    def entries(path: Path) -> list[Path]:
        try:
            path.lstat()
        except FileNotFoundError:
            return []
        return sorted(path.iterdir())

    def declared_path(payload: object, key: str, receipt: Path) -> Path:
        value = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
            raise HostProtectionError(f"missing or invalid {key}: {receipt}")
        return Path(value)

    report["complete"] = False
    report.pop("unreadable_other_user_processes", None)
    report.pop("process_paths_unverified", None)
    report.pop("process_unreadable", None)
    try:
        base = home / ".hermes"
        entries(base)  # Distinguish a missing tree from an unreadable or dangling one.
        selected_base = (hermes_home.parent.parent if hermes_home.parent.name.lower() == "profiles"
                         else hermes_home)
        bases = [base, selected_base]
        homes = [hermes_home]
        for parent in dict.fromkeys(bases):
            homes.append(parent)
            homes.extend(profile for profile in entries(parent / "profiles")
                         if stat.S_ISDIR(profile.stat().st_mode))
        items: list[ProtectedPath] = []
        for profile in dict.fromkeys(homes):
            state = profile / "state"
            entries(state)
            for name, kind in (("runtime-binding.json", "live_runtime_root"),
                               ("current-rollback.json", "rollback_runtime_root")):
                receipt = state / name
                try:
                    receipt.lstat()
                except FileNotFoundError:
                    continue
                payload, _ = read_json(receipt, "host runtime evidence")
                if kind == "rollback_runtime_root" and (
                        not isinstance(payload, dict) or payload.get("kind") != "botdoctor_current_rollback"):
                    raise HostProtectionError(f"invalid current rollback pointer kind: {receipt}")
                items.append(ProtectedPath(kind, declared_path(payload, "runtime_root", receipt), str(receipt)))
                if kind == "rollback_runtime_root":
                    items.append(ProtectedPath("rollback_source", declared_path(payload, "source_path", receipt), str(receipt)))
        report["declared_paths"] = [item.record() for item in items]
        report["process_probe"] = sys.platform if os.name != "nt" else "legacy_windows_runtime_references_only"
        processes = []
        if sys.platform == "linux":
            processes, unverified, unreadable = linux_process_paths()
            report.update(process_paths_unverified={"count": len(unverified), "processes": unverified},
                          process_unreadable=unreadable)
        elif sys.platform == "darwin":
            processes, unreadable = macos_process_paths()
            report["unreadable_other_user_processes"] = unreadable
        elif os.name != "nt":
            raise HostProtectionError(f"unsupported host process inventory: {sys.platform}")
        else:
            report["unreadable_other_user_processes"] = 0
        protection = HostProtection(items + processes)
        report["complete"] = True
        return protection
    except (OSError, ValueError, KeyError, IndexError, StopIteration, RecursionError, subprocess.SubprocessError) as exc:
        if isinstance(exc, HostProtectionError):
            raise
        raise HostProtectionError(f"host protection inventory failed: {exc}") from exc


def first_managed_rollback(hermes_home: Path, active: Path) -> bool:
    """An adopted first generation has no distinct earlier managed generation.

    Only the custody writer's explicit state, bound to the unchanged adopted
    binding and its verified adoption journal, can represent this case. A real
    cutover replaces the pointer and must use ordinary distinct generations.
    """
    pointer, _ = read_json(hermes_home / "state/current-rollback.json", "current rollback pointer")
    if not isinstance(pointer, dict):
        raise ValueError("first managed rollback pointer is invalid")
    if pointer.get("adoption_state") != "first_managed_no_prior_generation":
        return False
    binding, raw = read_json(hermes_home / "state/runtime-binding.json", "runtime binding receipt")
    if not isinstance(binding, dict):
        raise ValueError("first managed binding is invalid")
    adoption = binding.get("metadata_adoption") or {}
    before_digest = adoption.get("before_binding_sha256") if isinstance(adoption, dict) else None
    if not isinstance(before_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", before_digest):
        raise ValueError("first managed adoption identity is invalid")
    digest = hashlib.sha256(raw).hexdigest()
    journal = hermes_home / "state/fleet-rollouts" / ("tenant-metadata-" + before_digest)
    receipt, receipt_raw = read_json(journal / "receipt.json", "first managed adoption receipt")
    before, before_raw = read_json(journal / "binding.before", "first managed adoption beforeimage")
    if not isinstance(receipt, dict) or not isinstance(before, dict):
        raise ValueError("first managed adoption evidence is invalid")
    source = hermes_home / "state/fleet-rollouts" / ("first-managed-" + digest) / "runtime-binding.before"
    adopted_digest = digest
    adopted_pid = binding.get("process", {}).get("pid")
    if pointer.get("closed_rollback_path"):
        closed_path = source.parent / "rollback.closed.json"
        closed, closed_raw = read_json(closed_path, "first managed closed rollback")
        if not isinstance(closed, dict):
            raise ValueError("first managed closed rollback receipt is invalid")
        details = closed.get("details") or {}
        restored = details.get("restored_runtime_binding") or {}
        generation = details.get("rollback_gateway_generation") or {}
        backup = Path(details.get("backup") or "")
        if (pointer.get("closed_rollback_path") != str(closed_path)
                or pointer.get("closed_rollback_sha256") != hashlib.sha256(closed_raw).hexdigest()
                or closed.get("kind") != "posix_rollback_attempt" or closed.get("status") != "closed"
                or closed.get("manifest_host") != binding.get("identity", {}).get("host")
                or not re.fullmatch(r"[0-9a-f]{32}", str(closed.get("rollback_attempt_id") or ""))
                or details.get("standalone_rollback_attempt_id") != closed["rollback_attempt_id"]
                or details.get("ok") is not True or details.get("transaction_cleanup", {}).get("ok") is not True
                or backup.parent != hermes_home / "state/fleet-rollouts" or backup.resolve() != backup
                or restored != {"path": str(hermes_home / "state/runtime-binding.json"),
                                "sha256": digest, "process": binding.get("process")}
                or generation.get("gateway_pid") != binding.get("process", {}).get("pid")
                or generation.get("process_start_epoch") != binding.get("process", {}).get("started_at")
                or generation.get("runtime_root") != str(active)):
            raise ValueError("first managed closed rollback custody is invalid")
        adopted, adopted_raw = read_json(backup / "runtime-binding.before", "closed rollback adopted beforeimage")
        if not isinstance(adopted, dict):
            raise ValueError("closed rollback adopted beforeimage is invalid")
        if ({k: v for k, v in adopted.items() if k != "process"}
                != {k: v for k, v in binding.items() if k != "process"}):
            raise ValueError("closed rollback changed the adopted runtime identity")
        adopted_digest = hashlib.sha256(adopted_raw).hexdigest()
        adopted_pid = adopted.get("process", {}).get("pid")
    if (
        binding.get("schema_version") != 1 or binding.get("kind") != "botdoctor_runtime_binding"
        or binding.get("status") != "active" or binding.get("hermes_home") != str(hermes_home)
        or receipt_runtime_root(binding) != active
        or binding.get("release") != {"base_sha": None, "target_sha": binding.get("target_sha")}
        or adoption.get("schema") != "tenant-launchd-metadata-adoption/v1"
        or adoption.get("restart_performed") is not False
        or hashlib.sha256(before_raw).hexdigest() != adoption.get("before_binding_sha256")
        or before.get("hermes_home") != str(hermes_home) or receipt_runtime_root(before) != active
        or receipt.get("status") != "verified" or receipt.get("binding_after_sha256") != adopted_digest
        or receipt.get("binding_before_sha256") != adoption.get("before_binding_sha256")
        or receipt.get("native_generation", {}).get("gateway_pid") != adopted_pid
        or receipt.get("native_generation", {}).get("runtime_root") != str(active)
        or pointer.get("source_path") != str(source) or pointer.get("source_sha256") != digest
        or pointer.get("adoption_receipt_sha256") != hashlib.sha256(receipt_raw).hexdigest()
        or source.read_bytes() != raw
        or any((hermes_home / "state/host-receipts").glob("latest-*.json"))
    ):
        raise ValueError("first managed rollback custody differs from the adopted generation")
    return True


def protected_runtime_roots(hermes_home: Path) -> tuple[set[Path], dict[str, list[str]]]:
    allowed = candidate_roots(hermes_home)
    has_generations = False
    for root in allowed:
        if not root.exists():
            continue
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"unsafe runtime candidate root: {root}")
        try:
            has_generations = has_generations or any(
                (path.is_dir() or path.is_file()) and not path.is_symlink()
                for path in root.iterdir()
            )
        except OSError as exc:
            raise ValueError(f"runtime candidate root unreadable: {root}") from exc
    if not has_generations:
        # Operator/control profiles may own backups and snapshots without ever
        # hosting a switchable runtime.  With no generation below an eligible
        # runtime-candidates root there is nothing that active/rollback
        # evidence could protect, so requiring a fabricated binding only makes
        # the safe no-op path fail.  Any real generation still takes the full
        # fail-closed evidence path below.
        return set(), {
            "active": [],
            "rollback": [],
            "referenced": [],
            "stale_referenced": [],
            "stale_host_receipts": [],
            "superseded_pending_transactions": [],
            "foreign_live_referenced": [],
            "not_applicable": ["no_runtime_generations"],
        }
    active_root, stale_receipts = active_runtime_root(hermes_home)
    rollback_root, superseded_pending = rollback_runtime_root(hermes_home)
    active = validate_generation(active_root, allowed, "active")
    if rollback_root == hermes_home / "hermes-agent":
        # The digest-bound adoption record owns this original, non-generation root.
        if rollback_root.is_symlink() or not rollback_root.is_dir():
            raise ValueError(f"unsafe rollback runtime root: {rollback_root}")
        rollback = rollback_root.resolve(strict=True)
    else:
        rollback = validate_generation(rollback_root, allowed, "rollback")
    first_managed = active == rollback and first_managed_rollback(hermes_home, active)
    if active == rollback and not first_managed:
        raise ValueError("active and rollback runtime evidence are not distinct")
    configured_references = nominal_runtime_references(hermes_home)
    for text in reference_texts(hermes_home, active):
        configured_references.update(extract_candidate_roots(text))
    live_references = extract_candidate_roots(live_reference_text())
    referenced = configured_references | live_references
    validated_references: set[Path] = set()
    stale_references: set[Path] = set()
    foreign_live_references: set[Path] = set()
    for root in referenced:
        if not root.is_absolute():
            raise ValueError(f"unsafe referenced runtime root: {root}")
        if root.parent not in allowed:
            if root in configured_references:
                raise ValueError(f"unsafe referenced runtime root: {root}")
            foreign_live_references.add(root)
            continue
        if not os.path.lexists(root):
            if root in configured_references:
                raise ValueError(f"unsafe referenced runtime root: {root}")
            stale_references.add(root)
            continue
        validated_references.add(validate_generation(root, allowed, "referenced"))
    protected = {active, rollback, *validated_references}
    return protected, {
        "active": [str(active)],
        "rollback": [] if first_managed else [str(rollback)],
        "first_managed_adopted_predecessor": [str(active)] if first_managed else [],
        "referenced": sorted(str(path) for path in validated_references - {active, rollback}),
        "stale_referenced": sorted(str(path) for path in stale_references),
        "stale_host_receipts": sorted(str(path) for path in stale_receipts),
        "superseded_pending_transactions": sorted(str(path) for path in superseded_pending),
        "foreign_live_referenced": sorted(str(path) for path in foreign_live_references),
    }


@contextmanager
def mutation_lease(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\n")
            handle.flush()
        handle.seek(0)
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise RuntimeError("no supported mutation lease backend")
        except (BlockingIOError, OSError) as exc:
            raise ValueError(f"fleet mutation lease busy: {path}") from exc
        acquired = True
        yield
    finally:
        try:
            handle.seek(0)
            if acquired and fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif acquired and msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


def record(candidate: Candidate, action: str) -> dict[str, object]:
    return {
        "path": str(candidate.path),
        "kind": candidate.kind,
        "size_bytes": candidate.size_bytes,
        "mtime": datetime.fromtimestamp(candidate.mtime, UTC).isoformat().replace("+00:00", "Z")
        if candidate.mtime
        else None,
        "reason": candidate.reason,
        "action": action,
    }


def generations(root: Path, kind: str, reason: str) -> list[Candidate]:
    if not root.is_dir() or root.is_symlink():
        return []
    return sorted(
        (
            Candidate(path, kind, path_size(path), mtime(path), reason)
            for path in root.iterdir()
            if (path.is_dir() or path.is_file()) and not path.is_symlink()
        ),
        key=lambda item: item.mtime,
        reverse=True,
    )


def retention_identity(path: Path) -> tuple[int, ...]:
    """Bind verified bytes to the file without treating our own atime read as a change."""
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_uid, info.st_gid, info.st_nlink)


def checked_rollout_path(path: Path, home: Path, *, directory: bool = False) -> tuple[int, ...]:
    relative = path.relative_to(home)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("rollout evidence escapes profile owner home")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("rollout evidence contains a symlink")
    identity = retention_identity(path)
    if not (stat.S_ISDIR(identity[2]) if directory else stat.S_ISREG(identity[2])):
        raise ValueError("rollout evidence is not an ordinary file/directory")
    return identity


def require_closed_rollout_files(paths: list[Path]) -> None:
    if sys.platform == "linux":
        # fuser inspects the named inodes without lsof's unrelated mount scan.
        # Cross-user visibility requires root; reuse existing noninteractive
        # sudo authority for this read-only command, never grant it here.
        if not paths or any(not path.is_absolute() for path in paths):
            raise ValueError("rollout handle probe requires absolute files")
        filesystem = subprocess.run(
            ["stat", "-f", "-c", "%T", *(str(path) for path in paths)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        # fuser cannot reliably match mapped btrfs files; unknown filesystems
        # are not a reason to weaken the open-handle invariant.
        if (filesystem.returncode or filesystem.stderr.strip()
                or len(filesystem.stdout.splitlines()) != len(paths)
                or any(kind not in {"ext2/ext3", "xfs", "tmpfs"}
                       for kind in filesystem.stdout.splitlines())):
            raise ValueError("rollout handle probe filesystem is unverified")
        command = ["/usr/bin/fuser", "-v", *(str(path) for path in paths)]
        if os.geteuid() != 0:
            command = ["/usr/bin/sudo", "-n", *command]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            raise ValueError("rollout files are open or handle inventory is unverified")
        return
    # The SQLite producer is POSIX-only. Do not infer Windows handle ownership
    # or silently omit the open-file check when the native probe is unavailable.
    command = shutil.which("lsof") if os.name != "nt" else None
    if command is None:
        raise ValueError("rollout open-file inventory unavailable")
    result = subprocess.run(
        [command, "-Fn", "--", *(str(path) for path in paths)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
        raise ValueError("rollout files are open or handle inventory is unverified")


def rollout_reference_present(generation: Path, texts: list[str]) -> bool:
    pattern = re.escape(str(generation)) + r"(?=[/\\\s\"']|$)"
    return any(re.search(pattern, text) for text in texts)


def rollout_evidence_unchanged(evidence: tuple | list, home: Path) -> bool:
    return all(checked_rollout_path(path, home, directory=stat.S_ISDIR(identity[2])) == identity
               for path, identity in evidence)


def rollout_file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def completed_rollout_work(
    home: Path, hermes_home: Path, protected: set[Path],
) -> tuple[list[Candidate], list[dict[str, object]]]:
    """Prune only reconstructible work; never retire a raw family or its restore paths.

    A later rollout archives its predecessor's verified host receipt as
    receipt.before. Those receipts prove completion without a new producer
    schema. Directory age, a terminal family phase, or absence of a pending
    marker alone does not prove the enclosing rollout completed.
    """
    workdirs = [
        path
        for root in rollout_roots(hermes_home)
        for path in sorted(root.glob("*/sqlite-families/*/work"))
    ]
    if not workdirs:
        return [], []
    candidates: list[Candidate] = []
    inventory: list[dict[str, object]] = []
    try:
        pointer_path = hermes_home / "state/current-rollback.json"
        binding_path = hermes_home / "state/runtime-binding.json"
        guards = [(path, checked_rollout_path(path, home)) for path in (pointer_path, binding_path)]
        # Reuse the digest-bound rollback and active-binding checks even for a
        # control profile with no runtime generations (which normally no-ops).
        rollback_runtime_root(hermes_home)
        active, _ = active_runtime_root(hermes_home)
        pointer, _ = read_json(pointer_path, "current rollback pointer")
        rollback_source = Path(pointer["source_path"])
        guards.append((rollback_source, checked_rollout_path(rollback_source, home)))
        current = rollback_source.parent
        references = [live_reference_text(), *reference_texts(hermes_home, active)]
        completions: dict[Path, tuple[dict, Path, tuple[int, ...]]] = {}
        for root in rollout_roots(hermes_home):
            for source in sorted(root.glob("*/receipt.before")):
                try:
                    identity = checked_rollout_path(source, home)
                    receipt, _ = read_json(source, "archived rollout receipt")
                    if not isinstance(receipt, dict):
                        continue
                    contract = receipt.get("runtime_contract") or {}
                    block = receipt.get("sqlite_cutover") or {}
                    rollback = receipt.get("rollback") or {}
                    if not all(isinstance(item, dict) for item in (contract, block, rollback)):
                        continue
                    artifact = runtime_root(rollback.get("artifact"))
                    if (receipt.get("schema_version") != 1 or receipt.get("verified") is not True
                            or receipt.get("closure_eligible") is not True or receipt.get("recovery_pending")
                            or contract.get("profile_home", contract.get("hermes_home")) != str(hermes_home)
                            or artifact is None or artifact.parent not in rollout_roots(hermes_home)
                            or block.get("schema_version") != 1
                            or block.get("backup_root") != str(artifact / "sqlite-families")
                            or block.get("phase") != "candidate_start_attempted"
                            or block.get("candidate_start_attempted") is not True
                            or block.get("failure_kind")):
                        continue
                    completions[artifact] = (receipt, source, identity)
                except (OSError, ValueError, TypeError):
                    continue
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        for path in workdirs:
            inventory.append(record(Candidate(path, "rollout_sqlite_work", path_size(path), mtime(path),
                                              str(exc)), "keep_unverified"))
        return [], inventory
    for work in workdirs:
        family, generation = work.parent, work.parents[2]
        candidate = Candidate(work, "rollout_sqlite_work", path_size(work), mtime(work), "")
        try:
            if generation == current or os.path.lexists(generation / "rollback-transaction.pending"):
                raise ValueError("current rollback or pending rollout")
            if generation not in completions:
                raise ValueError("no verified completed-rollout receipt")
            receipt, source, source_identity = completions[generation]
            completed_runtime = receipt_runtime_root(receipt)
            if completed_runtime is None:
                raise ValueError("completed rollout runtime identity missing")
            if (any(is_relative_to(completed_runtime, item) or is_relative_to(item, completed_runtime)
                    for item in protected) or rollout_reference_present(generation, references)):
                raise ValueError("active, rollback, or referenced rollout")
            manifest_path = family / "manifest.json"
            evidence = [*guards, (source, source_identity)]
            for directory in (generation, family.parent, family, work, family / "raw"):
                evidence.append((directory, checked_rollout_path(directory, home, directory=True)))
            evidence.append((manifest_path, checked_rollout_path(manifest_path, home)))
            manifest, _ = read_json(manifest_path, "SQLite family manifest")
            if not isinstance(manifest, dict):
                raise ValueError("invalid SQLite family manifest")
            db = runtime_root(manifest.get("db"))
            block = receipt["sqlite_cutover"]
            result = manifest.get("result") or {}
            if (db is None or not is_relative_to(db, hermes_home)
                    or family.name != hashlib.sha256(str(db).encode()).hexdigest()[:24]
                    or manifest.get("schema_version") != 1 or manifest.get("phase") != "installed"
                    or manifest.get("capture_kind") != "quiescent_rollback"
                    or manifest.get("consistent_backup") is not True
                    or not re.fullmatch(r"[0-9a-f]{32}", str(manifest.get("operation_id") or ""))
                    or manifest.get("operation_id") != block.get("operation_id")
                    or not isinstance(block.get("databases"), list) or str(db) not in block["databases"]
                    or not isinstance(result, dict) or result.get("ok") is not True
                    or result.get("integrity_check") != "ok"):
                raise ValueError("SQLite family is not a completed owned installation")
            # These are exactly the producer's temporary copies. Unexpected
            # content is not presumed disposable, even inside a directory named work.
            files = sorted(work.iterdir())
            if not files or any(path.name not in {"database", "database-wal", "database-shm", "final"}
                                for path in files):
                raise ValueError("unknown SQLite working-copy contents")
            for path in files:
                identity = checked_rollout_path(path, home)
                if identity[-1] != 1:
                    raise ValueError("SQLite working copy has other hard links")
                evidence.append((path, identity))
            members = manifest.get("members")
            if (not isinstance(members, dict) or set(members) != {"", "-wal", "-shm"}
                    or not isinstance(members.get(""), dict) or members[""].get("present") is not True):
                raise ValueError("raw SQLite family coverage is incomplete")
            for suffix, member in members.items():
                path = family / "raw" / ("database" + suffix)
                if not isinstance(member, dict) or type(member.get("present")) is not bool:
                    raise ValueError("invalid raw SQLite member")
                if not member["present"]:
                    if os.path.lexists(path):
                        raise ValueError("unexpected raw SQLite member")
                    continue
                identity = checked_rollout_path(path, home)
                digest = rollout_file_digest(path)
                if (identity != retention_identity(path) or identity[3] != member.get("size")
                        or digest != member.get("sha256")):
                    raise ValueError("raw SQLite recovery bytes do not match manifest")
                evidence.append((path, identity))
            staged = manifest.get("staged_metadata") or {}
            if not isinstance(staged, dict):
                raise ValueError("invalid installed working-copy evidence")
            for path in files:
                expected = {staged.get("sha256")} if path.name == "final" else {
                    members[path.name.removeprefix("database")].get("sha256")}
                if path.name == "database":
                    expected.add(staged.get("sha256"))
                if rollout_file_digest(path) not in expected:
                    raise ValueError("working copy contains unverified unique bytes")
            require_closed_rollout_files(files)
            if not rollout_evidence_unchanged(evidence, home):
                raise ValueError("rollout evidence changed while planning")
            candidate = Candidate(work, candidate.kind, candidate.size_bytes, candidate.mtime,
                                  "completed superseded rollout; raw recovery family hash-verified", tuple(evidence))
            candidates.append(candidate)
            inventory.append(record(candidate, "prune"))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            inventory.append(record(Candidate(work, candidate.kind, candidate.size_bytes, candidate.mtime,
                                              str(exc)), "keep_unverified"))
    return candidates, inventory


def current_rollback_generation(hermes_home: Path) -> Path | None:
    pointer = hermes_home / "state/current-rollback.json"
    try:
        try:
            pointer.lstat()
        except FileNotFoundError:
            return None
        payload, _ = read_json(pointer, "current rollback pointer")
    except (ValueError, OSError, RecursionError) as exc:
        raise HostProtectionError(f"cannot read current rollback pointer: {pointer}") from exc
    if not isinstance(payload, dict) or payload.get("kind") != "botdoctor_current_rollback":
        raise HostProtectionError(f"invalid current rollback pointer kind: {pointer}")
    source = payload.get("source_path")
    if not isinstance(source, str) or not source.strip():
        raise HostProtectionError(f"current rollback pointer has no source_path: {pointer}")
    path = Path(source)
    if path.name not in {"receipt.before", "runtime-binding.before", "unmanaged-predecessor.json"}:
        return None
    generation = path.parent
    if generation.parent.name != "fleet-rollouts":
        return None
    return generation


def stale_unverified_sqlite_families(
    home: Path,
    hermes_home: Path,
    protected: set[Path],
    verified_work: list[Candidate],
) -> tuple[list[Candidate], list[dict[str, object]]]:
    """Prune sqlite-families on superseded generations that were not receipt-verified.

    Verified completed rollouts still only lose reconstructible work copies.
    Current rollback, pending, referenced, and fresh in-progress generations stay.
    """
    candidates: list[Candidate] = []
    inventory: list[dict[str, object]] = []
    verified_generations = {
        candidate.path.parents[2].resolve()
        for candidate in verified_work
        if candidate.kind == "rollout_sqlite_work"
    }
    current = current_rollback_generation(hermes_home)
    current_resolved = current.resolve() if current is not None else None
    try:
        active, _ = active_runtime_root(hermes_home)
        references = [live_reference_text(), *reference_texts(hermes_home, active)]
    except (OSError, ValueError, subprocess.SubprocessError):
        references = [live_reference_text()]
    now = time.time()
    for root in rollout_roots(hermes_home):
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            generations_found = list(root.iterdir())
        except OSError:
            continue
        for generation in generations_found:
            families = generation / "sqlite-families"
            if (
                not generation.is_dir()
                or generation.is_symlink()
                or not families.is_dir()
                or families.is_symlink()
            ):
                continue
            resolved_generation = generation.resolve()
            if resolved_generation in verified_generations:
                continue
            if current_resolved is not None and resolved_generation == current_resolved:
                inventory.append(
                    record(
                        Candidate(
                            families,
                            "rollout_sqlite_families",
                            path_size(families),
                            mtime(families),
                            "current rollback generation",
                        ),
                        "keep_protected",
                    )
                )
                continue
            if os.path.lexists(generation / "rollback-transaction.pending"):
                inventory.append(
                    record(
                        Candidate(
                            families,
                            "rollout_sqlite_families",
                            path_size(families),
                            mtime(families),
                            "pending rollout",
                        ),
                        "keep_protected",
                    )
                )
                continue
            try:
                age = now - generation.stat().st_mtime
            except OSError:
                continue
            if age < SQLITE_FAMILY_GRACE_S:
                inventory.append(
                    record(
                        Candidate(
                            families,
                            "rollout_sqlite_families",
                            path_size(families),
                            mtime(families),
                            "in-progress rollout grace",
                        ),
                        "keep_protected",
                    )
                )
                continue
            if rollout_reference_present(generation, references):
                inventory.append(
                    record(
                        Candidate(
                            families,
                            "rollout_sqlite_families",
                            path_size(families),
                            mtime(families),
                            "referenced rollout",
                        ),
                        "keep_protected",
                    )
                )
                continue
            if any(
                is_relative_to(families, item) or is_relative_to(item, families)
                for item in protected
            ):
                inventory.append(
                    record(
                        Candidate(
                            families,
                            "rollout_sqlite_families",
                            path_size(families),
                            mtime(families),
                            "protected runtime overlap",
                        ),
                        "keep_protected",
                    )
                )
                continue
            candidate = Candidate(
                families,
                "rollout_sqlite_families",
                path_size(families),
                mtime(families),
                "unverified superseded sqlite-families",
            )
            candidates.append(candidate)
            inventory.append(record(candidate, "prune"))
    return candidates, inventory


def space_policy(hermes_home: Path) -> dict | None:
    path = hermes_home / "config/host-steward-retention.json"
    if not path.exists():
        return None
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("retention policy contains symlink")
    raw, _ = read_json(path, "host retention policy")
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("invalid host retention policy")
    trigger = raw.get("trigger_free_percent", 20)
    target = raw.get("target_free_percent", 25)
    horizon = raw.get("exhaustion_hours", 24)
    if (not all(type(v) in (int, float) and 0 < v < 100 for v in (trigger, target))
            or target <= trigger or type(horizon) not in (int, float) or not 1 <= horizon <= 168):
        raise ValueError("invalid free-space target or forecast horizon")
    return {**raw, "trigger_free_percent": trigger, "target_free_percent": target,
            "exhaustion_hours": horizon}


def artifact_path(raw: str, home: Path, hermes_home: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("artifact path must be absolute without symlinks")
    path = path.resolve()
    if not is_relative_to(path, home) or path in {home, hermes_home}:
        raise ValueError("artifact must be a specific path within its owner home")
    protected = [home / ".codex/sessions", home / ".codex/archived_sessions",
                 home / ".ssh", home / ".gbrain", hermes_home / "data",
                 hermes_home / "profiles", hermes_home / "memories", hermes_home / "sessions",
                 hermes_home / "state/promotion", hermes_home / "state/host-steward"]
    if any(is_relative_to(path, p) or is_relative_to(p, path) for p in protected):
        raise ValueError("artifact intersects protected identity, memory or control state")
    if path.name in {"auth.json", "config.yaml", "config.toml", ".env"} or "secrets" in path.parts:
        raise ValueError("credentials or configuration cannot be disposable artifacts")
    return path


def process_birth(pid: int) -> str:
    if pid <= 0:
        raise ValueError("artifact owner PID must be positive")
    if os.name == "nt":
        import psutil
        try:
            return "win32:" + str(psutil.Process(pid).create_time())
        except psutil.NoSuchProcess:
            return ""
        except psutil.Error as exc:
            raise ValueError("artifact owner process inventory unavailable") from exc
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, timeout=10, check=False,
                            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    if result.returncode not in {0, 1} or result.stderr.strip():
        raise ValueError("artifact owner process inventory unavailable")
    return result.stdout.strip()


def artifact_registry(hermes_home: Path) -> Path:
    return hermes_home / "state/disk-retention-artifacts"


def has_registered_descendant(path: Path, home: Path, hermes_home: Path) -> bool:
    if not path.is_dir():
        return False
    for registration in artifact_registry(hermes_home).glob("*.json"):
        raw, _ = read_json(registration, "artifact registration")
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError(f"invalid artifact registration: {registration}")
        try:
            child = artifact_path(raw["path"], home, hermes_home)
        except (KeyError, TypeError, ValueError, OSError):
            # An unusable registration cannot authorize deletion, and one outside
            # this directory (for example under another owner's root) cannot be
            # its descendant. Keep the directory only when the raw path is inside it.
            try:
                child = Path(str(raw.get("path") or "")).expanduser().absolute()
            except (TypeError, ValueError, OSError):
                continue
            if child.is_absolute() and child != path and is_relative_to(child, path):
                return True
            continue
        if child != path and is_relative_to(child, path) and child.exists():
            return True
    return False


def register_artifact(args: argparse.Namespace, home: Path, hermes_home: Path) -> int:
    path = artifact_path(args.register_artifact, home, hermes_home)
    if not args.artifact_owner or not 60 <= args.artifact_ttl <= 365 * 86400 or args.artifact_budget <= 0:
        raise ValueError("artifact requires owner, expiry and positive size budget")
    if args.create_artifact_directory:
        path.mkdir(parents=True, exist_ok=False)
    identity = path.stat()
    if not (stat.S_ISREG(identity.st_mode) or stat.S_ISDIR(identity.st_mode)):
        raise ValueError("artifact must be a regular file or directory")
    birth = process_birth(args.artifact_pid)
    if not birth:
        raise ValueError("artifact owner process is not running")
    backup = None
    if args.artifact_backup:
        source = Path(args.artifact_backup).expanduser()
        if any(p.is_symlink() for p in (source, *source.parents)):
            raise ValueError("backup path contains symlink")
        source = source.resolve()
        if not source.is_file() or source.is_symlink() or is_relative_to(source, path):
            raise ValueError("backup must be a separate regular file")
        if not args.artifact_backup_sha256 or not re.fullmatch(r"[a-f0-9]{64}", args.artifact_backup_sha256):
            raise ValueError("backup requires an expected SHA256")
        if rollout_file_digest(source) != args.artifact_backup_sha256:
            raise ValueError("backup digest does not match")
        if args.artifact_kind == "recovery" and (not path.is_file() or rollout_file_digest(path) != args.artifact_backup_sha256):
            raise ValueError("recovery backup must preserve the exact artifact bytes")
        backup = {"path": str(source), "sha256": args.artifact_backup_sha256}
    key = hashlib.sha256(str(path).encode()).hexdigest()
    record_path = artifact_registry(hermes_home) / f"{key}.json"
    if record_path.exists():
        existing, _ = read_json(record_path, "artifact registration")
        if not isinstance(existing, dict):
            raise ValueError("invalid artifact registration")
        if existing.get("owner") != args.artifact_owner:
            raise ValueError("artifact belongs to another owner")
    atomic_json(record_path, {
        "schema_version": 1, "path": str(path), "owner": args.artifact_owner,
        "kind": args.artifact_kind, "expires_epoch": time.time() + args.artifact_ttl,
        "budget_bytes": args.artifact_budget, "device": identity.st_dev, "inode": identity.st_ino,
        "owner_pid": args.artifact_pid, "owner_birth": birth, "backup": backup,
        "registered_at": now_iso(),
    })
    if getattr(args, "codex_workspace", False):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
            f"Host Steward registered {path} as working data owned by {args.artifact_owner}, with a seven-day review and 5 GiB budget. "
            "Keep final documents outside work. Register disposable media/cache and recovery artifacts before writing; recovery deletion requires verified backup evidence."}}))
    else:
        print(json.dumps({"registered": str(path), "owner": args.artifact_owner}))
    return 0


def copy_recovery_artifact(args: argparse.Namespace, home: Path, hermes_home: Path) -> int:
    """Own the destination before any recovery image bytes are written."""
    destination = artifact_path(args.register_artifact, home, hermes_home)
    source = Path(args.copy_recovery_source).expanduser()
    if (not source.is_absolute() or any(p.is_symlink() for p in (source, *source.parents))
            or not source.is_file() or not is_relative_to(source.resolve(), home)):
        raise ValueError("recovery source must be a regular file within the owning home without symlinks")
    if args.artifact_kind != "recovery" or args.create_artifact_directory or args.artifact_backup:
        raise ValueError("recovery copy requires recovery kind; register backup evidence after the copy")
    if not args.artifact_owner or not 60 <= args.artifact_ttl <= 365 * 86400:
        raise ValueError("recovery copy requires an owner and valid expiry")
    before = source.stat()
    offset = args.recovery_offset
    length = args.recovery_length if args.recovery_length is not None else before.st_size - offset
    if offset < 0 or length <= 0 or offset + length > before.st_size or length > args.artifact_budget:
        raise ValueError("recovery range must fit the source and artifact size budget")
    text_sha256 = getattr(args, "recovery_text_sha256", None)
    if text_sha256 is not None:
        if (not re.fullmatch(r"[0-9a-f]{64}", text_sha256) or offset != 0
                or length != before.st_size or length > 1024 * 1024):
            raise ValueError("text recovery requires an exact full-file digest and a 1 MiB limit")
        with source.open("rb") as text_reader:
            snapshot = text_reader.read(length + 1)
        if len(snapshot) != length:
            raise ValueError("text recovery source changed size before validation")
        snapshot.decode("utf-8")
        if b"\0" in snapshot or hashlib.sha256(snapshot).hexdigest() != text_sha256:
            raise ValueError("text recovery source is binary or differs from its expected digest")
    else:
        require_closed_rollout_files([source])
    usage = shutil.disk_usage(destination.parent)
    policy = space_policy(hermes_home)
    reserve = max(args.block_free_bytes, int(usage.total * policy["target_free_percent"] / 100) if policy else args.warn_free_bytes)
    if usage.free - length < reserve:
        raise ValueError(f"recovery allocation would breach free-space reserve: free={usage.free} allocation={length} reserve={reserve}")
    args.artifact_pid = os.getpid()
    with source.open("rb") as reader:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError("recovery source changed before allocation")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as writer:
            register_artifact(args, home, hermes_home)
            reader.seek(offset)
            remaining = length
            digest = hashlib.sha256()
            while remaining:
                chunk = reader.read(min(remaining, 8 * 1024 * 1024))
                if not chunk:
                    raise ValueError("recovery source ended before the requested range")
                if shutil.disk_usage(destination.parent).free - len(chunk) < reserve:
                    raise ValueError("recovery copy stopped at the free-space reserve; registered partial file retained")
                writer.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
            current = source.stat()
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if any((v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns) != identity for v in (after, current)):
                raise ValueError("recovery source changed during copy; registered destination retained")
    if text_sha256 is not None and digest.hexdigest() != text_sha256:
        raise ValueError("text recovery copy differs from its exact preimage; registered destination retained")
    if rollout_file_digest(destination) != digest.hexdigest():
        raise ValueError("recovery destination digest mismatch; registered destination retained")
    free_after = shutil.disk_usage(destination.parent).free
    proof = {"source": str(source), "destination": str(destination), "offset": offset,
             "bytes": length, "sha256": digest.hexdigest(), "copied_at": now_iso(),
             "free_after_bytes": free_after, "reserve_free_bytes": reserve,
             "status": "pass" if free_after >= reserve else "block"}
    atomic_json(hermes_home / "state/disk-retention-recovery-last.json", proof)
    print(json.dumps(proof))
    return 0 if proof["status"] == "pass" else 2


def registered_recovery_bytes(path: Path, home: Path, hermes_home: Path) -> int:
    """Charge separately owned recovery files to their own registered budgets."""
    owned: dict[Path, int] = {}
    for record_path in artifact_registry(hermes_home).glob("*.json"):
        raw, raw_bytes = read_json(record_path, "artifact registration")
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or raw.get("kind") != "recovery":
            continue
        try:
            child = artifact_path(raw["path"], home, hermes_home)
            if child == path or not child.is_relative_to(path) or not child.is_file():
                continue
            identity = child.stat()
            if ((identity.st_dev, identity.st_ino) != (raw["device"], raw["inode"])
                    or identity.st_size > int(raw["budget_bytes"])
                    or record_path.read_bytes() != raw_bytes):
                continue
            owned[child] = identity.st_size
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return sum(owned.values())


def artifact_path_size(path: Path, deadline: float) -> int | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    code = "import runpy,sys; from pathlib import Path; print(runpy.run_path(sys.argv[1])['path_size'](Path(sys.argv[2])))"
    try:
        result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve()), str(path)],
                                capture_output=True, text=True, timeout=remaining)
        return max(0, int(result.stdout.strip())) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def artifact_candidate(record_path: Path, home: Path, hermes_home: Path,
                       protected: set[Path], *, pressure: bool = False,
                       size_deadline: float | None = None) -> tuple[Candidate | None, dict]:
    raw, raw_bytes = read_json(record_path, "artifact registration")
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("invalid artifact registration")
    path = artifact_path(raw["path"], home, hermes_home)
    row = {"path": str(path), "kind": "generated_artifact", "owner": raw.get("owner"),
           "action": "keep", "reason": "not expired and within budget", "size_bytes": 0}
    if not path.exists():
        return None, {**row, "reason": "already absent"}
    identity = path.stat()
    if (identity.st_dev, identity.st_ino) != (raw["device"], raw["inode"]):
        return None, {**row, "reason": "artifact identity changed; owner decision required"}
    size = (artifact_path_size(path, size_deadline if size_deadline is not None else time.monotonic() + 5)
            if stat.S_ISDIR(identity.st_mode) else identity.st_size)
    if size is None:
        return None, {**row, "size_bytes": None, "size_observation": "unavailable",
                      "expires_epoch": raw["expires_epoch"], "budget_bytes": raw["budget_bytes"],
                      "reason": "artifact retained; directory size observation exceeded bounded budget"}
    recovery_bytes = (registered_recovery_bytes(path, home, hermes_home)
                      if raw.get("kind") == "working" and path.is_dir() else 0)
    budget_size = max(0, size - recovery_bytes)
    row.update(size_bytes=size, expires_epoch=raw["expires_epoch"], budget_bytes=raw["budget_bytes"],
               budget_size_bytes=budget_size, registered_recovery_bytes=recovery_bytes,
               budget_exceeded=budget_size > int(raw["budget_bytes"]))
    if any(is_relative_to(path, p) or is_relative_to(p, path) for p in protected):
        return None, {**row, "reason": "required runtime or rollback"}
    observed_birth = process_birth(int(raw["owner_pid"]))
    if observed_birth == raw["owner_birth"]:
        return None, {**row, "reason": "owner process active"}
    # Protect task ownership independently of the producer PID.
    for lease in (hermes_home / "state/host-steward/leases").glob("*.json"):
        payload, _ = read_json(lease, "task ownership")
        if payload.get("task_id") == raw["owner"]:
            return None, {**row, "reason": "task lease retained"}
    kind = raw.get("kind")
    if kind == "working":
        return None, {**row, "reason": "protected working copy; workflow completion decision required"}
    if (time.time() < float(raw["expires_epoch"]) and size <= int(raw["budget_bytes"])
            and not (pressure and kind in {"scratch", "cache"})):
        return None, row
    if kind not in {"scratch", "cache", "recovery"}:
        return None, {**row, "reason": "unknown lifecycle class; owner decision required"}
    if kind == "recovery":
        backup = raw.get("backup")
        if not isinstance(backup, dict):
            return None, {**row, "reason": "required recovery backup evidence missing"}
        backup_path = Path(backup["path"])
        if (backup_path.is_symlink() or not backup_path.is_file()
                or is_relative_to(backup_path, path)
                or any(p.is_symlink() for p in backup_path.parents)
                or not path.is_file() or rollout_file_digest(path) != backup["sha256"]
                or rollout_file_digest(backup_path) != backup["sha256"]):
            return None, {**row, "reason": "required recovery backup digest unverified"}
    files = [path] if path.is_file() else [p for p in path.rglob("*") if not p.is_dir()]
    if any(p.is_symlink() or not p.is_file()
           or any(p.name.lower().endswith(ext + sidecar)
                  for ext in (".db", ".sqlite", ".sqlite3")
                  for sidecar in ("", "-wal", "-shm", "-journal"))
           or ".git" in p.parts or p.name in {"auth.json", "config.yaml", "config.toml", ".env"}
           or "secrets" in p.parts for p in files):
        return None, {**row, "reason": "database, repository or non-regular file protected"}
    if len(files) > 10000:
        return None, {**row, "reason": "open-file inventory exceeds bounded probe"}
    try:
        for offset in range(0, len(files), 100):
            require_closed_rollout_files(files[offset:offset + 100])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return None, {**row, "reason": f"active file or unverified handles: {exc}"}
    if record_path.read_bytes() != raw_bytes:
        raise ValueError("artifact registration changed during inventory")
    reason = ("expired" if time.time() >= float(raw["expires_epoch"]) else
              "size budget exceeded" if size > int(raw["budget_bytes"]) else "disposable artifact under pressure")
    return Candidate(path, "generated_artifact", size, mtime(path), reason,
                     ((record_path, retention_identity(record_path)),)), {**row, "action": "prune", "reason": reason}


def discover_consumers(home: Path, hermes_home: Path, policy: dict, *, write_cache: bool = True) -> dict:
    cache_path = hermes_home / "state/disk-consumer-inventory.json"
    if (cache_path.is_file() and time.time() - cache_path.stat().st_mtime < 3600
            and cache_path.stat().st_mtime >= mtime(hermes_home / "config/host-steward-retention.json")):
        cached, _ = read_json(cache_path, "disk consumer inventory")
        if cached.get("collector_version") == 3:
            return cached
    priority = policy.get("discovery_roots", [])
    if not isinstance(priority, list) or any(not isinstance(raw, str) or not Path(raw).is_absolute()
                                             or not is_relative_to(Path(raw), home) for raw in priority):
        raise ValueError("priority discovery roots must remain within the owner home")
    roots = [*(Path(raw).resolve() for raw in priority), hermes_home, home]
    if sys.platform == "darwin":
        roots += [Path("/System/Volumes/Data"), Path("/")]
    elif sys.platform == "linux":
        roots += [Path("/")]
    else:
        return {"checked_at": now_iso(), "complete": False, "consumers": [],
                "gaps": [{"path": str(home), "reason": "native broad discovery not supported on this platform"}]}
    rows, gaps = [], []
    if sys.platform == "darwin":
        gaps.append({"path": "/System/Volumes/Data",
                     "reason": "directory inventory excludes APFS snapshot and shared-block accounting; OS retention decision required"})
    minimum = int(policy.get("major_consumer_bytes", 1024**3))
    for root in dict.fromkeys(roots):
        command = ["du", "-x", "-a", "-k", str(root)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)
            output = result.stdout
            if result.returncode or result.stderr.strip():
                gaps.append({"path": str(root), "reason": "scan incomplete: " + result.stderr.strip()[:1000]})
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            gaps.append({"path": str(root), "reason": "scan time budget reached"})
        except OSError as exc:
            gaps.append({"path": str(root), "reason": str(exc)})
            continue
        for line in output.splitlines():
            size, separator, path = line.partition("\t")
            if separator and size.isdigit() and int(size) * 1024 >= minimum:
                rows.append({"path": path, "allocated_bytes": int(size) * 1024,
                             "reason": "major consumer; covered retention record or owner decision required"})
    result = {"collector_version": 3, "checked_at": now_iso(), "complete": not gaps, "consumers": rows, "gaps": gaps,
              "measurement": "allocated directory totals; nested totals overlap; metadata only"}
    if write_cache:
        atomic_json(cache_path, result)
    return result


def headroom_state(home: Path, hermes_home: Path, policy: dict) -> dict:
    usage = shutil.disk_usage(home)
    path = hermes_home / "state/disk-headroom.json"
    previous = {}
    if path.is_file():
        previous, _ = read_json(path, "disk headroom history")
    now = time.time()
    samples = [s for s in previous.get("samples", []) if now - s["epoch"] <= 48 * 3600]
    # A recovery resets the growth segment; do not average a near-full event away.
    if samples and usage.free > samples[-1]["free_bytes"] + 1024**3:
        samples = []
    rate = 0.0
    if samples and now - samples[0]["epoch"] >= 300:
        rate = max(0.0, (samples[0]["free_bytes"] - usage.free) / (now - samples[0]["epoch"]))
    exhaustion = usage.free / rate / 3600 if rate else None
    trigger = int(usage.total * policy["trigger_free_percent"] / 100)
    target = int(usage.total * policy["target_free_percent"] / 100)
    forecast = exhaustion is not None and exhaustion <= policy["exhaustion_hours"]
    active = usage.free < trigger or forecast or bool(previous.get("recovery_pending"))
    if usage.free >= target and not forecast:
        active = False
    return {"trigger_free_bytes": trigger, "target_free_bytes": target,
            "free_bytes": usage.free, "total_bytes": usage.total,
            "growth_bytes_per_second": rate, "exhaustion_hours": exhaustion,
            "growth_pressure": forecast, "recovery_pending": active,
            "samples": [*samples, {"epoch": now, "free_bytes": usage.free}][-192:]}


def collect_plan(
    home: Path,
    hermes_home: Path,
    protected: set[Path],
    keep_backups: int,
    keep_snapshots: int,
    prune_caches: bool,
    min_cache_bytes: int,
) -> tuple[list[Candidate], list[dict[str, object]]]:
    candidates: list[Candidate] = []
    inventory: list[dict[str, object]] = []
    rollout_candidates, rollout_inventory = completed_rollout_work(home, hermes_home, protected)
    candidates.extend(rollout_candidates)
    inventory.extend(rollout_inventory)
    stale_candidates, stale_inventory = stale_unverified_sqlite_families(
        home, hermes_home, protected, rollout_candidates
    )
    candidates.extend(stale_candidates)
    inventory.extend(stale_inventory)
    for root in candidate_roots(hermes_home):
        for candidate in generations(root, "runtime_candidate", f"direct child of {root}"):
            target = candidate.path.resolve()
            keep = any(
                target == item or is_relative_to(target, item) or is_relative_to(item, target)
                for item in protected
            )
            inventory.append(record(candidate, "keep_protected" if keep else "prune"))
            if not keep:
                candidates.append(candidate)
    backups: list[Candidate] = []
    for pattern in ("upgrade-backup-*", "embed-standardize-backup-*", "targeted-overlay-backup-*"):
        backups.extend(
            Candidate(path, "rollback_backup", path_size(path), mtime(path), f"matches {pattern}")
            for path in home.glob(pattern)
            if path.is_dir() and not path.is_symlink()
        )
    backup_root = hermes_home / "backups"
    for pattern in BACKUP_PATTERNS:
        backups.extend(
            Candidate(path, "rollback_backup", path_size(path), mtime(path), f"matches {pattern}")
            for path in backup_root.glob(pattern)
            if path.is_dir() and not path.is_symlink()
        )
    for index, candidate in enumerate(sorted(backups, key=lambda item: item.mtime, reverse=True)):
        action = "keep" if index < keep_backups else "prune"
        inventory.append(record(candidate, action))
        if action == "prune":
            candidates.append(candidate)
    for rel in ("apply-snapshots", "patch-snapshots", "fleet-stage"):
        snapshot_root = hermes_home / "state" / rel
        items = generations(snapshot_root, "snapshot", f"generation under {snapshot_root}")
        for index, candidate in enumerate(items):
            if (rel == "fleet-stage" and candidate.path.is_dir()
                    and re.fullmatch(r"golden-[0-9a-f]{40}", candidate.path.name)):
                action = "keep_protected"
            else:
                action = "keep" if index < keep_snapshots else "prune"
            inventory.append(record(candidate, action))
            if action == "prune":
                candidates.append(candidate)
    if prune_caches:
        for rel in CACHE_REL_PATHS:
            path = home / rel
            candidate = Candidate(path, "cache", path_size(path), mtime(path), "regenerable cache allowlist")
            if path.exists() and not path.is_symlink() and candidate.size_bytes >= min_cache_bytes:
                candidates.append(candidate)
                inventory.append(record(candidate, "prune"))
    home_resolved = home.resolve()
    hermes_resolved = hermes_home.resolve()
    for candidate in candidates:
        target = candidate.path.resolve()
        if not is_relative_to(target, home_resolved):
            raise ValueError(f"refusing path outside home: {candidate.path}")
        if is_relative_to(target, hermes_resolved):
            relative = target.relative_to(hermes_resolved)
            if relative.parts and relative.parts[0] in PROTECTED_HERMES_NAMES:
                raise ValueError(f"refusing protected Hermes path: {candidate.path}")
    return candidates, inventory


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def fileprovider_relief_config(path: Path, home: Path, hermes_home: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    payload, _ = read_json(path, "File Provider disk-relief config")
    if not isinstance(payload, dict):
        raise ValueError("File Provider disk-relief config must be an object")
    if payload.get("schema_version") != 1:
        raise ValueError("File Provider disk-relief config schema_version must be 1")
    if payload.get("enabled") is not True:
        return None
    target_free = payload.get("target_free_bytes")
    targets = payload.get("targets")
    if not isinstance(target_free, int) or target_free <= 0:
        raise ValueError("File Provider target_free_bytes must be a positive integer")
    if not isinstance(targets, list) or not targets:
        raise ValueError("File Provider targets must be a non-empty list")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(targets):
        if not isinstance(item, dict):
            raise ValueError(f"File Provider target {index} must be an object")
        raw_path = item.get("path")
        raw_root = item.get("allowed_root")
        if not isinstance(raw_path, str) or not isinstance(raw_root, str):
            raise ValueError(f"File Provider target {index} requires path and allowed_root")
        target = Path(raw_path).expanduser().resolve()
        allowed_root = Path(raw_root).expanduser().resolve()
        if not target.is_absolute() or not allowed_root.is_absolute():
            raise ValueError(f"File Provider target {index} must use absolute paths")
        if not is_relative_to(target, home) or is_relative_to(target, hermes_home):
            raise ValueError(f"File Provider target {index} is outside the safe host scope")
        if target == allowed_root or not is_relative_to(target, allowed_root):
            raise ValueError(f"File Provider target {index} must be a child of allowed_root")
        normalized.append({"path": str(target), "allowed_root": str(allowed_root)})
    return {
        "schema_version": 1,
        "target_free_bytes": target_free,
        "targets": normalized,
    }


def parse_json_result(result: subprocess.CompletedProcess[str], label: str) -> dict[str, object]:
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{label} produced no JSON result: {(result.stderr or '')[:300]}")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} produced invalid JSON") from exc
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        detail = payload.get("error") if isinstance(payload, dict) else None
        raise RuntimeError(f"{label} failed: {detail or (result.stderr or '')[:300]}")
    return payload


def run_fileprovider_relief(
    config: dict[str, object], home: Path, script: Path, *, guard: Callable[[Path], bool] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    if sys.platform != "darwin":
        return [], [{"path": str(script), "error": "File Provider relief requires macOS"}]
    swift = shutil.which("swift")
    if swift is None or not script.is_file():
        return [], [{"path": str(script), "error": "Swift runtime or relief tool missing"}]
    results: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    target_free = int(config["target_free_bytes"])
    for item in config["targets"]:
        if shutil.disk_usage(home).free >= target_free:
            break
        assert isinstance(item, dict)
        target = str(item["path"])
        allowed_root = str(item["allowed_root"])
        base = [swift, str(script), "--path", target, "--allowed-root", allowed_root]
        try:
            dry = parse_json_result(
                subprocess.run(base, capture_output=True, text=True, timeout=120, check=False),
                "File Provider dry-run",
            )
            confirmation = dry.get("confirmation_token")
            if not isinstance(confirmation, str) or not confirmation:
                raise RuntimeError("File Provider dry-run omitted confirmation token")
            if guard is not None and guard(Path(target)):
                continue
            applied = parse_json_result(
                subprocess.run(
                    [*base, "--apply", "--confirmation-token", confirmation],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                ),
                "File Provider apply",
            )
            results.append(applied)
        except HostProtectionError as exc:
            errors.append({"path": target, "error": f"{type(exc).__name__}: {exc}"})
            break
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": target, "error": f"{type(exc).__name__}: {exc}"})
    return results, errors


def clear_platform_flags(path: Path) -> None:
    if os.name == "nt":
        return
    command_name = "chflags" if sys.platform == "darwin" else "chattr"
    command_path = shutil.which(command_name)
    if command_path is None:
        return
    command = (
        [command_path, "nouchg", str(path)]
        if sys.platform == "darwin"
        else [command_path, "-i", "--", str(path)]
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode and shutil.which("sudo"):
        subprocess.run(
            [shutil.which("sudo") or "sudo", "-n", *command],
            capture_output=True,
            text=True,
            check=False,
        )


def chmod_without_following_symlinks(path: Path, mode: int) -> None:
    """Make a validated path writable across Python platform capabilities."""
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except NotImplementedError:
        # Windows Python does not implement follow_symlinks=False for chmod.
        # The caller already confines the path to the exact removal root; keep
        # the no-follow invariant explicit before using the supported call.
        if path.is_symlink():
            raise
        os.chmod(path, mode)


def windows_extended_path(value: str) -> str:
    """Return a Win32 extended-length path without changing its target."""
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def filesystem_removal_path(path: Path) -> Path:
    resolved = str(path.resolve(strict=True))
    return Path(windows_extended_path(resolved) if os.name == "nt" else resolved)


def removal_error_handler(root: Path, clear_flags: bool):
    root_resolved = root.resolve(strict=True)

    def handle(function, raw_path: str, exc_info) -> None:
        path = Path(raw_path)
        if path.is_symlink():
            raise exc_info[1]
        resolved = path.resolve(strict=False)
        if resolved != root_resolved and not is_relative_to(resolved, root_resolved):
            raise ValueError(f"refusing removal recovery outside candidate: {path}")
        if clear_flags:
            clear_platform_flags(path)
        for item in (path.parent, path):
            resolved_item = item.resolve(strict=False)
            if resolved_item != root_resolved and not is_relative_to(
                resolved_item, root_resolved
            ):
                continue
            try:
                mode = item.stat(follow_symlinks=False).st_mode
                additions = stat.S_IWUSR
                if item.is_dir():
                    additions |= stat.S_IRUSR | stat.S_IXUSR
                chmod_without_following_symlinks(item, mode | additions)
            except (OSError, NotImplementedError):
                continue
        function(raw_path)

    return handle


def run_retention(args: argparse.Namespace, home: Path, hermes_home: Path) -> int:
    before = shutil.disk_usage(home).free
    plan_json = getattr(args, "plan_json", False)
    policy = None
    headroom = None
    discovery = None
    protection: dict[str, list[str]] = {"active": [], "rollback": [], "referenced": []}
    candidates: list[Candidate] = []
    inventory: list[dict[str, object]] = []
    deleted: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    refusals: list[dict[str, object]] = []
    host_guard: dict[str, object] = {"refusals": refusals}
    host = None

    def guard_refuses(path: Path, kind: str, match: ProtectedPath | None) -> bool:
        if match is None:
            return False
        row = {"path": str(path), "kind": kind, "action": "keep_protected",
               "reason": "host protection overlap", "protected_by": match.record()}
        refusals.append(row)
        for entry in inventory:
            if entry["path"] == str(path) and entry["kind"] == kind:
                entry.update(row)
                break
        else:
            inventory.append(row)
        return True

    try:
        if getattr(args, "preserve_all", False):
            # Adoption has no managed binding yet. Do not invent one or build
            # a deletion plan: retain every path and measure free space only.
            if args.apply:
                raise ValueError("preserve-all cannot apply retention")
            protection["preserved"] = [str(hermes_home)]
        else:
            host = host_protection_inventory(home, hermes_home, host_guard)
            protected, protection = protected_runtime_roots(hermes_home)
            policy = space_policy(hermes_home)
            if policy is not None:
                headroom = headroom_state(home, hermes_home, policy)
                discovery = discover_consumers(home, hermes_home, policy, write_cache=not plan_json)
                required_backups = set()
                for registration in sorted(artifact_registry(hermes_home).glob("*.json")):
                    registered, _ = read_json(registration, "artifact registration")
                    if not isinstance(registered, dict) or registered.get("schema_version") != 1:
                        raise ValueError(f"invalid artifact registration: {registration}")
                    backup = registered.get("backup")
                    if backup is not None:
                        if (not isinstance(backup, dict) or not isinstance(backup.get("path"), str)
                                or not Path(backup["path"]).is_absolute()):
                            raise ValueError(f"invalid required backup path: {registration}")
                        required_backups.add(Path(backup["path"]))
                protected.update(required_backups)
                protection["artifact_backups"] = sorted(str(path) for path in required_backups)
            candidates, inventory = collect_plan(
                home, hermes_home, protected, args.keep_backups,
                args.keep_snapshots, args.prune_caches or bool(headroom and headroom["recovery_pending"]), args.min_cache_bytes,
            )
            if policy is not None:
                size_deadline = time.monotonic() + 5
                for registration in sorted(artifact_registry(hermes_home).glob("*.json")):
                    candidate, row = artifact_candidate(registration, home, hermes_home, protected,
                                                        pressure=bool(headroom["recovery_pending"]), size_deadline=size_deadline)
                    inventory.append(row)
                    if candidate is not None:
                        candidates.append(candidate)
                eligible = []
                for candidate in candidates:
                    if any(is_relative_to(candidate.path, path) or is_relative_to(path, candidate.path) for path in protected):
                        for row in inventory:
                            if row["path"] == str(candidate.path):
                                row.update(action="keep_protected", reason="required runtime or rollback backup")
                    elif has_registered_descendant(candidate.path, home, hermes_home):
                        inventory.append({**record(candidate, "keep"),
                                          "reason": "registered descendant requires its own lifecycle decision"})
                    else:
                        eligible.append(candidate)
                candidates = eligible
            candidates = [candidate for candidate in candidates
                          if not guard_refuses(candidate.path, candidate.kind, host.refusal(candidate.path))]
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        candidates = []
        # Publish failed preflight to the existing health consumer instead of
        # leaving an old successful receipt behind. No partial plan may run.
        errors.append({"path": str(hermes_home), "error": f"{type(exc).__name__}: {exc}"})
    if args.apply:
        for candidate in candidates:
            try:
                # Refresh host protection first: it can be slow (ps/lsof/procfs).
                # The class-specific identity and evidence checks below then stay
                # immediately adjacent to the destructive call.
                host = host_protection_inventory(home, hermes_home, host_guard)
                if guard_refuses(candidate.path, candidate.kind, host.refusal(candidate.path)):
                    continue
                if candidate.kind == "generated_artifact":
                    current, row = artifact_candidate(candidate.evidence[0][0], home, hermes_home, protected,
                                                      pressure=bool(headroom and (headroom["growth_pressure"]
                                                                    or (headroom["recovery_pending"]
                                                                        and shutil.disk_usage(home).free < headroom["target_free_bytes"]))))
                    if current is None:
                        inventory.append({**row, "action": "keep"})
                        continue
                    if current.path != candidate.path or current.evidence != candidate.evidence:
                        raise ValueError("artifact identity changed")
                elif candidate.kind == "cache":
                    command = shutil.which("lsof")
                    if command is None:
                        inventory.append({**record(candidate, "keep"), "reason": "cache handle inventory unavailable"})
                        continue
                    handles = subprocess.run([command, "-Fn", "+D", str(candidate.path)],
                                             capture_output=True, text=True, timeout=30, check=False)
                    if handles.returncode != 1 or handles.stdout.strip() or handles.stderr.strip():
                        inventory.append({**record(candidate, "keep"), "reason": "cache active or handle inventory unverified"})
                        continue
                elif candidate.kind == "rollout_sqlite_work":
                    generation = candidate.path.parents[2]
                    active, _ = active_runtime_root(hermes_home)
                    if (os.path.lexists(generation / "rollback-transaction.pending")
                            or not rollout_evidence_unchanged(candidate.evidence, home)
                            or rollout_reference_present(generation, [live_reference_text(),
                                                                     *reference_texts(hermes_home, active)])):
                        raise ValueError("rollout protection or verified evidence changed before removal")
                    require_closed_rollout_files(list(candidate.path.iterdir()))
                    if not rollout_evidence_unchanged(candidate.evidence, home):
                        raise ValueError("rollout evidence changed during the open-file probe")
                elif candidate.kind == "rollout_sqlite_families":
                    if candidate.path.name != "sqlite-families" or candidate.path.is_symlink():
                        raise ValueError("unsafe sqlite-families path")
                    generation = candidate.path.parent
                    if generation.parent.name != "fleet-rollouts":
                        raise ValueError("sqlite-families is not under fleet-rollouts")
                    current = current_rollback_generation(hermes_home)
                    active, _ = active_runtime_root(hermes_home)
                    if (
                        os.path.lexists(generation / "rollback-transaction.pending")
                        or (
                            current is not None
                            and generation.resolve() == current.resolve()
                        )
                        or rollout_reference_present(
                            generation,
                            [live_reference_text(), *reference_texts(hermes_home, active)],
                        )
                    ):
                        raise ValueError("sqlite-families protection changed before removal")
                directory = candidate.path.is_dir() and not candidate.path.is_symlink()
                onerror = None
                if directory:
                    if has_registered_descendant(candidate.path, home, hermes_home):
                        inventory.append({**record(candidate, "keep"),
                                          "reason": "registered descendant requires its own lifecycle decision"})
                        continue
                    removal_path = filesystem_removal_path(candidate.path)
                    onerror = removal_error_handler(removal_path, args.clear_flags)
                else:
                    removal_path = candidate.path
                # In-memory decision against the inventory refreshed above; no
                # slow discovery runs between the identity checks and removal.
                if guard_refuses(candidate.path, candidate.kind,
                                 host.refusal(candidate.path) or host.refusal(removal_path)):
                    continue
                if onerror is not None:
                    shutil.rmtree(removal_path, onerror=onerror)
                else:
                    candidate.path.unlink()
                deleted.append(record(candidate, "deleted"))
            except HostProtectionError as exc:
                errors.append({"path": str(candidate.path), "error": f"{type(exc).__name__}: {exc}"})
                break
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": str(candidate.path), "error": f"{type(exc).__name__}: {exc}"})
    after_runtime_retention = shutil.disk_usage(home).free
    relief_results: list[dict[str, object]] = []
    relief_errors: list[dict[str, str]] = []
    relief_config = None if errors else fileprovider_relief_config(args.fileprovider_config, home, hermes_home)
    if args.apply and relief_config is not None and host is not None:
        eligible_targets = []
        for item in relief_config["targets"]:
            target = Path(item["path"])
            if any(is_relative_to(target, path) or is_relative_to(path, target) for path in protected):
                inventory.append({"path": str(target), "kind": "fileprovider_target", "action": "keep_protected",
                                  "reason": "required runtime or rollback backup; online-only relief skipped"})
            elif not guard_refuses(target, "fileprovider_target", host.refusal(target)):
                eligible_targets.append(item)
        relief_config = {**relief_config, "targets": eligible_targets}
        if headroom is not None and headroom["recovery_pending"]:
            relief_config["target_free_bytes"] = max(int(relief_config["target_free_bytes"]), headroom["target_free_bytes"])
    def relief_guard(path: Path) -> bool:
        fresh = host_protection_inventory(home, hermes_home, host_guard)
        return guard_refuses(path, "fileprovider_target", fresh.refusal(path))

    if (args.apply and relief_config is not None and relief_config["targets"]
            and after_runtime_retention < int(relief_config["target_free_bytes"])):
        relief_results, relief_errors = run_fileprovider_relief(
            relief_config,
            home,
            Path(__file__).with_name("hermes-fileprovider-online-only.swift"),
            guard=relief_guard,
        )
        errors.extend(relief_errors)
    after = shutil.disk_usage(home).free
    status = "error" if errors else "pass"
    if status == "pass" and any(row.get("budget_exceeded") or row.get("size_observation") == "unavailable"
                                for row in inventory):
        status = "warn"
    if status in {"pass", "warn"} and after < args.block_free_bytes:
        status = "block"
    elif status == "pass" and after < args.warn_free_bytes:
        status = "warn"
    if headroom is not None:
        headroom["free_after_bytes"] = after
        headroom["target_restored"] = after >= headroom["target_free_bytes"]
        headroom["outcome"] = ("target_restored" if headroom["target_restored"] else
                               "target_not_restored" if headroom["recovery_pending"] else "above_cleanup_trigger")
        headroom["recovery_pending"] = headroom["recovery_pending"] and not headroom["target_restored"]
        if headroom["outcome"] == "target_not_restored" and status != "error":
            status = "block"
        elif headroom["growth_pressure"] and status == "pass":
            status = "warn"
        if not plan_json:
            atomic_json(hermes_home / "state/disk-headroom.json", headroom)
    checked_at = now_iso()
    manifest = {
        "schema_version": 2,
        "kind": "hermes_disk_retention",
        "checked_at": checked_at,
        "mode": "apply" if args.apply else "dry-run",
        "preserve_all": bool(getattr(args, "preserve_all", False)),
        "status": status,
        "home": str(home),
        "hermes_home": str(hermes_home),
        "policy": {
            "keep_backups": args.keep_backups,
            "keep_snapshots": args.keep_snapshots,
            "runtime_candidate_policy": "active_plus_one_rollback_plus_referenced_dependencies",
            "memory_policy": "never_eligible",
            "rollout_payload_policy": "verified_sqlite_work_plus_unverified_superseded_sqlite_families",
            "clear_flags": args.clear_flags,
            "fileprovider_relief_config": str(args.fileprovider_config),
            "fileprovider_relief_enabled": relief_config is not None,
        },
        **({"headroom": headroom, "discovery": discovery,
             "retained_decisions": [row for row in inventory if row.get("action") not in {"prune", "deleted"}]}
           if headroom is not None else {}),
        "free_before_bytes": before,
        "free_after_bytes": after,
        "planned_reclaim_bytes": sum(item.size_bytes for item in candidates),
        "deleted_reclaim_bytes": sum(int(item["size_bytes"]) for item in deleted),
        "runtime_protection": protection,
        "host_protection": host_guard,
        "inventory": inventory,
        "planned": [record(item, "delete" if args.apply else "would_delete") for item in candidates],
        "deleted": deleted,
        "fileprovider_relief": relief_results,
        "errors": errors,
    }
    if plan_json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 2 if status == "block" else (1 if status == "error" else 0)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    atomic_json(hermes_home / f"state/disk-retention/retention-{stamp}.json", manifest)
    atomic_json(hermes_home / "state/disk-retention-last.json", manifest)
    summary = {
        "ok": status not in {"error", "block"},
        "status": status,
        "mode": manifest["mode"],
        "planned_count": len(candidates),
        "deleted_count": len(deleted),
        "planned_reclaim_bytes": manifest["planned_reclaim_bytes"],
        "free_after_bytes": after,
    }
    print(json.dumps(summary, sort_keys=True) if args.json else "hermes-disk-retention: " + str(summary))
    return 2 if status == "block" else (1 if status == "error" else 0)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--register-artifact")
    parser.add_argument("--copy-recovery-source", help="Register an exclusive recovery destination before copying source bytes")
    parser.add_argument("--recovery-offset", type=int, default=0)
    parser.add_argument("--recovery-length", type=int)
    parser.add_argument("--recovery-text-sha256", help="Expected digest for a bounded stable UTF-8 text snapshot")
    parser.add_argument("--codex-workspace", action="store_true", help="Register a Codex SessionStart work folder before tool writes")
    parser.add_argument("--create-artifact-directory", action="store_true")
    parser.add_argument("--artifact-owner")
    parser.add_argument("--artifact-kind", choices=("scratch", "cache", "recovery", "working"), default="scratch")
    parser.add_argument("--artifact-ttl", type=int, default=86400)
    parser.add_argument("--artifact-budget", type=parse_size, default=parse_size("1G"))
    parser.add_argument("--artifact-pid", type=int, default=os.getppid())
    parser.add_argument("--artifact-backup")
    parser.add_argument("--artifact-backup-sha256")
    parser.add_argument("--preserve-all", action="store_true",
                        help="Read-only free-space check; preserve all paths without a deletion plan")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--plan-json", action="store_true",
                        help="Print the dry-run receipt without writing state or taking mutation leases")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument(
        "--keep-backups",
        type=int,
        default=int(os.environ.get("HERMES_DISK_RETENTION_KEEP_BACKUPS", "1")),
    )
    parser.add_argument(
        "--keep-snapshots",
        type=int,
        default=int(os.environ.get("HERMES_DISK_RETENTION_KEEP_SNAPSHOTS", "1")),
    )
    parser.add_argument(
        "--min-cache-bytes",
        type=parse_size,
        default=parse_size(os.environ.get("HERMES_DISK_RETENTION_MIN_CACHE", "256M")),
    )
    parser.add_argument(
        "--warn-free-bytes",
        type=parse_size,
        default=os.environ.get("HERMES_DISK_RETENTION_WARN_FREE"),
    )
    parser.add_argument(
        "--block-free-bytes",
        type=parse_size,
        default=parse_size(os.environ.get("HERMES_DISK_RETENTION_BLOCK_FREE", "15G")),
    )
    parser.add_argument("--prune-caches", dest="prune_caches", action="store_true")
    parser.add_argument("--no-prune-caches", dest="prune_caches", action="store_false")
    parser.add_argument(
        "--fileprovider-config",
        default=os.environ.get(
            "HERMES_DISK_RELIEF_CONFIG",
            str(Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config/disk-relief.json"),
        ),
    )
    parser.add_argument("--clear-flags", dest="clear_flags", action="store_true")
    parser.add_argument("--no-clear-flags", dest="clear_flags", action="store_false")
    parser.set_defaults(
        prune_caches=os.environ.get("HERMES_DISK_RETENTION_PRUNE_CACHES") == "1",
        clear_flags=os.environ.get("HERMES_DISK_RETENTION_CLEAR_FLAGS") == "1",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.plan_json and (args.apply or args.copy_recovery_source or args.register_artifact or args.codex_workspace):
        parser.error("--plan-json is only available for a dry run")
    if args.recovery_text_sha256 is not None and not args.copy_recovery_source:
        parser.error("--recovery-text-sha256 requires --copy-recovery-source")
    if args.copy_recovery_source and (not args.register_artifact or args.codex_workspace):
        parser.error("recovery copy requires --register-artifact and cannot run a Codex hook")
    if not args.copy_recovery_source and (args.recovery_offset or args.recovery_length is not None):
        parser.error("recovery range requires --copy-recovery-source")
    if args.codex_workspace and (args.apply or args.preserve_all or args.register_artifact):
        parser.error("Codex workspace registration cannot run cleanup or a second registration")
    if args.register_artifact and (args.apply or args.preserve_all):
        parser.error("artifact registration is a separate operation")
    if args.preserve_all and args.apply:
        parser.error("--preserve-all cannot be combined with --apply")
    if args.keep_backups != 1 or args.keep_snapshots != 1:
        raise SystemExit("fleet policy requires exactly one rollback backup and one snapshot")
    home = Path(args.home).expanduser().resolve()
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    args.fileprovider_config = Path(args.fileprovider_config).expanduser().resolve()
    if not home.is_dir() or not hermes_home.is_dir():
        raise SystemExit("home or Hermes home not found")
    if args.codex_workspace:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or event.get("hook_event_name") != "SessionStart" or not isinstance(event.get("cwd"), str):
            raise ValueError("Codex workspace registration requires a SessionStart event with cwd")
        cwd = Path(event["cwd"])
        root = home / "Documents/Codex"
        if not cwd.is_absolute() or not is_relative_to(cwd, root) or space_policy(hermes_home) is None:
            print("{}")
            return 0
        relative = cwd.resolve().relative_to(root.resolve())
        if len(relative.parts) < 2:
            print("{}")
            return 0
        workspace = root.joinpath(*relative.parts[:2])
        if not workspace.is_dir():
            raise ValueError("Codex task directory does not exist")
        work = workspace / "work"
        if work.exists() and not work.is_dir():
            raise ValueError("Codex work path is not a directory")
        args.register_artifact = str(work)
        args.create_artifact_directory = not work.exists()
        args.artifact_owner = "codex:" + str(workspace.relative_to(root))
        args.artifact_kind = "working"
        args.artifact_ttl = 7 * 86400
        args.artifact_budget = 5 * 1024**3
        args.artifact_pid = os.getpid()
    if args.warn_free_bytes is None:
        args.warn_free_bytes = max(args.block_free_bytes,
                                   min(parse_size("30G"), shutil.disk_usage(home).total // 5))
    if args.preserve_all or args.plan_json:
        # These modes never remove data. They can report while the rollout
        # lease is held; the stdout-only plan is advisory, not deletion authority.
        return run_retention(args, home, hermes_home)
    state = hermes_home / "state/promotion/executor"
    with mutation_lease(state / "active-rollout.lock"):
        with mutation_lease(state / "runtime-mutation.lock"):
            with mutation_lease(hermes_home / "state/host-steward/operation.lock"):
                if args.copy_recovery_source:
                    return copy_recovery_artifact(args, home, hermes_home)
                if args.register_artifact:
                    return register_artifact(args, home, hermes_home)
                return run_retention(args, home, hermes_home)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        if "--codex-workspace" in sys.argv:
            print(json.dumps({"continue": False, "stopReason": "Workspace ownership could not be registered", "systemMessage": str(exc)}))
            raise SystemExit(0)
        if not isinstance(exc, ValueError) or not str(exc).startswith("fleet mutation lease busy:"):
            raise
        print(json.dumps({"status": "deferred", "reason": str(exc), "deleted_count": 0}))
        raise SystemExit(75)
