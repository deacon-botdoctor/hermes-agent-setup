#!/usr/bin/env python3
"""Run bounded, receipt-backed Codex session projection and Luna carding."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "native-session-runner/v1"
MANIFEST_SCHEMA = "native-agent-continuity-manifest/v1"
INTERVAL_SECONDS = 1800


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def regular(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"required session continuity file is missing or unsafe: {path}")
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(regular(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        raise RuntimeError("native-agent continuity manifest is invalid")
    principal = str(value.get("principal_id") or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", principal):
        raise RuntimeError("native session principal is invalid")
    paths = value.get("paths")
    if not isinstance(paths, dict):
        raise RuntimeError("native-agent continuity paths are missing")
    resolved = {}
    for name in ("home", "hermes_home", "vault", "gbrain_home", "gbrain", "lock_file", "baseline_receipt"):
        candidate = Path(str(paths.get(name) or "")).expanduser()
        if not candidate.is_absolute():
            raise RuntimeError(f"native-agent continuity paths.{name} must be absolute")
        resolved[name] = candidate
    if resolved["home"].resolve() != Path.home().resolve():
        raise RuntimeError("native session principal home does not match process owner")
    hermes = resolved["hermes_home"].resolve()
    try:
        for name in ("vault", "gbrain_home", "gbrain", "lock_file", "baseline_receipt"):
            resolved[name].resolve().relative_to(hermes)
    except ValueError as exc:
        raise RuntimeError("native session managed path escapes the tenant runtime") from exc
    value["_paths"] = resolved
    return value


def codex_binary(home: Path) -> Path | None:
    found = shutil.which("codex")
    candidates = [Path(found)] if found else []
    if sys.platform == "darwin":
        candidates.append(Path("/Applications/ChatGPT.app/Contents/Resources/codex"))
    candidates.extend(
        [home / ".local/bin/codex", home / ".bun/bin/codex", home / ".bun/bin/codex.exe"]
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and not resolved.is_symlink():
            return resolved
    return None


def state_path(manifest: dict[str, Any]) -> Path:
    return manifest["_paths"]["hermes_home"] / "state/native-session-runner/state.json"


def prior_state(manifest: dict[str, Any]) -> dict[str, Any]:
    path = state_path(manifest)
    if not path.exists():
        return {}
    value = json.loads(regular(path).read_text(encoding="utf-8"))
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else {}


def session_records(home: Path) -> list[Path]:
    root = home / ".codex/sessions"
    if root.is_symlink() or not root.is_dir():
        return []
    return [path for path in root.rglob("*.jsonl") if path.is_file() and not path.is_symlink()]


def run(manifest: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    prior = prior_state(manifest)
    updated = str(prior.get("updated_at") or "")
    pending = int(prior.get("pending_count") or 0)
    if not force and updated and pending == 0:
        stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if time.time() - stamp.timestamp() < INTERVAL_SECONDS:
            return {"schema": SCHEMA, "status": "rate_limited", "pending_count": 0}
    paths = manifest["_paths"]
    baseline = json.loads(regular(paths["baseline_receipt"]).read_text(encoding="utf-8"))
    if baseline.get("status") != "verified" or baseline.get("persistent_gbrain_owners") not in (0, []):
        raise RuntimeError("tenant GBrain baseline is not verified")
    records = session_records(paths["home"])
    if not records:
        value = {
            "schema": SCHEMA,
            "status": "ready_no_sessions",
            "updated_at": iso(),
            "pending_count": 0,
            "record_count": 0,
        }
        atomic_json(state_path(manifest), value)
        return value
    hermes = paths["hermes_home"]
    exporter = regular(hermes / "bin/session-redact-export.py")
    projector = regular(hermes / "bin/native-session-sync.py")
    carder = regular(hermes / "bin/native-session-luna-carder.py")
    python = Path(sys.executable)
    codex = codex_binary(paths["home"])
    if codex is None:
        raise RuntimeError("Codex session records exist but native Codex is unavailable")
    login = subprocess.run(
        [str(codex), "login", "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=90,
    )
    if login.returncode or "logged in" not in (login.stdout + login.stderr).lower():
        value = {
            "schema": SCHEMA,
            "status": "detected_not_enrolled",
            "updated_at": iso(),
            "pending_count": len(records),
            "record_count": len(records),
        }
        atomic_json(state_path(manifest), value)
        return value
    marker = hermes / "state/native-session-luna-carder/enabled.json"
    marker_value = {
        "schema": "native-session-luna-carder-enable/v1",
        "target": manifest["principal_id"],
        "rollout_id": f"native-agent-continuity-{hashlib.sha256(regular(paths['baseline_receipt']).read_bytes()).hexdigest()[:16]}",
        "carder_sha256": sha256(carder),
        "session_sync_sha256": sha256(projector),
    }
    if marker.exists() and json.loads(regular(marker).read_text(encoding="utf-8")) != marker_value:
        raise RuntimeError("native session Luna enable marker is unowned or drifted")
    atomic_json(marker, marker_value)
    sync_root = hermes / "state/native-session-sync"
    env = os.environ.copy()
    env.update(
        {
            "GBRAIN_HOME": str(paths["gbrain_home"]),
            "NATIVE_SESSION_PRINCIPAL": manifest["principal_id"],
            "NATIVE_SESSION_RUNTIME": str(hermes),
            "NATIVE_SESSION_SYNC_ROOT": str(sync_root),
            "NATIVE_SESSION_EXPORT_ROOT": str(sync_root / "exported"),
            "NATIVE_SESSION_EXPORTER": str(exporter),
            "NATIVE_SESSION_GBRAIN": str(paths["gbrain"]),
            "NATIVE_SESSION_GBRAIN_LOCK": str(paths["lock_file"]),
            "NATIVE_SESSION_LUNA_CARDER": str(carder),
            "NATIVE_SESSION_LUNA_STATE_ROOT": str(marker.parent),
            "NATIVE_SESSION_LUNA_ENABLE_MARKER": str(marker),
            "NATIVE_SESSION_SYNC": str(projector),
            "NATIVE_SESSION_CODEX": str(codex),
            "NATIVE_SESSION_SOURCES": "codex",
            "NATIVE_SESSION_HOST_TAG": manifest["principal_id"],
            "NATIVE_SESSION_SYNC_SCHEMA": "native-session-sync/v1",
            "NATIVE_SESSION_STATE_SCHEMA": "native-session-sync-state/v1",
            "NATIVE_SESSION_LUNA_SCHEMA": "native-session-luna-carder/v1",
            "NATIVE_SESSION_LUNA_MARKER_SCHEMA": "native-session-luna-carder-enable/v1",
            "NATIVE_SESSION_LUNA_TARGET": manifest["principal_id"],
            "NATIVE_SESSION_VAULT": str(paths["vault"]),
        }
    )
    total_written = 0
    total_cards = 0
    batch_count = 0
    receipt: dict[str, Any] = {}
    carding: dict[str, Any] = {}
    maximum_batches = max(1, (len(records) + 7) // 8 + 1)
    for _batch in range(maximum_batches):
        result = subprocess.run(
            [str(python), str(projector), "run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
            cwd=paths["vault"],
            timeout=1800,
        )
        try:
            receipt = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("native session sync returned invalid JSON") from exc
        if result.returncode or receipt.get("status") != "verified":
            raise RuntimeError("native session sync or Luna carding failed")
        carding = receipt.get("luna_carder") or {}
        batch_written = int(receipt.get("written") or 0)
        batch_cards = int(carding.get("written") or 0)
        total_written += batch_written
        total_cards += batch_cards
        batch_count += 1
        pending_count = int(receipt.get("pending_count") or 0) + int(
            carding.get("pending_count") or 0
        )
        if pending_count == 0:
            break
        if batch_written == 0 and batch_cards == 0:
            raise RuntimeError("native session drain made no progress")
    else:
        raise RuntimeError("native session drain exceeded its receipt-derived batch bound")
    value = {
        "schema": SCHEMA,
        "status": "verified",
        "updated_at": iso(),
        "record_count": len(records),
        "written": total_written,
        "pending_count": pending_count,
        "cards_written": total_cards,
        "batch_count": batch_count,
        "card_model": carding.get("model"),
        "daily_limit": None,
    }
    atomic_json(state_path(manifest), value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            value = {"schema": SCHEMA, "status": "pass", "interval_seconds": INTERVAL_SECONDS}
        elif args.manifest:
            value = run(load_manifest(args.manifest), force=args.force)
        else:
            parser.error("--manifest or --self-test is required")
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - operator-facing JSON boundary
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
