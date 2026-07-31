#!/usr/bin/env python3
"""Ensure Golden's pinned Cua Driver through the exact Hermes runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "kit" / "config" / "cua-driver-release-v1.json"
VERSION_RE = re.compile(r"(?:cua-driver\s+)?(\d+(?:\.\d+){2,})", re.IGNORECASE)
SENSITIVE_ENV_RE = re.compile(
    r"(?:^|_)(?:api_?key|auth|credential|password|secret|token)(?:_|$)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    release = data.get("release") if isinstance(data, dict) else None
    installer = data.get("installer") if isinstance(data, dict) else None
    acceptance = data.get("acceptance") if isinstance(data, dict) else None
    if (
        data.get("schema_version") != 1
        or not isinstance(release, dict)
        or not re.fullmatch(r"\d+(?:\.\d+){2,}", str(release.get("version", "")))
        or release.get("tag") != f"cua-driver-rs-v{release.get('version')}"
        or not isinstance(data.get("assets"), dict)
        or not isinstance(installer, dict)
        or installer.get("method") != "native_hermes_computer_use_install"
        or installer.get("pin_environment") != "CUA_DRIVER_RS_VERSION"
        or not isinstance(acceptance, dict)
        or acceptance.get("baseline") != "exact_version_present"
    ):
        raise ValueError("invalid Cua Driver release contract")
    return data


def platform_asset_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"amd64", "x64", "x86_64"}:
        arch = "x86_64"
    else:
        raise ValueError(f"unsupported architecture: {machine or 'unknown'}")
    if system == "darwin":
        return "macos-universal"
    if system == "windows":
        return f"windows-{arch}"
    if system == "linux":
        return f"linux-{arch}"
    raise ValueError(f"unsupported platform: {system or 'unknown'}")


def child_env(home: Path, contract: dict[str, Any]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not SENSITIVE_ENV_RE.search(key)
    }
    installer = contract["installer"]
    env["HERMES_HOME"] = str(home)
    env[installer["pin_environment"]] = contract["release"]["version"]
    env[installer.get("telemetry_environment", "CUA_DRIVER_RS_TELEMETRY_ENABLED")] = str(
        installer.get("telemetry_default", "0")
    )
    return env


PROBE_CODE = r'''
import json
import os
import re
import subprocess
from tools.computer_use.cua_backend import cua_driver_child_env, resolve_cua_driver_cmd

path = resolve_cua_driver_cmd()
payload = {"installed": False, "path": path, "version": None, "version_output": None}
if path:
    proc = subprocess.run(
        [path, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        env=cua_driver_child_env(),
    )
    output = (proc.stdout or proc.stderr or "").strip()
    match = re.search(r"(?:cua-driver\s+)?(\d+(?:\.\d+){2,})", output, re.IGNORECASE)
    payload.update({
        "installed": proc.returncode == 0 and bool(match),
        "version": match.group(1) if match else None,
        "version_output": output[:200],
    })
print(json.dumps(payload, sort_keys=True))
'''


def run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )


def probe_driver(
    python: Path,
    *,
    env: dict[str, str],
) -> dict[str, Any]:
    proc = run([str(python), "-c", PROBE_CODE], env=env, timeout=20)
    if proc.returncode != 0:
        return {
            "installed": False,
            "path": None,
            "version": None,
            "probe_error": (proc.stderr or proc.stdout).strip()[-500:],
        }
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {
            "installed": False,
            "path": None,
            "version": None,
            "probe_error": "driver probe returned invalid JSON",
        }
    return payload if isinstance(payload, dict) else {"installed": False}


def parse_doctor(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def ensure_driver(
    *,
    python: Path,
    home: Path,
    contract_path: Path,
    dry_run: bool = False,
    require_ready: bool = False,
    system: str | None = None,
    machine: str | None = None,
) -> tuple[int, dict[str, Any]]:
    contract = load_contract(contract_path)
    asset_key = platform_asset_key(system, machine)
    asset = contract["assets"].get(asset_key)
    if not isinstance(asset, dict) or not re.fullmatch(
        r"[0-9a-f]{64}", str(asset.get("sha256", ""))
    ):
        raise ValueError(f"release contract has no valid asset for {asset_key}")
    if not python.is_file():
        raise ValueError(f"Hermes runtime Python is missing: {python}")

    env = child_env(home, contract)
    wanted = contract["release"]["version"]
    before = probe_driver(python, env=env)
    exact_before = before.get("installed") is True and before.get("version") == wanted
    install_attempted = False
    install_returncode: int | None = None

    if not exact_before and not dry_run:
        install_attempted = True
        proc = run(
            [
                str(python),
                "-m",
                "hermes_cli.main",
                "computer-use",
                "install",
                "--upgrade",
            ],
            env=env,
            timeout=900,
        )
        install_returncode = proc.returncode

    after = before if dry_run or exact_before else probe_driver(python, env=env)
    exact_after = after.get("installed") is True and after.get("version") == wanted

    doctor_payload = None
    doctor_returncode: int | None = None
    if exact_after:
        doctor = run(
            [
                str(python),
                "-m",
                "hermes_cli.main",
                "computer-use",
                "doctor",
                "--json",
            ],
            env=env,
            timeout=120,
        )
        doctor_returncode = doctor.returncode
        doctor_payload = parse_doctor(doctor.stdout)

    ready = (
        doctor_returncode == 0
        and isinstance(doctor_payload, dict)
        and doctor_payload.get("ok") is not False
        and doctor_payload.get("overall") not in {"degraded", "error", "failed"}
    )
    if dry_run:
        status = "idempotent" if exact_before else "would_install"
        ok = True
    elif not exact_after:
        status = "failed"
        ok = False
    elif require_ready and not ready:
        status = "blocked_not_ready"
        ok = False
    else:
        status = "idempotent" if exact_before else "installed"
        ok = True

    receipt = {
        "schema_version": 1,
        "kind": "cua_driver_install_receipt",
        "generated_at": utc_now(),
        "ok": ok,
        "status": status,
        "dry_run": dry_run,
        "require_ready": require_ready,
        "hermes_home": str(home),
        "hermes_python": str(python),
        "release": contract["release"],
        "asset": {"key": asset_key, **asset},
        "before": before,
        "after": after,
        "install_attempted": install_attempted,
        "install_returncode": install_returncode,
        "doctor_ready": ready,
        "doctor_returncode": doctor_returncode,
        "doctor": doctor_payload,
    }
    return (0 if ok else 1), receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-python", required=True, type=Path)
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        code, receipt = ensure_driver(
            python=args.hermes_python.expanduser(),
            home=args.hermes_home.expanduser(),
            contract_path=args.contract.expanduser(),
            dry_run=args.dry_run,
            require_ready=args.require_ready,
        )
    except Exception as exc:
        code = 1
        receipt = {
            "schema_version": 1,
            "kind": "cua_driver_install_receipt",
            "generated_at": utc_now(),
            "ok": False,
            "status": "failed",
            "error": str(exc),
        }

    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
