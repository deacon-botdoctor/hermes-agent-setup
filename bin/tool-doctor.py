#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

try:
    import yaml
except Exception:
    yaml = None


class UnverifiedConfig(RuntimeError):
    pass


DENY_NAMES = {".env", ".env.secrets", "auth.json"}
STAMP_SUFFIX = ".bak-tooldoctor-phase0-20260704"
RECIPE_PATH = Path(__file__).resolve().parents[1] / "recipes/tool-doctor/mcp-provisioning-v1.yaml"
HEALTH_PATH = "state/tool-health.json"
BUDGET_PATH = "state/tool-doctor-budget.json"
HEARTBEAT_PATH = "state/tool-doctor-heartbeat.json"
HEAL_LOCK_PATH = "state/tool-doctor-heal.lock"
CALL_INCIDENT_SIGNATURES = {
    "mcp_call_delivery_unknown",
    "mcp_call_retry_failed",
    "current_process_activation_failed",
}
TOOL_DOCTOR_VERSION = "phase5"
RECEIPT_SCHEMA_VERSION = 1
LIVE_PROBE_SCHEMA_VERSION = 1
BUILTIN_SAFE_PROBES = {
    "capability-router": {
        "name": "search_capabilities",
        "arguments": {"query": "stripe customer invoice subscription", "max_hits": 5},
        "read_only": True,
        "side_effects": False,
        "source": "tool_doctor_builtin",
    }
}
ESCALATION_RUNBOOKS = {
    "auth_or_token": "operator-auth-repair-runbook",
    "unknown": "operator-tool-doctor-unknown-signature",
    "pglite_wasm_runtime": "operations/mac-gbrain-pglite-wasm-resolution",
    "no_recipe": "operator-tool-doctor-recipe-missing",
    "stale_tree_reference": "operations/hermes/rollout-repoint-contract",
    "remote_mcp_unreachable": "operator-remote-mcp-connectivity-review",
    "stale_session": "operator-tool-doctor-stale-session",
    "required_server_undeclared": "operator-required-mcp-config-repair",
    "required_server_disabled": "operator-required-mcp-config-repair",
}
REMOTE_MCP_TRANSPORTS = {"streamable_http", "http", "sse"}
REQUIRED_MCP_SERVERS: tuple[str, ...] = ()
STALE_TREE_RE = re.compile(
    r"(?:\\.pre-[^/]*|\\.bak[^/]*|\\.failed-[^/]*|\\.OLD[^/]*|pre-v[^/]*|bak-[^/]*|failed-[^/]*)"
)
WINDOWS_ENV_VAR_RE = re.compile(r"%([^%]+)%")
CLASSIFY_SAMPLES = {
    "missing-browser-lane": {
        "server": "browser-lane",
        "signature": "missing executable '/home/hermes-test/.hermes/repos/browser-lane/.venv/bin/python3'",
        "command": "/home/hermes-test/.hermes/repos/browser-lane/.venv/bin/python3",
        "cwd": "/home/hermes-test",
        "env_key_names": ["HOME", "HERMES_HOME"],
    },
    "connection-closed-python": {
        "server": "local-document-tools",
        "signature": "Failed to connect to MCP server 'local-document-tools' (command=python3): Connection closed",
        "command": "python3",
        "cwd": "/home/hermes-test",
        "env_key_names": ["HOME", "HERMES_HOME"],
    },
    "auth-401": {
        "server": "google-drive",
        "signature": "HTTP 401 Unauthorized: OAuth token expired",
        "command": "python3",
        "cwd": "/home/hermes-test",
        "env_key_names": ["SERVICE_CLIENT_ID", "SERVICE_REFRESH_TOKEN"],
    },
    "unknown": {
        "server": "local-document-tools",
        "signature": "tool subprocess exited with strange unsupported frame",
        "command": "python3",
        "cwd": "/home/hermes-test",
        "env_key_names": ["HOME", "HERMES_HOME"],
    },
    "pglite-wasm": {
        "server": "gbrain",
        "signature": "PGLite failed to initialize its WASM runtime",
        "command": "/home/hermes-test/.hermes/bin/gbrain",
        "cwd": "/home/hermes-test",
        "env_key_names": ["HOME", "HERMES_HOME"],
    },
}


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_key() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def load_yaml(path: Path) -> dict:
    if yaml is None:
        if path.name == "config.yaml":
            return load_config_via_runtime(path.parent)
        raise UnverifiedConfig("PyYAML is required")
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SELECTED_CONFIG_PATH: Path | None = None


def validate_config_path(home: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UnverifiedConfig("Tool Doctor config path must be absolute")
    resolved = path.resolve()
    root_config = (home / "config.yaml").resolve()
    if resolved == root_config:
        if not resolved.is_file():
            raise UnverifiedConfig("selected Tool Doctor config does not exist")
        return resolved
    profiles_root = (home / "profiles").resolve()
    try:
        relative = resolved.relative_to(profiles_root)
    except ValueError as exc:
        raise UnverifiedConfig("Tool Doctor config path is outside the Hermes root") from exc
    if (
        len(relative.parts) != 2
        or not PROFILE_NAME_RE.fullmatch(relative.parts[0])
        or relative.parts[1] != "config.yaml"
    ):
        raise UnverifiedConfig("Tool Doctor profile config path is not canonical")
    if not resolved.is_file():
        raise UnverifiedConfig("selected Tool Doctor config does not exist")
    return resolved


def select_config_path(home: Path, raw: str | None) -> None:
    global _SELECTED_CONFIG_PATH
    _SELECTED_CONFIG_PATH = validate_config_path(home, raw) if raw else None


def load_config(home: Path) -> dict:
    path = _SELECTED_CONFIG_PATH or home / "config.yaml"
    if yaml is None:
        return load_config_via_runtime(home, path)
    return load_yaml(path)


def runtime_python_candidates(home: Path, cfg: dict | None = None) -> list[str]:
    candidates: list[str] = []

    def add(raw: object) -> None:
        if not isinstance(raw, str) or not raw:
            return
        expanded = raw.replace("${HOME}", str(home.parent)).replace("$HOME", str(home.parent))
        expanded = expanded.replace("${HERMES_HOME}", str(home)).replace("$HERMES_HOME", str(home))
        expanded = expanded.replace("%USERPROFILE%", str(home.parent)).replace("%HERMES_HOME%", str(home))
        expanded = expanded.replace("~", str(home.parent), 1) if expanded.startswith("~") else expanded
        if expanded not in candidates:
            candidates.append(expanded)

    for val in ((cfg or {}).get("mcp_servers") or {}).values():
        if not isinstance(val, dict) or val.get("enabled", True) is False:
            continue
        command = val.get("command")
        command_l = command.lower() if isinstance(command, str) else ""
        if isinstance(command, str) and (
            command_l.endswith("/python")
            or command_l.endswith("/python3")
            or "/bin/python" in command_l
            or command_l.endswith("\\python.exe")
            or "\\scripts\\python" in command_l
        ):
            add(command)
    add(str(home / "hermes-agent/venv/bin/python"))
    add(str(home / "hermes-agent/venv/bin/python3"))
    add(str(home / "hermes-agent/venv/Scripts/python.exe"))
    add(str(home / ".dayreview-venv/Scripts/python.exe"))
    add(str(home.parent / "venv/bin/python"))
    add(str(home.parent / "venv/bin/python3"))
    return candidates


def load_config_via_runtime(home: Path, cfg_path: Path | None = None) -> dict:
    cfg_path = cfg_path or home / "config.yaml"
    if not cfg_path.exists():
        return {}
    script = (
        "import json, pathlib, sys, yaml\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "print(json.dumps(yaml.safe_load(path.read_text()) or {}))\n"
    )
    errors: list[str] = []
    for candidate in runtime_python_candidates(home):
        try:
            path = Path(candidate)
            if not (path.exists() or path.is_symlink()):
                errors.append(f"{candidate}: not executable")
                continue
            proc = subprocess.run([candidate, "-c", script, str(cfg_path)], capture_output=True, text=True, timeout=20)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            continue
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{candidate}: JSON parse failed: {exc}")
                continue
        errors.append(f"{candidate}: {(proc.stderr or proc.stdout or proc.returncode)!s}"[:200])
    raise UnverifiedConfig("config.yaml unverified: no runtime interpreter with PyYAML (" + "; ".join(errors[:4]) + ")")


def resolve_home(raw: str) -> Path:
    home = Path(raw).expanduser().resolve()
    declared_home = os.environ.get("HERMES_HOME", "")
    declared_matches = declared_home and Path(declared_home).expanduser().resolve() == home
    if home.name != ".hermes" and not declared_matches:
        raise SystemExit(f"refusing non-Hermes home: {home}")
    return home


def current_user_for_home(home: Path) -> str | None:
    parent = home.parent
    return parent.name if str(parent).startswith("/home/") else None


def agent_id_for_home(home: Path) -> str:
    try:
        cfg = load_config(home)
    except UnverifiedConfig:
        return current_user_for_home(home) or home.parent.name
    for key in ("agent_id", "client_id", "id"):
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    for key in ("id", "agent_id", "client_id"):
        val = agent_cfg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return current_user_for_home(home) or home.parent.name


def is_enabled_server(val: object) -> bool:
    return not isinstance(val, dict) or val.get("enabled", True)


def server_config(home: Path, server: str) -> dict | None:
    cfg = load_config(home)
    mcp = cfg.get("mcp_servers") or {}
    val = mcp.get(server)
    if not isinstance(val, dict) or not is_enabled_server(val):
        return None
    return val


def enabled_mcp_servers(home: Path) -> dict[str, dict]:
    cfg = load_config(home)
    out: dict[str, dict] = {}
    for name, val in (cfg.get("mcp_servers") or {}).items():
        if isinstance(name, str) and isinstance(val, dict) and is_enabled_server(val):
            out[name] = val
    return out


def required_server_failures(cfg: dict) -> list[dict]:
    """Fail closed when a fleet-wide required MCP is absent or disabled."""
    mcp = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
    rows: list[dict] = []
    for server in REQUIRED_MCP_SERVERS:
        value = mcp.get(server)
        if not isinstance(value, dict):
            reason = "required_server_undeclared"
            declared = False
        elif not is_enabled_server(value):
            reason = "required_server_disabled"
            declared = True
        else:
            continue
        rows.append({
            "server": server,
            "declared": declared,
            "connect": "fail",
            "list_tools": "skip",
            "cheap_call": "skip",
            "duration_ms": 0,
            "signature": reason,
            "signature_class": reason,
            "status": "fail",
            "tools": [],
        })
    return rows


def _plugin_name_key(raw: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def configured_plugins(cfg: dict) -> tuple[list[str], set[str]]:
    plugins_cfg = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    enabled = plugins_cfg.get("enabled", [])
    disabled = plugins_cfg.get("disabled", [])
    enabled_list = [str(item).strip() for item in enabled if str(item).strip()] if isinstance(enabled, list) else []
    disabled_set = (
        {str(item).strip() for item in disabled if str(item).strip()} if isinstance(disabled, list) else set()
    )
    return enabled_list, disabled_set


def _manifest_value(path: Path, key: str) -> str | None:
    try:
        data = load_yaml(path)
    except Exception:
        data = {}
    val = data.get(key) if isinstance(data, dict) else None
    return str(val).strip() if isinstance(val, str) and val.strip() else None


def discover_plugin_manifests(home: Path) -> dict[str, dict]:
    roots = [home / "plugins", home / "hermes-agent" / "plugins"]
    found: dict[str, dict] = {}
    for root in roots:
        if not root.exists():
            continue
        for manifest in sorted(root.rglob("plugin.yaml")):
            try:
                rel = manifest.parent.relative_to(root).as_posix()
            except ValueError:
                rel = manifest.parent.name
            name = _manifest_value(manifest, "name") or manifest.parent.name
            plugin_id = _manifest_value(manifest, "id")
            keys = {rel, manifest.parent.name, name}
            if plugin_id:
                keys.add(plugin_id)
            record = {
                "key": rel,
                "name": name,
                "id": plugin_id,
                "path": str(manifest.parent),
                "source": "user" if root == home / "plugins" else "bundled",
                "aliases": sorted(keys),
            }
            for key in keys:
                found[_plugin_name_key(key)] = record
    return found


def check_plugin_activation(home: Path, cfg: dict) -> dict:
    enabled, disabled = configured_plugins(cfg)
    discovered = discover_plugin_manifests(home)
    disabled_normalized = {_plugin_name_key(item) for item in disabled}
    rows = []
    for raw in enabled:
        match = discovered.get(_plugin_name_key(raw))
        disabled_match = raw in disabled or _plugin_name_key(raw) in disabled_normalized
        if disabled_match:
            rows.append(
                {
                    "plugin": raw,
                    "status": "fail",
                    "configured_enabled": True,
                    "manifest_found": bool(match),
                    "activation": "disabled_conflict",
                    "signature": f"plugin {raw!r} is both enabled and disabled",
                }
            )
        elif match:
            rows.append(
                {
                    "plugin": raw,
                    "status": "pass",
                    "configured_enabled": True,
                    "manifest_found": True,
                    "activation": "configured_and_discovered",
                    "resolved_key": match["key"],
                    "resolved_name": match["name"],
                    "source": match["source"],
                    "path": match["path"],
                }
            )
        else:
            rows.append(
                {
                    "plugin": raw,
                    "status": "fail",
                    "configured_enabled": True,
                    "manifest_found": False,
                    "activation": "enabled_manifest_missing",
                    "signature": (
                        f"plugin {raw!r} is enabled in config.yaml but no plugin.yaml was found under "
                        "~/.hermes/plugins or ~/.hermes/hermes-agent/plugins"
                    ),
                }
            )
    status = "pass" if all(row["status"] == "pass" for row in rows) else "fail"
    if not rows:
        status = "skip"
    return {
        "status": status,
        "enabled_count": len(enabled),
        "discovered_count": len({item["path"] for item in discovered.values()}),
        "plugins": rows,
    }


def dotenv_env(home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home.parent)
    env["HERMES_HOME"] = str(home)
    path = home / ".env"
    if not path.exists():
        return env
    try:
        for raw in path.read_text(errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            env.setdefault(key, value)
    except OSError:
        return env
    return env


_ENV_VAR_RE = re.compile(r"\$(?:\{([^}]+)\}|([A-Za-z_][A-Za-z0-9_]*))")


def expand_env_value(value: object, env: dict, home: Path) -> str:
    raw = str(value or "")

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2) or ""
        return str(env.get(name, match.group(0)))

    def repl_windows(match: re.Match) -> str:
        name = match.group(1) or ""
        return str(env.get(name, match.group(0)))

    expanded = _ENV_VAR_RE.sub(repl, raw)
    expanded = WINDOWS_ENV_VAR_RE.sub(repl_windows, expanded)
    expanded = expanded.replace("~", str(home.parent), 1) if expanded.startswith("~") else expanded
    return expanded


def expanded_env(raw: dict, home: Path) -> dict:
    env = dotenv_env(home)
    for key, value in (raw or {}).items():
        if not isinstance(value, str):
            continue
        env[key] = expand_env_value(value, env, home)
    return env


def is_remote_mcp(cfg: dict) -> bool:
    transport = str(cfg.get("transport") or "").lower()
    return bool(cfg.get("url")) or transport in REMOTE_MCP_TRANSPORTS


def _redact_secret_values(text: str, env: dict) -> str:
    out = text or ""
    for key, value in env.items():
        if not isinstance(value, str) or len(value) < 8:
            continue
        if not re.search(r"(TOKEN|KEY|SECRET|PASSWORD|BEARER)", key, re.I):
            continue
        out = out.replace(value, "[REDACTED]")
    return out


def _snippet(text: str, env: dict, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    return _redact_secret_values(compact[:limit], env)


def _parse_json_or_sse(body: str) -> dict | None:
    stripped = (body or "").strip()
    if not stripped:
        return None
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line.split(":", 1)[1].strip()
        if not data or data == "[DONE]":
            continue
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            continue
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _post_json_preserve_redirects(url: str, headers: dict, payload: dict, timeout: float) -> tuple[int, dict, str]:
    body = json.dumps(payload).encode("utf-8")
    current = url
    merged_headers = {
        **headers,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    for _ in range(5):
        req = urllib.request.Request(current, data=body, headers=merged_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status), dict(resp.headers.items()), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in {307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    return int(exc.code), dict(exc.headers.items()), exc.read().decode("utf-8", "replace")
                current = urllib.parse.urljoin(current, location)
                continue
            return int(exc.code), dict(exc.headers.items()), exc.read().decode("utf-8", "replace")
    raise RuntimeError("too_many_redirects")


def probe_remote_http_mcp(home: Path, server: str, cfg: dict, timeout: int) -> dict:
    started = time.monotonic()
    env = expanded_env(cfg.get("env") or {}, home)
    url = expand_env_value(cfg.get("url") or "", env, home)
    transport = str(cfg.get("transport") or "url").lower()
    headers = {}
    for key, value in (cfg.get("headers") or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            headers[key] = expand_env_value(value, env, home)
    if not url:
        return {
            "status": "fail",
            "connect": "fail",
            "list_tools": "skip",
            "signature": "remote_mcp_missing_url",
            "signature_class": "remote_mcp_unreachable",
            "tools": [],
            "transport": transport,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tool-doctor", "version": TOOL_DOCTOR_VERSION},
            },
        }
        code, response_headers, body = _post_json_preserve_redirects(url, headers, init, timeout)
        payload = _parse_json_or_sse(body)
        if code >= 400:
            return {
                "status": "fail",
                "connect": "fail",
                "list_tools": "skip",
                "signature": f"remote_mcp_http_{code}: {_snippet(body, env)}",
                "signature_class": "remote_mcp_unreachable",
                "tools": [],
                "transport": transport,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        if not isinstance(payload, dict) or "result" not in payload:
            return {
                "status": "fail",
                "connect": "fail",
                "list_tools": "skip",
                "signature": f"remote_mcp_no_initialize_result: {_snippet(body, env)}",
                "signature_class": "remote_mcp_unreachable",
                "tools": [],
                "transport": transport,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        session_id = response_headers.get("mcp-session-id") or response_headers.get("Mcp-Session-Id")
        request_headers = dict(headers)
        if session_id:
            request_headers["Mcp-Session-Id"] = session_id
        _post_json_preserve_redirects(
            url,
            request_headers,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout,
        )
        code, _, body = _post_json_preserve_redirects(
            url,
            request_headers,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            timeout,
        )
        payload = _parse_json_or_sse(body)
        if code >= 400:
            return {
                "status": "fail",
                "connect": "pass",
                "list_tools": "fail",
                "signature": f"remote_mcp_http_{code}: {_snippet(body, env)}",
                "signature_class": "remote_mcp_unreachable",
                "tools": [],
                "transport": transport,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        if not isinstance(payload, dict) or "result" not in payload:
            return {
                "status": "fail",
                "connect": "pass",
                "list_tools": "fail",
                "signature": f"remote_mcp_no_tools_result: {_snippet(body, env)}",
                "signature_class": "remote_mcp_unreachable",
                "tools": [],
                "transport": transport,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        tools = payload.get("result", {}).get("tools", [])
        return {
            "status": "pass",
            "connect": "pass",
            "list_tools": "pass",
            "cheap_call": "skip",
            "signature": "",
            "tools_count": len(tools),
            "tools": [t.get("name") for t in tools[:20]],
            "transport": transport,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except TimeoutError:
        signature = "remote_mcp_timeout"
    except Exception as exc:  # noqa: BLE001
        signature = f"remote_mcp_error: {_snippet(f'{type(exc).__name__}: {exc}', env)}"
    return {
        "status": "fail",
        "connect": "fail",
        "list_tools": "skip",
        "signature": signature,
        "signature_class": "remote_mcp_unreachable",
        "tools": [],
        "transport": transport,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def resolve_command(command: str, *, env: dict | None = None, home: Path | None = None) -> str | None:
    raw = str(command or "")
    merged_env = dict(os.environ)
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items() if isinstance(k, str)})
    expanded = expand_env_value(raw, merged_env, home) if home is not None else raw
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    if expanded and Path(expanded).exists():
        return expanded
    resolved = shutil.which(expanded, path=merged_env.get("PATH"))
    if resolved:
        return resolved
    if os.name == "nt" and expanded and not os.path.splitext(expanded)[1]:
        for suffix in (".exe", ".cmd", ".bat", ".ps1"):
            resolved = shutil.which(expanded + suffix, path=merged_env.get("PATH"))
            if resolved:
                return resolved
    return None


def command_detail(cfg: dict, home: Path) -> dict:
    env = expanded_env(cfg.get("env") or {}, home)
    command = expand_env_value(cfg.get("command") or "", env, home)
    command = os.path.expandvars(os.path.expanduser(command))
    args = [expand_env_value(a, env, home) for a in (cfg.get("args") or [])]
    resolved = resolve_command(command, env=env, home=home)
    return {
        "command": command,
        "args": args,
        "command_resolved": bool(resolved),
        "resolved_command": resolved,
    }


def command_venv_dir(command: str) -> str | None:
    if "/venv/bin/" in command:
        return command.split("/venv/bin/", 1)[0] + "/venv"
    if "/.venv/bin/" in command:
        return command.split("/.venv/bin/", 1)[0] + "/.venv"
    lower = command.lower()
    if "\\venv\\scripts\\" in lower:
        idx = lower.index("\\venv\\scripts\\")
        return command[:idx] + "\\venv"
    if "\\.venv\\scripts\\" in lower:
        idx = lower.index("\\.venv\\scripts\\")
        return command[:idx] + "\\.venv"
    return None


def candidate_module_launch_issue(command: str, args: list[str]) -> dict | None:
    """Fail closed for a legacy module missing from its declared candidate.

    A runtime candidate's Python must not be made to appear healthy by a
    config-level PYTHONPATH that points at a different, stale checkout.  This
    check is intentionally limited to the retired ``workers.*`` launch shape.
    Current clean-room MCP modules own their declared source path and are not
    inferred from the candidate tree.
    """
    try:
        module_index = args.index("-m")
        module = str(args[module_index + 1])
    except (ValueError, IndexError):
        return None
    if not module.startswith("workers."):
        return None
    executable = Path(command)
    parts = executable.parts
    try:
        candidate_index = parts.index("runtime-candidates")
    except ValueError:
        return None
    if len(parts) <= candidate_index + 1:
        return None
    candidate_root = Path(*parts[: candidate_index + 2])
    expected = candidate_root.joinpath(*module.split(".")).with_suffix(".py")
    if expected.is_file():
        return None
    return {
        "module": module,
        "candidate_root": str(candidate_root),
        "expected_module": str(expected),
    }


def _readline_with_timeout(stream, timeout: float) -> str | None:
    if os.name != "nt":
        ready, _, _ = select.select([stream], [], [], timeout)
        if not ready:
            return None
        return stream.readline()
    q: queue.Queue[str] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            q.put(stream.readline())
        except Exception:
            q.put("")

    threading.Thread(target=worker, daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


def stale_tree_reference(home: Path, command: str, resolved: str | None) -> dict | None:
    checks: list[dict] = []
    active_tree = home / "hermes-agent"
    active_tree_realpath = str(active_tree.resolve()) if active_tree.exists() or active_tree.is_symlink() else None

    for label, raw in (("command", resolved or command), ("venv", command_venv_dir(command) or "")):
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not (path.exists() or path.is_symlink()):
            continue
        realpath = str(path.resolve())
        reasons: list[str] = []
        if STALE_TREE_RE.search(realpath):
            reasons.append("stale_pattern_realpath")
        if reasons:
            checks.append({"kind": label, "path": str(path), "realpath": realpath, "reasons": reasons})
    if not checks:
        return None
    return {
        "signature_class": "stale_tree_reference",
        "home": str(home),
        "active_tree": str(active_tree),
        "active_tree_realpath": active_tree_realpath,
        "references": checks,
    }


def read_jsonrpc(proc: subprocess.Popen, wanted_id: int, timeout: float) -> tuple[dict | None, str | None]:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    last_non_json = ""
    while time.monotonic() < deadline:
        raw = _readline_with_timeout(proc.stdout, max(0.1, deadline - time.monotonic()))
        if raw is None:
            continue
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            last_non_json = line[:300]
            continue
        if payload.get("id") == wanted_id:
            return payload, None
        if payload.get("error"):
            return payload, None
    return None, last_non_json or "timeout"


def call_tool(proc: subprocess.Popen, call_id: int, name: str, arguments: dict, timeout: float) -> dict:
    assert proc.stdin is not None
    started = time.monotonic()
    request = {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    payload, read_error = read_jsonrpc(proc, call_id, timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    if payload is None:
        return {"status": "fail", "duration_ms": duration_ms, "signature": f"cheap_call_no_response: {read_error}"}
    if payload.get("error"):
        return {"status": "fail", "duration_ms": duration_ms, "signature": str(payload.get("error"))[:300]}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"status": "fail", "duration_ms": duration_ms, "signature": "tool_call_missing_result"}
    if result.get("isError") is True:
        detail = result.get("content") or result
        return {"status": "fail", "duration_ms": duration_ms, "signature": str(detail)[:300]}
    return {"status": "pass", "duration_ms": duration_ms, "tool": name}


def cheap_call_config(cfg: dict, server: str) -> dict | None:
    raw = cfg.get("tool_doctor") or cfg.get("tool_health") or {}
    calls = raw.get("cheap_calls") if isinstance(raw, dict) else {}
    candidate = None
    if isinstance(calls, dict):
        candidate = calls.get(server)
    if candidate is None:
        metadata = cfg.get("metadata") if isinstance(cfg.get("metadata"), dict) else {}
        candidate = metadata.get("tool_doctor_cheap_call") or cfg.get("cheap_call")
    if not isinstance(candidate, dict) or not candidate.get("name"):
        return None
    arguments = candidate.get("arguments") if isinstance(candidate.get("arguments"), dict) else {}
    return {"name": str(candidate["name"]), "arguments": arguments}


def safe_probe_config(cfg: dict, server: str) -> tuple[dict | None, str]:
    """Return a declared no-side-effect probe, or the one mandated router probe."""
    health = cfg.get("health") if isinstance(cfg.get("health"), dict) else {}
    candidate = health.get("safe_probe") or cfg.get("tool_doctor_safe_probe")
    if candidate is None:
        candidate = BUILTIN_SAFE_PROBES.get(server)
    if not isinstance(candidate, dict) or not candidate.get("name"):
        return None, "no_declared_safe_probe"
    read_only = candidate.get("read_only", health.get("read_only"))
    side_effects = candidate.get("side_effects", health.get("side_effects"))
    if read_only is not True or side_effects is not False:
        return None, "safe_probe_contract_unconfirmed"
    arguments = candidate.get("arguments") if isinstance(candidate.get("arguments"), dict) else {}
    return {
        "name": str(candidate["name"]),
        "arguments": arguments,
        "read_only": True,
        "side_effects": False,
        "source": str(candidate.get("source") or "connector_config"),
    }, ""


def mcp_list_tools(
    home: Path,
    server: str,
    timeout: int = 15,
    cheap_call: bool = True,
    probe_call: dict | None = None,
) -> dict:
    started = time.monotonic()
    timeout = server_probe_timeout(server, timeout)
    base = {
        "declared": False,
        "connect": "skip",
        "list_tools": "skip",
        "cheap_call": "skip",
        "duration_ms": 0,
    }
    try:
        cfg = server_config(home, server)
    except UnverifiedConfig as exc:
        return {
            **base,
            "declared": False,
            "status": "unverified",
            "connect": "unverified",
            "signature": str(exc)[:300],
            "signature_class": "stale_refs_unverified",
            "needs_you": True,
            "tools": [],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    base["declared"] = bool(cfg)
    if not cfg:
        return {**base, "status": "skip", "reason": "server_not_declared", "tools": []}
    if is_remote_mcp(cfg):
        return {**base, **probe_remote_http_mcp(home, server, cfg, timeout)}
    detail = command_detail(cfg, home)
    command = detail["command"]
    args = detail["args"]
    resolved = detail["resolved_command"]
    base.update({k: v for k, v in detail.items() if k != "args"})
    stale_ref = stale_tree_reference(home, command, resolved)
    if stale_ref:
        signature = "stale_tree_reference: " + "; ".join(
            f"{item['kind']} {item['path']} -> {item['realpath']} ({','.join(item['reasons'])})"
            for item in stale_ref["references"]
        )
        classification = classify_signature(server, signature, command=command, cwd=str(home))
        return {
            **base,
            "status": "fail",
            "connect": "fail",
            "signature": signature[:300],
            "signature_class": "stale_tree_reference",
            "needs_you": True,
            "escalation": classification.get("escalation"),
            "stale_tree_reference": stale_ref,
            "tools": [],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    if not resolved:
        return {
            **base,
            "status": "fail",
            "connect": "fail",
            "signature": f"missing executable '{command}'",
            "tools": [],
        }
    module_issue = candidate_module_launch_issue(resolved, args)
    if module_issue:
        signature = f"live_launch_context_missing_module: {module_issue['module']}"
        classification = classify_signature(server, signature, command=command, cwd=str(home))
        return {
            **base,
            "status": "fail",
            "connect": "fail",
            "signature": signature,
            "signature_class": "live_launch_context_missing_module",
            "needs_you": False,
            "launch_context": module_issue,
            "escalation": classification.get("escalation"),
            "tools": [],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    proc = None
    try:
        probe_env = expanded_env(cfg.get("env") or {}, home)
        if Path(command).is_absolute():
            sibling_dir = str(Path(resolved).parent)
            current_path = str(probe_env.get("PATH") or "")
            path_parts = current_path.split(os.pathsep) if current_path else []
            if sibling_dir not in path_parts:
                probe_env["PATH"] = os.pathsep.join([sibling_dir, *path_parts])
        proc = subprocess.Popen(
            [resolved, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(home.parent),
            env=probe_env,
            bufsize=1,
            **mcp_process_group_kwargs(),
        )
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tool-doctor", "version": "phase1"},
            },
        }
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(init) + "\n")
        proc.stdin.flush()
        payload1, read_error = read_jsonrpc(proc, 1, timeout)
        if not payload1:
            reason = "initialize_no_response" if read_error == "timeout" else str(read_error)
            return failure_from_proc(proc, reason, base, started)
        if payload1.get("error"):
            return {
                **base,
                "status": "fail",
                "connect": "fail",
                "signature": str(payload1.get("error"))[:300],
                "tools": [],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        base["connect"] = "pass"
        server_info = payload1.get("result", {}).get("serverInfo", {})
        if isinstance(server_info, dict):
            base["server_version"] = str(server_info.get("version") or "")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
        proc.stdin.flush()
        payload, read_error = read_jsonrpc(proc, 2, timeout)
        if not payload:
            reason = "tools_list_no_response" if read_error == "timeout" else str(read_error)
            return failure_from_proc(proc, reason, base, started)
        if payload.get("error"):
            return {
                **base,
                "status": "fail",
                "list_tools": "fail",
                "signature": str(payload.get("error"))[:300],
                "tools": [],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        tools = payload.get("result", {}).get("tools", [])
        base["list_tools"] = "pass"
        cheap = probe_call if probe_call is not None else (cheap_call_config(cfg, server) if cheap_call else None)
        cheap_result: dict | str = "skip"
        if cheap:
            cheap_result = call_tool(proc, 3, cheap["name"], cheap["arguments"], min(timeout, 10))
            base["cheap_call"] = cheap_result.get("status", "fail")
        return {
            **base,
            "status": "pass" if base["cheap_call"] in {"skip", "pass"} else "fail",
            "signature": ""
            if base["cheap_call"] in {"skip", "pass"}
            else cheap_result.get("signature", "cheap_call_failed"),
            "tools_count": len(tools),
            "tools": [t.get("name") for t in tools[:20]],
            "cheap_call_result": cheap_result,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except FileNotFoundError:
        return {
            **base,
            "status": "fail",
            "connect": "fail",
            "signature": f"missing executable '{command}'",
            "tools": [],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            **base,
            "status": "fail",
            "signature": str(exc)[:300],
            "tools": [],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if proc is not None:
            terminate(proc)


def _sanitized_config_fingerprint(cfg: dict) -> str:
    safe = {
        "command": cfg.get("command"),
        "args": cfg.get("args"),
        "transport": cfg.get("transport"),
        "url_host": urllib.parse.urlparse(str(cfg.get("url") or "")).netloc,
        "enabled": cfg.get("enabled", True),
        "env_keys": sorted((cfg.get("env") or {}).keys()) if isinstance(cfg.get("env"), dict) else [],
        "header_keys": sorted((cfg.get("headers") or {}).keys()) if isinstance(cfg.get("headers"), dict) else [],
        "health": cfg.get("health") if isinstance(cfg.get("health"), dict) else {},
    }
    raw = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def classify_live_result(row: dict) -> dict:
    signature = str(row.get("signature") or "")
    lowered = signature.lower()
    if row.get("status") == "pass" and row.get("cheap_call") == "pass":
        return {"health_state": "healthy", "error_class": "none", "transient": False}
    if is_stale_session_signature(lowered):
        return {"health_state": "stale_session", "error_class": "stale_session", "transient": True}
    timeout = "timeout" in lowered or "timed out" in lowered or "no_response" in lowered
    if timeout:
        state = "degraded" if row.get("connect") == "pass" and row.get("list_tools") == "pass" else "unreachable"
        return {"health_state": state, "error_class": "timeout", "transient": True}
    if is_auth_signature(signature) or "missing executable" in lowered or "invalid schema" in lowered:
        return {"health_state": "misconfigured", "error_class": "misconfigured", "transient": False}
    if row.get("connect") == "pass" and row.get("list_tools") == "pass":
        return {"health_state": "degraded", "error_class": "invocation_failed", "transient": False}
    return {"health_state": "unreachable", "error_class": "unreachable", "transient": False}


def _live_attempt(route: str, probe: dict, attempt: int, row: dict) -> dict:
    classified = classify_live_result(row)
    return {
        "route": route,
        "probe": probe["name"],
        "attempt": attempt,
        "status": "pass" if classified["health_state"] == "healthy" else "fail",
        "health_state": classified["health_state"],
        "error_class": classified["error_class"],
        "transient": classified["transient"],
        "latency_ms": int(row.get("duration_ms") or 0),
        "signature": str(row.get("signature") or "")[:300],
        "connect": row.get("connect", "skip"),
        "discovery": row.get("list_tools", "skip"),
        "invocation": row.get("cheap_call", "skip"),
    }


def live_capability_probe(
    home: Path,
    route: str = "capability-router",
    *,
    timeout: int = 15,
    allow_recovery: bool = True,
) -> dict:
    generated_at = utcnow()
    try:
        cfg = load_config(home)
    except UnverifiedConfig as exc:
        return {
            "schema_version": LIVE_PROBE_SCHEMA_VERSION,
            "generated_at": generated_at,
            "route": route,
            "health_state": "misconfigured",
            "verdict": "MISCONFIGURED",
            "error_class": "config_unverified",
            "error": str(exc)[:300],
            "attempts": [],
            "recovery_action": "none",
            "retry_result": "not_attempted",
            "fingerprint": {"tool_doctor_version": TOOL_DOCTOR_VERSION, "config": "unverified", "route_version": ""},
            "side_effects_possible": False,
            "side_effects_attempted": False,
        }
    raw = (cfg.get("mcp_servers") or {}).get(route)
    if isinstance(raw, dict) and raw.get("enabled", True) is False:
        return {
            "schema_version": LIVE_PROBE_SCHEMA_VERSION,
            "generated_at": generated_at,
            "route": route,
            "probe": None,
            "health_state": "disabled",
            "verdict": "DISABLED",
            "error_class": "disabled",
            "attempts": [],
            "recovery_action": "none",
            "retry_result": "not_attempted",
            "fingerprint": {
                "tool_doctor_version": TOOL_DOCTOR_VERSION,
                "config": _sanitized_config_fingerprint(raw),
                "route_version": "",
            },
            "side_effects_possible": False,
            "side_effects_attempted": False,
        }
    if not isinstance(raw, dict):
        raw = {}
    probe_call, reason = safe_probe_config(raw, route)
    if probe_call is None:
        return {
            "schema_version": LIVE_PROBE_SCHEMA_VERSION,
            "generated_at": generated_at,
            "route": route,
            "probe": None,
            "health_state": "unknown",
            "verdict": "UNKNOWN",
            "error_class": reason,
            "attempts": [],
            "recovery_action": "none",
            "retry_result": "not_attempted",
            "fingerprint": {
                "tool_doctor_version": TOOL_DOCTOR_VERSION,
                "config": _sanitized_config_fingerprint(raw),
                "route_version": "",
            },
            "side_effects_possible": False,
            "side_effects_attempted": False,
        }

    first_row = mcp_list_tools(home, route, timeout=timeout, cheap_call=True, probe_call=probe_call)
    first = _live_attempt(route, probe_call, 1, first_row)
    attempts = [first]
    recovery_action = "none"
    retry_result = "not_attempted"
    final = first
    if first["status"] == "fail" and first["transient"] and allow_recovery:
        recovery_action = "restart_doctor_owned_mcp_session"
        retry_row = mcp_list_tools(home, route, timeout=timeout, cheap_call=True, probe_call=probe_call)
        retry = _live_attempt(route, probe_call, 2, retry_row)
        attempts.append(retry)
        final = retry
        retry_result = "pass" if retry["status"] == "pass" else "fail"

    recovered = first["status"] == "fail" and final["status"] == "pass"
    health_state = "degraded" if recovered else final["health_state"]
    verdict = "RECOVERED" if recovered else health_state.upper()
    route_version = str((first_row if len(attempts) == 1 else retry_row).get("server_version") or "")
    out = {
        "schema_version": LIVE_PROBE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "route": route,
        "probe": probe_call["name"],
        "probe_source": probe_call["source"],
        "health_state": health_state,
        "verdict": verdict,
        "error_class": first["error_class"] if recovered else final["error_class"],
        "attempts": attempts,
        "first_failure": first if first["status"] == "fail" else None,
        "recovery_action": recovery_action,
        "retry_result": retry_result,
        "fingerprint": {
            "tool_doctor_version": TOOL_DOCTOR_VERSION,
            "config": _sanitized_config_fingerprint(raw),
            "route_version": route_version,
        },
        "side_effects_possible": False,
        "side_effects_attempted": False,
    }
    out["operator_summary"] = render_live_summary(out)
    return out


def render_live_summary(out: dict) -> str:
    attempts = out.get("attempts") or []
    attempt_text = "; ".join(
        f"attempt {row['attempt']}={row['status'].upper()} {row['error_class']} {row['latency_ms']}ms"
        for row in attempts
    ) or "attempts=none"
    fp = out.get("fingerprint") or {}
    return (
        f"Tool Doctor — {out.get('generated_at', '')}\n"
        f"route={out.get('route')} probe={out.get('probe') or 'none'} {attempt_text}\n"
        f"recovery={out.get('recovery_action')} retry={out.get('retry_result')} "
        f"verdict={out.get('verdict')} health={out.get('health_state')}\n"
        f"fingerprint={fp.get('config', '')}/{fp.get('route_version', '') or 'unknown'} "
        f"side_effects_possible={'yes' if out.get('side_effects_possible') else 'no'} "
        f"side_effects_attempted={'yes' if out.get('side_effects_attempted') else 'no'}"
    )


def read_line(proc: subprocess.Popen, timeout: int) -> str:
    assert proc.stdout is not None
    raw = _readline_with_timeout(proc.stdout, timeout)
    if raw is None:
        return ""
    return raw.strip()


def failure_from_proc(
    proc: subprocess.Popen,
    fallback: str,
    base: dict | None = None,
    started: float | None = None,
) -> dict:
    err = ""
    try:
        terminate(proc)
        if proc.stderr:
            err = bounded_stream_read(proc.stderr, 1000)
    except Exception:
        pass
    sig = stderr_tail_signature(err, fallback)
    if "PGLite failed" in err:
        sig = "PGLite failed to initialize its WASM runtime"
    out = dict(base or {})
    if out.get("connect") != "pass":
        out["connect"] = "fail"
    elif out.get("list_tools") != "pass":
        out["list_tools"] = "fail"
    out.update({"status": "fail", "signature": sig[:300], "tools": []})
    if started is not None:
        out["duration_ms"] = int((time.monotonic() - started) * 1000)
    return out


def stderr_tail_signature(stderr: str, fallback: str, *, max_lines: int = 3, max_chars: int = 500) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return fallback
    tail = "\n".join(lines[-max_lines:])
    return tail[-max_chars:]


def server_probe_timeout(server: str, timeout: int) -> int:
    """Provider startup is slow; all other MCP probes retain their existing bound."""
    return max(timeout, 15) if server.startswith(("composio-", "maton-")) else timeout


def mcp_process_group_kwargs(platform: str | None = None) -> dict:
    platform = platform or os.name
    if platform == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def bounded_stream_read(stream, limit: int, timeout: float = 0.25) -> str:
    """Read diagnostics without letting an inherited pipe defeat probe bounds."""
    result: list[str] = []

    def reader() -> None:
        try:
            result.append(stream.read(limit) or "")
        except Exception:
            result.append("")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    return result[0] if result else ""


def terminate(proc: subprocess.Popen, platform: str | None = None) -> None:
    """Terminate the isolated MCP process tree, including pipe-holding descendants."""
    platform = platform or os.name
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    if platform == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
        return

    process_group = proc.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            break
        time.sleep(0.02)
    else:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def load_recipe_catalog() -> dict:
    return load_yaml(RECIPE_PATH).get("recipes") or {}


def recipe_supports_server(recipe: dict, server: str) -> bool:
    servers = recipe.get("servers")
    return isinstance(servers, list) and server in {str(item) for item in servers}


def recipe_for_missing_executable(server: str, signature: str) -> str | None:
    if "missing executable" not in (signature or ""):
        return None
    recipes = load_recipe_catalog()
    for recipe_id, recipe in recipes.items():
        if recipe.get("signature") == "missing executable" and recipe_supports_server(recipe, server):
            return str(recipe_id)
    if server in {"browser", "browser-lane"}:
        return "browser-lane-missing-venv"
    return None


def is_auth_signature(signature: str) -> bool:
    auth_pattern = r"\b(401|403|unauthorized|forbidden|oauth|token|auth(?:entication|orization)?)\b"
    return bool(re.search(auth_pattern, signature or "", re.I))


def is_stale_session_signature(signature: str) -> bool:
    lowered = (signature or "").lower()
    return any(
        marker in lowered
        for marker in (
            "closedresourceerror",
            "connection closed",
            "closed session",
            "session is closed",
            "rpc session closed",
            "channel closed",
            "broken pipe",
            "connection reset",
        )
    )


def redacted_evidence(
    signature: str,
    *,
    command: str | None = None,
    cwd: str | None = None,
    env_key_names: list[str] | None = None,
) -> dict:
    evidence = {"signature": (signature or "")[:500]}
    if command:
        evidence["command"] = command
    if cwd:
        evidence["cwd"] = cwd
    if env_key_names:
        evidence["env_key_names"] = sorted({str(name) for name in env_key_names if name})
    return evidence


def escalation_object(
    signature_class: str,
    reason: str,
    *,
    runbook: str | None = None,
    first_seen: str | None = None,
    attempt_count_24h: int = 0,
    failed_twice: bool = False,
    evidence: dict | None = None,
) -> dict:
    out = {
        "status": "escalated",
        "reason": reason,
        "runbook": runbook or ESCALATION_RUNBOOKS.get(signature_class, ESCALATION_RUNBOOKS["unknown"]),
        "first_seen": first_seen or utcnow(),
        "attempt_count_24h": int(attempt_count_24h),
        "failed_twice": bool(failed_twice),
    }
    if evidence:
        out["evidence"] = evidence
    return out


def classify_signature(
    server: str,
    signature: str,
    *,
    command: str | None = None,
    cwd: str | None = None,
    env_key_names: list[str] | None = None,
) -> dict:
    sig = signature or ""
    evidence = redacted_evidence(sig, command=command, cwd=cwd, env_key_names=env_key_names)
    if sig in CALL_INCIDENT_SIGNATURES:
        return {
            "server": server,
            "signature": sig,
            "signature_class": sig,
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": f"{sig}_requires_operator_reconciliation",
            "runbook": "operator-mcp-call-recovery-reconciliation",
            "evidence": evidence,
            "escalation": escalation_object(
                sig,
                f"{sig}_requires_operator_reconciliation",
                runbook="operator-mcp-call-recovery-reconciliation",
                evidence=evidence,
            ),
        }
    if sig in {"required_server_undeclared", "required_server_disabled"}:
        return {
            "server": server,
            "signature": sig,
            "signature_class": sig,
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": f"{sig}_requires_canonical_mcp_config_repair",
            "runbook": ESCALATION_RUNBOOKS[sig],
            "evidence": evidence,
            "escalation": escalation_object(
                sig,
                f"{sig}_requires_canonical_mcp_config_repair",
                evidence=evidence,
            ),
        }
    if sig.startswith("live_launch_context_missing_module:"):
        return {
            "server": server,
            "signature": sig,
            "signature_class": "live_launch_context_missing_module",
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": "declared_candidate_missing_legacy_module_requires_canonical_mcp_config_repair",
            "runbook": "operator-required-mcp-config-repair",
            "evidence": evidence,
            "escalation": escalation_object(
                "live_launch_context_missing_module",
                "declared_candidate_missing_legacy_module_requires_canonical_mcp_config_repair",
                runbook="operator-required-mcp-config-repair",
                evidence=evidence,
            ),
        }
    if "stale_tree_reference" in sig:
        esc = escalation_object(
            "stale_tree_reference",
            "live_reference_resolves_outside_active_tree",
            evidence=evidence,
        )
        esc["needs_you"] = True
        return {
            "server": server,
            "signature": sig,
            "signature_class": "stale_tree_reference",
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": "live_reference_resolves_outside_active_tree",
            "runbook": ESCALATION_RUNBOOKS["stale_tree_reference"],
            "needs_you": True,
            "evidence": evidence,
            "escalation": esc,
        }
    if "PGLite failed to initialize its WASM runtime" in sig:
        return {
            "server": server,
            "signature": sig,
            "signature_class": "pglite_wasm_runtime",
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": "engine_or_migration_level_gbrain_pglite_wasm_failure",
            "runbook": ESCALATION_RUNBOOKS["pglite_wasm_runtime"],
            "evidence": evidence,
            "escalation": escalation_object(
                "pglite_wasm_runtime",
                "engine_or_migration_level_gbrain_pglite_wasm_failure",
                evidence=evidence,
            ),
        }
    if is_stale_session_signature(sig):
        return {
            "server": server,
            "signature": sig,
            "signature_class": "stale_session",
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": "closed_mcp_or_rpc_session_requires_live_restart_retry",
            "runbook": ESCALATION_RUNBOOKS["stale_session"],
            "evidence": evidence,
            "escalation": escalation_object(
                "stale_session",
                "closed_mcp_or_rpc_session_requires_live_restart_retry",
                evidence=evidence,
            ),
        }
    if is_auth_signature(sig):
        return {
            "server": server,
            "signature": sig,
            "signature_class": "auth_or_token",
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": "auth_or_token_failures_require_operator_repair",
            "runbook": ESCALATION_RUNBOOKS["auth_or_token"],
            "evidence": evidence,
            "escalation": escalation_object(
                "auth_or_token",
                "auth_or_token_failures_require_operator_repair",
                evidence=evidence,
            ),
        }
    dependency_classes = (
        ("ModuleNotFoundError: No module named 'mcp'", "missing_python_dependency_mcp"),
        ("ModuleNotFoundError: No module named 'cv2'", "missing_python_dependency_cv2"),
        ("ModuleNotFoundError: No module named 'workers'", "missing_python_dependency_workers"),
        ("env: node: No such file or directory", "missing_node_runtime"),
    )
    for marker, signature_class in dependency_classes:
        if marker in sig:
            return {
                "server": server, "signature": sig, "signature_class": signature_class,
                "action": "escalate", "recipe_id": None, "repairable": False,
                "reason": f"{signature_class}_requires_canonical_runtime_repair",
                "runbook": ESCALATION_RUNBOOKS["unknown"], "evidence": evidence,
                "escalation": escalation_object(
                    signature_class,
                    f"{signature_class}_requires_canonical_runtime_repair",
                    evidence=evidence,
                ),
            }
    if sig.startswith("remote_mcp_"):
        return {
            "server": server,
            "signature": sig,
            "signature_class": "remote_mcp_unreachable",
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": "remote_mcp_connectivity_or_protocol_failure_requires_operator_review",
            "runbook": ESCALATION_RUNBOOKS["remote_mcp_unreachable"],
            "evidence": evidence,
            "escalation": escalation_object(
                "remote_mcp_unreachable",
                "remote_mcp_connectivity_or_protocol_failure_requires_operator_review",
                evidence=evidence,
            ),
        }
    if "missing executable" in sig:
        recipe_id = recipe_for_missing_executable(server, sig)
        if recipe_id:
            return {
                "server": server,
                "signature": sig,
                "signature_class": "missing_executable_declared_mcp_venv",
                "action": "repair",
                "recipe_id": recipe_id,
                "repairable": True,
                "reason": "missing_executable_under_declared_mcp_venv",
                "runbook": "tool-doctor-recipe-repair",
                "evidence": evidence,
            }
        return {
            "server": server,
            "signature": sig,
            "signature_class": "missing_executable_no_recipe",
            "action": "escalate",
            "recipe_id": None,
            "repairable": False,
            "reason": "missing_executable_has_no_server_recipe",
            "runbook": ESCALATION_RUNBOOKS["no_recipe"],
            "evidence": evidence,
            "escalation": escalation_object(
                "no_recipe",
                "missing_executable_has_no_server_recipe",
                evidence=evidence,
            ),
        }
    return {
        "server": server,
        "signature": sig,
        "signature_class": "unknown",
        "action": "escalate",
        "recipe_id": None,
        "repairable": False,
        "reason": "unknown_signature_requires_operator_review",
        "runbook": ESCALATION_RUNBOOKS["unknown"],
        "evidence": evidence,
        "escalation": escalation_object(
            "unknown",
            "unknown_signature_requires_operator_review",
            evidence=evidence,
        ),
    }


def classify(server: str, signature: str) -> str:
    detail = classify_signature(server, signature)
    return detail.get("recipe_id") or f"escalate-{detail['signature_class']}"


def ensure_safe_write(path: Path, home: Path) -> None:
    resolved = path.resolve()
    if not str(resolved).startswith(str(home.parent.resolve())):
        raise RuntimeError(f"refusing cross-home write: {path}")
    if any(part in DENY_NAMES for part in path.parts):
        raise RuntimeError(f"refusing secret/auth path: {path}")


def backup_path(path: Path) -> Path | None:
    if not path.exists():
        return None
    bak = Path(str(path) + STAMP_SUFFIX)
    if bak.exists():
        return bak
    if path.is_dir():
        shutil.copytree(path, bak, symlinks=True)
    else:
        shutil.copy2(path, bak)
    return bak


def repair_browser_lane(home: Path, dry_run: bool) -> tuple[str, list[str]]:
    target = home / "repos/browser-lane"
    venv = target / ".venv"
    source = Path(os.environ.get("TOOL_DOCTOR_BROWSER_LANE_SOURCE", "/tmp/hermes-browser-lane-source-phase0"))
    if not source.exists():
        return "escalated: source_missing", []
    touches = [str(venv)]
    if dry_run:
        return "would_create_browser_lane_venv", touches
    ensure_safe_write(target, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git"))
        touches.append(str(target))
    backup_path(venv)
    if not venv.exists():
        subprocess.run(["python3", "-m", "venv", str(venv)], check=True, timeout=120)
    subprocess.run([str(venv / "bin/pip"), "install", "-q", "--upgrade", "pip"], check=True, timeout=180)
    subprocess.run([str(venv / "bin/pip"), "install", "-q", "-e", "."], cwd=str(target), check=True, timeout=240)
    return "repaired", touches


def receipt_path(home: Path, override: str | None) -> Path:
    return Path(override).expanduser() if override else home / "state/tool-doctor-receipts.jsonl"


def budget_path(home: Path, override: str | None = None) -> Path:
    return Path(override).expanduser() if override else home / BUDGET_PATH


def load_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    try:
        return json.loads(path.read_text())
    except Exception:
        return dict(default or {})


def budget_key(agent_id: str, server: str, signature_class: str) -> str:
    return "|".join([agent_id, server, signature_class])


def reserve_daily_budget(
    home: Path,
    *,
    agent_id: str,
    server: str,
    signature_class: str,
    path_override: str | None = None,
) -> tuple[bool, dict]:
    path = budget_path(home, path_override)
    data = load_json(path, {"version": 1, "attempts": {}})
    data.setdefault("version", 1)
    attempts = data.setdefault("attempts", {})
    day = attempts.setdefault(today_key(), {})
    key = budget_key(agent_id, server, signature_class)
    now = utcnow()
    entry = day.setdefault(
        key,
        {
            "date": today_key(),
            "agent_id": agent_id,
            "server": server,
            "signature_class": signature_class,
            "first_seen": now,
            "seen_count": 0,
            "repair_attempt_count": 0,
        },
    )
    entry["seen_count"] = int(entry.get("seen_count") or 0) + 1
    entry["last_seen"] = now
    allowed = int(entry.get("repair_attempt_count") or 0) < 1
    if allowed:
        entry["repair_attempt_count"] = int(entry.get("repair_attempt_count") or 0) + 1
        entry["last_attempt_at"] = now
    else:
        entry["last_refused_at"] = now
    atomic_write_json(path, data)
    return allowed, dict(entry)


def receipt_id(row: dict) -> str:
    seed = json.dumps(
        {
            "attempted_at": row.get("attempted_at"),
            "agent_id": row.get("agent_id"),
            "server": row.get("server"),
            "signature_class": row.get("signature_class"),
            "result": row.get("result"),
        },
        sort_keys=True,
    )
    return "tdr_" + hashlib.sha256(seed.encode()).hexdigest()[:20]


def validate_receipt(row: dict) -> None:
    required_str = ["receipt_id", "attempted_at", "agent_id", "home", "server", "signature_class", "status", "result"]
    missing = [key for key in required_str if not isinstance(row.get(key), str) or not row.get(key)]
    if missing:
        raise ValueError(f"receipt missing required fields: {missing}")
    if row.get("receipt_schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("receipt_schema_version mismatch")
    if not isinstance(row.get("files_touched"), list):
        raise ValueError("receipt files_touched must be a list")
    if not isinstance(row.get("restart_performed"), bool):
        raise ValueError("receipt restart_performed must be a bool")
    if row["status"] not in {"attempted", "refused", "healed", "escalated", "unsupported_platform"}:
        raise ValueError(f"invalid receipt status: {row['status']}")
    if row["status"] == "escalated":
        esc = row.get("escalation")
        if not isinstance(esc, dict) or esc.get("status") != "escalated":
            raise ValueError("escalated receipt requires escalation object")


def complete_receipt(row: dict) -> dict:
    out = dict(row)
    out.setdefault("receipt_schema_version", RECEIPT_SCHEMA_VERSION)
    out.setdefault("receipt_id", receipt_id(out))
    out.setdefault("signature", "")
    out.setdefault("files_touched", [])
    out.setdefault("restart_performed", False)
    validate_receipt(out)
    return out



def validate_health_payload(payload: dict) -> None:
    required = ["generated_at", "agent_id", "home", "status", "servers"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"health payload missing required fields: {missing}")
    if payload["status"] not in {"pass", "fail"}:
        raise ValueError(f"invalid health status: {payload['status']}")
    if not isinstance(payload["servers"], list):
        raise ValueError("health servers must be a list")
    for row in payload["servers"]:
        if not isinstance(row, dict):
            raise ValueError("health server row must be an object")
        for key in ("server", "declared", "connect", "list_tools", "cheap_call", "status", "duration_ms"):
            if key not in row:
                raise ValueError(f"health server row missing {key}")
        if row["status"] not in {"pass", "fail", "skip"}:
            raise ValueError(f"invalid server status: {row['status']}")


def write_receipt(path: Path, row: dict) -> None:
    row = complete_receipt(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as fh:
        tmp = Path(fh.name)
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def last_healed_receipts(home: Path) -> dict[str, dict]:
    path = receipt_path(home, None)
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        for raw in path.read_text(errors="replace").splitlines()[-1000:]:
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row.get("result") == "healed" and row.get("server"):
                out[str(row["server"])] = {
                    "attempted_at": row.get("attempted_at"),
                    "recipe_id": row.get("recipe_id"),
                    "receipt_path": str(path),
                }
    except Exception:
        return out
    return out


def health_path(home: Path, override: str | None = None) -> Path:
    return Path(override).expanduser() if override else home / HEALTH_PATH


def heartbeat_path(home: Path, override: str | None = None) -> Path:
    return Path(override).expanduser() if override else home / HEARTBEAT_PATH


def _receipt_id_from_output(out: dict | None) -> str:
    if not isinstance(out, dict):
        return ""
    if isinstance(out.get("receipt_id"), str):
        return out["receipt_id"]
    receipt = out.get("latest_receipt") if isinstance(out.get("latest_receipt"), dict) else {}
    return str(receipt.get("receipt_id") or "")


def write_heartbeat(
    home: Path,
    *,
    agent_id: str,
    started_monotonic: float,
    exit_code: int,
    out: dict | None = None,
    error: str = "",
    path_override: str | None = None,
) -> dict:
    """Persist a watcher heartbeat for every tool-doctor invocation.

    A completed probe/repair/classify/refusal counts as a successful watcher run
    even when the agent's tool state is failing. Only tool-doctor execution
    errors should leave `last_success_at` unchanged.
    """
    path = heartbeat_path(home, path_override)
    prior = load_json(path, {})
    now = utcnow()
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    success = isinstance(out, dict) and out.get("status") != "error"
    row = {
        "host": socket.gethostname(),
        "agent_id": agent_id,
        "home": str(home),
        "version": TOOL_DOCTOR_VERSION,
        "last_run_at": now,
        "last_success_at": now if success else prior.get("last_success_at"),
        "last_error": "" if success else (error or f"exit {exit_code}"),
        "last_receipt_id": _receipt_id_from_output(out) or prior.get("last_receipt_id", ""),
        "duration_ms": duration_ms,
    }
    atomic_write_json(path, row)
    return row


def heartbeat_command(args: argparse.Namespace) -> dict:
    home = resolve_home(args.home)
    path = heartbeat_path(home, args.heartbeat_path)
    return load_json(path, {})


def repair(args: argparse.Namespace) -> dict:
    home = resolve_home(args.home)
    server = args.server
    agent_id = args.agent_id or current_user_for_home(home) or home.parent.name
    before = None
    signature = args.signature or ""
    if not signature:
        before = mcp_list_tools(home, server)
        signature = before.get("signature") or ""
    classification = classify_signature(server, signature)
    signature_class = classification["signature_class"]
    recipe = args.recipe or classification.get("recipe_id") or classify(server, signature)
    if classification["repairable"]:
        allowed, budget = reserve_daily_budget(
            home,
            agent_id=agent_id,
            server=server,
            signature_class=signature_class,
            path_override=args.budget_path,
        )
        if not allowed:
            row = {
                "attempted_at": utcnow(),
                "agent_id": agent_id,
                "home": str(home),
                "server": server,
                "signature": signature,
                "signature_class": signature_class,
                "recipe_id": recipe,
                "action": "refused_daily_budget",
                "status": "refused",
                "result": "refused_daily_budget",
                "files_touched": [],
                "restart_performed": False,
                "budget": budget,
                "probe_before": {"status": "skip", "reason": "daily_budget_refused"},
                "probe_after": {"status": "skip", "reason": "daily_budget_refused"},
                "escalation": escalation_object(
                    signature_class,
                    "daily_budget_already_spent_for_signature_server",
                    runbook="tool-doctor-daily-budget-refusal",
                    first_seen=budget.get("first_seen"),
                    attempt_count_24h=int(budget.get("seen_count") or 0),
                    failed_twice=int(budget.get("seen_count") or 0) >= 2,
                    evidence=classification.get("evidence"),
                ),
            }
            if not args.no_receipt:
                write_receipt(receipt_path(home, args.receipt_path), row)
            return complete_receipt(row)
    elif classification["action"] == "escalate":
        row = {
            "attempted_at": utcnow(),
            "agent_id": agent_id,
            "home": str(home),
            "server": server,
            "signature": signature,
            "signature_class": signature_class,
            "recipe_id": recipe,
            "action": "escalated_without_self_fix",
            "status": "escalated",
            "result": "escalated",
            "files_touched": [],
            "restart_performed": False,
            "probe_before": before or {"status": "skip", "reason": "escalate_only_signature"},
            "probe_after": {"status": "skip", "reason": "escalate_only_signature"},
            "escalation": classification["escalation"],
        }
        if not args.no_receipt:
            write_receipt(receipt_path(home, args.receipt_path), row)
        return complete_receipt(row)
    if before is None:
        before = mcp_list_tools(home, server)
    files_touched: list[str] = []
    action = "none"
    if before.get("status") == "pass":
        action = "verified_existing_mcp"
    elif recipe == "browser-lane-missing-venv":
        action, files_touched = repair_browser_lane(home, args.dry_run)
    elif recipe == "golden-mcp-python-server-connection-closed":
        action = "verified_existing_python_mcp" if before["status"] == "pass" else "escalated_python_mcp_still_fails"
    elif recipe == "gbrain-wrapper-connection-closed":
        action = "escalated_gbrain_pglite_no_phase0_patch"
    else:
        action = "escalated_unknown_signature"
    after = before if args.dry_run else mcp_list_tools(home, server)
    result = "dry_run" if args.dry_run else ("healed" if after.get("status") == "pass" else "escalated")
    status = "healed" if result == "healed" else ("attempted" if result == "dry_run" else "escalated")
    row = {
        "attempted_at": utcnow(),
        "agent_id": agent_id,
        "home": str(home),
        "server": server,
        "signature": signature,
        "signature_class": signature_class,
        "recipe_id": recipe,
        "action": action,
        "status": status,
        "result": result,
        "files_touched": files_touched,
        "restart_performed": False,
        "probe_before": before,
        "probe_after": after,
    }
    if status == "escalated":
        row["escalation"] = escalation_object(
            signature_class,
            "repair_attempt_failed_post_probe",
            evidence=classification.get("evidence"),
        )
    if not args.no_receipt:
        write_receipt(receipt_path(home, args.receipt_path), row)
    return complete_receipt(row)


def probe_configured_web_backends(
    home: Path, cfg: dict, timeout: int, cheap_call: bool
) -> list[dict]:
    web = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
    search_backend = str(web.get("search_backend") or web.get("backend") or "").strip().lower()
    if search_backend != "searxng":
        return []

    started = time.monotonic()
    env = dotenv_env(home)
    endpoint = str(env.get("SEARXNG_URL") or "http://127.0.0.1:8080").rstrip("/")
    base = {
        "server": "web-search:searxng",
        "declared": True,
        "connect": "fail",
        "list_tools": "skip",
        "cheap_call": "skip",
        "backend": "searxng",
    }
    if not cheap_call:
        return [
            {
                **base,
                "status": "skip",
                "connect": "skip",
                "signature": "cheap_call_disabled",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        ]

    params = urllib.parse.urlencode({"q": "tool-doctor-canary", "format": "json"})
    try:
        request = urllib.request.Request(
            f"{endpoint}/search?{params}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=min(timeout, 10)) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("invalid SearXNG JSON response")
        return [
            {
                **base,
                "status": "pass",
                "connect": "pass",
                "cheap_call": "pass",
                "signature": "",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        ]
    except Exception as exc:  # noqa: BLE001
        return [
            {
                **base,
                "status": "fail",
                "cheap_call": "fail",
                "signature": f"configured_searxng_unreachable: {type(exc).__name__}: {exc}"[:300],
                "signature_class": "web_backend_unreachable",
                "needs_you": True,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        ]

def probe(args: argparse.Namespace) -> dict:
    home = resolve_home(args.home)
    servers = args.server or []
    config_unverified = None
    cfg: dict = {}
    try:
        cfg = load_config(home)
    except UnverifiedConfig as exc:
        config_unverified = str(exc)
    if not servers and not config_unverified:
        servers = sorted(
            name
            for name, val in (cfg.get("mcp_servers") or {}).items()
            if isinstance(name, str) and isinstance(val, dict) and is_enabled_server(val)
        )
    healed = last_healed_receipts(home)
    rows = []
    if config_unverified:
        rows.append(
            {
                "server": "config.yaml",
                "declared": False,
                "status": "unverified",
                "connect": "unverified",
                "list_tools": "skip",
                "cheap_call": "skip",
                "duration_ms": 0,
                "signature": config_unverified[:300],
                "signature_class": "stale_refs_unverified",
                "needs_you": True,
                "tools": [],
                "last_healed_receipt": None,
            }
        )
    elif not args.server:
        rows.extend(required_server_failures(cfg))
    for server in servers:
        row = {
            "server": server,
            **mcp_list_tools(home, server, timeout=args.timeout, cheap_call=not args.no_cheap_calls),
        }
        row["last_healed_receipt"] = healed.get(server)
        rows.append(row)
    if not config_unverified and not args.server:
        rows.extend(probe_configured_web_backends(home, cfg, args.timeout, not args.no_cheap_calls))
    prior_health = load_json(health_path(home, args.health_path), {})
    for prior in prior_health.get("servers", []):
        if not isinstance(prior, dict) or prior.get("signature_class") not in CALL_INCIDENT_SIGNATURES:
            continue
        if args.server and prior.get("server") not in args.server:
            continue
        key = (prior.get("server"), prior.get("signature_class"))
        if any((row.get("server"), row.get("signature_class")) == key for row in rows):
            continue
        rows.append(dict(prior))
    plugin_health = {"status": "unverified", "enabled_count": 0, "discovered_count": 0, "plugins": []}
    if not config_unverified:
        plugin_health = check_plugin_activation(home, cfg)
    status = (
        "pass"
        if all(r["status"] in {"pass", "skip"} for r in rows) and plugin_health["status"] in {"pass", "skip"}
        else ("unverified" if config_unverified else "fail")
    )
    out = {
        "generated_at": utcnow(),
        "agent_id": args.agent_id or agent_id_for_home(home),
        "home": str(home),
        "status": status,
        "servers": rows,
        "plugins": plugin_health,
    }
    if args.write_health:
        atomic_write_json(health_path(home, args.health_path), out)
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False
    return True


def acquire_heal_lock(home: Path) -> tuple[int, Path] | None:
    """Acquire the one local reaction lock shared by every heal entrypoint."""
    path = home / HEAL_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, json.dumps({"pid": os.getpid(), "created_at": utcnow()}).encode())
            os.fsync(fd)
            return fd, path
        except FileExistsError:
            try:
                owner = load_json(path, {})
                owner_pid = int(owner.get("pid") or 0)
            except (TypeError, ValueError, OSError):
                return None
            if _pid_alive(owner_pid):
                return None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return None


def release_heal_lock(lock: tuple[int, Path]) -> None:
    fd, path = lock
    os.close(fd)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _heal_unlocked(args: argparse.Namespace, home: Path) -> dict:
    before = probe(args)
    failures = [
        row for row in before.get("servers", [])
        if isinstance(row, dict) and row.get("status") not in {"pass", "skip"}
    ]
    if not args.server:
        failures.extend(
            {
                "server": "plugin:" + str(row.get("plugin") or "unknown"),
                "signature": str(row.get("detail") or row.get("activation") or "plugin activation failed"),
            }
            for row in (before.get("plugins") or {}).get("plugins", [])
            if isinstance(row, dict) and row.get("status") == "fail"
        )
    repairs = []
    for row in failures:
        repairs.append(repair(SimpleNamespace(
            home=str(home), server=str(row.get("server") or "unknown"),
            signature=str(row.get("signature") or "unknown"), recipe=None,
            agent_id=args.agent_id, dry_run=args.dry_run,
            receipt_path=args.receipt_path, budget_path=args.budget_path,
            no_receipt=False,
        )))
    after = probe(args)
    files_touched = sorted({str(path) for row in repairs for path in row.get("files_touched", [])})
    restart_performed = any(bool(row.get("restart_performed")) for row in repairs)
    if after.get("status") == "pass":
        status = "healed" if repairs else "attempted"
        result = "healed" if repairs else "verified_healthy"
        escalation = None
    else:
        status, result = "escalated", "escalated"
        escalation = escalation_object(
            "local_heal_cycle", "post_repair_probe_still_failing",
            runbook="tool-doctor-local-heal-review", attempt_count_24h=len(repairs),
            failed_twice=any(row.get("status") in {"escalated", "refused"} for row in repairs),
        )
    cycle = {
        "attempted_at": utcnow(), "agent_id": args.agent_id or agent_id_for_home(home),
        "home": str(home), "server": "tool-doctor-heal",
        "signature": "local probe-repair-reprobe cycle", "signature_class": "local_heal_cycle",
        "recipe_id": "allowlisted-local-repair-v1", "action": "probe_repair_reprobe",
        "status": status, "result": result, "files_touched": files_touched,
        "restart_performed": restart_performed, "probe_before": before,
        "probe_after": after, "repairs": repairs,
    }
    if escalation:
        cycle["escalation"] = escalation
    write_receipt(receipt_path(home, args.receipt_path), cycle)
    return complete_receipt(cycle)


def heal(args: argparse.Namespace) -> dict:
    """Probe, apply one allowlisted local repair per failure, then re-probe."""
    home = resolve_home(args.home)
    lock = acquire_heal_lock(home)
    if lock is None:
        row = complete_receipt({
            "attempted_at": utcnow(), "agent_id": args.agent_id or agent_id_for_home(home),
            "home": str(home), "server": "tool-doctor-heal",
            "signature": "matching local heal is already running",
            "signature_class": "local_heal_overlap", "recipe_id": "none",
            "action": "refuse_duplicate_heal", "status": "refused",
            "result": "concurrent_heal_suppressed", "files_touched": [],
            "restart_performed": False,
        })
        write_receipt(receipt_path(home, args.receipt_path), row)
        return row
    try:
        return _heal_unlocked(args, home)
    finally:
        release_heal_lock(lock)


def classify_command(args: argparse.Namespace) -> dict:
    if args.sample:
        sample = CLASSIFY_SAMPLES[args.sample]
        server = sample["server"]
        signature = sample["signature"]
        command = sample.get("command")
        cwd = sample.get("cwd")
        env_key_names = sample.get("env_key_names") or []
    elif args.health_path:
        health = load_json(Path(args.health_path).expanduser(), {})
        rows = []
        for item in health.get("servers", []):
            if item.get("status") == "pass":
                continue
            rows.append(
                classify_signature(
                    str(item.get("server") or ""),
                    str(item.get("signature") or ""),
                    command=str(item.get("command") or item.get("resolved_command") or ""),
                    cwd=health.get("home"),
                    env_key_names=args.env_key_name or [],
                )
            )
        return {"source": str(args.health_path), "classifications": rows}
    else:
        server = args.server or ""
        signature = args.signature or ""
        command = args.command
        cwd = args.cwd
        env_key_names = args.env_key_name or []
    return classify_signature(server, signature, command=command, cwd=cwd, env_key_names=env_key_names)


def self_test() -> dict:
    checks = []

    def record(name: str, fn) -> None:
        started = time.monotonic()
        try:
            detail = fn() or {}
            checks.append(
                {
                    "name": name,
                    "status": "pass",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    **detail,
                }
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {
                    "name": name,
                    "status": "fail",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    def check_signature_classifier() -> dict:
        missing = classify_signature("browser-lane", CLASSIFY_SAMPLES["missing-browser-lane"]["signature"])
        auth = classify_signature("google-drive", CLASSIFY_SAMPLES["auth-401"]["signature"])
        assert missing["action"] == "repair" and missing["recipe_id"] == "browser-lane-missing-venv"
        assert auth["action"] == "escalate" and auth["recipe_id"] is None
        return {"repairable": missing["signature_class"], "escalated": auth["signature_class"]}

    def check_path_safety() -> dict:
        with tempfile.TemporaryDirectory(prefix="tool-doctor-self-test-") as tmp:
            home = Path(tmp) / ".hermes"
            home.mkdir()
            ensure_safe_write(home / "state/tool-health.json", home)
            blocked = []
            for candidate in (home / ".env", Path(tmp).parent / "other-home/.hermes/state/file"):
                try:
                    ensure_safe_write(candidate, home)
                except RuntimeError:
                    blocked.append(str(candidate))
            assert len(blocked) == 2
            return {"blocked": len(blocked)}

    def check_receipt_schema() -> dict:
        row = complete_receipt(
            {
                "attempted_at": utcnow(),
                "agent_id": "self-test",
                "home": "/tmp/self-test/.hermes",
                "server": "browser-lane",
                "signature": "missing executable",
                "signature_class": "missing_executable_declared_mcp_venv",
                "recipe_id": "browser-lane-missing-venv",
                "status": "attempted",
                "result": "dry_run",
                "files_touched": [],
                "restart_performed": False,
            }
        )
        return {"receipt_schema_version": row["receipt_schema_version"], "receipt_id": row["receipt_id"]}

    def check_health_schema() -> dict:
        payload = {
            "generated_at": utcnow(),
            "agent_id": "self-test",
            "home": "/tmp/self-test/.hermes",
            "status": "pass",
            "servers": [
                {
                    "server": "fake",
                    "declared": True,
                    "connect": "pass",
                    "list_tools": "pass",
                    "cheap_call": "skip",
                    "status": "pass",
                    "duration_ms": 1,
                }
            ],
        }
        validate_health_payload(payload)
        return {"servers": len(payload["servers"])}

    def check_recipe_contract() -> dict:
        data = load_yaml(RECIPE_PATH)
        recipes = data.get("recipes") or {}
        assert "browser-lane-missing-venv" in recipes
        unsafe = [rid for rid, recipe in recipes.items() if recipe.get("touches_secrets") is not False]
        assert not unsafe, unsafe
        return {"recipes": sorted(recipes)}

    record("signature_classifier", check_signature_classifier)
    record("recipe_path_safety", check_path_safety)
    record("receipt_schema", check_receipt_schema)
    record("health_json_schema", check_health_schema)
    record("recipe_contract", check_recipe_contract)
    status = "pass" if all(check["status"] == "pass" for check in checks) else "fail"
    return {
        "generated_at": utcnow(),
        "status": status,
        "version": TOOL_DOCTOR_VERSION,
        "checks": checks,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("probe")
    p.add_argument("--home", default=str(Path.home() / ".hermes"))
    p.add_argument("--config-path")
    p.add_argument("--server", action="append")
    p.add_argument("--agent-id")
    p.add_argument("--timeout", type=int, default=int(os.environ.get("TOOL_DOCTOR_MCP_TIMEOUT", "15")))
    p.add_argument("--write-health", action="store_true")
    p.add_argument("--health-path")
    p.add_argument("--no-cheap-calls", action="store_true")
    p.add_argument("--strict-exit", action="store_true", help="exit non-zero when any probed server fails")
    p.add_argument("--json", action="store_true")
    live = sub.add_parser("live")
    live.add_argument("--home", default=str(Path.home() / ".hermes"))
    live.add_argument("--route", default="capability-router")
    live.add_argument("--timeout", type=int, default=int(os.environ.get("TOOL_DOCTOR_MCP_TIMEOUT", "15")))
    live.add_argument("--no-recovery", action="store_true")
    live.add_argument("--agent-id")
    live.add_argument("--json", action="store_true")
    r = sub.add_parser("repair")
    r.add_argument("--home", default=str(Path.home() / ".hermes"))
    r.add_argument("--server", required=True)
    r.add_argument("--signature")
    r.add_argument("--recipe")
    r.add_argument("--agent-id")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--receipt-path")
    r.add_argument("--budget-path")
    r.add_argument("--no-receipt", action="store_true")
    r.add_argument("--json", action="store_true")
    heal_parser = sub.add_parser("heal", help="probe, repair allowlisted local failures, re-probe, and receipt")
    heal_parser.add_argument("--home", default=str(Path.home() / ".hermes"))
    heal_parser.add_argument("--config-path")
    heal_parser.add_argument("--server", action="append")
    heal_parser.add_argument("--agent-id")
    heal_parser.add_argument("--timeout", type=int, default=int(os.environ.get("TOOL_DOCTOR_MCP_TIMEOUT", "15")))
    heal_parser.add_argument("--write-health", action="store_true", default=True)
    heal_parser.add_argument("--health-path")
    heal_parser.add_argument("--no-cheap-calls", action="store_true")
    heal_parser.add_argument("--dry-run", action="store_true")
    heal_parser.add_argument("--receipt-path")
    heal_parser.add_argument("--budget-path")
    heal_parser.add_argument("--json", action="store_true")
    c = sub.add_parser("classify")
    c.add_argument("--sample", choices=sorted(CLASSIFY_SAMPLES))
    c.add_argument("--server")
    c.add_argument("--signature")
    c.add_argument("--health-path")
    c.add_argument("--command")
    c.add_argument("--cwd")
    c.add_argument("--env-key-name", action="append")
    c.add_argument("--json", action="store_true")
    h = sub.add_parser("heartbeat")
    h.add_argument("--home", default=str(Path.home() / ".hermes"))
    h.add_argument("--agent-id")
    h.add_argument("--heartbeat-path")
    h.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        out = self_test()
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["status"] == "pass" else 2
    if not args.cmd:
        ap.error("a subcommand is required unless --self-test is used")
    started = time.monotonic()
    out: dict | None = None
    exit_code = 0
    error = ""
    home = resolve_home(getattr(args, "home", str(Path.home() / ".hermes")))
    try:
        select_config_path(home, getattr(args, "config_path", None))
        if args.cmd == "probe":
            out = probe(args)
        elif args.cmd == "live":
            out = live_capability_probe(
                home,
                args.route,
                timeout=args.timeout,
                allow_recovery=not args.no_recovery,
            )
        elif args.cmd == "repair":
            out = repair(args)
        elif args.cmd == "heal":
            out = heal(args)
        elif args.cmd == "heartbeat":
            out = heartbeat_command(args)
        else:
            out = classify_command(args)
        if args.cmd == "probe" and getattr(args, "strict_exit", False) and out.get("status") != "pass":
            exit_code = 2
        elif args.cmd == "live" and out.get("verdict") not in {"HEALTHY", "RECOVERED", "DISABLED"}:
            exit_code = 2
        elif args.cmd == "repair" and out.get("result") not in {"healed", "dry_run"}:
            exit_code = 2
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        out = {"status": "error", "error": error}
        exit_code = 2
    agent_id = getattr(args, "agent_id", None) or (out or {}).get("agent_id") or agent_id_for_home(home)
    heartbeat = write_heartbeat(
        home,
        agent_id=str(agent_id),
        started_monotonic=started,
        exit_code=exit_code,
        out=out,
        error=error,
        path_override=getattr(args, "heartbeat_path", None),
    )
    if args.cmd == "heartbeat":
        out = heartbeat
    if args.cmd == "live" and not args.json:
        print((out or {}).get("operator_summary") or render_live_summary(out or {}))
    else:
        print(json.dumps(out, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
