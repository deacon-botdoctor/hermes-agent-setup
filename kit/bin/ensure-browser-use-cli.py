#!/usr/bin/env python3
"""Ensure Golden's exact Browser Use CLI in an isolated Hermes-home venv."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "kit" / "config" / "browser-use-cli-release-v1.json"
RECEIPT_NAME = "browser-use-cli-install-v1.json"
PINNED_ARTIFACTS = {
    "browser-use": {
        "filename": "browser_use-0.13.7-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/8a/b7/6df062c7cfd2a863f18dda0d5c7f8711e7316d772feb92370311391d8a2a/browser_use-0.13.7-py3-none-any.whl",
        "sha256": "2264439e45cc7dd7fe480ca37e9eabd040c31a4e4d5e20c069ad2f60c07e3ba8",
    },
    "browser-harness": {
        "filename": "browser_harness-0.1.8-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/0a/b9/aa8ab029af34e99784758fd5e8f76c853219cff1733ea422ff161313d108/browser_harness-0.1.8-py3-none-any.whl",
        "sha256": "4bbc414007750683408a6cf4e5c87dd62c85b8628e478d5020413814fde8ae50",
    },
}
PINNED_RELEASE = {
    "package": "browser-use",
    "version": "0.13.7",
    "harness_package": "browser-harness",
    "harness_version": "0.1.8",
}
SENSITIVE_ENV_RE = re.compile(
    r"(?:^|_)(?:api_?key|auth|credential|password|secret|token)(?:_|$)",
    re.IGNORECASE,
)
INDEX_ENV = {
    "PIP_CONFIG_FILE",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_INDEX",
    "UV_INDEX_URL",
    "UV_INSECURE_HOST",
    "UV_CONFIG_FILE",
}
INHERITED_ENV_NAMES = {
    "ALL_PROXY",
    "APPDATA",
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
}
HASH_CHUNK_BYTES = 1024 * 1024
RECEIPT_MAX_BYTES = 64 * 1024
CLI_MAX_BYTES = 1024 * 1024
INTERPRETER_MAX_BYTES = 256 * 1024 * 1024
FILE_HASH_MAX_BYTES = INTERPRETER_MAX_BYTES
FILE_HASH_TIMEOUT_SECONDS = 30
ENVIRONMENT_HASH_MAX_BYTES = 4 * 1024 * 1024 * 1024
ENVIRONMENT_HASH_MAX_ENTRIES = 10000
ENVIRONMENT_HASH_MAX_PATH_BYTES = 64 * 1024 * 1024
ENVIRONMENT_HASH_TIMEOUT_SECONDS = 300
PROBE_SUBPROCESS_TIMEOUT_SECONDS = 20
VENV_TIMEOUT_SECONDS = 120
INSTALL_TIMEOUT_SECONDS = 900
MAX_INSTALL_PROBES = 3
TRANSACTION_MARGIN_SECONDS = 300
PARENT_TIMEOUT_MARGIN_SECONDS = 600
HELPER_WORST_CASE_SECONDS = (
    FILE_HASH_TIMEOUT_SECONDS
    + VENV_TIMEOUT_SECONDS
    + INSTALL_TIMEOUT_SECONDS
    + MAX_INSTALL_PROBES
    * (2 * PROBE_SUBPROCESS_TIMEOUT_SECONDS + ENVIRONMENT_HASH_TIMEOUT_SECONDS + 2 * FILE_HASH_TIMEOUT_SECONDS)
    + TRANSACTION_MARGIN_SECONDS
)
MIN_PARENT_TIMEOUT_SECONDS = HELPER_WORST_CASE_SECONDS + PARENT_TIMEOUT_MARGIN_SECONDS


class IntegrityHashLimitError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class IntegrityReadError(RuntimeError):
    code = "integrity_read_failed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid Browser Use CLI release contract")
    release = data.get("release") if isinstance(data, dict) else None
    installer = data.get("installer") if isinstance(data, dict) else None
    artifacts = data.get("artifacts") if isinstance(data, dict) else None
    privacy = data.get("privacy_environment") if isinstance(data, dict) else None
    artifact_map = {
        item.get("package"): {
            "filename": item.get("filename"),
            "url": item.get("url"),
            "sha256": item.get("sha256"),
        }
        for item in artifacts or []
        if isinstance(item, dict)
    }
    if (
        data.get("schema_version") != 1
        or not isinstance(artifacts, list)
        or len(artifacts) != len(PINNED_ARTIFACTS)
        or not isinstance(release, dict)
        or release != PINNED_RELEASE
        or not isinstance(installer, dict)
        or installer.get("method") != "uv_isolated_venv"
        or installer.get("target") != f"tools/browser-use-{release.get('version')}"
        or installer.get("lock") != "pylock.browser-use-cli-v1.toml"
        or not re.fullmatch(r"[0-9a-f]{64}", str(installer.get("lock_sha256") or ""))
        or artifact_map != PINNED_ARTIFACTS
        or privacy
        != {
            "BH_TELEMETRY": "0",
            "BROWSER_HARNESS_TELEMETRY": "0",
            "ANONYMIZED_TELEMETRY": "false",
            "BROWSER_USE_CLOUD_SYNC": "false",
        }
    ):
        raise ValueError("invalid Browser Use CLI release contract")
    lock_path = path.parent / installer["lock"]
    if not lock_path.is_file() or _sha256(lock_path) != installer["lock_sha256"]:
        raise ValueError("invalid Browser Use CLI release lock")
    data["_lock_path"] = str(lock_path)
    return data


def _venv_python(root: Path, system: str | None = None) -> Path:
    return root / ("Scripts/python.exe" if (system or platform.system()) == "Windows" else "bin/python")


def _venv_cli(root: Path, system: str | None = None) -> Path:
    return root / ("Scripts/browser-use.exe" if (system or platform.system()) == "Windows" else "bin/browser-use")


def _check_hash_deadline(
    deadline: float,
    monotonic: Callable[[], float],
    code: str,
) -> None:
    if monotonic() >= deadline:
        raise IntegrityHashLimitError(code)


def _file_identity(info, system: str | None = None) -> tuple[Any, ...]:
    mode = getattr(info, "st_mode", None)
    if (system or platform.system()) == "Windows" and isinstance(mode, int):
        # CPython's Windows stat adapters can report 0666 for an executable
        # through fstat() and 0777 for the same file through stat().  The file
        # type remains authoritative; DOS execute-bit synthesis is not identity.
        mode = stat.S_IFMT(mode)
    return (
        getattr(info, "st_dev", None),
        getattr(info, "st_ino", None),
        mode,
        getattr(info, "st_size", None),
        getattr(info, "st_mtime_ns", None),
        getattr(info, "st_ctime_ns", None),
    )


def _consume_regular_file(
    path: Path,
    *,
    max_bytes: int,
    deadline_seconds: float,
    monotonic: Callable[[], float],
    consume: Callable[[bytes], Any],
    deadline: float | None = None,
    deadline_error_code: str = "file_hash_deadline_exceeded",
    byte_limit_error_code: str = "file_hash_byte_limit_exceeded",
) -> int:
    if deadline is None:
        deadline = monotonic() + deadline_seconds
    total_bytes = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("integrity input is not a regular file")
        if before.st_size > max_bytes:
            raise IntegrityHashLimitError(byte_limit_error_code)
        while True:
            _check_hash_deadline(deadline, monotonic, deadline_error_code)
            chunk = os.read(descriptor, min(HASH_CHUNK_BYTES, max_bytes - total_bytes + 1))
            _check_hash_deadline(deadline, monotonic, deadline_error_code)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise IntegrityHashLimitError(byte_limit_error_code)
            consume(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if _file_identity(before) != _file_identity(after):
            raise OSError("integrity input changed while reading")
        if _file_identity(before) != _file_identity(current):
            raise OSError("integrity input was replaced while reading")
        return total_bytes
    finally:
        os.close(descriptor)


def _sha256(
    path: Path,
    *,
    max_bytes: int = FILE_HASH_MAX_BYTES,
    deadline_seconds: int = FILE_HASH_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    digest = hashlib.sha256()
    _consume_regular_file(
        path,
        max_bytes=max_bytes,
        deadline_seconds=deadline_seconds,
        monotonic=monotonic,
        consume=digest.update,
    )
    return digest.hexdigest()


def _environment_sha256(
    root: Path,
    *,
    max_bytes: int = ENVIRONMENT_HASH_MAX_BYTES,
    max_entries: int = ENVIRONMENT_HASH_MAX_ENTRIES,
    max_path_bytes: int = ENVIRONMENT_HASH_MAX_PATH_BYTES,
    deadline_seconds: int = ENVIRONMENT_HASH_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
    if not root.is_dir() or root.is_symlink():
        return None
    digest = hashlib.sha256()
    deadline = monotonic() + deadline_seconds
    total_bytes = 0
    total_path_bytes = 0
    file_count = 0
    paths = []
    for path in root.rglob("*"):
        _check_hash_deadline(deadline, monotonic, "environment_hash_deadline_exceeded")
        if len(paths) >= max_entries:
            raise IntegrityHashLimitError("environment_hash_entry_limit_exceeded")
        relative_path = path.relative_to(root).as_posix()
        total_path_bytes += len(os.fsencode(relative_path))
        if total_path_bytes > max_path_bytes:
            raise IntegrityHashLimitError("environment_hash_path_limit_exceeded")
        paths.append((relative_path, path))
    for relative_path_text, path in sorted(paths, key=lambda item: item[1]):
        _check_hash_deadline(deadline, monotonic, "environment_hash_deadline_exceeded")
        relative_path = Path(relative_path_text)
        if path.suffix == ".pyc" or "__pycache__" in relative_path.parts:
            return None
        relative = os.fsencode(relative_path_text)
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
            if total_bytes > max_bytes:
                raise IntegrityHashLimitError("environment_hash_byte_limit_exceeded")
            digest.update(b"L")
            digest.update(len(target).to_bytes(4, "big"))
            digest.update(target)
        elif path.is_file():
            file_digest = hashlib.sha256()
            total_bytes += _consume_regular_file(
                path,
                max_bytes=max_bytes - total_bytes,
                deadline_seconds=deadline_seconds,
                monotonic=monotonic,
                consume=file_digest.update,
                deadline=deadline,
                deadline_error_code="environment_hash_deadline_exceeded",
                byte_limit_error_code="environment_hash_byte_limit_exceeded",
            )
            digest.update(b"F")
            digest.update(file_digest.digest())
        else:
            return None
        file_count += 1
    return digest.hexdigest() if file_count else None


def _profile_root(home: Path) -> Path:
    return home / "state" / "browser-use" / "browser-profile"


def _interpreter_integrity(root: Path, system: str | None = None) -> dict[str, Any] | None:
    python = _venv_python(root, system)
    try:
        resolved = python.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
        target_stat = resolved.stat()
        if not resolved.is_file() or target_stat.st_size > INTERPRETER_MAX_BYTES:
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
        executable = (system or platform.system()) == "Windows" or os.access(resolved, os.X_OK)
        return {
            "path": python.relative_to(root).as_posix(),
            "target_scope": target_scope,
            "target_path": target_path,
            "executable": executable,
            "mode": target_stat.st_mode,
            "size": target_stat.st_size,
            "mtime_ns": target_stat.st_mtime_ns,
            "sha256": _sha256(resolved, max_bytes=INTERPRETER_MAX_BYTES),
        }
    except (OSError, RuntimeError, ValueError):
        return None


def _interpreter_receipt_is_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    path = Path(str(value.get("path") or ""))
    target_path = Path(str(value.get("target_path") or ""))
    target_scope = value.get("target_scope")
    path_valid = bool(value.get("path") and not path.is_absolute() and ".." not in path.parts)
    if target_scope == "external":
        target_valid = target_path.is_absolute()
    else:
        target_valid = bool(
            value.get("target_path") and not target_path.is_absolute() and ".." not in target_path.parts
        )
    return bool(
        target_scope in {"managed", "profile", "external"}
        and path_valid
        and target_valid
        and value.get("executable") is True
        and isinstance(value.get("mode"), int)
        and value.get("mode") >= 0
        and isinstance(value.get("size"), int)
        and value.get("size") >= 0
        and isinstance(value.get("mtime_ns"), int)
        and re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or ""))
    )


def _artifact_sha256(contract: dict[str, Any]) -> dict[str, str]:
    return {item["package"]: item["sha256"] for item in contract["artifacts"]}


def _minimal_inherited_env(source: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in source.items()
        if key.upper() in INHERITED_ENV_NAMES or key.upper().startswith("LC_")
    }


def _child_env(
    home: Path,
    privacy: dict[str, str],
    profile_id: str | None = None,
) -> dict[str, str]:
    env = _minimal_inherited_env(os.environ)
    env["HERMES_HOME"] = str(home)
    env.update(privacy)
    state_root = home / "state" / "browser-use"
    profile_root = _profile_root(home)
    env.update(
        {
            "BH_HOME": str(state_root),
            "BH_CONFIG_DIR": str(state_root / "config"),
            "BH_RUNTIME_DIR": str(state_root / "runtime"),
            "BH_TMP_DIR": str(state_root / "tmp"),
            "BH_AGENT_WORKSPACE": str(state_root / "agent-workspace"),
            "HOME": str(profile_root),
            "USERPROFILE": str(profile_root),
            "APPDATA": str(profile_root / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(profile_root / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(profile_root / ".config"),
            "XDG_CACHE_HOME": str(profile_root / ".cache"),
            "BROWSER_USE_CONFIG_DIR": str(profile_root / ".config" / "browseruse"),
            "BROWSER_USE_CONFIG_PATH": str(profile_root / ".config" / "browseruse" / "config.json"),
            "UV_CACHE_DIR": str(state_root / "uv-cache"),
            "UV_LINK_MODE": "copy",
            "UV_NO_CONFIG": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "BU_NAME": "hermes_"
            + (
                profile_id
                if isinstance(profile_id, str) and re.fullmatch(r"[0-9a-f]{32}", profile_id)
                else secrets.token_hex(16)
            ),
        }
    )
    return env


def _external_state_env(env: dict[str, str], root: Path) -> dict[str, str]:
    isolated = env.copy()
    profile_root = root / "browser-profile"
    isolated.update(
        {
            "HERMES_HOME": str(root),
            "BH_HOME": str(root),
            "BH_CONFIG_DIR": str(root / "config"),
            "BH_RUNTIME_DIR": str(root / "runtime"),
            "BH_TMP_DIR": str(root / "tmp"),
            "BH_AGENT_WORKSPACE": str(root / "agent-workspace"),
            "HOME": str(profile_root),
            "USERPROFILE": str(profile_root),
            "APPDATA": str(profile_root / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(profile_root / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(profile_root / ".config"),
            "XDG_CACHE_HOME": str(profile_root / ".cache"),
            "XDG_DATA_HOME": str(profile_root / ".local" / "share"),
            "XDG_STATE_HOME": str(profile_root / ".local" / "state"),
            "BROWSER_USE_CONFIG_DIR": str(profile_root / ".config" / "browseruse"),
            "BROWSER_USE_CONFIG_PATH": str(profile_root / ".config" / "browseruse" / "config.json"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "TMPDIR": str(root / "tmp"),
            "TEMP": str(root / "tmp"),
            "TMP": str(root / "tmp"),
        }
    )
    return isolated


def _run(command: list[str], *, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="hermes-browser-use-pycache-") as pycache:
        isolated_env = env.copy()
        isolated_env["PYTHONDONTWRITEBYTECODE"] = "1"
        isolated_env["PYTHONNOUSERSITE"] = "1"
        isolated_env["PYTHONPYCACHEPREFIX"] = pycache
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            env=isolated_env,
        )


def probe(
    target: Path,
    release: dict[str, str],
    *,
    env: dict[str, str],
    system: str | None = None,
    expected_payload_sha256: str | None = None,
    expected_cli_sha256: str | None = None,
    expected_interpreter: dict[str, Any] | None = None,
    trusted_staging: bool = False,
) -> dict[str, Any]:
    python = _venv_python(target, system)
    cli = _venv_cli(target, system)
    if not python.is_file() or not cli.is_file():
        return {"exact": False, "cli_path": str(cli)}
    try:
        environment_sha256 = _environment_sha256(target)
        interpreter = _interpreter_integrity(target, system)
        cli_sha256 = _sha256(cli, max_bytes=CLI_MAX_BYTES)
    except OSError as exc:
        return {
            "exact": False,
            "cli_path": str(cli),
            "cli_sha256": None,
            "environment_sha256": None,
            "interpreter": None,
            "integrity_error": str(exc)[:200],
            "error_code": "integrity_read_failed",
        }
    entrypoints_executable = (system or platform.system()) == "Windows" or (
        os.access(python, os.X_OK) and os.access(cli, os.X_OK)
    )
    integrity_matches = bool(
        environment_sha256 is not None
        and interpreter is not None
        and interpreter["executable"]
        and entrypoints_executable
        and (
            trusted_staging
            or (
                expected_payload_sha256 is not None
                and expected_cli_sha256 is not None
                and expected_interpreter is not None
                and environment_sha256 == expected_payload_sha256
                and cli_sha256 == expected_cli_sha256
                and interpreter == expected_interpreter
            )
        )
    )
    if not integrity_matches:
        return {
            "exact": False,
            "cli_path": str(cli),
            "cli_sha256": None,
            "environment_sha256": None,
            "interpreter": None,
            "integrity_error": "managed environment integrity validation failed",
            "error_code": IntegrityReadError.code,
        }
    code = (
        "import json; from importlib.metadata import version; "
        "print(json.dumps({'browser_use': version('browser-use'), "
        "'browser_harness': version('browser-harness')}))"
    )
    try:
        proc = _run(
            [str(python), "-I", "-B", "-c", code],
            env=env,
            timeout=PROBE_SUBPROCESS_TIMEOUT_SECONDS,
        )
        python_returncode = proc.returncode
        python_version_output = proc.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        python_returncode = -1
        python_version_output = ""
        python_error = str(exc)[:200]
    else:
        python_error = ""
    try:
        cli_proc = _run(
            (
                [str(cli), "--version"]
                if (system or platform.system()) == "Windows"
                else [str(python), "-I", "-B", str(cli), "--version"]
            ),
            env=env,
            timeout=PROBE_SUBPROCESS_TIMEOUT_SECONDS,
        )
        cli_returncode = cli_proc.returncode
        cli_version_output = (cli_proc.stdout or cli_proc.stderr).strip()[:200]
    except (OSError, subprocess.TimeoutExpired) as exc:
        cli_returncode = -1
        cli_version_output = str(exc)[:200]
    try:
        versions = json.loads(python_version_output.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        versions = {}
    exact = (
        python_returncode == 0
        and cli_returncode == 0
        and versions.get("browser_use") == release["version"]
        and versions.get("browser_harness") == release["harness_version"]
        and environment_sha256 is not None
    )
    return {
        "exact": exact,
        "cli_path": str(cli),
        "cli_sha256": cli_sha256 if exact else None,
        "environment_sha256": environment_sha256 if exact else None,
        "interpreter": interpreter if exact else None,
        "cli_version_output": cli_version_output,
        "python_error": python_error,
        "versions": versions,
    }


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        payload = bytearray()
        _consume_regular_file(
            path,
            max_bytes=RECEIPT_MAX_BYTES,
            deadline_seconds=FILE_HASH_TIMEOUT_SECONDS,
            monotonic=time.monotonic,
            consume=payload.extend,
        )
        decoded = json.loads(bytes(payload).decode("utf-8"))
    except (
        OSError,
        IntegrityHashLimitError,
        UnicodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return None
    return decoded if isinstance(decoded, dict) else None


def _receipt_relative_path(home: Path, value: Any) -> Path | None:
    path = Path(str(value or ""))
    if not value or path.is_absolute() or ".." in path.parts:
        return None
    return home / path


def _receipt_profile_id(receipt: dict[str, Any] | None) -> str | None:
    value = receipt.get("profile_id") if receipt else None
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else None


def _receipt_matches_install(
    receipt: dict[str, Any] | None,
    *,
    contract: dict[str, Any],
    target: Path,
    hermes_home: Path,
    system: str | None,
) -> bool:
    return bool(
        receipt
        and receipt.get("schema_version") == 2
        and receipt.get("kind") == "browser_use_cli_install_receipt"
        and receipt.get("release") == contract["release"]
        and receipt.get("artifact_sha256") == _artifact_sha256(contract)
        and receipt.get("lock_sha256") == contract["installer"]["lock_sha256"]
        and receipt.get("profile_root") == "state/browser-use/browser-profile"
        and _receipt_profile_id(receipt) is not None
        and receipt.get("cli_path") == _venv_cli(Path(contract["installer"]["target"]), system).as_posix()
        and re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("environment_sha256") or ""))
        and re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("cli_sha256") or ""))
        and _interpreter_receipt_is_valid(receipt.get("interpreter"))
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif _lexists(path):
        path.unlink()


def _restore_swap(
    *,
    target: Path,
    target_backup: Path | None,
    receipt_path: Path,
    receipt_backup: Path | None,
    target_backup_moved: bool,
    receipt_backup_moved: bool,
    target_installed: bool,
) -> None:
    if target_installed:
        _remove_tree(target)
    if target_backup_moved and target_backup and _lexists(target_backup):
        os.replace(target_backup, target)
    if receipt_backup_moved and _lexists(receipt_path):
        receipt_path.unlink()
    if receipt_backup_moved and receipt_backup and _lexists(receipt_backup):
        os.replace(receipt_backup, receipt_path)


def _receipt_payload(
    *,
    contract: dict[str, Any],
    probe_result: dict[str, Any],
    hermes_home: Path,
    status: str,
    dry_run: bool,
    installed: bool,
    error: str | None,
    error_code: str | None = None,
    target_backup: Path | None = None,
    receipt_backup: Path | None = None,
    profile_id: str | None = None,
    system: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "kind": "browser_use_cli_install_receipt",
        "generated_at": utc_now(),
        "ok": status not in {"failed"},
        "status": status,
        "dry_run": dry_run,
        "release": contract["release"],
        "artifact_sha256": _artifact_sha256(contract),
        "lock_sha256": contract["installer"]["lock_sha256"],
        "cli_path": _venv_cli(Path(contract["installer"]["target"]), system).as_posix(),
        "cli_sha256": probe_result.get("cli_sha256"),
        "environment_sha256": probe_result.get("environment_sha256"),
        "interpreter": probe_result.get("interpreter"),
        "profile_root": "state/browser-use/browser-profile",
        "profile_id": profile_id,
        "privacy_environment": contract["privacy_environment"],
        "installed": installed,
        "error_code": error_code,
        "rollback": {
            "tool_backup": target_backup.relative_to(hermes_home).as_posix() if target_backup else None,
            "receipt_backup": receipt_backup.relative_to(hermes_home).as_posix() if receipt_backup else None,
        },
        "error": error,
    }


def ensure(
    *,
    hermes_python: Path,
    hermes_home: Path,
    contract_path: Path,
    dry_run: bool = False,
    system: str | None = None,
) -> tuple[int, dict[str, Any]]:
    contract = load_contract(contract_path)
    release = contract["release"]
    target = hermes_home / contract["installer"]["target"]
    receipt_path = hermes_home / "state" / RECEIPT_NAME
    prior_receipt = _read_receipt(receipt_path)
    prior_valid = _receipt_matches_install(
        prior_receipt,
        contract=contract,
        target=target,
        hermes_home=hermes_home,
        system=system,
    )
    profile_id = _receipt_profile_id(prior_receipt) if prior_valid else None
    env = _child_env(hermes_home, contract["privacy_environment"], profile_id)
    expected_payload = prior_receipt.get("environment_sha256") if prior_valid else None
    expected_cli = prior_receipt.get("cli_sha256") if prior_valid else None
    expected_interpreter = prior_receipt.get("interpreter") if prior_valid else None
    before = {"exact": False, "cli_path": str(_venv_cli(target, system))}
    try:
        if prior_valid and dry_run:
            with tempfile.TemporaryDirectory(prefix="hermes-browser-use-dry-run-") as dry_state:
                before = probe(
                    target,
                    release,
                    env=_external_state_env(env, Path(dry_state)),
                    system=system,
                    expected_payload_sha256=expected_payload,
                    expected_cli_sha256=expected_cli,
                    expected_interpreter=expected_interpreter,
                )
        elif prior_valid:
            before = probe(
                target,
                release,
                env=env,
                system=system,
                expected_payload_sha256=expected_payload,
                expected_cli_sha256=expected_cli,
                expected_interpreter=expected_interpreter,
            )
    except IntegrityHashLimitError as exc:
        before = {
            "exact": False,
            "cli_path": str(_venv_cli(target, system)),
            "integrity_error": str(exc),
            "error_code": exc.code,
        }
    before["exact"] = bool(before["exact"] and prior_valid)
    if before["exact"]:
        before["exact"] = before.get("cli_sha256") == prior_receipt.get("cli_sha256")

    if dry_run:
        status = "idempotent" if before["exact"] else "would_install"
        return 0, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status=status,
            dry_run=True,
            installed=False,
            error=None,
            profile_id=profile_id,
            system=system,
        )

    if before["exact"]:
        rollback = prior_receipt.get("rollback")
        rollback = rollback if isinstance(rollback, dict) else {}
        target_backup = _receipt_relative_path(hermes_home, rollback.get("tool_backup"))
        receipt_backup = _receipt_relative_path(hermes_home, rollback.get("receipt_backup"))
        return 0, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="idempotent",
            dry_run=False,
            installed=False,
            error=None,
            target_backup=target_backup,
            receipt_backup=receipt_backup,
            profile_id=profile_id,
            system=system,
        )

    uv = shutil.which("uv")
    if not uv:
        error = "uv executable is unavailable"
        return 1, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="failed",
            dry_run=False,
            installed=False,
            error=error,
            system=system,
        )
    if not hermes_python.is_file():
        error = f"Hermes runtime Python is missing: {hermes_python}"
        return 1, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="failed",
            dry_run=False,
            installed=False,
            error=error,
            system=system,
        )

    profile_id = profile_id or secrets.token_hex(16)
    env["BU_NAME"] = f"hermes_{profile_id}"
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
    try:
        create = _run(
            [
                uv,
                "--no-config",
                "venv",
                "--relocatable",
                "--python",
                str(hermes_python),
                str(staging),
            ],
            env=env,
            timeout=VENV_TIMEOUT_SECONDS,
        )
        if create.returncode == 0:
            staging_python = _venv_python(staging, system)
            install = _run(
                [
                    uv,
                    "--no-config",
                    "pip",
                    "install",
                    "--python",
                    str(staging_python),
                    "--requirements",
                    contract["_lock_path"],
                    "--require-hashes",
                    "--no-index",
                    "--no-deps",
                    "--only-binary",
                    ":all:",
                    "--link-mode",
                    "copy",
                ],
                env=env,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
        else:
            install = create
    except (OSError, subprocess.TimeoutExpired) as exc:
        _remove_tree(staging)
        return 1, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="failed",
            dry_run=False,
            installed=False,
            error=str(exc)[:800],
            system=system,
        )
    if create.returncode != 0 or install.returncode != 0:
        error = (install.stderr or install.stdout or "install command failed").strip()[-800:]
        _remove_tree(staging)
        return 1, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="failed",
            dry_run=False,
            installed=False,
            error=error,
            system=system,
        )
    try:
        staged = probe(
            staging,
            release,
            env=env,
            system=system,
            trusted_staging=True,
        )
    except IntegrityHashLimitError as exc:
        _remove_tree(staging)
        return 1, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="failed",
            dry_run=False,
            installed=False,
            error=str(exc),
            error_code=exc.code,
            system=system,
        )
    if not staged["exact"]:
        error = (
            staged.get("integrity_error") or install.stderr or install.stdout or "install verification failed"
        ).strip()[-800:]
        _remove_tree(staging)
        return 1, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="failed",
            dry_run=False,
            installed=False,
            error=error,
            error_code=staged.get("error_code"),
            system=system,
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target_backup = target.with_name(f"{target.name}.bak-{stamp}") if _lexists(target) else None
    receipt_backup = receipt_path.with_name(f"{receipt_path.name}.bak-{stamp}") if _lexists(receipt_path) else None
    target_backup_moved = False
    receipt_backup_moved = False
    target_installed = False
    try:
        if target_backup:
            os.replace(target, target_backup)
            target_backup_moved = True
        if receipt_backup:
            receipt_backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(receipt_path, receipt_backup)
            receipt_backup_moved = True
        os.replace(staging, target)
        target_installed = True
        after = probe(
            target,
            release,
            env=env,
            system=system,
            expected_payload_sha256=staged["environment_sha256"],
            expected_cli_sha256=staged["cli_sha256"],
            expected_interpreter=staged["interpreter"],
        )
        if not after["exact"]:
            if after.get("error_code"):
                raise IntegrityReadError(after.get("integrity_error") or "installed payload integrity read failed")
            raise RuntimeError("installed payload failed post-swap verification")
        receipt = _receipt_payload(
            contract=contract,
            probe_result=after,
            hermes_home=hermes_home,
            status="installed",
            dry_run=False,
            installed=True,
            error=None,
            target_backup=target_backup,
            receipt_backup=receipt_backup,
            profile_id=profile_id,
            system=system,
        )
        _write_receipt(receipt_path, receipt)
    except Exception as exc:
        if not target_installed:
            _remove_tree(staging)
        _restore_swap(
            target=target,
            target_backup=target_backup,
            receipt_path=receipt_path,
            receipt_backup=receipt_backup,
            target_backup_moved=target_backup_moved,
            receipt_backup_moved=receipt_backup_moved,
            target_installed=target_installed,
        )
        return 1, _receipt_payload(
            contract=contract,
            probe_result=before,
            hermes_home=hermes_home,
            status="failed",
            dry_run=False,
            installed=False,
            error=str(exc)[:800],
            error_code=getattr(exc, "code", None),
            system=system,
        )
    return 0, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-python", required=True, type=Path)
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    code, receipt = ensure(
        hermes_python=args.hermes_python,
        hermes_home=args.hermes_home,
        contract_path=args.contract,
        dry_run=args.dry_run,
    )
    print(json.dumps(receipt, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
