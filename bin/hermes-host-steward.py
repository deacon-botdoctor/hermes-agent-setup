#!/usr/bin/env python3
"""Own and reconcile task-scoped host resources.

The external interface is deliberately small:

* launch-process / register-process / create-browser-tab create resources with exact local ownership.
* finish releases resources owned by one completed task.
* reconcile repairs expired leases after repeated observations.
* snapshot reports bounded ownership and storage health without private content.

Unknown and interactive resources are inventory only.  They are never mutated.
Storage observes the existing retention receipt; it does not add deletion authority.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import http.client
import json
import math
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "hermes-host-steward/v1"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 30 * 86400
DEFAULT_TTL_SECONDS = 4 * 3600
STALE_OBSERVATIONS_REQUIRED = 2
MIN_STALE_OBSERVATION_INTERVAL_SECONDS = 300
MAX_RECONCILE_LEASES = 3
MAX_RECONCILE_INTENTS = 3
INTENT_GRACE_SECONDS = 60
MAX_LOOPBACK_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_STORAGE_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_STORAGE_POLICY_BYTES = 16 * 1024
LOOPBACK_RESPONSE_DEADLINE_SECONDS = 5.0
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
TARGET_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
LEASE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHELL_NAMES = {
    "bash",
    "cmd",
    "csh",
    "dash",
    "fish",
    "ksh",
    "nu",
    "nushell",
    "powershell",
    "pwsh",
    "sh",
    "tcsh",
    "zsh",
}
DETACHING_COMMANDS = {
    "daemon",
    "launchctl",
    "nohup",
    "open",
    "pm2",
    "screen",
    "service",
    "setsid",
    "systemctl",
    "tmux",
    "xdg-open",
}
DETACHING_ARGUMENTS = {
    "--background",
    "--daemon",
    "--daemonize",
    "--detach",
    "--fork",
}
_LAUNCH_PROVENANCE = object()


class LoopbackConnectError(OSError):
    """The loopback peer was not reached, so no request was dispatched."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_now() -> int:
    return int(time.time())


def _home(value: str | None = None) -> Path:
    return Path(value or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _state_dir(home: Path) -> Path:
    return home / "state" / "host-steward"


def _lease_dir(home: Path) -> Path:
    return _state_dir(home) / "leases"


def _intent_dir(home: Path) -> Path:
    return _state_dir(home) / "intents"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _registry_lock(home: Path, filename: str = "registry.lock"):
    """Serialize lease decisions across task completion, renewal, and reaping."""
    lock_path = _state_dir(home) / filename
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    descriptor = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextlib.contextmanager
def _defer_termination():
    """Finish create/register atomically before honoring common cancellation signals."""
    pending: list[int] = []
    previous: dict[int, Any] = {}

    def capture(signum, _frame):
        pending.append(int(signum))

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, capture)
    except (ValueError, OSError):
        previous.clear()
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if pending:
        raise KeyboardInterrupt(f"termination deferred until resource ownership was durable: {pending[0]}")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _task_id(explicit: str | None) -> str:
    value = explicit or next(
        (
            os.environ.get(name, "")
            for name in (
                "HERMES_TASK_ID",
                "HERMES_SESSION_ID",
                "CODEX_THREAD_ID",
                "CLAUDE_SESSION_ID",
            )
            if os.environ.get(name)
        ),
        "",
    )
    if not TASK_ID_RE.fullmatch(value):
        raise ValueError("task id is required and must contain only stable identifier characters")
    return value


def _ttl(value: int) -> int:
    if not MIN_TTL_SECONDS <= value <= MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds"
        )
    return value


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()


def _posix_birth_id(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2 :].split()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
            if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", boot_id):
                return None
            return f"linux:{boot_id.lower()}:{int(fields[19])}"
        except (OSError, UnicodeDecodeError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        class ProcBSDInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]
        try:
            library = ctypes.CDLL("libproc.dylib", use_errno=True)
            info = ProcBSDInfo()
            size = library.proc_pidinfo(
                int(pid), 3, 0, ctypes.byref(info), ctypes.sizeof(info)
            )
            if size != ctypes.sizeof(info) or not info.pbi_start_tvsec:
                return None
            return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
        except (OSError, ValueError):
            return None
    return None


def _is_gateway_command(command: str) -> bool:
    lowered = command.lower()
    return any(
        re.search(pattern, lowered)
        for pattern in (
            r"\bhermes(?:\.exe)?\b[^\n]*\bgateway\b",
            r"\bopenclaw(?:\.exe)?\b[^\n]*\bgateway\b",
            r"\bgateway\s+run\s+--replace\b",
            r"(?:^|\s)-m\s+gateway\.run\b",
            r"(?:^|\s)-m\s+hermes_cli\.main\b[^\n]*\bgateway\b",
            r"\bhermes_cli[/\\]main\.py\b[^\n]*\bgateway\b",
            r"\bcli\.py\s+--gateway\b",
        )
    )


def _posix_process_identity(pid: int) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [
                "ps",
                "-ww",
                "-p",
                str(pid),
                "-o",
                "uid=,ppid=,pgid=,tty=,lstart=,command=",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (result.stdout or "").strip()
    if result.returncode != 0 or not line:
        return None
    parts = line.split(None, 9)
    if len(parts) < 10:
        return None
    try:
        uid, ppid, pgid = (int(parts[index]) for index in range(3))
        sid = os.getsid(pid)
        birth_id = _posix_birth_id(pid)
    except (OSError, ValueError):
        return None
    if birth_id is None:
        return None
    identity = {
        "pid": pid,
        "uid": uid,
        "ppid": ppid,
        "pgid": pgid,
        "sid": sid,
        "birth_id": birth_id,
        "tty": parts[3],
        "started": " ".join(parts[4:9]),
        "command_sha256": _command_hash(parts[9]),
        "executable": Path(parts[9].split(None, 1)[0]).name,
    }
    if _is_gateway_command(parts[9]):
        identity["protected_class"] = "gateway"
    return identity


def _windows_open_process_anchor(pid: int, access: int = 0x1000) -> tuple[Any | None, str]:
    """Open a Windows process handle and return its kernel creation identity."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            filetime_pointer,
            filetime_pointer,
            filetime_pointer,
            filetime_pointer,
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            return None, ""
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            kernel32.CloseHandle(handle)
            return None, ""
        birth_id = f"filetime:{(created.dwHighDateTime << 32) | created.dwLowDateTime}"
        return handle, birth_id
    except (AttributeError, OSError, ValueError):
        return None, ""


def _windows_close_process_anchor(handle: Any) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
    except (AttributeError, OSError, ValueError):
        pass


def _windows_assign_process_job(
    pid: int, lease_id: str, expected_birth_id: str
) -> tuple[int, str, int] | None:
    """Put a birth-verified process in a uniquely named Windows job."""
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = (
            wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        )
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        process = kernel32.OpenProcess(0x1101, False, pid)  # query, set quota, terminate
        if not process:
            kernel32.CloseHandle(job)
            return None
        try:
            _anchor, birth_id = _windows_open_process_anchor(pid)
            if _anchor is not None:
                _windows_close_process_anchor(_anchor)
            if birth_id != expected_birth_id:
                return None
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                return None
            if not kernel32.AssignProcessToJobObject(job, process):
                return None
            owner_pid = os.getppid()
            owner, owner_birth_id = _windows_open_process_anchor(owner_pid, 0x1040)
            if owner is None or not owner_birth_id:
                return None
            try:
                guard = wintypes.HANDLE()
                if not kernel32.DuplicateHandle(
                    kernel32.GetCurrentProcess(),
                    job,
                    owner,
                    ctypes.byref(guard),
                    0,
                    False,
                    0x00000002,  # DUPLICATE_SAME_ACCESS
                ):
                    return None
                return owner_pid, owner_birth_id, int(guard.value)
            finally:
                _windows_close_process_anchor(owner)
        finally:
            kernel32.CloseHandle(process)
            kernel32.CloseHandle(job)
    except (AttributeError, OSError, ValueError):
        return None


def _windows_duplicate_remote_job_guard(
    owner_pid: int,
    owner_birth_id: str,
    guard_handle: int,
    *,
    close_source: bool,
) -> tuple[Any | None, str]:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = (
            wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        )
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        owner, current_birth_id = _windows_open_process_anchor(owner_pid, 0x1040)
        if owner is None or current_birth_id != owner_birth_id:
            if owner is not None:
                _windows_close_process_anchor(owner)
            return None, "gone"
        local = wintypes.HANDLE()
        try:
            copied = kernel32.DuplicateHandle(
                owner,
                wintypes.HANDLE(guard_handle),
                kernel32.GetCurrentProcess(),
                ctypes.byref(local),
                0,
                False,
                0x00000002 | (0x00000001 if close_source else 0),
            )
            if not copied:
                return None, "failed"
            return local, "opened"
        finally:
            _windows_close_process_anchor(owner)
    except (AttributeError, OSError, ValueError):
        return None, "failed"


def _windows_terminate_job(job: Any) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        terminate = ctypes.windll.kernel32.TerminateJobObject
        terminate.argtypes = (wintypes.HANDLE, wintypes.UINT)
        terminate.restype = wintypes.BOOL
        return bool(terminate(job, 1))
    except (AttributeError, OSError, ValueError):
        return False


def _windows_process_identity(pid: int) -> dict[str, Any] | None:
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}';"
        "if($p){[pscustomobject]@{pid=[int]$p.ProcessId;ppid=[int]$p.ParentProcessId;"
        "started=[string]$p.CreationDate;command=[string]$p.CommandLine;"
        "executable=[string]$p.ExecutablePath}|ConvertTo-Json -Compress}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        value = json.loads(result.stdout)
        command = str(value.pop("command", ""))
        started = str(value.get("started") or "")
        handle, birth_id = _windows_open_process_anchor(pid)
        if handle is None or not birth_id:
            return None
        _windows_close_process_anchor(handle)
        executable = Path(str(value.get("executable") or command.split(None, 1)[0])).name
        value["command_sha256"] = _command_hash(command)
        value["birth_id"] = birth_id
        value["executable"] = executable
        value["tty"] = ""
        if _is_gateway_command(command):
            value["protected_class"] = "gateway"
        return value
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def process_identity(pid: int) -> dict[str, Any] | None:
    if pid <= 1 or pid == os.getpid():
        return None
    if os.name == "nt":
        return _windows_process_identity(pid)
    return _posix_process_identity(pid)


def _wait_process_identity(pid: int, timeout_seconds: float = 2.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        identity = process_identity(pid)
        if identity is not None:
            return identity
        if not _pid_exists(pid):
            return None
        time.sleep(0.02)
    return process_identity(pid)


def _fingerprint_matches(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    keys = ("pid", "birth_id")
    if os.name != "nt":
        keys += ("uid", "pgid", "sid")
    return all(
        expected.get(key) not in {None, ""}
        and expected.get(key) == current.get(key)
        for key in keys
    )


def _posix_group_members(pgid: int, sid: int) -> list[dict[str, Any]] | None:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,uid=,pgid=,tty=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    members: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        try:
            pid, uid, row_pgid = (int(parts[index]) for index in range(3))
            row_sid = os.getsid(pid)
        except (OSError, ValueError):
            continue
        if row_sid != sid:
            continue
        members.append(
            {
                "pid": pid,
                "uid": uid,
                "pgid": row_pgid,
                "tty": parts[3],
                "protected": _is_gateway_command(parts[4]),
            }
        )
    return members


def _normalize_endpoint(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("browser endpoint must be loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("browser endpoint must not contain credentials, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("browser endpoint has an invalid port") from exc
    if not port or not 1 <= port <= 65535:
        raise ValueError("browser endpoint requires an explicit port")
    return f"http://127.0.0.1:{port}"


def _loopback_request(endpoint: str, path: str, method: str = "GET") -> bytes:
    normalized = _normalize_endpoint(endpoint)
    port = urllib.parse.urlparse(normalized).port
    assert port is not None
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        try:
            connection.connect()
        except OSError as exc:
            raise LoopbackConnectError(type(exc).__name__) from exc
        connection.request(method, path)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise OSError(f"loopback endpoint returned HTTP {response.status}")
        deadline = time.monotonic() + LOOPBACK_RESPONSE_DEADLINE_SECONDS
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("loopback response deadline exceeded")
            if connection.sock is not None:
                connection.sock.settimeout(max(0.05, remaining))
            chunk = response.read1(
                min(65536, MAX_LOOPBACK_RESPONSE_BYTES + 1 - total)
            )
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_LOOPBACK_RESPONSE_BYTES:
                raise OSError("loopback response exceeded size limit")
    finally:
        connection.close()


def _browser_targets(endpoint: str) -> list[dict[str, Any]] | None:
    try:
        value = json.loads(
            _loopback_request(endpoint, "/json").decode("utf-8", "replace")
        )
    except Exception:
        return None
    return value if isinstance(value, list) else None


def _browser_target_present(endpoint: str, target_id: str) -> bool | None:
    targets = _browser_targets(endpoint)
    if targets is None:
        return None
    return any(
        isinstance(item, dict)
        and str(item.get("id") or "") == target_id
        and item.get("type") == "page"
        for item in targets
    )


def _lease_path(home: Path, lease_id: str) -> Path:
    return _lease_dir(home) / f"{lease_id}.json"


def _base_lease(task_id: str, kind: str, ttl: int, protected: bool) -> dict[str, Any]:
    now = epoch_now()
    return {
        "schema": SCHEMA,
        "lease_id": uuid.uuid4().hex,
        "task_id": task_id,
        "kind": kind,
        "created_at": utc_now(),
        "created_epoch": now,
        "expires_epoch": now + ttl,
        "ttl_seconds": ttl,
        "protected": protected,
        "stale_observations": 0,
    }


def _intent_path(home: Path, lease_id: str) -> Path:
    return _intent_dir(home) / f"{lease_id}.json"


def _write_intent(home: Path, lease: dict[str, Any], resource: dict[str, Any]) -> Path:
    path = _intent_path(home, str(lease["lease_id"]))
    _atomic_json(
        path,
        {
            "schema": SCHEMA,
            "lease_id": lease["lease_id"],
            "task_id": lease["task_id"],
            "kind": lease["kind"],
            "created_at": lease["created_at"],
            "created_epoch": lease["created_epoch"],
            "protected": lease["protected"],
            "resource": resource,
        },
    )
    return path


def _validate_foreground_command(command: list[str]) -> None:
    """Reject launch forms whose documented behavior escapes process ownership.

    Steward process jobs are a cooperative foreground-only interface, not a
    sandbox for hostile programs.  Direct shells and known detach/fork modes
    are refused so agents cannot accidentally convert an owned job into an
    unowned daemon. Services belong in the host service manager instead.
    """
    index = 0
    executable = Path(command[index]).name.lower()
    while executable == "env":
        index += 1
        while index < len(command) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", command[index], re.S
        ):
            index += 1
        if index < len(command) and command[index] == "--":
            index += 1
        if index >= len(command) or command[index].startswith("-"):
            raise ValueError("launch-process cannot resolve env-wrapped command")
        executable = Path(command[index]).name.lower()
    effective_executables = {executable}
    if re.fullmatch(r"(?:python|pythonw|pypy)(?:\d+(?:\.\d+)*)?", executable):
        if "-c" in command[index + 1 :]:
            raise ValueError("launch-process rejects inline interpreter commands")
        if "-m" in command[index + 1 :]:
            module_index = command.index("-m", index + 1) + 1
            if module_index >= len(command):
                raise ValueError("launch-process cannot resolve interpreter module")
            effective_executables.add(command[module_index].split(".", 1)[0].lower())
    if executable in {"node", "perl", "ruby"} and any(
        argument in {"-e", "--eval"} for argument in command[index + 1 :]
    ):
        raise ValueError("launch-process rejects inline interpreter commands")
    if effective_executables & (DETACHING_COMMANDS | SHELL_NAMES):
        raise ValueError("launch-process accepts direct foreground commands only")
    if _is_gateway_launcher(command, index):
        raise ValueError("process class is never launchable: gateway")
    for argument in command[1:]:
        lowered = argument.lower()
        option = lowered.split("=", 1)[0]
        if option in DETACHING_ARGUMENTS:
            raise ValueError("launch-process rejects daemonizing/detaching arguments")
        if effective_executables & {"docker", "gunicorn", "nerdctl", "podman"} and re.fullmatch(
            r"-[^-]*d[^-]*", lowered
        ):
            raise ValueError("launch-process rejects daemonizing/detaching arguments")


def _is_gateway_launcher(command: list[str], executable_index: int) -> bool:
    if _is_gateway_command(" ".join(command)):
        return True
    raw = command[executable_index]
    basename = Path(raw).name.lower()
    if re.search(r"(?:^|[-_.])(?:start[-_.]?)?(?:hermes|openclaw)[-_.]?gateway", basename):
        return True
    if basename in {"start-hermes", "start-hermes.sh", "start-openclaw", "start-openclaw.sh"}:
        return True
    resolved = Path(raw).expanduser()
    if not resolved.is_absolute() and "/" not in raw and "\\" not in raw:
        located = shutil.which(raw)
        if not located:
            return False
        resolved = Path(located)
    try:
        if not resolved.is_file() or resolved.stat().st_size > 128 * 1024:
            return False
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not content.startswith("#!"):
        return False
    return _is_gateway_command(content)


def _register_launched_process(
    home: Path,
    *,
    task_id: str,
    pid: int,
    ttl: int,
    protected: bool,
    provenance: object,
    captured_identity: dict[str, Any] | None = None,
    registry_locked: bool = False,
) -> dict[str, Any]:
    if provenance is not _LAUNCH_PROVENANCE:
        raise ValueError("process registration requires steward launch provenance")
    current = process_identity(pid)
    if current is None:
        raise ValueError("process is missing or cannot be identified")
    identity = dict(captured_identity or current)
    if not _fingerprint_matches(identity, current):
        raise ValueError("process identity changed before registration")
    if os.name != "nt" and identity.get("uid") != os.getuid():
        raise ValueError("process is not owned by the current user")
    if identity.get("protected_class"):
        raise ValueError(f"process class is never claimable: {identity['protected_class']}")
    ttl = _ttl(ttl)
    with contextlib.nullcontext() if registry_locked else _registry_lock(home):
        for path, existing in load_leases(home)[0]:
            if existing.get("kind") != "process":
                continue
            resource = existing.get("resource") or {}
            if not _fingerprint_matches(resource, identity):
                continue
            if existing.get("task_id") != task_id:
                raise ValueError("process already has a different active owner")
            existing["expires_epoch"] = epoch_now() + ttl
            existing["ttl_seconds"] = ttl
            existing["stale_observations"] = 0
            existing["protected"] = bool(existing.get("protected") or protected)
            _atomic_json(path, existing)
            return _public_lease(existing)
        lease = _base_lease(task_id, "process", ttl, protected)
        lease["resource"] = identity
        if os.name == "nt":
            job_guard = _windows_assign_process_job(
                pid, str(lease["lease_id"]), str(identity.get("birth_id") or "")
            )
            if job_guard is None:
                raise ValueError("Windows process could not be assigned to an owned job")
            guard_owner_pid, guard_owner_birth_id, guard_handle = job_guard
            lease["resource"]["guard_owner_pid"] = guard_owner_pid
            lease["resource"]["guard_owner_birth_id"] = guard_owner_birth_id
            lease["resource"]["guard_handle"] = guard_handle
        _atomic_json(_lease_path(home, lease["lease_id"]), lease)
        return _public_lease(lease)


def _systemd_unit_owns_pid(systemd_unit: str, pid: int) -> bool:
    if not re.fullmatch(r"hermes-worker-proc_[a-z0-9]+(?:-pipe-fallback)?\.scope", systemd_unit):
        return False
    try:
        shown = subprocess.run(
            ["systemctl", "--user", "show", systemd_unit, "--property", "ControlGroup", "--value"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        control_group = shown.stdout.strip()
        cgroups = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
        return (
            shown.returncode == 0
            and control_group.startswith("/")
            and any(
                line.rsplit(":", 1)[-1] == control_group
                for line in cgroups.splitlines()
            )
        )
    except (OSError, subprocess.SubprocessError):
        return False


def register_process(
    home: Path,
    *,
    task_id: str,
    pid: int,
    ttl: int,
    protected: bool,
    systemd_unit: str = "",
) -> dict[str, Any]:
    """Claim one newly spawned, isolated background process.

    This is the narrow adapter for launchers that must retain their own stdio
    pipes (for example Hermes' background terminal registry). The caller must
    fail closed and terminate the process if registration is rejected. Only a
    same-user, non-interactive POSIX session leader or a birth-bound Windows
    process is claimable; arbitrary gateways and interactive TTY jobs are refused.
    """
    current = process_identity(pid)
    if current is None:
        raise ValueError("process is missing or cannot be identified")
    if os.name != "nt" and current.get("uid") != os.getuid():
        raise ValueError("process is not owned by the current user")
    if current.get("protected_class"):
        raise ValueError(f"process class is never claimable: {current['protected_class']}")
    unit_owns_pid = bool(systemd_unit) and _systemd_unit_owns_pid(systemd_unit, pid)
    if int(current.get("ppid") or 0) != os.getppid() and not unit_owns_pid:
        raise ValueError("process was not spawned by the registering parent")
    if str(current.get("tty") or "") not in {"", "?", "??", "-"}:
        raise ValueError("interactive TTY processes are never claimable")
    if os.name == "nt" and not _plain_int(current.get("ppid"), minimum=2):
        raise ValueError("Windows process must have a live non-system parent")
    if os.name == "nt" and not str(current.get("birth_id") or ""):
        raise ValueError("Windows process requires a birth identifier")
    if os.name == "nt" and protected:
        raise ValueError("protected Windows process ownership is unsupported")
    if os.name != "nt" and any(
        int(current.get(key) or 0) != pid for key in ("pgid", "sid")
    ):
        raise ValueError("process must be an isolated session leader")
    return _register_launched_process(
        home,
        task_id=task_id,
        pid=pid,
        ttl=ttl,
        protected=protected,
        provenance=_LAUNCH_PROVENANCE,
        captured_identity=current,
    )


def launch_process(
    home: Path,
    *,
    task_id: str,
    command: list[str],
    ttl: int,
    protected: bool,
) -> dict[str, Any]:
    if os.name == "nt":
        raise ValueError("process lifecycle is unsupported on Windows; use browser-tab leases")
    command = list(command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("launch-process requires a command after --")
    _validate_foreground_command(command)
    ttl = _ttl(ttl)
    process: subprocess.Popen[Any] | None = None
    read_fd = -1
    write_fd = -1
    status_read_fd = -1
    status_write_fd = -1
    registered_lease_id = ""
    lease = _base_lease(task_id, "process", ttl, protected)
    with _registry_lock(home):
        intent_path = _write_intent(
            home,
            lease,
            {"command_sha256": hashlib.sha256("\0".join(command).encode()).hexdigest()},
        )
        with _defer_termination():
            try:
                read_fd, write_fd = os.pipe()
                status_read_fd, status_write_fd = os.pipe()
                os.set_inheritable(read_fd, True)
                os.set_inheritable(status_write_fd, True)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "__exec-gate",
                        str(read_fd),
                        str(status_write_fd),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(read_fd, status_write_fd),
                )
                os.close(read_fd)
                read_fd = -1
                os.close(status_write_fd)
                status_write_fd = -1
                launched_identity = _wait_process_identity(process.pid)
                if launched_identity is None:
                    raise ValueError("process is missing or cannot be identified")
                _write_intent(home, lease, launched_identity)
                lease = _register_launched_process(
                    home,
                    task_id=task_id,
                    pid=process.pid,
                    ttl=ttl,
                    protected=protected,
                    provenance=_LAUNCH_PROVENANCE,
                    captured_identity=launched_identity,
                    registry_locked=True,
                )
                registered_lease_id = str(lease["lease_id"])
                intent_path.unlink(missing_ok=True)
                _write_pipe(write_fd, json.dumps(command).encode("utf-8"))
                os.close(write_fd)
                write_fd = -1
                if not _await_start_status(status_read_fd):
                    raise ValueError("foreground command failed to start")
                os.close(status_read_fd)
                status_read_fd = -1
            except BaseException as exc:
                if read_fd >= 0:
                    os.close(read_fd)
                    read_fd = -1
                if write_fd >= 0:
                    os.close(write_fd)
                    write_fd = -1
                if status_read_fd >= 0:
                    os.close(status_read_fd)
                    status_read_fd = -1
                if status_write_fd >= 0:
                    os.close(status_write_fd)
                    status_write_fd = -1
                cleanup_confirmed = process is None or _terminate_failed_launch(process)
                if cleanup_confirmed:
                    intent_path.unlink(missing_ok=True)
                    if registered_lease_id:
                        _lease_path(home, registered_lease_id).unlink(missing_ok=True)
                else:
                    raise RuntimeError(
                        "process registration failed and launched session cleanup was not confirmed"
                    ) from exc
                if isinstance(exc, OSError):
                    raise ValueError(f"process launch failed: {type(exc).__name__}") from exc
                raise
    assert process is not None
    return {**lease, "pid": process.pid}


def _exec_gate(argv: list[str]) -> int:
    if len(argv) != 2:
        return 125
    try:
        descriptor = int(argv[0])
        status_descriptor = int(argv[1])
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 1024 * 1024:
                return 125
            chunks.append(chunk)
        os.close(descriptor)
        command = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return 125
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        return 125
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        with contextlib.suppress(OSError):
            os.write(status_descriptor, b"0")
            os.close(status_descriptor)
        return 126
    with contextlib.suppress(OSError):
        os.write(status_descriptor, b"1")
        os.close(status_descriptor)
    child.wait()
    while True:
        time.sleep(3600)


def _write_pipe(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short pipe write")
        view = view[written:]


def _await_start_status(descriptor: int, timeout_seconds: float = 5.0) -> bool:
    try:
        readable, _, _ = select.select([descriptor], [], [], timeout_seconds)
        return bool(readable) and os.read(descriptor, 1) == b"1"
    except (OSError, ValueError):
        return False


def _terminate_failed_launch(process: subprocess.Popen[Any]) -> bool:
    pid = int(process.pid)
    for action in (signal.SIGTERM, signal.SIGKILL):
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.1)
        members = _posix_group_members(pid, pid)
        if members is None or not _safe_group_members(members):
            return False
        if not members:
            return True
        for group_id in _session_group_ids(members):
            with contextlib.suppress(OSError):
                os.killpg(group_id, action)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.1)
            members = _posix_group_members(pid, pid)
            if members == []:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.1)
                return True
            if members is None:
                break
            time.sleep(0.05)
    return False


def create_browser_tab(
    home: Path,
    *,
    task_id: str,
    endpoint: str,
    ttl: int,
    protected: bool,
) -> dict[str, Any]:
    endpoint = _normalize_endpoint(endpoint)
    ttl = _ttl(ttl)
    target_id = ""
    lease = _base_lease(task_id, "browser_tab", ttl, protected)
    with _registry_lock(home):
        intent_path = _write_intent(home, lease, {"endpoint": endpoint})
        request_attempted = False
        with _defer_termination():
            try:
                request_attempted = True
                created = json.loads(
                    _loopback_request(
                        endpoint, "/json/new?about%3Ablank", method="PUT"
                    ).decode("utf-8", "replace")
                )
                if not isinstance(created, dict) or created.get("type") != "page":
                    raise ValueError("browser target creation returned an invalid receipt")
                target_id = str(created.get("id") or "")
                if not TARGET_ID_RE.fullmatch(target_id):
                    raise ValueError("browser target creation returned an invalid target id")
                resource = {"endpoint": endpoint, "target_id": target_id}
                _write_intent(home, lease, resource)
                lease["resource"] = resource
                _atomic_json(_lease_path(home, lease["lease_id"]), lease)
                intent_path.unlink(missing_ok=True)
                return {**_public_lease(lease), "target_id": target_id}
            except BaseException as exc:
                if target_id:
                    quoted = urllib.parse.quote(target_id, safe="")
                    try:
                        _loopback_request(endpoint, f"/json/close/{quoted}")
                    except Exception as cleanup_exc:
                        raise RuntimeError(
                            "browser registration failed and target cleanup was not confirmed"
                        ) from cleanup_exc
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if _browser_target_present(endpoint, target_id) is False:
                            break
                        time.sleep(0.05)
                    else:
                        raise RuntimeError(
                            "browser registration failed and target cleanup was not confirmed"
                        ) from exc
                if isinstance(exc, (KeyboardInterrupt, SystemExit, ValueError)):
                    if (
                        target_id
                        or not request_attempted
                        or isinstance(exc, LoopbackConnectError)
                    ):
                        intent_path.unlink(missing_ok=True)
                    raise
                if (
                    target_id
                    or not request_attempted
                    or isinstance(exc, LoopbackConnectError)
                ):
                    intent_path.unlink(missing_ok=True)
                raise ValueError("browser target creation failed") from exc


def _public_lease(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "lease_id": lease.get("lease_id"),
        "task_id": lease.get("task_id"),
        "kind": lease.get("kind"),
        "protected": bool(lease.get("protected")),
        "expires_epoch": lease.get("expires_epoch"),
    }


def load_leases(home: Path) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    valid: list[tuple[Path, dict[str, Any]]] = []
    invalid = 0
    directory = _lease_dir(home)
    if not directory.is_dir():
        return valid, invalid
    for path in sorted(directory.glob("*.json")):
        lease = _read_json(path)
        if path.is_symlink() or lease is None or not _valid_lease(path, lease):
            invalid += 1
            continue
        valid.append((path, lease))
    return valid, invalid


def load_intents(home: Path) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    valid: list[tuple[Path, dict[str, Any]]] = []
    invalid = 0
    directory = _intent_dir(home)
    if not directory.is_dir():
        return valid, invalid
    for path in sorted(directory.glob("*.json")):
        intent = _read_json(path)
        if path.is_symlink() or intent is None or not _valid_intent(path, intent):
            invalid += 1
            continue
        valid.append((path, intent))
    return valid, invalid


def _plain_int(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _valid_intent(path: Path, intent: dict[str, Any]) -> bool:
    lease_id = intent.get("lease_id")
    resource = intent.get("resource")
    if (
        intent.get("schema") != SCHEMA
        or not isinstance(lease_id, str)
        or not LEASE_ID_RE.fullmatch(lease_id)
        or path.stem != lease_id
        or not isinstance(intent.get("task_id"), str)
        or not TASK_ID_RE.fullmatch(str(intent.get("task_id") or ""))
        or intent.get("kind") not in {"process", "browser_tab"}
        or not isinstance(intent.get("created_at"), str)
        or not _plain_int(intent.get("created_epoch"), minimum=1)
        or not isinstance(intent.get("protected"), bool)
        or not isinstance(resource, dict)
    ):
        return False
    if "stale_observations" in intent and not _plain_int(intent.get("stale_observations")):
        return False
    if "last_observation_epoch" in intent and not _plain_int(
        intent.get("last_observation_epoch"), minimum=1
    ):
        return False
    if intent["kind"] == "browser_tab":
        try:
            if _normalize_endpoint(str(resource.get("endpoint") or "")) != resource.get(
                "endpoint"
            ):
                return False
        except ValueError:
            return False
        target_id = resource.get("target_id")
        return target_id is None or (
            isinstance(target_id, str) and TARGET_ID_RE.fullmatch(target_id) is not None
        )
    if set(resource) == {"command_sha256"}:
        return re.fullmatch(r"[0-9a-f]{64}", str(resource.get("command_sha256") or "")) is not None
    required_strings = ("started", "birth_id", "command_sha256", "executable", "tty")
    return (
        _plain_int(resource.get("pid"), minimum=2)
        and all(isinstance(resource.get(key), str) for key in required_strings)
        and re.fullmatch(r"[0-9a-f]{64}", str(resource.get("command_sha256") or "")) is not None
        and all(_plain_int(resource.get(key), minimum=0) for key in ("uid", "ppid", "pgid", "sid"))
    )


def _valid_lease(path: Path, lease: dict[str, Any]) -> bool:
    lease_id = lease.get("lease_id")
    task_id = lease.get("task_id")
    kind = lease.get("kind")
    if (
        lease.get("schema") != SCHEMA
        or not isinstance(lease_id, str)
        or not LEASE_ID_RE.fullmatch(lease_id)
        or path.stem != lease_id
        or not isinstance(task_id, str)
        or not TASK_ID_RE.fullmatch(task_id)
        or kind not in {"process", "browser_tab"}
        or not isinstance(lease.get("created_at"), str)
        or not _plain_int(lease.get("created_epoch"), minimum=1)
        or not _plain_int(lease.get("expires_epoch"), minimum=1)
        or not _plain_int(lease.get("ttl_seconds"), minimum=MIN_TTL_SECONDS)
        or int(lease["ttl_seconds"]) > MAX_TTL_SECONDS
        or not isinstance(lease.get("protected"), bool)
        or not _plain_int(lease.get("stale_observations"))
        or not isinstance(lease.get("resource"), dict)
    ):
        return False
    if "last_reconciled_epoch" in lease and not _plain_int(
        lease.get("last_reconciled_epoch"), minimum=1
    ):
        return False
    if "last_observation_epoch" in lease and not _plain_int(
        lease.get("last_observation_epoch"), minimum=1
    ):
        return False
    resource = lease["resource"]
    if kind == "browser_tab":
        endpoint = resource.get("endpoint")
        target_id = resource.get("target_id")
        try:
            return (
                isinstance(endpoint, str)
                and _normalize_endpoint(endpoint) == endpoint
                and isinstance(target_id, str)
                and TARGET_ID_RE.fullmatch(target_id) is not None
            )
        except ValueError:
            return False
    required_strings = ("started", "birth_id", "command_sha256", "executable", "tty")
    if (
        not _plain_int(resource.get("pid"), minimum=2)
        or any(not isinstance(resource.get(key), str) for key in required_strings)
        or not re.fullmatch(r"[0-9a-f]{64}", str(resource.get("command_sha256") or ""))
    ):
        return False
    if os.name != "nt":
        return all(
            _plain_int(resource.get(key), minimum=0)
            for key in ("uid", "ppid", "pgid", "sid")
        )
    if not _plain_int(resource.get("ppid"), minimum=0):
        return False
    guard_fields = ("guard_owner_pid", "guard_owner_birth_id", "guard_handle")
    if not any(key in resource for key in guard_fields):
        return True  # pre-upgrade Windows process lease; audit-only drain
    return (
        _plain_int(resource.get("guard_owner_pid"), minimum=2)
        and isinstance(resource.get("guard_owner_birth_id"), str)
        and bool(resource.get("guard_owner_birth_id"))
        and _plain_int(resource.get("guard_handle"), minimum=1)
    )


def _release_process(lease: dict[str, Any], apply: bool) -> dict[str, Any]:
    expected = lease.get("resource") or {}
    if lease.get("protected"):
        return {"outcome": "preserved"}
    if os.name == "nt" and not str(expected.get("birth_id") or ""):
        return {"outcome": "blocked", "reason": "missing_process_birth_id"}
    if os.name == "nt":
        if not any(
            key in expected
            for key in ("guard_owner_pid", "guard_owner_birth_id", "guard_handle")
        ):
            return {"outcome": "preserved"}
        job, guard_status = _windows_duplicate_remote_job_guard(
            int(expected.get("guard_owner_pid") or 0),
            str(expected.get("guard_owner_birth_id") or ""),
            int(expected.get("guard_handle") or 0),
            close_source=False,
        )
        if job is None:
            return (
                {"outcome": "already_gone"}
                if guard_status == "gone"
                else {"outcome": "blocked", "reason": "job_guard_unavailable"}
            )
        if not apply:
            _windows_close_process_anchor(job)
            return {"outcome": "would_release"}
        try:
            if not _windows_terminate_job(job):
                return {"outcome": "failed", "reason": "terminate_job_failed"}
            closed_guard, _close_status = _windows_duplicate_remote_job_guard(
                int(expected.get("guard_owner_pid") or 0),
                str(expected.get("guard_owner_birth_id") or ""),
                int(expected.get("guard_handle") or 0),
                close_source=True,
            )
            if closed_guard is not None:
                _windows_close_process_anchor(closed_guard)
        finally:
            _windows_close_process_anchor(job)
        return {"outcome": "released"}
    current = process_identity(int(expected.get("pid") or 0))
    if current is None:
        pid = int(expected.get("pid") or 0)
        if _pid_exists(pid):
            return {"outcome": "blocked", "reason": "process_identity_unavailable"}
        pgid = int(expected.get("pgid") or 0)
        sid = int(expected.get("sid") or 0)
        if pgid <= 1 or sid <= 1 or pgid != sid or pgid != int(expected.get("pid") or 0):
            return {"outcome": "blocked", "reason": "invalid_launch_containment"}
        members = _posix_group_members(pgid, sid)
        if members is None:
            return {"outcome": "failed", "reason": "group_inventory_unavailable"}
        if not members:
            return {"outcome": "already_gone"}
        # The kernel retains an isolated SID while any member survives, so the
        # session identifier cannot be reused. Recheck every member before
        # signaling the exact session.
        if not _safe_group_members(members):
            return {"outcome": "blocked", "reason": "process_group_not_safe"}
        if not apply:
            return {"outcome": "would_release"}
        return _terminate_safe_group(pgid, sid)
    if not _fingerprint_matches(expected, current):
        return {"outcome": "blocked", "reason": "process_fingerprint_mismatch"}
    if current.get("protected_class"):
        return {"outcome": "blocked", "reason": "process_became_protected"}
    if str(current.get("tty") or "") not in {"", "?", "??", "-"}:
        return {"outcome": "blocked", "reason": "interactive_tty"}
    if not apply:
        return {"outcome": "would_release"}
    pid = int(current["pid"])
    pgid = int(current.get("pgid") or 0)
    group_cleanup = pgid == pid and pgid != os.getpgrp()
    if group_cleanup:
        members = _posix_group_members(pgid, int(current.get("sid") or 0))
        if members is None:
            return {"outcome": "failed", "reason": "group_inventory_unavailable"}
        if not _safe_group_members(members):
            return {"outcome": "blocked", "reason": "process_group_not_safe"}
        return _terminate_safe_group(pgid, int(current.get("sid") or 0))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"outcome": "released"}
    except OSError as exc:
        return {"outcome": "failed", "reason": type(exc).__name__}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        observed = process_identity(pid)
        if observed is None and not _pid_exists(pid):
            return {"outcome": "released"}
        if observed is not None and not _fingerprint_matches(expected, observed):
            return {"outcome": "released"}
        time.sleep(0.1)
    observed = process_identity(pid)
    if observed is None:
        return (
            {"outcome": "failed", "reason": "graceful_timeout"}
            if _pid_exists(pid)
            else {"outcome": "released"}
        )
    if not _fingerprint_matches(expected, observed):
        return {"outcome": "released"}
    if str(observed.get("tty") or "") not in {"", "?", "??", "-"}:
        return {"outcome": "blocked", "reason": "interactive_tty"}
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return {"outcome": "released"}
    except OSError as exc:
        return {"outcome": "failed", "reason": type(exc).__name__}
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return {"outcome": "released"}
        time.sleep(0.05)
    return {"outcome": "failed", "reason": "force_timeout"}


def _terminate_safe_group(pgid: int, sid: int) -> dict[str, Any]:
    for action, wait_seconds in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        members = _posix_group_members(pgid, sid)
        if members is None:
            return {"outcome": "failed", "reason": "group_inventory_unavailable"}
        if not members:
            return {"outcome": "released"}
        if not _safe_group_members(members):
            return {"outcome": "blocked", "reason": "process_group_not_safe"}
        try:
            for group_id in _session_group_ids(members):
                os.killpg(group_id, action)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return {"outcome": "failed", "reason": type(exc).__name__}
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            remaining = _posix_group_members(pgid, sid)
            if remaining == []:
                return {"outcome": "released"}
            if remaining is None:
                return {"outcome": "failed", "reason": "group_inventory_unavailable"}
            time.sleep(0.05)
    return {"outcome": "failed", "reason": "force_timeout"}


def _session_group_ids(members: list[dict[str, Any]]) -> list[int]:
    return sorted(
        {
            int(member.get("pgid") or member.get("pid") or 0)
            for member in members
            if int(member.get("pgid") or member.get("pid") or 0) > 1
        }
    )


def _pid_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _safe_group_members(members: list[dict[str, Any]]) -> bool:
    return all(
        member["uid"] == os.getuid()
        and member["tty"] in {"", "?", "??", "-"}
        and not member["protected"]
        for member in members
    )


def renew(home: Path, *, task_id: str, ttl: int) -> dict[str, Any]:
    with _registry_lock(home, "operation.lock"):
        return _renew(home, task_id=task_id, ttl=ttl)


def renew_lease(home: Path, *, lease_id: str, ttl: int) -> dict[str, Any]:
    if LEASE_ID_RE.fullmatch(lease_id) is None:
        raise ValueError("invalid lease id")
    ttl = _ttl(ttl)
    with _registry_lock(home, "operation.lock"):
        with _registry_lock(home):
            leases, invalid = load_leases(home)
            renewed = 0
            for path, lease in leases:
                if lease["lease_id"] != lease_id:
                    continue
                lease["expires_epoch"] = epoch_now() + ttl
                lease["ttl_seconds"] = ttl
                lease["stale_observations"] = 0
                _atomic_json(path, lease)
                renewed = 1
                break
        payload = _snapshot_payload(home)
        payload.update(
            {
                "operation": "renew-lease",
                "lease_id": lease_id,
                "renewed": renewed,
                "invalid_leases_seen": invalid,
            }
        )
        if renewed != 1:
            payload["status"] = "fail"
            _add_failure_cause(payload, "renewal_failed")
        return _write_receipt(home, payload)


def _renew(home: Path, *, task_id: str, ttl: int) -> dict[str, Any]:
    ttl = _ttl(ttl)
    with _registry_lock(home):
        leases, invalid = load_leases(home)
        renewed = 0
        now = epoch_now()
        for path, lease in leases:
            if lease.get("task_id") != task_id:
                continue
            lease["expires_epoch"] = now + ttl
            lease["ttl_seconds"] = ttl
            lease["stale_observations"] = 0
            _atomic_json(path, lease)
            renewed += 1
    payload = _snapshot_payload(home)
    payload.update(
        {
            "operation": "renew",
            "renewed": renewed,
            "invalid_leases_seen": invalid,
        }
    )
    if renewed == 0:
        payload["status"] = "fail"
        _add_failure_cause(payload, "renewal_failed")
    return _write_receipt(home, payload)


def _release_browser_tab(lease: dict[str, Any], apply: bool) -> dict[str, Any]:
    if lease.get("protected"):
        return {"outcome": "preserved"}
    resource = lease.get("resource") or {}
    try:
        endpoint = _normalize_endpoint(str(resource.get("endpoint") or ""))
    except ValueError:
        return {"outcome": "blocked", "reason": "invalid_endpoint"}
    target_id = str(resource.get("target_id") or "")
    present = _browser_target_present(endpoint, target_id)
    if present is False:
        return {"outcome": "already_gone"}
    if present is None:
        return {"outcome": "failed", "reason": "endpoint_unavailable"}
    if not apply:
        return {"outcome": "would_release"}
    try:
        quoted = urllib.parse.quote(target_id, safe="")
        _loopback_request(endpoint, f"/json/close/{quoted}")
    except Exception as exc:
        return {"outcome": "failed", "reason": type(exc).__name__}
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _browser_target_present(endpoint, target_id) is False:
            return {"outcome": "released"}
        time.sleep(0.05)
    return {"outcome": "failed", "reason": "target_still_present"}


def _release(lease: dict[str, Any], apply: bool) -> dict[str, Any]:
    if lease.get("kind") == "process":
        return _release_process(lease, apply)
    if lease.get("kind") == "browser_tab":
        return _release_browser_tab(lease, apply)
    return {"outcome": "blocked", "reason": "unknown_kind"}


def _resource_present(lease: dict[str, Any]) -> bool | None:
    resource = lease.get("resource") or {}
    if lease.get("kind") == "browser_tab":
        try:
            return _browser_target_present(
                _normalize_endpoint(str(resource.get("endpoint") or "")),
                str(resource.get("target_id") or ""),
            )
        except ValueError:
            return None
    if lease.get("kind") != "process":
        return None
    if os.name == "nt":
        if not any(
            key in resource
            for key in ("guard_owner_pid", "guard_owner_birth_id", "guard_handle")
        ):
            return None
        job, guard_status = _windows_duplicate_remote_job_guard(
            int(resource.get("guard_owner_pid") or 0),
            str(resource.get("guard_owner_birth_id") or ""),
            int(resource.get("guard_handle") or 0),
            close_source=False,
        )
        if job is None:
            return False if guard_status == "gone" else None
        _windows_close_process_anchor(job)
        return True
    current = process_identity(int(resource.get("pid") or 0))
    if current is not None and _fingerprint_matches(resource, current):
        return True
    pid = int(resource.get("pid") or 0)
    pgid = int(resource.get("pgid") or 0)
    sid = int(resource.get("sid") or 0)
    # register_process only admits session leaders with PID == PGID == SID;
    # broader process-group leases are outside this ownership contract.
    if pgid > 1 and sid > 1 and pgid == sid == pid:
        # An isolated POSIX session retains its SID while any descendant lives,
        # even after its leader exits and that numeric PID is reused elsewhere.
        members = _posix_group_members(pgid, sid)
        return None if members is None else bool(members)
    if current is not None:
        return False
    return None if _pid_exists(pid) else False


def _matching_lease_exists(intent: dict[str, Any], leases: list[tuple[Path, dict[str, Any]]]) -> bool:
    resource = intent.get("resource") or {}
    for _path, lease in leases:
        if lease.get("kind") != intent.get("kind"):
            continue
        owned = lease.get("resource") or {}
        if intent.get("kind") == "browser_tab":
            if (
                resource.get("endpoint") == owned.get("endpoint")
                and resource.get("target_id") == owned.get("target_id")
            ):
                return True
        elif resource.get("pid") and _fingerprint_matches(resource, owned):
            return True
    return False


def _recover_intent(
    intent: dict[str, Any],
    leases: list[tuple[Path, dict[str, Any]]],
    apply: bool,
) -> dict[str, Any]:
    resource = intent.get("resource") or {}
    if _matching_lease_exists(intent, leases):
        return {"outcome": "registered"}
    if intent.get("kind") == "process":
        if not isinstance(resource.get("pid"), int):
            return {"outcome": "abandoned_before_create"}
        synthetic = {
            "kind": "process",
            "protected": bool(intent.get("protected")),
            "resource": resource,
        }
        return _release_process(synthetic, apply)
    target_id = resource.get("target_id")
    if not isinstance(target_id, str):
        return {"outcome": "blocked", "reason": "browser_create_outcome_unknown"}
    synthetic = {
        "kind": "browser_tab",
        "protected": bool(intent.get("protected")),
        "resource": resource,
    }
    return _release_browser_tab(synthetic, apply)


def _process_census() -> dict[str, int]:
    if os.name == "nt":
        return {"tty_shells": 0, "idle_tty_shells": 0, "debug_browser_roots": 0}
    try:
        result = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,ppid=,uid=,tty=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return {"tty_shells": 0, "idle_tty_shells": 0, "debug_browser_roots": 0}
    rows: list[tuple[int, int, int, str, str]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4]))
        except ValueError:
            continue
    children = {ppid for _pid, ppid, _uid, _tty, _cmd in rows}
    tty_shells = 0
    idle_tty_shells = 0
    debug_browser_roots = 0
    for pid, _ppid, uid, tty, command in rows:
        if uid != os.getuid():
            continue
        executable = Path(command.split(None, 1)[0]).name.lstrip("-")
        if tty not in {"?", "??", "-"} and executable in SHELL_NAMES:
            tty_shells += 1
            if pid not in children:
                idle_tty_shells += 1
        lowered = command.lower()
        if (
            "--remote-debugging-port=" in lowered
            and "--type=" not in lowered
            and any(
            name in lowered for name in ("brave", "chrome", "chromium")
            )
        ):
            debug_browser_roots += 1
    return {
        "tty_shells": tty_shells,
        "idle_tty_shells": idle_tty_shells,
        "debug_browser_roots": debug_browser_roots,
    }


def _process_registry_coverage(
    home: Path, leases: list[tuple[Path, dict[str, Any]]]
) -> dict[str, int | str]:
    """Compare live host-scoped ProcessRegistry rows with durable leases."""
    path = home / "processes.json"
    empty = {"status": "pass", "known": 0, "owned": 0, "unowned": 0, "invalid": 0}
    if not path.exists():
        return empty
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return {**empty, "status": "unknown", "invalid": 1}
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {**empty, "status": "unknown", "invalid": 1}
    if not isinstance(rows, list) or len(rows) > 128:
        return {**empty, "status": "unknown", "invalid": 1}
    process_leases = [
        lease
        for _lease_path, lease in leases
        if lease.get("kind") == "process" and isinstance(lease.get("resource"), dict)
    ]
    known = owned = invalid = 0
    for row in rows:
        if not isinstance(row, dict) or row.get("pid_scope", "host") != "host":
            continue
        ownership_scope = row.get("host_steward_scope", "legacy")
        if ownership_scope == "user":
            continue
        if ownership_scope != "managed":
            invalid += 1
            continue
        pid = row.get("pid")
        task_id = (
            row.get("task_id")
            or row.get("session_key")
            or row.get("session_id")
            or row.get("id")
        )
        if (
            not _plain_int(pid, minimum=2)
            or not isinstance(task_id, str)
            or TASK_ID_RE.fullmatch(task_id) is None
        ):
            invalid += 1
            continue
        current = process_identity(pid)
        if current is None:
            continue
        known += 1
        if any(
            lease.get("task_id") == task_id
            and _fingerprint_matches(lease.get("resource") or {}, current)
            for lease in process_leases
        ):
            owned += 1
    unowned = known - owned
    return {
        "status": "unknown" if invalid else ("gap" if unowned else "pass"),
        "known": known,
        "owned": owned,
        "unowned": unowned,
        "invalid": invalid,
    }


def _process_registry_manages_lease(
    home: Path, lease: dict[str, Any]
) -> bool | None:
    """Return whether the durable runtime inventory still owns this process.

    ``None`` means the inventory cannot be trusted, so reconciliation must
    preserve the process rather than risk killing valid work.
    """
    path = home / "processes.json"
    if not path.exists():
        return False
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return None
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list) or len(rows) > 128:
        return None
    expected = lease.get("resource") or {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        if row.get("pid_scope", "host") != "host":
            continue
        scope = row.get("host_steward_scope")
        if scope == "user":
            continue
        if scope != "managed":
            return None
        task_id = (
            row.get("task_id")
            or row.get("session_key")
            or row.get("session_id")
            or row.get("id")
        )
        if (
            not isinstance(task_id, str)
            or TASK_ID_RE.fullmatch(task_id) is None
            or not _plain_int(row.get("pid"), minimum=2)
        ):
            return None
        if task_id != lease.get("task_id"):
            continue
        current = process_identity(int(row["pid"]))
        if current is not None and _fingerprint_matches(expected, current):
            return True
    return False


def _storage_headroom(home: Path, used_pct: int | None) -> dict[str, Any] | None:
    path = home / "config/host-steward-storage.json"
    headroom: dict[str, Any] = {
        "policy_status": "invalid", "idle_used_percent": None,
        "working_used_percent": None, "idle_status": "unknown",
        "working_status": "unknown", "policy_sha256": None,
    }
    try:
        if path.parent.is_symlink():
            raise ValueError("unsafe policy directory")
        try:
            expected = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError("unsafe policy file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            observed = os.fstat(stream.fileno())
            if (not stat.S_ISREG(observed.st_mode)
                    or (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino)):
                raise ValueError("changed policy file")
            raw = stream.read(MAX_STORAGE_POLICY_BYTES + 1)
        if len(raw) > MAX_STORAGE_POLICY_BYTES:
            raise ValueError("oversized policy")
        headroom["policy_sha256"] = hashlib.sha256(raw).hexdigest()
        policy = json.loads(raw)
        if not isinstance(policy, dict) or type(policy.get("schema_version")) is not int or policy["schema_version"] != 1:
            raise ValueError("invalid policy schema")
        idle, working = policy.get("idle_used_percent"), policy.get("working_used_percent")
        if (not all(type(value) in (int, float) and math.isfinite(value) for value in (idle, working))
                or not 0 < idle <= working < 100):
            raise ValueError("invalid headroom targets")
        headroom.update(policy_status="valid", idle_used_percent=idle, working_used_percent=working)
        if used_pct is not None:
            headroom.update(idle_status="warn" if used_pct > idle else "pass",
                            working_status="warn" if used_pct > working else "pass")
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    return headroom


def _storage_snapshot(home: Path) -> dict[str, Any]:
    """Observe volume pressure and the existing retention owner, without scanning files."""
    storage: dict[str, Any] = {
        "schema_version": 1, "measured_at": utc_now(), "status": "unknown",
        "pressure_status": "unknown", "total_bytes": None, "used_bytes": None,
        "free_bytes": None, "used_pct": None,
        "cleanup_owner": "hermes-disk-retention", "automatic_delete": False,
    }
    try:
        usage = shutil.disk_usage(home)
        warn_pct = int(os.environ.get("HERMES_DISK_WARN_PERCENT", "85"))
        fail_pct = int(os.environ.get("HERMES_DISK_FAIL_PERCENT", "92"))
        warn_free = int(os.environ.get("HERMES_DISK_WARN_FREE_BYTES", str(15 * 1024**3)))
        fail_free = int(os.environ.get("HERMES_DISK_FAIL_FREE_BYTES", str(5 * 1024**3)))
        if not (0 < warn_pct <= fail_pct <= 100 and 0 <= fail_free <= warn_free
                and usage.total > 0 and 0 <= usage.free <= usage.total
                and 0 <= usage.used <= usage.total):
            raise ValueError("invalid storage measurement or thresholds")
        pct = int(usage.used * 100 / usage.total)
        # Match the existing local-selfcheck disk policy, including its hard 95% ceiling.
        pressure = "pass"
        if pct >= 95 or usage.free < fail_free or (pct >= fail_pct and usage.free < warn_free):
            pressure = "fail"
        elif pct >= warn_pct or usage.free < warn_free:
            pressure = "warn"
        storage.update(total_bytes=usage.total, used_bytes=usage.used,
                       free_bytes=usage.free, used_pct=pct, pressure_status=pressure)
    except (OSError, ValueError):
        pass

    headroom = _storage_headroom(home, storage["used_pct"])
    if headroom is not None:
        storage["headroom"] = headroom

    retention: dict[str, Any] = {
        "status": "missing", "checked_at": None, "age_seconds": None, "mode": None,
        "planned_reclaim_bytes": 0, "deleted_reclaim_bytes": 0,
        "errors_count": 0, "protected_runtime_count": 0,
    }
    path = home / "state/disk-retention-last.json"
    try:
        if path.is_symlink() or not path.is_file():
            if path.exists() or path.is_symlink():
                raise ValueError("unsafe retention receipt")
        else:
            with path.open("rb") as stream:
                raw = stream.read(MAX_STORAGE_RECEIPT_BYTES + 1)
            if len(raw) > MAX_STORAGE_RECEIPT_BYTES:
                raise ValueError("oversized retention receipt")
            receipt = json.loads(raw)
            if (not isinstance(receipt, dict) or receipt.get("kind") != "hermes_disk_retention"
                    or receipt.get("schema_version") != 2
                    or receipt.get("status") not in {"pass", "warn", "block", "error"}
                    or receipt.get("mode") not in {"apply", "dry-run"}
                    or not isinstance(receipt.get("hermes_home"), str)
                    or not Path(receipt["hermes_home"]).is_absolute()
                    or Path(str(receipt.get("hermes_home") or "")).resolve() != home.resolve()):
                raise ValueError("wrong retention receipt")
            checked = receipt.get("checked_at")
            stamp = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("ambiguous retention timestamp")
            age = int(epoch_now() - stamp.timestamp())
            if age < -300:
                raise ValueError("future retention receipt")
            errors = receipt.get("errors")
            protection = receipt.get("runtime_protection")
            if not isinstance(errors, list) or not isinstance(protection, dict):
                raise ValueError("incomplete retention evidence")
            counts = {}
            for key in ("planned_reclaim_bytes", "deleted_reclaim_bytes"):
                value = receipt.get(key)
                if type(value) is not int or value < 0:
                    raise ValueError("invalid retention count")
                counts[key] = value
            protected = set()
            for key in ("active", "rollback", "referenced"):
                roots = protection.get(key)
                if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
                    raise ValueError("invalid retention protection")
                protected.update(roots)
            retention.update(
                status="stale" if age > 172800 else ("error" if errors else receipt["status"]),
                checked_at=checked, age_seconds=max(0, age), mode=receipt["mode"],
                errors_count=len(errors), protected_runtime_count=len(protected), **counts,
            )
    except (OSError, ValueError, TypeError, OverflowError):
        retention["status"] = "invalid"
    storage["retention"] = retention
    pressure = storage["pressure_status"]
    if pressure == "fail" or retention["status"] in {"block", "error"}:
        storage["status"] = "fail"
    elif pressure == "unknown":
        storage["status"] = "unknown"
    elif (pressure == "warn" or retention["status"] != "pass"
          or (headroom is not None and headroom["idle_status"] != "pass")):
        storage["status"] = "warn"
    else:
        storage["status"] = "pass"
    return storage


def _snapshot_payload(home: Path) -> dict[str, Any]:
    leases, invalid = load_leases(home)
    intents, invalid_intents = load_intents(home)
    now = epoch_now()
    counts = {
        "active": 0,
        "expired": 0,
        "protected": 0,
        "process": 0,
        "browser_tab": 0,
        "invalid": invalid,
        "pending_intents": len(intents),
        "invalid_intents": invalid_intents,
    }
    managed_processes: set[int] = set()
    managed_endpoints: set[str] = set()
    for _path, lease in leases:
        counts["expired" if int(lease.get("expires_epoch") or 0) <= now else "active"] += 1
        counts[str(lease["kind"])] += 1
        if lease.get("protected"):
            counts["protected"] += 1
        resource = lease.get("resource") or {}
        if lease["kind"] == "process" and isinstance(resource.get("pid"), int):
            managed_processes.add(resource["pid"])
        if lease["kind"] == "browser_tab" and isinstance(resource.get("endpoint"), str):
            managed_endpoints.add(resource["endpoint"])
    coverage = _process_registry_coverage(home, leases)
    failure_causes: list[str] = []
    if invalid:
        failure_causes.append("invalid_leases")
    if invalid_intents:
        failure_causes.append("invalid_intents")
    if coverage["status"] == "unknown":
        failure_causes.append("process_registry_unknown")
    elif coverage["status"] == "gap":
        failure_causes.append("process_registry_gap")
    snapshot_status = "pass"
    if invalid or invalid_intents or coverage["status"] == "unknown":
        snapshot_status = "invalid"
    elif coverage["status"] == "gap":
        snapshot_status = "fail"
    storage = _storage_snapshot(home)
    if storage["pressure_status"] in {"warn", "fail"}:
        failure_causes.append("storage_pressure")
    if storage["retention"]["status"] in {"warn", "block", "error"}:
        failure_causes.append("storage_retention")
    if (storage["pressure_status"] == "unknown"
            or storage["retention"]["status"] in {"missing", "stale", "invalid"}):
        failure_causes.append("storage_evidence_unknown")
    if snapshot_status == "pass":
        snapshot_status = "fail" if storage["status"] == "fail" else (
            "pass" if storage["status"] == "pass" else "warn"
        )
    return {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "status": snapshot_status,
        "failure_causes": failure_causes,
        "storage": storage,
        "counts": counts,
        "census": _process_census(),
        "managed": {
            "processes": len(managed_processes),
            "browser_endpoints": len(managed_endpoints),
        },
        "coverage": {"process_registry": coverage},
        "safety": {
            "unowned_resources_audit_only": True,
            "interactive_ttys_protected": True,
            "content_free_snapshot": True,
            "windows_process_apply": True,
        },
    }


def _write_receipt(home: Path, payload: dict[str, Any]) -> dict[str, Any]:
    _atomic_json(_state_dir(home) / "latest.json", payload)
    _atomic_json(
        home / "state" / "capabilities" / "host-steward-v1.json",
        {
            "schema": "hermes-capability/v1",
            "capability": SCHEMA,
            "activated_at": utc_now(),
        },
    )
    return payload


def _add_failure_cause(payload: dict[str, Any], cause: str) -> None:
    causes = payload.setdefault("failure_causes", [])
    if cause not in causes:
        causes.append(cause)


def snapshot(home: Path) -> dict[str, Any]:
    with _registry_lock(home, "operation.lock"):
        with _registry_lock(home):
            return _write_receipt(home, _snapshot_payload(home))


def finish(home: Path, *, task_id: str, apply: bool) -> dict[str, Any]:
    with _registry_lock(home, "operation.lock"):
        return _finish(home, task_id=task_id, apply=apply)


def release_lease(home: Path, *, lease_id: str, apply: bool) -> dict[str, Any]:
    """Release exactly one birth-bound resource lease."""
    if LEASE_ID_RE.fullmatch(lease_id) is None:
        raise ValueError("invalid lease id")
    with _registry_lock(home, "operation.lock"):
        with _registry_lock(home):
            leases, invalid = load_leases(home)
            match = next(
                ((path, lease) for path, lease in leases if lease["lease_id"] == lease_id),
                None,
            )
            if match is None:
                result = {"outcome": "already_gone"}
            else:
                path, lease = match
                result = _release(lease, apply)
                if apply and result["outcome"] in {"released", "already_gone", "preserved"}:
                    path.unlink(missing_ok=True)
        payload = _snapshot_payload(home)
        payload.update(
            {
                "operation": "release-lease",
                "mode": "apply" if apply else "dry_run",
                "lease_id": lease_id,
                "invalid_leases_seen": invalid,
                **result,
            }
        )
        if apply and result["outcome"] in {"failed", "blocked"}:
            payload["status"] = "fail"
            _add_failure_cause(payload, "release_failed")
        return _write_receipt(home, payload)


def _finish(home: Path, *, task_id: str, apply: bool) -> dict[str, Any]:
    with _registry_lock(home):
        leases, invalid = load_leases(home)
        intents, invalid_intents = load_intents(home)
        results: list[dict[str, Any]] = []
        intent_results: list[dict[str, Any]] = []
        for path, lease in leases:
            if lease.get("task_id") != task_id:
                continue
            result = _release(lease, apply)
            results.append({"lease_id": lease["lease_id"], "kind": lease["kind"], **result})
            if apply and result["outcome"] in {"released", "already_gone", "preserved"}:
                path.unlink(missing_ok=True)
        for path, intent in intents:
            if intent.get("task_id") != task_id:
                continue
            result = _recover_intent(intent, leases, apply)
            intent_results.append(
                {"lease_id": intent["lease_id"], "kind": intent["kind"], **result}
            )
            if apply and result["outcome"] in {
                "released",
                "already_gone",
                "registered",
                "abandoned_before_create",
                "preserved",
            }:
                path.unlink(missing_ok=True)
    payload = _snapshot_payload(home)
    payload.update(
        {
            "operation": "finish",
            "mode": "apply" if apply else "dry_run",
            "invalid_leases_seen": invalid,
            "invalid_intents_seen": invalid_intents,
            "intent_results": intent_results,
            "results": results,
        }
    )
    if apply and any(
        row.get("outcome") not in {"released", "already_gone", "preserved"}
        for row in results
    ) or apply and any(
        row.get("outcome")
        not in {
            "released",
            "already_gone",
            "registered",
            "abandoned_before_create",
            "preserved",
        }
        for row in intent_results
    ):
        payload["status"] = "fail"
        _add_failure_cause(payload, "finish_failed")
    return _write_receipt(home, payload)


def reconcile(home: Path, *, apply: bool) -> dict[str, Any]:
    with _registry_lock(home, "operation.lock"):
        return _reconcile(home, apply=apply)


def _reconcile(home: Path, *, apply: bool) -> dict[str, Any]:
    with _registry_lock(home):
        leases, invalid = load_leases(home)
        intents, invalid_intents = load_intents(home)
        now = epoch_now()
        results: list[dict[str, Any]] = []
        intent_results: list[dict[str, Any]] = []
        stale_intents = [
            (path, intent)
            for path, intent in intents
            if int(intent.get("created_epoch") or 0) <= now - INTENT_GRACE_SECONDS
        ]
        stale_intents.sort(
            key=lambda item: (
                int(item[1].get("last_observation_epoch") or 0),
                int(item[1].get("created_epoch") or 0),
                str(item[1].get("lease_id") or ""),
            )
        )
        selected_intents = stale_intents[:MAX_RECONCILE_INTENTS]
        for path, intent in selected_intents:
            last_observation = int(intent.get("last_observation_epoch") or 0)
            advance = apply and (
                last_observation == 0
                or now - last_observation >= MIN_STALE_OBSERVATION_INTERVAL_SECONDS
            )
            observations = int(intent.get("stale_observations") or 0) + (1 if advance else 0)
            intent["stale_observations"] = observations
            if advance:
                intent["last_observation_epoch"] = now
            should_apply = advance and observations >= STALE_OBSERVATIONS_REQUIRED
            result = _recover_intent(intent, leases, should_apply)
            intent_results.append(
                {
                    "lease_id": intent["lease_id"],
                    "kind": intent["kind"],
                    "stale_observations": observations,
                    "observation_advanced": advance,
                    **result,
                }
            )
            if should_apply and result["outcome"] in {
                "released",
                "already_gone",
                "registered",
                "abandoned_before_create",
                "preserved",
            }:
                path.unlink(missing_ok=True)
            elif apply:
                _atomic_json(path, intent)
        expired = [
            (path, lease)
            for path, lease in leases
            if int(lease.get("expires_epoch") or 0) <= now
        ]
        expired.sort(
            key=lambda item: (
                int(item[1].get("last_reconciled_epoch") or 0),
                int(item[1].get("expires_epoch") or 0),
                str(item[1].get("lease_id") or ""),
            )
        )
        selected = expired[:MAX_RECONCILE_LEASES]
        for path, lease in selected:
            if lease.get("protected"):
                present = _resource_present(lease)
                if present is False:
                    if apply:
                        path.unlink(missing_ok=True)
                    results.append(
                        {
                            "lease_id": lease["lease_id"],
                            "kind": lease["kind"],
                            "stale_observations": 0,
                            "observation_advanced": False,
                            "outcome": "already_gone",
                        }
                    )
                    continue
                if apply:
                    lease["expires_epoch"] = now + int(lease["ttl_seconds"])
                    lease["stale_observations"] = 0
                    lease["last_reconciled_epoch"] = now
                    _atomic_json(path, lease)
                results.append(
                    {
                        "lease_id": lease["lease_id"],
                        "kind": lease["kind"],
                        "stale_observations": 0,
                        "observation_advanced": False,
                        "outcome": "renewed_protected" if apply else "would_renew_protected",
                    }
                )
                continue
            if lease.get("kind") == "process":
                managed = _process_registry_manages_lease(home, lease)
                if managed is None:
                    results.append(
                        {
                            "lease_id": lease["lease_id"],
                            "kind": lease["kind"],
                            "stale_observations": int(lease.get("stale_observations") or 0),
                            "observation_advanced": False,
                            "outcome": "blocked",
                            "reason": "process_registry_inventory_unavailable",
                        }
                    )
                    continue
                if managed:
                    if apply:
                        lease["expires_epoch"] = now + int(lease["ttl_seconds"])
                        lease["stale_observations"] = 0
                        lease["last_reconciled_epoch"] = now
                        _atomic_json(path, lease)
                    results.append(
                        {
                            "lease_id": lease["lease_id"],
                            "kind": lease["kind"],
                            "stale_observations": 0,
                            "observation_advanced": False,
                            "outcome": "renewed" if apply else "would_renew",
                        }
                    )
                    continue
            last_observation = int(lease.get("last_observation_epoch") or 0)
            advance_observation = apply and (
                last_observation == 0
                or now - last_observation >= MIN_STALE_OBSERVATION_INTERVAL_SECONDS
            )
            observations = int(lease.get("stale_observations") or 0) + (
                1 if advance_observation else 0
            )
            lease["stale_observations"] = observations
            if apply:
                lease["last_reconciled_epoch"] = now
            if advance_observation:
                lease["last_observation_epoch"] = now
            should_apply = advance_observation and observations >= STALE_OBSERVATIONS_REQUIRED
            result = _release(lease, should_apply)
            results.append(
                {
                    "lease_id": lease["lease_id"],
                    "kind": lease["kind"],
                    "stale_observations": observations,
                    "observation_advanced": advance_observation,
                    **result,
                }
            )
            if should_apply and result["outcome"] in {"released", "already_gone", "preserved"}:
                path.unlink(missing_ok=True)
            elif apply:
                _atomic_json(path, lease)
    payload = _snapshot_payload(home)
    payload.update(
        {
            "operation": "reconcile",
            "mode": "apply" if apply else "dry_run",
            "required_stale_observations": STALE_OBSERVATIONS_REQUIRED,
            "minimum_observation_interval_seconds": MIN_STALE_OBSERVATION_INTERVAL_SECONDS,
            "max_leases_per_run": MAX_RECONCILE_LEASES,
            "max_intents_per_run": MAX_RECONCILE_INTENTS,
            "deferred_expired": max(0, len(expired) - len(selected)),
            "deferred_intents": max(0, len(stale_intents) - len(selected_intents)),
            "invalid_leases_seen": invalid,
            "invalid_intents_seen": invalid_intents,
            "intent_results": intent_results,
            "results": results,
        }
    )
    if apply and any(
        row.get("outcome") in {"failed", "blocked"}
        for row in [*intent_results, *results]
    ):
        payload["status"] = "fail"
        _add_failure_cause(payload, "reconcile_failed")
    return _write_receipt(home, payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--hermes-home")
    sub = parser.add_subparsers(dest="operation", required=True)

    launch_process_parser = sub.add_parser("launch-process")
    launch_process_parser.add_argument("--task-id")
    launch_process_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    launch_process_parser.add_argument("--protected", action="store_true")
    launch_process_parser.add_argument("launch_argv", nargs=argparse.REMAINDER)

    register_process_parser = sub.add_parser("register-process")
    register_process_parser.add_argument("--task-id")
    register_process_parser.add_argument("--pid", type=int, required=True)
    register_process_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    register_process_parser.add_argument("--protected", action="store_true")
    register_process_parser.add_argument("--systemd-unit", default="")

    create_tab_parser = sub.add_parser("create-browser-tab")
    create_tab_parser.add_argument("--task-id")
    create_tab_parser.add_argument("--endpoint", required=True)
    create_tab_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    create_tab_parser.add_argument("--protected", action="store_true")

    finish_parser = sub.add_parser("finish")
    finish_parser.add_argument("--task-id")
    finish_parser.add_argument("--apply", action="store_true")

    release_parser = sub.add_parser("release-lease")
    release_parser.add_argument("--lease-id", required=True)
    release_parser.add_argument("--apply", action="store_true")

    renew_parser = sub.add_parser("renew")
    renew_parser.add_argument("--task-id")
    renew_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)

    renew_lease_parser = sub.add_parser("renew-lease")
    renew_lease_parser.add_argument("--lease-id", required=True)
    renew_lease_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)

    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--apply", action="store_true")
    sub.add_parser("snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "__exec-gate":
        return _exec_gate(raw_argv[1:])
    args = _parser().parse_args(raw_argv)
    home = _home(args.hermes_home)
    try:
        if args.operation == "launch-process":
            payload = launch_process(
                home,
                task_id=_task_id(args.task_id),
                command=args.launch_argv,
                ttl=args.ttl,
                protected=args.protected,
            )
        elif args.operation == "register-process":
            payload = register_process(
                home,
                task_id=_task_id(args.task_id),
                pid=args.pid,
                ttl=args.ttl,
                protected=args.protected,
                systemd_unit=args.systemd_unit,
            )
        elif args.operation == "create-browser-tab":
            payload = create_browser_tab(
                home,
                task_id=_task_id(args.task_id),
                endpoint=args.endpoint,
                ttl=args.ttl,
                protected=args.protected,
            )
        elif args.operation == "finish":
            payload = finish(home, task_id=_task_id(args.task_id), apply=args.apply)
        elif args.operation == "release-lease":
            payload = release_lease(home, lease_id=args.lease_id, apply=args.apply)
        elif args.operation == "renew":
            payload = renew(home, task_id=_task_id(args.task_id), ttl=args.ttl)
        elif args.operation == "renew-lease":
            payload = renew_lease(home, lease_id=args.lease_id, ttl=args.ttl)
        elif args.operation == "reconcile":
            payload = reconcile(home, apply=args.apply)
        else:
            payload = snapshot(home)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if payload.get("status") in {"fail", "invalid", "error"} else 0
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"schema": SCHEMA, "status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
