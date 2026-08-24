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
    if hermes_home.name == "doc":
        return "hermes-doc.service"
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
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service = unit_dir / f"{WATCHDOG_NAME}.service"
    timer = unit_dir / f"{WATCHDOG_NAME}.timer"
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
            "AccuracySec=15s",
            "Persistent=true",
            f"Unit={WATCHDOG_NAME}.service",
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
            ["systemctl", "--user", "enable", "--now", f"{WATCHDOG_NAME}.timer"],
            dry_run=dry_run,
        ),
    ]
    return {
        "kind": "systemd-user-timer",
        "unit": f"{WATCHDOG_NAME}.timer",
        "gateway_supervisor_unit": supervisor_unit,
        "files": [str(service), str(timer)],
        "commands": commands,
    }


def install_macos(
    argv: list[str], supervisor_unit: str, *, dry_run: bool
) -> dict[str, Any]:
    destination = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
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
        destination.write_bytes(plistlib.dumps(payload, sort_keys=False))
    domain = f"gui/{os.getuid()}"
    if not dry_run:
        subprocess.run(
            ["/bin/launchctl", "bootout", domain, str(destination)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    command = run_checked(
        ["/bin/launchctl", "bootstrap", domain, str(destination)], dry_run=dry_run
    )
    return {
        "kind": "launchd",
        "unit": MAC_LABEL,
        "gateway_supervisor_unit": supervisor_unit,
        "files": [str(destination)],
        "commands": [command],
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


def install(
    *,
    source: Path,
    hermes_home: Path,
    hermes_python: Path,
    supervisor_unit: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    current_platform = platform_key()
    unit = supervisor_unit or detect_gateway_unit(hermes_home, current_platform)
    if not SAFE_UNIT.fullmatch(unit):
        raise ValueError(f"unsafe gateway supervisor unit: {unit!r}")
    destination = hermes_home / "bin" / source.name
    file_result = atomic_copy(source, destination, dry_run=dry_run)
    kind = {
        "linux": "systemd-user",
        "macos": "launchd",
        "windows": "windows-scheduled-task",
    }[current_platform]
    argv = watchdog_argv(hermes_python, destination, hermes_home, kind, unit)
    if current_platform == "linux":
        service = install_linux(argv, hermes_home, unit, dry_run=dry_run)
    elif current_platform == "macos":
        service = install_macos(argv, unit, dry_run=dry_run)
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
            and wrapper_status in {None, "idempotent"}
            else "installed"
        ),
        "generated_at": utc_now(),
        "platform": current_platform,
        "watchdog": service,
        "file": file_result,
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
