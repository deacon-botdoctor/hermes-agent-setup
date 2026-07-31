#!/usr/bin/env python3
"""Read-only readiness audit for Golden semantic computer control v2."""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "contracts" / "semantic-computer-control-v2.json"


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    plugin = data.get("plugin") if isinstance(data, dict) else None
    if (
        data.get("schema_version") != 1
        or not isinstance(data.get("contract_id"), str)
        or not isinstance(data.get("policy_key"), str)
        or not isinstance(plugin, dict)
        or not isinstance(plugin.get("name"), str)
        or not isinstance(plugin.get("files"), list)
        or not all(isinstance(item, str) and item for item in plugin["files"])
        or not isinstance(plugin.get("hooks"), list)
        or not all(isinstance(item, str) and item for item in plugin["hooks"])
        or not isinstance(data.get("skill"), str)
        or not isinstance(data.get("toolset"), str)
        or not isinstance(data.get("runtime_source"), str)
        or not isinstance(data.get("runtime_permission_resolver"), str)
        or not isinstance(data.get("guard_markers"), list)
        or not all(
            isinstance(item, str) and item for item in data["guard_markers"]
        )
        or data.get("probe_action") != "list_windows"
    ):
        raise ValueError("invalid semantic computer-control contract")
    return data


def load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} unreadable or invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    return data


def nested_bool(config: dict[str, Any], dotted_key: str) -> bool:
    value: Any = config
    for key in dotted_key.split("."):
        if not isinstance(value, dict):
            return False
        value = value.get(key)
    return value is True


def compile_python(path: Path, label: str, gaps: list[str]) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        ast.parse(text, filename=str(path))
        return text
    except (OSError, UnicodeError, SyntaxError):
        gaps.append(f"{label}:missing-or-invalid")
        return ""


def configured_surfaces(config: dict[str, Any], toolset: str) -> list[str]:
    toolsets = config.get("platform_toolsets")
    if not isinstance(toolsets, dict):
        return []
    return sorted(
        str(name)
        for name, values in toolsets.items()
        if isinstance(name, str)
        and isinstance(values, list)
        and toolset in values
    )


def runtime_python(runtime_root: Path) -> Path:
    candidates = (
        runtime_root / ".venv/bin/python",
        runtime_root / "venv/bin/python",
        runtime_root / ".venv/Scripts/python.exe",
        runtime_root / "venv/Scripts/python.exe",
    )
    return next((path for path in candidates if path.is_file()), Path(sys.executable))


def semantic_probe(
    home: Path,
    runtime_root: Path,
    python: Path | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    probe = """
import json
from hermes_cli.plugins import discover_plugins
from model_tools import handle_function_call
discover_plugins(force=True)
raw = handle_function_call(
    "computer_use",
    {"action": "list_windows"},
    session_id="semantic-readiness-probe",
    enabled_tools=["computer_use"],
    enabled_toolsets=["computer_use"],
)
parsed = json.loads(raw) if isinstance(raw, str) else raw
if isinstance(parsed, dict) and parsed.get("error"):
    raise SystemExit(str(parsed["error"]))
count = len(parsed) if isinstance(parsed, list) else None
print(json.dumps({"ok": True, "window_count": count}, sort_keys=True))
"""
    executable = python or runtime_python(runtime_root)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    try:
        proc = subprocess.run(
            [str(executable), "-c", probe],
            cwd=runtime_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": str(exc)}
    detail = (proc.stderr or proc.stdout).strip()
    if proc.returncode != 0:
        return {"ok": False, "detail": detail or f"exit {proc.returncode}"}
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "detail": "probe returned invalid JSON"}
    return payload if payload.get("ok") is True else {"ok": False, "detail": detail}


def audit(
    contract: dict[str, Any],
    home: Path,
    runtime_root: Path,
    *,
    required: bool = False,
    run_probe: bool = False,
    python: Path | None = None,
) -> dict[str, Any]:
    gaps: list[str] = []
    try:
        config = load_yaml_mapping(home / "config.yaml", "config")
    except ValueError as exc:
        return {
            "contract_id": contract["contract_id"],
            "ok": False,
            "status": "blocked",
            "gaps": [f"config:{exc}"],
            "surfaces": [],
            "probe": None,
        }

    enabled = nested_bool(config, contract["policy_key"])
    if not enabled and not required:
        return {
            "contract_id": contract["contract_id"],
            "ok": True,
            "status": "not_applicable",
            "gaps": [],
            "surfaces": [],
            "probe": None,
        }
    if not enabled:
        gaps.append("config:semantic-control-not-enabled")

    plugin_name = contract["plugin"]["name"]
    plugins = config.get("plugins")
    enabled_plugins = plugins.get("enabled") if isinstance(plugins, dict) else None
    if not isinstance(enabled_plugins, list) or plugin_name not in enabled_plugins:
        gaps.append("config:guard-plugin-not-enabled")

    surfaces = configured_surfaces(config, contract["toolset"])
    if not surfaces:
        gaps.append("config:no-computer-use-surface")

    plugin_root = home / "plugins" / plugin_name
    for filename in contract["plugin"]["files"]:
        path = plugin_root / filename
        if filename.endswith(".py"):
            compile_python(path, f"plugin:{filename}", gaps)
        elif not path.is_file():
            gaps.append(f"plugin:{filename}:missing")

    manifest_path = plugin_root / "plugin.yaml"
    if manifest_path.is_file():
        try:
            manifest = load_yaml_mapping(manifest_path, "plugin manifest")
            hooks = manifest.get("provides_hooks")
            if manifest.get("name") != plugin_name:
                gaps.append("plugin:manifest-name-mismatch")
            if not isinstance(hooks, list) or not set(
                contract["plugin"]["hooks"]
            ) <= set(item for item in hooks if isinstance(item, str)):
                gaps.append("plugin:manifest-hooks-missing")
        except ValueError:
            gaps.append("plugin:plugin.yaml:missing-or-invalid")

    guard_path = plugin_root / "guard.py"
    if guard_path.is_file():
        try:
            guard_text = guard_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            guard_text = ""
        for marker in contract["guard_markers"]:
            if marker not in guard_text:
                gaps.append(f"plugin:guard-marker-missing:{marker}")

    skill_path = home / "skills" / contract["skill"]
    if not skill_path.is_file():
        gaps.append("skill:missing")

    runtime_path = runtime_root / contract["runtime_source"]
    runtime_text = compile_python(runtime_path, "runtime:computer-use", gaps)
    if (
        runtime_text
        and contract["runtime_permission_resolver"] not in runtime_text
    ):
        gaps.append("runtime:permission-resolver-missing")

    probe_result = None
    if run_probe and not gaps:
        probe_result = semantic_probe(home, runtime_root, python)
        if probe_result.get("ok") is not True:
            gaps.append("probe:semantic-list-windows-failed")

    return {
        "contract_id": contract["contract_id"],
        "ok": not gaps,
        "status": "ready" if not gaps else "blocked",
        "gaps": sorted(set(gaps)),
        "surfaces": surfaces,
        "probe": probe_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser(),
    )
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--semantic-probe", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    runtime_root = (
        args.runtime_root.expanduser().resolve()
        if args.runtime_root
        else home / "hermes-agent"
    )
    try:
        result = audit(
            load_contract(args.contract.resolve()),
            home,
            runtime_root,
            required=args.required,
            run_probe=args.semantic_probe,
            python=args.python,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"ok": False, "status": "blocked", "gaps": [str(exc)]}
    print(
        json.dumps(result, sort_keys=True)
        if args.json
        else f"{result['status']}: {', '.join(result.get('gaps') or ['no gaps'])}"
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
