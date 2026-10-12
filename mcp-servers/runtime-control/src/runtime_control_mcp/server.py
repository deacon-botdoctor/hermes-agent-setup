from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer as FastMCP


SERVER_NAME = "runtime-control"
HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
).expanduser()
DEFAULT_TIMEOUT = int(os.environ.get("RUNTIME_CONTROL_TIMEOUT", "30"))
DEFAULT_LOG_LINES = int(os.environ.get("RUNTIME_CONTROL_LOG_LINES", "80"))

mcp = FastMCP(SERVER_NAME)


def _run(command: list[str], *, timeout: int = DEFAULT_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or f"rc={completed.returncode}"
        raise ValueError(detail)
    return completed


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _service_command(service_name: str, action: str, user_mode: bool) -> list[str]:
    command = ["systemctl"]
    if user_mode:
        command.append("--user")
    command.extend([action, service_name])
    return command


def _normalize_service_name(service_name: str) -> str:
    name = service_name.strip()
    if name and not name.endswith(".service"):
        name = f"{name}.service"
    return name


def _current_gateway_service_name() -> str | None:
    try:
        parts = HERMES_HOME.resolve().parts
    except Exception:
        return None
    if "profiles" in parts:
        idx = parts.index("profiles")
        if idx + 1 < len(parts) and parts[idx + 1]:
            return f"hermes-{parts[idx + 1]}.service"
    return os.environ.get("HERMES_SYSTEMD_SERVICE")


def _restart_audit(event: str, service_name: str, user_mode: bool, detail: str = "") -> None:
    try:
        log_dir = HERMES_HOME / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = (
            f"{datetime.now(timezone.utc).isoformat()} event={event} "
            f"service={service_name!r} user_mode={user_mode} "
            f"pid={os.getpid()} ppid={os.getppid()} detail={detail!r}\n"
        )
        with (log_dir / "runtime-control-restarts.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _safe_log_path(path_str: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    allowed_roots = [
        HERMES_HOME.resolve(),
        (HERMES_HOME / "logs").resolve(),
        Path("/var/log").resolve(),
    ]
    if not any(root == path or root in path.parents for root in allowed_roots):
        raise ValueError(f"log path outside allowed roots: {path}")
    if not path.exists():
        raise ValueError(f"log path missing: {path}")
    if not path.is_file():
        raise ValueError(f"log path is not a file: {path}")
    return path


def _gateway_processes() -> list[dict[str, Any]]:
    ps = _run(["ps", "aux"], check=True)
    rows: list[dict[str, Any]] = []
    for line in (ps.stdout or "").splitlines():
        lowered = line.lower()
        if (
            "gateway run" not in lowered
            and "gateway.run" not in lowered
            and "gateway/run.py" not in lowered
            and "hermes_cli.main gateway" not in lowered
        ):
            continue
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        rows.append(
            {
                "user": parts[0],
                "pid": parts[1],
                "cpu": parts[2],
                "mem": parts[3],
                "command": parts[10],
            }
        )
    return rows


@mcp.tool()
def runtime_control_healthcheck() -> dict[str, Any]:
    return {
        "ok": True,
        "server": SERVER_NAME,
        "hermes_home": str(HERMES_HOME),
        "hermes_home_exists": HERMES_HOME.exists(),
        "logs_dir_exists": (HERMES_HOME / "logs").exists(),
        "systemctl_available": subprocess.run(
            ["which", "systemctl"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0,
        "gateway_process_count": len(_gateway_processes()),
    }


@mcp.tool()
def runtime_control_service_status(service_name: str, user_mode: bool = True) -> dict[str, Any]:
    command = _service_command(service_name, "status", user_mode)
    completed = _run(command, check=False)
    is_active = _run(_service_command(service_name, "is-active", user_mode), check=False)
    is_enabled = _run(_service_command(service_name, "is-enabled", user_mode), check=False)
    return {
        "ok": completed.returncode == 0,
        "service_name": service_name,
        "user_mode": user_mode,
        "status_returncode": completed.returncode,
        "active_state": (is_active.stdout or is_active.stderr).strip(),
        "enabled_state": (is_enabled.stdout or is_enabled.stderr).strip(),
        "status_text": (completed.stdout or completed.stderr).strip(),
        "command": command,
    }


@mcp.tool()
def runtime_control_restart_service(service_name: str, user_mode: bool = True) -> dict[str, Any]:
    normalized_service = _normalize_service_name(service_name)
    current_service = _current_gateway_service_name()
    if (
        user_mode
        and current_service
        and normalized_service == current_service
        and os.environ.get("RUNTIME_CONTROL_ALLOW_SELF_RESTART") != "1"
    ):
        detail = "refusing to restart owning gateway service from its own runtime-control MCP"
        _restart_audit("denied_self_restart", normalized_service, user_mode, detail)
        return {
            "ok": False,
            "service_name": service_name,
            "normalized_service_name": normalized_service,
            "user_mode": user_mode,
            "current_gateway_service": current_service,
            "restart_returncode": None,
            "restart_output": detail,
            "active_state": None,
        }
    _restart_audit("restart_requested", normalized_service, user_mode)
    restart = _run(_service_command(normalized_service, "restart", user_mode), check=False)
    active = _run(_service_command(normalized_service, "is-active", user_mode), check=False)
    _restart_audit("restart_completed", normalized_service, user_mode, f"rc={restart.returncode} active={(active.stdout or active.stderr).strip()}")
    return {
        "ok": restart.returncode == 0 and (active.stdout or "").strip() == "active",
        "service_name": service_name,
        "normalized_service_name": normalized_service,
        "user_mode": user_mode,
        "restart_returncode": restart.returncode,
        "restart_output": (restart.stdout or restart.stderr).strip(),
        "active_state": (active.stdout or active.stderr).strip(),
    }


@mcp.tool()
def runtime_control_process_summary(pattern: str = "hermes", limit: int = 20) -> dict[str, Any]:
    ps = _run(["ps", "aux"], check=True)
    needle = (pattern or "").strip().lower()
    matches = []
    for line in (ps.stdout or "").splitlines():
        if needle and needle not in line.lower():
            continue
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        matches.append(
            {
                "user": parts[0],
                "pid": parts[1],
                "cpu": parts[2],
                "mem": parts[3],
                "command": parts[10],
            }
        )
        if len(matches) >= max(1, limit):
            break
    return {
        "ok": True,
        "pattern": pattern,
        "count": len(matches),
        "processes": matches,
    }


@mcp.tool()
def runtime_control_recent_log(path: str, lines: int = DEFAULT_LOG_LINES) -> dict[str, Any]:
    log_path = _safe_log_path(path)
    completed = _run(["tail", "-n", str(max(1, lines)), str(log_path)], check=True)
    return {
        "ok": True,
        "path": str(log_path),
        "lines": max(1, lines),
        "content": (completed.stdout or "").rstrip(),
    }


@mcp.tool()
def runtime_control_gateway_status() -> dict[str, Any]:
    logs_dir = HERMES_HOME / "logs"
    gateway_log = logs_dir / "gateway.log"
    gateway_err = logs_dir / "gateway.err.log"
    agent_log = logs_dir / "agent.log"
    return {
        "ok": True,
        "hermes_home": str(HERMES_HOME),
        "gateway_processes": _gateway_processes(),
        "known_logs": {
            "gateway_log": str(gateway_log),
            "gateway_log_exists": gateway_log.exists(),
            "gateway_err_log": str(gateway_err),
            "gateway_err_log_exists": gateway_err.exists(),
            "agent_log": str(agent_log),
            "agent_log_exists": agent_log.exists(),
        },
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
