#!/usr/bin/env python3
"""Install the canonical five-minute gateway watchdog for this host."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WATCHDOG_NAME = "hermes-gateway-watchdog-client"
MAC_LABEL = "com.hermes.gateway-watchdog-client"
WINDOWS_TASK = "HermesGatewayWatchdog"
SAFE_UNIT = re.compile(r"^[A-Za-z0-9_.@-]+$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def platform_key() -> str:
    name = platform.system().lower()
    if name == "darwin":
        return "macos"
    if name.startswith("win"):
        return "windows"
    return "linux"


def detect_gateway_unit(hermes_home: Path, current_platform: str) -> str:
    config = hermes_home / "config.yaml"
    text = config.read_text(encoding="utf-8", errors="ignore") if config.is_file() else ""
    if current_platform == "windows":
        match = re.search(r"^\s*windows_task_name\s*:\s*['\"]?([^'\"#\r\n]+)", text, re.MULTILINE)
        return (match.group(1).strip() if match else "HermesGateway")
    if current_platform == "macos":
        for unit in ("ai.hermes.gateway", "com.hermes.gateway"):
            if (Path.home() / "Library" / "LaunchAgents" / f"{unit}.plist").is_file():
                return unit
        return "ai.hermes.gateway"
    if hermes_home.parent.name == "profiles":
        if not re.fullmatch(r"[A-Za-z0-9_-]+", hermes_home.name):
            raise ValueError("unsafe Hermes profile name")
        return f"hermes-{hermes_home.name}.service"
    return "hermes-gateway.service"


def atomic_copy(source: Path, destination: Path, *, dry_run: bool) -> dict[str, Any]:
    source_sha = sha256(source)
    if destination.is_file() and sha256(destination) == source_sha:
        return {"status": "idempotent", "destination": str(destination), "sha256": source_sha}
    backup = None
    if destination.exists():
        backup = destination.with_name(
            f"{destination.name}.bak-watchdog-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if backup:
            shutil.copy2(destination, backup)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        try:
            shutil.copy2(source, temporary)
            temporary.chmod(0o755)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "status": "would_install" if dry_run else "installed",
        "destination": str(destination),
        "sha256": source_sha,
        "backup": str(backup) if backup else None,
    }


def atomic_write_text(
    content: str, destination: Path, *, dry_run: bool
) -> dict[str, Any]:
    """Write a generated launcher atomically with the same receipt shape as a copy."""
    import hashlib

    payload = content.encode("utf-8")
    payload_sha = hashlib.sha256(payload).hexdigest()
    if destination.is_file() and destination.read_bytes() == payload:
        return {
            "status": "idempotent",
            "destination": str(destination),
            "sha256": payload_sha,
        }
    backup = None
    if destination.exists():
        backup = destination.with_name(
            f"{destination.name}.bak-watchdog-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if backup:
            shutil.copy2(destination, backup)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "status": "would_install" if dry_run else "installed",
        "destination": str(destination),
        "sha256": payload_sha,
        "backup": str(backup) if backup else None,
    }


def watchdog_argv(
    python: Path, script: Path, hermes_home: Path, supervisor_kind: str, supervisor_unit: str
) -> list[str]:
    return [
        str(python),
        str(script),
        "--hermes-home",
        str(hermes_home),
        "--supervisor-kind",
        supervisor_kind,
        "--supervisor-unit",
        supervisor_unit,
        "--heartbeat-max-age",
        "600",
    ]


def run_checked(command: list[str], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"command": command, "returncode": None}
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {(result.stderr or result.stdout).strip()[-500:]}"
        )
    return {"command": command, "returncode": result.returncode}


def install_linux(
    argv: list[str], hermes_home: Path, supervisor_unit: str, *, dry_run: bool
) -> dict[str, Any]:
    # Keep the existing Doc unit stable; other profiles need their own timer.
    name = WATCHDOG_NAME
    if hermes_home.parent.name == "profiles" and hermes_home.name != "doc":
        if not re.fullmatch(r"[A-Za-z0-9_-]+", hermes_home.name):
            raise ValueError("unsafe Hermes profile name")
        name += "-" + hermes_home.name
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service = unit_dir / f"{name}.service"
    timer = unit_dir / f"{name}.timer"
    service_text = "\n".join(
        [
            "[Unit]",
            "Description=Bounded Hermes gateway watchdog",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={shlex.join(argv)}",
            "",
        ]
    )
    timer_text = "\n".join(
        [
            "[Unit]",
            "Description=Run the bounded Hermes gateway watchdog every five minutes",
            "",
            "[Timer]",
            "OnBootSec=5min",
            "OnUnitActiveSec=5min",
            "OnCalendar=*-*-* *:0/5:00",
            "AccuracySec=15s",
            "Persistent=true",
            f"Unit={name}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    if not dry_run:
        unit_dir.mkdir(parents=True, exist_ok=True)
        service.write_text(service_text, encoding="utf-8")
        timer.write_text(timer_text, encoding="utf-8")
    commands = [
        run_checked(["systemctl", "--user", "daemon-reload"], dry_run=dry_run),
        run_checked(
            ["systemctl", "--user", "enable", "--now", f"{name}.timer"],
            dry_run=dry_run,
        ),
    ]
    return {
        "kind": "systemd-user-timer",
        "unit": f"{name}.timer",
        "gateway_supervisor_unit": supervisor_unit,
        "files": [str(service), str(timer)],
        "commands": commands,
    }


def install_macos(
    argv: list[str], supervisor_unit: str, *, dry_run: bool
) -> dict[str, Any]:
    destination = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if not dry_run:
        disabled = subprocess.run(
            ["/bin/launchctl", "print-disabled", domain],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if disabled.returncode:
            raise RuntimeError("cannot verify watchdog maintenance state")
        if re.search(rf'["\']?{re.escape(MAC_LABEL)}["\']?\s*=>\s*(?:true|disabled)\b', disabled.stdout):
            return {"kind": "launchd", "unit": MAC_LABEL,
                    "gateway_supervisor_unit": supervisor_unit,
                    "status": "paused", "files": [], "commands": []}
    payload = {
        "Label": MAC_LABEL,
        "ProgramArguments": argv,
        "StartInterval": 300,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(Path.home() / ".hermes" / "logs" / "gateway-watchdog-client.log"),
        "StandardErrorPath": str(Path.home() / ".hermes" / "logs" / "gateway-watchdog-client.err.log"),
    }
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(plistlib.dumps(payload, sort_keys=False).decode("utf-8"),
                          destination, dry_run=False)
    if not dry_run:
        subprocess.run(
            ["/bin/launchctl", "bootout", domain, str(destination)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    enabled = run_checked(
        ["/bin/launchctl", "enable", f"{domain}/{MAC_LABEL}"], dry_run=dry_run
    )
    command = run_checked(
        ["/bin/launchctl", "bootstrap", domain, str(destination)], dry_run=dry_run
    )
    return {
        "kind": "launchd",
        "unit": MAC_LABEL,
        "gateway_supervisor_unit": supervisor_unit,
        "files": [str(destination)],
        "commands": [enabled, command],
    }


def install_windows(
    argv: list[str], hermes_home: Path, supervisor_unit: str, *, dry_run: bool
) -> dict[str, Any]:
    existing = discover_equivalent_windows_watchdogs(hermes_home)
    if len(existing) > 1:
        raise RuntimeError(
            "multiple enabled Hermes gateway watchdog tasks already exist: "
            + ", ".join(existing)
        )
    if existing:
        return {
            "kind": "windows-scheduled-task",
            "unit": existing[0],
            "gateway_supervisor_unit": supervisor_unit,
            "files": [],
            "commands": [],
            "preserved_existing": True,
        }
    # schtasks.exe rejects /TR values longer than 261 characters. Candidate
    # interpreter and runtime paths can legitimately exceed that on Windows,
    # so keep the exact command in a stable host-local wrapper and schedule the
    # short wrapper invocation instead.
    wrapper = hermes_home / "bin" / f"{WATCHDOG_NAME}.cmd"
    wrapper_result = atomic_write_text(
        "@echo off\r\n" + subprocess.list2cmdline(argv) + "\r\n",
        wrapper,
        dry_run=dry_run,
    )
    action = subprocess.list2cmdline(["cmd.exe", "/d", "/c", str(wrapper)])
    if len(action) > 261:
        raise RuntimeError("Windows watchdog wrapper action exceeds schtasks /TR limit")
    command = [
        "schtasks.exe",
        "/Create",
        "/TN",
        WINDOWS_TASK,
        "/SC",
        "MINUTE",
        "/MO",
        "5",
        "/TR",
        action,
        "/RL",
        "HIGHEST",
        "/F",
    ]
    result = run_checked(command, dry_run=dry_run)
    return {
        "kind": "windows-scheduled-task",
        "unit": WINDOWS_TASK,
        "gateway_supervisor_unit": supervisor_unit,
        "files": [str(wrapper)],
        "wrapper": wrapper_result,
        "commands": [result],
    }


def discover_equivalent_windows_watchdogs(hermes_home: Path) -> list[str]:
    """Return enabled five-minute watchdog tasks already bound to this profile."""
    try:
        listing = subprocess.run(
            ["schtasks.exe", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if listing.returncode:
        return []
    names = []
    for row in csv.reader(io.StringIO(listing.stdout)):
        if not row:
            continue
        name = row[0].strip().lstrip("\\")
        if (
            name
            and name != WINDOWS_TASK
            and name.lower().endswith("gatewaywatchdog")
            and SAFE_UNIT.fullmatch(name)
        ):
            names.append(name)
    profile = str(hermes_home).replace("/", "\\").rstrip("\\").lower()
    equivalent = []
    for name in sorted(set(names)):
        try:
            query = subprocess.run(
                ["schtasks.exe", "/Query", "/TN", name, "/XML"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if query.returncode:
                continue
            root = ET.fromstring(query.stdout)
        except (OSError, subprocess.SubprocessError, ET.ParseError):
            continue
        values = {node.tag.rsplit("}", 1)[-1]: (node.text or "") for node in root.iter()}
        enabled = values.get("Enabled", "true").strip().lower() != "false"
        action = " ".join((values.get("Command", ""), values.get("Arguments", ""))).lower()
        interval = values.get("Interval", "").strip().upper()
        if enabled and profile in action and "watchdog" in action and interval == "PT5M":
            equivalent.append(name)
    return equivalent


def macos_system_gateway_unit(hermes_home: Path) -> str | None:
    binding_path = hermes_home / "state" / "runtime-binding.json"
    if not binding_path.is_file():
        return None
    binding = json.loads(binding_path.read_text())
    service = binding.get("service") or {}
    if service.get("kind") != "launchd-daemon":
        return None
    import pwd

    owner = pwd.getpwuid(os.getuid()).pw_name
    directory = Path("/Library/LaunchDaemons")
    gateway_path = Path(str(service.get("definition_path") or ""))
    if (gateway_path.parent != directory or gateway_path.suffix != ".plist"
            or not SAFE_UNIT.fullmatch(gateway_path.stem)
            or gateway_path.is_symlink() or gateway_path.stat().st_uid != 0
            or gateway_path.stat().st_mode & 0o022
            or sha256(gateway_path) != service.get("definition_sha256")):
        raise ValueError("system gateway definition does not match the runtime binding")
    gateway = plistlib.loads(gateway_path.read_bytes())
    unit = gateway_path.stem
    if (gateway.get("Label") != unit or gateway.get("UserName") != owner
            or gateway.get("RunAtLoad") is not True
            or not (gateway.get("KeepAlive") is True
                    or gateway.get("KeepAlive") == {"SuccessfulExit": False})):
        raise ValueError("system gateway owner or KeepAlive contract changed")
    run_checked(["/bin/launchctl", "print", "system/" + unit], dry_run=False)
    return unit


def existing_macos_system_watchdog(
    hermes_home: Path, script: Path, unit: str
) -> dict[str, Any] | None:
    import pwd

    owner = pwd.getpwuid(os.getuid()).pw_name
    directory = Path("/Library/LaunchDaemons")
    expected = watchdog_argv(Path("python"), script, hermes_home, "launchd-system-observe", unit)[1:]
    matches = []
    for path in directory.glob("*.plist"):
        try:
            if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                continue
            document = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):
            continue
        if not isinstance(document, dict):
            continue
        argv = document.get("ProgramArguments") or []
        if (not isinstance(argv, list) or len(argv) != len(expected) + 1
                or argv[1:] != expected):
            continue
        label = str(document.get("Label") or "")
        if (label != path.stem or not SAFE_UNIT.fullmatch(label)
                or document.get("UserName") != owner
                or document.get("StartInterval") != 300
                or not isinstance(argv[0], str) or not Path(argv[0]).is_file()):
            raise ValueError("existing system watchdog contract changed")
        matches.append(label)
    if len(matches) > 1:
        raise ValueError("system gateway has multiple existing observer watchdogs")
    if not matches:
        path = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022):
            raise ValueError("system gateway requires an existing owned watchdog")
        document = plistlib.loads(path.read_bytes())
        argv = document.get("ProgramArguments") or []
        legacy = watchdog_argv(Path("python"), script, hermes_home, "launchd",
                               detect_gateway_unit(hermes_home, "macos"))[1:]
        if (document.get("Label") != MAC_LABEL
                or document.get("RunAtLoad") is not True
                or document.get("StartInterval") != 300
                or not isinstance(argv, list) or len(argv) != len(expected) + 1
                or argv[1:] not in (legacy, expected)
                or not isinstance(argv[0], str) or not Path(argv[0]).is_file()):
            raise ValueError("existing user watchdog contract changed")
        return None
    run_checked(["/bin/launchctl", "print", "system/" + matches[0]], dry_run=False)
    return {"kind": "launchd-daemon", "unit": matches[0],
            "gateway_supervisor_unit": unit, "files": [], "commands": [],
            "preserved_existing": True}


def install(
    *,
    source: Path,
    hermes_home: Path,
    hermes_python: Path,
    supervisor_unit: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    current_platform = platform_key()
    destination = hermes_home / "bin" / source.name
    system_unit = (macos_system_gateway_unit(hermes_home)
                   if current_platform == "macos" else None)
    existing_system = (existing_macos_system_watchdog(hermes_home, destination, system_unit)
                       if system_unit else None)
    unit = system_unit or supervisor_unit or detect_gateway_unit(hermes_home, current_platform)
    if supervisor_unit and supervisor_unit != unit:
        raise ValueError("requested gateway differs from the existing system watchdog")
    if not SAFE_UNIT.fullmatch(unit):
        raise ValueError(f"unsafe gateway supervisor unit: {unit!r}")
    dependencies = []
    if "client-medic.json" in source.read_text():
        names = ["client_medic.py", "client-medic-run.py"]
        if current_platform == "linux":
            # Both scheduled repair callers must share the opted-in ledger.
            names.append("client-selfheal-heartbeat.sh")
        dependency_sources = [source.with_name(name) for name in names]
        if not all(path.is_file() and not path.is_symlink() for path in dependency_sources):
            raise ValueError("watchdog medic dependencies missing")
        dependencies = [atomic_copy(path, hermes_home / "bin" / path.name, dry_run=dry_run)
                        for path in dependency_sources]
    file_result = atomic_copy(source, destination, dry_run=dry_run)
    kind = {
        "linux": "systemd-user",
        "macos": "launchd-system-observe" if system_unit else "launchd",
        "windows": "windows-scheduled-task",
    }[current_platform]
    argv = watchdog_argv(hermes_python, destination, hermes_home, kind, unit)
    if current_platform == "linux":
        service = install_linux(argv, hermes_home, unit, dry_run=dry_run)
    elif current_platform == "macos":
        service = existing_system or install_macos(argv, unit, dry_run=dry_run)
    else:
        service = install_windows(argv, hermes_home, unit, dry_run=dry_run)
    wrapper_status = (service.get("wrapper") or {}).get("status")
    return {
        "ok": True,
        "status": (
            "would_install"
            if dry_run
            else "idempotent"
            if file_result["status"] == "idempotent"
            and all(item["status"] == "idempotent" for item in dependencies)
            and wrapper_status in {None, "idempotent"}
            else "installed"
        ),
        "generated_at": utc_now(),
        "platform": current_platform,
        "watchdog": service,
        "file": file_result,
        "dependencies": dependencies,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--supervisor-unit", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = install(
            source=args.source,
            hermes_home=args.hermes_home,
            hermes_python=args.hermes_python,
            supervisor_unit=args.supervisor_unit or None,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
