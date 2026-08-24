#!/usr/bin/env python3
"""Bounded, cross-platform Hermes gateway watchdog.

The watchdog restarts only a missing/dead gateway, at most once per incident.
Transport staleness is reported for the central fleet reducer but never causes
a local restart by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
SAFE_UNIT = re.compile(r"^[A-Za-z0-9_.@-]+$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            UTC
        )
    except Exception:
        return None


def pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        if os.name == "nt":
            return windows_pid_alive(value)
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def windows_pid_alive(pid: int, kernel32: Any | None = None) -> bool:
    """Check a Windows PID without using ``os.kill(pid, 0)``.

    Unlike POSIX, CPython's Windows ``os.kill`` implementation can route signal
    0 through ``TerminateProcess``. Querying the process handle and exit code
    gives the watchdog a genuinely non-destructive liveness check instead.
    """
    import ctypes
    from ctypes import wintypes

    api = kernel32
    if api is None:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    still_active = 259
    handle = api.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        api.CloseHandle(handle)


def telegram_heartbeat(state: dict[str, Any]) -> str | None:
    telegram = ((state.get("platforms") or {}).get("telegram") or {})
    return telegram.get("last_successful_poll_at") or telegram.get("updated_at")


def classify_gateway(
    state: dict[str, Any],
    *,
    now: datetime,
    heartbeat_max_age: int,
) -> dict[str, Any]:
    pid = state.get("pid")
    gateway_state = str(state.get("gateway_state") or "missing")
    alive = pid_alive(pid)
    heartbeat = telegram_heartbeat(state)
    heartbeat_at = parse_iso(heartbeat)
    heartbeat_age = (
        max(0, int((now - heartbeat_at).total_seconds())) if heartbeat_at else None
    )
    if not state:
        health, reason = "outage", "gateway_state_missing"
    elif not alive:
        health, reason = "outage", "gateway_process_missing"
    elif gateway_state != "running":
        # A live gateway may report a transitional state while its owning
        # supervisor is already stopping/reloading it. A second watchdog must
        # not convert that graceful transition into a forceful duplicate
        # restart. Only a missing/dead process is restart-eligible.
        health, reason = "degraded", f"gateway_state_{gateway_state}"
    elif heartbeat_age is None or heartbeat_age > heartbeat_max_age:
        health, reason = "degraded", "telegram_heartbeat_stale"
    else:
        health, reason = "healthy", "healthy"
    return {
        "health": health,
        "reason": reason,
        "gateway_state": gateway_state,
        "pid": pid,
        "pid_alive": alive,
        "telegram_heartbeat_at": heartbeat,
        "telegram_heartbeat_age_seconds": heartbeat_age,
    }


def restart_command(kind: str, unit: str) -> list[str]:
    if not SAFE_UNIT.fullmatch(unit):
        raise ValueError(f"unsafe supervisor unit: {unit!r}")
    if kind == "systemd-user":
        return ["systemctl", "--user", "restart", unit]
    if kind == "launchd":
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{unit}"]
    if kind == "windows-scheduled-task":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Start-ScheduledTask -TaskName '{unit}'",
        ]
    raise ValueError(f"unsupported supervisor kind: {kind!r}")


def run_watchdog(
    *,
    hermes_home: Path,
    supervisor_kind: str,
    supervisor_unit: str,
    heartbeat_max_age: int,
    now: datetime | None = None,
    command_runner=subprocess.run,
) -> dict[str, Any]:
    observed_at = now or datetime.now(UTC)
    state_path = hermes_home / "gateway_state.json"
    incident_path = hermes_home / "state" / "gateway-watchdog-incident.json"
    intent_path = hermes_home / "state" / "gateway-restart-intent.json"
    receipt_path = hermes_home / "state" / "gateway-watchdog-client.json"
    gateway = load_json(state_path, {})
    observation = classify_gateway(
        gateway,
        now=observed_at,
        heartbeat_max_age=max(300, int(heartbeat_max_age)),
    )
    prior = load_json(incident_path, {})
    restart = {
        "eligible": observation["health"] == "outage",
        "attempted": False,
        "succeeded": None,
        "detail": None,
    }
    restart_intent = load_json(intent_path, {})

    if observation["health"] == "healthy":
        incident = {}
    elif observation["health"] == "degraded":
        incident = {
            "signature": observation["reason"],
            "first_seen_at": prior.get("first_seen_at") or utc_now(),
            "restart_attempted": False,
        }
    else:
        signature = f"{observation['reason']}:{observation.get('pid') or 'none'}"
        same_incident = prior.get("signature") == signature
        already_attempted = same_incident and prior.get("restart_attempted") is True
        incident = {
            "signature": signature,
            "first_seen_at": (
                prior.get("first_seen_at") if same_incident else utc_now()
            ),
            "restart_attempted": bool(already_attempted),
        }
        if not already_attempted:
            command = restart_command(supervisor_kind, supervisor_unit)
            restart_intent = {
                "schema": "hermes-gateway-restart-intent/v1",
                "status": "in_flight",
                "created_at": utc_now(),
                "owner": "canonical_gateway_watchdog",
                "supervisor": {
                    "kind": supervisor_kind,
                    "unit": supervisor_unit,
                },
                "reason": observation["reason"],
                "observed_pid": observation.get("pid"),
                "incident_signature": signature,
            }
            atomic_json(intent_path, restart_intent)
            try:
                result = command_runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
            except Exception as exc:
                restart_intent.update(
                    {
                        "status": "failed",
                        "completed_at": utc_now(),
                        "succeeded": False,
                        "detail": type(exc).__name__,
                    }
                )
                atomic_json(intent_path, restart_intent)
                raise
            restart.update(
                {
                    "attempted": True,
                    "succeeded": result.returncode == 0,
                    "detail": (result.stdout or result.stderr or "").strip()[:240],
                }
            )
            restart_intent.update(
                {
                    "status": "completed",
                    "completed_at": utc_now(),
                    "succeeded": restart["succeeded"],
                    "detail": restart["detail"],
                }
            )
            atomic_json(intent_path, restart_intent)
            incident["restart_attempted"] = True
            incident["restart_attempted_at"] = utc_now()
            incident["restart_succeeded"] = restart["succeeded"]

    if incident:
        atomic_json(incident_path, incident)
    else:
        incident_path.unlink(missing_ok=True)
    receipt = {
        "schema": "hermes-gateway-watchdog-client/v1",
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "hermes_home": str(hermes_home),
        "supervisor": {"kind": supervisor_kind, "unit": supervisor_unit},
        "observation": observation,
        "restart": restart,
        "restart_intent": restart_intent,
        "incident": incident,
    }
    atomic_json(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))),
    )
    parser.add_argument("--supervisor-kind", required=True)
    parser.add_argument("--supervisor-unit", required=True)
    parser.add_argument("--heartbeat-max-age", type=int, default=600)
    args = parser.parse_args(argv)
    receipt = run_watchdog(
        hermes_home=args.hermes_home.expanduser(),
        supervisor_kind=args.supervisor_kind,
        supervisor_unit=args.supervisor_unit,
        heartbeat_max_age=args.heartbeat_max_age,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
