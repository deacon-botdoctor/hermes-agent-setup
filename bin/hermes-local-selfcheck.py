#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
HERMES = Path(os.environ.get("HERMES_HOME") or HOME / ".hermes").expanduser()
AGENT_PROBE_MAX_AGE_S = int(os.environ.get("HERMES_AGENT_PROBE_MAX_AGE_SECONDS", "1800"))
# Unified search is free-first; Exa is an optional paid escalation backend.
OPTIONAL_CAPABILITY_ENV_KEYS = {"EXA_API_KEY"}
DISK_WARN_PERCENT = int(os.environ.get("HERMES_DISK_WARN_PERCENT", "85"))
DISK_FAIL_PERCENT = int(os.environ.get("HERMES_DISK_FAIL_PERCENT", "92"))
DISK_WARN_FREE_BYTES = int(os.environ.get("HERMES_DISK_WARN_FREE_BYTES", str(15 * 1024**3)))
DISK_FAIL_FREE_BYTES = int(os.environ.get("HERMES_DISK_FAIL_FREE_BYTES", str(5 * 1024**3)))
GATEWAY_FD_WARN = int(os.environ.get("HERMES_GATEWAY_FD_WARN", "128"))
GATEWAY_FD_FAIL = int(os.environ.get("HERMES_GATEWAY_FD_FAIL", "512"))
GATEWAY_STATE_DB_HANDLE_WARN = int(os.environ.get("HERMES_GATEWAY_STATE_DB_HANDLE_WARN", "24"))
GATEWAY_STATE_DB_HANDLE_FAIL = int(os.environ.get("HERMES_GATEWAY_STATE_DB_HANDLE_FAIL", "96"))
GATEWAY_RSS_WARN_BYTES = int(os.environ.get("HERMES_GATEWAY_RSS_WARN_BYTES", str(4 * 1024**3)))
GATEWAY_RSS_FAIL_BYTES = int(os.environ.get("HERMES_GATEWAY_RSS_FAIL_BYTES", str(8 * 1024**3)))


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


def run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    except Exception as exc:

        class Result:
            returncode = 127
            stdout = ""
            stderr = f"{type(exc).__name__}: {str(exc)[:160]}"

        return Result()


def check_config():
    p = HERMES / "config.yaml"
    return {"name": "config", "status": "pass" if p.exists() and p.stat().st_size > 0 else "fail", "detail": str(p)}


def check_client_context():
    p = HERMES / "CLIENT_CONTEXT.md"
    return {"name": "client_context", "status": "pass" if p.exists() else "warn", "detail": str(p)}


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
            raw = json.dumps(d).lower()
            state = str(d.get("gateway_state") or d.get("state") or d.get("status") or "").lower()
            ts = parse_dt(d.get("updated_at") or d.get("generated_at") or d.get("ts"))
            age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else None
            ok = "running" in state or "connected" in raw or "healthy" in raw or state in {"ok", "active"}
            if ok and age is not None and age > 7200:
                proc = run(["pgrep", "-f", str(HERMES)], timeout=5)
                if proc.returncode == 0 and proc.stdout.strip():
                    return {
                        "name": "gateway_state",
                        "status": "pass",
                        "detail": f"{p.name} state={state or 'unknown'} age={age} process_alive=true pids="
                        + ",".join(proc.stdout.split()[:5]),
                    }
                ok = False
            return {
                "name": "gateway_state",
                "status": "pass" if ok else "fail",
                "detail": f"{p.name} state={state or 'unknown'} age={age}",
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
    process_alive = False
    if pid:
        try:
            os.kill(pid, 0)
            process_alive = True
        except OSError:
            process_alive = False
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
    if interval != 600:
        failures.append(f"interval={interval!r} expected=600")
    if global_enabled is not False:
        failures.append(f"global={global_enabled!r} expected=False")
    if telegram_enabled is not True:
        failures.append(f"telegram={telegram_enabled!r} expected=True")

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


def check_tool_readiness():
    p = HERMES / "state/tool-readiness-probe-latest.json"
    if not p.exists():
        return {"name": "tool_readiness", "status": "skip", "detail": "no tool-readiness state"}
    d = load_json(p, {})
    ts = parse_dt(d.get("timestamp") or d.get("generated_at") or d.get("checked_at"))
    age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else 10**9
    core = {"api_key_validity", "email"}
    broken = []
    for name, body in (d.get("tools") or {}).items():
        st = str((body or {}).get("status") or "")
        if name in core and st == "broken":
            broken.append(name)
    status = "fail" if age > 12 * 3600 or broken else "pass"
    return {"name": "tool_readiness", "status": status, "detail": f"age={age}s core_broken={broken[:8]}"}


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


def check_canary_reconciler():
    p = HERMES / "state/canary-reconciler-latest.json"
    if not p.exists():
        return {"name": "canary_reconciler", "status": "warn", "detail": "no canary reconciler state yet"}
    d = load_json(p, {})
    ts = parse_dt(d.get("checked_at") or d.get("timestamp") or d.get("generated_at"))
    age = int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else 10**9
    missing = d.get("missing_canaries") or []
    inventory = []
    for c in d.get("capabilities") or []:
        canary = c.get("canary") or {}
        if canary.get("status") == "inventory_only":
            inventory.append(c.get("id"))
    status = "warn" if age > 7200 or missing else "pass"
    detail = (
        f"age={age}s capabilities={len(d.get('capabilities') or [])} "
        f"missing={len(missing)} inventory_only={inventory[:8]}"
    )
    return {"name": "canary_reconciler", "status": status, "detail": detail}


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


def _sqlite(path: Path, query: str, params=()):
    import sqlite3

    con = sqlite3.connect(path)
    try:
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
        if "telegram_dm_topic_bindings" not in tables:
            return {
                "name": "topic_session_bindings",
                "status": "fail" if require else "warn",
                "detail": "telegram_dm_topic_bindings table missing"
                + (" (required)" if require else " (topic mode unverified)"),
            }
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
            "detail": f"bindings={count} enabled_topic_modes={enabled_count} required={require}",
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
    missing = [
        k for k in capability_refs if not env_keys.get(k) and not os.environ.get(k) and not _keychain_has_env_key(k)
    ]
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
    p = run(["df", "-Pk", str(HERMES)], timeout=5)
    if p.returncode != 0:
        return {"name": "disk", "status": "warn", "detail": (p.stderr or p.stdout)[:120]}
    lines = p.stdout.splitlines()
    if len(lines) < 2:
        return {"name": "disk", "status": "warn", "detail": "df parse failed"}
    parts = lines[-1].split()
    if len(parts) < 6:
        return {"name": "disk", "status": "warn", "detail": "df parse failed"}
    used = parts[4]
    try:
        pct = int(used.rstrip("%"))
        free_bytes = int(parts[3]) * 1024
    except Exception:
        return {"name": "disk", "status": "warn", "detail": "df parse failed"}
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
        "detail": f"used={used} free_bytes={free_bytes}",
    }


def _gateway_resource_metrics(pid):
    metrics = {"open_descriptors": None, "state_db_handles": None, "rss_bytes": None}
    proc_fd = Path(f"/proc/{pid}/fd")
    if proc_fd.is_dir():
        try:
            metrics["open_descriptors"] = sum(1 for _ in proc_fd.iterdir())
            state_handles = 0
            for fd in proc_fd.iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if re.search(r"/state\.db(?:-(?:wal|shm))?$", target):
                    state_handles += 1
            metrics["state_db_handles"] = state_handles
        except OSError:
            pass
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
        or (isinstance(rss, int) and rss >= GATEWAY_RSS_FAIL_BYTES)
    )
    warn = (
        (isinstance(fd, int) and fd >= GATEWAY_FD_WARN)
        or (isinstance(state_handles, int) and state_handles >= GATEWAY_STATE_DB_HANDLE_WARN)
        or (isinstance(rss, int) and rss >= GATEWAY_RSS_WARN_BYTES)
        or (isinstance(fd_growth, int) and fd_growth >= 32)
        or (isinstance(state_growth, int) and state_growth >= 8)
        or all(value is None for value in metrics.values())
    )
    return {
        "name": "gateway_resource_pressure",
        "status": "fail" if fail else ("warn" if warn else "pass"),
        "detail": (
            f"pid={pid} open_descriptors={fd} state_db_handles={state_handles} "
            f"rss_bytes={rss} fd_growth={fd_growth} state_db_growth={state_growth}"
        ),
        "gateway_pid": pid,
        **metrics,
        "fd_growth": fd_growth,
        "state_db_growth": state_growth,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default=os.environ.get("HERMES_AGENT_ID", "unknown"))
    ap.add_argument("--agent-name", default=os.environ.get("HERMES_AGENT_NAME", "unknown"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    checks = []
    check_functions = [
        check_config,
        check_client_context,
        check_gateway_state,
        check_local_brain,
        check_topic_session_bindings,
        check_advertised_tool_env,
        check_credential_friction_recent,
        check_telegram_transcript_hook,
        check_immersion_quality,
        check_telegram_organic_checkpoints,
        check_agent_probe,
        check_logs,
        check_tool_readiness,
        check_document_visual_delivery,
        check_canary_reconciler,
        check_disk,
        check_gateway_resource_pressure,
    ]
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
        "schema_version": 3,
        "agent_id": args.agent_id,
        "agent_name": args.agent_name,
        "hermes_home": str(HERMES),
        "host": (os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "unknown")),
        "checked_at": iso(),
        "gateway_runtime": gateway_runtime_binding(),
        "status": "fail" if failures else "pass",
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }
    state = HERMES / "state/local-selfcheck-latest.json"
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (HERMES / "logs/local-selfcheck.log").open("a", encoding="utf-8").write(
            f"{payload['checked_at']} status={payload['status']} failures={len(failures)}\n"
        )
    except Exception:
        pass
    print(json.dumps(payload, indent=2 if args.json else None, sort_keys=True))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
