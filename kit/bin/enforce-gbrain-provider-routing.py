#!/usr/bin/env python3
"""Enforce Bot Doctor's OpenRouter-only GBrain auxiliary provider contract.

The GBrain `balanced` search mode enables the ZeroEntropy reranker when the
database override is absent.  This helper removes the ambient ZeroEntropy
credential from the file plane and writes the explicit DB-plane reranker-off
override while preserving the existing per-runtime OpenRouter auxiliary key.

It intentionally refuses embedding model or dimension migrations.  Existing
brains must already use the approved 1536-dimensional OpenRouter model; schema
migrations require a separate, evidence-backed procedure.

Hosts without a GBrain installation are outside this contract.  They are
reported as not applicable instead of being forced into a new knowledge or
credential topology during an unrelated runtime rollout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APPROVED_MODEL = "openrouter:openai/text-embedding-3-small"
APPROVED_DIMENSIONS = 1536
RERANKER_KEY = "search.reranker.enabled"
FORBIDDEN_ENV_KEYS = {"ZEROENTROPY_API_KEY", "HERMES_ZEROENTROPY_API_KEY"}
ENV_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*="
)
ENGINE_UNAVAILABLE_MARKERS = (
    "failed to initialize its wasm runtime",
    "pglite wasm runtime",
    "original error: aborted()",
)


class RerankerEngineUnavailable(RuntimeError):
    """The local GBrain engine cannot currently read its configuration store."""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_gbrain() -> Path:
    home = Path.home()
    candidates = (
        home / ".hermes/bin/gbrain",
        home / ".hermes/bin/gbrain.cmd",
        home / ".hermes/bin/gbrain.exe",
        home / ".hermes/bin/gbrain.bat",
    )
    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink():
            return candidate
    found = shutil.which("gbrain")
    return Path(found) if found else candidates[0]


def gbrain_install_present(config_path: Path, gbrain_bin: Path) -> bool:
    return (
        config_path.parent.exists()
        or gbrain_bin.exists()
        or gbrain_bin.is_symlink()
    )


def run_gbrain(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def get_reranker_override(binary: Path) -> str | None:
    result = run_gbrain(binary, "config", "get", RERANKER_KEY)
    combined = (result.stdout + result.stderr).lower()
    if any(marker in combined for marker in ENGINE_UNAVAILABLE_MARKERS):
        raise RerankerEngineUnavailable("gbrain_engine_unavailable")
    if result.returncode == 0:
        value = result.stdout.strip().lower()
        if not value:
            raise RerankerEngineUnavailable("gbrain_engine_unavailable")
        return value
    if "not found" in combined:
        return None
    raise RuntimeError(
        f"could not inspect {RERANKER_KEY}: rc={result.returncode} "
        f"stderr={result.stderr.strip()[-240:]}"
    )


def set_reranker_override(binary: Path, value: str | None) -> None:
    args = (
        ("config", "unset", RERANKER_KEY)
        if value is None
        else ("config", "set", RERANKER_KEY, value)
    )
    result = run_gbrain(binary, *args)
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).lower()
        if value is None and "not found" in combined:
            return
        raise RuntimeError(
            f"could not write {RERANKER_KEY}: rc={result.returncode} "
            f"stderr={result.stderr.strip()[-240:]}"
        )


def load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"GBrain config missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GBrain config is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("GBrain config must be a JSON object")
    return payload


def validate_embedding_contract(config: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if config.get("embedding_model") != APPROVED_MODEL:
        failures.append("embedding_model_migration_required")
    if config.get("embedding_dimensions") != APPROVED_DIMENSIONS:
        failures.append("embedding_dimensions_migration_required")
    if not isinstance(config.get("openrouter_api_key"), str) or not config[
        "openrouter_api_key"
    ].strip():
        failures.append("openrouter_aux_key_missing")
    return failures


def atomic_write(path: Path, payload: dict[str, Any], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def forbidden_env_lines(path: Path) -> list[int]:
    if not path.is_file():
        return []
    findings: list[int] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        match = ENV_ASSIGNMENT.match(line)
        if match and match.group("key") in FORBIDDEN_ENV_KEYS:
            findings.append(number)
    return findings


def remove_forbidden_env_lines(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="strict")
    kept = []
    for line in text.splitlines(keepends=True):
        match = ENV_ASSIGNMENT.match(line)
        if not (match and match.group("key") in FORBIDDEN_ENV_KEYS):
            kept.append(line)
    mode = path.stat().st_mode & 0o777
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(kept))
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def audit(config_path: Path, gbrain_bin: Path, hermes_home: Path) -> dict[str, Any]:
    config = load_config(config_path)
    failures = validate_embedding_contract(config)
    warnings: list[str] = []
    try:
        reranker = get_reranker_override(gbrain_bin)
        reranker_observed = True
    except RerankerEngineUnavailable:
        reranker = None
        reranker_observed = False
        warnings.append("reranker_override_deferred_engine_unavailable")
    if "zeroentropy_api_key" in config:
        failures.append("zeroentropy_file_credential_present")
    if os.environ.get("ZEROENTROPY_API_KEY") or os.environ.get(
        "HERMES_ZEROENTROPY_API_KEY"
    ):
        failures.append("zeroentropy_environment_credential_present")
    env_findings = {
        str(path): forbidden_env_lines(path)
        for path in (hermes_home / ".env", hermes_home / ".env.secrets")
        if forbidden_env_lines(path)
    }
    if env_findings:
        failures.append("zeroentropy_environment_file_credential_present")
    if reranker_observed and reranker not in {"false", "0"}:
        failures.append("zeroentropy_default_reranker_reachable")
    return {
        "ok": not failures,
        "status": "compliant" if not failures else "noncompliant",
        "config": str(config_path),
        "embedding_model": config.get("embedding_model"),
        "embedding_dimensions": config.get("embedding_dimensions"),
        "openrouter_aux_key_present": bool(config.get("openrouter_api_key")),
        "zeroentropy_file_credential_present": "zeroentropy_api_key" in config,
        "zeroentropy_environment_credential_present": bool(
            os.environ.get("ZEROENTROPY_API_KEY")
            or os.environ.get("HERMES_ZEROENTROPY_API_KEY")
        ),
        "zeroentropy_environment_file_findings": env_findings,
        "reranker_override": reranker,
        "reranker_override_observed": reranker_observed,
        "failures": failures,
        "warnings": warnings,
        "credential_values_recorded": False,
    }


def apply_contract(
    config_path: Path, gbrain_bin: Path, hermes_home: Path, dry_run: bool
) -> dict[str, Any]:
    config = load_config(config_path)
    failures = validate_embedding_contract(config)
    if failures:
        return {
            "ok": False,
            "status": "blocked_migration_required",
            "failures": failures,
            "credential_values_recorded": False,
        }
    try:
        prior_reranker = get_reranker_override(gbrain_bin)
        reranker_observed = True
    except RerankerEngineUnavailable:
        prior_reranker = None
        reranker_observed = False
    file_change = "zeroentropy_api_key" in config
    env_paths = [
        path
        for path in (hermes_home / ".env", hermes_home / ".env.secrets")
        if forbidden_env_lines(path)
    ]
    reranker_change = reranker_observed and prior_reranker not in {"false", "0"}
    if not file_change and not env_paths and not reranker_change:
        result = audit(config_path, gbrain_bin, hermes_home)
        status = "idempotent" if reranker_observed else "deferred_engine_unavailable"
        return {**result, "status": status}
    if dry_run:
        return {
            "ok": True,
            "status": "would_update",
            "remove_zeroentropy_file_credential": file_change,
            "remove_zeroentropy_environment_files": [str(path) for path in env_paths],
            "disable_default_reranker": reranker_change,
            "reranker_override_observed": reranker_observed,
            "credential_values_recorded": False,
        }

    rollback_dir = config_path.parent / "rollbacks" / f"provider-routing-{utc_stamp()}"
    rollback_dir.mkdir(parents=True, mode=0o700)
    backup = rollback_dir / "config.json"
    shutil.copy2(config_path, backup)
    original_mode = config_path.stat().st_mode & 0o777
    metadata = rollback_dir / "rollback.json"
    environment_backups = []
    for path in env_paths:
        env_backup = rollback_dir / f"env-{len(environment_backups)}.backup"
        shutil.copy2(path, env_backup)
        environment_backups.append(
            {
                "path": str(path),
                "backup": str(env_backup),
                "sha256": sha256(path),
            }
        )
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config": str(config_path),
                "backup": str(backup),
                "config_sha256": sha256(config_path),
                "prior_reranker_override": prior_reranker,
                "prior_reranker_observed": reranker_observed,
                "environment_backups": environment_backups,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    updated = dict(config)
    updated.pop("zeroentropy_api_key", None)
    try:
        atomic_write(config_path, updated, original_mode)
        for path in env_paths:
            remove_forbidden_env_lines(path)
        if reranker_observed:
            set_reranker_override(gbrain_bin, "false")
        result = audit(config_path, gbrain_bin, hermes_home)
        if not result["ok"]:
            raise RuntimeError(f"post-apply audit failed: {result['failures']}")
    except Exception:
        shutil.copy2(backup, config_path)
        for item in environment_backups:
            shutil.copy2(item["backup"], item["path"])
        if reranker_observed:
            set_reranker_override(gbrain_bin, prior_reranker)
        raise
    return {
        **result,
        "status": "installed",
        "rollback": str(metadata),
        "prior_config_sha256": sha256(backup),
    }


def rollback(metadata_path: Path, gbrain_bin: Path) -> dict[str, Any]:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    config_path = Path(metadata["config"])
    backup = Path(metadata["backup"])
    if sha256(backup) != metadata["config_sha256"]:
        raise RuntimeError("rollback backup digest mismatch")
    shutil.copy2(backup, config_path)
    for item in metadata.get("environment_backups", []):
        env_backup = Path(item["backup"])
        if sha256(env_backup) != item["sha256"]:
            raise RuntimeError("environment rollback backup digest mismatch")
        shutil.copy2(env_backup, item["path"])
    if metadata.get("prior_reranker_observed", True):
        set_reranker_override(gbrain_bin, metadata.get("prior_reranker_override"))
    return {
        "ok": True,
        "status": "restored",
        "config": str(config_path),
        "credential_values_recorded": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".gbrain/config.json"
    )
    parser.add_argument("--gbrain-bin", type=Path, default=None)
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--audit", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--rollback", type=Path)
    args = parser.parse_args(argv)
    gbrain_bin = args.gbrain_bin or find_gbrain()
    try:
        if args.rollback:
            result = rollback(args.rollback, gbrain_bin)
        elif not args.config.is_file() and not gbrain_install_present(
            args.config, gbrain_bin
        ):
            result = {
                "ok": True,
                "status": "not_applicable",
                "reason": "gbrain_not_installed",
                "credential_values_recorded": False,
            }
        elif args.audit:
            result = audit(args.config, gbrain_bin, args.hermes_home)
        else:
            result = apply_contract(
                args.config, gbrain_bin, args.hermes_home, args.dry_run
            )
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "credential_values_recorded": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
