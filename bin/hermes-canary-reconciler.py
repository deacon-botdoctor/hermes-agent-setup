#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

HOME = Path(os.environ.get("HOME") or str(Path.home())).expanduser()
HERMES = Path(os.environ.get("HERMES_HOME") or str(HOME / ".hermes")).expanduser()
STATE_DIR = HERMES / "state"
LOG_DIR = HERMES / "logs"
CAP_PATH = STATE_DIR / "runtime-capabilities.json"
LATEST_PATH = STATE_DIR / "canary-reconciler-latest.json"
LOG_PATH = LOG_DIR / "canary-reconciler.log"
REGISTRY_PATHS = [HERMES / "config" / "local-canary-registry.json", HERMES / "state" / "local-canary-registry.json"]
PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + str(HOME / ".local/bin")
LAUNCH_AGENT_DIRS = (
    HOME / "Library/LaunchAgents",
    Path("/Library/LaunchAgents"),
    Path("/System/Library/LaunchAgents"),
)
RETIRED_CRON_TAGS = ("HERMES_CODEX_EXEC_HEALTH",)
RETIRED_SCRIPTS = {"HERMES_CODEX_EXEC_HEALTH": "bin/codex-exec-health.py"}


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


def current_crontab() -> list[str] | None:
    p = run(["crontab", "-l"], timeout=10, env=env_for_runtime(), cwd=HOME)
    if p.returncode != 0:
        detail = f"{p.stdout}\n{p.stderr}".lower()
        return [] if "no crontab for" in detail else None
    return [line for line in p.stdout.splitlines() if line.strip()]


def _same_script_path(value: str, script: Path, systemd: bool = False) -> bool:
    candidate = value
    if systemd:
        escaped_percent = "\x00"
        candidate = candidate.replace("%%", escaped_percent).replace("%h", str(HOME)).replace(escaped_percent, "%")
    if not os.path.isabs(candidate):
        return False
    return os.path.normpath(candidate) == os.path.normpath(str(script))


def _command_invokes_script(argv: list[str], script: Path, systemd: bool = False) -> bool:
    """Recognize only deterministic native-supervisor argv forms.

    Cron is handled by exact canonical-line equality.  Trying to emulate shell
    parsing here recreates a shell interpreter and makes schedule proof less
    reliable, not more reliable.
    """
    if not argv:
        return False
    argv = list(argv)
    if systemd and argv:
        argv = [argv[0].lstrip("-@:+!"), *argv[1:]]
    index = 0
    if index < len(argv) and Path(argv[index]).name == "env":
        index += 1
        while index < len(argv) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[index], re.DOTALL):
            index += 1
    if index >= len(argv):
        return False
    command = argv[index:]
    if _same_script_path(command[0], script, systemd):
        return True
    executable = Path(command[0]).name
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return len(command) > 1 and _same_script_path(command[1], script, systemd)
    return False


def _launchd_invokes_script(payload: Any, script: Path) -> bool:
    if not isinstance(payload, dict):
        return False
    program = payload.get("Program")
    arguments = payload.get("ProgramArguments")
    if program is None:
        return (
            isinstance(arguments, list)
            and all(isinstance(item, str) for item in arguments)
            and _command_invokes_script(arguments, script)
        )
    if not isinstance(program, str):
        return False
    argv = [program]
    if isinstance(arguments, list) and arguments and all(isinstance(item, str) for item in arguments):
        argv.extend(arguments[1:])
    return _command_invokes_script(argv, script)


def _launchd_has_recurring_trigger(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    interval = payload.get("StartInterval")
    if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0:
        return True
    calendar = payload.get("StartCalendarInterval")
    if isinstance(calendar, dict):
        return True
    if isinstance(calendar, list) and any(isinstance(item, dict) for item in calendar):
        return True
    keep_alive = payload.get("KeepAlive")
    if keep_alive is True or isinstance(keep_alive, dict) and bool(keep_alive):
        return True
    for key in ("WatchPaths", "QueueDirectories"):
        value = payload.get(key)
        if isinstance(value, list) and any(isinstance(item, str) and item for item in value):
            return True
    return payload.get("StartOnMount") is True


def _launchd_print_invokes_script(text: str, script: Path) -> bool:
    program: str | None = None
    arguments: list[str] = []
    in_arguments = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if in_arguments:
            if line == "}":
                in_arguments = False
            elif line:
                arguments.append(re.sub(r"^\d+\s*=\s*", "", line))
            continue
        if line == "arguments = {":
            in_arguments = True
        elif line.startswith("program = "):
            program = line.removeprefix("program = ").strip()
    payload: dict[str, Any] = {"ProgramArguments": arguments}
    if program is not None:
        payload["Program"] = program
    return _launchd_invokes_script(payload, script)


def _launchd_print_has_recurring_trigger(text: str) -> bool:
    if re.search(r"(?:^|\n)\s*run interval = [1-9][0-9]* seconds\s*(?:\n|$)", text):
        return True
    if re.search(r"(?:^|\n)\s*continuous = true\s*(?:\n|$)", text):
        return True
    match = re.search(r"(?:^|\n)\s*event triggers = \{(.*?)\n\s*\}", text, re.DOTALL)
    return match is not None and bool(match.group(1).strip())


def _active_launchd_schedule(path: Path, script: Path) -> bool:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        label = payload.get("Label") if isinstance(payload, dict) else None
        if (
            not isinstance(label, str)
            or not _launchd_invokes_script(payload, script)
            or not _launchd_has_recurring_trigger(payload)
        ):
            return False
        domain = f"gui/{os.getuid()}"
        loaded = run(
            ["launchctl", "print", f"{domain}/{label}"],
            timeout=10,
            env=env_for_runtime(),
            cwd=HOME,
        )
        if (
            loaded.returncode != 0
            or not _launchd_print_invokes_script(loaded.stdout, script)
            or not _launchd_print_has_recurring_trigger(loaded.stdout)
        ):
            return False
        disabled = run(
            ["launchctl", "print-disabled", domain],
            timeout=10,
            env=env_for_runtime(),
            cwd=HOME,
        )
    except (OSError, plistlib.InvalidFileException, ExpatError, subprocess.SubprocessError):
        return False
    return (
        disabled.returncode != 0 or re.search(rf'["\']?{re.escape(label)}["\']?\s*=>\s*true', disabled.stdout) is None
    )


def _systemd_show_invokes_script(text: str, script: Path) -> bool:
    for line in text.splitlines():
        match = re.search(
            r"(?:^|;\s*)argv\[\]=(.*?)(?:\s*;\s*(?:ignore_errors|start_time|stop_time|pid|code|status)=|$)",
            line,
        )
        if not match:
            continue
        try:
            argv = shlex.split(match.group(1), comments=False, posix=True)
        except ValueError:
            continue
        if _command_invokes_script(argv, script, systemd=True):
            return True
    return False


def _systemd_schedule_details(paths: list[Path], script: Path) -> list[dict[str, Any]]:
    timer_names = {path.name for path in paths if path.suffix == ".timer"}
    try:
        discovered = run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=timer",
                "--state=active",
                "--all",
                "--plain",
                "--no-legend",
                "--no-pager",
            ],
            timeout=10,
            env=env_for_runtime(),
            cwd=HOME,
        )
    except (OSError, subprocess.SubprocessError):
        discovered = None
    if discovered is not None and discovered.returncode == 0:
        for line in discovered.stdout.splitlines():
            fields = line.split()
            if fields and fields[0].endswith(".timer") and Path(fields[0]).name == fields[0]:
                timer_names.add(fields[0])
    schedules: list[dict[str, Any]] = []
    for timer_name in sorted(timer_names):
        try:
            enabled = run(
                ["systemctl", "--user", "is-enabled", timer_name],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
            active = run(
                ["systemctl", "--user", "is-active", timer_name],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
            triggers = run(
                ["systemctl", "--user", "show", timer_name, "--property=Triggers", "--value"],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
            timer_fragment = run(
                ["systemctl", "--user", "show", timer_name, "--property=FragmentPath", "--value"],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if triggers.returncode != 0:
            continue
        for service_name in triggers.stdout.split():
            if not service_name.endswith(".service") or Path(service_name).name != service_name:
                continue
            try:
                command = run(
                    ["systemctl", "--user", "show", service_name, "--property=ExecStart", "--value"],
                    timeout=10,
                    env=env_for_runtime(),
                    cwd=HOME,
                )
                service_fragment = run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        service_name,
                        "--property=FragmentPath",
                        "--value",
                    ],
                    timeout=10,
                    env=env_for_runtime(),
                    cwd=HOME,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if command.returncode == 0 and _systemd_show_invokes_script(command.stdout, script):
                schedules.append(
                    {
                        "timer": timer_name,
                        "service": service_name,
                        "enabled": enabled.returncode == 0,
                        "active": active.returncode == 0,
                        "timer_fragment": (
                            timer_fragment.stdout.strip()
                            if timer_fragment.returncode == 0
                            else ""
                        ),
                        "service_fragment": (
                            service_fragment.stdout.strip()
                            if service_fragment.returncode == 0
                            else ""
                        ),
                    }
                )
    return schedules


def _active_systemd_schedule(paths: list[Path], script: Path) -> bool:
    return any(
        schedule["enabled"] and schedule["active"]
        for schedule in _systemd_schedule_details(paths, script)
    )


def retire_launchd_schedules(script: Path, dry_run: bool) -> str | None:
    schedules: list[tuple[Path, str]] = []
    for directory in LAUNCH_AGENT_DIRS:
        for path in directory.glob("*.plist"):
            try:
                with path.open("rb") as handle:
                    payload = plistlib.load(handle)
            except (OSError, plistlib.InvalidFileException, ExpatError):
                continue
            label = payload.get("Label") if isinstance(payload, dict) else None
            if isinstance(label, str) and _launchd_invokes_script(payload, script):
                schedules.append((path, label))
    if not schedules:
        return None
    if dry_run:
        return "would_update"
    domain = f"gui/{os.getuid()}"
    for path, label in schedules:
        try:
            loaded = run(
                ["launchctl", "print", f"{domain}/{label}"],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
            if loaded.returncode == 0:
                if not _launchd_print_invokes_script(loaded.stdout, script):
                    return f"failed:launchd_label_collision:{label}"
                stopped = run(
                    ["launchctl", "bootout", f"{domain}/{label}"],
                    timeout=10,
                    env=env_for_runtime(),
                    cwd=HOME,
                )
                if stopped.returncode != 0:
                    return f"failed:launchd_bootout:{label}"
            path.unlink()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"failed:launchd_retire:{type(exc).__name__}"
    return "updated"


def retire_systemd_schedules(script: Path, dry_run: bool) -> str | None:
    systemd_home = HOME / ".config/systemd/user"
    systemd_paths = [*systemd_home.glob("*.service"), *systemd_home.glob("*.timer")]
    schedules = _systemd_schedule_details(systemd_paths, script)
    if not schedules:
        return None
    if dry_run:
        return "would_update"
    timer_names = sorted({str(schedule["timer"]) for schedule in schedules})
    fragments: set[Path] = set()
    for schedule in schedules:
        for key in ("timer_fragment", "service_fragment"):
            raw_fragment = str(schedule.get(key) or "")
            if not raw_fragment:
                continue
            fragment = Path(raw_fragment)
            if not fragment.is_absolute():
                continue
            fragment = Path(os.path.normpath(str(fragment)))
            try:
                fragment.relative_to(systemd_home)
            except ValueError:
                continue
            fragments.add(fragment)
    for timer_name in timer_names:
        try:
            stopped = run(
                ["systemctl", "--user", "disable", "--now", timer_name],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
            if stopped.returncode != 0:
                return f"failed:systemd_disable:{timer_name}"
        except (OSError, subprocess.SubprocessError) as exc:
            return f"failed:systemd_retire:{type(exc).__name__}"
    try:
        for fragment in sorted(fragments):
            fragment.unlink(missing_ok=True)
    except OSError as exc:
        return f"failed:systemd_retire:{type(exc).__name__}"
    try:
        reloaded = run(
            ["systemctl", "--user", "daemon-reload"],
            timeout=10,
            env=env_for_runtime(),
            cwd=HOME,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed:systemd_reload:{type(exc).__name__}"
    if reloaded.returncode != 0:
        return "failed:systemd_reload"
    for timer_name in timer_names:
        try:
            masked = run(
                ["systemctl", "--user", "mask", timer_name],
                timeout=10,
                env=env_for_runtime(),
                cwd=HOME,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"failed:systemd_mask:{type(exc).__name__}"
        if masked.returncode != 0:
            return f"failed:systemd_mask:{timer_name}"
    return "updated"


def existing_schedule(
    tag: str,
    script: Path,
    expected_cron_line: str | None = None,
    cron_lines: list[str] | None = None,
) -> str | None:
    lines = current_crontab() if cron_lines is None else cron_lines
    if expected_cron_line is not None and lines is not None and expected_cron_line in lines:
        return "cron"
    for directory in LAUNCH_AGENT_DIRS:
        for path in directory.glob("*.plist"):
            if _active_launchd_schedule(path, script):
                return "launchd"
    systemd_home = HOME / ".config/systemd/user"
    systemd_paths = [*systemd_home.glob("*.service"), *systemd_home.glob("*.timer")]
    if _active_systemd_schedule(systemd_paths, script):
        return "systemd-user"
    return None


def _cron_line_has_tag(line: str, tag: str) -> bool:
    return re.search(rf"(?:^|\s)#\s*{re.escape(tag)}\s*$", line) is not None


def install_cron(
    tag: str,
    line: str,
    dry_run: bool,
    ensure_present: bool = True,
    current: list[str] | None = None,
) -> str:
    if current is None:
        current = current_crontab()
    if current is None:
        return "failed:crontab_read"
    matching = [x for x in current if _cron_line_has_tag(x, tag)]
    desired = [line] if ensure_present else []
    if matching == desired:
        return "unchanged"
    lines = [x for x in current if not _cron_line_has_tag(x, tag)]
    if ensure_present:
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

    for retired_tag in RETIRED_CRON_TAGS:
        retired_script = expand_path(RETIRED_SCRIPTS[retired_tag])
        for scheduler, retire in (
            ("launchd", retire_launchd_schedules),
            ("systemd-user", retire_systemd_schedules),
        ):
            native_status = retire(retired_script, args.dry_run)
            if native_status is not None:
                actions.append(
                    {
                        "capability": "retired_scheduler_cleanup",
                        "canary": retired_tag,
                        "action": "ensure_retired",
                        "scheduler": scheduler,
                        "status": native_status,
                    }
                )
        cron_lines = current_crontab()
        if cron_lines is None:
            status = "failed:crontab_read"
        elif not any(_cron_line_has_tag(line, retired_tag) for line in cron_lines):
            continue
        else:
            status = install_cron(retired_tag, "", args.dry_run, ensure_present=False, current=cron_lines)
        actions.append(
            {
                "capability": "retired_scheduler_cleanup",
                "canary": retired_tag,
                "action": "ensure_retired",
                "status": status,
            }
        )

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
        if tag in RETIRED_CRON_TAGS:
            cap["canary"].update({"status": "retired", "reason": "superseded by the contract-derived fleet pulse"})
            capabilities.append(cap)
            continue
        line = cron_line_for(canary, script, agent_id, agent_name)
        cron_lines = current_crontab()
        schedule_kind = existing_schedule(tag, script, line, cron_lines or [])
        native_schedule = schedule_kind in {"launchd", "systemd-user"}
        if cron_lines is None:
            cron_change = "failed:crontab_read"
        else:
            cron_change = install_cron(
                tag,
                line,
                args.dry_run,
                ensure_present=not native_schedule,
                current=cron_lines,
            )
        if cron_change == "unchanged" and schedule_kind:
            cron_status = f"present:{schedule_kind}"
        elif schedule_kind and cron_change == "updated":
            cron_status = f"normalized:{schedule_kind}"
        elif schedule_kind and cron_change == "would_update":
            cron_status = f"would_normalize:{schedule_kind}"
        else:
            cron_status = cron_change
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
