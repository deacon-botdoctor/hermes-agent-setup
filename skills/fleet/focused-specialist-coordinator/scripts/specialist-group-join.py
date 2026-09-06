#!/usr/bin/env python3
"""Join two or three reviewed specialist results into one manager-owned delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
from pathlib import Path
from typing import Any

SCHEMA = "specialist-group-join/v1"
RECEIPT_SCHEMA = "specialist-group-join-receipt/v1"
GATE_SCHEMA = "specialist-manager-gate/v2"
GATE_RECEIPT_SCHEMA = "specialist-manager-gate-receipt/v2"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MANAGER_GATE = runpy.run_path(str(Path(__file__).with_name("specialist-manager-gate.py")))
MAX_BYTES = 2 * 1024 * 1024
RAW_FAILURE_MARKERS = (
    "traceback (most recent call last)", "syntaxerror:", "modulenotfounderror:",
    "tool_call_error", "internal server error",
)


class JoinError(ValueError):
    pass


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise JoinError(f"{label}:invalid_sha256")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JoinError(f"{label}:invalid_text")
    return value


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise JoinError(f"{label}:invalid_path")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise JoinError(f"{label}:not_regular_absolute_file")
    if path.stat().st_size > MAX_BYTES:
        raise JoinError(f"{label}:too_large")
    return path


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JoinError(f"{label}:invalid_json:{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise JoinError(f"{label}:not_object")
    return value


def _canonical_sha(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def evaluate(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    bindings: dict[str, str] = {}
    member_receipts: list[str] = []
    try:
        if not isinstance(payload, dict) or set(payload) != {
            "schema", "group_id", "request_sha256", "members", "manager_synthesis"
        }:
            raise JoinError("root:invalid_keys")
        if payload["schema"] != SCHEMA:
            raise JoinError("schema:invalid")
        _text(payload["group_id"], "group_id")
        request_sha = _sha(payload["request_sha256"], "request_sha256")
        members = payload["members"]
        if not isinstance(members, list) or not 2 <= len(members) <= 3:
            raise JoinError("members:requires_two_or_three")
        task_ids: set[str] = set()
        roles: set[str] = set()
        worker_artifacts: list[Path] = []
        for index, member in enumerate(members):
            label = f"members[{index}]"
            if not isinstance(member, dict) or set(member) != {
                "task_id", "role", "gate_payload_path", "gate_payload_sha256",
                "gate_receipt_path", "gate_receipt_sha256"
            }:
                raise JoinError(f"{label}:invalid_keys")
            task_id = _text(member["task_id"], f"{label}.task_id")
            role = _text(member["role"], f"{label}.role")
            if task_id in task_ids or role in roles:
                raise JoinError(f"{label}:duplicate_task_or_role")
            task_ids.add(task_id)
            roles.add(role)
            gate_path = _path(member["gate_payload_path"], f"{label}.gate_payload_path")
            receipt_path = _path(member["gate_receipt_path"], f"{label}.gate_receipt_path")
            if _file_sha(gate_path) != _sha(member["gate_payload_sha256"], f"{label}.gate_payload_sha256"):
                raise JoinError(f"{label}.gate_payload_path:sha256_mismatch")
            receipt_hash = _file_sha(receipt_path)
            if receipt_hash != _sha(member["gate_receipt_sha256"], f"{label}.gate_receipt_sha256"):
                raise JoinError(f"{label}.gate_receipt_path:sha256_mismatch")
            gate = _json(gate_path, f"{label}.gate_payload")
            receipt = _json(receipt_path, f"{label}.gate_receipt")
            if gate.get("schema") != GATE_SCHEMA or receipt.get("schema") != GATE_RECEIPT_SCHEMA:
                raise JoinError(f"{label}:invalid_gate_schema")
            if (gate.get("worker") or {}).get("task_id") != task_id:
                raise JoinError(f"{label}:task_id_mismatch")
            if (gate.get("validation") or {}).get("input_sha256") != request_sha:
                raise JoinError(f"{label}:request_sha256_mismatch")
            if receipt.get("input_sha256") != _canonical_sha(gate):
                raise JoinError(f"{label}:gate_receipt_input_mismatch")
            if MANAGER_GATE["evaluate"](gate) != receipt:
                raise JoinError(f"{label}:manager_gate_revalidation_failed")
            if receipt.get("status") != "valid" or receipt.get("delivery_permitted") is not True:
                raise JoinError(f"{label}:manager_gate_not_permitted")
            worker_path_value = (gate.get("artifacts") or {}).get("worker_artifact_path")
            if worker_path_value:
                worker_artifacts.append(_path(worker_path_value, f"{label}.worker_artifact_path"))
            bindings[f"member_{index}_gate_payload"] = _file_sha(gate_path)
            bindings[f"member_{index}_gate_receipt"] = receipt_hash
            member_receipts.append(receipt_hash)

        synthesis = payload["manager_synthesis"]
        if not isinstance(synthesis, dict) or set(synthesis) != {
            "artifact_path", "artifact_sha256", "user_safe_summary", "ready"
        }:
            raise JoinError("manager_synthesis:invalid_keys")
        artifact = _path(synthesis["artifact_path"], "manager_synthesis.artifact_path")
        artifact_sha = _file_sha(artifact)
        if artifact_sha != _sha(synthesis["artifact_sha256"], "manager_synthesis.artifact_sha256"):
            raise JoinError("manager_synthesis.artifact_path:sha256_mismatch")
        if synthesis["ready"] is not True:
            raise JoinError("manager_synthesis:not_ready")
        summary = _text(synthesis["user_safe_summary"], "manager_synthesis.user_safe_summary")
        if any(marker in summary.lower() for marker in RAW_FAILURE_MARKERS):
            raise JoinError("manager_synthesis:raw_failure_text")
        synthesis_bytes = artifact.read_bytes()
        synthesis_text = synthesis_bytes.decode("utf-8", errors="replace")
        if any(
            synthesis_bytes == worker.read_bytes()
            or MANAGER_GATE["_verbatim_worker_summary"](synthesis_text, worker)
            or MANAGER_GATE["_verbatim_worker_summary"](summary, worker)
            for worker in worker_artifacts
        ):
            raise JoinError("manager_synthesis:verbatim_worker_artifact")
        bindings["manager_synthesis"] = artifact_sha
    except JoinError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"internal_validation_error:{type(exc).__name__}")
    unique = sorted(set(errors))
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "valid" if not unique else "invalid",
        "delivery_permitted": not unique,
        "input_sha256": _canonical_sha(payload),
        "member_receipt_sha256": member_receipts,
        "artifact_bindings": dict(sorted(bindings.items())),
        "errors": unique,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        receipt = evaluate(json.loads(args.input.read_text(encoding="utf-8")))
    except Exception as exc:
        receipt = {"schema": RECEIPT_SCHEMA, "status": "invalid", "delivery_permitted": False, "errors": [f"input:{type(exc).__name__}"]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
        if os.name != "nt":
            os.chmod(args.output, stat.S_IRUSR | stat.S_IWUSR)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("status") == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
