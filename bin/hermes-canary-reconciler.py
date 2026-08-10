#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HOME") or str(Path.home())).expanduser()
HERMES = Path(os.environ.get("HERMES_HOME") or str(HOME / ".hermes")).expanduser()
STATE_DIR = HERMES / "state"
LOG_DIR = HERMES / "logs"
CAP_PATH = STATE_DIR / "runtime-capabilities.json"
LATEST_PATH = STATE_DIR / "canary-reconciler-latest.json"
LOG_PATH = LOG_DIR / "canary-reconciler.log"
REGISTRY_PATHS = [HERMES / "config" / "local-canary-registry.json", HERMES / "state" / "local-canary-registry.json"]
PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + str(HOME / ".local/bin")


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{iso()} {line}\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(
    cmd: list[str], timeout: int = 30, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def state_age(path: Path) -> int | None:
    data = read_json(path, None)
    if not isinstance(data, dict):
        return None
    dt = parse_dt(data.get("checked_at") or data.get("updated_at") or data.get("timestamp"))
    if not dt:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def env_for_runtime() -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(HOME)
    env["HERMES_HOME"] = str(HERMES)
    env["CODEX_HOME"] = str(HOME / ".codex")
    env["PATH"] = PATH_PREFIX + ":" + env.get("PATH", "")
    return env


def command_exists(name: str) -> str | None:
    return shutil.which(name, path=env_for_runtime().get("PATH"))


def default_registry() -> list[dict[str, Any]]:
    return [
        {
            "capability_id": "hermes_core",
            "title": "Hermes runtime core",
            "detect": {"any_path": ["config.yaml", "CLIENT_CONTEXT.md"]},
            "canary": {
                "id": "local_selfcheck",
                "required": True,
                "script": "bin/hermes-local-selfcheck.py",
                "state": "state/local-selfcheck-latest.json",
                "interval_minutes": 15,
                "cron_tag": "HERMES_LOCAL_SELFCHECK",
                "run_if_stale_seconds": 1800,
                "args": ["--agent-id", "{agent_id}", "--agent-name", "{agent_name}"],
            },
        },
        {
            "capability_id": "codex",
            "title": "Codex CLI/auth lane",
            "detect": {"any_command": ["codex"], "any_path": ["~/.codex/auth.json", "~/.codex/config.toml"]},
            "canary": {
                "id": "codex_exec_health",
                "required": False,
                "script": "bin/codex-exec-health.py",
                "state": "state/codex-exec-health.json",
                "interval_minutes": 20,
                "cron_tag": "HERMES_CODEX_EXEC_HEALTH",
                "run_if_stale_seconds": 3600,
                "env": {
                    "CODEX_EXEC_HEALTH_TIMEOUT": "30",
                    # Real backend exec is gated to at most once per 6h per host.
                    # The login-status + endpoint + blob checks still run every tick;
                    # only the billed gpt-5.5 exec is throttled. Prevents a 401'd host
                    # from firing a real probe every 20 min (subscription quota storm).
                    "CODEX_EXEC_HEALTH_REAL_PROBE": "periodic",
                    "CODEX_EXEC_HEALTH_REAL_PROBE_MIN_INTERVAL_SECONDS": "21600",
                },
            },
        },
        {
            "capability_id": "mcp",
            "title": "MCP tool servers",
            "detect": {"config_contains": ["mcp_servers", "mcp:", "model_context_protocol"]},
            "canary": None,
        },
    ]


def load_registry() -> list[dict[str, Any]]:
    for path in REGISTRY_PATHS:
        data = read_json(path, None)
        if isinstance(data, dict) and isinstance(data.get("capabilities"), list):
            return data["capabilities"]
        if isinstance(data, list):
            return data
    return default_registry()


def expand_path(raw: str) -> Path:
    text = raw.format(home=str(HOME), hermes=str(HERMES))
    if text.startswith("~/"):
        return HOME / text[2:]
    p = Path(text)
    return p if p.is_absolute() else HERMES / p


def config_text() -> str:
    chunks: list[str] = []
    for rel in ["config.yaml", "config.yml", "mcp.json", ".mcp.json"]:
        p = HERMES / rel
        try:
            if p.exists():
                chunks.append(p.read_text(encoding="utf-8", errors="replace")[:200000])
        except Exception:
            pass
    return "\n".join(chunks).lower()


def detected(spec: dict[str, Any], cfg_text: str) -> tuple[bool, list[str]]:
    detect = spec.get("detect") or {}
    reasons: list[str] = []
    for raw in detect.get("any_path") or []:
        p = expand_path(str(raw))
        if p.exists():
            reasons.append(f"path:{raw}")
    for cmd in detect.get("any_command") or []:
        found = command_exists(str(cmd))
        if found:
            reasons.append(f"command:{cmd}={found}")
    for needle in detect.get("config_contains") or []:
        if str(needle).lower() in cfg_text:
            reasons.append(f"config:{needle}")
    if not detect and spec.get("always"):
        reasons.append("always")
    return bool(reasons), reasons


def agent_ids(args: argparse.Namespace) -> tuple[str, str]:
    agent_id = args.agent_id or os.environ.get("HERMES_AGENT_ID") or HERMES.name or "unknown"
    agent_name = args.agent_name or os.environ.get("HERMES_AGENT_NAME") or agent_id
    return str(agent_id), str(agent_name)


def format_args(items: list[str], agent_id: str, agent_name: str) -> list[str]:
    return [str(x).format(agent_id=agent_id, agent_name=agent_name, home=str(HOME), hermes=str(HERMES)) for x in items]


def cron_line_for(canary: dict[str, Any], script: Path, agent_id: str, agent_name: str) -> str:
    interval = int(canary.get("interval_minutes") or 20)
    tag = str(canary.get("cron_tag") or f"HERMES_CANARY_{canary.get('id', 'UNKNOWN')}")
    jitter_seed = f"{HERMES}\x00{tag}".encode("utf-8")
    jitter = int.from_bytes(hashlib.sha256(jitter_seed).digest()[:4], "big") % 180
    env_parts = [f"HERMES_HOME={sh_quote(str(HERMES))}"]
    for k, v in (canary.get("env") or {}).items():
        env_parts.append(f"{k}={sh_quote(str(v))}")
    args = " ".join(sh_quote(x) for x in format_args(canary.get("args") or [], agent_id, agent_name))
    cmd = (
        f"cd {sh_quote(str(HOME))} && {' '.join(env_parts)} "
        f"{sh_quote(str(script))}{(' ' + args) if args else ''} >/dev/null 2>&1"
    )
    return f"*/{interval} * * * * sleep {jitter}; {cmd} # {tag}"


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def current_crontab() -> list[str]:
    p = run(["crontab", "-l"], timeout=10, env=env_for_runtime(), cwd=HOME)
    if p.returncode != 0:
        return []
    return [line for line in p.stdout.splitlines() if line.strip()]


def _mentions_schedule(value: Any, needles: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(needle in value for needle in needles)
    if isinstance(value, dict):
        return any(_mentions_schedule(item, needles) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_mentions_schedule(item, needles) for item in value)
    return False


def _active_launchd_schedule(path: Path, needles: tuple[str, ...]) -> bool:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        label = payload.get("Label") if isinstance(payload, dict) else None
        if not isinstance(label, str) or not _mentions_schedule(payload, needles):
            return False
        domain = f"gui/{os.getuid()}"
        loaded = run(
            ["launchctl", "print", f"{domain}/{label}"],
            timeout=10,
            env=env_for_runtime(),
            cwd=HOME,
        )
        if loaded.returncode != 0:
            return False
        disabled = run(
            ["launchctl", "print-disabled", domain],
            timeout=10,
            env=env_for_runtime(),
            cwd=HOME,
        )
    except (OSError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return False
    return (
        disabled.returncode != 0 or re.search(rf'["\']?{re.escape(label)}["\']?\s*=>\s*true', disabled.stdout) is None
    )


def _active_systemd_schedule(paths: list[Path], needles: tuple[str, ...]) -> bool:
    timers: set[Path] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(needle in text for needle in needles):
            continue
        timer = path if path.suffix == ".timer" else path.with_suffix(".timer")
        if timer.is_file():
            timers.add(timer)
    for timer in timers:
        try:
            enabled = run(
                ["systemctl", "--user", "is-enabled", timer.name],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
            active = run(
                ["systemctl", "--user", "is-active", timer.name],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if enabled.returncode == 0 and active.returncode == 0:
            return True
    return False


def existing_schedule(tag: str, script: Path) -> str | None:
    if any(not line.lstrip().startswith("#") and (tag in line or str(script) in line) for line in current_crontab()):
        return "cron"
    portable_name = script.stem.removeprefix("hermes-")
    needles = (tag, str(script), script.name, portable_name)
    for path in (HOME / "Library/LaunchAgents").glob("*.plist"):
        if _active_launchd_schedule(path, needles):
            return "launchd"
    systemd_home = HOME / ".config/systemd/user"
    systemd_paths = [*systemd_home.glob("*.service"), *systemd_home.glob("*.timer")]
    if _active_systemd_schedule(systemd_paths, needles):
        return "systemd-user"
    return None


def install_cron(tag: str, line: str, dry_run: bool) -> str:
    current = current_crontab()
    matching = [x for x in current if tag in x]
    if matching == [line]:
        return "unchanged"
    lines = [x for x in current if tag not in x]
    lines.append(line)
    if dry_run:
        return "would_update"
    payload = "\n".join(lines) + "\n"
    temp_path: Path | None = None
    try:
        # Darwin's crontab truncates long file arguments. Keep the install
        # payload in the OS temp directory rather than beneath a deep profile
        # path; NamedTemporaryFile remains private (0600) and is always removed.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=tempfile.gettempdir(),
            prefix="hermes-cron-",
            delete=False,
        ) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        p = subprocess.run(
            ["crontab", str(temp_path)],
            text=True,
            capture_output=True,
            timeout=10,
            env=env_for_runtime(),
            cwd=str(HOME),
        )
    except subprocess.TimeoutExpired:
        return "failed:crontab_timeout"
    except Exception as exc:
        return "failed:" + type(exc).__name__ + ": " + str(exc)[:160]
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    if p.returncode != 0:
        return "failed:" + ((p.stderr or p.stdout).strip()[:200])
    return "updated"


def run_canary(script: Path, canary: dict[str, Any], agent_id: str, agent_name: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"ran": False, "reason": "dry_run"}
    env = env_for_runtime()
    for k, v in (canary.get("env") or {}).items():
        env[str(k)] = str(v)
    argv = [str(script), *format_args(canary.get("args") or [], agent_id, agent_name)]
    try:
        p = run(argv, timeout=int(canary.get("timeout_seconds") or 90), env=env, cwd=HOME)
        return {"ran": True, "rc": p.returncode, "detail": ((p.stdout or "") + " " + (p.stderr or "")).strip()[-300:]}
    except Exception as exc:
        return {"ran": True, "rc": 125, "detail": f"{type(exc).__name__}: {str(exc)[:180]}"}


def reconcile(args: argparse.Namespace) -> dict[str, Any]:
    agent_id, agent_name = agent_ids(args)
    registry = load_registry()
    cfg = config_text()
    capabilities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    optional_unavailable: list[dict[str, Any]] = []

    for spec in registry:
        ok, reasons = detected(spec, cfg)
        if not ok:
            continue
        cap_id = str(spec.get("capability_id") or spec.get("id") or "unknown")
        cap: dict[str, Any] = {"id": cap_id, "title": spec.get("title") or cap_id, "detected": True, "reasons": reasons}
        canary = spec.get("canary")
        if not isinstance(canary, dict):
            cap["canary"] = {"status": "inventory_only", "reason": "no local canary registered yet"}
            capabilities.append(cap)
            continue
        script = expand_path(str(canary.get("script") or ""))
        state = expand_path(str(canary.get("state") or ""))
        cap["canary"] = {"id": canary.get("id"), "script": str(script), "state": str(state)}
        if not script.exists():
            unavailable = {
                "capability": cap_id,
                "canary": canary.get("id"),
                "reason": "script missing",
                "script": str(script),
            }
            if bool(canary.get("required", True)):
                cap["canary"].update({"status": "missing", "reason": "required script missing"})
                missing.append(unavailable)
            else:
                cap["canary"].update({"status": "inventory_only", "reason": "optional script missing"})
                optional_unavailable.append(unavailable)
            capabilities.append(cap)
            continue
        tag = str(canary.get("cron_tag") or f"HERMES_CANARY_{canary.get('id', 'UNKNOWN')}")
        line = cron_line_for(canary, script, agent_id, agent_name)
        schedule_kind = existing_schedule(tag, script)
        cron_status = f"present:{schedule_kind}" if schedule_kind else install_cron(tag, line, args.dry_run)
        actions.append(
            {"capability": cap_id, "canary": canary.get("id"), "action": "ensure_cron", "status": cron_status}
        )
        age = state_age(state)
        stale = age is None or age > int(canary.get("run_if_stale_seconds") or 3600)
        run_result = {"ran": False, "reason": "fresh", "age_seconds": age}
        if stale or args.run_canaries:
            run_result = run_canary(script, canary, agent_id, agent_name, args.dry_run)
            actions.append({"capability": cap_id, "canary": canary.get("id"), "action": "run_if_stale", **run_result})
        cap["canary"].update(
            {"status": "enabled", "cron": cron_status, "state_age_seconds": state_age(state), "last_run": run_result}
        )
        capabilities.append(cap)

    failed_actions = [
        action
        for action in actions
        if str(action.get("status") or "").startswith("failed:")
        or (action.get("action") == "run_if_stale" and action.get("ran") and int(action.get("rc") or 0) != 0)
    ]
    payload = {
        "schema_version": 1,
        "checked_at": iso(),
        "agent_id": agent_id,
        "agent_name": agent_name,
        "home": str(HOME),
        "hermes_home": str(HERMES),
        "capabilities": capabilities,
        "missing_canaries": missing,
        "optional_canaries_unavailable": optional_unavailable,
        "failed_actions": failed_actions,
        "actions": actions,
        "ok": not missing and not failed_actions,
    }
    if not args.dry_run:
        capability_keys = [
            "schema_version",
            "checked_at",
            "agent_id",
            "agent_name",
            "home",
            "hermes_home",
            "capabilities",
            "missing_canaries",
            "optional_canaries_unavailable",
            "failed_actions",
            "ok",
        ]
        write_json(CAP_PATH, {key: payload[key] for key in capability_keys})
        write_json(LATEST_PATH, payload)
        log(f"capabilities={len(capabilities)} missing={len(missing)} actions={len(actions)} ok={payload['ok']}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id")
    ap.add_argument("--agent-name")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-canaries", action="store_true", help="Run enabled canaries even if their state is fresh.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    payload = reconcile(args)
    print(json.dumps(payload, indent=2 if args.json else None, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
