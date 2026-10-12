from __future__ import annotations

import ast
import base64
import json
import pprint
import shutil
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml
from mcp.server.mcpserver import MCPServer as FastMCP


SERVER_NAME = "fleet-rollout"
HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
).expanduser()
DEFAULT_CONFIG = Path(
    os.environ.get("FLEET_ROLLOUT_CONFIG", str(HERMES_HOME / "config.yaml"))
).expanduser()
DEFAULT_REGISTRY = Path(
    os.environ.get(
        "FLEET_ROLLOUT_REGISTRY",
        str(Path(__file__).resolve().parents[4] / "kit" / "config" / "fleet-registry.json"),
    )
).expanduser()
DEFAULT_BOUNDARIES = Path(
    os.environ.get(
        "FLEET_ROLLOUT_BOUNDARIES",
        str(HERMES_HOME / "workspace" / "shared-context" / "gitnexus-boundaries.json"),
    )
).expanduser()
DEFAULT_RUNTIME_CONTRACTS = Path(
    os.environ.get(
        "FLEET_ROLLOUT_RUNTIME_CONTRACTS",
        str(Path(__file__).resolve().parents[4] / "kit" / "config" / "fleet-runtime-contracts.json"),
    )
).expanduser()
DEFAULT_RUNTIME_EXPECTATIONS = Path(
    os.environ.get(
        "FLEET_ROLLOUT_RUNTIME_EXPECTATIONS",
        str(Path(__file__).resolve().parents[4] / "kit" / "config" / "fleet-runtime-generated.json"),
    )
).expanduser()
DEFAULT_FLEET_SCANNER = Path(
    os.environ.get(
        "FLEET_ROLLOUT_FLEET_SCANNER",
        str(Path(__file__).resolve().parents[4] / "kit" / "bin" / "fleet-connection-check.py"),
    )
).expanduser()
DEFAULT_BROKER = Path(
    os.environ.get(
        "FLEET_ROLLOUT_BROKER",
        str(Path.home() / ".hermes" / "bin" / "client-runtime-broker.py"),
    )
).expanduser()
DEFAULT_PROMOTION_CANARY_WRITER = Path(
    os.environ.get(
        "FLEET_ROLLOUT_PROMOTION_CANARY_WRITER",
        str(Path(__file__).resolve().parents[4] / "kit" / "bin" / "promotion-canary-proof-write.py"),
    )
).expanduser()

mcp = FastMCP(SERVER_NAME)


def _allowed_root() -> Path:
    return HERMES_HOME.resolve()


def _resolve_repo_path(repo_path: str) -> Path:
    path = Path(repo_path).expanduser().resolve()
    home = Path.home().resolve()
    tmp = Path("/tmp").resolve()
    if path != home and home not in path.parents and path != tmp and tmp not in path.parents:
        raise ValueError(f"repo path outside allowed roots: {path}")
    if not path.exists():
        raise ValueError(f"repo missing: {path}")
    if not (path / ".git").exists():
        raise ValueError(f"repo missing .git: {path}")
    return path


def _promotion_canary_writer_path() -> Path:
    path = DEFAULT_PROMOTION_CANARY_WRITER.resolve()
    if not path.exists():
        raise ValueError(f"promotion canary writer missing: {path}")
    return path


def _registry_path() -> Path:
    return DEFAULT_REGISTRY.resolve()


def _read_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        raise ValueError(f"fleet registry missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_registry(data: dict[str, Any]) -> None:
    path = _registry_path()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _runtime_contracts_path() -> Path:
    return DEFAULT_RUNTIME_CONTRACTS.resolve()


def _fleet_scanner_path() -> Path:
    return DEFAULT_FLEET_SCANNER.resolve()


def _runtime_expectations_path() -> Path:
    return DEFAULT_RUNTIME_EXPECTATIONS.resolve()


def _read_runtime_contracts() -> dict[str, Any]:
    path = _runtime_contracts_path()
    if not path.exists():
        return {"version": 1, "contracts": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime_contract_map() -> dict[str, dict[str, Any]]:
    return {
        item["machine_id"]: item
        for item in (_read_runtime_contracts().get("contracts") or [])
        if item.get("machine_id")
    }


def _read_runtime_expectations() -> dict[str, Any]:
    path = _runtime_expectations_path()
    if not path.exists():
        return {"version": 1, "machines": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_runtime_expectations(data: dict[str, Any]) -> None:
    path = _runtime_expectations_path()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _read_boundaries() -> dict[str, Any]:
    path = DEFAULT_BOUNDARIES.resolve()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    boundaries = data.get("boundaries") or {}
    return boundaries if isinstance(boundaries, dict) else {}


def _machine(machine_id: str) -> dict[str, Any]:
    machines = _read_registry().get("machines", [])
    for item in machines:
        if item.get("id") == machine_id:
            return item
    raise ValueError(f"unknown machine_id: {machine_id}")


def _machine_canary_lane(machine: dict[str, Any]) -> str:
    os_name = str(machine.get("os") or "").strip().lower()
    if os_name == "windows":
        return "windows"
    if os_name == "mac":
        return "mac"
    if os_name == "linux":
        return "spark"
    raise ValueError(f"unsupported canary lane for machine {machine.get('id')}: {os_name}")


def _record_runtime_canary(
    *,
    machine: dict[str, Any],
    repo_path: str,
    target_ref: str,
    status: str,
    check: str,
    detail: str,
    host_override: str | None = None,
) -> dict[str, Any]:
    repo = _resolve_repo_path(repo_path)
    lane = _machine_canary_lane(machine)
    writer = _promotion_canary_writer_path()
    host = (host_override or str(machine.get("id") or "")).strip()
    if not host:
        raise ValueError("host override or machine id is required")
    command = [
        sys.executable,
        str(writer),
        "--repo",
        str(repo),
        "--target-ref",
        target_ref,
        "--lane",
        lane,
        "--status",
        status,
        "--host",
        host,
        "--check",
        check,
    ]
    if detail:
        command.extend(["--detail", detail])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail_msg = (completed.stderr or completed.stdout).strip() or f"rc={completed.returncode}"
        raise ValueError(detail_msg)
    payload = json.loads((completed.stdout or "").strip())
    return {
        "machine_id": machine.get("id"),
        "machine_label": machine.get("label"),
        "lane": lane,
        "host": host,
        **payload,
    }


def _machine_runtime_contract(machine_id: str) -> dict[str, Any]:
    contract = _runtime_contract_map().get(machine_id)
    if not contract:
        raise ValueError(f"unknown runtime contract machine_id: {machine_id}")
    return contract


def _machine_index(machine_id: str) -> tuple[dict[str, Any], int, dict[str, Any]]:
    data = _read_registry()
    machines = list(data.get("machines", []))
    for idx, item in enumerate(machines):
        if item.get("id") == machine_id:
            return data, idx, item
    raise ValueError(f"unknown machine_id: {machine_id}")


def _all_machines() -> list[dict[str, Any]]:
    return list(_read_registry().get("machines", []))


def _select_machines(
    machine_ids: list[str] | None = None,
    *,
    os_filter: str | None = None,
    include_windows: bool = True,
) -> list[dict[str, Any]]:
    if machine_ids:
        machines = [_machine(machine_id) for machine_id in machine_ids]
    else:
        machines = _all_machines()
    if os_filter:
        wanted = os_filter.strip().lower()
        machines = [m for m in machines if str(m.get("os") or "").strip().lower() == wanted]
    if not include_windows:
        machines = [m for m in machines if str(m.get("os") or "").strip().lower() != "windows"]
    return machines


def _cohort_result(machine_id: str, fn) -> dict[str, Any]:
    try:
        payload = fn()
        return {
            "ok": True,
            "machine_id": machine_id,
            "result": payload,
        }
    except Exception as exc:
        return {
            "ok": False,
            "machine_id": machine_id,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _summarize_cohort(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok_count = len([item for item in results if item.get("ok")])
    fail_count = len(results) - ok_count
    return {
        "ok": fail_count == 0,
        "machine_count": len(results),
        "ok_count": ok_count,
        "fail_count": fail_count,
        "results": results,
    }


def _resolve_allowed_path(path_str: str | None) -> Path:
    raw = Path(path_str).expanduser() if path_str else DEFAULT_CONFIG
    path = raw.resolve()
    root = _allowed_root()
    if path != root and root not in path.parents:
        raise ValueError(f"path outside allowed root: {path}")
    return path


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"config missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config must parse to a mapping")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _split_key(path: str) -> list[str]:
    parts = [part.strip() for part in path.split(".") if part.strip()]
    if not parts:
        raise ValueError("dot path is required")
    return parts


def _get_nested(data: dict[str, Any], key_path: str) -> Any:
    cur: Any = data
    for part in _split_key(key_path):
        if not isinstance(cur, dict) or part not in cur:
            raise ValueError(f"missing config path: {key_path}")
        cur = cur[part]
    return cur


def _set_nested(
    data: dict[str, Any],
    key_path: str,
    value: Any,
    *,
    create_missing_paths: bool,
) -> None:
    cur: dict[str, Any] = data
    parts = _split_key(key_path)
    for part in parts[:-1]:
        child = cur.get(part)
        if child is None:
            if not create_missing_paths:
                raise ValueError(f"missing config path: {key_path}")
            child = {}
            cur[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"non-mapping encountered at {part} while setting {key_path}")
        cur = child
    if parts[-1] not in cur and not create_missing_paths:
        raise ValueError(f"missing config path: {key_path}")
    cur[parts[-1]] = value


def _delete_nested(data: dict[str, Any], key_path: str) -> Any:
    cur: Any = data
    parts = _split_key(key_path)
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            raise ValueError(f"missing config path: {key_path}")
        cur = cur[part]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        raise ValueError(f"missing config path: {key_path}")
    before = cur[parts[-1]]
    del cur[parts[-1]]
    return before


def _backup_path(path: Path, suffix: str = "bak") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.{suffix}-{stamp}")


def _remote_config_path(machine: dict[str, Any]) -> str:
    hermes_path = str(machine.get("hermes_path") or machine.get("hermes_home") or "~/.hermes")
    if machine.get("os") == "windows":
        return str(Path(hermes_path) / "config.yaml")
    return hermes_path.rstrip("/") + "/config.yaml"


def _ssh_user(machine: dict[str, Any]) -> str | None:
    user = str(machine.get("ssh_user") or "").strip()
    if user:
        return user
    entry = _candidate_boundary(machine)
    if entry:
        source_client = entry.get("source_client") or {}
        source_user = str(source_client.get("ssh_user") or "").strip()
        if source_user:
            return source_user
    alias = str(machine.get("ssh_alias") or "").strip()
    if "@" in alias:
        return alias.split("@", 1)[0]
    return None


def _remote_python_executable(machine: dict[str, Any]) -> str:
    hermes_path = str(machine.get("hermes_path") or machine.get("hermes_home") or "~/.hermes").rstrip("/")
    if hermes_path.startswith("~/"):
        hermes_path = "$HOME/" + hermes_path[2:]
    return f"{hermes_path}/hermes-agent/venv/bin/python"


def _candidate_boundary(machine: dict[str, Any]) -> dict[str, Any] | None:
    boundaries = _read_boundaries()
    owner_guess = f"spark-{str(machine.get('id', '')).split('-')[0]}"
    label = str(machine.get("label") or "").strip().lower()
    for entry in boundaries.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("owner") == owner_guess:
            return entry
        src = entry.get("source_client") or {}
        if str(src.get("label") or "").strip().lower() == label and label:
            return entry
    return None


def _spark_hosted_user(machine: dict[str, Any]) -> str | None:
    entry = _candidate_boundary(machine)
    if not entry:
        return None
    source_client = entry.get("source_client") or {}
    if str(source_client.get("machine") or "").strip().lower() != "spark":
        return None
    owner = str(entry.get("owner") or "").strip()
    return owner or None


def _spark_hosted_local_config(machine: dict[str, Any]) -> Path | None:
    entry = _candidate_boundary(machine)
    if not entry:
        return None
    source_client = entry.get("source_client") or {}
    if str(source_client.get("machine") or "").strip().lower() != "spark":
        return None
    runtime = ((entry.get("runtime_notes") or {}).get("hermes_runtime")) or {}
    artifacts = runtime.get("required_runtime_artifacts") or []
    for artifact in artifacts:
        artifact_str = str(artifact)
        if artifact_str.endswith("/config.yaml"):
            return Path(artifact_str).expanduser()
    owner = str(entry.get("owner") or "").strip()
    if owner:
        return Path(f"/home/{owner}/.hermes/config.yaml")
    return None


def _ssh_destinations(machine: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("ssh_alias", "tailscale_ip"):
        value = str(machine.get(key) or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    if not candidates:
        raise ValueError(f"machine {machine.get('id')} has no SSH destination")
    return candidates


def _run_remote_python(machine: dict[str, Any], script: str) -> str:
    if machine.get("os") == "windows":
        raise ValueError("windows hosts are not supported by fleet-rollout; use windows-runtime")
    ssh_user = _ssh_user(machine)
    last_detail = "remote command failed"
    for dest in _ssh_destinations(machine):
        remote_python = _remote_python_executable(machine)
        command = [
            "ssh",
            "-o",
            "ConnectTimeout=20",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            f"{ssh_user}@{dest}" if ssh_user else dest,
            f"{remote_python} -",
        ]
        completed = subprocess.run(
            command,
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return (completed.stdout or "").strip()
        last_detail = (completed.stderr or completed.stdout).strip() or f"rc={completed.returncode}"
        fallback = command[:-1] + ["python3 -"]
        completed = subprocess.run(
            fallback,
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return (completed.stdout or "").strip()
        last_detail = (completed.stderr or completed.stdout).strip() or f"rc={completed.returncode}"
    raise ValueError(last_detail)


def _run_broker(
    machine: dict[str, Any],
    action: str,
    key_path: str,
    value: Any | None = None,
    create_backup: bool = True,
    create_missing_paths: bool = False,
) -> dict[str, Any]:
    broker = DEFAULT_BROKER.resolve()
    if not broker.exists():
        raise ValueError(f"fleet rollout broker missing: {broker}")
    user = _spark_hosted_user(machine)
    config_path = _spark_hosted_local_config(machine)
    if not user or config_path is None:
        raise ValueError(f"machine {machine.get('id')} is not broker-addressable")
    command = [
        "python3",
        str(broker),
        action,
        "--user",
        user,
        "--config",
        str(config_path),
        "--key",
        key_path,
    ]
    if action == "set":
        command.extend(["--value-json", json.dumps(value)])
        if not create_backup:
            command.append("--no-backup")
        if create_missing_paths:
            command.append("--create-missing-paths")
    elif action == "delete":
        if not create_backup:
            command.append("--no-backup")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or f"rc={completed.returncode}"
        raise ValueError(detail)
    return json.loads((completed.stdout or "").strip())


def _load_machine_yaml(machine: dict[str, Any]) -> tuple[dict[str, Any], str]:
    local_config = _spark_hosted_local_config(machine)
    if local_config is not None:
        data = _load_yaml(local_config)
        return data, str(local_config)
    config_path = _remote_config_path(machine)
    script = (
        "import yaml\n"
        "from pathlib import Path\n"
        f"p = Path({config_path!r}).expanduser()\n"
        "print(p.read_text(encoding='utf-8'))\n"
    )
    text = _run_remote_python(machine, script)
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("remote config must parse to a mapping")
    return data, config_path


def _runtime_contract_preflight(machine_id: str) -> dict[str, Any]:
    machine = _machine(machine_id)
    contract = _runtime_contract_map().get(machine_id)
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not contract:
        warnings.append(
            {
                "issue_code": "runtime_contract_missing",
                "summary": "No runtime contract declared for this machine",
            }
        )
        return {
            "ok": True,
            "machine": {
                "id": machine["id"],
                "label": machine.get("label"),
                "os": machine.get("os"),
            },
            "contract_present": False,
            "issues": issues,
            "warnings": warnings,
        }

    topology = contract.get("topology") or {}
    instances = topology.get("instances") or []
    expected_count = topology.get("expected_gateway_count")
    registry_expected = machine.get("expected_gateway_count")
    registry_contract_id = machine.get("runtime_contract_id")

    if registry_contract_id != machine_id:
        issues.append(
            {
                "issue_code": "registry_runtime_contract_link_mismatch",
                "summary": "Registry runtime_contract_id does not point at this machine contract",
                "expected": machine_id,
                "actual": registry_contract_id,
            }
        )
    if expected_count is not None and registry_expected is not None and expected_count != registry_expected:
        issues.append(
            {
                "issue_code": "registry_expected_gateway_count_mismatch",
                "summary": "Registry expected gateway count differs from runtime contract",
                "expected": expected_count,
                "actual": registry_expected,
            }
        )
    if topology.get("layout") == "dual_unix" and len(instances) < 2:
        issues.append(
            {
                "issue_code": "runtime_contract_topology_incomplete",
                "summary": "Dual-unix runtime contract does not declare at least two instances",
                "expected": "2+ instances",
                "actual": len(instances),
            }
        )
    if len(instances) > 1:
        warnings.append(
            {
                "issue_code": "multi_instance_runtime",
                "summary": "Machine has multiple runtime instances; rollout writes should be lane-aware",
                "instance_ids": [item.get("id") for item in instances],
            }
        )

    return {
        "ok": len(issues) == 0,
        "machine": {
            "id": machine["id"],
            "label": machine.get("label"),
            "os": machine.get("os"),
        },
        "contract_present": True,
        "contract": {
            "machine_id": contract.get("machine_id"),
            "topology": topology,
            "health_policy": contract.get("health_policy") or {},
            "automation_bindings": contract.get("automation_bindings") or [],
        },
        "issues": issues,
        "warnings": warnings,
    }


def _contract_primary_instance(contract: dict[str, Any]) -> dict[str, Any] | None:
    instances = ((contract.get("topology") or {}).get("instances")) or []
    if not instances:
        return None
    for item in instances:
        if item.get("role") == "primary":
            return item
    return instances[0]


def _contract_registry_projection(machine_id: str) -> dict[str, Any]:
    machine = _machine(machine_id)
    contract = _machine_runtime_contract(machine_id)
    topology = contract.get("topology") or {}
    primary = _contract_primary_instance(contract) or {}
    projection = {
        "runtime_contract_id": machine_id,
        "expected_gateway_count": topology.get("expected_gateway_count"),
        "layout": topology.get("layout"),
    }
    primary_home = primary.get("hermes_home")
    if primary_home:
        if "hermes_home" in machine:
            projection["hermes_home"] = primary_home
        elif "hermes_path" in machine:
            projection["hermes_path"] = primary_home
        else:
            projection["hermes_home"] = primary_home
    return projection


def _contract_projection(machine_id: str) -> dict[str, Any]:
    machine = _machine(machine_id)
    contract = _machine_runtime_contract(machine_id)
    topology = contract.get("topology") or {}
    health_policy = contract.get("health_policy") or {}
    bindings = contract.get("automation_bindings") or []
    instances = topology.get("instances") or []
    return {
        "machine": {
            "id": machine["id"],
            "label": machine.get("label"),
            "os": machine.get("os"),
            "ssh_alias": machine.get("ssh_alias"),
        },
        "registry_projection": _contract_registry_projection(machine_id),
        "fleet_scanner_projection": {
            "machine_id": machine_id,
            "layout": topology.get("layout"),
            "expected": topology.get("expected_gateway_count"),
            "auto_restart": health_policy.get("auto_restart"),
            "allow_expected_multi": health_policy.get("allow_expected_multi"),
            "instance_roots": [
                {
                    "name": item.get("id"),
                    "root": item.get("hermes_home"),
                    "label": item.get("launch_label"),
                }
                for item in instances
            ],
        },
        "watchdog_projection": {
            "machine_id": machine_id,
            "instances": [
                {
                    "name": item.get("id"),
                    "hermes_home": item.get("hermes_home"),
                    "launch_label": item.get("launch_label"),
                }
                for item in instances
            ],
            "health_policy": health_policy,
        },
        "automation_projection": bindings,
    }


def _python_literal(value: Any) -> str:
    return pprint.pformat(value, width=1000, sort_dicts=False)


def _scanner_entry_updates(machine_id: str) -> dict[str, Any]:
    projection = _contract_projection(machine_id)["fleet_scanner_projection"]
    updates = {
        "layout": projection.get("layout"),
        "expected": projection.get("expected"),
        "auto_restart": projection.get("auto_restart"),
    }
    instance_roots = projection.get("instance_roots") or []
    if instance_roots:
        updates["instance_roots"] = instance_roots
    elif projection.get("layout") == "dual_unix":
        updates["instance_roots"] = []
    return {key: value for key, value in updates.items() if value is not None}


def _find_scanner_machine_line(lines: list[str], machine_id: str) -> int:
    needle = f'"id": "{machine_id}"'
    for idx, line in enumerate(lines):
        if needle in line:
            return idx
    raise ValueError(f"machine_id not found in fleet scanner source: {machine_id}")


def _apply_fleet_scanner_projection(machine_id: str, *, create_backup: bool = True) -> dict[str, Any]:
    path = _fleet_scanner_path()
    if not path.exists():
        raise ValueError(f"fleet scanner missing: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = _find_scanner_machine_line(lines, machine_id)
    before_line = lines[idx]
    line = before_line.rstrip()
    trailing_comma = "," if line.endswith(",") else ""
    payload = line[:-1] if trailing_comma else line
    before = ast.literal_eval(payload.strip())
    if not isinstance(before, dict):
        raise ValueError(f"fleet scanner entry must be a dict literal: {machine_id}")
    after = dict(before)
    after.update(_scanner_entry_updates(machine_id))
    lines[idx] = f"    {_python_literal(after)}{trailing_comma}"
    backup_path = None
    if create_backup:
        backup = _backup_path(path)
        shutil.copy2(path, backup)
        backup_path = str(backup)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "machine_id": machine_id,
        "fleet_scanner_path": str(path),
        "backup_path": backup_path,
        "before": before,
        "after": after,
        "applied_projection": _scanner_entry_updates(machine_id),
    }


def _watchdog_service_label(machine: dict[str, Any], contract: dict[str, Any]) -> str:
    layout = str((contract.get("topology") or {}).get("layout") or "")
    if str(machine.get("os") or "").lower() == "mac" and layout == "dual_unix":
        return "com.hermes.gateway-watchdog-client"
    return "com.hermes.gateway-watchdog"


def _launch_agent_path(label: str, home_root: str) -> str:
    user_home = str(Path(home_root).expanduser().parent)
    return str(Path(user_home) / "Library" / "LaunchAgents" / f"{label}.plist")


def _runtime_expectation_projection(machine_id: str) -> dict[str, Any]:
    machine = _machine(machine_id)
    contract = _machine_runtime_contract(machine_id)
    topology = contract.get("topology") or {}
    health_policy = contract.get("health_policy") or {}
    instances = topology.get("instances") or []
    primary = _contract_primary_instance(contract) or {}
    primary_home = str(primary.get("hermes_home") or machine.get("hermes_home") or machine.get("hermes_path") or "~/.hermes")
    watchdog_label = _watchdog_service_label(machine, contract)
    gateway_services = []
    for item in instances:
        hermes_home = str(item.get("hermes_home") or primary_home)
        label = str(item.get("launch_label") or "")
        env = {
            "HERMES_HOME": hermes_home,
            "VIRTUAL_ENV": f"{hermes_home}/hermes-agent/venv",
        }
        gateway_services.append(
            {
                "instance_id": item.get("id"),
                "role": item.get("role"),
                "label": label,
                "plist_path": _launch_agent_path(label, hermes_home) if label else None,
                "working_directory": hermes_home,
                "program_arguments": [f"{hermes_home}/start-hermes.sh"],
                "stdout_path": f"{hermes_home}/logs/gateway.log",
                "stderr_path": f"{hermes_home}/logs/gateway.error.log",
                "environment": env,
                "token_lane": item.get("token_lane"),
            }
        )
    automation_services = []
    for binding in contract.get("automation_bindings") or []:
        script_path = str(binding.get("script_path") or "")
        workspace_root = str(binding.get("workspace_root") or "")
        bound_instance_id = binding.get("bound_instance_id")
        bound_instance = next((item for item in instances if item.get("id") == bound_instance_id), None) or {}
        hermes_home = str(bound_instance.get("hermes_home") or primary_home)
        label = str(binding.get("launch_label") or "")
        automation_services.append(
            {
                "binding_id": binding.get("id"),
                "label": label,
                "kind": binding.get("kind"),
                "bound_instance_id": bound_instance_id,
                "plist_path": _launch_agent_path(label, hermes_home) if label else None,
                "program_arguments": ["/bin/bash", script_path] if script_path else [],
                "working_directory": workspace_root or hermes_home,
                "environment": {
                    "HERMES_HOME": hermes_home,
                },
                "script_path": script_path,
                "workspace_root": workspace_root,
            }
        )
    return {
        "machine_id": machine_id,
        "source_runtime_contract_id": machine_id,
        "machine": {
            "id": machine["id"],
            "label": machine.get("label"),
            "os": machine.get("os"),
            "ssh_alias": machine.get("ssh_alias"),
        },
        "topology": {
            "layout": topology.get("layout"),
            "expected_gateway_count": topology.get("expected_gateway_count"),
            "instances": [
                {
                    "id": item.get("id"),
                    "role": item.get("role"),
                    "hermes_home": item.get("hermes_home"),
                    "launch_label": item.get("launch_label"),
                    "token_lane": item.get("token_lane"),
                }
                for item in instances
            ],
        },
        "darwin_launchd": {
            "gateway_services": gateway_services,
            "watchdog_service": {
                "label": watchdog_label,
                "plist_path": _launch_agent_path(watchdog_label, primary_home),
                "program_arguments": ["/bin/bash", f"{primary_home}/bin/hermes-gateway-watchdog-client.sh"],
                "working_directory": primary_home,
                "stdout_path": f"{primary_home}/logs/watchdog.log",
                "stderr_path": f"{primary_home}/logs/watchdog.err.log",
                "interval_seconds": 120,
            }
            if str(machine.get("os") or "").lower() == "mac"
            else None,
            "automation_services": automation_services,
        },
        "watchdog_expectations": {
            "controller_home": primary_home,
            "script_path": f"{primary_home}/bin/hermes-gateway-watchdog-client.sh",
            "service_label": watchdog_label,
            "allow_expected_multi": health_policy.get("allow_expected_multi"),
            "auto_restart": health_policy.get("auto_restart"),
            "restart_strategy": health_policy.get("restart_strategy"),
            "distinct_token_lanes_required": health_policy.get("require_distinct_bot_tokens"),
            "instances": [
                {
                    "id": item.get("id"),
                    "hermes_home": item.get("hermes_home"),
                    "launch_label": item.get("launch_label"),
                    "state_dir": f"{item.get('hermes_home')}/state",
                    "log_dir": f"{item.get('hermes_home')}/logs",
                    "token_lane": item.get("token_lane"),
                }
                for item in instances
            ],
        },
        "automation_bindings": contract.get("automation_bindings") or [],
    }


def _apply_runtime_expectation_projection(machine_id: str, *, create_backup: bool = True) -> dict[str, Any]:
    path = _runtime_expectations_path()
    data = _read_runtime_expectations()
    machines = dict(data.get("machines") or {})
    before = machines.get(machine_id)
    after = _runtime_expectation_projection(machine_id)
    machines[machine_id] = after
    data["version"] = data.get("version") or 1
    data["updated"] = datetime.now(timezone.utc).date().isoformat()
    data["machines"] = machines
    backup_path = None
    if create_backup and path.exists():
        backup = _backup_path(path)
        shutil.copy2(path, backup)
        backup_path = str(backup)
    _write_runtime_expectations(data)
    return {
        "machine_id": machine_id,
        "runtime_expectations_path": str(path),
        "backup_path": backup_path,
        "before": before,
        "after": after,
    }


def _apply_registry_projection(machine_id: str, *, create_backup: bool = True) -> dict[str, Any]:
    data, idx, before = _machine_index(machine_id)
    machines = list(data.get("machines", []))
    after = dict(before)
    projection = _contract_registry_projection(machine_id)
    after.update({k: v for k, v in projection.items() if v is not None})
    backup_path = None
    path = _registry_path()
    if create_backup and path.exists():
        backup = _backup_path(path)
        shutil.copy2(path, backup)
        backup_path = str(backup)
    machines[idx] = after
    data["machines"] = machines
    _write_registry(data)
    return {
        "machine_id": machine_id,
        "registry_path": str(path),
        "backup_path": backup_path,
        "before": before,
        "after": after,
        "applied_projection": projection,
    }


@mcp.tool()
def fleet_rollout_healthcheck() -> dict[str, Any]:
    return {
        "ok": True,
        "server": SERVER_NAME,
        "hermes_home": str(HERMES_HOME),
        "allowed_root": str(_allowed_root()),
        "default_config": str(DEFAULT_CONFIG),
        "default_config_exists": DEFAULT_CONFIG.exists(),
        "registry_path": str(_registry_path()),
        "registry_exists": _registry_path().exists(),
        "runtime_contracts_path": str(_runtime_contracts_path()),
        "runtime_contracts_exists": _runtime_contracts_path().exists(),
        "runtime_expectations_path": str(_runtime_expectations_path()),
        "runtime_expectations_exists": _runtime_expectations_path().exists(),
        "fleet_scanner_path": str(_fleet_scanner_path()),
        "fleet_scanner_exists": _fleet_scanner_path().exists(),
        "boundaries_path": str(DEFAULT_BOUNDARIES.resolve()),
        "boundaries_exists": DEFAULT_BOUNDARIES.resolve().exists(),
        "broker_path": str(DEFAULT_BROKER.resolve()),
        "broker_exists": DEFAULT_BROKER.resolve().exists(),
        "promotion_canary_writer_path": str(DEFAULT_PROMOTION_CANARY_WRITER.resolve()),
        "promotion_canary_writer_exists": DEFAULT_PROMOTION_CANARY_WRITER.resolve().exists(),
    }


@mcp.tool()
def fleet_rollout_backup_file(path: str | None = None) -> dict[str, Any]:
    target = _resolve_allowed_path(path)
    if not target.exists():
        raise ValueError(f"path missing: {target}")
    backup = _backup_path(target)
    shutil.copy2(target, backup)
    return {
        "ok": True,
        "path": str(target),
        "backup_path": str(backup),
    }


@mcp.tool()
def fleet_rollout_runtime_contract_status() -> dict[str, Any]:
    """Return rollout visibility into the runtime-contract file."""
    data = _read_runtime_contracts()
    return {
        "ok": True,
        "runtime_contracts_path": str(_runtime_contracts_path()),
        "runtime_contract_count": len(data.get("contracts", [])),
    }


@mcp.tool()
def fleet_rollout_get_machine_runtime_contract(machine_id: str) -> dict[str, Any]:
    """Return the runtime contract for one machine as rollout sees it."""
    machine = _machine(machine_id)
    contract = _machine_runtime_contract(machine_id)
    return {
        "ok": True,
        "machine": {
            "id": machine["id"],
            "label": machine.get("label"),
            "os": machine.get("os"),
            "runtime_contract_id": machine.get("runtime_contract_id"),
        },
        "contract_path": str(_runtime_contracts_path()),
        "contract": contract,
    }


@mcp.tool()
def fleet_rollout_verify_machine_runtime_contract(machine_id: str) -> dict[str, Any]:
    """Preflight one machine's runtime contract before rollout actions."""
    return _runtime_contract_preflight(machine_id)


@mcp.tool()
def fleet_rollout_build_machine_runtime_projection(machine_id: str) -> dict[str, Any]:
    """Build the concrete registry/scanner/watchdog projection for one machine from its runtime contract."""
    return {
        "ok": True,
        "projection": _contract_projection(machine_id),
    }


@mcp.tool()
def fleet_rollout_build_machine_runtime_expectations(machine_id: str) -> dict[str, Any]:
    """Build the generated watchdog/launchd expectation payload for one machine."""
    return {
        "ok": True,
        "expectations": _runtime_expectation_projection(machine_id),
    }


@mcp.tool()
def fleet_rollout_apply_machine_runtime_registry(
    machine_id: str,
    create_backup: bool = True,
) -> dict[str, Any]:
    """Apply one machine's runtime contract back into the shared fleet registry metadata."""
    preflight = _runtime_contract_preflight(machine_id)
    if not preflight["contract_present"]:
        raise ValueError(f"runtime contract missing for machine_id: {machine_id}")
    payload = _apply_registry_projection(machine_id, create_backup=create_backup)
    return {
        "ok": True,
        "preflight": preflight,
        **payload,
    }


@mcp.tool()
def fleet_rollout_apply_machine_runtime_expectations(
    machine_id: str,
    create_backup: bool = True,
) -> dict[str, Any]:
    """Apply one machine's runtime contract into the generated runtime-expectations registry."""
    preflight = _runtime_contract_preflight(machine_id)
    if not preflight["contract_present"]:
        raise ValueError(f"runtime contract missing for machine_id: {machine_id}")
    payload = _apply_runtime_expectation_projection(machine_id, create_backup=create_backup)
    return {
        "ok": True,
        "preflight": preflight,
        **payload,
    }


@mcp.tool()
def fleet_rollout_apply_machine_runtime_fleet_scanner(
    machine_id: str,
    create_backup: bool = True,
) -> dict[str, Any]:
    """Apply one machine's runtime contract back into the golden fleet scanner source."""
    preflight = _runtime_contract_preflight(machine_id)
    if not preflight["contract_present"]:
        raise ValueError(f"runtime contract missing for machine_id: {machine_id}")
    payload = _apply_fleet_scanner_projection(machine_id, create_backup=create_backup)
    return {
        "ok": True,
        "preflight": preflight,
        **payload,
    }


@mcp.tool()
def fleet_rollout_apply_machine_runtime_projection(
    machine_id: str,
    create_backup: bool = True,
) -> dict[str, Any]:
    """Apply one machine's runtime contract into bounded local rollout artifacts."""
    preflight = _runtime_contract_preflight(machine_id)
    if not preflight["contract_present"]:
        raise ValueError(f"runtime contract missing for machine_id: {machine_id}")
    registry_payload = _apply_registry_projection(machine_id, create_backup=create_backup)
    expectations_payload = _apply_runtime_expectation_projection(machine_id, create_backup=create_backup)
    scanner_payload = _apply_fleet_scanner_projection(machine_id, create_backup=create_backup)
    return {
        "ok": True,
        "preflight": preflight,
        "registry": registry_payload,
        "runtime_expectations": expectations_payload,
        "fleet_scanner": scanner_payload,
    }


@mcp.tool()
def fleet_rollout_record_runtime_canary(
    machine_id: str,
    repo_path: str,
    target_ref: str = "HEAD",
    status: str = "verified",
    check: str = "gateway_startup",
    detail: str = "",
    host_override: str | None = None,
) -> dict[str, Any]:
    """Record one bounded promotion canary result using the machine's inferred runtime lane."""
    machine = _machine(machine_id)
    payload = _record_runtime_canary(
        machine=machine,
        repo_path=repo_path,
        target_ref=target_ref,
        status=status,
        check=check,
        detail=detail,
        host_override=host_override,
    )
    return {
        "ok": True,
        "repo_path": str(_resolve_repo_path(repo_path)),
        "target_ref": target_ref,
        **payload,
    }


@mcp.tool()
def fleet_rollout_record_runtime_canary_set(
    repo_path: str,
    spark_machine_id: str,
    mac_machine_id: str,
    windows_machine_id: str,
    target_ref: str = "HEAD",
    status: str = "verified",
    check: str = "gateway_startup",
    detail: str = "",
) -> dict[str, Any]:
    """Record Spark, Mac, and Windows promotion canary proof for one candidate SHA."""
    assignments = [
        ("spark", _machine(spark_machine_id), spark_machine_id),
        ("mac", _machine(mac_machine_id), mac_machine_id),
        ("windows", _machine(windows_machine_id), windows_machine_id),
    ]
    results = []
    for expected_lane, machine, machine_id in assignments:
        actual_lane = _machine_canary_lane(machine)
        if actual_lane != expected_lane:
            raise ValueError(
                f"machine {machine_id} does not match required lane {expected_lane}; got {actual_lane}"
            )
        results.append(
            _record_runtime_canary(
                machine=machine,
                repo_path=repo_path,
                target_ref=target_ref,
                status=status,
                check=check,
                detail=detail,
            )
        )
    first = results[0]
    return {
        "ok": True,
        "repo_path": str(_resolve_repo_path(repo_path)),
        "target_ref": target_ref,
        "status": status,
        "check": check,
        "detail": detail,
        "output": first.get("output"),
        "latest": first.get("latest"),
        "results": results,
    }


@mcp.tool()
def fleet_rollout_get_config_value(key_path: str, path: str | None = None) -> dict[str, Any]:
    target = _resolve_allowed_path(path)
    data = _load_yaml(target)
    value = _get_nested(data, key_path)
    return {
        "ok": True,
        "path": str(target),
        "key_path": key_path,
        "value": value,
    }


@mcp.tool()
def fleet_rollout_set_config_value(
    key_path: str,
    value: Any,
    path: str | None = None,
    create_backup: bool = True,
    create_missing_paths: bool = False,
) -> dict[str, Any]:
    target = _resolve_allowed_path(path)
    data = _load_yaml(target)
    before = None
    try:
        before = _get_nested(data, key_path)
    except ValueError:
        before = None
    backup_path = None
    if create_backup:
        backup = _backup_path(target)
        shutil.copy2(target, backup)
        backup_path = str(backup)
    _set_nested(data, key_path, value, create_missing_paths=create_missing_paths)
    _write_yaml(target, data)
    after = _get_nested(data, key_path)
    return {
        "ok": True,
        "path": str(target),
        "key_path": key_path,
        "before": before,
        "after": after,
        "backup_path": backup_path,
    }


@mcp.tool()
def fleet_rollout_delete_config_value(
    key_path: str,
    path: str | None = None,
    create_backup: bool = True,
) -> dict[str, Any]:
    target = _resolve_allowed_path(path)
    data = _load_yaml(target)
    before = _delete_nested(data, key_path)
    backup_path = None
    if create_backup:
        backup = _backup_path(target)
        shutil.copy2(target, backup)
        backup_path = str(backup)
    _write_yaml(target, data)
    return {
        "ok": True,
        "path": str(target),
        "key_path": key_path,
        "before": before,
        "deleted": True,
        "backup_path": backup_path,
    }


@mcp.tool()
def fleet_rollout_verify_postcondition(
    key_path: str,
    expected_value: Any,
    path: str | None = None,
) -> dict[str, Any]:
    target = _resolve_allowed_path(path)
    data = _load_yaml(target)
    actual = _get_nested(data, key_path)
    return {
        "ok": actual == expected_value,
        "path": str(target),
        "key_path": key_path,
        "expected_value": expected_value,
        "actual_value": actual,
    }


@mcp.tool()
def fleet_rollout_get_machine_config_value(machine_id: str, key_path: str) -> dict[str, Any]:
    machine = _machine(machine_id)
    if _spark_hosted_user(machine):
        payload = _run_broker(machine, "get", key_path)
        return {"ok": True, "machine_id": machine_id, **payload}
    data, config_path = _load_machine_yaml(machine)
    value = _get_nested(data, key_path)
    return {"ok": True, "machine_id": machine_id, "path": str(config_path), "key_path": key_path, "value": value}


@mcp.tool()
def fleet_rollout_set_machine_config_value(
    machine_id: str,
    key_path: str,
    value: Any,
    create_backup: bool = True,
    create_missing_paths: bool = False,
) -> dict[str, Any]:
    machine = _machine(machine_id)
    if _spark_hosted_user(machine):
        payload = _run_broker(
            machine,
            "set",
            key_path,
            value=value,
            create_backup=create_backup,
            create_missing_paths=create_missing_paths,
        )
        return {"ok": True, "machine_id": machine_id, **payload}
    config_path = _remote_config_path(machine)
    parts = _split_key(key_path)
    script = (
        "import json, shutil, yaml\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        f"p = Path({config_path!r}).expanduser()\n"
        "data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}\n"
        f"parts = {parts!r}\n"
        "parts_label = '.'.join(parts)\n"
        f"value = json.loads({json.dumps(json.dumps(value))})\n"
        f"create_missing_paths = {create_missing_paths!r}\n"
        "cur = data\n"
        "before = None\n"
        "probe = data\n"
        "exists = True\n"
        "for part in parts:\n"
        "    if not isinstance(probe, dict) or part not in probe:\n"
        "        exists = False\n"
        "        break\n"
        "    probe = probe[part]\n"
        "if exists:\n"
        "    before = probe\n"
        "for part in parts[:-1]:\n"
        "    child = cur.get(part)\n"
        "    if child is None:\n"
        "        if not create_missing_paths:\n"
        "            raise SystemExit('missing config path: ' + parts_label)\n"
        "        child = {}\n"
        "        cur[part] = child\n"
        "    if not isinstance(child, dict):\n"
        "        raise SystemExit(f'non-mapping encountered at {part}')\n"
        "    cur = child\n"
        "if parts[-1] not in cur and not create_missing_paths:\n"
        "    raise SystemExit('missing config path: ' + parts_label)\n"
        "cur[parts[-1]] = value\n"
        "backup_path = None\n"
        f"if {create_backup!r}:\n"
        "    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')\n"
        "    backup = p.with_name(f'{p.name}.bak-{stamp}')\n"
        "    shutil.copy2(p, backup)\n"
        "    backup_path = str(backup)\n"
        "p.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')\n"
        "print(json.dumps({'path': str(p), 'key_path': '.'.join(parts), 'before': before, 'after': value, 'backup_path': backup_path}))\n"
    )
    payload = json.loads(_run_remote_python(machine, script))
    return {"ok": True, "machine_id": machine_id, **payload}


@mcp.tool()
def fleet_rollout_delete_machine_config_value(
    machine_id: str,
    key_path: str,
    create_backup: bool = True,
) -> dict[str, Any]:
    machine = _machine(machine_id)
    if _spark_hosted_user(machine):
        payload = _run_broker(
            machine,
            "delete",
            key_path,
            create_backup=create_backup,
        )
        return {"ok": True, "machine_id": machine_id, **payload}
    config_path = _remote_config_path(machine)
    parts = _split_key(key_path)
    script = (
        "import json, shutil, yaml\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        f"p = Path({config_path!r}).expanduser()\n"
        "data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}\n"
        f"parts = {parts!r}\n"
        "parts_label = '.'.join(parts)\n"
        "cur = data\n"
        "for part in parts[:-1]:\n"
        "    if not isinstance(cur, dict) or part not in cur:\n"
        "        raise SystemExit('missing config path: ' + parts_label)\n"
        "    cur = cur[part]\n"
        "if not isinstance(cur, dict) or parts[-1] not in cur:\n"
        "    raise SystemExit('missing config path: ' + parts_label)\n"
        "before = cur[parts[-1]]\n"
        "backup_path = None\n"
        f"if {create_backup!r}:\n"
        "    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')\n"
        "    backup = p.with_name(f'{p.name}.bak-{stamp}')\n"
        "    shutil.copy2(p, backup)\n"
        "    backup_path = str(backup)\n"
        "del cur[parts[-1]]\n"
        "p.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')\n"
        "print(json.dumps({'path': str(p), 'key_path': '.'.join(parts), 'before': before, 'deleted': True, 'backup_path': backup_path}))\n"
    )
    payload = json.loads(_run_remote_python(machine, script))
    return {"ok": True, "machine_id": machine_id, **payload}


@mcp.tool()
def fleet_rollout_verify_machine_postcondition(
    machine_id: str,
    key_path: str,
    expected_value: Any,
) -> dict[str, Any]:
    result = fleet_rollout_get_machine_config_value(machine_id=machine_id, key_path=key_path)
    actual = result["value"]
    return {
        "ok": actual == expected_value,
        "machine_id": machine_id,
        "path": result["path"],
        "key_path": key_path,
        "expected_value": expected_value,
        "actual_value": actual,
    }


@mcp.tool()
def fleet_rollout_get_cohort_config_value(
    key_path: str,
    machine_ids: list[str] | None = None,
    os_filter: str | None = None,
    include_windows: bool = False,
) -> dict[str, Any]:
    machines = _select_machines(machine_ids, os_filter=os_filter, include_windows=include_windows)
    results = [
        _cohort_result(machine["id"], lambda m=machine: fleet_rollout_get_machine_config_value(m["id"], key_path))
        for machine in machines
    ]
    return {
        "key_path": key_path,
        "selected_machine_ids": [machine["id"] for machine in machines],
        **_summarize_cohort(results),
    }


@mcp.tool()
def fleet_rollout_set_cohort_config_value(
    key_path: str,
    value: Any,
    machine_ids: list[str] | None = None,
    os_filter: str | None = None,
    include_windows: bool = False,
    create_backup: bool = True,
    create_missing_paths: bool = False,
) -> dict[str, Any]:
    machines = _select_machines(machine_ids, os_filter=os_filter, include_windows=include_windows)
    results = [
        _cohort_result(
            machine["id"],
            lambda m=machine: fleet_rollout_set_machine_config_value(
                m["id"],
                key_path,
                value,
                create_backup=create_backup,
                create_missing_paths=create_missing_paths,
            ),
        )
        for machine in machines
    ]
    return {
        "key_path": key_path,
        "value": value,
        "selected_machine_ids": [machine["id"] for machine in machines],
        **_summarize_cohort(results),
    }


@mcp.tool()
def fleet_rollout_delete_cohort_config_value(
    key_path: str,
    machine_ids: list[str] | None = None,
    os_filter: str | None = None,
    include_windows: bool = False,
    create_backup: bool = True,
) -> dict[str, Any]:
    machines = _select_machines(machine_ids, os_filter=os_filter, include_windows=include_windows)
    results = [
        _cohort_result(
            machine["id"],
            lambda m=machine: fleet_rollout_delete_machine_config_value(
                m["id"],
                key_path,
                create_backup=create_backup,
            ),
        )
        for machine in machines
    ]
    return {
        "key_path": key_path,
        "selected_machine_ids": [machine["id"] for machine in machines],
        **_summarize_cohort(results),
    }


@mcp.tool()
def fleet_rollout_verify_cohort_postcondition(
    key_path: str,
    expected_value: Any,
    machine_ids: list[str] | None = None,
    os_filter: str | None = None,
    include_windows: bool = False,
) -> dict[str, Any]:
    machines = _select_machines(machine_ids, os_filter=os_filter, include_windows=include_windows)
    results = [
        _cohort_result(
            machine["id"],
            lambda m=machine: fleet_rollout_verify_machine_postcondition(
                m["id"],
                key_path,
                expected_value,
            ),
        )
        for machine in machines
    ]
    return {
        "key_path": key_path,
        "expected_value": expected_value,
        "selected_machine_ids": [machine["id"] for machine in machines],
        **_summarize_cohort(results),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
