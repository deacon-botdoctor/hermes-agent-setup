#!/usr/bin/env python3
"""Bind native Browser Use to Golden's managed, privacy-safe CLI install."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

MARKER = "HERMES_NATIVE_BROWSER_USE_SAFETY_v1"
REVISION_MARKER = "HERMES_NATIVE_BROWSER_USE_SAFETY_v1_r3"
TARGET = Path("tools/browser_use_cli.py")
TEST_TARGET = Path("tests/tools/test_browser_use_cli.py")
MODEL_TOOLS_TARGET = Path("model_tools.py")
INSTALL_SH_TARGET = Path("scripts/install.sh")
INSTALL_PS1_TARGET = Path("scripts/install.ps1")
TOOLS_CONFIG_TARGET = Path("hermes_cli/tools_config.py")
BACKUP_SUFFIX = ".bak-pre-native-browser-use-safety-v1"

IMPORT_ANCHOR = "import json\n"
IMPORT_REPLACEMENT = "import hashlib\nimport json\nimport stat\nimport threading\n"
TYPING_IMPORT_ANCHOR = "from typing import Any, Dict, List, Optional\n"
TYPING_IMPORT_REPLACEMENT = "from pathlib import Path\nfrom typing import Any, Dict, List, Optional\n"
SHUTIL_IMPORT_ANCHOR = "import shutil\n"
SHUTIL_IMPORT_REPLACEMENT = "import shutil\nimport tempfile\n"

CONSTANT_ANCHOR = """_BACKEND_KEY = "browser-use"
BACKEND_DISABLED = "off"
"""
CONSTANT_REPLACEMENT = f"""_BACKEND_KEY = "browser-use"
BACKEND_DISABLED = "off"

# {MARKER}: Golden receipt-binds the complete environment, entry point, and interpreter.
# {REVISION_MARKER}
_PINNED_BROWSER_USE_VERSION = "0.13.7"
_PINNED_BROWSER_HARNESS_VERSION = "0.1.8"
_MANAGED_RECEIPT = "browser-use-cli-install-v1.json"
_MAX_RECEIPT_BYTES = 65536
_MAX_CLI_BYTES = 1048576
_MAX_INTERPRETER_BYTES = 268435456
_MAX_ENVIRONMENT_BYTES = 4294967296
_MAX_ENVIRONMENT_ENTRIES = 10000
_MAX_ENVIRONMENT_PATH_BYTES = 67108864
_ENVIRONMENT_HASH_TIMEOUT_SECONDS = 300
# The full environment fingerprint walks the managed browser environment. Keep
# definition lookup cheap during normal turns while still revalidating it on a
# bounded cadence and immediately after any integrity-check failure.
_STATE_FINGERPRINT_TTL_SECONDS = 300.0
_PINNED_LOCK_SHA256 = "baafc493e5b7e104c4417dede1dd722bd14fc31ac6188f13fe2ad124d573bdec"
_PINNED_ARTIFACT_SHA256 = {{
    "browser-use": "2264439e45cc7dd7fe480ca37e9eabd040c31a4e4d5e20c069ad2f60c07e3ba8",
    "browser-harness": "4bbc414007750683408a6cf4e5c87dd62c85b8628e478d5020413814fde8ae50",
}}
_EPHEMERAL_PROFILE_NAME = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
_PRIVACY_ENV = {{
    "BH_TELEMETRY": "0",
    "BROWSER_HARNESS_TELEMETRY": "0",
    "ANONYMIZED_TELEMETRY": "false",
    "BROWSER_USE_CLOUD_SYNC": "false",
}}
_INHERITED_ENV_NAMES = {{
    "ALL_PROXY",
    "APPDATA",
    "BROWSER_USE_API_KEY",
    "COMSPEC",
    "CURL_CA_BUNDLE",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LOCALAPPDATA",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "REQUESTS_CA_BUNDLE",
    "SECURITYSESSIONID",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "USERNAME",
    "WAYLAND_DISPLAY",
    "WINDIR",
    "XAUTHORITY",
    "__CF_USER_TEXT_ENCODING",
}}
"""

MODE_ANCHOR = """    backend = get_browser_backend()
    if backend:
        return backend == _BACKEND_KEY
    if is_legacy_browser_use_cloud_config(_read_browser_cfg()):
        return True
    # Default (backend unset): Browser Use mode when the CLI can run at all;
    # otherwise keep the built-in tools so browsing never silently breaks.
    return _find_cli() is not None
"""
MODE_REPLACEMENT = """    backend = get_browser_backend()
    if backend and backend != _BACKEND_KEY:
        return False
    return _find_cli() is not None
"""

ENV_ANCHOR = """def _base_subprocess_env() -> dict:
    from tools.browser_tool import _build_browser_env

    return _build_browser_env()
"""
ENV_REPLACEMENT = """def _base_subprocess_env() -> dict:
    from hermes_constants import get_hermes_home
    from tools.browser_tool import _build_browser_env

    source_env = _build_browser_env()
    env = {
        key: value
        for key, value in source_env.items()
        if key.upper() in _INHERITED_ENV_NAMES or key.upper().startswith("LC_")
    }
    env.update(_PRIVACY_ENV)
    env.pop("BU_CDP_URL", None)
    env.pop("BU_CDP_WS", None)
    state_root = Path(get_hermes_home()) / "state" / "browser-use"
    profile_root = state_root / "browser-profile"
    profile_name = _profile_name()
    env.update({
        "BH_HOME": str(state_root),
        "BH_CONFIG_DIR": str(state_root / "config"),
        "BH_RUNTIME_DIR": str(state_root / "runtime"),
        "BH_TMP_DIR": str(state_root / "tmp"),
        "BH_AGENT_WORKSPACE": str(state_root / "agent-workspace"),
        "BU_NAME": f"hermes_{profile_name}",
        "HOME": str(profile_root),
        "USERPROFILE": str(profile_root),
        "APPDATA": str(profile_root / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile_root / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(profile_root / ".config"),
        "XDG_CACHE_HOME": str(profile_root / ".cache"),
        "BROWSER_USE_CONFIG_DIR": str(profile_root / ".config" / "browseruse"),
        "BROWSER_USE_CONFIG_PATH": str(profile_root / ".config" / "browseruse" / "config.json"),
    })
    return env


def _profile_name() -> str:
    from hermes_constants import get_hermes_home

    receipt_path = Path(get_hermes_home()) / "state" / _MANAGED_RECEIPT
    receipt_bytes = _read_regular_file(receipt_path, _MAX_RECEIPT_BYTES)
    if receipt_bytes is not None:
        try:
            receipt = json.loads(receipt_bytes.decode("utf-8"))
            profile_id = receipt.get("profile_id") if isinstance(receipt, dict) else None
            if isinstance(profile_id, str) and re.fullmatch(r"[0-9a-f]{32}", profile_id):
                return profile_id
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return _EPHEMERAL_PROFILE_NAME


def _profile_session_name(session: str) -> str:
    prefix = f"hermes_{_profile_name()}_"
    if len(prefix) + len(session) <= 64:
        return prefix + session
    digest = hashlib.sha256(session.encode()).hexdigest()[:16]
    available = 64 - len(prefix) - len(digest) - 1
    return f"{prefix}{session[:available]}_{digest}"


def _run_managed_cli(*args, env: dict, **kwargs):
    with tempfile.TemporaryDirectory(prefix="hermes-browser-use-pycache-") as pycache:
        isolated_env = env.copy()
        isolated_env["PYTHONDONTWRITEBYTECODE"] = "1"
        isolated_env["PYTHONNOUSERSITE"] = "1"
        isolated_env["PYTHONPYCACHEPREFIX"] = pycache
        return subprocess.run(*args, env=isolated_env, **kwargs)
"""

FIND_ANCHOR = '''def _find_cli() -> Optional[List[str]]:
    """Locate the browser-use CLI, or None when it can't be run.

    Prefers an installed browser-use binary; falls back to running it
    through uvx
    """
    direct = shutil.which("browser-use")
    if direct:
        return [direct]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "browser-use"]
    return None
'''
FIND_REPLACEMENT = '''def _file_identity(info):
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    return tuple(getattr(info, field, None) for field in fields)


def _consume_regular_file(
    path: Path,
    max_bytes: int,
    consume,
    deadline_seconds: float = 30,
    monotonic=None,
    deadline=None,
) -> Optional[int]:
    monotonic = monotonic or time.monotonic
    if deadline is None:
        deadline = monotonic() + deadline_seconds
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        total_bytes = 0
        while True:
            if monotonic() >= deadline:
                return None
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total_bytes + 1))
            if monotonic() >= deadline:
                return None
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                return None
            consume(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if _file_identity(before) != _file_identity(after):
            return None
        if _file_identity(before) != _file_identity(current):
            return None
        return total_bytes
    except (OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sha256_file(
    path: Path,
    max_bytes: int,
    deadline_seconds: float = 30,
    monotonic=None,
    deadline=None,
    include_size: bool = False,
):
    digest = hashlib.sha256()
    consumed = _consume_regular_file(
        path,
        max_bytes,
        digest.update,
        deadline_seconds,
        monotonic,
        deadline,
    )
    if consumed is None:
        return None
    result = digest.hexdigest()
    return (result, consumed) if include_size else result


def _read_regular_file(path: Path, max_bytes: int) -> Optional[bytes]:
    payload = bytearray()
    if _consume_regular_file(path, max_bytes, payload.extend) is None:
        return None
    return bytes(payload)


def _interpreter_integrity(root: Path) -> Optional[dict]:
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        resolved = python.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
        target_stat = resolved.stat()
        if not resolved.is_file() or target_stat.st_size > _MAX_INTERPRETER_BYTES:
            return None
        try:
            target_path = resolved.relative_to(root_resolved).as_posix()
            target_scope = "managed"
        except ValueError:
            try:
                target_path = resolved.relative_to(root.parent.parent.resolve(strict=True)).as_posix()
                target_scope = "profile"
            except ValueError:
                target_path = str(resolved)
                target_scope = "external"
        target_digest = _sha256_file(resolved, _MAX_INTERPRETER_BYTES)
        if target_digest is None:
            return None
        return {
            "path": python.relative_to(root).as_posix(),
            "target_scope": target_scope,
            "target_path": target_path,
            "executable": os.name == "nt" or os.access(resolved, os.X_OK),
            "mode": target_stat.st_mode,
            "size": target_stat.st_size,
            "mtime_ns": target_stat.st_mtime_ns,
            "sha256": target_digest,
        }
    except (OSError, RuntimeError, ValueError):
        return None


def _sha256_environment(
    root: Path,
    deadline_seconds: float = _ENVIRONMENT_HASH_TIMEOUT_SECONDS,
    monotonic=None,
) -> Optional[str]:
    if not root.is_dir() or root.is_symlink():
        return None
    monotonic = monotonic or time.monotonic
    deadline = monotonic() + deadline_seconds
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    total_path_bytes = 0
    paths = []
    try:
        for path in root.rglob("*"):
            if monotonic() >= deadline:
                return None
            if len(paths) >= _MAX_ENVIRONMENT_ENTRIES:
                return None
            relative_path = path.relative_to(root)
            relative = os.fsencode(relative_path.as_posix())
            total_path_bytes += len(relative)
            if total_path_bytes > _MAX_ENVIRONMENT_PATH_BYTES:
                return None
            paths.append((path, relative_path, relative))
    except (OSError, UnicodeError):
        return None
    for path, relative_path, relative in sorted(paths, key=lambda item: item[0]):
        if monotonic() >= deadline:
            return None
        if path.suffix == ".pyc" or "__pycache__" in relative_path.parts:
            return None
        if path.is_dir() and not path.is_symlink():
            directory_stat = path.lstat()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(b"D")
            digest.update(directory_stat.st_mode.to_bytes(8, "big"))
            digest.update(directory_stat.st_mtime_ns.to_bytes(16, "big", signed=True))
            file_count += 1
            continue
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        if path.is_symlink():
            target = os.fsencode(os.readlink(path))
            total_bytes += len(target)
            if total_bytes > _MAX_ENVIRONMENT_BYTES:
                return None
            digest.update(b"L")
            digest.update(len(target).to_bytes(4, "big"))
            digest.update(target)
        elif path.is_file():
            file_result = _sha256_file(
                path,
                _MAX_ENVIRONMENT_BYTES - total_bytes,
                monotonic=monotonic,
                deadline=deadline,
                include_size=True,
            )
            if file_result is None:
                return None
            file_digest, consumed = file_result
            total_bytes += consumed
            digest.update(b"F")
            digest.update(bytes.fromhex(file_digest))
        else:
            return None
        file_count += 1
    return digest.hexdigest() if file_count else None


def _metadata_platform_name() -> str:
    return os.name


def _windows_change_time(path: Path) -> Optional[int]:
    try:
        import ctypes
        from ctypes import wintypes

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_file_info = kernel32.GetFileInformationByHandleEx
        get_file_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        get_file_info.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(path),
            0x80,
            0x7,
            None,
            3,
            0x02200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            return None
        try:
            info = FileBasicInfo()
            if not get_file_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            token = int(info.ChangeTime)
            return token if token > 0 else None
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _entry_change_token(path: Path, stat) -> Optional[int]:
    if _metadata_platform_name() == "nt":
        return _windows_change_time(path)
    return stat.st_ctime_ns


def _interpreter_state_fingerprint(root: Path):
    integrity = _interpreter_integrity(root)
    if integrity is None:
        return None
    python = root / integrity["path"]
    try:
        resolved = python.resolve(strict=True)
        target_stat = resolved.stat()
        change_token = _entry_change_token(resolved, target_stat)
        if change_token is None:
            return None
        return (
            integrity["path"],
            integrity["target_scope"],
            integrity["target_path"],
            integrity["executable"],
            integrity["mode"],
            integrity["size"],
            integrity["mtime_ns"],
            integrity["sha256"],
            change_token,
        )
    except (OSError, RuntimeError, ValueError):
        return None


def _environment_state_fingerprint(root: Path) -> Optional[str]:
    if not root.is_dir() or root.is_symlink():
        return None
    records = []
    pending = []
    visited = 0
    total_bytes = 0
    total_path_bytes = 0
    try:
        pending.append(os.scandir(root))
        while pending:
            try:
                entry = next(pending[-1])
            except StopIteration:
                pending.pop().close()
                continue
            visited += 1
            if visited > _MAX_ENVIRONMENT_ENTRIES:
                return None
            path = Path(entry.path)
            relative_path = path.relative_to(root)
            relative = os.fsencode(relative_path.as_posix())
            total_path_bytes += len(relative)
            if total_path_bytes > _MAX_ENVIRONMENT_PATH_BYTES:
                return None
            if path.suffix == ".pyc" or "__pycache__" in relative_path.parts:
                return None
            stat = entry.stat(follow_symlinks=False)
            change_token = _entry_change_token(path, stat)
            if change_token is None:
                return None
            target = os.fsencode(os.readlink(path)) if entry.is_symlink() else None
            if target is not None:
                total_bytes += len(target)
            elif not entry.is_dir(follow_symlinks=False):
                total_bytes += stat.st_size
            if total_bytes > _MAX_ENVIRONMENT_BYTES:
                return None
            records.append((relative_path.as_posix(), relative, stat, target, change_token))
            if entry.is_dir(follow_symlinks=False):
                pending.append(os.scandir(path))
    except (OSError, UnicodeError):
        return None
    finally:
        for iterator in pending:
            iterator.close()
    digest = hashlib.sha256()
    for _, relative, stat, target, change_token in sorted(records, key=lambda record: record[0]):
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(stat.st_mode.to_bytes(8, "big"))
        digest.update(stat.st_size.to_bytes(8, "big"))
        digest.update(stat.st_mtime_ns.to_bytes(16, "big", signed=True))
        digest.update(change_token.to_bytes(16, "big", signed=True))
        if target is not None:
            digest.update(len(target).to_bytes(4, "big"))
            digest.update(target)
    return digest.hexdigest() if records else None


_verified_environment_sha256_cache = {}
_integrity_retry_generation = 0
_browser_use_state_fingerprint_candidate = None
_browser_use_state_fingerprint_cache = None
_browser_use_state_fingerprint_cache_deadline = 0.0
_browser_use_state_fingerprint_cache_generation = None
_browser_use_integrity_rejection_state = None
_browser_use_integrity_cache_lock = threading.RLock()


def _invalidate_browser_use_integrity_caches(
    force_generation: bool = False,
    rejection_state=None,
) -> None:
    global _integrity_retry_generation
    global _browser_use_state_fingerprint_candidate
    global _browser_use_state_fingerprint_cache
    global _browser_use_state_fingerprint_cache_deadline
    global _browser_use_state_fingerprint_cache_generation
    global _browser_use_integrity_rejection_state

    with _browser_use_integrity_cache_lock:
        stable_rejection = (
            rejection_state is not None
            and rejection_state == _browser_use_integrity_rejection_state
        )
        _browser_use_integrity_rejection_state = rejection_state
        if stable_rejection and not force_generation:
            return

        cached_state = (
            _browser_use_state_fingerprint_candidate is not None
            or _browser_use_state_fingerprint_cache is not None
        )
        if force_generation or cached_state:
            _integrity_retry_generation += 1
        _browser_use_state_fingerprint_candidate = None
        _browser_use_state_fingerprint_cache = None
        _browser_use_state_fingerprint_cache_deadline = 0.0
        _browser_use_state_fingerprint_cache_generation = None


def _verified_environment_sha256(root: str, state_fingerprint: str) -> Optional[str]:
    key = (root, state_fingerprint)
    with _browser_use_integrity_cache_lock:
        cached = _verified_environment_sha256_cache.get(key)
    if cached is not None:
        return cached
    try:
        digest = _sha256_environment(Path(root))
    except OSError:
        _invalidate_browser_use_integrity_caches(force_generation=True)
        return None
    if digest is None:
        _invalidate_browser_use_integrity_caches(force_generation=True)
        return None
    if _environment_state_fingerprint(Path(root)) != state_fingerprint:
        _invalidate_browser_use_integrity_caches(force_generation=True)
        return None
    with _browser_use_integrity_cache_lock:
        cached = _verified_environment_sha256_cache.get(key)
        if cached is not None:
            return cached
        if len(_verified_environment_sha256_cache) >= 8:
            oldest = next(iter(_verified_environment_sha256_cache))
            _verified_environment_sha256_cache.pop(oldest)
        _verified_environment_sha256_cache[key] = digest
        return digest


def _clear_verified_environment_sha256_cache() -> None:
    with _browser_use_integrity_cache_lock:
        _verified_environment_sha256_cache.clear()


_verified_environment_sha256.cache_clear = _clear_verified_environment_sha256_cache


def _browser_use_file_identity(path: Path, max_bytes: int):
    try:
        file_stat = path.stat()
    except OSError:
        return None
    digest = _sha256_file(path, max_bytes)
    return (
        file_stat.st_mode,
        file_stat.st_mtime_ns,
        file_stat.st_size,
        digest if file_stat.st_size <= max_bytes else None,
    )


def browser_use_cli_state_fingerprint():
    global _browser_use_state_fingerprint_candidate
    global _browser_use_state_fingerprint_cache
    global _browser_use_state_fingerprint_cache_deadline
    global _browser_use_state_fingerprint_cache_generation

    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    receipt = home / "state" / _MANAGED_RECEIPT
    cli = home / "tools" / f"browser-use-{_PINNED_BROWSER_USE_VERSION}" / (
        "Scripts/browser-use.exe" if os.name == "nt" else "bin/browser-use"
    )
    managed_root = cli.parent.parent
    cheap_identity = (
        _browser_use_file_identity(receipt, _MAX_RECEIPT_BYTES),
        _browser_use_file_identity(cli, _MAX_CLI_BYTES),
    )
    with _browser_use_integrity_cache_lock:
        generation = _integrity_retry_generation
        cached_fingerprint = _browser_use_state_fingerprint_cache
        if (
            cached_fingerprint is not None
            and tuple(cached_fingerprint[:2]) == cheap_identity
            and _browser_use_state_fingerprint_cache_generation == generation
            and time.monotonic() < _browser_use_state_fingerprint_cache_deadline
        ):
            return cached_fingerprint

    values = list(cheap_identity)
    values.append(_interpreter_state_fingerprint(managed_root))
    values.append(_environment_state_fingerprint(managed_root))
    values.append(generation)
    fingerprint = tuple(values)
    with _browser_use_integrity_cache_lock:
        if generation != _integrity_retry_generation:
            return fingerprint
        cacheable = (
            all(value is not None for value in fingerprint[:4])
            or _browser_use_integrity_rejection_state is not None
        )
        if cacheable and _browser_use_state_fingerprint_candidate == fingerprint:
            _browser_use_state_fingerprint_cache = fingerprint
            _browser_use_state_fingerprint_cache_deadline = (
                time.monotonic() + _STATE_FINGERPRINT_TTL_SECONDS
            )
            _browser_use_state_fingerprint_cache_generation = generation
        else:
            _browser_use_state_fingerprint_cache = None
            _browser_use_state_fingerprint_cache_deadline = 0.0
            _browser_use_state_fingerprint_cache_generation = None
        _browser_use_state_fingerprint_candidate = fingerprint if cacheable else None
    return fingerprint


def _find_cli() -> Optional[List[str]]:
    """Resolve only Golden's exact, receipt-bound Browser Use executable."""
    global _browser_use_integrity_rejection_state

    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        receipt_path = home / "state" / _MANAGED_RECEIPT
        receipt_bytes = _read_regular_file(receipt_path, _MAX_RECEIPT_BYTES)
        if receipt_bytes is None:
            receipt_identity = _browser_use_file_identity(
                receipt_path, _MAX_RECEIPT_BYTES
            )
            if receipt_identity is not None and receipt_identity[-1] is not None:
                _invalidate_browser_use_integrity_caches(force_generation=True)
            else:
                _invalidate_browser_use_integrity_caches(
                    rejection_state=("unreadable-receipt", receipt_identity)
                )
            return None
        try:
            receipt = json.loads(receipt_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _invalidate_browser_use_integrity_caches(
                rejection_state=(
                    "invalid-receipt-json",
                    hashlib.sha256(receipt_bytes).hexdigest(),
                )
            )
            return None
        if not isinstance(receipt, dict):
            _invalidate_browser_use_integrity_caches(
                rejection_state=(
                    "invalid-receipt-type",
                    hashlib.sha256(receipt_bytes).hexdigest(),
                )
            )
            return None
        release = receipt.get("release")
        cli_relative = str(receipt.get("cli_path") or "")
        profile_root = "state/browser-use/browser-profile"
        managed_root = home / "tools" / f"browser-use-{_PINNED_BROWSER_USE_VERSION}"
        managed_cli = managed_root / ("Scripts/browser-use.exe" if os.name == "nt" else "bin/browser-use")
        managed_python = managed_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cli = home / cli_relative
        interpreter = _interpreter_integrity(managed_root)
        environment_state = _environment_state_fingerprint(managed_root)
        cli_sha256 = _sha256_file(cli, _MAX_CLI_BYTES)
        if (
            receipt.get("schema_version") != 2
            or receipt.get("kind") != "browser_use_cli_install_receipt"
            or not isinstance(release, dict)
            or release.get("version") != _PINNED_BROWSER_USE_VERSION
            or release.get("harness_version") != _PINNED_BROWSER_HARNESS_VERSION
            or receipt.get("artifact_sha256") != _PINNED_ARTIFACT_SHA256
            or receipt.get("lock_sha256") != _PINNED_LOCK_SHA256
            or receipt.get("profile_root") != profile_root
            or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("profile_id") or ""))
            or cli_relative != managed_cli.relative_to(home).as_posix()
            or cli != managed_cli
            or not cli.is_file()
            or cli.stat().st_size > _MAX_CLI_BYTES
            or not managed_python.is_file()
            or (os.name != "nt" and not os.access(cli, os.X_OK))
            or (os.name != "nt" and not os.access(managed_python, os.X_OK))
            or cli_sha256 != receipt.get("cli_sha256")
            or interpreter is None
            or interpreter != receipt.get("interpreter")
            or environment_state is None
            or _verified_environment_sha256(str(managed_root), environment_state)
            != receipt.get("environment_sha256")
        ):
            _invalidate_browser_use_integrity_caches(
                rejection_state=(
                    "invalid-managed-environment",
                    hashlib.sha256(receipt_bytes).hexdigest(),
                    cli_sha256,
                    json.dumps(interpreter, sort_keys=True, default=str),
                    environment_state,
                )
            )
            return None
        with _browser_use_integrity_cache_lock:
            _browser_use_integrity_rejection_state = None
        if os.name == "nt":
            return [str(cli)]
        return [str(managed_python), "-I", "-B", str(cli)]
    except (OSError, ValueError, TypeError):
        _invalidate_browser_use_integrity_caches(force_generation=True)
    return None
'''

INSTALL_REPLACEMENT = '''def install_cli(timeout_s: int = 600):
    """Accept only Golden's receipt-bound Browser Use installation.

    Upstream's interactive installer intentionally tracks the newest package.
    Golden fleet runtimes instead install an exact, hash-pinned release through
    the host-artifact transaction, so this runtime entry point must not create
    an unreceipted environment.
    """
    del timeout_s
    managed = _find_cli()
    if managed:
        return True, f"Golden managed browser-use CLI is ready ({managed[-1]})"
    return False, (
        "Golden's managed Browser Use CLI is unavailable. Run the Golden host "
        "artifact installer so the pinned release and receipt are installed."
    )
'''

INSTALL_SH_MARKER = "Golden's exact host-artifact installer"
INSTALL_SH_ANCHOR = '''install_browser_use_cli() {
    # The Browser Use CLI is the default browser backend when it is runnable
    # (tools/browser_use_cli.py). Provision it here so fresh installs don't
    # silently fall back to the built-in browser tools. Best-effort: any
    # failure is non-fatal because browser_exec can still run via uvx and
    # `hermes tools` can install it later.
    if [ "$SKIP_BROWSER" = true ]; then
        log_info "Skipping Browser Use CLI install (--skip-browser)"
        return 0
    fi
    if [ "$DISTRO" = "termux" ]; then
        return 0
    fi
    if [ -z "$UV_CMD" ]; then
        log_info "Skipping Browser Use CLI install (uv unavailable)"
        return 0
    fi
    if command -v browser-use >/dev/null 2>&1 || [ -x "$HERMES_HOME/bin/browser-use" ]; then
        log_success "Browser Use CLI already installed"
        return 0
    fi

    log_info "Installing Browser Use CLI (default browser backend)..."
    # UV_TOOL_BIN_DIR keeps the binary inside Hermes' managed bin dir, where
    # the browser tool resolves it — no reliance on the user's PATH.
    if run_with_timeout 600 env UV_NO_CONFIG=1 UV_TOOL_BIN_DIR="$HERMES_HOME/bin" \\
        "$UV_CMD" tool install browser-use >/dev/null 2>&1; then
        log_success "Browser Use CLI installed"
    else
        log_warn "Browser Use CLI install failed — browser automation falls back to built-in tools."
        log_info "Install later with: $UV_CMD tool install browser-use  (or via 'hermes tools')"
    fi
}
'''
INSTALL_SH_REPLACEMENT = '''install_browser_use_cli() {
    if [ "$SKIP_BROWSER" = true ]; then
        log_info "Skipping Browser Use CLI install (--skip-browser)"
        return 0
    fi
    if [ "$DISTRO" = "termux" ]; then
        return 0
    fi
    log_info "Browser Use CLI provisioning is owned by Golden's exact host-artifact installer."
}
'''

INSTALL_PS1_MARKER = "Golden's exact host-artifact installer"
INSTALL_PS1_ANCHOR = '''# The Browser Use CLI is the default browser backend when it is runnable
# (tools/browser_use_cli.py). Provision it at install time so fresh installs
# don't silently fall back to the built-in browser tools. Best-effort: any
# failure is non-fatal (browser_exec can still run via uvx, and `hermes tools`
# can install it later).
function Install-BrowserUseCli {
    if (-not $script:UvCmd) { Resolve-UvCmd }
    if (-not $script:UvCmd) {
        Write-Info "Skipping Browser Use CLI install (uv unavailable)"
        return
    }
    $managedBin = Join-Path $HermesHome "bin"
    $managedBu = Join-Path $managedBin "browser-use.exe"
    if ((Get-Command browser-use -ErrorAction SilentlyContinue) -or (Test-Path $managedBu)) {
        Write-Success "Browser Use CLI already installed"
        return
    }

    Write-Info "Installing Browser Use CLI (default browser backend)..."
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # UV_TOOL_BIN_DIR keeps the binary inside Hermes' managed bin dir,
        # where the browser tool resolves it -- no reliance on the user PATH.
        $env:UV_TOOL_BIN_DIR = $managedBin
        $env:UV_NO_CONFIG = "1"
        & $script:UvCmd tool install browser-use 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Success "Browser Use CLI installed"
        } else {
            Write-Warn "Browser Use CLI install failed (exit $LASTEXITCODE) -- browser automation falls back to built-in tools."
            Write-Info "Install later with: uv tool install browser-use  (or via 'hermes tools')"
        }
    } catch {
        Write-Warn "Browser Use CLI install failed: $_"
    } finally {
        $ErrorActionPreference = $prevEAP
        Remove-Item Env:\\UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue
        Remove-Item Env:\\UV_NO_CONFIG -ErrorAction SilentlyContinue
    }
}
'''  # noqa: E501
INSTALL_PS1_REPLACEMENT = '''function Install-BrowserUseCli {
    Write-Info "Browser Use CLI provisioning is owned by Golden's exact host-artifact installer."
}
'''

TOOLS_CONFIG_MARKER = "Golden's managed Browser Use host artifact installer"
TOOLS_CONFIG_ANCHOR = '''    elif post_setup_key == "browser_use_cli":
        if shutil.which("browser-use"):
            _print_success("    browser-use CLI found on PATH")
        else:
            _print_info("    Installing browser-use CLI (uv tool install browser-use)...")
            try:
                from tools.browser_use_cli import install_cli

                ok, message = install_cli()
            except Exception as exc:  # pragma: no cover — defensive
                ok, message = False, f"install failed: {exc}"
            if ok:
                _print_success(f"    {message}")
            else:
                for line in str(message).splitlines():
                    _print_warning(f"    {line[:200]}")
                if shutil.which("uvx"):
                    _print_info("    Falling back to zero-install runs via `uvx browser-use`")
                else:
                    _print_info("    Install manually: uv tool install browser-use  (https://docs.astral.sh/uv/)")
        _print_info("    Local Chrome needs remote debugging: chrome://inspect/#remote-debugging")
        _print_info("    Cloud browsers: browser-use auth login  (or set BROWSER_USE_API_KEY)")
'''
TOOLS_CONFIG_REPLACEMENT = '''    elif post_setup_key == "browser_use_cli":
        try:
            from tools.browser_use_cli import install_cli

            ok, message = install_cli()
        except Exception as exc:  # pragma: no cover — defensive
            ok, message = False, f"managed Browser Use check failed: {exc}"
        if ok:
            _print_success(f"    {message}")
        else:
            for line in str(message).splitlines():
                _print_warning(f"    {line[:200]}")
            _print_info("    Golden's managed Browser Use host artifact installer owns provisioning.")
        _print_info("    Local Chrome needs remote debugging: chrome://inspect/#remote-debugging")
        _print_info("    Cloud browsers: browser-use auth login  (or set BROWSER_USE_API_KEY)")
'''

MODEL_CACHE_ANCHOR = """    cache_key = None
    if quiet_mode:
        try:
            from hermes_cli.config import get_config_path
            cfg_path = get_config_path()
            cfg_stat = cfg_path.stat()
            cfg_fp = (cfg_stat.st_mtime_ns, cfg_stat.st_size)
        except (FileNotFoundError, OSError, ImportError):
            cfg_fp = None
        profile_scope = check_fn_cache_scope()
        if profile_scope != CHECK_FN_CACHE_BYPASS:
            cache_key = (
"""
MODEL_CACHE_REPLACEMENT = """    try:
        from tools.browser_use_cli import browser_use_cli_state_fingerprint
        browser_use_state = browser_use_cli_state_fingerprint()
    except (ImportError, OSError):
        browser_use_state = None
    previous_browser_use_state = getattr(
        get_tool_definitions, "_browser_use_state", object()
    )
    if previous_browser_use_state != browser_use_state:
        from tools.registry import invalidate_check_fn_cache
        invalidate_check_fn_cache()
        _clear_tool_defs_cache()
        get_tool_definitions._browser_use_state = browser_use_state
    cache_key = None
    if quiet_mode:
        try:
            from hermes_cli.config import get_config_path
            cfg_path = get_config_path()
            cfg_stat = cfg_path.stat()
            cfg_fp = (cfg_stat.st_mtime_ns, cfg_stat.st_size)
        except (FileNotFoundError, OSError, ImportError):
            cfg_fp = None
        profile_scope = check_fn_cache_scope()
        if profile_scope != CHECK_FN_CACHE_BYPASS:
            cache_key = (
                browser_use_state,
"""

EXEC_ANCHOR = """        proc = subprocess.run(
            cmd,
"""
EXEC_REPLACEMENT = """        proc = _run_managed_cli(
            cmd,
"""

SESSION_ANCHOR = """        env["BU_NAME"] = session
"""
SESSION_REPLACEMENT = """        env["BU_NAME"] = _profile_session_name(session)
"""

WORKSPACE_ANCHOR = """    existing = os.environ.get("BH_AGENT_WORKSPACE")
    if existing:
        return existing
"""
WORKSPACE_REPLACEMENT = """"""

MISSING_ANCHOR = """        return tool_error(
            "browser-use CLI not found on PATH, and uvx is unavailable for a "
            "zero-install run. Install it with `uv tool install browser-use` "
            "(or `pipx install browser-use`), then run `browser-use --doctor` "
            "to verify the setup."
        )
"""
MISSING_REPLACEMENT = """        return tool_error(
            "Golden's managed browser-use CLI 0.13.7 is unavailable or its "
            "receipt does not match the executable. Run the Browser Use host "
            "artifact installer before enabling browser.backend=browser-use."
        )
"""

SCHEMA_ANCHOR = """    # Static fallback, used only when the CLI (and uvx) is unavailable
    "description": (
        _HEADER_BASE
        + _HELPERS_DIGEST
        + "\\n\\n(The browser-use CLI is not installed yet. Install it with "
        "`uv tool install browser-use`.)"
    ),
"""
SCHEMA_REPLACEMENT = """    # Static fallback when the managed CLI receipt is unavailable.
    "description": (
        _HEADER_BASE
        + _HELPERS_DIGEST
        + "\\n\\n(Golden's managed browser-use CLI 0.13.7 is not installed "
        "or its receipt is invalid.)"
    ),
"""

FIND_TESTS_ANCHOR = '''class TestFindCli:
    """The tests/tools conftest pins _find_cli to None (host isolation);
    exercise the real function via the preserved _find_cli_unpatched."""

    def test_prefers_installed_binary(self, monkeypatch):
        monkeypatch.setattr(
            bu_cli.shutil, "which",
            lambda name: "/usr/local/bin/browser-use" if name == "browser-use" else "/usr/local/bin/uvx",
        )
        assert bu_cli._find_cli_unpatched() == ["/usr/local/bin/browser-use"]

    def test_falls_back_to_uvx(self, monkeypatch):
        monkeypatch.setattr(
            bu_cli.shutil, "which",
            lambda name: "/usr/local/bin/uvx" if name == "uvx" else None,
        )
        assert bu_cli._find_cli_unpatched() == ["/usr/local/bin/uvx", "browser-use"]

    def test_none_when_neither_available(self, monkeypatch):
        monkeypatch.setattr(bu_cli.shutil, "which", lambda name: None)
        assert bu_cli._find_cli_unpatched() is None
'''
FIND_TESTS_REPLACEMENT = """class TestFindCli:
    @staticmethod
    def _receipt(tmp_path, *, version="0.13.7", digest=None, environment_digest=None):
        managed = tmp_path / "tools" / "browser-use-0.13.7"
        cli = managed / ("Scripts/browser-use.exe" if os.name == "nt" else "bin/browser-use")
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\\n", encoding="utf-8")
        python = managed / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.write_text("#!/bin/sh\\n", encoding="utf-8")
        if os.name != "nt":
            cli.chmod(0o755)
            python.chmod(0o755)
        site = cli.parent.parent / ("Lib/site-packages" if os.name == "nt" else "lib/python3.12/site-packages")
        for package in ("browser_use", "browser_harness"):
            package_dir = site / package
            package_dir.mkdir(parents=True)
            (package_dir / "__init__.py").write_text(f"{package}\\n")
            dist_info = site / f"{package}-1.dist-info"
            dist_info.mkdir()
            (dist_info / "METADATA").write_text(package)
        receipt = {
            "schema_version": 2,
            "kind": "browser_use_cli_install_receipt",
            "release": {"version": version, "harness_version": "0.1.8"},
            "artifact_sha256": bu_cli._PINNED_ARTIFACT_SHA256,
            "lock_sha256": bu_cli._PINNED_LOCK_SHA256,
            "profile_id": "1" * 32,
            "cli_path": cli.relative_to(tmp_path).as_posix(),
            "cli_sha256": digest or bu_cli._sha256_file(cli, bu_cli._MAX_CLI_BYTES),
            "environment_sha256": environment_digest or bu_cli._sha256_environment(managed),
            "interpreter": bu_cli._interpreter_integrity(managed),
            "profile_root": "state/browser-use/browser-profile",
        }
        state = tmp_path / "state"
        state.mkdir()
        (state / bu_cli._MANAGED_RECEIPT).write_text(json.dumps(receipt))
        return cli

    def test_accepts_exact_receipt_bound_binary(self, tmp_path, monkeypatch):
        cli = self._receipt(tmp_path)
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        expected = (
            [str(cli)]
            if os.name == "nt"
            else [str(cli.parent / "python"), "-I", "-B", str(cli)]
        )
        assert bu_cli._find_cli_unpatched() == expected

    def test_rejects_wrong_version(self, tmp_path, monkeypatch):
        self._receipt(tmp_path, version="0.13.6")
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        assert bu_cli._find_cli_unpatched() is None

    def test_rejects_executable_digest_drift(self, tmp_path, monkeypatch):
        self._receipt(tmp_path, digest="0" * 64)
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        assert bu_cli._find_cli_unpatched() is None

    def test_rejects_installed_payload_drift(self, tmp_path, monkeypatch):
        self._receipt(tmp_path, environment_digest="0" * 64)
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        assert bu_cli._find_cli_unpatched() is None
"""

FIND_MANAGED_BIN_TESTS_REPLACEMENT = """class TestFindCliManagedBin:
    def test_unreceipted_managed_binary_is_rejected(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "home" / "bin"
        bin_dir.mkdir(parents=True)
        binary = bin_dir / "browser-use"
        binary.write_text("#!/bin/sh\\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        assert bu_cli._find_cli_unpatched() is None
"""

INSTALL_TESTS_REPLACEMENT = """class TestInstallCli:
    def test_accepts_receipt_bound_install(self, monkeypatch):
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/managed/python", "/managed/browser-use"])
        ok, message = bu_cli.install_cli()
        assert ok is True
        assert "Golden managed" in message

    def test_refuses_unpinned_runtime_install(self, monkeypatch):
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        ok, message = bu_cli.install_cli()
        assert ok is False
        assert "host artifact installer" in message
"""

NOTICE_HINT_ANCHOR = """            "Run `hermes tools` (Browser Automation → Browser Use) to install it, "
            "or `browser.backend: off` in config.yaml to silence this."
"""
NOTICE_HINT_REPLACEMENT = """            "Run Golden's managed Browser Use host artifact installer, or set "
            "`browser.backend: off` in config.yaml to silence this."
"""

NOTICE_TEST_ANCHOR = '        assert "hermes tools" in notice\n'
NOTICE_TEST_REPLACEMENT = '        assert "managed Browser Use" in notice\n'
SESSION_TEST_ANCHOR = '        assert "bu:r7k2" in result["output"]\n'
SESSION_TEST_REPLACEMENT = (
    '        assert result["output"].strip().endswith("_r7k2")\n'
)

STATIC_HINT_TEST_ANCHOR = """        assert "uv tool install browser-use" in desc
"""
STATIC_HINT_TEST_REPLACEMENT = """        assert "managed browser-use CLI 0.13.7" in desc
"""

MODE_TEST_ANCHOR = """    def test_config_opt_in(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"backend": "browser-use"}},
        )
        assert bu_cli.is_browser_use_cli_mode() is True
"""
MODE_TEST_REPLACEMENT = """    def test_config_opt_in(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"backend": "browser-use"}},
        )
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/managed/browser-use"])
        assert bu_cli.is_browser_use_cli_mode() is True

    def test_config_opt_in_falls_back_without_managed_cli(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"backend": "browser-use"}},
        )
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        assert bu_cli.is_browser_use_cli_mode() is False
"""

LEGACY_DIRECT_TEST_ANCHOR = """    def test_direct_api_config_migrates(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: self._LEGACY)
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert bu_cli.is_browser_use_cli_mode() is True
"""
LEGACY_DIRECT_TEST_REPLACEMENT = """    def test_direct_api_config_waits_for_managed_cli(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: self._LEGACY)
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert bu_cli.is_browser_use_cli_mode() is False
"""

LEGACY_AUTO_TEST_ANCHOR = '''    def test_auto_detect_with_key_migrates(self, monkeypatch):
        """No cloud_provider configured + BROWSER_USE_API_KEY set: credential
        auto-detection prefers Browser Use (even when Browserbase creds are
        also present), which now means Browser Use mode."""
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-key")
        monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "bb-project")
        assert bu_cli.is_browser_use_cli_mode() is True
'''
LEGACY_AUTO_TEST_REPLACEMENT = '''    def test_auto_detect_with_key_waits_for_managed_cli(self, monkeypatch):
        """Ambient cloud credentials cannot bypass the managed CLI gate."""
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-key")
        monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "bb-project")
        assert bu_cli.is_browser_use_cli_mode() is False
'''

MISSING_HINT_TEST_ANCHOR = """        assert "uv tool install browser-use" in result["error"]
"""
MISSING_HINT_TEST_REPLACEMENT = """        assert "managed browser-use CLI 0.13.7" in result["error"]
"""

TIMEOUT_DECORATOR_ANCHOR = """    def test_timeout_returns_actionable_error(self, tmp_path, monkeypatch):
"""
TIMEOUT_DECORATOR_REPLACEMENT = """    @pytest.mark.live_system_guard_bypass
    def test_timeout_returns_actionable_error(self, tmp_path, monkeypatch):
"""

TIMEOUT_TEST_ANCHOR = """        cli = _fake_cli(tmp_path, "cat > /dev/null\\nsleep 30\\n")
"""
TIMEOUT_TEST_REPLACEMENT = """        # HERMES_NATIVE_BROWSER_USE_SAFETY_v1: replace the shell with the
        # sleeper so subprocess.run owns and terminates the exact timeout PID.
        cli = _fake_cli(tmp_path, "cat > /dev/null\\nexec sleep 30\\n")
"""


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"native Browser Use {label} anchor drift")
    if source.count(old) != 1:
        raise RuntimeError(f"native Browser Use {label} anchor is ambiguous")
    return source.replace(old, new, 1)


def _replace_named_node(
    source: str,
    name: str,
    replacement: str,
    label: str,
    *,
    required: bool = True,
) -> str:
    tree = ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == name
    ]
    if not matches:
        if required:
            raise RuntimeError(f"native Browser Use {label} anchor drift")
        return source
    if len(matches) != 1:
        raise RuntimeError(f"native Browser Use {label} anchor is ambiguous")
    node = matches[0]
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    replacement_text = replacement.rstrip() + "\n\n"
    return "".join(lines[:start]) + replacement_text + "".join(lines[end:])


def patch_native_browser_use_safety_v1(root: Path) -> bool:
    root = Path(root)
    target = root / TARGET
    test_target = root / TEST_TARGET
    model_tools_target = root / MODEL_TOOLS_TARGET
    install_sh_target = root / INSTALL_SH_TARGET
    install_ps1_target = root / INSTALL_PS1_TARGET
    tools_config_target = root / TOOLS_CONFIG_TARGET
    source = target.read_text(encoding="utf-8")
    test_source = test_target.read_text(encoding="utf-8")
    model_tools_source = model_tools_target.read_text(encoding="utf-8")
    install_sh_source = install_sh_target.read_text(encoding="utf-8")
    install_ps1_source = install_ps1_target.read_text(encoding="utf-8")
    tools_config_source = tools_config_target.read_text(encoding="utf-8")
    if MARKER in source and REVISION_MARKER not in source:
        raise RuntimeError("native Browser Use stale patch revision requires a clean candidate rebuild")
    if (
        REVISION_MARKER in source
        and MARKER in test_source
        and "browser_use_state" in model_tools_source
        and INSTALL_SH_MARKER in install_sh_source
        and INSTALL_PS1_MARKER in install_ps1_source
        and TOOLS_CONFIG_MARKER in tools_config_source
    ):
        return False

    patched = source
    if MARKER not in patched:
        patched = _replace_once(patched, IMPORT_ANCHOR, IMPORT_REPLACEMENT, "import")
        if "from pathlib import Path\n" not in patched:
            patched = _replace_once(
                patched,
                TYPING_IMPORT_ANCHOR,
                TYPING_IMPORT_REPLACEMENT,
                "Path import",
            )
        patched = _replace_once(
            patched,
            SHUTIL_IMPORT_ANCHOR,
            SHUTIL_IMPORT_REPLACEMENT,
            "managed subprocess import",
        )
        patched = _replace_once(patched, CONSTANT_ANCHOR, CONSTANT_REPLACEMENT, "constants")
        patched = _replace_once(patched, MODE_ANCHOR, MODE_REPLACEMENT, "mode gating")
        patched = _replace_once(patched, ENV_ANCHOR, ENV_REPLACEMENT, "environment")
        patched = _replace_once(patched, SESSION_ANCHOR, SESSION_REPLACEMENT, "session namespace")
        patched = _replace_named_node(
            patched, "_find_cli", FIND_REPLACEMENT, "CLI resolver"
        )
        patched = _replace_named_node(
            patched,
            "install_cli",
            INSTALL_REPLACEMENT,
            "unpinned installer",
            required=False,
        )
        patched = _replace_once(
            patched,
            WORKSPACE_ANCHOR,
            WORKSPACE_REPLACEMENT,
            "ambient workspace override",
        )
        patched = _replace_once(patched, MISSING_ANCHOR, MISSING_REPLACEMENT, "missing-CLI response")
        patched = _replace_once(patched, SCHEMA_ANCHOR, SCHEMA_REPLACEMENT, "static schema hint")
        patched = _replace_once(patched, EXEC_ANCHOR, EXEC_REPLACEMENT, "managed subprocess")
        if NOTICE_HINT_ANCHOR in patched:
            patched = _replace_once(
                patched,
                NOTICE_HINT_ANCHOR,
                NOTICE_HINT_REPLACEMENT,
                "downgrade notice",
            )
    patched_test = test_source
    patched_model_tools = model_tools_source
    patched_install_sh = install_sh_source
    patched_install_ps1 = install_ps1_source
    patched_tools_config = tools_config_source
    if "browser_use_state" not in patched_model_tools:
        patched_model_tools = _replace_once(
            patched_model_tools,
            MODEL_CACHE_ANCHOR,
            MODEL_CACHE_REPLACEMENT,
            "tool-definition cache state",
        )
    if MARKER not in patched_test:
        patched_test = _replace_named_node(
            patched_test,
            "TestFindCli",
            FIND_TESTS_REPLACEMENT,
            "managed receipt tests",
        )
        patched_test = _replace_named_node(
            patched_test,
            "TestFindCliManagedBin",
            FIND_MANAGED_BIN_TESTS_REPLACEMENT,
            "managed-bin tests",
            required=False,
        )
        patched_test = _replace_named_node(
            patched_test,
            "TestInstallCli",
            INSTALL_TESTS_REPLACEMENT,
            "unpinned installer tests",
            required=False,
        )
        patched_test = _replace_once(
            patched_test,
            STATIC_HINT_TEST_ANCHOR,
            STATIC_HINT_TEST_REPLACEMENT,
            "static schema hint test",
        )
        patched_test = _replace_once(
            patched_test,
            MODE_TEST_ANCHOR,
            MODE_TEST_REPLACEMENT,
            "managed mode test",
        )
        patched_test = _replace_once(
            patched_test,
            LEGACY_DIRECT_TEST_ANCHOR,
            LEGACY_DIRECT_TEST_REPLACEMENT,
            "legacy direct API managed gate test",
        )
        patched_test = _replace_once(
            patched_test,
            LEGACY_AUTO_TEST_ANCHOR,
            LEGACY_AUTO_TEST_REPLACEMENT,
            "legacy auto-detection managed gate test",
        )
        patched_test = _replace_once(
            patched_test,
            MISSING_HINT_TEST_ANCHOR,
            MISSING_HINT_TEST_REPLACEMENT,
            "missing-CLI hint test",
        )
        patched_test = _replace_once(
            patched_test,
            TIMEOUT_DECORATOR_ANCHOR,
            TIMEOUT_DECORATOR_REPLACEMENT,
            "timeout guard marker",
        )
        patched_test = _replace_once(
            patched_test,
            TIMEOUT_TEST_ANCHOR,
            TIMEOUT_TEST_REPLACEMENT,
            "timeout fixture",
        )
        if NOTICE_TEST_ANCHOR in patched_test:
            patched_test = _replace_once(
                patched_test,
                NOTICE_TEST_ANCHOR,
                NOTICE_TEST_REPLACEMENT,
                "downgrade notice test",
            )
        if SESSION_TEST_ANCHOR in patched_test:
            patched_test = patched_test.replace(
                SESSION_TEST_ANCHOR,
                SESSION_TEST_REPLACEMENT,
            )

    if INSTALL_SH_MARKER not in patched_install_sh:
        patched_install_sh = _replace_once(
            patched_install_sh,
            INSTALL_SH_ANCHOR,
            INSTALL_SH_REPLACEMENT,
            "POSIX installer ownership",
        )
    if INSTALL_PS1_MARKER not in patched_install_ps1:
        patched_install_ps1 = _replace_once(
            patched_install_ps1,
            INSTALL_PS1_ANCHOR,
            INSTALL_PS1_REPLACEMENT,
            "PowerShell installer ownership",
        )
    if TOOLS_CONFIG_MARKER not in patched_tools_config:
        patched_tools_config = _replace_once(
            patched_tools_config,
            TOOLS_CONFIG_ANCHOR,
            TOOLS_CONFIG_REPLACEMENT,
            "tools installer ownership",
        )

    ast.parse(patched)
    ast.parse(patched_test)
    ast.parse(patched_model_tools)
    ast.parse(patched_tools_config)
    for path, content in (
        (target, patched),
        (test_target, patched_test),
        (model_tools_target, patched_model_tools),
        (install_sh_target, patched_install_sh),
        (install_ps1_target, patched_install_ps1),
        (tools_config_target, patched_tools_config),
    ):
        if path.read_text(encoding="utf-8") == content:
            continue
        shutil.copy2(path, Path(str(path) + BACKUP_SUFFIX))
        path.write_text(content, encoding="utf-8")
    return True
