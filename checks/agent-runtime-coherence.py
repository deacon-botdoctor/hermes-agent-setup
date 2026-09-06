#!/usr/bin/env python3
"""Cross-platform no-model proof for one active Hermes runtime root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_BINDING_KIND = "botdoctor_runtime_binding"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def probe_program(runtime_root: Path) -> str:
    root = json.dumps(str(runtime_root.resolve()))
    return f"""
import ast, inspect, json, sys
from pathlib import Path
root=Path({root}).resolve()
sys.path.insert(0, str(root))
import gateway.run as gateway_run
if hasattr(gateway_run, '_load_runtime_ai_agent_class'):
    ai_agent=gateway_run._load_runtime_ai_agent_class()
else:
    import run_agent as first_run_agent
    ai_agent=first_run_agent.AIAgent
import run_agent
import agent.agent_init as agent_init
origins={{
  'gateway.run':str(Path(inspect.getfile(gateway_run)).resolve()),
  'run_agent':str(Path(inspect.getfile(run_agent)).resolve()),
  'agent.agent_init':str(Path(inspect.getfile(agent_init)).resolve()),
  'AIAgent':str(Path(inspect.getfile(ai_agent)).resolve()),
}}
expected={{
  'gateway.run':str(root/'gateway'/'run.py'),
  'run_agent':str(root/'run_agent.py'),
  'agent.agent_init':str(root/'agent'/'agent_init.py'),
  'AIAgent':str(root/'run_agent.py'),
}}
origin_mismatches={{name:{{'expected':expected[name],'actual':actual}}
                   for name,actual in origins.items() if Path(actual)!=Path(expected[name])}}
init_sig=inspect.signature(agent_init.init_agent)
init_params={{name for name,param in init_sig.parameters.items()
             if name!='agent' and param.kind not in (inspect.Parameter.VAR_POSITIONAL,inspect.Parameter.VAR_KEYWORD)}}
accepts_kwargs=any(param.kind==inspect.Parameter.VAR_KEYWORD for param in init_sig.parameters.values())
caller_tree=ast.parse((root/'run_agent.py').read_text(encoding='utf-8'))
constructors=[item for node in caller_tree.body if isinstance(node,ast.ClassDef) and node.name=='AIAgent'
              for item in node.body if isinstance(item,(ast.FunctionDef,ast.AsyncFunctionDef)) and item.name=='__init__']
calls=[node for constructor in constructors for node in ast.walk(constructor)
       if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='init_agent']
dynamic=any(keyword.arg is None for call in calls for keyword in call.keywords)
forwarded={{keyword.arg for call in calls for keyword in call.keywords if keyword.arg is not None}}
missing=(['dynamic argument expansion'] if dynamic else [])
if not accepts_kwargs:
    missing.extend(sorted(forwarded-init_params))
print('HERMES_RUNTIME_COHERENCE='+json.dumps({{
  'runtime_root':str(root),'origins':origins,'origin_mismatches':origin_mismatches,
  'missing_init_params':missing}},sort_keys=True))
raise SystemExit(42 if origin_mismatches or missing else 0)
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_runtime_binding(
    *,
    binding_path: Path,
    runtime_root: Path,
    runtime_python: Path,
    hermes_home: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(binding_path),
        "ok": False,
        "reason": "binding_missing",
    }
    if not binding_path.is_file() or binding_path.is_symlink():
        return result
    try:
        payload = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        result["reason"] = "binding_invalid"
        return result
    expected = {
        "schema_version": 1,
        "kind": RUNTIME_BINDING_KIND,
        "status": "active",
        "hermes_home": str(hermes_home.resolve()),
        "runtime_root": str(runtime_root.resolve()),
        "runtime_python": str(runtime_python.absolute()),
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        result["reason"] = "binding_tuple_mismatch"
        return result
    service = payload.get("service")
    if (
        not isinstance(service, dict)
        or not isinstance(service.get("kind"), str)
        or not service.get("kind").strip()
        or not isinstance(service.get("owner"), str)
        or not service.get("owner").strip()
    ):
        result["reason"] = "binding_service_invalid"
        return result
    definition_path = service.get("definition_path")
    definition_sha = service.get("definition_sha256")
    if (
        not isinstance(definition_sha, str)
        or len(definition_sha) != 64
        or any(character not in "0123456789abcdef" for character in definition_sha)
    ):
        result["reason"] = "binding_definition_invalid"
        return result
    if definition_path is not None:
        definition = Path(str(definition_path))
        if (
            not definition.is_file()
            or definition.is_symlink()
            or not isinstance(definition_sha, str)
            or _sha256(definition) != definition_sha
        ):
            result["reason"] = "binding_definition_drift"
            return result
    launchers = service.get("launchers")
    if not isinstance(launchers, list) or (definition_path is None and not launchers):
        result["reason"] = "binding_launchers_invalid"
        return result
    for row in launchers:
        if not isinstance(row, dict):
            result["reason"] = "binding_launchers_invalid"
            return result
        launcher = Path(str(row.get("path") or ""))
        if (
            not launcher.is_file()
            or launcher.is_symlink()
            or _sha256(launcher) != row.get("sha256")
        ):
            result["reason"] = "binding_launcher_drift"
            return result
    result.update({"ok": True, "reason": "bound", "service_kind": service.get("kind")})
    return result


def run_probe(
    *,
    runtime_root: Path,
    runtime_python: Path,
    hermes_home: Path,
    agent_id: str,
    binding_path: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "agent_id": agent_id,
        "hermes_home": str(hermes_home.resolve()),
        "runtime_root": str(runtime_root.resolve()),
        # The venv entrypoint is part of runtime identity. Keep its lexical
        # path instead of collapsing it to the host interpreter symlink target.
        "runtime_python": str(runtime_python.absolute()),
        "ok": False,
        "kind": "spawn_failure",
        "origins": {},
        "origin_mismatches": {},
        "missing_init_params": [],
    }
    binding = validate_runtime_binding(
        binding_path=binding_path or hermes_home / "state/runtime-binding.json",
        runtime_root=runtime_root,
        runtime_python=runtime_python,
        hermes_home=hermes_home,
    )
    result["runtime_binding"] = binding
    if not binding["ok"]:
        result["kind"] = str(binding["reason"])
        return result
    if not runtime_root.is_dir() or not runtime_python.is_file():
        result["kind"] = "binding_missing"
        return result
    try:
        # Imports may initialize SQLite stores. Never join the live profile's WAL.
        with tempfile.TemporaryDirectory(prefix="hermes-runtime-coherence-") as temporary:
            probe_home = Path(temporary) / ".hermes"
            probe_home.mkdir()
            env = os.environ.copy()
            env.update({"HOME": temporary, "USERPROFILE": temporary,
                        "HERMES_HOME": str(probe_home), "PYTHONNOUSERSITE": "1"})
            proc = subprocess.run(
                [str(runtime_python), "-I", "-c", probe_program(runtime_root)],
                cwd=runtime_root,
                env=env,
                text=True,
                capture_output=True,
                timeout=45,
                check=False,
            )
    except subprocess.TimeoutExpired:
        result["kind"] = "timeout"
        return result
    except OSError as exc:
        result["detail"] = f"{type(exc).__name__}: {exc}"[:300]
        return result
    payload = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("HERMES_RUNTIME_COHERENCE="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                pass
    if isinstance(payload, dict):
        result.update(payload)
    if proc.returncode == 0 and isinstance(payload, dict):
        result.update({"ok": True, "kind": "coherent"})
    elif proc.returncode == 42 and isinstance(payload, dict):
        result["kind"] = "runtime_coherence"
    else:
        result["kind"] = "import_failure"
        result["detail"] = " | ".join((proc.stderr or proc.stdout or "").splitlines()[-4:])[:300]
    return result


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--runtime-python", required=True, type=Path)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--binding-receipt", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_probe(
        runtime_root=args.runtime_root.expanduser(),
        runtime_python=args.runtime_python.expanduser(),
        hermes_home=args.home.expanduser(),
        agent_id=args.agent_id,
        binding_path=args.binding_receipt.expanduser() if args.binding_receipt else None,
    )
    atomic_write(args.receipt.expanduser(), result)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
