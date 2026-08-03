#!/usr/bin/env python3
"""Bind profile-scoped circuit state and prove native service ownership."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import json
import locale
import ntpath
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

try:
    import pwd
except ImportError:
    pwd = None

try:
    import winreg
except ImportError:
    winreg = None

KEY = "HERMES_CODEX_401_CIRCUIT_STATE"
RELEVANT_SERVICE_ENV = ("HERMES_HOME", "VIRTUAL_ENV", "PYTHONPATH")
RUNTIME_BINDING_KIND = "botdoctor_runtime_binding"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validate_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"path must be absolute and lexical: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink() or _is_reparse_point(current):
            raise RuntimeError(f"path contains a link or reparse point: {current}")
        if current != path and current.exists() and not current.is_dir():
            raise RuntimeError(f"path parent is not a directory: {current}")


def _inside_runtime(raw: str, runtime: Path, *, windows: bool = False) -> bool:
    if windows:
        candidate = ntpath.normcase(ntpath.abspath(raw.strip('"')))
        root = ntpath.normcase(ntpath.abspath(str(runtime)))
        try:
            return ntpath.commonpath((candidate, root)) == root
        except ValueError:
            return False
    try:
        _lexical(Path(raw)).relative_to(runtime)
    except (ValueError, OSError):
        return False
    return True


def _atomic_bytes(
    path: Path,
    data: bytes,
    mode: int,
    owner: tuple[int, int] | None = None,
) -> None:
    _validate_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if owner is not None:
                if not hasattr(os, "fchown"):
                    raise RuntimeError("file ownership cannot be preserved")
                os.fchown(stream.fileno(), *owner)
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if not hasattr(os, "fchmod"):
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_runtime_binding(
    home: Path,
    runtime: Path,
    *,
    service_kind: str,
    service_owner: str,
    definition_path: Path | None = None,
    definition_bytes: bytes,
    launcher_paths: tuple[Path, ...] = (),
) -> tuple[Path, Path]:
    """Persist the exact service/runtime tuple after live proof succeeds."""
    runtime_python = _runtime_python(runtime).absolute()
    launchers = []
    for launcher in launcher_paths:
        _validate_path(launcher)
        if not launcher.is_file() or launcher.is_symlink() or _is_reparse_point(launcher):
            raise RuntimeError(f"runtime launcher is missing or unsafe: {launcher}")
        launchers.append(
            {
                "path": str(launcher),
                "sha256": _sha256(launcher.read_bytes()),
            }
        )
    payload = {
        "schema_version": 1,
        "kind": RUNTIME_BINDING_KIND,
        "status": "active",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hermes_home": str(home),
        "runtime_root": str(runtime),
        "runtime_python": str(runtime_python),
        "service": {
            "kind": service_kind,
            "owner": service_owner,
            "definition_path": str(definition_path) if definition_path else None,
            "definition_sha256": _sha256(definition_bytes),
            "launchers": launchers,
        },
    }
    path = home / "state/runtime-binding.json"
    after = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    backup = _snapshot_runtime_binding(home, path, after)
    try:
        _atomic_bytes(path, after, 0o600, _path_owner(home))
    except BaseException:
        _restore_runtime_binding(home, backup, allow_pending=True)
        raise
    _complete_runtime_binding_backup(home, backup)
    return path, backup


def _runtime_binding_backup_receipt(home: Path, backup: Path) -> dict:
    root = home / "state/runtime-binding-backups"
    if not backup.is_relative_to(root):
        raise ValueError("runtime binding backup is outside HERMES_HOME")
    _validate_path(backup)
    receipt_path = backup / "receipt.json"
    _validate_path(receipt_path)
    if (
        not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _is_reparse_point(receipt_path)
    ):
        raise ValueError("runtime binding backup receipt is missing or unsafe")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("kind") != "botdoctor_runtime_binding_backup"
        or receipt.get("status") not in {"pending", "completed"}
        or receipt.get("hermes_home") != str(home)
        or receipt.get("rollback") != str(backup)
        or not isinstance(receipt.get("existed"), bool)
    ):
        raise ValueError("runtime binding backup receipt is invalid")
    return receipt


def _snapshot_runtime_binding(home: Path, path: Path, after: bytes) -> Path:
    _validate_path(path)
    if path.exists() and (
        not path.is_file() or path.is_symlink() or _is_reparse_point(path)
    ):
        raise RuntimeError("active runtime binding is unsafe")
    before = path.read_bytes() if path.is_file() else None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / "state/runtime-binding-backups" / stamp
    _validate_path(backup)
    if backup.exists():
        raise RuntimeError("runtime binding backup path collision")
    backup.mkdir(parents=True, mode=0o700)
    if before is not None:
        _atomic_bytes(backup / "runtime-binding.before", before, 0o600)
    receipt = {
        "schema_version": 1,
        "kind": "botdoctor_runtime_binding_backup",
        "status": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hermes_home": str(home),
        "existed": before is not None,
        "before_sha256": _sha256(before) if before is not None else None,
        "after_sha256": _sha256(after),
        "rollback": str(backup),
    }
    _atomic_bytes(
        backup / "receipt.json",
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    return backup


def _complete_runtime_binding_backup(home: Path, backup: Path) -> None:
    receipt = _runtime_binding_backup_receipt(home, backup)
    receipt["status"] = "completed"
    _atomic_bytes(
        backup / "receipt.json",
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )


def _restore_runtime_binding(
    home: Path, backup: Path, *, allow_pending: bool = False
) -> None:
    receipt = _runtime_binding_backup_receipt(home, backup)
    if receipt["status"] == "pending" and not allow_pending:
        raise ValueError("runtime binding backup is incomplete")
    path = home / "state/runtime-binding.json"
    _validate_path(path)
    current = path.read_bytes() if path.is_file() else None
    before_path = backup / "runtime-binding.before"
    existed = receipt["existed"]
    before = before_path.read_bytes() if existed and before_path.is_file() else None
    if existed and (
        before is None
        or before_path.is_symlink()
        or _is_reparse_point(before_path)
        or _sha256(before) != receipt.get("before_sha256")
    ):
        raise RuntimeError("runtime binding rollback payload is missing or invalid")
    if not existed and (before_path.exists() or receipt.get("before_sha256") is not None):
        raise ValueError("runtime binding backup has unexpected prior data")
    expected_current = receipt.get("after_sha256")
    current_matches_after = current is not None and _sha256(current) == expected_current
    current_matches_before = (before is None and current is None) or (
        before is not None and current is not None and _sha256(current) == _sha256(before)
    )
    if not current_matches_after and not (
        allow_pending and receipt["status"] == "pending" and current_matches_before
    ):
        raise RuntimeError("active runtime binding changed after activation")
    if before is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_bytes(path, before, 0o600, _path_owner(home))


def _parse_assignment_value(raw: bytes) -> str:
    raw = raw.strip(b" \t")
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("profile circuit assignment is not valid UTF-8") from exc
    if text.startswith("'") or text.startswith('"'):
        quote = text[0]
        if len(text) < 2 or not text.endswith(quote):
            raise ValueError("profile circuit assignment has invalid quoting")
        body = text[1:-1]
        if quote == "'":
            return body
        rendered: list[str] = []
        index = 0
        while index < len(body):
            character = body[index]
            if character != "\\":
                rendered.append(character)
                index += 1
                continue
            index += 1
            if index >= len(body) or body[index] not in {'\\', '"'}:
                raise ValueError("profile circuit assignment has unsupported escaping")
            rendered.append(body[index])
            index += 1
        return "".join(rendered)
    return text


def _binding_values(data: bytes) -> list[str]:
    key = re.escape(KEY.encode())
    pattern = re.compile(
        rb"^[ \t]*(?:export[ \t]+)?" + key + rb"[ \t]*=(.*)$"
    )
    values: list[str] = []
    for line in data.splitlines():
        match = pattern.fullmatch(line.rstrip(b"\r"))
        if match:
            values.append(_parse_assignment_value(match.group(1)))
    return values


def _render_binding(data: bytes, value: str) -> bytes:
    if any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError("circuit state path contains unsafe characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    separator = b"" if not data or data.endswith((b"\n", b"\r")) else b"\n"
    return data + separator + f'{KEY}="{escaped}"\n'.encode()


def _runtime_python(runtime: Path) -> Path:
    candidates = (
        runtime / "venv" / "bin" / "python",
        runtime / ".venv" / "bin" / "python",
        runtime / "venv" / "Scripts" / "python.exe",
        runtime / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError("candidate runtime Python is missing")


def _receipt_hash(value: object, field: str) -> str:
    raw = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", raw) is None:
        raise ValueError(f"profile environment receipt has invalid {field}")
    return raw


def _receipt_mode(value: object, field: str) -> int:
    raw = str(value or "")
    if re.fullmatch(r"0[0-7]{3}", raw) is None:
        raise ValueError(f"profile environment receipt has invalid {field}")
    return int(raw, 8)


def _receipt_owner(receipt: dict[str, object], prefix: str) -> tuple[int, int] | None:
    uid = receipt.get(f"{prefix}_uid")
    gid = receipt.get(f"{prefix}_gid")
    if uid is None and gid is None:
        return None
    if (
        isinstance(uid, bool)
        or isinstance(gid, bool)
        or not isinstance(uid, int)
        or not isinstance(gid, int)
        or uid < 0
        or gid < 0
    ):
        raise ValueError(f"profile environment receipt has invalid {prefix} owner")
    return uid, gid


def _path_owner(path: Path) -> tuple[int, int] | None:
    if os.name == "nt":
        return None
    metadata = path.stat()
    return metadata.st_uid, metadata.st_gid


def _new_profile_owner(home: Path) -> tuple[int, int] | None:
    if os.name == "nt":
        return None
    owner = _path_owner(home)
    if owner is None or pwd is None:
        raise RuntimeError("profile owner cannot be proven")
    try:
        pwd.getpwuid(owner[0])
    except KeyError as exc:
        raise RuntimeError("profile owner cannot be proven") from exc
    return owner


def _restore_backup(home: Path, backup: Path) -> None:
    root = home / "state" / "profile-environment-backups"
    if not backup.is_relative_to(root):
        raise ValueError("profile environment backup is outside HERMES_HOME")
    _validate_path(backup)
    receipt_path = backup / "receipt.json"
    saved_path = backup / ".env.before"
    _validate_path(receipt_path)
    _validate_path(saved_path)
    if (
        not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _is_reparse_point(receipt_path)
    ):
        raise ValueError("profile environment receipt is missing or unsafe")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 2
        or receipt.get("kind") != "botdoctor_profile_environment_binding"
        or receipt.get("status") not in {"pending", "completed"}
        or receipt.get("hermes_home") != str(home)
        or receipt.get("rollback") != str(backup)
        or not isinstance(receipt.get("existed"), bool)
    ):
        raise ValueError("profile environment receipt is invalid")
    existed = receipt["existed"]
    before_mode = _receipt_mode(receipt.get("before_mode"), "before_mode")
    after_mode = _receipt_mode(receipt.get("after_mode"), "after_mode")
    before_owner = _receipt_owner(receipt, "before")
    after_owner = _receipt_owner(receipt, "after")
    before_hash = receipt.get("before_sha256")
    if existed:
        before_hash = _receipt_hash(before_hash, "before_sha256")
        if (
            not saved_path.is_file()
            or saved_path.is_symlink()
            or _is_reparse_point(saved_path)
        ):
            raise RuntimeError("profile environment backup is missing or unsafe")
        before = saved_path.read_bytes()
        if _sha256(before) != before_hash:
            raise RuntimeError("profile environment backup hash mismatch")
    else:
        if before_hash is not None or saved_path.exists() or saved_path.is_symlink():
            raise ValueError("profile environment receipt has unexpected backup data")
        before = None
        if before_owner is not None:
            raise ValueError("profile environment receipt has unexpected owner")
    after_hash = _receipt_hash(receipt.get("after_sha256"), "after_sha256")
    env_path = home / ".env"
    _validate_path(env_path)
    current = env_path.read_bytes() if env_path.is_file() else None
    current_mode = env_path.stat().st_mode & 0o777 if current is not None else None
    current_owner = _path_owner(env_path) if current is not None else None
    valid_after = (
        current is not None
        and _sha256(current) == after_hash
        and current_mode == after_mode
        and current_owner == after_owner
    )
    valid_before = (
        existed
        and current is not None
        and before is not None
        and _sha256(current) == _sha256(before)
        and current_mode == before_mode
        and current_owner == before_owner
    ) or (not existed and current is None)
    if not valid_after and not (
        receipt["status"] == "pending" and valid_before
    ):
        raise RuntimeError("profile environment changed after binding")
    if before is None:
        env_path.unlink(missing_ok=True)
    else:
        _atomic_bytes(env_path, before, before_mode, before_owner)


def _bind_environment(home: Path) -> Path | None:
    env_path = home / ".env"
    _validate_path(env_path)
    if env_path.exists() and not env_path.is_file():
        raise RuntimeError("profile environment is not a regular file")
    before = env_path.read_bytes() if env_path.is_file() else b""
    existed = env_path.is_file()
    before_mode = env_path.stat().st_mode & 0o777 if existed else 0
    before_owner = _path_owner(env_path) if existed else None
    after_owner = before_owner if existed else _new_profile_owner(home)
    expected = str(home / "state" / "codex-401-circuit.json")
    values = _binding_values(before)
    if len(values) > 1:
        raise RuntimeError("profile circuit binding is duplicated")
    if values:
        if values[0] != expected:
            raise RuntimeError("profile circuit binding conflicts with this profile")
        return None
    after = _render_binding(before, expected)
    after_mode = before_mode if existed else 0o600
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / "state" / "profile-environment-backups" / stamp
    _validate_path(backup)
    if backup.exists():
        raise RuntimeError("profile environment backup path collision")
    backup.mkdir(parents=True, mode=0o700)
    if existed:
        _atomic_bytes(backup / ".env.before", before, 0o600)
    receipt = {
        "schema_version": 2,
        "kind": "botdoctor_profile_environment_binding",
        "status": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hermes_home": str(home),
        "existed": existed,
        "before_sha256": _sha256(before) if existed else None,
        "before_mode": format(before_mode, "04o"),
        "before_uid": before_owner[0] if before_owner else None,
        "before_gid": before_owner[1] if before_owner else None,
        "after_sha256": _sha256(after),
        "after_mode": format(after_mode, "04o"),
        "after_uid": after_owner[0] if after_owner else None,
        "after_gid": after_owner[1] if after_owner else None,
        "rollback": str(backup),
    }
    receipt_path = backup / "receipt.json"
    _atomic_bytes(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    try:
        _atomic_bytes(env_path, after, after_mode, after_owner)
        if _binding_values(env_path.read_bytes()) != [expected]:
            raise RuntimeError("profile circuit binding verification failed")
    except BaseException:
        _restore_backup(home, backup)
        raise
    receipt["status"] = "completed"
    _atomic_bytes(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    return backup


def _single_line(text: str, prefix: str) -> str:
    values = [
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith(prefix + "=")
    ]
    if len(values) != 1 or not values[0]:
        raise RuntimeError(f"service definition has ambiguous {prefix}")
    return values[0]


def _service_environment(tokens: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise RuntimeError("service environment is invalid")
        key, value = token.split("=", 1)
        if key in environment:
            raise RuntimeError("service environment is ambiguous")
        environment[key] = value
    return {
        key: environment[key]
        for key in RELEVANT_SERVICE_ENV
        if key in environment
    }


def _systemd_definition_spec(data: bytes) -> tuple[list[str], dict[str, str]]:
    text = data.decode("utf-8")
    try:
        argv = shlex.split(_single_line(text, "ExecStart"))
        environment_tokens = [
            token
            for line in text.splitlines()
            if line.startswith("Environment=")
            for token in shlex.split(line.split("=", 1)[1].strip())
        ]
    except ValueError as exc:
        raise RuntimeError("systemd unit launch specification is invalid") from exc
    if not argv:
        raise RuntimeError("systemd unit launch specification is invalid")
    if argv[0][0] in "-@+!:|":
        raise RuntimeError("systemd unit has unverified execution flags")
    return argv, _service_environment(environment_tokens)


def _prove_systemd(
    data: bytes,
    home: Path,
    runtime: Path,
    scope: str,
    owner: str,
    expected_argv: list[str],
    expected_environment: dict[str, str],
    current_user: str | None = None,
) -> None:
    text = data.decode("utf-8")
    matches = [
        line
        for line in text.splitlines()
        if line == f'Environment="HERMES_HOME={home}"'
    ]
    if len(matches) != 1:
        raise RuntimeError("systemd unit does not bind the proven profile")
    argv, environment = _systemd_definition_spec(data)
    if (
        argv != expected_argv
        or environment != expected_environment
        or not _inside_runtime(argv[0], runtime)
    ):
        raise RuntimeError("systemd unit does not match the pinned launch spec")
    users = [
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("User=")
    ]
    if scope == "system":
        if len(users) != 1 or users[0] != owner:
            raise RuntimeError("systemd unit does not use the proven owner")
    elif users or owner.casefold() != (
        current_user or _current_identity()[0]
    ).casefold():
        raise RuntimeError("systemd user scope does not match the proven owner")


def _systemd_unescape(value: str) -> str:
    def replace_hex(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    value = re.sub(r"\\x([0-9A-Fa-f]{2})", replace_hex, value)
    return value.replace(r"\s", " ").replace(r"\\", "\\")


def _systemd_properties(
    service: str,
    scope: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    prefix = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
    reload_result = run(
        [*prefix, "daemon-reload"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if reload_result.returncode:
        raise RuntimeError("systemd daemon-reload failed")
    names = (
        "ExecStart",
        "ExecStartEx",
        "Environment",
        "EnvironmentFiles",
        "PassEnvironment",
        "User",
        "FragmentPath",
        "DropInPaths",
    )
    result = run(
        [
            *prefix,
            "show",
            service,
            "--no-pager",
            "--property",
            ",".join(names),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("effective systemd service query failed")
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in properties:
            raise RuntimeError("effective systemd properties are ambiguous")
        properties[key] = value
    if set(properties) != set(names):
        raise RuntimeError("effective systemd properties are incomplete")
    return properties


def _effective_systemd_argv(value: str) -> list[str]:
    matches = re.findall(
        r"(?:^|[ {])argv\[\]=(.*?)(?:\s*;|$)",
        _systemd_unescape(value),
    )
    if len(matches) != 1:
        raise RuntimeError("effective systemd command is ambiguous")
    try:
        argv = shlex.split(matches[0].strip())
    except ValueError as exc:
        raise RuntimeError("effective systemd command is invalid") from exc
    if not argv:
        raise RuntimeError("effective systemd command is invalid")
    return argv


def _effective_systemd_flags(value: str) -> str:
    matches = re.findall(
        r"(?:^|[ {])flags=(.*?)(?:\s*;|$)",
        _systemd_unescape(value),
    )
    if len(matches) != 1:
        raise RuntimeError("effective systemd execution flags are ambiguous")
    return matches[0].strip()


def _effective_systemd_path(value: str) -> str:
    matches = re.findall(
        r"(?:^|[ {])path=(.*?)(?:\s*;|$)",
        _systemd_unescape(value),
    )
    if len(matches) != 1 or not matches[0].strip():
        raise RuntimeError("effective systemd executable is ambiguous")
    return matches[0].strip()


def _systemd_manager_environment(
    scope: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    if scope != "user":
        return {}
    result = run(
        ["systemctl", "--user", "show-environment"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("systemd manager environment query failed")
    values: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("PYTHONPATH="):
            values.append(line.split("=", 1)[1])
    if len(values) > 1:
        raise RuntimeError("systemd manager PYTHONPATH is ambiguous")
    return {"PYTHONPATH": values[0] if values else ""}


def _prove_pythonpath_source(
    value: str,
    runtime: Path,
    separator: str,
    *,
    windows: bool = False,
    source: str,
) -> None:
    if not isinstance(value, str):
        raise RuntimeError(f"{source} is invalid")
    if not value:
        return
    entries = value.split(separator)
    if any(not entry for entry in entries):
        raise RuntimeError(f"{source} contains a current-directory entry")
    if any(
        not _inside_runtime(entry, runtime, windows=windows)
        for entry in entries
    ):
        raise RuntimeError(f"{source} escapes the candidate")


def _prove_effective_systemd(
    properties: dict[str, str],
    definition: Path,
    home: Path,
    runtime: Path,
    scope: str,
    owner: str,
    expected_argv: list[str],
    expected_environment: dict[str, str],
    current_user: str | None = None,
    manager_environment: dict[str, str] | None = None,
) -> None:
    if properties["DropInPaths"].strip():
        raise RuntimeError("systemd service has unverified drop-in overrides")
    if (
        properties["EnvironmentFiles"].strip()
        or properties["PassEnvironment"].strip()
    ):
        raise RuntimeError("systemd service has unverified environment sources")
    fragment = _lexical(Path(_systemd_unescape(properties["FragmentPath"])))
    if fragment != definition:
        raise RuntimeError("effective systemd fragment is not the native definition")
    argv = _effective_systemd_argv(properties["ExecStart"])
    extended_argv = _effective_systemd_argv(properties["ExecStartEx"])
    flags = _effective_systemd_flags(properties["ExecStartEx"])
    if (
        argv != expected_argv
        or extended_argv != expected_argv
        or _effective_systemd_path(properties["ExecStart"]) != expected_argv[0]
        or _effective_systemd_path(properties["ExecStartEx"]) != expected_argv[0]
        or flags
        or not _inside_runtime(argv[0], runtime)
    ):
        raise RuntimeError(
            "effective systemd service does not match the pinned launch spec"
        )
    try:
        environment = _service_environment(
            shlex.split(_systemd_unescape(properties["Environment"]))
        )
    except ValueError as exc:
        raise RuntimeError("effective systemd environment is invalid") from exc
    if environment != expected_environment:
        raise RuntimeError(
            "effective systemd environment does not match the pinned launch spec"
        )
    effective_user = properties["User"].strip()
    if scope == "system":
        if effective_user != owner:
            raise RuntimeError("effective systemd service has the wrong owner")
    elif effective_user or owner.casefold() != (
        current_user or _current_identity()[0]
    ).casefold():
        raise RuntimeError("effective systemd user scope has the wrong owner")
    if scope == "user":
        if manager_environment is None or set(manager_environment) != {
            "PYTHONPATH"
        }:
            raise RuntimeError("systemd manager environment proof is incomplete")
        _prove_pythonpath_source(
            manager_environment["PYTHONPATH"],
            runtime,
            os.pathsep,
            source="systemd manager PYTHONPATH",
        )


def _current_identity() -> tuple[str, Path]:
    try:
        if pwd is None:
            raise AttributeError
        entry = pwd.getpwuid(os.getuid())
        return entry.pw_name, Path(entry.pw_dir)
    except (AttributeError, KeyError):
        return getpass.getuser(), Path.home()


def _runtime_value(
    runtime: Path,
    home: Path,
    code: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    token: str | None = None,
) -> str:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    marker = f"__BOTDOCTOR_PROOF_{token or os.urandom(16).hex()}__"
    wrapped = (
        "import json;"
        f"_proof_marker={marker!r};"
        "_proof_emit=lambda value:print("
        "_proof_marker+json.dumps(value,separators=(',',':')));"
        + code
    )
    result = run(
        [str(_runtime_python(runtime)), "-c", wrapped],
        cwd=runtime,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    lines = [
        line.removeprefix(marker)
        for line in result.stdout.splitlines()
        if line.startswith(marker)
    ]
    if result.returncode or len(lines) != 1:
        raise RuntimeError("candidate native service path resolution failed")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "candidate native service path resolution failed"
        ) from exc
    if not isinstance(value, str) or not value:
        raise RuntimeError("candidate native service path resolution failed")
    return value


def _native_systemd_spec(
    runtime: Path,
    home: Path,
    scope: str,
    owner: str,
) -> tuple[list[str], dict[str, str]]:
    encoded = _runtime_value(
        runtime,
        home,
        "import base64;"
        "from hermes_cli.gateway import generate_systemd_unit;"
        "unit=generate_systemd_unit("
        f"system={scope == 'system'},run_as_user={owner!r}"
        ");"
        "_proof_emit(base64.b64encode(unit.encode()).decode())",
    )
    try:
        return _systemd_definition_spec(base64.b64decode(encoded, validate=True))
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError("candidate systemd launch spec is invalid") from exc


def _native_launchd_spec(
    runtime: Path,
    home: Path,
) -> tuple[list[str], dict[str, str]]:
    raw = _runtime_value(
        runtime,
        home,
        "import json;"
        "import hermes_cli.gateway as g;"
        "h=str(g.get_hermes_home().resolve());"
        "p=g._profile_arg(h);"
        "v=g._detect_venv_dir();"
        "a=[g.get_python_path(),'-m','hermes_cli.main'];"
        "a.extend(p.split() if p else []);"
        "a.extend(['gateway','run','--replace']);"
        "e={'HERMES_HOME':h,'VIRTUAL_ENV':str(v or g.PROJECT_ROOT/'venv')};"
        "_proof_emit(json.dumps({'argv':a,'environment':e},separators=(',',':')))",
    )
    try:
        payload = json.loads(raw)
        argv = payload["argv"]
        environment = payload["environment"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("candidate launchd launch spec is invalid") from exc
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(value, str) for value in argv)
        or not isinstance(environment, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        )
    ):
        raise RuntimeError("candidate launchd launch spec is invalid")
    return argv, _service_environment(
        [f"{key}={value}" for key, value in environment.items()]
    )


def _prove_launchd(
    data: bytes,
    home: Path,
    runtime: Path,
    owner: str,
    expected_argv: list[str],
    expected_environment: dict[str, str],
    current_user: str | None = None,
) -> None:
    payload = plistlib.loads(data)
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    if (
        not isinstance(environment, dict)
        or not isinstance(arguments, list)
        or not arguments
        or arguments != expected_argv
        or _service_environment(
            [
                f"{key}={value}"
                for key, value in environment.items()
                if key in RELEVANT_SERVICE_ENV
            ]
        )
        != expected_environment
        or not _inside_runtime(arguments[0], runtime)
        or owner.casefold() != (current_user or _current_identity()[0]).casefold()
    ):
        raise RuntimeError("launchd definition does not match the proven runtime owner")


def _launchd_block(text: str, name: str) -> list[str]:
    lines = text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line.strip() == f"{name} = {{"
    ]
    if len(starts) != 1:
        raise RuntimeError(f"loaded launchd {name} is ambiguous")
    values: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.strip() == "}":
            return values
        values.append(line.strip())
    raise RuntimeError(f"loaded launchd {name} is incomplete")


def _launchd_field(text: str, name: str) -> str:
    values = [
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith(f"{name} = ")
    ]
    if len(values) != 1 or not values[0]:
        raise RuntimeError(f"loaded launchd {name} is ambiguous")
    return values[0]


def _launchd_environment_block(text: str, name: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    for line in _launchd_block(text, name):
        if "=>" not in line:
            raise RuntimeError(f"loaded launchd {name} is invalid")
        key, value = (part.strip() for part in line.split("=>", 1))
        if not key or key in environment:
            raise RuntimeError(f"loaded launchd {name} is ambiguous")
        environment[key] = value
    return environment


def _loaded_launchd_state(
    label: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    uid: int | None = None,
    launchctl: str | None = None,
) -> tuple[str, str]:
    proven_uid = uid if uid is not None else os.getuid()
    domain = f"gui/{proven_uid}"
    executable = launchctl or shutil.which("launchctl")
    if executable is None:
        raise RuntimeError("launchctl is unavailable")
    result = run(
        [executable, "print", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("loaded launchd service query failed")
    environment_result = run(
        [
            executable,
            "asuser",
            str(proven_uid),
            executable,
            "getenv",
            "PYTHONPATH",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if environment_result.returncode:
        raise RuntimeError("launchd domain environment query failed")
    return result.stdout, environment_result.stdout.rstrip("\r\n")


def _prove_loaded_launchd(
    text: str,
    domain_pythonpath: str,
    definition: Path,
    runtime: Path,
    expected_argv: list[str],
    expected_environment: dict[str, str],
    label: str,
    uid: int | None = None,
) -> None:
    domain = f"gui/{uid if uid is not None else os.getuid()}"
    headers = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(f"{domain}/{label} = ")
    ]
    if headers != [f"{domain}/{label} = {{"]:
        raise RuntimeError("loaded launchd service identity is ambiguous")
    if _lexical(Path(_launchd_field(text, "path"))) != definition:
        raise RuntimeError("loaded launchd service is not the native definition")
    arguments = _launchd_block(text, "arguments")
    program = _launchd_field(text, "program")
    if (
        arguments != expected_argv
        or program != expected_argv[0]
        or not _inside_runtime(program, runtime)
    ):
        raise RuntimeError(
            "loaded launchd service does not match the pinned launch spec"
        )
    environment = _launchd_environment_block(text, "environment")
    relevant_environment = _service_environment(
        [
            f"{key}={value}"
            for key, value in environment.items()
            if key in RELEVANT_SERVICE_ENV
        ]
    )
    if relevant_environment != expected_environment:
        raise RuntimeError(
            "loaded launchd environment does not match the pinned launch spec"
        )
    pythonpath_sources = {
        "loaded launchd environment PYTHONPATH": environment.get(
            "PYTHONPATH", ""
        ),
        "inherited launchd PYTHONPATH": _launchd_environment_block(
            text, "inherited environment"
        ).get("PYTHONPATH", ""),
        "default launchd PYTHONPATH": _launchd_environment_block(
            text, "default environment"
        ).get("PYTHONPATH", ""),
        "launchd domain PYTHONPATH": domain_pythonpath,
    }
    for source, value in pythonpath_sources.items():
        _prove_pythonpath_source(
            value,
            runtime,
            os.pathsep,
            source=source,
        )


def _first_windows_executable(command: str) -> str:
    command = command.strip()
    if command.startswith('"'):
        match = re.match(r'^"([^"]+)"(?:\s|$)', command)
        if match:
            return match.group(1)
    return command.split(None, 1)[0] if command else ""


def _vbs_literal(raw: str) -> str:
    return raw.replace('""', '"')


def _windows_path(value: str) -> str:
    return ntpath.normcase(ntpath.abspath(value.strip('"')))


def _windows_equal(left: str, right: str) -> bool:
    return _windows_path(left) == _windows_path(right)


def _windows_gateway_executable(command: str) -> str:
    executable = _first_windows_executable(command)
    if not executable:
        return ""
    remainder = command.strip()[len(executable) :]
    if command.strip().startswith('"'):
        remainder = command.strip()[len(executable) + 2 :]
    if re.fullmatch(
        r'\s+-m\s+hermes_cli\.main'
        r'(?:\s+--profile\s+(?:"[^"]+"|\S+))?'
        r"\s+gateway\s+run\s*",
        remainder,
        flags=re.IGNORECASE,
    ) is None:
        return ""
    return executable


def _windows_launch_spec(runtime: Path) -> tuple[str, str, tuple[str, ...]]:
    python = _runtime_python(runtime)
    venv = python.parent.parent
    windowed = python.with_name(python.stem + "w" + python.suffix)
    config: dict[str, str] = {}
    try:
        for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                config[key.strip().lower()] = value.strip()
    except OSError:
        pass
    site_packages = venv / "Lib" / "site-packages"
    base_pythonw = Path(config.get("home", "")) / "pythonw.exe"
    if "uv" in config and config.get("home") and base_pythonw.is_file():
        if not site_packages.is_dir():
            raise RuntimeError("candidate site-packages are missing")
        return str(base_pythonw), str(venv), (str(runtime), str(site_packages))
    if not windowed.is_file():
        windowed = python
    return str(windowed), str(venv), (str(runtime),)


def _cmd_environment(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(
            r'\s*set\s+"([A-Za-z_][A-Za-z0-9_]*)=(.*)"\s*',
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        key = match.group(1).upper()
        if key in values:
            raise RuntimeError("Windows CMD environment is ambiguous")
        values[key] = match.group(2)
    return values


def _vbs_environment(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(
            r'\s*env\.Item\("([A-Za-z_][A-Za-z0-9_]*)"\)\s*=\s*'
            r'"((?:""|[^"])*)"\s*',
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        key = match.group(1).upper()
        if key == "PYTHONPATH":
            continue
        if key in values:
            raise RuntimeError("Windows VBS environment is ambiguous")
        values[key] = _vbs_literal(match.group(2))
    return values


def _vbs_pythonpath(text: str) -> str:
    inherited_reads = [
        line
        for line in text.splitlines()
        if re.fullmatch(
            r'\s*existing_pp\s*=\s*env\.Item\("PYTHONPATH"\)\s*',
            line,
            flags=re.IGNORECASE,
        )
    ]
    static_values: list[str] = []
    inherited_values: list[str] = []
    for line in text.splitlines():
        match = re.fullmatch(
            r'\s*env\.Item\("PYTHONPATH"\)\s*=\s*'
            r'"((?:""|[^"])*)"\s*(?:&\s*(existing_pp))?\s*',
            line,
            flags=re.IGNORECASE,
        )
        if match:
            target = inherited_values if match.group(2) else static_values
            target.append(_vbs_literal(match.group(1)).removesuffix(";"))
    if (
        len(inherited_reads) != 1
        or len(static_values) != 1
        or len(inherited_values) != 1
        or static_values != inherited_values
    ):
        raise RuntimeError("Windows VBS PYTHONPATH is ambiguous")
    return static_values[0]


def _prove_windows_pythonpath(
    rendered: str,
    runtime: Path,
    required: tuple[str, ...],
) -> None:
    _prove_pythonpath_source(
        rendered,
        runtime,
        ";",
        windows=True,
        source="Windows PYTHONPATH",
    )
    entries = rendered.split(";")
    normalized = {_windows_path(entry) for entry in entries}
    if any(_windows_path(path) not in normalized for path in required):
        raise RuntimeError("Windows PYTHONPATH omits candidate launch paths")


def _windows_sid_string(sid: object) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    rendered = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
        raise RuntimeError("Windows SID cannot be resolved")
    try:
        value = rendered.value
        if not value:
            raise RuntimeError("Windows SID cannot be resolved")
        return value
    finally:
        kernel32.LocalFree(rendered)


def _windows_process_sid() -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise RuntimeError("Windows process token cannot be inspected")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, 1, None, 0, ctypes.byref(size)
        )
        if ctypes.get_last_error() != 122 or not size.value:
            raise RuntimeError("Windows process token identity is unavailable")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            size.value,
            ctypes.byref(size),
        ):
            raise RuntimeError("Windows process token identity is unavailable")

        class TokenUser(ctypes.Structure):
            _fields_ = [
                ("sid", wintypes.LPVOID),
                ("attributes", wintypes.DWORD),
            ]

        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        return _windows_sid_string(user.sid)
    finally:
        kernel32.CloseHandle(token)


def _windows_account_sid(owner: str) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if re.fullmatch(r"S-\d+(?:-\d+)+", owner, flags=re.IGNORECASE):
        sid_pointer = wintypes.LPVOID()
        if not advapi32.ConvertStringSidToSidW(
            owner, ctypes.byref(sid_pointer)
        ):
            raise RuntimeError("scheduled-task principal cannot be resolved")
        try:
            return _windows_sid_string(sid_pointer)
        finally:
            kernel32.LocalFree(sid_pointer)
    advapi32.LookupAccountNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.LookupAccountNameW.restype = wintypes.BOOL
    sid_size = wintypes.DWORD()
    domain_size = wintypes.DWORD()
    sid_type = wintypes.DWORD()
    advapi32.LookupAccountNameW(
        None,
        owner,
        None,
        ctypes.byref(sid_size),
        None,
        ctypes.byref(domain_size),
        ctypes.byref(sid_type),
    )
    if ctypes.get_last_error() != 122 or not sid_size.value:
        raise RuntimeError("scheduled-task principal cannot be resolved")
    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(max(domain_size.value, 1))
    if not advapi32.LookupAccountNameW(
        None,
        owner,
        sid,
        ctypes.byref(sid_size),
        domain,
        ctypes.byref(domain_size),
        ctypes.byref(sid_type),
    ):
        raise RuntimeError("scheduled-task principal cannot be resolved")
    return _windows_sid_string(sid)


def _prove_windows_operator(
    owner: str,
    *,
    process_sid: str | None = None,
    owner_sid: str | None = None,
) -> None:
    operator = process_sid or _windows_process_sid()
    principal = owner_sid or _windows_account_sid(owner)
    pattern = r"S-\d+(?:-\d+)+"
    if (
        re.fullmatch(pattern, operator, flags=re.IGNORECASE) is None
        or re.fullmatch(pattern, principal, flags=re.IGNORECASE) is None
        or operator.casefold() != principal.casefold()
    ):
        raise RuntimeError(
            "Windows operator does not match the scheduled-task principal"
        )


def _literal_windows_registry_pythonpath(
    value: object,
    kind: object,
    string_kind: object,
) -> str:
    if (
        not isinstance(value, str)
        or kind != string_kind
        or "%" in value
        or "!" in value
    ):
        raise RuntimeError("Windows registry PYTHONPATH is not literal")
    return value


def _windows_pythonpath_sources(
    owner: str,
    *,
    process_sid: str | None = None,
    owner_sid: str | None = None,
) -> dict[str, str]:
    if winreg is None:
        raise RuntimeError("Windows registry access is unavailable")
    _prove_windows_operator(
        owner,
        process_sid=process_sid,
        owner_sid=owner_sid,
    )
    sources = {"process": os.environ.get("PYTHONPATH", "")}
    locations = (
        ("user", winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            "system",
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    for label, root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, kind = winreg.QueryValueEx(key, "PYTHONPATH")
        except FileNotFoundError:
            value = ""
            kind = winreg.REG_SZ
        except OSError as exc:
            raise RuntimeError("Windows PYTHONPATH source query failed") from exc
        sources[label] = _literal_windows_registry_pythonpath(
            value,
            kind,
            winreg.REG_SZ,
        )
    return sources


def _prove_windows_inherited_pythonpath(
    sources: dict[str, str],
    runtime: Path,
) -> None:
    if set(sources) != {"process", "user", "system"}:
        raise RuntimeError("Windows PYTHONPATH sources are incomplete")
    for value in sources.values():
        if not isinstance(value, str) or "%" in value or "!" in value:
            raise RuntimeError("Windows PYTHONPATH source is unresolved")
        _prove_pythonpath_source(
            value,
            runtime,
            ";",
            windows=True,
            source="inherited Windows PYTHONPATH",
        )


def _prove_windows_launchers(
    cmd_data: bytes,
    vbs_data: bytes,
    home: Path,
    runtime: Path,
    expected_python: str | None = None,
    venv_dir: str | None = None,
    required_pythonpath: tuple[str, ...] | None = None,
    inherited_pythonpaths: dict[str, str] | None = None,
    service_owner: str | None = None,
) -> None:
    cmd = cmd_data.decode("utf-8")
    vbs = vbs_data.decode("utf-8")
    if expected_python is None or venv_dir is None or required_pythonpath is None:
        expected_python, venv_dir, required_pythonpath = _windows_launch_spec(
            runtime
        )
    cmd_env = _cmd_environment(cmd)
    vbs_env = _vbs_environment(vbs)
    if (
        cmd_env.get("HERMES_HOME") != str(home)
        or vbs_env.get("HERMES_HOME") != str(home)
        or not _windows_equal(cmd_env.get("VIRTUAL_ENV", ""), venv_dir)
        or not _windows_equal(vbs_env.get("VIRTUAL_ENV", ""), venv_dir)
    ):
        raise RuntimeError("Windows launchers do not bind the proven profile")
    cmd_pythonpath = cmd_env.get("PYTHONPATH", "")
    suffix = ";%PYTHONPATH%"
    if not cmd_pythonpath.upper().endswith(suffix):
        raise RuntimeError("Windows CMD PYTHONPATH is invalid")
    cmd_static = cmd_pythonpath[: -len(suffix)]
    vbs_static = _vbs_pythonpath(vbs)
    if cmd_static != vbs_static:
        raise RuntimeError("Windows launcher PYTHONPATH values differ")
    _prove_windows_pythonpath(cmd_static, runtime, required_pythonpath)
    if inherited_pythonpaths is None:
        if service_owner is None:
            raise RuntimeError("Windows service owner is required")
        inherited_pythonpaths = _windows_pythonpath_sources(service_owner)
    _prove_windows_inherited_pythonpath(
        inherited_pythonpaths,
        runtime,
    )
    command_lines = [
        line
        for line in cmd.splitlines()
        if " -m hermes_cli.main " in line and " gateway run" in line
    ]
    vbs_runs = [
        _vbs_literal(match.group(1))
        for line in vbs.splitlines()
        if (
            match := re.fullmatch(
                r'\s*sh\.Run\s+"((?:""|[^"])*)"\s*,\s*0\s*,\s*False\s*',
                line,
                flags=re.IGNORECASE,
            )
        )
    ]
    if (
        len(command_lines) != 1
        or len(vbs_runs) != 1
        or not _windows_equal(
            _windows_gateway_executable(command_lines[0]), expected_python
        )
        or not _windows_equal(
            _windows_gateway_executable(vbs_runs[0]), expected_python
        )
    ):
        raise RuntimeError("Windows launchers do not match the pinned launch spec")


def _task_proof(task_xml: str, vbs_path: Path, owner: str) -> None:
    root = ET.fromstring(task_xml)
    values = {
        name: [
            (element.text or "").strip()
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == name
        ]
        for name in ("Command", "Arguments", "UserId")
    }
    if (
        len(values["Command"]) != 1
        or values["Command"][0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        != "wscript.exe"
        or len(values["Arguments"]) != 1
        or len(values["UserId"]) != 1
        or values["UserId"][0].casefold() != owner.casefold()
    ):
        raise RuntimeError("scheduled task does not match the proven owner")
    match = re.fullmatch(
        r'\s*//B\s+//Nologo\s+"([^"]+)"\s*',
        values["Arguments"][0],
        flags=re.IGNORECASE,
    )
    if not match or ntpath.normcase(ntpath.abspath(match.group(1))) != ntpath.normcase(
        ntpath.abspath(str(vbs_path))
    ):
        raise RuntimeError("scheduled task does not target the generated launcher")


def _query_windows_task(task_name: str) -> str:
    executable = shutil.which("schtasks.exe") or shutil.which("schtasks")
    if executable is None:
        raise RuntimeError("schtasks.exe is unavailable")
    result = subprocess.run(
        [executable, "/Query", "/TN", task_name, "/XML"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    output = result.stdout or b""
    encoding = (
        "utf-16"
        if output.startswith((b"\xff\xfe", b"\xfe\xff"))
        else locale.getpreferredencoding(False) or "utf-8"
    )
    if result.returncode:
        raise RuntimeError("scheduled task query failed")
    return output.decode(encoding, errors="strict")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--restore-backup", type=Path)
    parser.add_argument("--restore-runtime-binding", type=Path)
    parser.add_argument(
        "--prove-kind", choices=("systemd", "launchd", "windows")
    )
    parser.add_argument("--definition", type=Path)
    parser.add_argument("--scope", choices=("user", "system"))
    parser.add_argument("--service-owner")
    parser.add_argument("--cmd-launcher", type=Path)
    parser.add_argument("--vbs-launcher", type=Path)
    parser.add_argument("--task-name")
    args = parser.parse_args()

    home = _lexical(args.hermes_home)
    _validate_path(home)
    if not home.is_dir():
        raise ValueError("HERMES_HOME does not exist")
    if args.restore_backup and args.restore_runtime_binding:
        parser.error("choose one rollback operation")
    if args.restore_backup:
        _restore_backup(home, _lexical(args.restore_backup))
        print(json.dumps({"ok": True, "restored": True}, indent=2))
        return 0
    if args.restore_runtime_binding:
        _restore_runtime_binding(home, _lexical(args.restore_runtime_binding))
        print(json.dumps({"ok": True, "runtime_binding_restored": True}, indent=2))
        return 0
    if args.runtime_dir is None:
        parser.error("--runtime-dir is required")
    runtime = _lexical(args.runtime_dir)
    _validate_path(runtime)
    if not runtime.is_dir():
        raise ValueError("candidate runtime does not exist")
    if not args.prove_kind:
        backup = _bind_environment(home)
        print(
            json.dumps(
                {
                    "ok": True,
                    "changed": backup is not None,
                    "backup": str(backup) if backup else None,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.service_owner:
        parser.error("service proof requires --service-owner")
    binding_path: Path
    binding_backup: Path
    if args.prove_kind in {"systemd", "launchd"}:
        if not args.definition:
            parser.error("POSIX service proof requires --definition")
        definition = _lexical(args.definition)
        _validate_path(definition)
        if not definition.is_file():
            raise ValueError("native service definition is missing")
        if args.prove_kind == "systemd":
            if not args.scope:
                parser.error("systemd proof requires --scope")
            expected = _lexical(
                Path(
                    _runtime_value(
                        runtime,
                        home,
                        "from hermes_cli.gateway import get_systemd_unit_path;"
                        "_proof_emit(str(get_systemd_unit_path("
                        f"system={args.scope == 'system'})))",
                    )
                )
            )
            if definition != expected:
                raise RuntimeError("systemd definition is not the native path")
            expected_argv, expected_environment = _native_systemd_spec(
                runtime,
                home,
                args.scope,
                args.service_owner,
            )
            _prove_systemd(
                definition.read_bytes(),
                home,
                runtime,
                args.scope,
                args.service_owner,
                expected_argv,
                expected_environment,
            )
            service = _runtime_value(
                runtime,
                home,
                "from hermes_cli.gateway import get_service_name;"
                "_proof_emit(str(get_service_name()))",
            )
            properties = _systemd_properties(service, args.scope)
            manager_environment = _systemd_manager_environment(args.scope)
            _prove_effective_systemd(
                properties,
                definition,
                home,
                runtime,
                args.scope,
                args.service_owner,
                expected_argv,
                expected_environment,
                manager_environment=manager_environment,
            )
            binding_path, binding_backup = _write_runtime_binding(
                home,
                runtime,
                service_kind=f"systemd-{args.scope}",
                service_owner=args.service_owner,
                definition_path=definition,
                definition_bytes=definition.read_bytes(),
            )
        else:
            expected = _lexical(
                Path(
                    _runtime_value(
                        runtime,
                        home,
                        "from hermes_cli.gateway import get_launchd_plist_path;"
                        "_proof_emit(str(get_launchd_plist_path()))",
                    )
                )
            )
            if definition != expected:
                raise RuntimeError("launchd definition is not the native path")
            expected_argv, expected_environment = _native_launchd_spec(
                runtime,
                home,
            )
            _prove_launchd(
                definition.read_bytes(),
                home,
                runtime,
                args.service_owner,
                expected_argv,
                expected_environment,
            )
            label = _runtime_value(
                runtime,
                home,
                "from hermes_cli.gateway import get_launchd_label;"
                "_proof_emit(str(get_launchd_label()))",
            )
            loaded, domain_pythonpath = _loaded_launchd_state(label)
            _prove_loaded_launchd(
                loaded,
                domain_pythonpath,
                definition,
                runtime,
                expected_argv,
                expected_environment,
                label,
            )
            binding_path, binding_backup = _write_runtime_binding(
                home,
                runtime,
                service_kind="launchd-user",
                service_owner=args.service_owner,
                definition_path=definition,
                definition_bytes=definition.read_bytes(),
            )
    else:
        if not args.cmd_launcher or not args.vbs_launcher or not args.task_name:
            parser.error(
                "Windows proof requires both launchers and --task-name"
            )
        cmd = _lexical(args.cmd_launcher)
        vbs = _lexical(args.vbs_launcher)
        _validate_path(cmd)
        _validate_path(vbs)
        expected_cmd = _lexical(
            Path(
                _runtime_value(
                    runtime,
                    home,
                    "from hermes_cli.gateway_windows import get_task_script_path;"
                    "_proof_emit(str(get_task_script_path()))",
                )
            )
        )
        expected_task = _runtime_value(
            runtime,
            home,
            "from hermes_cli.gateway_windows import get_task_name;"
            "_proof_emit(str(get_task_name()))",
        )
        if (
            cmd != expected_cmd
            or vbs != expected_cmd.with_suffix(".vbs")
            or args.task_name.casefold() != expected_task.casefold()
            or cmd.stem.casefold() != vbs.stem.casefold()
            or cmd.suffix.casefold() != ".cmd"
            or vbs.suffix.casefold() != ".vbs"
            or not cmd.is_file()
            or not vbs.is_file()
        ):
            raise RuntimeError("Windows launchers do not match the proven profile")
        task_xml = _query_windows_task(args.task_name)
        _task_proof(task_xml, vbs, args.service_owner)
        inherited_pythonpaths = _windows_pythonpath_sources(
            args.service_owner
        )
        _prove_windows_launchers(
            cmd.read_bytes(),
            vbs.read_bytes(),
            home,
            runtime,
            *_windows_launch_spec(runtime),
            inherited_pythonpaths=inherited_pythonpaths,
            service_owner=args.service_owner,
        )
        binding_path, binding_backup = _write_runtime_binding(
            home,
            runtime,
            service_kind="windows-scheduled-task",
            service_owner=args.service_owner,
            definition_bytes=task_xml.encode("utf-8"),
            launcher_paths=(cmd, vbs),
        )
    print(
        json.dumps(
            {
                "ok": True,
                "service_proven": args.prove_kind,
                "runtime_binding": str(binding_path),
                "runtime_binding_rollback": str(binding_backup),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
