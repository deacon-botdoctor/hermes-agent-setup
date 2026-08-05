#!/usr/bin/env python3
"""probe-client-health.py — Rich health probe for Hermes client gateways.

Produces the same heartbeat schema as probe-enoch-health.py and
probe-doc-health.py so fleet scanners and watchers get consistent
diagnostic depth from every host — not just Enoch/Doc.

Runs on macOS clients (via launchd) and Spark-hosted Linux clients
(via systemd timer). Called by client-selfheal-heartbeat.sh.

Output: $HERMES_HOME/state/client-heartbeat.json
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"
OUTPUT = STATE_DIR / "client-heartbeat.json"
GATEWAY_STATE_PATH = HERMES_HOME / "gateway_state.json"
GATEWAY_LOG_PATH = HERMES_HOME / "logs" / "gateway.log"
AGENT_LOG_PATH = HERMES_HOME / "logs" / "agent.log"
SAFE_RESTART_LOG_PATH = HERMES_HOME / "logs" / "safe-restart.log"
RESTART_LOOP_WINDOW_SEC = int(os.environ.get("HERMES_RESTART_LOOP_WINDOW_SEC", "600"))
RESTART_LOOP_THRESHOLD = int(os.environ.get("HERMES_RESTART_LOOP_THRESHOLD", "3"))
RESPONSE_WINDOW_LINES = 1200
SLOW_RESPONSE_SEC = 120.0
FAIL_RESPONSE_SEC = 300.0
RESPONSE_WINDOW_SEC = 6 * 3600
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


def iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None


def parse_log_timestamp(line: str) -> datetime | None:
    for pat in (
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),",
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
    ):
        match = re.match(pat, line)
        if not match:
            continue
        try:
            parsed = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            return parsed.astimezone(UTC)
        except ValueError:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (PermissionError, FileNotFoundError, json.JSONDecodeError):
        return {}


def safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except PermissionError:
        return False


def safe_read_lines(path: Path, tail: int = 400) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail:]
    except (PermissionError, FileNotFoundError):
        return []


def kill_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def count_recent_errors(path: Path) -> int:
    if not safe_exists(path):
        return 0
    pattern = re.compile(r"polling conflict|Traceback|FATAL|PANIC|Connection refused|Invalid config")
    lines = safe_read_lines(path, 400)
    return sum(1 for line in lines if pattern.search(line))


def collect_response_metrics(path: Path, lower_bound: datetime | None = None) -> dict:
    metrics: dict = {
        "count": 0,
        "avg_sec": None,
        "max_sec": None,
        "slow_count": 0,
        "critical_count": 0,
    }
    if not safe_exists(path):
        return metrics
    pattern = re.compile(r"response ready: .* time=([0-9.]+)s ")
    cutoff = datetime.now(UTC).timestamp() - RESPONSE_WINDOW_SEC
    lines = safe_read_lines(path, RESPONSE_WINDOW_LINES)
    timings: list[float] = []
    for line in lines:
        ts = parse_log_timestamp(line)
        if ts is None or ts.timestamp() < cutoff:
            continue
        if lower_bound is not None and ts < lower_bound:
            continue
        match = pattern.search(line)
        if not match:
            continue
        try:
            elapsed = float(match.group(1))
        except ValueError:
            continue
        timings.append(elapsed)
    if not timings:
        return metrics
    metrics["count"] = len(timings)
    metrics["avg_sec"] = round(sum(timings) / len(timings), 1)
    metrics["max_sec"] = round(max(timings), 1)
    metrics["slow_count"] = sum(1 for v in timings if v >= SLOW_RESPONSE_SEC)
    metrics["critical_count"] = sum(1 for v in timings if v >= FAIL_RESPONSE_SEC)
    return metrics


def collect_state_pressure_metrics(path: Path, lower_bound: datetime | None = None) -> dict:
    metrics: dict = {
        "max_input_tokens": 0,
        "max_context_tokens": 0,
        "max_api_calls": 0,
        "max_tool_turns": 0,
        "large_context_count": 0,
        "gbrain_write_error_count": 0,
        "memory_capacity_error_count": 0,
        "same_tool_failure_count": 0,
    }
    if not safe_exists(path):
        return metrics
    cutoff = datetime.now(UTC).timestamp() - RESPONSE_WINDOW_SEC
    lines = safe_read_lines(path, RESPONSE_WINDOW_LINES)
    for line in lines:
        ts = parse_log_timestamp(line)
        if ts is None or ts.timestamp() < cutoff:
            continue
        if lower_bound is not None and ts < lower_bound:
            continue
        api_match = re.search(r"API call #(\d+): .* in=(\d+) out=(\d+) total=(\d+) latency=([0-9.]+)s", line)
        if api_match:
            metrics["max_api_calls"] = max(metrics["max_api_calls"], int(api_match.group(1)))
            metrics["max_input_tokens"] = max(metrics["max_input_tokens"], int(api_match.group(2)))
        context_match = re.search(r"context=~([\d,]+) tokens", line)
        if context_match:
            context_tokens = int(context_match.group(1).replace(",", ""))
            metrics["max_context_tokens"] = max(metrics["max_context_tokens"], context_tokens)
            if context_tokens >= 25000:
                metrics["large_context_count"] += 1
        turn_match = re.search(r"Turn ended: .* api_calls=(\d+)/\d+ .* tool_turns=(\d+)", line)
        if turn_match:
            metrics["max_api_calls"] = max(metrics["max_api_calls"], int(turn_match.group(1)))
            metrics["max_tool_turns"] = max(metrics["max_tool_turns"], int(turn_match.group(2)))
        lower = line.lower()
        if "gbrain capture failed" in lower or "pglite lock" in lower or "multixactid" in lower:
            metrics["gbrain_write_error_count"] += 1
        if "memory returned error" in lower and (
            "would exceed the limit" in lower or "shorten the new content" in lower or "memory at " in lower
        ):
            metrics["memory_capacity_error_count"] += 1
        if "same_tool_failure_warning" in lower or "tool loop warning" in lower:
            metrics["same_tool_failure_count"] += 1
    return metrics


def count_polling_conflicts(path: Path) -> int:
    if not safe_exists(path):
        return 0
    lines = safe_read_lines(path, 400)
    return sum(1 for line in lines if "Telegram polling conflict" in line)



def safe_restart_churn(
    path: Path,
    window_sec: int = RESTART_LOOP_WINDOW_SEC,
    threshold: int = RESTART_LOOP_THRESHOLD,
) -> dict:
    """Return recent safe-restart SIGTERM churn from safe-restart.log.

    A single restart can be intentional; repeated restart attempts are an
    operator-impacting loop even if the gateway recovers between probes.
    """
    result = {
        "recent_count": 0,
        "window_sec": window_sec,
        "threshold": threshold,
        "restart_loop": False,
        "first": None,
        "last": None,
    }
    if not safe_exists(path):
        return result
    cutoff = datetime.now(UTC).timestamp() - window_sec
    events: list[str] = []
    for line in safe_read_lines(path, 800):
        if "sending SIGTERM to gateway pid" not in line:
            continue
        ts = parse_log_timestamp(line)
        if ts is None or ts.timestamp() < cutoff:
            continue
        events.append(line.strip())
    result["recent_count"] = len(events)
    result["restart_loop"] = len(events) >= threshold
    if events:
        result["first"] = events[0][:220]
        result["last"] = events[-1][:220]
    return result

def performance_score(response_metrics: dict, recent_errors: int, polling_conflicts: int) -> int:
    score = 100
    avg_sec = response_metrics.get("avg_sec") or 0
    max_sec = response_metrics.get("max_sec") or 0
    slow_count = int(response_metrics.get("slow_count") or 0)
    critical_count = int(response_metrics.get("critical_count") or 0)
    score -= min(40, critical_count * 20)
    score -= min(20, slow_count * 5)
    score -= min(15, recent_errors * 5)
    score -= min(15, polling_conflicts * 5)
    if avg_sec >= 30:
        score -= min(20, int((avg_sec - 30) / 15))
    if max_sec >= 120:
        score -= min(10, int((max_sec - 120) / 60))
    return max(0, min(100, score))


def state_pressure_status(state_metrics: dict) -> str:
    max_input = int(state_metrics.get("max_input_tokens") or 0)
    max_context = int(state_metrics.get("max_context_tokens") or 0)
    max_api_calls = int(state_metrics.get("max_api_calls") or 0)
    max_tool_turns = int(state_metrics.get("max_tool_turns") or 0)
    large_context_count = int(state_metrics.get("large_context_count") or 0)
    gbrain_errors = int(state_metrics.get("gbrain_write_error_count") or 0)
    memory_errors = int(state_metrics.get("memory_capacity_error_count") or 0)
    same_tool_failures = int(state_metrics.get("same_tool_failure_count") or 0)
    if (
        max_input >= 80000
        or max_context >= 80000
        or max_api_calls >= 30
        or max_tool_turns >= 30
        or gbrain_errors >= 2
        or memory_errors >= 2
        or same_tool_failures >= 1
    ):
        return "fail"
    if (
        max_input >= 40000
        or max_context >= 40000
        or max_api_calls >= 12
        or max_tool_turns >= 12
        or large_context_count >= 3
        or gbrain_errors >= 1
        or memory_errors >= 1
    ):
        return "warn"
    return "pass"


def close_wait_count(pid: int) -> int:
    if pid <= 0:
        return 0
    try:
        if IS_MACOS:
            result = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True, check=False)
        else:
            result = subprocess.run(["ss", "-tnp"], capture_output=True, text=True, check=False)
        return sum(1 for line in result.stdout.splitlines() if "CLOSE_WAIT" in line or "CLOSE-WAIT" in line)
    except Exception:
        return 0


def detect_scheduler() -> tuple[str, str, str]:
    """Return (scheduler_type, state, sub_state)."""
    if IS_MACOS:
        # launchd: check if the gateway plist is loaded
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, check=False,
        )
        for line in (result.stdout or "").splitlines():
            if "ai.hermes.gateway" in line:
                parts = line.split()
                state = "active" if parts[1] != "-" else "inactive"
                return "launchd", state, parts[1] if parts[1] != "-" else "unknown"
        return "launchd", "unknown", "unknown"
    else:
        # systemd: check user service
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "hermes-gateway.service"],
            capture_output=True, text=True, check=False,
        )
        state = (result.stdout or "").strip() or "unknown"
        return "systemd", state, state


def main() -> None:
    now = datetime.now(UTC)
    gateway_state = load_json(GATEWAY_STATE_PATH)
    gateway_state_value = str(gateway_state.get("gateway_state") or "unknown")
    telegram_state = str(
        ((gateway_state.get("platforms") or {}).get("telegram") or {}).get("state") or "unknown"
    )
    gateway_updated_at = str(gateway_state.get("updated_at") or "")
    gateway_started_at = str(gateway_state.get("started_at") or gateway_updated_at or "")
    pid = int(gateway_state.get("pid") or 0)

    scheduler_type, scheduler_state, scheduler_sub = detect_scheduler()
    unit_state = "active" if kill_alive(pid) else "inactive"

    gateway_updated = parse_iso(gateway_updated_at)
    gateway_started = parse_iso(gateway_started_at)
    gateway_age = max(0, int((now - gateway_updated).total_seconds())) if gateway_updated else -1

    recent_errors = count_recent_errors(GATEWAY_LOG_PATH)
    close_wait = close_wait_count(pid)

    response_lower_bound = gateway_updated
    if gateway_started and (response_lower_bound is None or gateway_started > response_lower_bound):
        response_lower_bound = gateway_started
    response_metrics = collect_response_metrics(GATEWAY_LOG_PATH, response_lower_bound)
    state_pressure = collect_state_pressure_metrics(AGENT_LOG_PATH, response_lower_bound)
    state_pressure_verdict = state_pressure_status(state_pressure)
    polling_conflicts = count_polling_conflicts(GATEWAY_LOG_PATH)
    restart_churn = safe_restart_churn(SAFE_RESTART_LOG_PATH)
    score = performance_score(response_metrics, recent_errors, polling_conflicts)

    # --- Build indicators (same schema as Enoch/Doc) ---
    indicators = []

    indicators.append({
        "name": "process_alive",
        "status": "pass" if unit_state == "active" else "fail",
        "detail": f"pid={pid or 0} unit_state={unit_state}",
    })
    indicators.append({
        "name": "gateway_state",
        "status": "pass" if gateway_state_value == "running" else "fail",
        "detail": gateway_state_value,
    })
    indicators.append({
        "name": "telegram_state",
        "status": "pass" if telegram_state == "connected" else "fail",
        "detail": telegram_state,
    })
    indicators.append({
        "name": "gateway_freshness",
        "status": "pass" if 0 <= gateway_age <= 300 else "warn",
        "detail": f"age_sec={gateway_age}",
    })
    indicators.append({
        "name": "recent_errors",
        "status": "pass" if recent_errors == 0 else "warn",
        "detail": f"count={recent_errors}",
    })

    response_status = "pass"
    if response_metrics["critical_count"] > 0:
        response_status = "fail"
    elif response_metrics["slow_count"] >= 2 or ((response_metrics["avg_sec"] or 0) >= 90):
        response_status = "warn"
    avg_detail = response_metrics["avg_sec"] if response_metrics["avg_sec"] is not None else "na"
    max_detail = response_metrics["max_sec"] if response_metrics["max_sec"] is not None else "na"
    indicators.append({
        "name": "response_latency",
        "status": response_status,
        "detail": (
            f"count={response_metrics['count']} avg_sec={avg_detail} "
            f"max_sec={max_detail} slow_count={response_metrics['slow_count']} "
            f"critical_count={response_metrics['critical_count']}"
        ),
    })

    indicators.append({
        "name": "state_pressure",
        "status": state_pressure_verdict,
        "detail": (
            f"max_input_tokens={state_pressure['max_input_tokens']} "
            f"max_context_tokens={state_pressure['max_context_tokens']} "
            f"max_api_calls={state_pressure['max_api_calls']} "
            f"max_tool_turns={state_pressure['max_tool_turns']} "
            f"gbrain_errors={state_pressure['gbrain_write_error_count']} "
            f"memory_errors={state_pressure['memory_capacity_error_count']}"
        ),
    })

    conflict_status = "pass" if polling_conflicts == 0 else ("fail" if polling_conflicts >= 3 else "warn")
    indicators.append({
        "name": "polling_conflicts",
        "status": conflict_status,
        "detail": f"count={polling_conflicts}",
    })
    indicators.append({
        "name": "close_wait",
        "status": "pass" if close_wait <= 5 else "warn",
        "detail": f"count={close_wait}",
    })
    indicators.append({
        "name": "restart_churn",
        "status": "fail" if restart_churn["restart_loop"] else "pass",
        "detail": (
            f"count={restart_churn['recent_count']} threshold={restart_churn['threshold']} "
            f"window_sec={restart_churn['window_sec']} last={restart_churn.get('last') or 'none'}"
        ),
    })
    indicators.append({
        "name": "performance_score",
        "status": "pass" if score >= 85 else ("warn" if score >= 60 else "fail"),
        "detail": f"score={score}",
    })

    # --- Verdict ---
    if restart_churn["restart_loop"]:
        verdict = "degraded"
    elif unit_state != "active":
        verdict = "down"
    elif (
        gateway_state_value == "running"
        and telegram_state == "connected"
        and response_status == "pass"
        and state_pressure_verdict == "pass"
        and score >= 85
    ):
        verdict = "healthy"
    else:
        verdict = "degraded"

    # --- Client identity ---
    # Derive from HERMES_HOME path
    home = Path.home()
    if HERMES_HOME.name == ".hermes" and HERMES_HOME.parent != home:
        # A shared-host runtime derives its identity from its parent directory.
        agent_name = HERMES_HOME.parent.name
    elif HERMES_HOME.parent.name == "sandboxes":
        agent_name = HERMES_HOME.name  # alfred, sarah, etc.
    elif HERMES_HOME.parent.name == "profiles":
        agent_name = HERMES_HOME.name  # doc, etc.
    else:
        agent_name = home.name  # native client: use username

    payload = {
        "schema_version": 1,
        "probe": {
            "name": "probe-client-health",
            "agent": agent_name,
            "role": "fleet_client",
            "host": platform.node(),
            "platform": "macos" if IS_MACOS else "linux",
        },
        "generated_at": iso_now(),
        "generated_epoch": int(now.timestamp()),
        "verdict": verdict,
        "summary": {
            "performance_score": score,
            "unit_state": unit_state,
            "gateway_state": gateway_state_value,
            "telegram_state": telegram_state,
            "recent_error_count": recent_errors,
            "gateway_state_age_sec": gateway_age,
            "response_count": response_metrics["count"],
            "avg_response_sec": response_metrics["avg_sec"],
            "max_response_sec": response_metrics["max_sec"],
            "slow_response_count": response_metrics["slow_count"],
            "critical_response_count": response_metrics["critical_count"],
            "max_input_tokens": state_pressure["max_input_tokens"],
            "max_context_tokens": state_pressure["max_context_tokens"],
            "max_api_calls": state_pressure["max_api_calls"],
            "max_tool_turns": state_pressure["max_tool_turns"],
            "gbrain_write_error_count": state_pressure["gbrain_write_error_count"],
            "memory_capacity_error_count": state_pressure["memory_capacity_error_count"],
            "state_pressure_status": state_pressure_verdict,
            "polling_conflict_count": polling_conflicts,
            "restart_loop": restart_churn["restart_loop"],
            "restart_churn_count": restart_churn["recent_count"],
            "restart_churn_window_sec": restart_churn["window_sec"],
            "scheduler_type": scheduler_type,
        },
        "unit": {
            "state": unit_state,
            "sub_state": scheduler_sub,
            "main_pid": pid,
            "active_since": gateway_started_at,
            "uptime_sec": 0,
            "scheduler": scheduler_type,
        },
        "gateway_state": gateway_state_value,
        "telegram_state": telegram_state,
        "gateway_state_updated_at": gateway_updated_at,
        "gateway_state_age_sec": gateway_age,
        "recent_error_count": recent_errors,
        "response_metrics": response_metrics,
        "state_pressure": state_pressure,
        "polling_conflict_count": polling_conflicts,
        "restart_churn": restart_churn,
        "performance_score": score,
        "indicators": indicators,
    }
    # Write output — prefer HERMES_HOME/state, fall back to a mirror location
    # for Spark-hosted clients where the probe runs as a different user
    written = False
    try:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written = True
    except PermissionError:
        pass
    if not written:
        mirror_dir = Path.home() / ".hermes" / "state" / "spark-mirror"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        mirror_path = mirror_dir / f"{agent_name}-heartbeat.json"
        mirror_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "output": str(mirror_path), "verdict": verdict}))
        return
    print(json.dumps({"ok": True, "output": str(OUTPUT), "verdict": verdict}))


if __name__ == "__main__":
    main()
