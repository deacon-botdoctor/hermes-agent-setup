#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import os
import re
import runpy
import shutil
import signal
import sqlite3
import subprocess
import threading
import queue
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME") or HOME / ".hermes").expanduser()
PROC_ROOT = Path("/proc")
AGENT_PROBE_MAX_AGE_S = int(os.environ.get("HERMES_AGENT_PROBE_MAX_AGE_SECONDS", "1800"))
# Unified search is free-first; Exa is an optional paid escalation backend.
OPTIONAL_CAPABILITY_ENV_KEYS = {"EXA_API_KEY"}
KEYLESS_MCP_ENV_ARGS = {"EXA_API_KEY": ["-m", "search_mcp.server"]}


def _gateway_descriptor_defaults(platform_name):
    # Windows HandleCount includes events, registry keys, pipes, and MCP child
    # plumbing; a healthy tool-rich gateway routinely exceeds Unix fd limits.
    return (1024, 4096) if platform_name == "nt" else (128, 512)


_GATEWAY_FD_WARN_DEFAULT, _GATEWAY_FD_FAIL_DEFAULT = _gateway_descriptor_defaults(os.name)
GATEWAY_FD_WARN = int(os.environ.get("HERMES_GATEWAY_FD_WARN", str(_GATEWAY_FD_WARN_DEFAULT)))
GATEWAY_FD_FAIL = int(os.environ.get("HERMES_GATEWAY_FD_FAIL", str(_GATEWAY_FD_FAIL_DEFAULT)))
GATEWAY_STATE_DB_HANDLE_WARN = int(os.environ.get("HERMES_GATEWAY_STATE_DB_HANDLE_WARN", "24"))
GATEWAY_STATE_DB_HANDLE_FAIL = int(os.environ.get("HERMES_GATEWAY_STATE_DB_HANDLE_FAIL", "96"))
GATEWAY_RSS_WARN_BYTES = int(os.environ.get("HERMES_GATEWAY_RSS_WARN_BYTES", str(4 * 1024**3)))
GATEWAY_RSS_FAIL_BYTES = int(os.environ.get("HERMES_GATEWAY_RSS_FAIL_BYTES", str(8 * 1024**3)))
TELEGRAM_POLL_MAX_AGE_S = int(os.environ.get("HERMES_TELEGRAM_POLL_MAX_AGE_SECONDS", "180"))
HOST_VIRTUAL_FREE_WARN_PERCENT = float(os.environ.get("HERMES_HOST_VIRTUAL_FREE_WARN_PERCENT", "20"))
HOST_VIRTUAL_FREE_FAIL_PERCENT = float(os.environ.get("HERMES_HOST_VIRTUAL_FREE_FAIL_PERCENT", "10"))
HOST_SWAP_ALLOCATED_WARN_PERCENT = float(
    os.environ.get("HERMES_HOST_SWAP_ALLOCATED_WARN_PERCENT", "75")
)
HOST_SWAP_ACTIVE_WARN_BYTES_PER_MINUTE = int(
    os.environ.get("HERMES_HOST_SWAP_ACTIVE_WARN_BYTES_PER_MINUTE", str(64 * 1024**2))
)
HOST_SWAP_ACTIVE_FAIL_BYTES_PER_MINUTE = int(
    os.environ.get("HERMES_HOST_SWAP_ACTIVE_FAIL_BYTES_PER_MINUTE", str(256 * 1024**2))
)
HOST_SWAP_RATE_MIN_WINDOW_SECONDS = 30
HOST_SWAP_RATE_MAX_WINDOW_SECONDS = 2 * 60 * 60
HOST_PROCESS_WARN = int(os.environ.get("HERMES_HOST_PROCESS_WARN", "500"))
HOST_PROCESS_FAIL = int(os.environ.get("HERMES_HOST_PROCESS_FAIL", "800"))
HOST_POWERSHELL_WARN = int(os.environ.get("HERMES_HOST_POWERSHELL_WARN", "25"))
HOST_PROCESS_HANDLE_FAIL = int(os.environ.get("HERMES_HOST_PROCESS_HANDLE_FAIL", "50000"))
WINDOWS_HANDLE_OBSERVATION_LIMIT = 40
WINDOWS_HANDLE_GROWTH_WARN = 256
WINDOWS_HANDLE_ABSOLUTE_WARN = 8192
WINDOWS_HANDLE_MIN_WINDOW_SECONDS = 5 * 60
WINDOWS_HANDLE_MAX_WINDOW_SECONDS = 45 * 60
LARGE_JOB_ESTIMATE_BYTES = int(os.environ.get("HERMES_LARGE_JOB_ESTIMATE_BYTES", str(5 * 1024**3)))
DISK_WARN_NEW_PAYLOAD_LIMIT_BYTES = int(
    os.environ.get("HERMES_DISK_WARN_NEW_PAYLOAD_LIMIT_BYTES", str(1024**3))
)
MAX_CONCURRENT_LARGE_JOBS = int(os.environ.get("HERMES_MAX_CONCURRENT_LARGE_JOBS", "1"))


def iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(p: Path, default):
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return json.loads(p.read_text(encoding=enc))
        except Exception:
            continue
    return default


def parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _scaled_bytes(value, unit):
    multipliers = {
        "B": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }
    try:
        return round(float(value) * multipliers[str(unit).upper()])
    except (KeyError, TypeError, ValueError):
        return None


def _counter_rate_bytes_per_minute(current, previous, page_size, window_seconds):
    if not all(isinstance(item, int) for item in (current, previous, page_size)):
        return None
    if not isinstance(window_seconds, (int, float)):
        return None
    if not HOST_SWAP_RATE_MIN_WINDOW_SECONDS <= window_seconds <= HOST_SWAP_RATE_MAX_WINDOW_SECONDS:
        return None
    if current < previous or page_size <= 0:
        return None
    return round((current - previous) * page_size * 60 / window_seconds)


def _elapsed_seconds(raw):
    text = str(raw or "").strip()
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    try:
        parts = [int(item) for item in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 1:
        hours, minutes, seconds = 0, 0, parts[0]
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    except Exception as exc:

        class Result:
            returncode = 127
            stdout = ""
            stderr = f"{type(exc).__name__}: {str(exc)[:160]}"

        return Result()


def run_user_service(*args):
    # Cron and sudo do not carry the service owner's login-session environment.
    runtime_dir = f"/run/user/{os.geteuid()}"
    return run([
        "env", f"XDG_RUNTIME_DIR={runtime_dir}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime_dir}/bus", "TZ=UTC",
        "systemctl", "--user", *args,
    ], timeout=5)


def pid_is_alive(pid: int) -> bool:
    """Use a Windows-native process lookup; ``os.kill(pid, 0)`` is unreliable there."""
    if os.name == "nt":
        result = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=5)
        return result.returncode == 0 and re.search(rf"\b{pid}\b", result.stdout or "") is not None
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def check_config():
    p = HERMES / "config.yaml"
    return {"name": "config", "status": "pass" if p.exists() and p.stat().st_size > 0 else "fail", "detail": str(p)}


def check_on_demand_runtime():
    missing = [name for name in ("SOUL.md", "AGENTS.md")
               if not (HERMES / name).is_file() or not (HERMES / name).stat().st_size]
    if missing:
        return {"name": "on_demand_runtime", "status": "fail",
                "detail": "missing profile instructions: " + ", ".join(missing)}
    probe = subprocess.run(
        [sys.executable, "-I", "-c",
         "import json,run_agent,hermes_cli; print(json.dumps({'source':run_agent.__file__}))"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if probe.returncode:
        return {"name": "on_demand_runtime", "status": "fail",
                "detail": f"native runtime import failed, exit={probe.returncode}"}
    source = json.loads(probe.stdout.splitlines()[-1])["source"]
    return {"name": "on_demand_runtime", "status": "pass",
            "detail": f"profile instructions present; native runtime import passed; source={source}"}


def check_client_context():
    p = HERMES / "CLIENT_CONTEXT.md"
    return {"name": "client_context", "status": "pass" if p.exists() else "warn", "detail": str(p)}


def api_only_profile():
    """Recognize an explicitly configured API surface, never missing config."""
    import yaml

    try:
        config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8-sig")) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(config, dict):
        return False
    platforms = config.get("platforms") or {}
    declared = (isinstance(platforms, dict)
            and isinstance(platforms.get("api_server"), dict)
            and platforms["api_server"].get("enabled") is True
            and all(isinstance(value, dict) and value.get("enabled") is False
                    for name, value in platforms.items() if name != "api_server"))
    if not declared:
        return False
    # The native loader owns gateway.json, nested YAML and environment/plugin
    # precedence. Do not infer that an omitted chat platform is disabled.
    program = ("import json;from gateway.config import load_gateway_config;"
               "c=load_gateway_config();print(json.dumps(sorted("
               "p.value for p,v in c.platforms.items() if v.enabled)))")
    try:
        result = subprocess.run([sys.executable, "-I", "-c", program],
                                env={**_read_env_keys(), **os.environ, "HERMES_HOME": str(HERMES)},
                                capture_output=True, text=True, timeout=15)
        return result.returncode == 0 and json.loads(result.stdout) == ["api_server"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def telegram_transport_expected():
    """Honor configured API-only profiles and explicit Telegram opt-outs."""
    try:
        import yaml

        config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return True
    if api_only_profile():
        return False
    telegram = ((config.get("platforms") or {}).get("telegram") or {})
    return not (isinstance(telegram, dict) and telegram.get("enabled") is False)


def runtime_host_receipt(binding):
    """Read the receipt from the bound service owner's native publication home."""
    agent_id = (binding.get("identity") or {}).get("agent_id")
    if (binding.get("service") or {}).get("kind") != "windows-scheduled-task":
        return (load_json(HERMES / "state/host-receipts" / f"latest-{agent_id}.json", {})
                if isinstance(agent_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", agent_id) else {})
    # Windows publishes at the base home and deliberately has no POSIX identity.
    # Do not search other profiles or accept a receipt outside this exact owner.
    try:
        base = Path(str(binding.get("hermes_home") or ""))
        if (binding.get("schema_version") != 1 or binding.get("kind") != "botdoctor_runtime_binding"
                or binding.get("status") != "active" or not base.is_absolute()
                or not isinstance(binding.get("target_sha"), str) or not binding["target_sha"]
                or not isinstance(binding.get("runtime_fingerprint_digest"), str)
                or not binding["runtime_fingerprint_digest"]):
            return {}
        # Only Windows scheduled-task bindings reach this point; others returned above.
        receipts = base / "state/host-receipts"
        if (HERMES.parent.name == "profiles" and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", HERMES.name)
                and base.resolve() == HERMES.parent.parent.resolve()):
            candidates = [receipts / f"latest-{HERMES.name}.json"]
        elif base.resolve() == HERMES.resolve():
            # A single-home Windows runtime is its own base. Its receipt is named
            # for the manifest host, which the binding does not carry, so accept
            # only one verified receipt bound to this exact binding and home.
            candidates = sorted(receipts.glob("latest-*.json"))
        else:
            return {}
        matches = []
        for path in candidates:
            host = path.name[len("latest-"):-len(".json")]
            receipt = load_json(path, {}) if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", host) else {}
            if not isinstance(receipt, dict):
                continue
            contract = receipt.get("runtime_contract") or {}
            if not isinstance(contract, dict):
                continue
            profile = Path(str(contract.get("profile_home") or ""))
            owner = Path(str(contract.get("hermes_home") or ""))
            if (receipt.get("verified") is True and receipt.get("manifest_host") == host
                    and receipt.get("target_sha") == binding.get("target_sha")
                    and receipt.get("runtime_fingerprint_digest") == binding.get("runtime_fingerprint_digest")
                    and profile.is_absolute() and profile.resolve() == HERMES.resolve()
                    and owner.is_absolute() and owner.resolve() == base.resolve()):
                matches.append(receipt)
        return matches[0] if len(matches) == 1 else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _api_health_evidence(payload, pid):
    """Validate the server's actual contract without inventing diagnostics."""
    if not isinstance(payload, dict):
        return False, None, None
    if "state_transaction" in payload:
        health = payload["state_transaction"]
        ok = (isinstance(health, dict) and health.get("pid") == pid
              and health.get("database_status", health.get("status")) == "pass"
              and isinstance(health.get("checked_at"), (int, float))
              and 0 <= time.time() - health["checked_at"] <= 30)
        return ok, "writer_transaction", None
    readiness = payload.get("readiness") or {}
    readiness = readiness if isinstance(readiness, dict) else {}
    checks = readiness.get("checks") or {}
    checks = checks if isinstance(checks, dict) else {}
    heartbeat = parse_dt(payload.get("last_heartbeat"))
    ok = (payload.get("pid") == pid and payload.get("status") == "ok"
          and payload.get("gateway_state") == "running" and readiness.get("status") == "ok"
          and all(isinstance(checks.get(name), dict) and checks[name].get("status") == "ok"
                  for name in ("state_db", "session_store", "gateway"))
          and checks["state_db"].get("detail") != "not initialized"
          and heartbeat is not None and 0 <= time.time() - heartbeat.timestamp() <= 30)
    return ok, "native_readiness_read_only", checks


def check_gateway_state():
    candidates = [
        HERMES / "state/gateway-state.json",
        HERMES / "state/gateway_state.json",
        HERMES / "state/agent-state.json",
        HERMES / "gateway_state.json",
    ]
    for p in candidates:
        d = load_json(p, {})
        if d:
            state = str(d.get("gateway_state") or d.get("state") or d.get("status") or "").lower()
            ts = parse_dt(d.get("updated_at") or d.get("generated_at") or d.get("ts"))
            age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else None
            pid = d.get("pid")
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                pid = None
            pid_alive = pid_is_alive(pid) if pid else False
            telegram = ((d.get("platforms") or {}).get("telegram") or {})
            telegram_state = str(telegram.get("state") or "").lower()
            poll_at = parse_dt(telegram.get("last_successful_poll_at"))
            poll_age = (
                int((datetime.now(timezone.utc) - poll_at).total_seconds())
                if poll_at
                else None
            )
            telegram_expected = telegram_transport_expected()
            binding = load_json(HERMES / "state/runtime-binding.json", {})
            receipt = runtime_host_receipt(binding)
            native_poll_health = (
                (receipt.get("native_image_config") or {}).get("status") == "pass"
                and receipt.get("target_sha") == binding.get("target_sha")
                and receipt.get("runtime_fingerprint_digest") == binding.get("runtime_fingerprint_digest")
            )
            if native_poll_health:
                started = _live_process_start_time(pid)
                writer_start = telegram.get("writer_start_time")
                poll_healthy = (
                    started is not None and telegram.get("writer_pid") == pid
                    and writer_start == started
                    and (telegram.get("ingress") or {}).get("stalled") is not True
                )
            else:
                poll_healthy = poll_age is not None and 0 <= poll_age <= TELEGRAM_POLL_MAX_AGE_S
            telegram_ok = telegram_state == "connected" and poll_healthy
            api_only = api_only_profile()
            api_ok = False
            api_health_contract = None
            api_readiness_checks = None
            if api_only:
                api = (d.get("platforms") or {}).get("api_server") or {}
                started = _live_process_start_time(pid) if pid_alive else None
                payload = _gateway_api_state_transaction(full_document=True)
                health_ok, api_health_contract, api_readiness_checks = _api_health_evidence(payload, pid)
                api_ok = (api.get("state") == "connected" and started is not None
                          and api.get("writer_pid") == pid and api.get("writer_start_time") == started
                          and health_ok
                          and age is not None and 0 <= age <= 120)
            session_status = (d.get("session_store") or {}).get("status", "unknown")
            session_failed = session_status in {"unavailable", "retrying"}
            ok = (
                not session_failed
                and state == "running"
                and pid_alive
                and (telegram_ok or not telegram_expected)
                and (api_ok or not api_only)
            )
            return {
                "name": "gateway_state",
                "status": "pass" if ok else "fail",
                "severity": "P1",
                "fix_class": "gateway_liveness",
                "detail": (
                    f"{p.name} state={state or 'unknown'} age={age} pid={pid} "
                    f"telegram_expected={str(telegram_expected).lower()} "
                    f"pid_alive={str(pid_alive).lower()} telegram={telegram_state or 'unknown'} "
                    f"poll_age={poll_age} poll_max={TELEGRAM_POLL_MAX_AGE_S}"
                ),
                "gateway_pid": pid,
                "pid_alive": pid_alive,
                "transport_healthy": api_ok if api_only else (telegram_ok if telegram_expected else None),
                "transport_health_source": "api_server" if api_only else ("native_adapter" if native_poll_health else "golden_poll_receipt"),
                "api_health_contract": api_health_contract,
                "api_readiness_checks": api_readiness_checks,
                "session_store_healthy": False if session_failed else (True if session_status == "ok" else None),
                "agent_turn_healthy": False if session_failed else None,
                "session_store_status": session_status,
                "telegram_state": telegram_state or "unknown",
                "telegram_expected": telegram_expected,
                "last_successful_poll_at": telegram.get("last_successful_poll_at"),
                "poll_age_seconds": poll_age,
            }
    if api_only_profile():
        return {"name": "gateway_state", "status": "fail", "severity": "P1",
                "detail": "configured API-only profile has no gateway-owned platform state"}
    proc = run(["pgrep", "-f", str(HERMES)], timeout=5)
    ok = proc.returncode == 0 and bool(proc.stdout.strip())
    return {
        "name": "gateway_process",
        "status": "pass" if ok else "fail",
        "detail": ("pids=" + ",".join(proc.stdout.split()[:5])) if ok else "no process matched hermes home",
    }


def gateway_runtime_binding():
    candidates = [
        HERMES / "state/gateway-state.json",
        HERMES / "state/gateway_state.json",
        HERMES / "gateway_state.json",
    ]
    data = {}
    state_path = None
    for candidate in candidates:
        loaded = load_json(candidate, {})
        if loaded:
            data = loaded
            state_path = candidate
            break
    pid = data.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pid = None
    process_alive = pid_is_alive(pid) if pid else False
    argv = data.get("argv") if isinstance(data.get("argv"), list) else []
    runtime_root = ""
    if argv:
        first = Path(str(argv[0])).expanduser()
        if first.name == "main.py" and first.parent.name == "hermes_cli":
            runtime_root = str(first.parent.parent)
    return {
        "state_path": str(state_path) if state_path else "",
        "pid": pid,
        "process_alive": process_alive,
        "runtime_root": runtime_root,
        "argv": [str(value) for value in argv[:8]],
    }


def runtime_release_identity():
    """Return the exact shared release/composition identity activated by Operator Control."""
    binding = load_json(HERMES / "state/runtime-binding.json", {})
    target_sha = str(binding.get("target_sha") or "").strip().lower()
    fingerprint = str(binding.get("runtime_fingerprint_digest") or "").strip().lower()
    composition = str(binding.get("runtime_composition_digest") or "").strip().lower()
    valid = (
        binding.get("kind") == "botdoctor_runtime_binding"
        and binding.get("status") == "active"
        and re.fullmatch(r"[0-9a-f]{40}", target_sha) is not None
        and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is not None
        and re.fullmatch(r"[0-9a-f]{64}", composition) is not None
    )
    return {
        "schema_version": 1,
        "status": "pass" if valid else "fail",
        "target_sha": target_sha,
        "runtime_fingerprint_digest": fingerprint,
        "runtime_composition_digest": composition,
    }


def check_runtime_release_identity():
    identity = runtime_release_identity()
    return {
        "name": "runtime_release_identity",
        "status": identity["status"],
        "severity": "P1",
        "fix_class": "runtime_contract",
        "detail": (
            f"target={identity['target_sha']} composition={identity['runtime_composition_digest']}"
            if identity["status"] == "pass"
            else "active runtime binding lacks exact Golden target/fingerprint/composition identity"
        ),
        **identity,
    }


def check_active_client_capabilities():
    """Verify client-overlay bytes against the active rollout binding."""
    binding_path = HERMES / "state/runtime-binding.json"
    binding = load_json(binding_path, {})
    overlay = binding.get("client_overlay")
    if not isinstance(overlay, dict):
        return {
            "name": "active_client_capabilities",
            "status": "fail",
            "severity": "P1",
            "fix_class": "restart_or_redeploy",
            "detail": "active runtime has no client overlay capability contract",
        }
    proof = overlay.get("capability_contract")
    files = proof.get("files") if isinstance(proof, dict) else None
    composition_mode = str(overlay.get("composition_mode") or "").strip()
    preservation_proof = overlay.get("proof")
    runtime_raw = str(binding.get("runtime_root") or "").strip()
    runtime_python_raw = str(binding.get("runtime_python") or "").strip()
    runtime = Path(runtime_raw).expanduser()
    failures = []
    checked = []
    if binding.get("kind") != "botdoctor_runtime_binding" or binding.get("status") != "active":
        failures.append("active runtime binding is invalid")
    if not runtime_raw or not runtime.is_dir():
        failures.append("active runtime root is missing")
    if not runtime_python_raw or not Path(runtime_python_raw).expanduser().is_file():
        failures.append("active runtime Python is missing")
    if not overlay.get("repo") or not re.fullmatch(r"[0-9a-f]{40}", str(overlay.get("sha") or "")):
        failures.append("client overlay source identity is incomplete")
    external_state_only = composition_mode == "preserved_external_state"
    if external_state_only:
        if not isinstance(preservation_proof, dict) or not all(
            preservation_proof.get(key) is True
            for key in ("immutable_runtime_switch", "client_home_unchanged")
        ):
            failures.append("external-state preservation proof is incomplete")
    elif not isinstance(proof, dict) or proof.get("status") != "pass" or not isinstance(files, dict) or not files:
        failures.append("client capability proof is missing")
    elif runtime.is_dir():
        root = runtime.resolve()
        for relative, expected_sha in sorted(files.items()):
            relative_path = Path(str(relative))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                failures.append(f"unsafe capability path: {relative}")
                continue
            path = runtime / relative_path
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root) or path.is_symlink() or not path.is_file():
                    raise OSError("not a regular in-runtime file")
                actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, RuntimeError):
                failures.append(f"capability file missing or unsafe: {relative}")
                continue
            if actual_sha != expected_sha:
                failures.append(f"capability hash mismatch: {relative}")
            checked.append(str(relative))
    live_root = str(gateway_runtime_binding().get("runtime_root") or "")
    if runtime_raw and live_root and Path(live_root) != runtime:
        failures.append(f"live runtime mismatch live={live_root} bound={runtime}")
    return {
        "name": "active_client_capabilities",
        "status": "fail" if failures else "pass",
        "severity": "P1",
        "fix_class": "restart_or_redeploy",
        "detail": (
            "; ".join(failures)
            if failures
            else (
                f"repo={overlay.get('repo')} sha={overlay.get('sha')} "
                f"composition_mode={composition_mode or 'runtime_overlay'} "
                f"files={','.join(checked)} runtime_python={runtime_python_raw}"
            )
        ),
        "client_overlay_repo": overlay.get("repo"),
        "client_overlay_sha": overlay.get("sha"),
        "runtime_python": runtime_python_raw,
        "checked_files": checked,
    }


def check_telegram_transcript_hook():
    hook = HERMES / "hooks/telegram-transcript/HOOK.yaml"
    if not hook.exists():
        return {"name": "telegram_transcript_hook", "status": "warn", "detail": f"missing {hook}"}
    try:
        text = hook.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"name": "telegram_transcript_hook", "status": "warn", "detail": f"read failed: {type(e).__name__}"}
    required = ["agent:start", "agent:end"]
    missing = [event for event in required if event not in text]
    disabled = "events: []" in text or "events:[]" in text.replace(" ", "")
    log = HERMES / "logs/gateway.log"
    recent_skip = False
    if log.exists():
        try:
            recent = "\n".join(log.read_text(errors="replace").splitlines()[-800:])
            recent_skip = "Skipping telegram-transcript: no events declared" in recent
        except Exception:
            recent_skip = False
    if disabled or missing:
        return {
            "name": "telegram_transcript_hook",
            "status": "warn",
            "detail": f"disabled={disabled} missing={missing} path={hook}",
        }
    if recent_skip:
        return {
            "name": "telegram_transcript_hook",
            "status": "warn",
            "detail": (
                "manifest is fixed, but running gateway logs still show prior "
                "no-events load; reload required for live recovery"
            ),
        }
    return {"name": "telegram_transcript_hook", "status": "pass", "detail": str(hook)}


def _top_level_block(text: str, name: str) -> str | None:
    m = re.search(rf"^{re.escape(name)}:\s*$", text, re.M)
    if not m:
        return None
    start = m.end()
    end = start
    for line in text[start:].splitlines(True):
        if line.strip() and not line.startswith((" ", "\t", "#")):
            break
        end += len(line)
    return text[start:end]


def _nested_block(block: str | None, key: str, indent: int = 2) -> str | None:
    if block is None:
        return None
    pad = " " * indent
    m = re.search(rf"^{pad}{re.escape(key)}:\s*$", block, re.M)
    if not m:
        return None
    start = m.end()
    end = start
    child_prefix = " " * (indent + 2)
    for line in block[start:].splitlines(True):
        if line.strip() and not line.startswith((child_prefix, "#")):
            break
        end += len(line)
    return block[start:end]


def _nested_list_items(block: str | None, key: str, indent: int = 2) -> set[str]:
    """Read a nested YAML list, including safe_dump's indentless sequence form."""
    if block is None:
        return set()
    lines = block.splitlines()
    key_line = " " * indent + key + ":"
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() and line.rstrip() == key_line)
    except StopIteration:
        return set()
    items: set[str] = set()
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        leading = len(line) - len(line.lstrip(" "))
        match = re.match(r"^\s*-\s*([A-Za-z0-9_-]+)\s*$", line)
        if match and leading >= indent:
            items.add(match.group(1))
            continue
        if leading <= indent:
            break
    return items


def _scalar_from_block(block: str | None, key: str, indent: int = 2):
    if block is None:
        return None
    pad = " " * indent
    m = re.search(rf"^{pad}{re.escape(key)}:\s*(.*?)\s*$", block, re.M)
    if not m:
        return None
    value = m.group(1).strip().strip("\"'")
    if value.lower() == "false":
        return False
    if value.lower() == "true":
        return True
    try:
        return int(value)
    except Exception:
        try:
            return float(value)
        except Exception:
            return value


def _local_manager_binding():
    """Accept the manager role only from the exact installed rollout receipt."""
    path = HERMES / "state/runtime-binding.json"
    binding = load_json(path, {})
    identity = binding.get("identity") or {}
    if identity.get("role") != "local_manager":
        return None
    agent_id = identity.get("agent_id", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", agent_id):
        raise ValueError("invalid local manager identity")
    receipt = load_json(HERMES / "state/host-receipts" / f"latest-{agent_id}.json", {})
    if (binding.get("kind") != "botdoctor_runtime_binding" or binding.get("status") != "active"
            or binding.get("hermes_home") != str(HERMES)
            or HERMES.parent.name != "profiles" or HERMES.name != agent_id
            or identity.get("host") != agent_id
            or receipt.get("verified") is not True or receipt.get("manifest_host") != agent_id
            or not binding.get("target_sha") or receipt.get("target_sha") != binding["target_sha"]
            or (receipt.get("runtime_binding") or {}).get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError("local manager role lacks matching rollout custody")
    return binding


def check_local_manager_health(binding):
    """Observe existing API/job/watchdog coverage without running jobs or repairs."""
    import yaml
    from urllib.parse import urlsplit

    config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8"))
    gateway = config.get("gateway") or {}
    platforms = config.get("platforms") or {}
    live = load_json(HERMES / "gateway_state.json", {})
    watchdog = load_json(HERMES / "state/gateway-watchdog-client.json", {})
    pid = (binding.get("process") or {}).get("pid")
    agent_id = binding["identity"]["agent_id"]
    service = f"hermes-{agent_id}.service"
    failures = []
    if (gateway.get("standalone") is not True or gateway.get("multiplex_profiles") is not False
            or (platforms.get("api_server") or {}).get("enabled") is not True
            or (platforms.get("telegram") or {}).get("enabled") is not False
            or any(not isinstance(value, dict) or value.get("enabled") is not False
                   for name, value in platforms.items() if name != "api_server")):
        failures.append("manager must remain a standalone API-only profile")
    observed_platforms = live.get("platforms") or {}
    if (set(observed_platforms) != {"api_server"}
            or observed_platforms["api_server"].get("state") != "connected"
            or live.get("gateway_state") != "running" or live.get("pid") != pid or not pid_is_alive(pid)):
        failures.append("live manager API/process does not match the binding")
    listener = urlsplit((observed_platforms.get("api_server") or {}).get("listener_base", ""))
    if listener.scheme != "http" or listener.hostname != "127.0.0.1" or not listener.port:
        failures.append("manager API listener is not the local-only endpoint")
    # This existing authenticated probe validates same-PID database diagnostics
    # and freshness. It never opens the database or exposes the credential.
    try:
        transaction = _gateway_state_transaction()
        if transaction.get("pid") != pid:
            failures.append("manager API returned a different process")
    except Exception:
        failures.append("authenticated manager database diagnostics unavailable")
    jobs = load_json(HERMES / "cron/jobs.json", {}).get("jobs", [])
    enabled = [row for row in jobs if isinstance(row, dict) and row.get("enabled") is True]
    if (not enabled or any(row.get("deliver") != "local" or row.get("paused") is True
                           or not row.get("id") or not isinstance(row.get("schedule"), dict)
                           for row in enabled)):
        failures.append("manager local scheduled-job contract is missing or changed")
    stamp = parse_dt(watchdog.get("observed_at"))
    age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else -1
    observation = watchdog.get("observation") or {}
    if (watchdog.get("hermes_home") != str(HERMES) or not 0 <= age <= 600
            or (watchdog.get("supervisor") or {}) != {"kind": "systemd-user", "unit": service}
            or observation.get("pid") != pid or observation.get("health") != "healthy"
            or observation.get("pid_alive") is not True or observation.get("telegram_required") is not False):
        failures.append("profile watchdog evidence is missing, stale, or unhealthy")
    timer = f"hermes-gateway-watchdog-client-{agent_id}.timer"
    state = run_user_service("show", service, "-p", "MainPID", "-p", "ActiveState")
    if state.returncode or f"MainPID={pid}" not in state.stdout.splitlines() or "ActiveState=active" not in state.stdout.splitlines():
        failures.append("manager service is not active on the bound process")
    for action, expected in (("is-active", "active"), ("is-enabled", "enabled")):
        result = run_user_service(action, timer)
        if result.returncode or result.stdout.strip() != expected:
            failures.append(f"profile watchdog timer failed {action}")
    return {"name": "local_manager_health", "status": "fail" if failures else "pass",
            "severity": "P1", "detail": "; ".join(failures) if failures else
            f"same-PID API/database, {len(enabled)} local jobs, service and profile watchdog verified"}


# Installed configuration and source-tree configuration share the same format.
_rule_hash_path = Path(__file__).resolve().parents[1] / "config/injected-rule-hashes.json"
if not _rule_hash_path.exists():
    _rule_hash_path = Path(__file__).resolve().parents[1] / "kit/config/injected-rule-hashes.json"
try:
    INJECTED_RULE_HASHES = json.loads(_rule_hash_path.read_text(encoding="utf-8"))
    if (not isinstance(INJECTED_RULE_HASHES, dict) or not INJECTED_RULE_HASHES
            or any(not isinstance(name, str) or Path(name).name != name
                   or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for name, digest in INJECTED_RULE_HASHES.items())):
        raise ValueError("expected a nonempty rule-name to SHA-256 object")
    _rule_hash_error = None
except (OSError, ValueError) as exc:
    INJECTED_RULE_HASHES = {}
    _rule_hash_error = str(exc)
INJECTED_FLOOR_MARKERS = ('HERMES_OPERATING_FLOOR_v1',
 'HERMES_SELF_REPAIR_FLOOR_v1',
 'HERMES_CREDENTIAL_INTAKE_FLOOR_v1',
 'HERMES_COVENANT_CORE_v1',
 'HERMES_TRUTH_OVER_COMFORT_v1',
 'HERMES_OUTCOME_CONTRACT_v1',
 'HERMES_MACHINE_CAPABILITY_v1',
 'HERMES_SIMPLIFIED_TECHNICAL_ENGLISH_v1',
 'HERMES_CAMPAIGN_WORK_v1',
 'HERMES_OPERATOR_DUTY_v1',
 'HERMES_PRIVACY_JUDGMENT_v1',
 'HERMES_HYBRID_KNOWLEDGE_RETRIEVAL_v1')


def check_injected_shared_rules():
    if _rule_hash_error:
        return {"name": "injected_shared_rules", "status": "fail", "severity": "P1",
                "detail": f"cannot load rule hashes: {_rule_hash_error}",
                "mismatches": ["injected-rule-hashes.json:invalid"]}
    mismatches = []
    matched = 0
    floors = 0
    for name, expected in INJECTED_RULE_HASHES.items():
        try:
            raw = (HERMES / "shared-rules" / name).read_bytes()
        except OSError:
            mismatches.append(f"{name}:unreadable")
            continue
        text = raw.decode("utf-8", errors="replace")
        floors += sum(text.count(f"<!-- {marker}:END -->") == 1
                      for marker in INJECTED_FLOOR_MARKERS
                      if text.count(f"<!-- {marker}:START -->") == 1)
        if hashlib.sha256(raw).hexdigest() != expected:
            mismatches.append(f"{name}:hash_drift")
        else:
            matched += 1
    return {"name": "injected_shared_rules", "status": "warn" if mismatches else "pass",
            "detail": f"files={matched}/{len(INJECTED_RULE_HASHES)} floors={floors}/12"
                      + ("; " + ", ".join(mismatches) if mismatches else ""),
            "mismatches": mismatches}


def check_immersion_plugin():
    """Installed middleware has no effect unless Hermes can enable it."""
    if api_only_profile():
        return {"name": "immersion_plugin", "status": "skip", "detail": "configured API-only profile has no chat middleware surface"}
    if _local_manager_binding():
        return {"name": "immersion_plugin", "status": "skip", "detail": "local manager has no chat surface; manager health is checked separately"}
    try:
        import yaml

        config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8"))
        plugins = config.get("plugins") or {}
        enabled = plugins.get("enabled") or []
        disabled = plugins.get("disabled") or []
        if not isinstance(enabled, list) or not isinstance(disabled, list):
            raise ValueError("plugin lists must be lists")
        active = "botdoctor-immersion" in enabled and "botdoctor-immersion" not in disabled
    except Exception:
        return {"name": "immersion_plugin", "status": "fail", "severity": "P1",
                "detail": "cannot read valid plugin configuration"}
    return {"name": "immersion_plugin", "status": "pass" if active else "fail",
            **({} if active else {"severity": "P1"}),
            "detail": "botdoctor-immersion is configured to load" if active else
                      "botdoctor-immersion must be enabled and absent from plugins.disabled"}


def check_immersion_quality():
    p = HERMES / "config.yaml"
    if not p.exists():
        return {
            "name": "immersion_quality",
            "status": "fail",
            "severity": "P1",
            "fix_class": "auto_safe",
            "detail": f"missing config: {p}",
        }
    text = p.read_text(encoding="utf-8", errors="replace")
    display = _top_level_block(text, "display")
    session_reset = _top_level_block(text, "session_reset")
    platforms = _top_level_block(text, "platforms")
    telegram = _nested_block(platforms, "telegram")
    required_display = {
        "background_process_notifications": False,
        "bell_on_complete": False,
        "busy_ack_detail": False,
        "busy_ack_enabled": False,
        "busy_input_mode": "steer",
        "busy_steer_ack_enabled": False,
        "busy_text_mode": "steer",
        "cleanup_progress": False,
        "interim_assistant_messages": False,
        "long_running_notifications": False,
        "progress_on_typing": False,
        "show_cost": False,
        "show_reasoning": False,
        "streaming": False,
        "timestamps": False,
        "tool_preview_length": 0,
        "tool_progress": False,
        "tool_progress_command": False,
    }
    manager = _local_manager_binding()
    if api_only_profile():
        required_display = {}
    elif manager:
        required_display.pop("progress_on_typing")
    drift = []
    for key, expected in required_display.items():
        actual = _scalar_from_block(display, key)
        if actual != expected:
            drift.append(f"display.{key}={actual!r}->{expected!r}")
    binding = load_json(HERMES / "state/runtime-binding.json", {})
    # Installed behavior reads must use native defaults, expansion and managed
    # overlays, not Golden's retired raw compression presets. Before a runtime
    # is bound, only the explicit configuration is available for diagnostics.
    try:
        if isinstance(binding, dict) and (binding.get("runtime_root") or binding.get("runtime_python")):
            runtime = Path(binding["runtime_root"]).resolve(strict=True)
            python = Path(binding["runtime_python"]).expanduser()
            profile_home = HERMES.resolve(strict=True)
            binding_homes = {profile_home}
            if profile_home.parent.name == "profiles":
                # Windows bindings name the base home, including the copy
                # installed in its selected profile. Keep runtime custody
                # below this profile; do not accept another profile's root.
                binding_homes.add(profile_home.parent.parent)
            if (binding.get("kind") != "botdoctor_runtime_binding" or binding.get("status") != "active"
                    or Path(binding["hermes_home"]).resolve(strict=True) not in binding_homes
                    or not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK)):
                raise ValueError("invalid runtime binding")
            # Match the existing Refero selector's managed-root and physical
            # interpreter-parent custody; retain the final venv Python symlink.
            for candidate_root in (HERMES / "runtime-candidates", HERMES / "state/runtime-candidates"):
                try:
                    relative_root = runtime.relative_to(candidate_root.resolve(strict=True))
                except (OSError, ValueError):
                    continue
                if relative_root.parts:
                    break
            else:
                raise ValueError("runtime is outside a managed candidate root")
            python = Path(os.path.abspath(python))
            if python.relative_to(runtime).as_posix() not in {
                    f"{venv}/{suffix}" for venv in ("venv", ".venv")
                    for suffix in ("bin/python", "bin/python3", "Scripts/python.exe")}:
                raise ValueError("unexpected runtime interpreter layout")
            python.parent.resolve(strict=True).relative_to(runtime)
            probe = run([
                str(python), "-I", "-B", "-c",
                "import json, os, sys; os.environ['HERMES_HOME'] = sys.argv[1]; "
                "sys.path.insert(0, sys.argv[2]); "
                "from hermes_cli.config import load_config_readonly; "
                "config = load_config_readonly(); "
                "print(json.dumps(None if getattr(config, 'read_error', None) else "
                "{'compression': config.get('compression')}))",
                str(HERMES), str(runtime),
            ], timeout=10)
            if probe.returncode:
                raise ValueError("native effective configuration unavailable")
            config = json.loads(probe.stdout)
        else:
            import yaml

            config = yaml.safe_load(text)
        compression = config.get("compression", {})
        if not isinstance(compression, dict):
            raise ValueError("compression must be a mapping")
    except Exception as exc:
        compression = {}
        drift.append(f"compression effective configuration unavailable or invalid ({type(exc).__name__})")
    if compression.get("enabled", True) is not True:
        drift.append("compression.enabled must remain True")
    if compression.get("progress_notices", False) is not False:
        drift.append("compression.progress_notices must remain False")
    # A receipt may own an explicit cap/idle override, but absence is never a
    # match for that override. Native no-cap/idle-off needs no Golden preset.
    required_compression = {}
    identity = binding.get("identity") if isinstance(binding, dict) else None
    agent_id = identity.get("agent_id") if isinstance(identity, dict) else None
    receipt = {}
    if isinstance(agent_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", agent_id):
        receipt = load_json(HERMES / "state/host-receipts" / f"latest-{agent_id}.json", {})
    if (isinstance(receipt, dict) and receipt.get("verified") is True
            and receipt.get("manifest_host") == agent_id
            and binding.get("target_sha")
            and receipt.get("target_sha") == binding["target_sha"]):
        policy = receipt.get("effective_session_policy")
        approved = policy.get("compression") if isinstance(policy, dict) else None
        if isinstance(policy, dict) and "compression" not in policy:
            # The installed policy owns no compression override, so the native
            # no-cap/idle-off check below applies (fleet baseline, Nov 15 2026).
            pass
        elif (isinstance(approved, dict)
                and type(approved.get("threshold_tokens")) is int
                and approved["threshold_tokens"] > 0
                and type(approved.get("idle_compact_after_seconds")) is int
                and approved["idle_compact_after_seconds"] >= 0):
            required_compression.update(
                threshold_tokens=approved["threshold_tokens"],
                idle_compact_after_seconds=approved["idle_compact_after_seconds"])
        elif "effective_session_policy" in receipt:
            drift.append("compression installed policy is malformed")
    elif isinstance(receipt, dict) and "effective_session_policy" in receipt:
        drift.append("compression installed policy lacks verified host/target custody")
    if not required_compression:
        try:
            try:
                raw_cap = compression.get("threshold_tokens")
                cap = int(raw_cap) if raw_cap is not None else 0
            except (TypeError, ValueError):
                cap = 0  # Native _positive_int treats these values as no cap.
            idle = max(0, int(compression.get("idle_compact_after_seconds", 0)))
        except (OverflowError, TypeError, ValueError):
            drift.append("compression effective cap/idle configuration is invalid")
        else:
            if cap > 0 or idle > 0:
                drift.append("compression override lacks verified host/target policy")
    for key, expected in required_compression.items():
        actual = compression.get(key)
        if type(actual) is not int or actual != expected:
            drift.append(f"compression.{key}={actual!r}->{expected!r}")
    actual = _scalar_from_block(session_reset, "notify")
    if actual is not False:
        drift.append(f"session_reset.notify={actual!r}->False")
    actual = _scalar_from_block(telegram, "gateway_restart_notification", indent=4)
    if not manager and telegram_transport_expected() and actual is not False:
        drift.append(f"platforms.telegram.gateway_restart_notification={actual!r}->False")
    if drift:
        return {
            "name": "immersion_quality",
            "status": "fail",
            "severity": "P1",
            "fix_class": "auto_safe",
            "detail": "silent immersion policy drift: " + "; ".join(drift[:12]),
        }
    return {"name": "immersion_quality", "status": "pass", "detail": "silent immersion policy enforced"}








def _agent_probe_producer_installed():
    return (HOME / "Library/LaunchAgents/ai.hermes.agent-probe.plist").exists()


def check_agent_probe():
    p = HERMES / "state/agent-probe-latest.json"
    if not _agent_probe_producer_installed():
        return {
            "name": "agent_runtime_probe",
            "status": "skip",
            "detail": "macOS agent probe producer not installed for this runtime",
        }
    if not p.exists():
        return {
            "name": "agent_runtime_probe",
            "status": "fail",
            "detail": "agent probe state missing while launchd probe is installed",
        }
    d = load_json(p, {})
    ts = parse_dt(d.get("generated_at") or d.get("checked_at") or d.get("timestamp"))
    age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else 10**9
    gateways = d.get("per_gateway") if isinstance(d.get("per_gateway"), dict) else {}
    main = gateways.get("ai.hermes.gateway")
    if not isinstance(main, dict):
        matches = [
            body
            for body in gateways.values()
            if isinstance(body, dict) and Path(str(body.get("hermes_home") or "")).expanduser() == HERMES
        ]
        main = matches[0] if len(matches) == 1 else None
    result = main.get("result") if isinstance(main, dict) and isinstance(main.get("result"), dict) else {}
    failures = []
    if age > AGENT_PROBE_MAX_AGE_S:
        failures.append(f"stale age={age}s max={AGENT_PROBE_MAX_AGE_S}s")
    if not main:
        failures.append("primary gateway result missing")
    elif not result.get("ok"):
        failures.append(f"{result.get('kind') or 'unknown'}: {str(result.get('detail') or '')[:240]}")
    elif not result.get("runtime_root") or not isinstance(result.get("origins"), dict):
        failures.append("assembled runtime contract evidence missing")
    binding = gateway_runtime_binding()
    live_root = str(binding.get("runtime_root") or "")
    probed_root = str(result.get("runtime_root") or "")
    if live_root and probed_root and Path(live_root) != Path(probed_root):
        failures.append(f"live/probed runtime root mismatch live={live_root} probed={probed_root}")
    return {
        "name": "agent_runtime_probe",
        "status": "fail" if failures else "pass",
        "detail": "; ".join(failures) if failures else f"age={age}s runtime_root={probed_root}",
    }



def check_session_store_errors():
    """Observe gateway logs without SQLite; retain failures until a new process starts."""
    binding = gateway_runtime_binding()
    heartbeat = load_json(HERMES / "state/gateway.heartbeat", {})
    pid = binding.get("pid")
    started = heartbeat.get("start_time")
    result = {"name": "session_store_errors", "status": "warn", "severity": "P1",
              "gateway_pid": pid, "gateway_started_at": started,
              "session_store_healthy": None, "agent_turn_healthy": None,
              "detail": "gateway generation unavailable", "database_error_kinds": []}
    if (not binding.get("process_alive") or heartbeat.get("pid") != pid
            or not isinstance(started, (int, float)) or isinstance(started, bool)
            or not 946684800 < started <= time.time()):
        return result
    prior = load_json(HERMES / "state/local-selfcheck-latest.json", {})
    markers = ("SessionStore SQLite handle unavailable", "file is not a database",
               "database disk image is malformed", "disk I/O error", "SQLite session store unavailable")
    kinds = set()
    for check in prior.get("checks", []):
        if (isinstance(check, dict) and check.get("name") == result["name"]
                and check.get("gateway_pid") == pid and check.get("gateway_started_at") == started):
            kinds.update(kind for kind in (check.get("database_error_kinds") or []) if kind in markers)
    read_errors = []
    # Stream current and rotated logs; do not materialize transcripts or expose lines.
    paths = set((HERMES / "logs").glob("gateway.log*")) | set((HERMES / "logs").glob("gateway.error.log*"))
    for path in sorted(paths):
        try:
            if not path.is_file() or path.stat().st_mtime < started:
                continue
            stamp = None
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", errors="replace") as stream:
                for line in stream:
                    match = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", line)
                    if match:
                        try:
                            stamp = datetime.fromisoformat(match.group(1).replace(",", ".").replace("Z", "+00:00")).timestamp()
                        except ValueError:
                            stamp = None
                    # Whole-second log timestamps overlap the fractional startup second.
                    # Conservatively retain errors in that first second as current.
                    if stamp is not None and stamp >= int(started):
                        kinds.update(marker for marker in markers if marker.lower() in line.lower())
        except OSError as exc:
            read_errors.append(type(exc).__name__)
    result.update(status="fail" if kinds else ("warn" if read_errors else "pass"),
                  database_error_kinds=sorted(kinds),
                  detail="database errors since gateway start: " + ", ".join(sorted(kinds)) if kinds else
                         ("log inspection unavailable: " + ", ".join(sorted(set(read_errors))) if read_errors else "no database errors observed since gateway start"))
    if kinds:
        result.update(session_store_healthy=False, agent_turn_healthy=False)
    return result

def check_logs():
    paths = [HERMES / "logs/gateway.error.log", HERMES / "logs/gateway.log"]
    patterns = re.compile(r"(traceback|authenticationerror|unauthorized|permission denied|fatal|uncaught)", re.I)
    coherence_patterns = re.compile(
        r"(requested_provider|runtime coherence violation|"
        r"init_agent\(\) got an unexpected keyword argument|"
        r"run_agent origin mismatch|agent\.agent_init origin mismatch)",
        re.I,
    )
    hits = []
    coherence_hits = []
    cutoff = time.time() - 1800
    for p in paths:
        try:
            if not p.exists() or p.stat().st_mtime < cutoff:
                continue
            current_ts = None
            for line in p.read_text(errors="replace").splitlines()[-800:]:
                match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if match:
                    try:
                        current_ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        current_ts = None
                if current_ts is None or current_ts < cutoff:
                    continue
                if patterns.search(line):
                    hits.append(f"{p.name}: {line[:160]}")
                if coherence_patterns.search(line):
                    coherence_hits.append(f"{p.name}: {line[:240]}")
        except Exception:
            pass
    if coherence_hits:
        return {
            "name": "runtime_coherence_errors",
            "status": "fail",
            "detail": f"hits={len(coherence_hits)} sample={coherence_hits[-1]}",
        }
    return {
        "name": "recent_errors",
        "status": "warn" if len(hits) >= 10 else "pass",
        "detail": f"hits={len(hits)}" + ((" sample=" + hits[-1]) if hits else ""),
    }


def _normalize_email_auth_choice(text):
    value = " ".join(str(text or "").lower().replace("’", "'").split()).strip(" .!,")
    if value.startswith("i was responding to the chat"):
        return None
    if re.fullmatch(
        r"(?:yes[, ]+)?(?:please )?(?:keep|fix|restore|reauthenticate)(?: my)? email(?: access| checks)?",
        value,
    ):
        return "reauth_requested"
    if value == "stop email checks" or re.fullmatch(
        r"(?:i )?(?:don't|dont|do not) (?:care(?: about email)?|need email(?: access)?)",
        value,
    ) or re.fullmatch(
        r"stop (?:flagging|checking) (?:it|email|email auth|email access)(?: for me)?",
        value,
    ):
        return "opted_out"
    return None


def email_auth_client_choice():
    """Recognize explicit choices in checkpointed history; absence remains pending."""
    marker_path = HERMES / "state/email-auth-client-choice.json"
    marker = load_json(marker_path, {})
    if not isinstance(marker, dict):
        return "unset"
    status = str(marker.get("status") or "unset")
    if status != "pending":
        return status

    chat_id = str(marker.get("chat_id") or "").removeprefix("telegram:").split(":", 1)[0]
    prompt_message_id = str(marker.get("prompt_message_id") or "")
    prompted_at = parse_dt(marker.get("prompted_at"))
    transcript = HERMES / "data/telegram-transcript.db"
    if not chat_id or not transcript.is_file():
        return status
    try:
        before = transcript.stat()
        with sqlite3.connect(transcript.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
            rows = connection.execute(
                """
                SELECT text, reply_to_message_id, timestamp
                FROM telegram_messages
                WHERE role = 'user' AND chat_id IN (?, ?)
                ORDER BY id DESC
                LIMIT 50
                """,
                (chat_id, f"telegram:{chat_id}"),
            ).fetchall()
        connection.close()
        after = transcript.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            # A checkpoint overlapped the historical read. Keep the durable
            # policy untouched instead of acting on an inconsistent result.
            return status
    except Exception:
        return status

    for text, reply_to_message_id, timestamp in rows:
        observed_at = parse_dt(timestamp)
        if prompted_at and (not observed_at or observed_at < prompted_at):
            continue
        decision = _normalize_email_auth_choice(text)
        if not decision:
            continue
        if prompt_message_id and str(reply_to_message_id or "") not in {"", prompt_message_id}:
            continue
        marker.update(
            {
                "status": decision,
                "selected_at": observed_at.isoformat() if observed_at else iso(),
            }
        )
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path = marker_path.with_suffix(".json.pending")
        pending_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pending_path.replace(marker_path)
        return decision
    return status


def configured_readiness_core():
    """Use native surface resolution; unresolved configuration stays conservative."""
    default = {"api_key_validity", "firecrawl", "browser", "email", "mcp_servers"}
    try:
        import yaml
        from hermes_cli.tools_config import _get_platform_tools
        from toolsets import resolve_toolset

        config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8-sig")) or {}
        platforms = config.get("platforms") or {}
        surfaces = {"cli"} | set(config.get("platform_toolsets") or {}) | set(platforms)
        exposed = set()
        for surface in surfaces:
            if (platforms.get(surface) or {}).get("enabled") is not False:
                exposed.update(_get_platform_tools(config, surface))
        tools = {tool for name in exposed for tool in resolve_toolset(name)}
        core = {"api_key_validity"}
        if tools & {"web_search", "web_extract"}:
            core.add("firecrawl")
        if any(tool.startswith("browser_") for tool in tools):
            core.add("browser")
        servers = config.get("mcp_servers") or {}
        enabled_servers = {
            name: entry for name, entry in servers.items()
            if isinstance(entry, dict) and entry.get("enabled", True) is not False
            and (name in exposed or f"mcp-{name}" in exposed)
        }
        if enabled_servers:
            core.add("mcp_servers")
        if "email" in exposed or any(
            "composio" in name.lower() and any(
                word in (name + " " + str((entry.get("metadata") or {}).get("description", ""))).lower()
                for word in ("gmail", "google")
            ) for name, entry in enabled_servers.items()
        ):
            core.add("email")
        # The readiness producer also supports these existing legacy email routes.
        env = _read_env_keys()
        if any(env.get(key) for key in ("COMPOSIO_API_KEY", "MATON_API_KEY")):
            core.add("email")
        return core
    except Exception:
        return default


def check_tool_readiness():
    p = HERMES / "state/tool-readiness-probe-latest.json"
    if not p.exists():
        return {"name": "tool_readiness", "status": "skip", "detail": "no tool-readiness state"}
    d = load_json(p, {})
    ts = parse_dt(d.get("timestamp") or d.get("generated_at") or d.get("checked_at"))
    age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else 10**9
    email_choice = email_auth_client_choice()
    core = configured_readiness_core()
    if email_choice in {"opted_out", "pending"}:
        core.discard("email")
    unhealthy = []
    for name, body in (d.get("tools") or {}).items():
        st = str((body or {}).get("status") or "")
        if name in core and st in {"broken", "degraded", "error"}:
            unhealthy.append(f"{name}:{st}")
    firecrawl_smoke = str((((d.get("tools") or {}).get("firecrawl") or {}).get("smoke") or "missing"))
    status = "fail" if age > 2 * 3600 or unhealthy else "pass"
    return {
        "name": "tool_readiness",
        "status": status,
        "detail": (
            f"age={age}s firecrawl_smoke={firecrawl_smoke} "
            f"email_policy={email_choice} "
            f"transcript_freshness=checkpointed "
            f"core_unhealthy={unhealthy[:8]}"
        ),
    }


def check_workspace_write():
    workspace = HERMES / "workspace"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=".selfcheck-write-", dir=str(workspace))
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("workspace-write-proof\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            path.unlink(missing_ok=True)
        return {"name": "workspace_write", "status": "pass", "detail": f"create_remove_ok={workspace}"}
    except Exception as exc:
        return {
            "name": "workspace_write",
            "status": "fail",
            "detail": f"create/remove failed at {workspace}: {type(exc).__name__}: {str(exc)[:120]}",
        }


def check_image_quality_surface():
    """Verify image guidance is backed by an executable Telegram tool lane."""
    config_path = HERMES / "config.yaml"
    if not config_path.is_file():
        return {
            "name": "image_quality_surface",
            "status": "fail",
            "detail": f"missing config: {config_path}",
        }
    text = config_path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        import yaml

        config = yaml.safe_load(text) or {}
        platforms = config.get("platforms") or {}
        telegram = platforms.get("telegram")
        configured = "telegram" in platforms or "telegram" in (config.get("platform_toolsets") or {})
        if not configured or (isinstance(telegram, dict) and telegram.get("enabled") is False):
            return {"name": "image_quality_surface", "status": "skip", "detail": "not required"}
    except Exception:
        return {"name": "image_quality_surface", "status": "fail", "detail": "unreadable config"}
    image_gen = _top_level_block(text, "image_gen")
    provider = str(_scalar_from_block(image_gen, "provider") or "").strip()
    model = str(_scalar_from_block(image_gen, "model") or "").strip()
    platform_toolsets = _top_level_block(text, "platform_toolsets")
    required_tools = {"image_gen", "skills", "vision"}
    present_tools = _nested_list_items(platform_toolsets, "telegram")
    missing_tools = sorted(required_tools - present_tools)
    required_paths = {
        "routing_rule": HERMES / "shared-rules/image-generation-routing.md",
        "creative_skill": HERMES / "skills/internal/creative-output-escalation/SKILL.md",
    }
    missing_paths = [name for name, path in required_paths.items() if not path.is_file()]
    ok = bool(provider and model) and not missing_tools and not missing_paths
    return {
        "name": "image_quality_surface",
        "status": "pass" if ok else "fail",
        "detail": (
            f"provider={provider or 'missing'} model={model or 'missing'} "
            f"missing_tools={missing_tools} missing_paths={missing_paths}"
        ),
    }


def check_document_visual_delivery():
    """Verify the actual Telegram surface exposes vision and the release gate."""
    marker = HERMES / "state/required-canaries/document-visual-delivery"
    if not marker.is_file():
        return {
            "name": "document_visual_delivery",
            "status": "skip",
            "detail": f"not required for this runtime (marker absent: {marker})",
        }
    binding = load_json(HERMES / "state/runtime-binding.json", {})
    runtime = Path(str(binding.get("runtime_root") or ""))
    python = Path(str(binding.get("runtime_python") or ""))
    if (
        binding.get("kind") != "botdoctor_runtime_binding"
        or binding.get("status") != "active"
        or not runtime.is_absolute()
        or not python.is_absolute()
        or not python.is_relative_to(runtime)
    ):
        return {
            "name": "document_visual_delivery",
            "status": "fail",
            "detail": "active runtime binding is missing or invalid",
        }
    cli = runtime / "hermes_cli/main.py"
    gate = HERMES / "bin/client-doc-artifact-qa"
    missing = []
    if not python.is_file():
        missing.append(f"runtime python missing: {python}")
    if not cli.is_file():
        missing.append(f"Hermes CLI missing: {cli}")
    if not gate.is_file():
        missing.append(f"document QA gate missing: {gate}")
    if missing:
        return {
            "name": "document_visual_delivery",
            "status": "fail",
            "detail": "; ".join(missing),
        }
    probe = run(
        [str(python), str(cli), "tools", "list", "--platform", "telegram"],
        timeout=20,
    )
    output = (probe.stdout or "") + "\n" + (probe.stderr or "")
    vision_enabled = bool(re.search(r"(?m)^\s*[✓+]\s+enabled\s+vision\b", output))
    if probe.returncode != 0 or not vision_enabled:
        detail = (
            f"telegram vision enabled={vision_enabled} cli_rc={probe.returncode}; "
            + output.strip().replace("\n", " ")[:240]
        )
        return {"name": "document_visual_delivery", "status": "fail", "detail": detail}
    return {
        "name": "document_visual_delivery",
        "status": "pass",
        "detail": "Telegram resolves vision toolset and document QA gate is installed",
    }




def check_canary_reconciler():
    if api_only_profile():
        result = check_gateway_state()
        return {**result, "name": "canary_reconciler",
                "detail": "configured API-only surface: " + result["detail"]}
    manager = _local_manager_binding()
    if manager:
        return check_local_manager_health(manager)
    p = HERMES / "state/canary-reconciler-latest.json"
    if not p.exists():
        return {
            "name": "canary_reconciler",
            "status": "fail",
            "severity": "P1",
            "fix_class": "restart_or_redeploy",
            "detail": "required canary reconciler state is missing",
        }
    d = load_json(p, {})
    ts = parse_dt(d.get("checked_at") or d.get("timestamp") or d.get("generated_at"))
    age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else 10**9
    missing = d.get("missing_canaries") or []
    inventory = []
    for c in d.get("capabilities") or []:
        canary = c.get("canary") or {}
        if canary.get("status") == "inventory_only":
            inventory.append(c.get("id"))
    raw_reconciler_ok = d.get("ok") is True
    failed_actions = d.get("failed_actions") or []
    maintenance_warnings = d.get("warnings") or []
    # The reconciler runs this self-check as one of its canaries. If the last
    # reconciler cycle failed only because this self-check observed the prior
    # red reconciler state, treating that action as a fresh P1 creates a
    # permanent false-red feedback loop. The current self-check invocation is
    # authoritative for local_selfcheck; all other failed actions still block.
    local_selfcheck_failures = [
        action
        for action in failed_actions
        if action.get("canary") == "local_selfcheck"
    ]
    blocking_failed_actions = [
        action
        for action in failed_actions
        if action.get("canary") != "local_selfcheck"
    ]
    selfcheck_only_latch = (
        not raw_reconciler_ok
        and bool(local_selfcheck_failures)
        and not blocking_failed_actions
        and not missing
    )
    reconciler_ok = raw_reconciler_ok or selfcheck_only_latch
    status = (
        "fail"
        if missing or not reconciler_ok or blocking_failed_actions
        else "warn"
        if age > 7200 or maintenance_warnings
        else "pass"
    )
    detail = (
        f"age={age}s capabilities={len(d.get('capabilities') or [])} "
        f"missing={len(missing)} failed_actions={len(blocking_failed_actions)} "
        f"ignored_local_selfcheck_failures={len(local_selfcheck_failures)} ok={reconciler_ok} "
        f"inventory_only={inventory[:8]} maintenance_warnings={len(maintenance_warnings)}"
    )
    result = {
        "name": "canary_reconciler",
        "status": status,
        "detail": detail,
    }
    if status == "fail":
        result.update({"severity": "P1", "fix_class": "restart_or_redeploy"})
    return result


def check_disk_retention():
    path = HERMES / "state/disk-retention-last.json"
    if not path.exists():
        return {
            "name": "disk_retention",
            "status": "warn",
            "detail": "no disk-retention receipt yet",
        }
    payload = load_json(path, {})
    timestamp = parse_dt(
        payload.get("checked_at")
        or payload.get("timestamp")
        or payload.get("generated_at")
    )
    age = int((datetime.now(timezone.utc) - timestamp).total_seconds()) if timestamp else 10**9
    retention_status = str(payload.get("status") or "unknown")
    errors = payload.get("errors") or []
    referenced = ((payload.get("runtime_protection") or {}).get("referenced") or [])
    status = "pass"
    if retention_status in {"error", "block"} or errors:
        status = "fail"
    elif age > 172800 or retention_status != "pass":
        status = "warn"
    return {
        "name": "disk_retention",
        "status": status,
        "detail": (
            f"age={age}s retention_status={retention_status} errors={len(errors)} "
            f"referenced_runtime_dependencies={len(referenced)} "
            f"deleted={len(payload.get('deleted') or [])} "
            f"free_after_bytes={payload.get('free_after_bytes')}"
        ),
    }


def _read_env_keys(env_paths=None):
    keys = {}
    for env_path in (env_paths if env_paths is not None else (HERMES / ".env", HERMES / ".env.secrets")):
        try:
            lines = env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except Exception:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                keys[key] = value.strip().strip('"').strip("'")
    return keys


def _keychain_has_env_key(key):
    """Recognize a macOS Keychain-backed capability without reading its value."""
    if sys.platform != "darwin":
        return False
    result = run(
        ["/usr/bin/security", "find-generic-password", "-s", key],
        timeout=3,
    )
    return result.returncode == 0


def _linux_process_start_time(stat):
    """Return immutable /proc stat starttime (field 22)."""
    closing = stat.rfind(b")")
    if closing < 0:
        raise ValueError("process stat has no command terminator")
    fields = stat[closing + 1 :].split()
    if len(fields) <= 19:
        raise ValueError("process stat is truncated")
    return int(fields[19])


def _live_process_start_time(pid):
    """Return the same PID-reuse fingerprint persisted by gateway.status."""
    if sys.platform.startswith("linux"):
        try:
            return _linux_process_start_time((PROC_ROOT / str(pid) / "stat").read_bytes())
        except (OSError, ValueError):
            return None
    binding_path = HERMES / "state/runtime-binding.json"
    if not binding_path.is_file() or binding_path.is_symlink():
        return None
    binding = load_json(binding_path, {})
    if (
        binding.get("schema_version") != 1
        or binding.get("kind") != "botdoctor_runtime_binding"
        or binding.get("status") != "active"
        or runtime_release_identity().get("status") != "pass"
    ):
        return None
    try:
        expected_home = HERMES.resolve(strict=True)
        bound_home = Path(str(binding.get("hermes_home") or "")).resolve(strict=True)
        runtime_root = Path(str(binding.get("runtime_root") or "")).resolve(strict=True)
        runtime_python = Path(str(binding.get("runtime_python") or "")).expanduser().absolute()
    except OSError:
        return None
    expected_python = runtime_root / (
        "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"
    )
    home_bound = bound_home == expected_home
    if os.name == "nt" and (binding.get("service") or {}).get("kind") == "windows-scheduled-task":
        home_bound = bool(runtime_host_receipt(binding)) and runtime_root.is_relative_to(expected_home)
    if (
        not home_bound
        or not runtime_root.is_dir()
        or runtime_python != expected_python
        or not runtime_python.is_file()
    ):
        return None
    probe = run(
        [
            str(runtime_python),
            "-c",
            (
                "import os, sys, tempfile\n"
                "with tempfile.TemporaryDirectory(prefix='hermes-process-probe-') as home:\n"
                " os.environ['HERMES_HOME'] = home\n"
                " sys.path.insert(0, sys.argv[2])\n"
                " from gateway.status import get_process_start_time\n"
                " value=get_process_start_time(int(sys.argv[1]))\n"
                " print('' if value is None else value)"
            ),
            str(pid),
            str(runtime_root),
        ],
        timeout=5,
    )
    if probe.returncode != 0:
        return None
    try:
        return int((probe.stdout or "").strip())
    except ValueError:
        return None


def _gateway_env_present_keys():
    """Read names from the current gateway generation, without a stored receipt."""
    pid_path = HERMES / "gateway.pid"
    if not pid_path.is_file() or pid_path.is_symlink():
        return frozenset()
    record = load_json(pid_path, {})
    if not isinstance(record, dict) or record.get("kind") != "hermes-gateway":
        return frozenset()
    try:
        pid = int(record.get("pid") or 0)
        start_time = int(record.get("start_time"))
        if pid <= 0 or not pid_is_alive(pid) or _live_process_start_time(pid) != start_time:
            return frozenset()
        if sys.platform.startswith("linux"):
            entries = (PROC_ROOT / str(pid) / "environ").read_bytes().split(b"\0")
            keys = [entry.split(b"=", 1)[0].decode("ascii") for entry in entries
                    if b"=" in entry and entry.split(b"=", 1)[1]]
        else:
            # _live_process_start_time validates this profile's exact bound interpreter.
            binding = load_json(HERMES / "state/runtime-binding.json", {})
            probe = run([
                str(binding["runtime_python"]), "-c",
                "import json, psutil, sys; print(json.dumps([k for k,v in psutil.Process(int(sys.argv[1])).environ().items() if v]))",
                str(pid),
            ], timeout=5)
            if probe.returncode != 0:
                return frozenset()
            keys = json.loads(probe.stdout)
        if (_live_process_start_time(pid) != start_time
                or load_json(pid_path, {}) != record
                or not isinstance(keys, list)
                or not all(isinstance(key, str) for key in keys)):
            return frozenset()
        return frozenset(keys)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return frozenset()



def _gateway_api_state_transaction(*, full_document=False):
    """Read authenticated native health, retaining its actual proof contract."""
    import urllib.request
    env = _read_env_keys()
    key = os.environ.get("API_SERVER_KEY") or env.get("API_SERVER_KEY")
    if not key:
        # The approved tenant API may keep its key outside the shared dotenv.
        # Only accept this profile's fixed managed location; never a receipt's
        # arbitrary path or a sibling profile's credential.
        receipt = load_json(HERMES / "state/tenant-local-api/receipt.json", {})
        relative = "state/tenant-local-api/managed/.env"
        managed = HERMES / relative
        if (isinstance(receipt, dict)
                and receipt.get("home")
                and Path(receipt["home"]).resolve() == HERMES.resolve()
                and receipt.get("credential_file") == relative
                and managed.resolve().is_relative_to(HERMES.resolve())):
            env = _read_env_keys((managed,))
            key = env.get("API_SERVER_KEY")
    if not key:
        raise RuntimeError("gateway API credential unavailable")
    port = int(os.environ.get("API_SERVER_PORT") or env.get("API_SERVER_PORT") or 8642)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health/detailed?diagnostics=1",
        headers={"Authorization": f"Bearer {key}"},
    )
    # Local owner only; do not send its credential through an HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("gateway API health document is not a mapping")
    if full_document:
        return payload
    return payload.get("state_transaction")


def _gateway_socket_state_transaction():
    import socket
    import stat

    if os.name == "nt":
        return None
    path = HERMES / "gateway.sock"
    connected = False
    try:
        if not path.exists():
            pointer = HERMES / "gateway.sock.path"
            metadata = pointer.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022 or metadata.st_size > 4096):
                return None
            path = Path(pointer.read_text(encoding="utf-8").strip())
            home_hash = hashlib.sha256(os.path.normcase(str(HERMES.resolve())).encode("utf-8")).hexdigest()[:16]
            if not path.is_absolute() or path.name != f"hermes-gw-{home_hash}.sock":
                return None
        metadata = path.lstat()
        if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            return None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            deadline = time.monotonic() + 10
            client.settimeout(10)
            client.connect(str(path))
            connected = True
            client.sendall(b'{"verb":"state-transaction-health","protocol":1}\n')
            payload = bytearray()
            while b"\n" not in payload:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("gateway socket diagnostics timed out")
                client.settimeout(remaining)
                chunk = client.recv(65536)
                if not chunk:
                    raise ValueError("incomplete gateway socket diagnostics response")
                payload.extend(chunk)
                if len(payload) > 512 * 1024:
                    raise ValueError("gateway socket diagnostics response too large")
        response = json.loads(payload.partition(b"\n")[0])
    except OSError as exc:
        if connected:
            raise ValueError("gateway socket diagnostics transport failed") from exc
        return None
    if (not isinstance(response, dict) or type(response.get("protocol")) is not int
            or response["protocol"] != 1):
        raise ValueError("invalid gateway socket diagnostics response")
    if response.get("ok") is not True:
        supported = response.get("supported_verbs")
        if (response.get("error") == "unknown verb: 'state-transaction-health'"
                and isinstance(supported, list) and "state-transaction-health" not in supported):
            return None
        raise ValueError("gateway socket diagnostics failed")
    health = response.get("result")
    if not isinstance(health, dict):
        raise ValueError("invalid gateway socket diagnostics result")
    return dict(health, _transport="gateway_socket")


def _gateway_state_transaction():
    """Read fresh evidence from the owning gateway; never open state.db."""
    health = _gateway_socket_state_transaction()
    if health is None:
        health = _gateway_api_state_transaction()
    if not isinstance(health, dict):
        raise RuntimeError("gateway state transaction is unavailable, failed, or stale")
    live = load_json(HERMES / "gateway_state.json", {})
    # The producer fills these two fields before optional activity statistics.
    # A budget expiry after both fields cannot invalidate their completed reads;
    # real store failures replace error_type with the latched failure class.
    required_reads_complete = (
        isinstance(health, dict)
        and all(type(health.get(name)) is int and health[name] >= 0
                for name in ("topic_bindings", "credential_tool_errors"))
    )
    optional_statistics_expired = (
        required_reads_complete and health.get("error_type") == "DiagnosticBudgetExceeded"
    )
    if (not isinstance(health, dict)
            or not required_reads_complete
            or (not optional_statistics_expired and (
                health.get("database_status", health.get("status")) != "pass"
                or ("database_status" in health and health.get("diagnostics_complete") is not True)
                or health.get("diagnostics_complete") is False))
            or health.get("pid") != live.get("pid")
            or not isinstance(health.get("checked_at"), (int, float))
            or not 0 <= time.time() - health["checked_at"] <= 30):
        raise RuntimeError("gateway state transaction is unavailable, failed, or stale")
    return health


def check_local_brain():
    candidates = [HOME / ".gbrain", HOME / "Documents" / "Brain" / "Principal Brain", HERMES / ".gbrain"]
    present = [str(path) for path in candidates if path.exists()]
    if not present:
        manifest = load_json(HERMES / "state/native-agent-continuity/manifest.json", {})
        paths = manifest.get("paths") or {}
        brain_home = Path(str(paths.get("gbrain_home") or ""))
        if (manifest.get("schema") == "native-agent-continuity-manifest/v1"
                and manifest.get("storage_mode") == "existing_remote"
                and Path(str(paths.get("home") or "")) == HOME
                and Path(str(paths.get("hermes_home") or "")) == HERMES
                and brain_home.is_absolute() and brain_home.is_relative_to(HOME)
                and (brain_home / ".gbrain/config.json").is_file()):
            return {"name": "local_brain", "status": "pass",
                    "detail": "existing remote GBrain configuration present"}
    status = "pass" if present else "warn"
    detail = "present=" + ",".join(present[:3]) if present else "no local gbrain/principal brain path found"
    return {"name": "local_brain", "status": status, "detail": detail}


def _principal_home():
    """The user root that owns this runtime's brain, independent of the process account.

    Order: native-agent-continuity manifest, the parent of a standard ``.hermes`` home, then the
    nearest ancestor of HERMES that holds a ``.gbrain`` directory (Windows service runtimes).
    """
    manifest = load_json(HERMES / "state/native-agent-continuity/manifest.json", {})
    configured = str((manifest.get("paths") or {}).get("home") or "")
    if (manifest.get("schema") == "native-agent-continuity-manifest/v1"
            and Path(str((manifest.get("paths") or {}).get("hermes_home") or "")) == HERMES
            and Path(configured).is_absolute()):
        return Path(configured)
    if HERMES.name == ".hermes":
        return HERMES.parent
    for ancestor in HERMES.resolve().parents:
        if (ancestor / ".gbrain").is_dir():
            return ancestor
    return None


def check_gateway_brain_binding():
    """Fail when this principal has a brain but the gateway has no usable GBrain MCP.

    A `gbrain` shell command is not a binding: agent terminals may not see it.
    """
    # A proprietary principal Brain is not a native GBrain store. Require an
    # explicit owner declaration and an actual read, never merely tools/list.
    try:
        import yaml
        declared_config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8-sig")) or {}
    except FileNotFoundError:
        declared_config = {}
    except Exception as exc:
        return {"name": "gateway_brain_binding", "status": "fail",
                "detail": f"config.yaml unreadable: {type(exc).__name__}"}
    if isinstance(declared_config, dict) and "principal_brain" in declared_config:
        return _check_declared_principal_brain(declared_config)
    home = _principal_home()
    manifest = load_json(HERMES / "state/native-agent-continuity/manifest.json", {})
    pinned = str((manifest.get("paths") or {}).get("gbrain_home") or "")
    if pinned and Path(pinned).is_absolute():
        stores = [Path(pinned)]
    else:
        # ``home / ".gbrain"`` covers a brain opened with GBRAIN_HOME=~/.gbrain, whose config
        # lives at ~/.gbrain/.gbrain/config.json (one client runtime). Without it that brain looks foreign.
        candidates = (home, home / ".gbrain" if home else None, HERMES, HERMES / "state/gbrain")
        stores = [path for path in candidates if path and (path / ".gbrain").exists()]
    if not stores:
        return {"name": "gateway_brain_binding", "status": "pass", "detail": "no principal brain on this host"}
    try:
        import yaml

        config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8-sig")) or {}
    except Exception as e:
        return {"name": "gateway_brain_binding", "status": "fail",
                "detail": f"config.yaml unreadable: {type(e).__name__}"}
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    servers = servers if isinstance(servers, dict) else {}
    agent = config.get("agent")
    disabled = set((agent.get("disabled_toolsets") or []) if isinstance(agent, dict) else [])
    root = home or HERMES
    bound = sorted(
        name for name, entry in servers.items()
        if isinstance(entry, dict)
        and entry.get("enabled", True) is not False and f"mcp-{name}" not in disabled
        and _brain_binding_targets_principal(entry, root, HERMES, stores)
        and _serves_brain(_probe_brain_mcp(entry, HERMES))
    )
    if bound:
        return {"name": "gateway_brain_binding", "status": "pass",
                "detail": "enabled GBrain MCP: " + ",".join(bound)}
    present = sorted(name for name in servers if str(name).startswith("gbrain"))
    detail = ("GBrain MCP present but disabled, remote, not this principal's store, or did not serve get_page/search: "
              + ",".join(present) if present
              else "principal brain exists but config.yaml has no GBrain MCP server")
    return {"name": "gateway_brain_binding", "status": "fail", "detail": detail}


def _check_declared_principal_brain(config):
    declaration = config.get("principal_brain")
    declaration = declaration if isinstance(declaration, dict) else {}
    name = declaration.get("mcp_server")
    probe = declaration.get("read_probe")
    servers = config.get("mcp_servers")
    entry = servers.get(name) if isinstance(servers, dict) and isinstance(name, str) else None
    agent = config.get("agent")
    disabled = (agent.get("disabled_toolsets") or []) if isinstance(agent, dict) else []
    result = {"name": "gateway_brain_binding", "status": "fail"}
    if (not isinstance(entry, dict) or entry.get("enabled", True) is False
            or f"mcp-{name}" in disabled or "mcp" in disabled
            or not isinstance(probe, dict) or not isinstance(probe.get("tool"), str)
            or not isinstance(probe.get("arguments"), dict)):
        return {**result, "detail": "principal_brain requires an enabled MCP server and a read_probe tool/arguments"}
    # Run the configured transport in a finite child. Never emit route values,
    # credentials, page contents or exception messages in health publication.
    code = ("import runpy,sys; m=runpy.run_path(sys.argv[1]); "
            "m['_declared_principal_brain_probe']()")
    proc = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(Path(__file__).resolve())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        output, _ = proc.communicate(input=json.dumps(declaration).encode(), timeout=55)
        if proc.returncode == 0 and output.strip() == b"principal-brain-read-ok":
            return {**result, "status": "pass", "detail": f"declared principal Brain MCP live read succeeded: {name}"}
        return {**result, "detail": f"declared principal Brain MCP read failed or was not read-only: {name}"}
    except subprocess.TimeoutExpired:
        return {**result, "detail": f"declared principal Brain MCP read timed out: {name}"}
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()


def _declared_principal_brain_probe():
    """Private finite reader; config names the principal server and known page."""
    import asyncio
    from contextlib import AsyncExitStack
    import yaml
    from dotenv import load_dotenv
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def read():
        config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8-sig"))
        declaration = json.load(sys.stdin)
        entry = config["mcp_servers"][declaration["mcp_server"]]
        probe = declaration["read_probe"]
        load_dotenv(HERMES / ".env", override=False)
        expand = lambda value: os.path.expandvars(str(value))
        async with AsyncExitStack() as stack:
            if entry.get("url"):
                url = expand(entry["url"])
                headers = {key: expand(value) for key, value in (entry.get("headers") or {}).items()}
                if entry.get("transport") == "sse" or url.rstrip("/").endswith("/sse"):
                    from mcp.client.sse import sse_client
                    streams = await stack.enter_async_context(sse_client(url, headers=headers, timeout=40))
                else:
                    from mcp.client.streamable_http import streamable_http_client
                    from mcp.shared._httpx_utils import create_mcp_http_client
                    client = await stack.enter_async_context(create_mcp_http_client(headers=headers))
                    streams = await stack.enter_async_context(streamable_http_client(url, http_client=client))
            else:
                env = dict(os.environ)
                env.update({key: expand(value) for key, value in (entry.get("env") or {}).items()})
                cwd = Path(os.path.expanduser(str(entry.get("cwd") or HERMES)))
                cwd = cwd if cwd.is_absolute() else HERMES / cwd
                parameters = StdioServerParameters(
                    command=expand(entry["command"]), args=[expand(value) for value in entry.get("args", [])],
                    env=env, cwd=str(cwd),
                )
                streams = await stack.enter_async_context(stdio_client(parameters))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            tools = await session.list_tools()
            tool = next((tool for tool in tools.tools if tool.name == probe["tool"]), None)
            # Legacy Wiki.js adapters predate MCP annotations. Only their exact
            # page-read operation has a name-based compatibility allowance.
            if tool is None or not (
                tool.name == "wiki_get_page_by_path"
                or (tool.model_dump(by_alias=True).get("annotations") or {}).get("readOnlyHint") is True
            ):
                return False
            answer = await session.call_tool(tool.name, probe["arguments"])
            result = answer.model_dump(by_alias=True)
            text = any(
                isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str) and block["text"].strip()
                for block in result.get("content") or []
            )
            return not result.get("isError", False) and bool(text or result.get("structuredContent"))

    try:
        success = asyncio.run(asyncio.wait_for(read(), timeout=45))
    except Exception:
        success = False
    if success:
        print("principal-brain-read-ok")
    raise SystemExit(0 if success else 1)


def _serves_brain(tools):
    """Same as native-agent-continuity.serves_brain (deployed separately)."""
    return bool(tools) and ({"get_page", "search"} <= set(tools) or tools == ["gbrain_status", "lock_held"])


def _probe_brain_mcp(entry, base, timeout = 60):
    """Launch a stdio MCP entry exactly as configured and return its tool names.

    Waits for each reply before the next message and closes stdin last, as a gateway does. Returns None when the process
    cannot start, times out, or answers without a tool list. Same as native-agent-continuity.probe_brain_mcp (deployed separately).
    """
    command = str(entry.get("command") or "")
    args = [str(value) for value in (entry.get("args") or [])]
    env = os.environ.copy()
    if isinstance(entry.get("env"), dict):
        env.update({str(key): str(value) for key, value in entry["env"].items()})
    cwd = Path(os.path.expanduser(str(entry.get("cwd") or base)))
    cwd = cwd if cwd.is_absolute() else base / cwd
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "botdoctor-brain-binding-probe", "version": "1"}}}
    try:
        proc = subprocess.Popen(
            [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env, cwd=str(cwd) if cwd.is_dir() else None,
        )
    except OSError:
        return None
    replies = queue.Queue()
    errors = []
    threading.Thread(target=lambda: [replies.put(line) for line in proc.stdout], daemon=True).start()
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(message):
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def reply(wanted):
        while time.monotonic() < deadline:
            try:
                line = replies.get(timeout=max(0.05, min(1.0, deadline - time.monotonic())))
            except queue.Empty:
                if proc.poll() is not None and replies.empty():
                    return None
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == wanted:
                return message
        return None

    tools = None
    try:
        if send(initialize) and reply(1) is not None:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            if send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}):
                answer = reply(2)
                tools = ((answer or {}).get("result") or {}).get("tools")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if not isinstance(tools, list):
        return None
    names = [str(row.get("name")) for row in tools if isinstance(row, dict)]
    # A single-writer brain already open in the live gateway's own server answers in
    # status-only mode with reason lock_held. That proves this entry launches the real
    # server for an existing, healthy store; any other status-mode reason is a failure.
    if names == ["gbrain_status"] and '"reason":"lock_held"' in "".join(errors).replace(" ", ""):
        return ["gbrain_status", "lock_held"]
    return names


def _brain_binding_targets_principal(entry, home, base, expected):
    """True when an MCP entry can run and opens one of the ``expected`` GBrain stores.

    A store is identified by the engine and database named in ``<GBRAIN_HOME>/.gbrain/config.json``
    (GBRAIN_HOME, else HOME, from the entry's env), so two homes that share one database match and
    an alternate store under the same home does not. Remote URL entries carry no verifiable store
    identity and never satisfy the binding. Every path the entry passes must stay inside ``home``.
    Relative values resolve against the entry's cwd (or ``base``). Same rule as native-agent-continuity.binding_targets_principal (deployed separately).
    """
    if entry.get("url"):
        return False
    cwd = Path(os.path.expanduser(str(entry.get("cwd") or base)))
    cwd = cwd if cwd.is_absolute() else base / cwd

    def resolve(value):
        text = os.path.expanduser(str(value))
        if os.path.isabs(text):
            return Path(text).resolve()
        if "/" in text or "\\" in text or text.startswith("."):
            return (cwd / text).resolve()
        return None

    command = str(entry.get("command") or "")
    if not command:
        return False
    command_path = resolve(command)
    if command_path is None:
        if shutil.which(command) is None:
            return False
    elif not (command_path.is_file() and os.access(command_path, os.X_OK)):
        return False
    root = Path(home).resolve()
    env = entry.get("env")
    env = env if isinstance(env, dict) else {}
    for value in [*(entry.get("args") or []), *env.values()]:
        path = resolve(value)
        if path is not None and not path.is_relative_to(root):
            return False
    store_home = resolve(env.get("GBRAIN_HOME") or env.get("HOME") or "")
    # API-only named profiles may own a packaged Brain adapter rather than
    # the Unix account's local GBrain database. Its executable source must be
    # profile-local; the caller still requires actual search/get_page tools.
    if api_only_profile() and store_home is None:
        package = (base / "mcp-servers").resolve()
        arguments = [resolve(value) for value in entry.get("args") or []]
        return (package.is_relative_to(base.resolve())
                and all(path is None or path.is_relative_to(base.resolve()) for path in arguments)
                and any(path is not None and path.is_relative_to(package)
                        and path.suffix == ".py" and path.is_file() for path in arguments))
    if store_home is None or not store_home.is_relative_to(root):
        return False
    wanted = [Path(path).resolve() for path in expected]
    if store_home in wanted:
        return True
    identity = _brain_store_identity(store_home)
    return identity is not None and identity in {_brain_store_identity(path) for path in wanted}


def _brain_store_identity(gbrain_home):
    """(engine, database) from a GBrain home's config, or None when it has no readable config."""
    config_dir = Path(gbrain_home) / ".gbrain"
    try:
        config = json.loads((config_dir / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    if config.get("database_url"):
        return (str(config.get("engine") or ""), "url", str(config["database_url"]))
    if config.get("database_path"):
        db = Path(os.path.expanduser(str(config["database_path"])))
        return (str(config.get("engine") or ""), "path", str((db if db.is_absolute() else config_dir / db).resolve()))
    return None




def check_advertised_tool_env():
    cfg = HERMES / "config.yaml"
    if not cfg.exists():
        return {"name": "advertised_tool_env", "status": "warn", "detail": f"missing config: {cfg}"}
    try:
        text = cfg.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return {
            "name": "advertised_tool_env",
            "status": "warn",
            "detail": f"config read failed: {type(e).__name__}",
        }
    env_keys = _read_env_keys()
    refs = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)))
    capability_refs = [k for k in refs if any(token in k for token in ("API_KEY", "TOKEN", "SECRET", "OAUTH"))]
    unresolved = [
        k
        for k in capability_refs
        if not env_keys.get(k)
        and not os.environ.get(k)
        and not _keychain_has_env_key(k)
    ]
    gateway_present_keys = (
        _gateway_env_present_keys()
        if unresolved
        else frozenset()
    )
    missing = [key for key in unresolved if key not in gateway_present_keys]
    required_missing = [key for key in missing if key not in OPTIONAL_CAPABILITY_ENV_KEYS]
    optional_missing = [key for key in missing if key in OPTIONAL_CAPABILITY_ENV_KEYS]
    if optional_missing:
        try:
            import yaml
            servers = (yaml.safe_load(text) or {}).get("mcp_servers") or {}
            for key in optional_missing[:]:
                consumers = [server for server in servers.values() if isinstance(server, dict)
                             and "${" + key + "}" in str(server.get("env") or {})]
                if key in KEYLESS_MCP_ENV_ARGS and consumers and all(
                    server.get("args") == KEYLESS_MCP_ENV_ARGS[key] for server in consumers):
                    optional_missing.remove(key)
        except (ImportError, ValueError, AttributeError):
            pass
    status = "fail" if required_missing else ("warn" if optional_missing else "pass")
    return {
        "name": "advertised_tool_env",
        "status": status,
        "detail": (
            f"refs={len(capability_refs)} required_missing={required_missing[:12]} "
            f"optional_missing={optional_missing[:12]}"
        ),
    }




CHECK_METADATA = {
    "gateway_brain_binding": {
        "title": "Agent cannot reach its own brain",
        "severity": "P1",
        "fix_class": "gateway_brain_binding",
        "recommended_action": (
            "Run native-agent-continuity reconcile --force, or add/enable mcp_servers.gbrain for this "
            "principal's brain; the gateway hot-reconciles MCP config without a restart."
        ),
    },
    "advertised_tool_env": {
        "title": "Advertised tool env missing",
        "severity": "P2",
        "fix_class": "tool_env_contract",
        "recommended_action": (
            "Either provide the referenced env key or stop advertising the capability in this profile."
        ),
    },
    "tool_readiness": {
        "title": "Tool readiness stale or broken",
        "severity": "P2",
        "fix_class": "tool_readiness_refresh",
        "recommended_action": (
            "Refresh the local tool readiness probe and repair broken core tools before client-visible use."
        ),
    },
    "image_quality_surface": {
        "title": "Native image-quality lane unavailable",
        "severity": "P1",
        "fix_class": "image_quality_surface",
        "recommended_action": (
            "Restore image_gen provider/model, Telegram image_gen/skills/vision exposure, "
            "and the Golden image routing rule plus creative skill before image work."
        ),
    },
    "document_visual_delivery": {
        "title": "Telegram document visual-delivery lane unavailable",
        "severity": "P1",
        "fix_class": "document_visual_delivery",
        "recommended_action": (
            "Restore the Telegram vision toolset and evidence-bound document QA "
            "gate, then rerun the private document canary."
        ),
    },
    "agent_runtime_probe": {
        "title": "Agent runtime constructor probe failed",
        "severity": "P1",
        "fix_class": "runtime_coherence",
        "recommended_action": "Restore one assembled runtime root and rerun the gateway/agent contract probe.",
    },
    "runtime_coherence_errors": {
        "title": "Runtime coherence failure in live gateway logs",
        "severity": "P1",
        "fix_class": "runtime_coherence",
        "recommended_action": "Restore one assembled runtime root, restart once, and prove a private agent turn.",
    },
    "runtime_release_identity": {
        "title": "Runtime release identity is incomplete",
        "severity": "P1",
        "fix_class": "runtime_contract",
        "recommended_action": (
            "Restore the target-bound runtime composition receipt and activate through Operator Control."
        ),
    },
}


def enrich_check(check):
    if not isinstance(check, dict):
        return check
    meta = CHECK_METADATA.get(check.get("name"), {})
    out = dict(check)
    for key, value in meta.items():
        out.setdefault(key, value)
    return out


def check_disk():
    try:
        helper = Path(__file__).with_name("hermes-host-steward.py")
        if not helper.is_file():
            owner = HERMES.parent.parent if HERMES.parent.name == "profiles" else HERMES
            helper = owner / "bin/hermes-host-steward.py"
        policy = runpy.run_path(str(helper))["storage_thresholds"](HERMES)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return {"name": "disk", "status": "fail",
                "detail": f"storage policy unavailable: {type(exc).__name__}: {str(exc)[:100]}"}
    try:
        usage = shutil.disk_usage(HERMES)
        if usage.total <= 0:
            raise ValueError("disk total is zero")
        pct = int(usage.used * 100 / usage.total)
        free_bytes = int(usage.free)
    except (OSError, ValueError) as exc:
        return {
            "name": "disk",
            "status": "warn",
            "detail": f"{type(exc).__name__}: {str(exc)[:100]}",
        }
    if (
        pct >= 95
        or free_bytes < policy["HERMES_DISK_FAIL_FREE_BYTES"]
        or (pct >= policy["HERMES_DISK_FAIL_PERCENT"] and free_bytes < policy["HERMES_DISK_WARN_FREE_BYTES"])
    ):
        status = "fail"
    elif (pct >= policy["HERMES_DISK_WARN_PERCENT"] and free_bytes < 2 * policy["HERMES_DISK_WARN_FREE_BYTES"]) or free_bytes < policy["HERMES_DISK_WARN_FREE_BYTES"]:
        status = "warn"
    else:
        status = "pass"
    return {
        "name": "disk",
        "status": status,
        "detail": f"used={pct}% free_bytes={free_bytes}",
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": free_bytes,
        "used_pct": pct,
        "warn_percent": policy["HERMES_DISK_WARN_PERCENT"],
        "fail_percent": policy["HERMES_DISK_FAIL_PERCENT"],
        "warn_free_bytes": policy["HERMES_DISK_WARN_FREE_BYTES"],
        "fail_free_bytes": policy["HERMES_DISK_FAIL_FREE_BYTES"],
    }


def _gateway_resource_metrics(pid):
    metrics: dict = {
        "open_descriptors": None,
        "state_db_handles": None,
        "max_state_db_handles": None,
        "state_db_count": None,
        "deleted_state_db_handles": None,
        "deleted_state_db_targets": [],
        "descriptor_inspection_error": None,
        "rss_bytes": None,
    }
    proc_fd = PROC_ROOT / str(pid) / "fd"
    if sys.platform.startswith("linux"):
        try:
            descriptors = list(proc_fd.iterdir())
            metrics["open_descriptors"] = len(descriptors)
            state_handles = 0
            state_counts = {}
            deleted_targets = []
            for fd in descriptors:
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    # A descriptor may close between the listing and readlink.
                    continue
                except OSError as exc:
                    metrics["descriptor_inspection_error"] = type(exc).__name__
                    continue
                if re.search(r"/state\.db(?:-(?:wal|shm))?(?: \(deleted\))?$", target):
                    state_handles += 1
                    database = re.sub(r"-(?:wal|shm)$", "", target.removesuffix(" (deleted)"))
                    state_counts[database] = state_counts.get(database, 0) + 1
                    if target.endswith(" (deleted)"):
                        deleted_targets.append(target)
            metrics["state_db_handles"] = state_handles
            metrics["max_state_db_handles"] = max(state_counts.values(), default=0)
            metrics["state_db_count"] = len(state_counts)
            metrics["deleted_state_db_handles"] = len(deleted_targets)
            metrics["deleted_state_db_targets"] = sorted(deleted_targets)
        except OSError as exc:
            metrics["descriptor_inspection_error"] = type(exc).__name__
    elif sys.platform == "darwin":
        p = run(["lsof", "-n", "-P", "-p", str(pid)], timeout=8)
        if p.returncode == 0:
            lines = []
            for line in p.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) > 3 and re.fullmatch(r"\d+[A-Za-z]*", parts[3]):
                    lines.append(parts)
            metrics["open_descriptors"] = len(lines)
            state_counts = {}
            for parts in lines:
                if re.search(r"/state\.db(?:-(?:wal|shm))?$", parts[-1]):
                    database = re.sub(r"-(?:wal|shm)$", "", parts[-1])
                    state_counts[database] = state_counts.get(database, 0) + 1
            metrics["state_db_handles"] = sum(state_counts.values())
            metrics["max_state_db_handles"] = max(state_counts.values(), default=0)
            metrics["state_db_count"] = len(state_counts)
    elif os.name == "nt":
        p = run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f'$p=Get-Process -Id {pid}; "$($p.HandleCount) $($p.WorkingSet64)"',
            ],
            timeout=8,
        )
        if p.returncode == 0:
            try:
                handles, rss = p.stdout.strip().split()[-2:]
                metrics["open_descriptors"] = int(handles)
                metrics["rss_bytes"] = int(rss)
            except (TypeError, ValueError):
                pass
    if metrics["rss_bytes"] is None and os.name != "nt":
        p = run(["ps", "-o", "rss=", "-p", str(pid)], timeout=5)
        if p.returncode == 0:
            try:
                metrics["rss_bytes"] = int(p.stdout.strip().split()[-1]) * 1024
            except (IndexError, ValueError):
                pass
    return metrics


def check_gateway_resource_pressure():
    binding = gateway_runtime_binding()
    pid = binding.get("pid")
    if not pid or not binding.get("process_alive"):
        return {
            "name": "gateway_resource_pressure",
            "status": "warn",
            "detail": "gateway pid unavailable",
        }
    metrics = _gateway_resource_metrics(pid)
    prior = load_json(HERMES / "state/local-selfcheck-latest.json", {})
    prior_check = next(
        (
            row
            for row in prior.get("checks", [])
            if isinstance(row, dict) and row.get("name") == "gateway_resource_pressure"
        ),
        {},
    )
    fd = metrics.get("open_descriptors")
    state_handles = metrics.get("state_db_handles")
    per_database_handles = metrics.get("max_state_db_handles")
    if per_database_handles is None:
        per_database_handles = state_handles
    deleted_state_handles = metrics.get("deleted_state_db_handles")
    rss = metrics.get("rss_bytes")
    same_process = prior_check.get("gateway_pid") == pid
    fd_growth = (
        (fd - prior_check.get("open_descriptors"))
        if (same_process and isinstance(fd, int) and isinstance(prior_check.get("open_descriptors"), int))
        else None
    )
    state_growth = (
        (state_handles - prior_check.get("state_db_handles"))
        if (same_process and isinstance(state_handles, int) and isinstance(prior_check.get("state_db_handles"), int))
        else None
    )
    fd_pressure = fd
    database_count = metrics.get("state_db_count")
    if (isinstance(fd, int) and isinstance(database_count, int) and database_count > 1
            and isinstance(state_handles, int) and isinstance(per_database_handles, int)
            and per_database_handles < GATEWAY_STATE_DB_HANDLE_WARN):
        fd_pressure = fd - state_handles + per_database_handles
    fail = (
        (isinstance(fd, int) and fd >= GATEWAY_FD_FAIL)
        or (isinstance(per_database_handles, int) and per_database_handles >= GATEWAY_STATE_DB_HANDLE_FAIL)
        or (isinstance(deleted_state_handles, int) and deleted_state_handles > 0)
        or (isinstance(rss, int) and rss >= GATEWAY_RSS_FAIL_BYTES)
    )
    warn = (
        (isinstance(fd_pressure, int) and fd_pressure >= GATEWAY_FD_WARN)
        or (isinstance(per_database_handles, int) and per_database_handles >= GATEWAY_STATE_DB_HANDLE_WARN)
        or (isinstance(rss, int) and rss >= GATEWAY_RSS_WARN_BYTES)
        or (isinstance(fd_growth, int) and fd_growth >= 32)
        or (isinstance(state_growth, int) and state_growth >= 8)
        or bool(metrics.get("descriptor_inspection_error"))
        or all(metrics.get(key) is None for key in ("open_descriptors", "state_db_handles", "rss_bytes"))
    )
    return {
        "name": "gateway_resource_pressure",
        "status": "fail" if fail else ("warn" if warn else "pass"),
        "severity": "P1" if deleted_state_handles else "P2",
        "detail": (
            f"pid={pid} open_descriptors={fd} descriptor_pressure={fd_pressure} state_db_handles={state_handles} "
            f"max_state_db_handles={per_database_handles} state_db_count={metrics.get('state_db_count')} "
            f"deleted_state_db_handles={deleted_state_handles} "
            f"rss_bytes={rss} fd_growth={fd_growth} state_db_growth={state_growth}"
        ),
        "gateway_pid": pid,
        "descriptor_pressure": fd_pressure,
        **metrics,
        "fd_growth": fd_growth,
        "state_db_growth": state_growth,
    }


def _macos_process_memory():
    """Read the OS footprint ledger, which includes compressed process memory.

    RSS alone loses the largest consumers precisely when macOS swaps them out.
    Only executable names and numeric identities leave this collector, never argv.
    The structure is rusage_info_v2 from the macOS SDK's sys/resource.h.
    """
    import ctypes

    class RUsageV2(ctypes.Structure):
        _fields_ = [("uuid", ctypes.c_uint8 * 16), ("values", ctypes.c_uint64 * 18)]

    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        lib.proc_pid_rusage.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
    except (OSError, AttributeError):
        return lambda _pid: {}

    def read(pid):
        usage = RUsageV2()
        if lib.proc_pid_rusage(pid, 2, ctypes.byref(usage)) != 0:
            return {}
        result = {"footprint_bytes": int(usage.values[7]),
                  "start_abstime": int(usage.values[8])}
        path = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(pid, path, len(path)) > 0:
            executable = Path(os.fsdecode(path.value))
            result["command"] = executable.name[:80]
            result["family"] = next(
                (part[:-4][:80] for part in executable.parts if part.endswith(".app")),
                executable.name[:80],
            )
        return result

    return read


def _host_capacity_metrics():
    if os.name == "nt":
        script = r"""
$ErrorActionPreference='Stop'
$os=Get-CimInstance Win32_OperatingSystem
$procs=@(Get-Process -ErrorAction Stop)
$pageFiles=@(Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue)
$memoryPerf=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -ErrorAction SilentlyContinue
$now=Get-Date
$counts=@{}
foreach($name in @('powershell','pwsh','cmd','conhost','sshd')) {
  $counts[$name]=@($procs | Where-Object { $_.ProcessName -ieq $name }).Count
}
$maxHandles=0
$maxHandlePid=$null
foreach($proc in $procs) {
  try {
    if([int64]$proc.HandleCount -gt $maxHandles) {
      $maxHandles=[int64]$proc.HandleCount
      $maxHandlePid=[int]$proc.Id
    }
} catch {}
}
$handleObservations=@($procs | Where-Object { $_.ProcessName -in @('explorer','Telegram','Telegram Desktop','brave','chrome','msedge','cua-driver','cua-driver.exe','python','pythonw','node') } | Sort-Object @{Expression={ if ($_.ProcessName -in @('explorer','Telegram','Telegram Desktop','cua-driver','cua-driver.exe')) { 0 } else { 1 } }},@{Expression={$_.HandleCount};Descending=$true} | Select-Object -First 40 | ForEach-Object {
  $start=$null
  try { $start=$_.StartTime.ToUniversalTime().ToString('o') } catch {}
  [ordered]@{pid=[int]$_.Id; process_start=$start; process=[string]$_.ProcessName; handles=[int64]$_.HandleCount}
})
$cdpRoots=0
try {
  $cdpRoots=@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
    $_.Name -ieq 'brave.exe' -and $_.CommandLine -like '*--remote-debugging-port=9222*'
  }).Count
} catch {}
$topMemory=@($procs | Sort-Object WorkingSet64 -Descending | Select-Object -First 5 | ForEach-Object {
  $age=$null
  try { $age=[int]($now-$_.StartTime).TotalSeconds } catch {}
  [ordered]@{pid=[int]$_.Id; rss_bytes=[int64]$_.WorkingSet64; age_seconds=$age; command=[string]$_.ProcessName}
})
$oldest=@($procs | ForEach-Object {
  try {
    [ordered]@{
      pid=[int]$_.Id
      rss_bytes=[int64]$_.WorkingSet64
      age_seconds=[int]($now-$_.StartTime).TotalSeconds
      command=[string]$_.ProcessName
    }
  } catch {}
} | Sort-Object age_seconds -Descending | Select-Object -First 5)
[ordered]@{
  process_count=$procs.Count
  powershell_count=([int]$counts['powershell']+[int]$counts['pwsh'])
  cmd_count=[int]$counts['cmd']
  conhost_count=[int]$counts['conhost']
  ssh_session_count=[int]$counts['sshd']
  cdp_browser_root_count=[int]$cdpRoots
  max_process_handles=$maxHandles
  max_handle_pid=$maxHandlePid
  handle_observations=$handleObservations
  physical_total_bytes=([int64]$os.TotalVisibleMemorySize*1024)
  physical_free_bytes=([int64]$os.FreePhysicalMemory*1024)
  virtual_total_bytes=([int64]$os.TotalVirtualMemorySize*1024)
  virtual_free_bytes=([int64]$os.FreeVirtualMemory*1024)
  swap_total_bytes=([int64](($pageFiles | Measure-Object -Property AllocatedBaseSize -Sum).Sum)*1MB)
  swap_used_bytes=([int64](($pageFiles | Measure-Object -Property CurrentUsage -Sum).Sum)*1MB)
  swap_in_bytes_per_minute=([int64]$memoryPerf.PagesInputPerSec*4096*60)
  swap_out_bytes_per_minute=([int64]$memoryPerf.PagesOutputPerSec*4096*60)
  top_memory_processes=$topMemory
  oldest_processes=$oldest
} | ConvertTo-Json -Compress -Depth 4
"""
        proc = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=12,
        )
        if proc.returncode != 0:
            return {"error": (proc.stderr or proc.stdout or "capacity probe failed")[:240]}
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as exc:
            return {"error": f"capacity JSON invalid: {type(exc).__name__}"}

    metrics = {
        "process_count": None,
        "powershell_count": 0,
        "cmd_count": 0,
        "conhost_count": 0,
        "ssh_session_count": 0,
        "cdp_browser_root_count": 0,
        "max_process_handles": None,
        "max_handle_pid": None,
        "handle_observations": [],
        "physical_total_bytes": None,
        "physical_free_bytes": None,
        "virtual_total_bytes": None,
        "virtual_free_bytes": None,
        "swap_total_bytes": None,
        "swap_used_bytes": None,
        "swap_in_pages_total": None,
        "swap_out_pages_total": None,
        "swap_page_size_bytes": None,
        "top_memory_processes": [],
        "oldest_processes": [],
        "absolute_process_limit_enabled": True,
    }
    # ``comm`` is not consistently argument-free on every BSD/macOS build.
    # ``ucomm`` asks ps for the executable name, and the token clamp below is
    # defense in depth: health receipts must never persist credential-bearing
    # argv strings from processes such as ``tool --api-key=...``.
    proc = run(
        ["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,etime=,rss=,ucomm="],
        timeout=8,
    )
    if proc.returncode == 0:
        rows = []
        memory_reader = _macos_process_memory() if sys.platform == "darwin" else lambda _pid: {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 4)
            if len(parts) != 5:
                continue
            try:
                pid, ppid, elapsed, rss_kb = (
                    int(parts[0]),
                    int(parts[1]),
                    _elapsed_seconds(parts[2]),
                    int(parts[3]),
                )
            except ValueError:
                continue
            if elapsed is None:
                continue
            raw_command = parts[4].strip()
            command_token = raw_command.split(None, 1)[0] if raw_command else "unknown"
            rows.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "age_seconds": elapsed,
                    "rss_bytes": rss_kb * 1024,
                    "command": Path(command_token).name[:80],
                    **memory_reader(pid),
                }
            )
        names = [str(row["command"]).lower() for row in rows]
        metrics["process_count"] = len(names)
        metrics["powershell_count"] = sum(name in {"powershell", "pwsh"} for name in names)
        metrics["cmd_count"] = sum(name in {"cmd", "cmd.exe"} for name in names)
        metrics["conhost_count"] = sum(name in {"conhost", "conhost.exe"} for name in names)
        metrics["ssh_session_count"] = sum(name.startswith("sshd") for name in names)
        public_keys = ("pid", "ppid", "age_seconds", "rss_bytes", "footprint_bytes", "start_abstime", "command")
        metrics["top_memory_processes"] = [
            {key: row[key] for key in public_keys if key in row}
            for row in sorted(rows, key=lambda item: item.get("footprint_bytes", item["rss_bytes"]), reverse=True)[:5]
        ]
        metrics["oldest_processes"] = [
            {key: row[key] for key in public_keys if key in row}
            for row in sorted(rows, key=lambda item: item["age_seconds"], reverse=True)[:5]
        ]
        families = {}
        for row in rows:
            name = row.get("family", row["command"])
            group = families.setdefault(name, {"command": name, "process_count": 0,
                "rss_bytes": 0, "footprint_bytes": 0, "footprint_process_count": 0})
            group["process_count"] += 1
            group["rss_bytes"] += row["rss_bytes"]
            if "footprint_bytes" in row:
                group["footprint_bytes"] += row["footprint_bytes"]
                group["footprint_process_count"] += 1
        metrics["top_memory_families"] = sorted(families.values(), key=lambda group:
            group["footprint_bytes"] if group["footprint_process_count"] else group["rss_bytes"], reverse=True)[:5]
        metrics["memory_evidence_scope"] = "current_user_processes"
        metrics["footprint_process_count"] = sum("footprint_bytes" in row for row in rows)
    if sys.platform.startswith("linux"):
        try:
            meminfo = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, raw = line.partition(":")
                match = re.match(r"\s*(\d+)", raw)
                if match:
                    meminfo[key] = int(match.group(1)) * 1024
            metrics["physical_total_bytes"] = meminfo.get("MemTotal")
            metrics["physical_free_bytes"] = meminfo.get("MemAvailable") or meminfo.get("MemFree")
            # Linux CommitLimit/Committed_AS is an overcommit accounting view,
            # not available host capacity; Committed_AS can legitimately exceed
            # CommitLimit and would create a false 0%-free incident. Use the
            # reclaimable physical+swap pool as the cross-platform analogue.
            memory_total = meminfo.get("MemTotal")
            memory_free = meminfo.get("MemAvailable") or meminfo.get("MemFree")
            if memory_total is not None and memory_free is not None:
                metrics["virtual_total_bytes"] = memory_total + int(meminfo.get("SwapTotal") or 0)
                metrics["virtual_free_bytes"] = memory_free + int(meminfo.get("SwapFree") or 0)
            metrics["swap_total_bytes"] = meminfo.get("SwapTotal")
            swap_free = meminfo.get("SwapFree")
            if isinstance(metrics["swap_total_bytes"], int) and isinstance(swap_free, int):
                metrics["swap_used_bytes"] = max(0, metrics["swap_total_bytes"] - swap_free)
            vmstat = {}
            for line in Path("/proc/vmstat").read_text().splitlines():
                key, _, raw = line.partition(" ")
                if key in {"pswpin", "pswpout"}:
                    vmstat[key] = int(raw.strip())
            metrics["swap_in_pages_total"] = vmstat.get("pswpin")
            metrics["swap_out_pages_total"] = vmstat.get("pswpout")
            metrics["swap_page_size_bytes"] = int(os.sysconf("SC_PAGE_SIZE"))
        except Exception:
            pass
    elif sys.platform == "darwin":
        # A full macOS GUI login legitimately owns hundreds of per-user XPC and
        # app-helper processes. Use the OS pressure oracle and process growth;
        # an absolute user-process threshold alone is not a capacity signal.
        metrics["absolute_process_limit_enabled"] = False
        total = run(["sysctl", "-n", "hw.memsize"], timeout=5)
        pressure = run(["memory_pressure", "-Q"], timeout=8)
        if total.returncode == 0 and pressure.returncode == 0:
            total_match = re.search(r"(\d+)", total.stdout or "")
            free_match = re.search(r"System-wide memory free percentage:\s*(\d+)%", pressure.stdout or "")
            if total_match and free_match:
                total_bytes = int(total_match.group(1))
                free_bytes = round(total_bytes * int(free_match.group(1)) / 100)
                metrics["physical_total_bytes"] = total_bytes
                metrics["physical_free_bytes"] = free_bytes
                metrics["virtual_total_bytes"] = total_bytes
                metrics["virtual_free_bytes"] = free_bytes
        swap = run(["sysctl", "-n", "vm.swapusage"], timeout=5)
        if swap.returncode == 0:
            total_match = re.search(r"total\s*=\s*([0-9.]+)([KMGTP])", swap.stdout or "", re.I)
            used_match = re.search(r"used\s*=\s*([0-9.]+)([KMGTP])", swap.stdout or "", re.I)
            if total_match:
                metrics["swap_total_bytes"] = _scaled_bytes(*total_match.groups())
            if used_match:
                metrics["swap_used_bytes"] = _scaled_bytes(*used_match.groups())
        vmstat = run(["vm_stat"], timeout=8)
        if vmstat.returncode == 0:
            page_size_match = re.search(r"page size of\s+(\d+) bytes", vmstat.stdout or "", re.I)
            swap_in_match = re.search(r"(?:swapins|pages swapped in):\s*(\d+)", vmstat.stdout or "", re.I)
            swap_out_match = re.search(r"(?:swapouts|pages swapped out):\s*(\d+)", vmstat.stdout or "", re.I)
            if page_size_match:
                metrics["swap_page_size_bytes"] = int(page_size_match.group(1))
            if swap_in_match:
                metrics["swap_in_pages_total"] = int(swap_in_match.group(1))
            if swap_out_match:
                metrics["swap_out_pages_total"] = int(swap_out_match.group(1))
    return metrics


def check_host_capacity():
    metrics = _host_capacity_metrics()
    if metrics.get("error"):
        return {"name": "host_capacity", "status": "warn", "detail": metrics["error"]}
    virtual_total = metrics.get("virtual_total_bytes")
    virtual_free = metrics.get("virtual_free_bytes")
    virtual_free_pct = (
        round(virtual_free / virtual_total * 100, 1)
        if isinstance(virtual_total, int) and virtual_total > 0 and isinstance(virtual_free, int)
        else None
    )
    prior = load_json(HERMES / "state/local-selfcheck-latest.json", {})
    prior_check = next(
        (
            row for row in prior.get("checks", [])
            if isinstance(row, dict) and row.get("name") == "host_capacity"
        ),
        {},
    )
    checked_at = parse_dt(prior.get("checked_at"))
    observation_window_seconds = (
        (datetime.now(timezone.utc) - checked_at).total_seconds()
        if checked_at
        else None
    )
    page_size = metrics.get("swap_page_size_bytes")
    swap_in_bytes_per_minute = metrics.get("swap_in_bytes_per_minute")
    if not isinstance(swap_in_bytes_per_minute, (int, float)):
        swap_in_bytes_per_minute = _counter_rate_bytes_per_minute(
            metrics.get("swap_in_pages_total"),
            prior_check.get("swap_in_pages_total"),
            page_size,
            observation_window_seconds,
        )
    swap_out_bytes_per_minute = metrics.get("swap_out_bytes_per_minute")
    if not isinstance(swap_out_bytes_per_minute, (int, float)):
        swap_out_bytes_per_minute = _counter_rate_bytes_per_minute(
            metrics.get("swap_out_pages_total"),
            prior_check.get("swap_out_pages_total"),
            page_size,
            observation_window_seconds,
        )
    swap_total = metrics.get("swap_total_bytes")
    swap_used = metrics.get("swap_used_bytes")
    swap_used_pct = (
        round(swap_used / swap_total * 100, 1)
        if isinstance(swap_total, int) and swap_total > 0 and isinstance(swap_used, int)
        else None
    )
    process_count = metrics.get("process_count")
    absolute_process_limit_enabled = metrics.get("absolute_process_limit_enabled", True) is not False
    process_growth = (
        process_count - prior_check.get("process_count")
        if isinstance(process_count, int) and isinstance(prior_check.get("process_count"), int)
        else None
    )
    powershell_count = int(metrics.get("powershell_count") or 0)
    powershell_growth = (
        powershell_count - prior_check.get("powershell_count")
        if isinstance(prior_check.get("powershell_count"), int)
        else None
    )
    # Keep this history in the existing self-check receipt. A process restart,
    # missing identity, or a stale observation must start a new baseline.
    handle_growth = []
    current_handles = metrics.get("handle_observations") or []
    previous_handles = prior_check.get("handle_observations") or []
    current_handles = current_handles[:WINDOWS_HANDLE_OBSERVATION_LIMIT] if isinstance(current_handles, list) else []
    previous_handles = previous_handles[:WINDOWS_HANDLE_OBSERVATION_LIMIT] if isinstance(previous_handles, list) else []
    metrics["handle_observations"] = current_handles
    previous_by_identity = {
        (row.get("pid"), row.get("process_start")): row
        for row in previous_handles if isinstance(row, dict)
    }
    handle_window_valid = (
        isinstance(observation_window_seconds, (int, float))
        and WINDOWS_HANDLE_MIN_WINDOW_SECONDS <= observation_window_seconds <= WINDOWS_HANDLE_MAX_WINDOW_SECONDS
    )
    for row in current_handles:
        if not isinstance(row, dict):
            continue
        previous = previous_by_identity.get((row.get("pid"), row.get("process_start")), {})
        delta, streak = None, 0
        if (handle_window_valid and row.get("process_start")
                and isinstance(row.get("handles"), int)
                and isinstance(previous.get("handles"), int)):
            delta = row["handles"] - previous["handles"]
            if delta >= WINDOWS_HANDLE_GROWTH_WARN:
                # Two consecutive intervals prove sustained growth; keep the
                # stored counter bounded even during long-running growth.
                streak = 2 if previous.get("growth_observations") in (1, 2) else 1
        row["growth_observations"] = streak
        handle_growth.append({"pid": row.get("pid"), "process_start": row.get("process_start"),
                              "process": row.get("process"), "delta": delta, "growth_observations": streak})
    sustained_handle_growth = [row for row in handle_growth if row["growth_observations"] == 2]
    high_handle_processes = [row for row in current_handles if isinstance(row, dict)
                             and isinstance(row.get("handles"), int)
                             and row["handles"] >= WINDOWS_HANDLE_ABSOLUTE_WARN]
    swap_active_fail = (
        isinstance(swap_out_bytes_per_minute, (int, float))
        and (
            swap_out_bytes_per_minute >= HOST_SWAP_ACTIVE_FAIL_BYTES_PER_MINUTE
            or (
                swap_out_bytes_per_minute >= HOST_SWAP_ACTIVE_WARN_BYTES_PER_MINUTE
                and virtual_free_pct is not None
                and virtual_free_pct < HOST_VIRTUAL_FREE_FAIL_PERCENT
            )
        )
    )
    swap_active_warn = (
        isinstance(swap_out_bytes_per_minute, (int, float))
        and swap_out_bytes_per_minute >= HOST_SWAP_ACTIVE_WARN_BYTES_PER_MINUTE
    )
    swap_allocation_high = (
        swap_used_pct is not None and swap_used_pct >= HOST_SWAP_ALLOCATED_WARN_PERCENT
    )
    # macOS retains elastic swap after pressure subsides; allocation alone is not current pressure.
    swap_allocated_warn = swap_allocation_high and (
        sys.platform != "darwin" or virtual_free_pct is None
        or virtual_free_pct < HOST_VIRTUAL_FREE_WARN_PERCENT
    )
    prior_swap_used_pct = prior_check.get("swap_used_pct")
    if swap_allocation_high:
        prior_allocation_observations = prior_check.get("swap_allocation_observations")
        swap_allocation_observations = (
            min(
                2,
                max(
                    1,
                    prior_allocation_observations
                    if isinstance(prior_allocation_observations, int)
                    else 1,
                )
                + 1,
            )
            if isinstance(prior_swap_used_pct, (int, float))
            and prior_swap_used_pct >= HOST_SWAP_ALLOCATED_WARN_PERCENT
            else 1
        )
    else:
        swap_allocation_observations = 0
    fail = (
        (virtual_free_pct is not None and virtual_free_pct < HOST_VIRTUAL_FREE_FAIL_PERCENT)
        or (
            absolute_process_limit_enabled
            and isinstance(process_count, int)
            and process_count > HOST_PROCESS_FAIL
        )
        or int(metrics.get("max_process_handles") or 0) > HOST_PROCESS_HANDLE_FAIL
        or int(metrics.get("cdp_browser_root_count") or 0) > 2
        or swap_active_fail
    )
    warn = (
        (virtual_free_pct is not None and virtual_free_pct < HOST_VIRTUAL_FREE_WARN_PERCENT)
        or (
            absolute_process_limit_enabled
            and isinstance(process_count, int)
            and process_count > HOST_PROCESS_WARN
        )
        or powershell_count > HOST_POWERSHELL_WARN
        or (isinstance(process_growth, int) and process_growth >= 100)
        or (isinstance(powershell_growth, int) and powershell_growth >= 10)
        or swap_active_warn
        or swap_allocated_warn
        or bool(sustained_handle_growth)
        or bool(high_handle_processes)
    )
    status = "fail" if fail else ("warn" if warn else "pass")
    swap_pressure_status = (
        "fail"
        if swap_active_fail
        else ("warn" if swap_active_warn or swap_allocated_warn else "pass")
    )
    return {
        "name": "host_capacity",
        "status": status,
        "severity": "P1" if fail else "P2",
        "fix_class": "host_capacity",
        "detail": (
            f"virtual_free_pct={virtual_free_pct} process_count={process_count} "
            f"powershell={powershell_count} cmd={metrics.get('cmd_count')} "
            f"conhost={metrics.get('conhost_count')} ssh_sessions={metrics.get('ssh_session_count')} "
            f"cdp_browser_roots={metrics.get('cdp_browser_root_count')} "
            f"max_handles={metrics.get('max_process_handles')} process_growth={process_growth} "
            f"sustained_handle_growth={len(sustained_handle_growth)} high_handle_processes={len(high_handle_processes)} "
            f"powershell_growth={powershell_growth} swap_used_pct={swap_used_pct} "
            f"swap_out_bytes_per_minute={swap_out_bytes_per_minute}"
        ),
        **metrics,
        "virtual_free_pct": virtual_free_pct,
        "process_growth": process_growth,
        "powershell_growth": powershell_growth,
        "handle_growth": handle_growth,
        "sustained_handle_growth": sustained_handle_growth,
        "high_handle_processes": high_handle_processes,
        "swap_used_pct": swap_used_pct,
        "swap_allocation_observations": swap_allocation_observations,
        "swap_in_bytes_per_minute": swap_in_bytes_per_minute,
        "swap_out_bytes_per_minute": swap_out_bytes_per_minute,
        "swap_activity_window_seconds": round(observation_window_seconds)
        if isinstance(observation_window_seconds, (int, float))
        else None,
        "swap_pressure_status": swap_pressure_status,
        "large_job_posture": "inspect" if status in {"warn", "fail"} else "normal",
    }


def check_host_steward():
    """Observe ownership without starting maintenance inside a health probe."""
    steward_home = HERMES
    steward = steward_home / "bin/hermes-host-steward.py"
    # Named profiles share the host owner's lease registry and steward.
    if not steward.is_file() and HERMES.parent.name == "profiles":
        steward_home = HERMES.parent.parent
        steward = steward_home / "bin/hermes-host-steward.py"
    if not steward.is_file():
        return {
            "name": "host_steward",
            "status": "warn",
            "detail": "task-owned host resource steward is not installed",
        }
    result = run(
        [
            sys.executable,
            str(steward),
            "--hermes-home",
            str(steward_home),
            "snapshot",
            "--retention-home",
            str(HERMES),
        ],
        timeout=90,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "name": "host_steward",
            "status": "fail",
            "detail": f"invalid host resource receipt rc={result.returncode}; error={str(getattr(result, 'stderr', '') or '').strip()[:160]}",
        }
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    intent_results = (
        payload.get("intent_results")
        if isinstance(payload.get("intent_results"), list)
        else []
    )
    failed_release = any(
        isinstance(row, dict) and row.get("outcome") in {"failed", "blocked"}
        for row in [*results, *intent_results]
    )
    expired = int(counts.get("expired") or 0)
    invalid = int(counts.get("invalid") or 0)
    invalid_intents = int(counts.get("invalid_intents") or 0)
    status = "pass"
    if (
        result.returncode != 0
        or payload.get("status") not in {"pass", "warn"}
        or invalid
        or invalid_intents
        or failed_release
    ):
        status = "fail"
    elif expired or payload.get("status") == "warn":
        status = "warn"
    return {
        "name": "host_steward",
        "status": status,
        "detail": (
            f"active={int(counts.get('active') or 0)} expired={expired} "
            f"invalid={invalid} invalid_intents={invalid_intents}"
        ),
    }


def atomic_write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_machine_profile(checks):
    """Build the bounded host contract agents and the control plane may consume."""
    by_name = {
        row.get("name"): row
        for row in checks
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    disk = by_name.get("disk") or {}
    capacity = by_name.get("host_capacity") or {}
    storage = {
        key: disk.get(key)
        for key in (
            "total_bytes",
            "used_bytes",
            "free_bytes",
            "used_pct",
            "warn_percent",
            "fail_percent",
            "warn_free_bytes",
            "fail_free_bytes",
        )
    }
    memory = {
        key: capacity.get(key)
        for key in (
            "physical_total_bytes",
            "physical_free_bytes",
            "virtual_total_bytes",
            "virtual_free_bytes",
            "virtual_free_pct",
        )
    }
    pressure = {
        key: capacity.get(key)
        for key in (
            "swap_total_bytes",
            "swap_used_bytes",
            "swap_used_pct",
            "swap_allocation_observations",
            "swap_in_bytes_per_minute",
            "swap_out_bytes_per_minute",
            "swap_activity_window_seconds",
            "swap_pressure_status",
            "large_job_posture",
            "top_memory_processes",
            "top_memory_families",
            "memory_evidence_scope",
            "footprint_process_count",
            "oldest_processes",
        )
    }
    storage_observed = all(
        isinstance(storage.get(key), (int, float))
        for key in ("total_bytes", "free_bytes", "used_pct")
    )
    return {
        "schema_version": 2,
        "status": "ready" if storage_observed else "degraded",
        "source": "local_selfcheck",
        "platform": sys.platform,
        "hostname": (
            os.uname().nodename
            if hasattr(os, "uname")
            else os.environ.get("COMPUTERNAME", "unknown")
        ),
        "hermes_home": str(HERMES),
        "storage": storage,
        "memory": memory,
        "pressure": pressure,
        "limits": {
            "max_concurrent_large_jobs": MAX_CONCURRENT_LARGE_JOBS,
            "large_job_estimate_bytes": LARGE_JOB_ESTIMATE_BYTES,
            "disk_warn_new_payload_limit_bytes": DISK_WARN_NEW_PAYLOAD_LIMIT_BYTES,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default=os.environ.get("HERMES_AGENT_ID", "unknown"))
    ap.add_argument("--agent-name", default=os.environ.get("HERMES_AGENT_NAME", "unknown"))
    ap.add_argument(
        "--skip-canary-reconciler",
        action="store_true",
        help="Skip the recursive reconciler check when invoked by the reconciler itself.",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--scope", choices=("gateway", "host", "on-demand"), default="gateway",
                    help="Select the actual runtime lifecycle; host and on-demand checks do not qualify a Golden gateway.")
    args = ap.parse_args()
    checks = []
    check_functions = [
        check_config,
        check_client_context,
        check_gateway_state,
        check_runtime_release_identity,
        check_active_client_capabilities,
        check_local_brain,
        check_gateway_brain_binding,
        check_advertised_tool_env,
        check_telegram_transcript_hook,
        check_immersion_plugin,
        check_injected_shared_rules,
        check_immersion_quality,
        check_agent_probe,
        check_session_store_errors,
        check_logs,
        check_workspace_write,
        check_tool_readiness,
        check_image_quality_surface,
        check_document_visual_delivery,
        check_canary_reconciler,
        check_disk_retention,
        check_disk,
        check_gateway_resource_pressure,
        check_host_capacity,
        check_host_steward,
    ]
    if args.scope == "host":
        check_functions = [check_disk_retention, check_disk, check_host_capacity]
    elif args.scope == "on-demand":
        check_functions = [check_config, check_on_demand_runtime, check_workspace_write, check_disk, check_host_capacity]
    if args.skip_canary_reconciler and check_canary_reconciler in check_functions:
        check_functions.remove(check_canary_reconciler)
    for fn in check_functions:
        try:
            checks.append(fn())
        except Exception as e:
            checks.append(
                {
                    "name": fn.__name__.removeprefix("check_"),
                    "status": "fail",
                    "detail": f"{type(e).__name__}: {str(e)[:160]}",
                }
            )
    checks = [enrich_check(c) for c in checks]
    failures = [c for c in checks if c["status"] == "fail"]
    warnings = [c for c in checks if c["status"] == "warn"]
    payload = {
        "schema_version": 4,
        "agent_id": args.agent_id,
        "agent_name": args.agent_name,
        "hermes_home": str(HERMES),
        "host": (os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "unknown")),
        "checked_at": iso(),
        "scope": args.scope,
        "gateway_runtime": gateway_runtime_binding() if args.scope == "gateway" else {},
        "runtime_release": runtime_release_identity() if args.scope == "gateway" else {},
        "machine_profile": build_machine_profile(checks),
        "status": "fail" if failures else "pass",
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }
    state = HERMES / "state/local-selfcheck-latest.json"
    try:
        atomic_write_json(state, payload)
        (HERMES / "logs/local-selfcheck.log").open("a", encoding="utf-8").write(
            f"{payload['checked_at']} status={payload['status']} failures={len(failures)}\n"
        )
    except Exception:
        pass
    print(json.dumps(payload, indent=2 if args.json else None, sort_keys=True))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
