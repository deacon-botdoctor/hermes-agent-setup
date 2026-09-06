#!/usr/bin/env python3
"""Read-only preflight for every enabled inference cron's effective toolsets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


RUNTIME_PROBE = r'''
import json
import sys
from pathlib import Path

from agent.outcome_stop import unknown_requested_toolsets
from cron.scheduler import _resolve_cron_enabled_toolsets
from hermes_cli.config import load_config
from hermes_cli.plugins import discover_plugins
from hermes_cli.tools_config import enabled_mcp_server_names
from tools.registry import discover_builtin_tools, registry

jobs_path = Path(sys.argv[1])
payload = json.loads(jobs_path.read_text(encoding="utf-8-sig")) if jobs_path.is_file() else {"jobs": []}
discover_plugins()
discover_builtin_tools()
config = load_config() or {}
configured_mcp = enabled_mcp_server_names(config)
rows = []
for job in payload.get("jobs") or []:
    if not job.get("enabled") or job.get("no_agent"):
        continue
    requested = _resolve_cron_enabled_toolsets(job, config)
    unknown = unknown_requested_toolsets(
        requested,
        registry.get_registered_toolset_names(),
        registry.get_registered_toolset_aliases(),
        configured_mcp,
    )
    rows.append({
        "job_id": str(job.get("id") or ""),
        "name": str(job.get("name") or ""),
        "requested_toolsets": requested,
        "unknown_toolsets": unknown,
    })
print(json.dumps({"jobs": rows}, separators=(",", ":"), sort_keys=True))
'''


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("runtime probe returned no JSON object")


def resolve_runtime(hermes_home: Path) -> tuple[Path, Path]:
    binding = load_json(hermes_home / "state/runtime-binding.json")
    runtime_root_raw = str(binding.get("runtime_root") or "").strip()
    runtime_python_raw = str(binding.get("runtime_python") or "").strip()
    runtime_root = Path(runtime_root_raw).expanduser()
    runtime_python = Path(runtime_python_raw).expanduser()
    if not runtime_root_raw or not runtime_root.is_dir():
        raise ValueError("active runtime binding has no usable runtime_root")
    if not runtime_python_raw or not runtime_python.is_file():
        raise ValueError("active runtime binding has no usable runtime_python")
    return runtime_root, runtime_python


def evaluate(hermes_home: Path) -> dict:
    report = {
        "schema_version": 1,
        "kind": "hermes_cron_toolset_preflight",
        "status": "fail",
        "hermes_home": str(hermes_home),
        "enabled_inference_jobs": 0,
        "jobs_with_unknown_toolsets": 0,
        "findings": [],
    }
    try:
        runtime_root, runtime_python = resolve_runtime(hermes_home)
        with tempfile.TemporaryDirectory(prefix="hermes-cron-toolset-preflight-") as raw:
            probe_home = Path(raw)
            config_path = hermes_home / "config.yaml"
            if config_path.is_file():
                shutil.copy2(config_path, probe_home / "config.yaml")
            plugins_path = hermes_home / "plugins"
            if plugins_path.is_dir():
                shutil.copytree(plugins_path, probe_home / "plugins", symlinks=True)
            env = os.environ.copy()
            # Runtime discovery imports every built-in/plugin tool module. Some
            # modules initialize SQLite-backed state at import time. Point them
            # at an isolated disposable home so this read-only audit never
            # opens, migrates, or rejects a live state.db; the real config and
            # enabled jobs remain the explicit audit inputs.
            env["HERMES_HOME"] = str(probe_home)
            proc = subprocess.run(
                [
                    str(runtime_python),
                    "-c",
                    RUNTIME_PROBE,
                    str(hermes_home / "cron/jobs.json"),
                ],
                cwd=runtime_root,
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
                check=False,
            )
        if proc.returncode:
            detail = (proc.stderr or proc.stdout or "runtime probe failed").strip()
            raise RuntimeError(detail[-800:])
        payload = last_json(proc.stdout)
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("runtime probe returned an invalid jobs collection")
        findings = [
            {
                "job_id": str(row.get("job_id") or ""),
                "name": str(row.get("name") or ""),
                "unknown_toolsets": list(row.get("unknown_toolsets") or []),
            }
            for row in jobs
            if row.get("unknown_toolsets")
        ]
        report.update(
            {
                "status": "pass" if not findings else "fail",
                "runtime_root": str(runtime_root),
                "enabled_inference_jobs": len(jobs),
                "jobs_with_unknown_toolsets": len(findings),
                "findings": findings,
            }
        )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:800]}"
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = evaluate(Path(args.hermes_home).expanduser())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"{report['status']}: enabled_inference_jobs={report['enabled_inference_jobs']} "
            f"jobs_with_unknown_toolsets={report['jobs_with_unknown_toolsets']}"
        )
        for finding in report["findings"]:
            print(
                f"- {finding['job_id']} {finding['name']}: "
                + ", ".join(finding["unknown_toolsets"])
            )
        if report.get("error"):
            print(report["error"], file=sys.stderr)
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
