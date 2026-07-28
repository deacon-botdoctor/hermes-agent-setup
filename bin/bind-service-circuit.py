#!/usr/bin/env python3
"""Bind profile-scoped circuit state and prove native service ownership."""

from __future__ import annotations

import argparse
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

KEY = "HERMES_CODEX_401_CIRCUIT_STATE"


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


def _occurrence_count(data: bytes) -> int:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("profile environment is not valid UTF-8") from exc
    pattern = re.compile(
        rf"(?m)^\s*(?:export\s+)?{re.escape(KEY)}\s*="
    )
    return len(pattern.findall(text))


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


def _loaded_value(home: Path, runtime: Path) -> str | None:
    python = _runtime_python(runtime)
    code = (
        "import os,sys;"
        "from pathlib import Path;"
        "from hermes_cli.env_loader import load_hermes_dotenv;"
        "load_hermes_dotenv(hermes_home=Path(sys.argv[1]),"
        "project_env=Path(sys.argv[2]));"
        f"sys.stdout.write(os.environ.get({KEY!r},''))"
    )
    env = os.environ.copy()
    env.pop(KEY, None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(python),
            "-c",
            code,
            str(home),
            str(runtime / ".botdoctor-no-project-env"),
        ],
        cwd=runtime,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("candidate environment loader verification failed")
    return result.stdout


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
        or receipt.get("schema_version") != 1
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
    after_hash = _receipt_hash(receipt.get("after_sha256"), "after_sha256")
    env_path = home / ".env"
    _validate_path(env_path)
    current = env_path.read_bytes() if env_path.is_file() else None
    current_mode = env_path.stat().st_mode & 0o777 if current is not None else None
    valid_after = (
        current is not None
        and _sha256(current) == after_hash
        and current_mode == after_mode
    )
    valid_before = (
        existed
        and current is not None
        and before is not None
        and _sha256(current) == _sha256(before)
        and current_mode == before_mode
    ) or (not existed and current is None)
    if not valid_after and not (
        receipt["status"] == "pending" and valid_before
    ):
        raise RuntimeError("profile environment changed after binding")
    if before is None:
        env_path.unlink(missing_ok=True)
    else:
        _atomic_bytes(env_path, before, before_mode)


def _bind_environment(
    home: Path,
    runtime: Path,
    verify: Callable[[], str | None],
) -> Path | None:
    env_path = home / ".env"
    _validate_path(env_path)
    if env_path.exists() and not env_path.is_file():
        raise RuntimeError("profile environment is not a regular file")
    before = env_path.read_bytes() if env_path.is_file() else b""
    existed = env_path.is_file()
    before_mode = env_path.stat().st_mode & 0o777 if existed else 0
    expected = str(home / "state" / "codex-401-circuit.json")
    occurrences = _occurrence_count(before)
    if occurrences > 1:
        raise RuntimeError("profile circuit binding is duplicated")
    if occurrences == 1:
        if verify() != expected:
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
        "schema_version": 1,
        "kind": "botdoctor_profile_environment_binding",
        "status": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hermes_home": str(home),
        "existed": existed,
        "before_sha256": _sha256(before) if existed else None,
        "before_mode": format(before_mode, "04o"),
        "after_sha256": _sha256(after),
        "after_mode": format(after_mode, "04o"),
        "rollback": str(backup),
    }
    receipt_path = backup / "receipt.json"
    _atomic_bytes(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    try:
        _atomic_bytes(env_path, after, after_mode)
        if verify() != expected:
            raise RuntimeError("candidate environment loader rejected profile binding")
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


def _prove_systemd(
    data: bytes,
    home: Path,
    runtime: Path,
    scope: str,
    owner: str,
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
    executable = shlex.split(_single_line(text, "ExecStart"))[0].lstrip("-@+!:")
    if not _inside_runtime(executable, runtime):
        raise RuntimeError("systemd unit does not target the exact candidate")
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


def _current_identity() -> tuple[str, Path]:
    try:
        if pwd is None:
            raise AttributeError
        entry = pwd.getpwuid(os.getuid())
        return entry.pw_name, Path(entry.pw_dir)
    except (AttributeError, KeyError):
        return getpass.getuser(), Path.home()


def _runtime_value(runtime: Path, home: Path, code: str) -> str:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(_runtime_python(runtime)), "-c", code],
        cwd=runtime,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 1 or not lines[0]:
        raise RuntimeError("candidate native service path resolution failed")
    return lines[0]


def _prove_launchd(
    data: bytes,
    home: Path,
    runtime: Path,
    owner: str,
    current_user: str | None = None,
) -> None:
    payload = plistlib.loads(data)
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    if (
        not isinstance(environment, dict)
        or environment.get("HERMES_HOME") != str(home)
        or not isinstance(arguments, list)
        or not arguments
        or not _inside_runtime(str(arguments[0]), runtime)
        or owner.casefold() != (current_user or _current_identity()[0]).casefold()
    ):
        raise RuntimeError("launchd definition does not match the proven runtime owner")


def _first_windows_executable(command: str) -> str:
    command = command.strip()
    if command.startswith('"'):
        match = re.match(r'^"([^"]+)"(?:\s|$)', command)
        if match:
            return match.group(1)
    return command.split(None, 1)[0] if command else ""


def _vbs_literal(raw: str) -> str:
    return raw.replace('""', '"')


def _prove_windows_launchers(
    cmd_data: bytes,
    vbs_data: bytes,
    home: Path,
    runtime: Path,
) -> None:
    cmd = cmd_data.decode("utf-8")
    vbs = vbs_data.decode("utf-8")
    cmd_home = f'set "HERMES_HOME={home}"'
    vbs_home = f'env.Item("HERMES_HOME") = "{str(home).replace(chr(34), chr(34) * 2)}"'
    if cmd.splitlines().count(cmd_home) != 1 or vbs.splitlines().count(vbs_home) != 1:
        raise RuntimeError("Windows launchers do not bind the proven profile")
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
        or not _inside_runtime(
            _first_windows_executable(command_lines[0]), runtime, windows=True
        )
        or not _inside_runtime(
            _first_windows_executable(vbs_runs[0]), runtime, windows=True
        )
    ):
        raise RuntimeError("Windows launchers do not target the exact candidate")


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
    if args.restore_backup:
        _restore_backup(home, _lexical(args.restore_backup))
        print(json.dumps({"ok": True, "restored": True}, indent=2))
        return 0
    if args.runtime_dir is None:
        parser.error("--runtime-dir is required")
    runtime = _lexical(args.runtime_dir)
    _validate_path(runtime)
    if not runtime.is_dir():
        raise ValueError("candidate runtime does not exist")
    if not args.prove_kind:
        backup = _bind_environment(
            home, runtime, lambda: _loaded_value(home, runtime)
        )
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
                        f"print(get_systemd_unit_path(system={args.scope == 'system'}))",
                    )
                )
            )
            if definition != expected:
                raise RuntimeError("systemd definition is not the native path")
            _prove_systemd(
                definition.read_bytes(),
                home,
                runtime,
                args.scope,
                args.service_owner,
            )
        else:
            expected = _lexical(
                Path(
                    _runtime_value(
                        runtime,
                        home,
                        "from hermes_cli.gateway import get_launchd_plist_path;"
                        "print(get_launchd_plist_path())",
                    )
                )
            )
            if definition != expected:
                raise RuntimeError("launchd definition is not the native path")
            _prove_launchd(
                definition.read_bytes(),
                home,
                runtime,
                args.service_owner,
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
                    "print(get_task_script_path())",
                )
            )
        )
        expected_task = _runtime_value(
            runtime,
            home,
            "from hermes_cli.gateway_windows import get_task_name;"
            "print(get_task_name())",
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
        _prove_windows_launchers(cmd.read_bytes(), vbs.read_bytes(), home, runtime)
        _task_proof(_query_windows_task(args.task_name), vbs, args.service_owner)
    print(
        json.dumps(
            {"ok": True, "service_proven": args.prove_kind},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
