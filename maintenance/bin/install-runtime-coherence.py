#!/usr/bin/env python3
"""Install, verify, or roll back the runtime-coherence scheduler."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHECK_SOURCE = ROOT / "checks/agent-runtime-coherence.py"
TEMPLATES = {
    "macos": ROOT / "maintenance/launchd/com.hermes.runtime-coherence.plist.template",
    "linux_service": ROOT / "maintenance/systemd/hermes-runtime-coherence@.service",
    "linux_timer": ROOT / "maintenance/systemd/hermes-runtime-coherence@.timer",
    "windows": ROOT / "maintenance/windows/hermes-runtime-coherence-task.ps1.template",
}
TOKENS = (
    "AGENT_ID",
    "BASELINE_PYTHON",
    "BINDING_RECEIPT",
    "CHECK_PATH",
    "HERMES_HOME",
    "RECEIPT_PATH",
    "RUNTIME_PYTHON",
    "RUNTIME_ROOT",
    "RUNTIME_USER",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def platform_id(value: str | None = None) -> str:
    if value:
        return value
    return {"Darwin": "macos", "Windows": "windows"}.get(
        platform.system(), "linux"
    )


def safe_agent_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ValueError("agent id is not scheduler-safe")
    return value


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise ValueError(f"refusing symlink target: {path}")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def render(path: Path, values: dict[str, str]) -> bytes:
    text = path.read_text(encoding="utf-8")
    for token in TOKENS:
        text = text.replace(f"__{token}__", values[token])
    unresolved = sorted(set(re.findall(r"__[A-Z_]+__", text)))
    if unresolved:
        raise ValueError(f"unresolved scheduler tokens: {unresolved}")
    return text.encode()


def template_values(values: dict[str, str], system: str) -> dict[str, str]:
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("scheduler values cannot contain newlines")
    if system == "macos":
        return {key: html.escape(value) for key, value in values.items()}
    if system == "linux":
        return {
            key: value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            for key, value in values.items()
        }
    return {key: value.replace("'", "''") for key, value in values.items()}


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = safe_agent_id(args.agent_id)
    system = platform_id(args.platform)
    user_home = args.user_home.expanduser().resolve()
    hermes_home = args.home.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve()
    # Preserve the candidate-local venv entrypoint. Resolving the symlink turns
    # it into the host interpreter and silently drops the venv site-packages.
    runtime_python = args.runtime_python.expanduser().absolute()
    scheduler_python = args.scheduler_python.expanduser().absolute()
    if not runtime_root.is_dir():
        raise ValueError("runtime root is missing")
    for label, path in (
        ("runtime Python", runtime_python),
        ("scheduler Python", scheduler_python),
    ):
        if not path.is_file():
            raise ValueError(f"{label} is missing")
    try:
        scheduler_python.relative_to(runtime_root)
    except ValueError:
        pass
    else:
        raise ValueError(
            "scheduler Python must be outside the monitored runtime root"
        )
    receipt = (
        args.receipt.expanduser().resolve()
        if args.receipt
        else hermes_home / "state/health/runtime-coherence.json"
    )
    binding_receipt = (
        args.binding_receipt.expanduser().resolve()
        if args.binding_receipt
        else None
    )
    check_path = hermes_home / "bin/agent-runtime-coherence.py"
    runtime_user = args.runtime_user or getpass.getuser()
    values = {
        "AGENT_ID": agent_id,
        "BASELINE_PYTHON": str(scheduler_python),
        "BINDING_RECEIPT": str(binding_receipt) if binding_receipt else "",
        "CHECK_PATH": str(check_path),
        "HERMES_HOME": str(hermes_home),
        "RECEIPT_PATH": str(receipt),
        "RUNTIME_PYTHON": str(runtime_python),
        "RUNTIME_ROOT": str(runtime_root),
        "RUNTIME_USER": runtime_user,
    }
    rendered_values = template_values(values, system)
    if system == "macos":
        scheduler = [
            {
                "path": user_home
                / f"Library/LaunchAgents/com.hermes.runtime-coherence.{agent_id}.plist",
                "data": render(TEMPLATES["macos"], rendered_values),
                "mode": 0o600,
            }
        ]
        unit = f"com.hermes.runtime-coherence.{agent_id}"
    elif system == "linux":
        unit = f"hermes-runtime-coherence-{agent_id}"
        unit_root = user_home / ".config/systemd/user"
        scheduler = [
            {
                "path": unit_root / f"{unit}.service",
                "data": render(TEMPLATES["linux_service"], rendered_values),
                "mode": 0o600,
            },
            {
                "path": unit_root / f"{unit}.timer",
                "data": render(TEMPLATES["linux_timer"], rendered_values),
                "mode": 0o600,
            },
        ]
    else:
        unit = f"Hermes Runtime Coherence - {agent_id}"
        scheduler = [
            {
                "path": hermes_home
                / f"bin/install-runtime-coherence-{agent_id}.ps1",
                "data": render(TEMPLATES["windows"], rendered_values),
                "mode": 0o600,
            }
        ]
    # Keep the owning Linux timer cadence; only the service command changes.
    if system == "linux":
        timer = scheduler[1]["path"]
        if timer.is_file() and not timer.is_symlink():
            scheduler[1]["data"] = timer.read_bytes()
            scheduler[1]["mode"] = timer.stat().st_mode & 0o777
    state_dir = (
        args.state_dir.expanduser().resolve()
        if args.state_dir
        else hermes_home / f"state/runtime-coherence/install/{agent_id}"
    )
    return {
        "schema_version": 1,
        "agent_id": agent_id,
        "platform": system,
        "unit": unit,
        "check_path": check_path,
        "receipt": receipt,
        "binding_receipt": binding_receipt,
        "runtime_root": runtime_root,
        "runtime_python": runtime_python,
        "scheduler": scheduler,
        "state_dir": state_dir,
        "values": values,
    }


def command(plan: dict[str, Any], action: str) -> list[list[str]]:
    system = plan["platform"]
    unit = plan["unit"]
    scheduler = plan["scheduler"]
    if system == "macos":
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{unit}"
        if action == "remove":
            return [["launchctl", "bootout", target]]
        return [
            ["launchctl", "bootout", target],
            ["launchctl", "bootstrap", domain, str(scheduler[0]["path"])],
            ["launchctl", "kickstart", "-k", target],
        ]
    if system == "linux":
        timer = f"{unit}.timer"
        if action == "remove":
            return [["systemctl", "--user", "disable", "--now", timer],
                    ["systemctl", "--user", "stop", f"{unit}.service"]]
        return [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", timer],
            ["systemctl", "--user", "start", f"{unit}.service"],
        ]
    task = unit
    if action == "remove":
        return [
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false",
            ]
        ]
    return [
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scheduler[0]["path"]),
        ]
    ]


def run_commands(commands: list[list[str]], *, ignore_first: bool = False, ignore_absent: bool = False) -> None:
    for index, argv in enumerate(commands):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=45)
        if proc.returncode and ignore_absent and argv[:2] == ["systemctl", "--user"]:
            state = subprocess.run(["systemctl", "--user", "show", argv[-1],
                                    "--property=LoadState", "--value"],
                                   capture_output=True, text=True, timeout=30)
            if state.returncode == 0 and state.stdout.strip() == "not-found":
                continue
        if proc.returncode and not (ignore_first and index == 0):
            detail = (proc.stderr or proc.stdout or "")[-500:]
            raise RuntimeError(f"scheduler command failed: {argv[0]}: {detail}")


def scheduler_snapshot(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Capture enough native scheduler state to make rollback non-activating."""
    if plan["platform"] == "linux":
        state = {}
        for action in ("is-enabled", "is-active"):
            result = subprocess.run(["systemctl", "--user", action, plan["unit"] + ".timer"],
                                    capture_output=True, text=True, timeout=30)
            state[action] = result.stdout.strip()
        if not state["is-enabled"]:
            result = subprocess.run(["systemctl", "--user", "show", plan["unit"] + ".timer",
                                     "--property=LoadState", "--value"],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip() == "not-found":
                state["is-enabled"] = "not-found"
        if (state["is-enabled"], state["is-active"]) not in {
            ("enabled", "active"), ("disabled", "inactive"), ("not-found", "inactive"),
            ("enabled", "inactive"), ("disabled", "active"),
        }:
            raise RuntimeError(f"unsupported prior timer state: {state}")
        return state
    if plan["platform"] == "macos":
        target = f"gui/{os.getuid()}/{plan['unit']}"
        result = subprocess.run(["launchctl", "print", target], capture_output=True,
                                text=True, timeout=30)
        if result.returncode == 0:
            return {"loaded": True}
        detail = (result.stderr or result.stdout or "").lower()
        if "could not find service" in detail:
            return {"loaded": False}
        raise RuntimeError(
            "cannot determine prior launchd state; refusing scheduler mutation: "
            f"{detail[-500:]}"
        )
    task = plan["unit"]
    script = (
        "$task = Get-ScheduledTask -TaskName '" + task + "' -ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { @{ exists = $false } | ConvertTo-Json -Compress; exit 0 }; "
        "$xml = Export-ScheduledTask -TaskName '" + task + "'; "
        "@{ exists = $true; state = $task.State.ToString(); "
        "xml = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($xml)) } | "
        "ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"cannot determine prior scheduled-task state: {(result.stderr or result.stdout)[-500:]}")
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("cannot parse prior scheduled-task state") from exc
    if state == {"exists": False}:
        return state
    if (
        not isinstance(state, dict)
        or state.get("exists") is not True
        or state.get("state") not in {"Disabled", "Ready", "Running", "Queued"}
        or not isinstance(state.get("xml"), str)
    ):
        raise RuntimeError(f"unsupported prior scheduled-task state: {state!r}")
    try:
        base64.b64decode(state["xml"], validate=True)
    except ValueError as exc:
        raise RuntimeError("prior scheduled-task definition is invalid") from exc
    return state


def restore_windows_task(plan: dict[str, Any], state: dict[str, Any]) -> None:
    if state == {"exists": False}:
        return
    if (
        state.get("exists") is not True
        or state.get("state") not in {"Disabled", "Ready", "Running", "Queued"}
        or not isinstance(state.get("xml"), str)
    ):
        raise RuntimeError("scheduled-task rollback state is missing or invalid")
    try:
        base64.b64decode(state["xml"], validate=True)
    except ValueError as exc:
        raise RuntimeError("scheduled-task rollback definition is invalid") from exc
    script = (
        "$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
        + state["xml"] + "')); Register-ScheduledTask -TaskName '" + plan["unit"]
        + "' -Xml $xml -Force"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    run_commands([[
        "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
    ]])


def snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    # Resolve state before touching rollback authority or any managed file.
    scheduler_state = scheduler_snapshot(plan)
    state_dir = plan["state_dir"]
    state_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = Path(tempfile.mkdtemp(prefix="backup-", dir=state_dir))
    if (state_dir / "rollback.json").is_file():
        shutil.copy2(state_dir / "rollback.json", backup_dir / "prior-rollback.json")
    rows = []
    paths = [(plan["check_path"], "check")] + [
        (row["path"], "scheduler") for row in plan["scheduler"]
    ] + [(plan["receipt"], "receipt")]
    for index, (path, kind) in enumerate(paths):
        if path.is_symlink():
            raise ValueError(f"refusing symlink target: {path}")
        backup = backup_dir / str(index)
        existed = path.is_file()
        if existed:
            shutil.copy2(path, backup)
        rows.append(
            {
                "path": str(path),
                "kind": kind,
                "existed": existed,
                "backup": str(backup),
                "mode": backup.stat().st_mode & 0o777 if existed else None,
                "sha256": hashlib.sha256(backup.read_bytes()).hexdigest() if existed else None,
            }
        )
    record = {"schema_version": 1, "generated_at": utc_now(), "files": rows,
              "scheduler_state": scheduler_state}
    atomic_write(state_dir / "rollback.json", (json.dumps(record, indent=2) + "\n").encode(), 0o600)
    return record


def restore(plan: dict[str, Any]) -> None:
    rollback = plan["state_dir"] / "rollback.json"
    if not rollback.is_file():
        raise ValueError("rollback record is missing")
    record = json.loads(rollback.read_text(encoding="utf-8"))
    def finish_rollback():
        backup_dir = Path(record["files"][0]["backup"]).parent
        atomic_write(backup_dir / "rolled-back.json", rollback.read_bytes(), 0o600)
        prior = backup_dir / "prior-rollback.json"
        if prior.is_file():
            atomic_write(rollback, prior.read_bytes(), 0o600)
        else:
            rollback.unlink()
    # Validate the whole backup before stopping jobs or restoring the first file.
    for row in record["files"]:
        if row["existed"]:
            raw = Path(row["backup"]).read_bytes()
            if row.get("sha256") and hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise RuntimeError(f"rollback backup digest changed: {row['path']}")
    before = record.get("scheduler_state")
    if plan["platform"] == "macos" and (
        not isinstance(before, dict) or not isinstance(before.get("loaded"), bool)
    ):
        raise RuntimeError("macOS rollback scheduler state is missing or unknown")
    if plan["platform"] == "windows" and not isinstance(before, dict):
        raise RuntimeError("Windows rollback scheduler state is missing or unknown")
    run_commands(command(plan, "remove"), ignore_first=plan["platform"] != "linux", ignore_absent=True)
    for row in record["files"]:
        path = Path(row["path"])
        if row["existed"]:
            raw = Path(row["backup"]).read_bytes()
            if row.get("sha256") and hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise RuntimeError(f"rollback backup digest changed: {path}")
            atomic_write(path, raw, int(row["mode"]))
        elif path.exists():
            path.unlink()
    if plan["platform"] == "linux" and isinstance(record.get("scheduler_state"), dict):
        run_commands([["systemctl", "--user", "daemon-reload"]])
        before = record["scheduler_state"]
        if before["is-enabled"] == "enabled":
            run_commands([["systemctl", "--user", "enable", plan["unit"] + ".timer"]])
        if before["is-active"] == "active":
            run_commands([["systemctl", "--user", "start", plan["unit"] + ".timer"]])
        finish_rollback()
        return
    if plan["platform"] == "macos":
        if before["loaded"]:
            run_commands(command(plan, "apply"), ignore_first=True)
    elif plan["platform"] == "windows":
        restore_windows_task(plan, before)
    elif any(
        row["kind"] == "scheduler" and row["existed"]
        for row in record["files"]
    ):
        run_commands(
            command(plan, "apply"),
            ignore_first=plan["platform"] == "macos",
        )
    finish_rollback()


def expected_files(plan: dict[str, Any]) -> list[tuple[Path, bytes, int]]:
    return [(plan["check_path"], CHECK_SOURCE.read_bytes(), 0o755)] + [
        (row["path"], row["data"], row["mode"]) for row in plan["scheduler"]
    ]


def files_current(plan: dict[str, Any]) -> bool:
    return all(
        path.is_file() and not path.is_symlink() and path.read_bytes() == data
        and (os.name == "nt" or path.stat().st_mode & 0o777 == mode)
        for path, data, mode in expected_files(plan)
    )


def probe(plan: dict[str, Any]) -> None:
    values = plan["values"]
    proc = subprocess.run(
        [
            values["BASELINE_PYTHON"],
            str(plan["check_path"]),
            "--home",
            values["HERMES_HOME"],
            "--agent-id",
            values["AGENT_ID"],
            "--receipt",
            values["RECEIPT_PATH"],
            *(
                ["--binding-receipt", values["BINDING_RECEIPT"]]
                if values["BINDING_RECEIPT"]
                else []
            ),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode:
        raise RuntimeError(f"runtime coherence probe failed: {(proc.stderr or proc.stdout)[-500:]}")


def verify(plan: dict[str, Any], *, check_scheduler: bool = True) -> dict[str, Any]:
    drift = []
    for path, data, mode in expected_files(plan):
        if not path.is_file() or path.read_bytes() != data:
            drift.append(str(path))
        elif os.name != "nt" and path.stat().st_mode & 0o777 != mode:
            drift.append(f"{path}:mode")
    receipt = plan["receipt"]
    payload = json.loads(receipt.read_text()) if receipt.is_file() else {}
    try:
        generated = datetime.fromisoformat(
            str(payload.get("generated_at") or "").replace("Z", "+00:00")
        )
        fresh = (datetime.now(timezone.utc) - generated).total_seconds() <= 1800
    except ValueError:
        fresh = False
    if (
        payload.get("ok") is not True
        or payload.get("agent_id") != plan["agent_id"]
        or payload.get("hermes_home") != plan["values"]["HERMES_HOME"]
        or payload.get("runtime_root") != str(plan["runtime_root"])
        or payload.get("runtime_python") != str(plan["runtime_python"])
        or not fresh
    ):
        drift.append(str(receipt))
    if check_scheduler:
        if plan["platform"] == "macos":
            check = ["launchctl", "print", f"gui/{os.getuid()}/{plan['unit']}"]
        elif plan["platform"] == "linux":
            check = ["systemctl", "--user", "is-enabled", f"{plan['unit']}.timer"]
        else:
            check = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Get-ScheduledTask -TaskName '{plan['unit']}' | Out-Null",
            ]
        if subprocess.run(check, capture_output=True, timeout=30).returncode:
            drift.append(f"scheduler:{plan['unit']}")
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "agent_id": plan["agent_id"],
        "platform": plan["platform"],
        "ok": not drift,
        "drift": drift,
        "receipt": str(receipt),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply", "verify", "rollback"))
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--runtime-python", required=True, type=Path)
    parser.add_argument(
        "--scheduler-python",
        type=Path,
        default=Path(getattr(sys, "_base_executable", sys.executable)),
    )
    parser.add_argument("--runtime-user")
    parser.add_argument("--user-home", type=Path, default=Path.home())
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--binding-receipt", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--platform", choices=("macos", "linux", "windows"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        plan = build_plan(args)
        if args.action == "plan":
            result = {
                "ok": True,
                "platform": plan["platform"],
                "unit": plan["unit"],
                "files": [str(path) for path, _, _ in expected_files(plan)],
            }
        elif args.action == "apply" and files_current(plan):
            # Refresh health without replacing the last meaningful rollback.
            # Already-current scheduler files are never rewritten on retry.
            probe(plan)
            run_commands(command(plan, "apply"), ignore_first=plan["platform"] == "macos")
            result = verify(plan)
        elif args.action == "apply":
            backup = snapshot(plan)
            if plan["platform"] == "linux":
                run_commands(command(plan, "remove"), ignore_absent=True)
            # A failed CAS owns no file mutation: never restore over external
            # edits. Keep this scheduler stopped and report the explicit hold.
            for row in backup["files"]:
                if row["kind"] == "receipt":
                    continue
                path = Path(row["path"])
                actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                if (path.is_symlink() or actual != row["sha256"]
                        or (row["existed"] and os.name != "nt"
                            and path.stat().st_mode & 0o777 != row["mode"])):
                    raise RuntimeError(f"coherence preimage changed; scheduler held: {path}")
            try:
                for path, data, mode in expected_files(plan):
                    atomic_write(path, data, mode)
                probe(plan)
                run_commands(
                    command(plan, "apply"),
                    ignore_first=plan["platform"] == "macos",
                )
                result = verify(plan)
                if not result["ok"]:
                    raise RuntimeError(f"installed scheduler did not verify: {result['drift']}")
            except Exception:
                restore(plan)
                raise
        elif args.action == "verify":
            result = verify(plan)
        else:
            restore(plan)
            result = {"ok": True, "rolled_back": True, "unit": plan["unit"]}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
