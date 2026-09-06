#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
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
DISK_WARN_PERCENT = int(os.environ.get("HERMES_DISK_WARN_PERCENT", "85"))
DISK_FAIL_PERCENT = int(os.environ.get("HERMES_DISK_FAIL_PERCENT", "92"))
DISK_WARN_FREE_BYTES = int(os.environ.get("HERMES_DISK_WARN_FREE_BYTES", str(15 * 1024**3)))
DISK_FAIL_FREE_BYTES = int(os.environ.get("HERMES_DISK_FAIL_FREE_BYTES", str(5 * 1024**3)))


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
HOST_SSH_SESSION_WARN = int(os.environ.get("HERMES_HOST_SSH_SESSION_WARN", "20"))
HOST_PROCESS_HANDLE_FAIL = int(os.environ.get("HERMES_HOST_PROCESS_HANDLE_FAIL", "50000"))
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


def check_client_context():
    p = HERMES / "CLIENT_CONTEXT.md"
    return {"name": "client_context", "status": "pass" if p.exists() else "warn", "detail": str(p)}


def telegram_transport_expected():
    """Return False only for an explicit ``platforms.telegram.enabled: false``."""
    try:
        import yaml

        config = yaml.safe_load((HERMES / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return True
    telegram = ((config.get("platforms") or {}).get("telegram") or {})
    return not (isinstance(telegram, dict) and telegram.get("enabled") is False)


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
            telegram_ok = (
                telegram_state == "connected"
                and poll_age is not None
                and 0 <= poll_age <= TELEGRAM_POLL_MAX_AGE_S
            )
            session_status = (d.get("session_store") or {}).get("status", "unknown")
            session_failed = session_status in {"unavailable", "retrying"}
            ok = (
                not session_failed
                and state == "running"
                and pid_alive
                and (telegram_ok or not telegram_expected)
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
                "transport_healthy": telegram_ok if telegram_expected else None,
                "session_store_healthy": False if session_failed else (True if session_status == "ok" else None),
                "agent_turn_healthy": False if session_failed else None,
                "session_store_status": session_status,
                "telegram_state": telegram_state or "unknown",
                "telegram_expected": telegram_expected,
                "last_successful_poll_at": telegram.get("last_successful_poll_at"),
                "poll_age_seconds": poll_age,
            }
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
    compression = _top_level_block(text, "compression")
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
    drift = []
    for key, expected in required_display.items():
        actual = _scalar_from_block(display, key)
        if actual != expected:
            drift.append(f"display.{key}={actual!r}->{expected!r}")
    required_compression = {
        "threshold": 0.85,
        "threshold_tokens": 240000,
        "progress_notices": False,
    }
    for key, expected in required_compression.items():
        actual = _scalar_from_block(compression, key)
        if actual != expected:
            drift.append(f"compression.{key}={actual!r}->{expected!r}")
    actual = _scalar_from_block(session_reset, "notify")
    if actual is not False:
        drift.append(f"session_reset.notify={actual!r}->False")
    actual = _scalar_from_block(telegram, "gateway_restart_notification", indent=4)
    if actual is not False:
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


def _process_started_at(pid: int):
    """Return a cross-platform UTC process start time when it can be proved."""
    if os.name == "nt":
        proc = run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Process -Id {pid}).StartTime.ToUniversalTime().ToString('o')",
            ],
            timeout=8,
        )
        return parse_dt(proc.stdout.strip()) if proc.returncode == 0 else None
    proc = run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=5)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        local = datetime.strptime(" ".join(proc.stdout.split()), "%a %b %d %H:%M:%S %Y")
        return local.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc)
    except ValueError:
        return None


def _overlay_config_exemptions() -> set[str]:
    manifest = load_json(HERMES / "runtime-manifest.json", {})
    exemptions = manifest.get("overlay_config_exemptions")
    if not isinstance(exemptions, list):
        return set()
    return {str(value) for value in exemptions}


def check_telegram_organic_checkpoints():
    """Prove the effective v3 contract from the active immutable runtime."""
    config_path = HERMES / "config.yaml"
    binding_path = HERMES / "state/runtime-binding.json"
    failures = []
    if not config_path.exists():
        failures.append("config missing")
        text = ""
    else:
        text = config_path.read_text(encoding="utf-8", errors="replace")

    agent = _top_level_block(text, "agent")
    display = _top_level_block(text, "display")
    display_platforms = _nested_block(display, "platforms")
    telegram = _nested_block(display_platforms, "telegram", indent=4)
    interval = _scalar_from_block(agent, "gateway_notify_interval")
    global_enabled = _scalar_from_block(display, "long_running_notifications")
    telegram_enabled = _scalar_from_block(telegram, "long_running_notifications", indent=6)
    telegram_cleanup = _scalar_from_block(telegram, "cleanup_progress", indent=6)
    telegram_typing_progress = _scalar_from_block(
        telegram, "progress_on_typing", indent=6
    )
    exemptions = _overlay_config_exemptions()
    if "agent.gateway_notify_interval" not in exemptions and interval != 300:
        failures.append(f"interval={interval!r} expected=300")
    if "display.long_running_notifications" not in exemptions and global_enabled is not False:
        failures.append(f"global={global_enabled!r} expected=False")
    if "display.platforms.telegram.long_running_notifications" not in exemptions and telegram_enabled is not True:
        failures.append(f"telegram={telegram_enabled!r} expected=True")
    if "display.platforms.telegram.cleanup_progress" not in exemptions and telegram_cleanup is not True:
        failures.append(f"cleanup={telegram_cleanup!r} expected=True")
    if (
        "display.platforms.telegram.progress_on_typing" not in exemptions
        and telegram_typing_progress is not True
    ):
        failures.append(
            f"progress_on_typing={telegram_typing_progress!r} expected=True"
        )

    binding = load_json(binding_path, {})
    runtime_raw = str(binding.get("runtime_root") or "").strip()
    runtime_root = Path(runtime_raw).expanduser()
    marker_paths = (
        runtime_root / "gateway/run.py",
        runtime_root / "run_agent.py",
        runtime_root / "agent/codex_runtime.py",
    )
    marker = "HERMES_TELEGRAM_COMMENTARY_CAPTURE_v3"
    marker_hash = ""
    if (
        binding.get("kind") != "botdoctor_runtime_binding"
        or binding.get("status") != "active"
        or not runtime_raw
        or not runtime_root.is_dir()
    ):
        failures.append("active immutable runtime binding missing or invalid")
    elif any(not path.is_file() for path in marker_paths):
        failures.append("active runtime checkpoint source missing")
    else:
        sources = [path.read_bytes() for path in marker_paths]
        marker_hash = hashlib.sha256(b"\0".join(sources)).hexdigest()
        if any(marker.encode() not in source for source in sources):
            failures.append("active runtime v3 commentary-capture marker missing")
        if b'"progress_on_typing"' not in sources[0]:
            failures.append("active runtime progress_on_typing implementation missing")

    live = gateway_runtime_binding()
    live_root = Path(str(live.get("runtime_root") or "")).expanduser()
    if runtime_raw and live_root != runtime_root:
        failures.append(f"live runtime mismatch live={live_root} bound={runtime_root}")
    activated_at = parse_dt(binding.get("generated_at"))
    process_started = _process_started_at(live["pid"]) if live.get("pid") else None
    if activated_at is None:
        failures.append("binding activation timestamp missing")
    if process_started is None:
        failures.append("process generation unavailable")
    elif activated_at is not None and process_started < activated_at:
        failures.append("gateway process predates runtime activation")

    detail = (
        f"interval={interval!r} global={global_enabled!r} telegram={telegram_enabled!r} "
        f"cleanup={telegram_cleanup!r} progress_on_typing={telegram_typing_progress!r} "
        f"exemptions={','.join(sorted(exemptions)) or 'none'} "
        f"runtime={runtime_root} marker_sha256={marker_hash or 'missing'} "
        f"process_started={process_started.isoformat() if process_started else 'unknown'}"
    )
    return {
        "name": "telegram_organic_checkpoints",
        "status": "fail" if failures else "pass",
        "severity": "P1",
        "fix_class": "restart_or_redeploy",
        "detail": "; ".join(failures) + ("; " if failures else "") + detail,
    }


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


def check_tool_readiness():
    p = HERMES / "state/tool-readiness-probe-latest.json"
    if not p.exists():
        return {"name": "tool_readiness", "status": "skip", "detail": "no tool-readiness state"}
    d = load_json(p, {})
    ts = parse_dt(d.get("timestamp") or d.get("generated_at") or d.get("checked_at"))
    age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else 10**9
    email_choice = email_auth_client_choice()
    core = {"api_key_validity", "firecrawl", "browser", "email", "mcp_servers"}
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
    runtime = HERMES / "hermes-agent"
    python = runtime / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
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


def check_cron_toolset_preflight():
    """Fail before rollout when an enabled cron resolves an unknown toolset."""
    preflight = HERMES / "bin/hermes-cron-toolset-preflight.py"
    if not preflight.is_file():
        return {
            "name": "cron_toolset_preflight",
            "status": "fail",
            "severity": "P1",
            "fix_class": "restart_or_redeploy",
            "detail": f"cron toolset preflight missing: {preflight}",
        }
    probe = run([sys.executable, str(preflight), "--hermes-home", str(HERMES), "--json"], timeout=100)
    try:
        payload = json.loads(probe.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    passed = probe.returncode == 0 and payload.get("status") == "pass"
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    detail = (
        f"enabled_inference_jobs={payload.get('enabled_inference_jobs', 0)} "
        f"jobs_with_unknown_toolsets={payload.get('jobs_with_unknown_toolsets', 0)}"
    )
    if payload.get("error"):
        detail += f" error={str(payload['error'])[:240]}"
    elif findings:
        detail += " findings=" + ",".join(
            f"{item.get('job_id')}:{'|'.join(item.get('unknown_toolsets') or [])}"
            for item in findings[:8]
        )
    return {
        "name": "cron_toolset_preflight",
        "status": "pass" if passed else "fail",
        "severity": "P1",
        "fix_class": "runtime_contract",
        "detail": detail,
        "enabled_inference_jobs": payload.get("enabled_inference_jobs", 0),
        "jobs_with_unknown_toolsets": payload.get("jobs_with_unknown_toolsets", 0),
    }


def check_canary_reconciler():
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


def _read_env_keys():
    keys = {}
    for env_path in (HERMES / ".env", HERMES / ".env.secrets"):
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
    if (
        bound_home != expected_home
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


def _gateway_env_receipt_present_keys(config_bytes):
    """Return names proven present by one exact live-gateway receipt check."""
    pid_path = HERMES / "gateway.pid"
    receipt_path = HERMES / "state/gateway-capability-env-presence.json"
    if (
        not pid_path.is_file()
        or pid_path.is_symlink()
        or not receipt_path.is_file()
        or receipt_path.is_symlink()
    ):
        return frozenset()
    pid_record = load_json(pid_path, {})
    receipt = load_json(receipt_path, {})
    if not isinstance(pid_record, dict) or not isinstance(receipt, dict):
        return frozenset()
    if pid_record.get("kind") != "hermes-gateway":
        return frozenset()
    if receipt.get("schema") != "botdoctor.gateway-capability-env-presence.v1":
        return frozenset()
    try:
        pid = int(pid_record.get("pid") or 0)
        start_time = int(pid_record.get("start_time"))
        receipt_pid = int(receipt.get("pid") or 0)
        receipt_start_time = int(receipt.get("start_time"))
        expected_home = HERMES.resolve(strict=True)
        receipt_home = Path(str(receipt.get("hermes_home") or "")).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return frozenset()
    if (
        pid <= 0
        or receipt_pid != pid
        or receipt_start_time != start_time
        or receipt_home != expected_home
        or not pid_is_alive(pid)
        or _live_process_start_time(pid) != start_time
    ):
        return frozenset()
    if receipt.get("config_sha256") != hashlib.sha256(config_bytes).hexdigest():
        return frozenset()
    observed_keys = receipt.get("observed_keys")
    present_keys = receipt.get("present_keys")
    if not isinstance(observed_keys, list) or not isinstance(present_keys, list):
        return frozenset()
    if not all(isinstance(item, str) for item in observed_keys + present_keys):
        return frozenset()
    observed = frozenset(observed_keys)
    present = frozenset(present_keys)
    if len(observed) != len(observed_keys) or len(present) != len(present_keys):
        return frozenset()
    if not present.issubset(observed):
        return frozenset()
    return present


def _sqlite(path: Path, query: str, params=()):
    import sqlite3

    # A short-lived read-only connection can still join a live WAL database and
    # remove its shared-memory sidecars when the process exits. Immutable mode
    # reads the checkpointed database file without touching live WAL state.
    con = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=3
    )
    try:
        con.execute("PRAGMA query_only = ON")
        return con.execute(query, params).fetchall()
    finally:
        con.close()


def check_local_brain():
    candidates = [HOME / ".gbrain", HOME / "Documents" / "Brain" / "Principal Brain", HERMES / ".gbrain"]
    present = [str(path) for path in candidates if path.exists()]
    status = "pass" if present else "warn"
    detail = "present=" + ",".join(present[:3]) if present else "no local gbrain/principal brain path found"
    return {"name": "local_brain", "status": status, "detail": detail}


def check_topic_session_bindings():
    db = HERMES / "state.db"
    if not db.exists():
        return {
            "name": "topic_session_bindings",
            "status": "warn",
            "detail": f"missing state db: {db}",
        }
    require = os.environ.get("HERMES_REQUIRE_TOPIC_BINDINGS", "").lower() in {"1", "true", "yes"}
    try:
        tables = {row[0] for row in _sqlite(db, "select name from sqlite_master where type='table'")}
        if "telegram_dm_topic_bindings" in tables:
            count = _sqlite(db, "select count(*) from telegram_dm_topic_bindings")[0][0]
            enabled_count = 0
            if "telegram_dm_topic_mode" in tables:
                try:
                    enabled_count = _sqlite(db, "select count(*) from telegram_dm_topic_mode where enabled = 1")[0][0]
                except Exception:
                    enabled_count = 0
            status = "pass" if count or not require else "fail"
            return {
                "name": "topic_session_bindings",
                "status": status,
                "detail": (
                    f"backend=legacy bindings={count} enabled_topic_modes={enabled_count} "
                    f"required={require}"
                ),
            }
        if "sessions" in tables:
            columns = {row[1] for row in _sqlite(db, "pragma table_info(sessions)")}
            required_columns = {"source", "session_key", "thread_id"}
            if required_columns.issubset(columns):
                topic_count = _sqlite(
                    db,
                    "select count(*) from sessions "
                    "where source = 'telegram' and thread_id is not null",
                )[0][0]
                dm_count = _sqlite(
                    db,
                    "select count(*) from sessions "
                    "where source = 'telegram' and thread_id is null",
                )[0][0]
                status = "pass" if topic_count or not require else "fail"
                return {
                    "name": "topic_session_bindings",
                    "status": status,
                    "detail": (
                        f"backend=native_sessions topic_sessions={topic_count} "
                        f"non_topic_sessions={dm_count} required={require}"
                    ),
                }
        else:
            columns = set()
        missing_columns = sorted({"source", "session_key", "thread_id"} - columns)
        if "sessions" in tables and missing_columns:
            detail = "native sessions schema missing columns=" + ",".join(missing_columns)
        else:
            detail = "no supported topic/session binding schema"
        return {
            "name": "topic_session_bindings",
            "status": "fail" if require else "warn",
            "detail": detail + (" (required)" if require else " (topic mode unverified)"),
        }
    except Exception as e:
        return {
            "name": "topic_session_bindings",
            "status": "fail",
            "detail": f"{type(e).__name__}: {str(e)[:160]}",
        }


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
        _gateway_env_receipt_present_keys(text.encode("utf-8"))
        if unresolved
        else frozenset()
    )
    missing = [key for key in unresolved if key not in gateway_present_keys]
    required_missing = [key for key in missing if key not in OPTIONAL_CAPABILITY_ENV_KEYS]
    optional_missing = [key for key in missing if key in OPTIONAL_CAPABILITY_ENV_KEYS]
    status = "fail" if required_missing else ("warn" if optional_missing else "pass")
    return {
        "name": "advertised_tool_env",
        "status": status,
        "detail": (
            f"refs={len(capability_refs)} required_missing={required_missing[:12]} "
            f"optional_missing={optional_missing[:12]}"
        ),
    }


_CREDENTIAL_ERROR_RE = re.compile(
    r"(no connected account|not authenticated|authentication required|"
    r"(?:invalid|missing|expired|unauthorized|forbidden)[^\\n]{0,80}(?:api[ _-]?key|credential|token)|"
    r"(?:api[ _-]?key|credential|token)[^\\n]{0,80}(?:invalid|missing|expired|unauthorized|forbidden))",
    re.I,
)
_READ_ARTIFACT_TOOLS = {"read_file", "search_files", "session_search", "skill_view", "web_extract"}


def _is_explicit_tool_credential_error(tool_name, content):
    """Only classify a direct tool error, never assistant prose or read artifacts."""
    if str(tool_name or "") in _READ_ARTIFACT_TOOLS:
        return False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    evidence = " ".join(str(payload.get(key) or "") for key in ("error", "stderr", "output"))
    failed = (
        bool(payload.get("error"))
        or payload.get("exit_code") not in (None, 0)
        or str(payload.get("status") or "").lower() in {"error", "fail", "failed"}
    )
    return failed and bool(_CREDENTIAL_ERROR_RE.search(evidence))


def _is_synthetic_compaction_reference(role, content):
    return str(role or "") == "assistant" and str(content or "").startswith(("[PRIOR CONTEXT", "[CONTEXT COMPACTION]"))


def check_credential_friction_recent():
    state_db = HERMES / "state.db"
    if not state_db.exists():
        return {"name": "credential_friction_recent", "status": "skip", "detail": "no state db"}
    patterns = [
        "api key",
        "connected account",
        "connect your",
        "not authenticated",
        "authentication required",
        "no connected account",
        "credentials",
    ]
    try:
        where = " or ".join(["lower(content) like ?" for _ in patterns])
        params = tuple(f"%{pattern}%" for pattern in patterns)
        rows = _sqlite(
            state_db,
            "select role, tool_name, timestamp, content "
            f"from messages where {where} "
            "order by coalesce(timestamp,0) desc limit 12",
            params,
        )
        rows = [row for row in rows if not _is_synthetic_compaction_reference(row[0], row[3])]
        tool_fail_hits = [
            row for row in rows if str(row[0]) == "tool" and _is_explicit_tool_credential_error(row[1], row[3])
        ]
        status = "fail" if tool_fail_hits else "pass"
        samples = []
        for role, tool_name, _ts, content in tool_fail_hits[:3]:
            samples.append(f"{role}/{tool_name}:{str(content).replace(chr(10), ' ')[:140]}")
        return {
            "name": "credential_friction_recent",
            "status": status,
            "detail": (f"hits={len(rows)} explicit_tool_errors={len(tool_fail_hits)} samples=" + " | ".join(samples)),
        }
    except Exception as e:
        return {"name": "credential_friction_recent", "status": "warn", "detail": f"{type(e).__name__}: {str(e)[:160]}"}


CHECK_METADATA = {
    "topic_session_bindings": {
        "title": "Topic/session binding contract drift",
        "severity": "P2",
        "fix_class": "topic_binding_contract",
        "recommended_action": "Verify topic mode schema and per-topic session binding writer.",
    },
    "credential_friction_recent": {
        "title": "Recent credential friction surfaced to user",
        "severity": "P2",
        "fix_class": "auth_connection_classification",
        "recommended_action": (
            "Classify whether the missing credential is expected, a Composio connection gap, or stale tool advertising."
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
        or free_bytes < DISK_FAIL_FREE_BYTES
        or (pct >= DISK_FAIL_PERCENT and free_bytes < DISK_WARN_FREE_BYTES)
    ):
        status = "fail"
    elif pct >= DISK_WARN_PERCENT or free_bytes < DISK_WARN_FREE_BYTES:
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
        "warn_percent": DISK_WARN_PERCENT,
        "fail_percent": DISK_FAIL_PERCENT,
        "warn_free_bytes": DISK_WARN_FREE_BYTES,
        "fail_free_bytes": DISK_FAIL_FREE_BYTES,
    }


def _gateway_resource_metrics(pid):
    metrics: dict = {
        "open_descriptors": None,
        "state_db_handles": None,
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
                    if target.endswith(" (deleted)"):
                        deleted_targets.append(target)
            metrics["state_db_handles"] = state_handles
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
            metrics["state_db_handles"] = sum(
                bool(re.search(r"/state\.db(?:-(?:wal|shm))?$", parts[-1])) for parts in lines
            )
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
    fail = (
        (isinstance(fd, int) and fd >= GATEWAY_FD_FAIL)
        or (isinstance(state_handles, int) and state_handles >= GATEWAY_STATE_DB_HANDLE_FAIL)
        or (isinstance(deleted_state_handles, int) and deleted_state_handles > 0)
        or (isinstance(rss, int) and rss >= GATEWAY_RSS_FAIL_BYTES)
    )
    warn = (
        (isinstance(fd, int) and fd >= GATEWAY_FD_WARN)
        or (isinstance(state_handles, int) and state_handles >= GATEWAY_STATE_DB_HANDLE_WARN)
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
            f"pid={pid} open_descriptors={fd} state_db_handles={state_handles} "
            f"deleted_state_db_handles={deleted_state_handles} "
            f"rss_bytes={rss} fd_growth={fd_growth} state_db_growth={state_growth}"
        ),
        "gateway_pid": pid,
        **metrics,
        "fd_growth": fd_growth,
        "state_db_growth": state_growth,
    }


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
                }
            )
        names = [str(row["command"]).lower() for row in rows]
        metrics["process_count"] = len(names)
        metrics["powershell_count"] = sum(name in {"powershell", "pwsh"} for name in names)
        metrics["cmd_count"] = sum(name in {"cmd", "cmd.exe"} for name in names)
        metrics["conhost_count"] = sum(name in {"conhost", "conhost.exe"} for name in names)
        metrics["ssh_session_count"] = sum(name.startswith("sshd") for name in names)
        public_keys = ("pid", "age_seconds", "rss_bytes", "command")
        metrics["top_memory_processes"] = [
            {key: row[key] for key in public_keys}
            for row in sorted(rows, key=lambda item: item["rss_bytes"], reverse=True)[:5]
        ]
        metrics["oldest_processes"] = [
            {key: row[key] for key in public_keys}
            for row in sorted(rows, key=lambda item: item["age_seconds"], reverse=True)[:5]
        ]
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
    swap_allocated_warn = (
        swap_used_pct is not None and swap_used_pct >= HOST_SWAP_ALLOCATED_WARN_PERCENT
    )
    prior_swap_used_pct = prior_check.get("swap_used_pct")
    if swap_allocated_warn:
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
        or int(metrics.get("ssh_session_count") or 0) > HOST_SSH_SESSION_WARN
        or (isinstance(process_growth, int) and process_growth >= 100)
        or (isinstance(powershell_growth, int) and powershell_growth >= 10)
        or swap_active_warn
        or swap_allocated_warn
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
            f"powershell_growth={powershell_growth} swap_used_pct={swap_used_pct} "
            f"swap_out_bytes_per_minute={swap_out_bytes_per_minute}"
        ),
        **metrics,
        "virtual_free_pct": virtual_free_pct,
        "process_growth": process_growth,
        "powershell_growth": powershell_growth,
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
    """Run the ownership reconciler and expose only bounded lease counts."""
    steward = HERMES / "bin/hermes-host-steward.py"
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
            str(HERMES),
            "reconcile",
            "--apply",
        ],
        timeout=90,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "name": "host_steward",
            "status": "fail",
            "detail": f"invalid reconcile receipt rc={result.returncode}",
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
    args = ap.parse_args()
    checks = []
    check_functions = [
        check_config,
        check_client_context,
        check_gateway_state,
        check_runtime_release_identity,
        check_active_client_capabilities,
        check_local_brain,
        check_topic_session_bindings,
        check_advertised_tool_env,
        check_credential_friction_recent,
        check_telegram_transcript_hook,
        check_immersion_quality,
        check_telegram_organic_checkpoints,
        check_agent_probe,
        check_session_store_errors,
        check_logs,
        check_workspace_write,
        check_tool_readiness,
        check_image_quality_surface,
        check_document_visual_delivery,
        check_cron_toolset_preflight,
        check_canary_reconciler,
        check_disk_retention,
        check_disk,
        check_gateway_resource_pressure,
        check_host_capacity,
        check_host_steward,
    ]
    if args.skip_canary_reconciler:
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
        "gateway_runtime": gateway_runtime_binding(),
        "runtime_release": runtime_release_identity(),
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
