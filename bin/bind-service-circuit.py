#!/usr/bin/env python3
"""Persist and verify profile-scoped Codex circuit state in native services."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import locale
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable


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


def _atomic_bytes(path: Path, data: bytes, mode: int) -> None:
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
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _systemd_quote(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("systemd environment value contains unsafe characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _systemd_binding(
    definition: bytes, home: Path, runtime: Path
) -> bytes:
    text = definition.decode("utf-8")
    expected_home = f'Environment="HERMES_HOME={home}"'
    if text.count(expected_home) != 1:
        raise RuntimeError("systemd unit does not bind the proven HERMES_HOME")
    exec_starts = [
        line for line in text.splitlines() if line.startswith("ExecStart=")
    ]
    if len(exec_starts) != 1 or str(runtime) not in exec_starts[0]:
        raise RuntimeError("systemd unit does not target the candidate runtime")
    circuit = _systemd_quote(
        str(home / "state" / "codex-401-circuit.json")
    )
    return (
        "[Service]\n"
        f'Environment="HERMES_CODEX_401_CIRCUIT_STATE={circuit}"\n'
    ).encode()


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _launchd_binding(
    definition: bytes, home: Path, runtime: Path
) -> bytes:
    payload = plistlib.loads(definition)
    if not isinstance(payload, dict):
        raise ValueError("launchd plist root is invalid")
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    label = payload.get("Label")
    if (
        not isinstance(environment, dict)
        or environment.get("HERMES_HOME") != str(home)
        or not isinstance(arguments, list)
        or not arguments
        or not isinstance(label, str)
        or not label.startswith("ai.hermes.gateway")
    ):
        raise RuntimeError("launchd plist does not match the proven profile")
    program = _lexical(Path(str(arguments[0])))
    if not _path_within(program, runtime):
        raise RuntimeError("launchd plist does not target the candidate runtime")
    environment["HERMES_CODEX_401_CIRCUIT_STATE"] = str(
        home / "state" / "codex-401-circuit.json"
    )
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _normalized_windows(value: str) -> str:
    return value.replace("/", "\\").casefold()


def _quote_vbs(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("VBScript value contains a newline")
    return '"' + value.replace('"', '""') + '"'


def _insert_launcher_environment(
    text: str, anchor_fragment: str, variable_fragment: str, rendered: str
) -> str:
    lines = [
        line for line in text.splitlines() if variable_fragment not in line
    ]
    matches = [
        index for index, line in enumerate(lines) if anchor_fragment in line
    ]
    if len(matches) != 1:
        raise RuntimeError("gateway launcher environment anchor is ambiguous")
    lines.insert(matches[0] + 1, rendered)
    return "\r\n".join(lines) + "\r\n"


def _windows_launcher_bindings(
    cmd_data: bytes,
    vbs_data: bytes,
    home: Path,
    runtime: Path,
) -> tuple[bytes, bytes]:
    home_text = str(home)
    circuit = str(home / "state" / "codex-401-circuit.json")
    if any(character in circuit for character in ('\r', '\n', '"', "%")):
        raise ValueError("Windows circuit path contains unsafe characters")
    cmd = cmd_data.decode("utf-8")
    vbs = vbs_data.decode("utf-8")
    if f'set "HERMES_HOME={home_text}"' not in cmd:
        raise RuntimeError("CMD launcher does not bind the proven HERMES_HOME")
    vbs_home = (
        f'env.Item("HERMES_HOME") = {_quote_vbs(home_text)}'
    )
    if vbs_home not in vbs:
        raise RuntimeError("VBS launcher does not bind the proven HERMES_HOME")
    normalized_runtime = _normalized_windows(str(runtime))
    if (
        normalized_runtime not in _normalized_windows(cmd)
        or normalized_runtime not in _normalized_windows(vbs)
    ):
        raise RuntimeError("Windows launchers do not target the candidate runtime")
    cmd_bound = _insert_launcher_environment(
        cmd,
        'set "HERMES_HOME=',
        "HERMES_CODEX_401_CIRCUIT_STATE",
        f'set "HERMES_CODEX_401_CIRCUIT_STATE={circuit}"',
    )
    vbs_bound = _insert_launcher_environment(
        vbs,
        'env.Item("HERMES_HOME")',
        'env.Item("HERMES_CODEX_401_CIRCUIT_STATE")',
        (
            'env.Item("HERMES_CODEX_401_CIRCUIT_STATE") = '
            f"{_quote_vbs(circuit)}"
        ),
    )
    return cmd_bound.encode(), vbs_bound.encode()


def _task_targets_vbs(task_xml: str, vbs_path: Path) -> bool:
    root = ET.fromstring(task_xml)
    commands = [
        element.text or ""
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "Command"
    ]
    arguments = [
        element.text or ""
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "Arguments"
    ]
    if (
        len(commands) != 1
        or commands[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        != "wscript.exe"
        or len(arguments) != 1
    ):
        return False
    match = re.fullmatch(
        r'\s*//B\s+//Nologo\s+"([^"]+)"\s*',
        arguments[0],
        flags=re.IGNORECASE,
    )
    return bool(
        match
        and _normalized_windows(match.group(1))
        == _normalized_windows(str(vbs_path))
    )


def _verify_windows_task(task_name: str, vbs_path: Path) -> None:
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
    if output.startswith((b"\xff\xfe", b"\xfe\xff")):
        task_xml = output.decode("utf-16")
    else:
        task_xml = output.decode(
            locale.getpreferredencoding(False) or "utf-8", errors="replace"
        )
    if result.returncode or not _task_targets_vbs(task_xml, vbs_path):
        raise RuntimeError(
            "scheduled task does not target the generated VBS launcher"
        )


def _receipt_mode(value: object, field: str) -> int:
    raw = str(value or "")
    if re.fullmatch(r"0[0-7]{3}", raw) is None:
        raise ValueError(f"service binding receipt has invalid {field}")
    return int(raw, 8)


def _receipt_hash(value: object, field: str) -> str:
    raw = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", raw) is None:
        raise ValueError(f"service binding receipt has invalid {field}")
    return raw


def _restore_memory(
    rows: list[tuple[Path, bytes | None, int]]
) -> None:
    for destination, before, mode in reversed(rows):
        _validate_path(destination)
        if before is None:
            destination.unlink(missing_ok=True)
        else:
            _atomic_bytes(destination, before, mode)


def _validate_restore_destination(kind: str, home: Path, destination: Path) -> None:
    if kind == "systemd":
        user_root = _lexical(Path.home() / ".config" / "systemd" / "user")
        system_root = Path("/etc/systemd/system")
        valid_parent = (
            destination.parent.parent in {user_root, system_root}
            and destination.parent.name.startswith("hermes-gateway")
            and destination.parent.name.endswith(".service.d")
        )
        if (
            not valid_parent
            or destination.name != "40-botdoctor-profile-state.conf"
        ):
            raise ValueError("service binding receipt has an invalid systemd path")
    elif kind == "launchd":
        launchd_root = _lexical(Path.home() / "Library" / "LaunchAgents")
        if (
            destination.parent != launchd_root
            or not destination.name.startswith("ai.hermes.gateway")
            or destination.suffix != ".plist"
        ):
            raise ValueError("service binding receipt has an invalid launchd path")
    elif kind == "windows":
        if (
            destination.parent != home / "gateway-service"
            or destination.suffix.casefold() not in {".cmd", ".vbs"}
        ):
            raise ValueError("service binding receipt has an invalid Windows path")
    else:
        raise ValueError("service binding receipt has an invalid binding kind")


def _apply_binding(
    home: Path,
    kind: str,
    files: dict[Path, tuple[bytes, int]],
    post_verify: Callable[[], None] | None = None,
) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / "state" / "service-binding-backups" / stamp
    _validate_path(backup)
    if backup.exists():
        raise RuntimeError(f"service binding backup path collision: {backup}")
    backup.mkdir(parents=True, mode=0o700)
    receipt_rows: list[dict[str, object]] = []
    for destination, (after, requested_mode) in files.items():
        _validate_path(destination)
        if destination.exists() and not destination.is_file():
            raise RuntimeError(
                f"service binding destination is not a file: {destination}"
            )
        if requested_mode < 0 or requested_mode > 0o777:
            raise ValueError("service binding mode is invalid")
        before = destination.read_bytes() if destination.is_file() else None
        before_mode = (
            destination.stat().st_mode & 0o777
            if before is not None
            else requested_mode
        )
        key = hashlib.sha256(str(destination).encode()).hexdigest()
        if before is not None:
            _atomic_bytes(backup / key, before, 0o600)
        receipt_rows.append(
            {
                "destination": str(destination),
                "existed": before is not None,
                "backup_key": key,
                "before_sha256": _sha256(before) if before is not None else None,
                "before_mode": format(before_mode, "04o"),
                "after_sha256": _sha256(after),
                "after_mode": format(requested_mode, "04o"),
            }
        )
    receipt = {
        "schema_version": 1,
        "kind": "botdoctor_service_circuit_binding",
        "status": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "binding_kind": kind,
        "hermes_home": str(home),
        "files": receipt_rows,
    }
    receipt_path = backup / "receipt.json"
    _atomic_bytes(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    try:
        for destination, (after, requested_mode) in files.items():
            _atomic_bytes(destination, after, requested_mode)
        for destination, (after, _requested_mode) in files.items():
            if not destination.is_file() or _sha256(
                destination.read_bytes()
            ) != _sha256(after):
                raise RuntimeError(f"service binding verification failed: {destination}")
        if post_verify:
            post_verify()
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


def _restore_backup(home: Path, backup: Path) -> int:
    root = home / "state" / "service-binding-backups"
    if not _path_within(backup, root):
        raise ValueError("service binding backup is outside HERMES_HOME")
    _validate_path(backup)
    receipt_path = backup / "receipt.json"
    _validate_path(receipt_path)
    if (
        not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _is_reparse_point(receipt_path)
    ):
        raise ValueError("service binding receipt is missing or unsafe")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("kind") != "botdoctor_service_circuit_binding"
        or receipt.get("status") not in {"pending", "completed"}
        or receipt.get("hermes_home") != str(home)
        or receipt.get("binding_kind")
        not in {"systemd", "launchd", "windows"}
        or not isinstance(receipt.get("files"), list)
    ):
        raise ValueError("service binding receipt is invalid")
    binding_kind = receipt["binding_kind"]
    status = receipt["status"]
    prepared: list[tuple[Path, bytes | None, int]] = []
    seen: set[Path] = set()
    for row in receipt["files"]:
        if not isinstance(row, dict):
            raise ValueError("service binding receipt file entry is invalid")
        destination = _lexical(Path(str(row.get("destination") or "")))
        _validate_path(destination)
        _validate_restore_destination(binding_kind, home, destination)
        if destination in seen:
            raise ValueError("service binding receipt has duplicate destinations")
        seen.add(destination)
        after_sha = _receipt_hash(row.get("after_sha256"), "after_sha256")
        after_mode = _receipt_mode(row.get("after_mode"), "after_mode")
        existed = row.get("existed")
        if not isinstance(existed, bool):
            raise ValueError("service binding receipt existed flag is invalid")
        before_mode = _receipt_mode(row.get("before_mode"), "before_mode")
        key = _receipt_hash(row.get("backup_key"), "backup_key")
        before: bytes | None = None
        if existed:
            before_sha = _receipt_hash(
                row.get("before_sha256"), "before_sha256"
            )
            saved = backup / key
            _validate_path(saved)
            if (
                not saved.is_file()
                or saved.is_symlink()
                or _is_reparse_point(saved)
            ):
                raise RuntimeError(f"service binding backup is missing: {saved}")
            before = saved.read_bytes()
            if _sha256(before) != before_sha:
                raise RuntimeError(f"service binding backup is corrupt: {saved}")
        elif row.get("before_sha256") is not None:
            raise ValueError("new service binding unexpectedly has a before hash")
        current = destination.read_bytes() if destination.is_file() else None
        current_mode = (
            destination.stat().st_mode & 0o777
            if current is not None
            else None
        )
        valid_current = (
            current is not None
            and _sha256(current) == after_sha
            and current_mode == after_mode
        )
        if status == "pending":
            valid_current = valid_current or (
                existed
                and current is not None
                and before is not None
                and _sha256(current) == _sha256(before)
                and current_mode == before_mode
            ) or (not existed and current is None)
        if not valid_current:
            raise RuntimeError(
                f"service definition changed after binding: {destination}"
            )
        prepared.append((destination, before, before_mode))
    destinations = [destination for destination, _before, _mode in prepared]
    if binding_kind in {"systemd", "launchd"} and len(destinations) != 1:
        raise ValueError("service binding receipt has the wrong file count")
    if binding_kind == "windows":
        suffixes = {destination.suffix.casefold() for destination in destinations}
        stems = {destination.stem.casefold() for destination in destinations}
        if len(destinations) != 2 or suffixes != {".cmd", ".vbs"} or len(stems) != 1:
            raise ValueError("service binding receipt has invalid Windows launchers")
    _restore_memory(prepared)
    return len(prepared)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument(
        "--kind", choices=("systemd", "launchd", "windows")
    )
    parser.add_argument("--definition", type=Path)
    parser.add_argument("--scope", choices=("user", "system"))
    parser.add_argument("--cmd-launcher", type=Path)
    parser.add_argument("--vbs-launcher", type=Path)
    parser.add_argument("--task-name")
    parser.add_argument("--restore-backup", type=Path)
    args = parser.parse_args()

    home = _lexical(args.hermes_home)
    _validate_path(home)
    if not home.is_dir():
        raise ValueError("HERMES_HOME does not exist")
    if args.restore_backup:
        restored = _restore_backup(home, _lexical(args.restore_backup))
        print(json.dumps({"ok": True, "restored_files": restored}, indent=2))
        return 0
    if not args.kind or not args.runtime_dir:
        parser.error("--kind and --runtime-dir are required for binding")
    runtime = _lexical(args.runtime_dir)
    _validate_path(runtime)
    if not runtime.is_dir():
        raise ValueError("candidate runtime does not exist")

    files: dict[Path, tuple[bytes, int]]
    post_verify: Callable[[], None] | None = None
    if args.kind == "systemd":
        if not args.definition or not args.scope:
            parser.error("systemd binding requires --definition and --scope")
        definition = _lexical(args.definition)
        _validate_path(definition)
        expected_parent = (
            Path("/etc/systemd/system")
            if args.scope == "system"
            else _lexical(Path.home() / ".config" / "systemd" / "user")
        )
        if (
            definition.parent != expected_parent
            or not definition.name.startswith("hermes-gateway")
            or definition.suffix != ".service"
            or not definition.is_file()
        ):
            raise RuntimeError("systemd definition does not match the selected scope")
        dropin = Path(str(definition) + ".d") / "40-botdoctor-profile-state.conf"
        files = {
            dropin: (
                _systemd_binding(definition.read_bytes(), home, runtime),
                0o644,
            )
        }
    elif args.kind == "launchd":
        if not args.definition:
            parser.error("launchd binding requires --definition")
        definition = _lexical(args.definition)
        _validate_path(definition)
        expected_parent = _lexical(Path.home() / "Library" / "LaunchAgents")
        if (
            definition.parent != expected_parent
            or not definition.name.startswith("ai.hermes.gateway")
            or definition.suffix != ".plist"
            or not definition.is_file()
        ):
            raise RuntimeError("launchd definition is outside the native user scope")
        mode = definition.stat().st_mode & 0o777
        files = {
            definition: (
                _launchd_binding(definition.read_bytes(), home, runtime),
                mode,
            )
        }
    else:
        if (
            not args.cmd_launcher
            or not args.vbs_launcher
            or not args.task_name
        ):
            parser.error(
                "Windows binding requires both launchers and --task-name"
            )
        cmd = _lexical(args.cmd_launcher)
        vbs = _lexical(args.vbs_launcher)
        _validate_path(cmd)
        _validate_path(vbs)
        expected_parent = home / "gateway-service"
        if (
            cmd.parent != expected_parent
            or vbs.parent != expected_parent
            or cmd.stem != vbs.stem
            or cmd.suffix.casefold() != ".cmd"
            or vbs.suffix.casefold() != ".vbs"
            or not cmd.is_file()
            or not vbs.is_file()
        ):
            raise RuntimeError("Windows launchers do not match the proven profile")
        _verify_windows_task(args.task_name, vbs)
        cmd_bound, vbs_bound = _windows_launcher_bindings(
            cmd.read_bytes(), vbs.read_bytes(), home, runtime
        )
        files = {
            cmd: (cmd_bound, cmd.stat().st_mode & 0o777),
            vbs: (vbs_bound, vbs.stat().st_mode & 0o777),
        }
        post_verify = lambda: _verify_windows_task(args.task_name, vbs)

    backup = _apply_binding(home, args.kind, files, post_verify)
    print(
        json.dumps(
            {
                "ok": True,
                "kind": args.kind,
                "backup": str(backup),
                "circuit_state": str(
                    home / "state" / "codex-401-circuit.json"
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
