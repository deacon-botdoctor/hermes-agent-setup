#!/usr/bin/env python3
"""Bind actual specialist evidence before manager-owned delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "specialist-manager-gate/v2"
RECEIPT_SCHEMA = "specialist-manager-gate-receipt/v2"
VALIDATION_SCHEMA = "specialist-validation-receipt/v1"
LIFECYCLE_SCHEMA = "specialist-lifecycle-receipt/v1"
OVERRIDE_SCHEMA = "specialist-manager-override-receipt/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_TEXT_INSPECTION_BYTES = 2 * 1024 * 1024
RAW_FAILURE_MARKERS = (
    "traceback (most recent call last)",
    "syntaxerror:",
    "modulenotfounderror:",
    "attributeerror:",
    "runtimeerror:",
    "exception in thread",
    "tool_call_error",
    "internal server error",
)
MANAGEMENT_EVENT_SCHEMA = "specialist-management-event/v1"
MANAGEMENT_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
DEFAULT_MANAGEMENT_LOG = (
    Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    / "state/specialist-management/events.jsonl"
)


class GateError(ValueError):
    pass


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateError(f"{label}:not_object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise GateError(f"{label}:keys:missing={missing}:extra={extra}")
    return value


def _sha(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise GateError(f"{label}:invalid_sha256")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise GateError(f"{label}:invalid:{value!r}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise GateError(f"{label}:not_boolean")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise GateError(f"{label}:not_string")
    if not value.strip() and not allow_empty:
        raise GateError(f"{label}:empty")
    if len(value) > 1600:
        raise GateError(f"{label}:too_long")
    return value


def _artifact_path(value: Any, label: str, *, nullable: bool) -> Path | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"{label}:invalid_path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise GateError(f"{label}:not_absolute")
    try:
        if path.is_symlink() or not path.is_file():
            raise GateError(f"{label}:not_regular_file")
    except OSError as exc:
        raise GateError(f"{label}:unreadable:{type(exc).__name__}") from exc
    return path


def _file_sha256(path: Path, label: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise GateError(f"{label}:unreadable:{type(exc).__name__}") from exc
    return digest.hexdigest()


def _bind_artifact(
    declared_sha: str | None,
    path_value: Any,
    label: str,
    bindings: dict[str, str],
) -> Path | None:
    path = _artifact_path(path_value, f"artifacts.{label}_path", nullable=declared_sha is None)
    if declared_sha is None:
        if path is not None:
            raise GateError(f"artifacts.{label}_path:unexpected_without_sha")
        return None
    if path is None:
        raise GateError(f"artifacts.{label}_path:required")
    actual = _file_sha256(path, f"artifacts.{label}_path")
    if actual != declared_sha:
        raise GateError(f"artifacts.{label}_path:sha256_mismatch")
    bindings[label] = actual
    return path


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise GateError(f"{label}:too_large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except GateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError(f"{label}:invalid_json:{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise GateError(f"{label}:not_object")
    return value


def _normalized_text(value: str) -> str:
    return " ".join(value.lower().split())


def _verbatim_worker_summary(summary: str, artifact: Path | None) -> bool:
    if artifact is None:
        return False
    normalized_summary = _normalized_text(summary)
    if not normalized_summary:
        return False
    try:
        with artifact.open("rb") as handle:
            raw = handle.read(MAX_TEXT_INSPECTION_BYTES + 1)
    except OSError as exc:
        raise GateError(
            f"artifacts.worker_artifact_path:unreadable:{type(exc).__name__}"
        ) from exc
    if len(raw) > MAX_TEXT_INSPECTION_BYTES or b"\x00" in raw:
        return False
    normalized_worker = _normalized_text(raw.decode("utf-8", errors="ignore"))
    if not normalized_worker:
        return False
    if normalized_summary == normalized_worker:
        return True
    if normalized_worker in normalized_summary:
        return True
    if len(normalized_summary) < 32:
        return False
    return normalized_summary in normalized_worker


def _validation_state(
    path: Path | None,
    *,
    task_id: str,
    input_sha: str,
    worker_artifact_sha: str | None,
) -> tuple[str, bool]:
    if path is None:
        return "not_applicable", False
    receipt = _exact_keys(
        _read_json(path, "validation_receipt"),
        {
            "schema",
            "task_id",
            "input_sha256",
            "artifact_sha256",
            "status",
            "actual_artifact_inspected",
            "specialist_receipt_sha256",
        },
        "validation_receipt",
    )
    if receipt["schema"] != VALIDATION_SCHEMA:
        raise GateError("validation_receipt.schema:invalid")
    if _text(receipt["task_id"], "validation_receipt.task_id") != task_id:
        raise GateError("validation_receipt.task_id:mismatch")
    if _sha(receipt["input_sha256"], "validation_receipt.input_sha256") != input_sha:
        raise GateError("validation_receipt.input_sha256:mismatch")
    artifact_sha = _sha(
        receipt["artifact_sha256"],
        "validation_receipt.artifact_sha256",
        nullable=True,
    )
    if artifact_sha != worker_artifact_sha:
        raise GateError("validation_receipt.artifact_sha256:mismatch")
    _sha(
        receipt["specialist_receipt_sha256"],
        "validation_receipt.specialist_receipt_sha256",
        nullable=True,
    )
    status = _enum(
        receipt["status"],
        {"valid", "invalid", "not_applicable"},
        "validation_receipt.status",
    )
    inspected = _boolean(
        receipt["actual_artifact_inspected"],
        "validation_receipt.actual_artifact_inspected",
    )
    return status, inspected


def _lifecycle_safe(path: Path, *, task_id: str, profile: str) -> bool:
    receipt = _exact_keys(
        _read_json(path, "lifecycle_receipt"),
        {
            "schema",
            "task_id",
            "profile",
            "worker_exited",
            "gateway_stopped",
            "credentialless_idle",
            "side_effect_drift",
            "root_auth_stable",
            "platform_privacy",
        },
        "lifecycle_receipt",
    )
    if receipt["schema"] != LIFECYCLE_SCHEMA:
        raise GateError("lifecycle_receipt.schema:invalid")
    if _text(receipt["task_id"], "lifecycle_receipt.task_id") != task_id:
        raise GateError("lifecycle_receipt.task_id:mismatch")
    if _text(receipt["profile"], "lifecycle_receipt.profile") != profile:
        raise GateError("lifecycle_receipt.profile:mismatch")
    privacy = _enum(
        receipt["platform_privacy"],
        {"posix-private-mode", "windows-private-acl"},
        "lifecycle_receipt.platform_privacy",
    )
    return all(
        (
            _boolean(receipt["worker_exited"], "lifecycle_receipt.worker_exited"),
            _boolean(receipt["gateway_stopped"], "lifecycle_receipt.gateway_stopped"),
            _boolean(
                receipt["credentialless_idle"],
                "lifecycle_receipt.credentialless_idle",
            ),
            not _boolean(
                receipt["side_effect_drift"],
                "lifecycle_receipt.side_effect_drift",
            ),
            _boolean(receipt["root_auth_stable"], "lifecycle_receipt.root_auth_stable"),
            bool(privacy),
        )
    )


def _override_valid(path: Path | None, *, task_id: str, root_artifact_sha: str | None) -> bool:
    if path is None:
        return False
    receipt = _exact_keys(
        _read_json(path, "override_receipt"),
        {
            "schema",
            "task_id",
            "root_artifact_sha256",
            "status",
            "actual_artifact_inspected",
        },
        "override_receipt",
    )
    if receipt["schema"] != OVERRIDE_SCHEMA:
        raise GateError("override_receipt.schema:invalid")
    if _text(receipt["task_id"], "override_receipt.task_id") != task_id:
        raise GateError("override_receipt.task_id:mismatch")
    if (
        _sha(receipt["root_artifact_sha256"], "override_receipt.root_artifact_sha256")
        != root_artifact_sha
    ):
        raise GateError("override_receipt.root_artifact_sha256:mismatch")
    return (
        _enum(receipt["status"], {"valid", "invalid"}, "override_receipt.status")
        == "valid"
        and _boolean(
            receipt["actual_artifact_inspected"],
            "override_receipt.actual_artifact_inspected",
        )
    )


def evaluate(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    bindings: dict[str, str] = {}
    raw_overlap = False
    delivery_ready = False
    try:
        root = _exact_keys(
            payload,
            {"schema", "worker", "validation", "manager", "delivery", "artifacts"},
            "root",
        )
        if root["schema"] != SCHEMA:
            raise GateError("schema:invalid")

        worker = _exact_keys(
            root["worker"],
            {
                "task_id",
                "profile",
                "terminal_outcome",
                "artifact_sha256",
                "lifecycle_receipt_sha256",
            },
            "worker",
        )
        task_id = _text(worker["task_id"], "worker.task_id")
        profile = _text(worker["profile"], "worker.profile")
        outcome = _enum(
            worker["terminal_outcome"],
            {"done", "blocked", "failed", "timeout"},
            "worker.terminal_outcome",
        )
        worker_artifact_sha = _sha(
            worker["artifact_sha256"],
            "worker.artifact_sha256",
            nullable=True,
        )
        lifecycle_sha = _sha(
            worker["lifecycle_receipt_sha256"],
            "worker.lifecycle_receipt_sha256",
        )

        validation = _exact_keys(
            root["validation"],
            {"input_sha256", "receipt_sha256"},
            "validation",
        )
        input_sha = _sha(validation["input_sha256"], "validation.input_sha256")
        validation_sha = _sha(
            validation["receipt_sha256"],
            "validation.receipt_sha256",
            nullable=True,
        )

        manager = _exact_keys(
            root["manager"],
            {
                "verdict",
                "action",
                "root_artifact_sha256",
                "override_receipt_sha256",
            },
            "manager",
        )
        verdict = _enum(
            manager["verdict"],
            {"accepted", "rework", "needs-human", "retired"},
            "manager.verdict",
        )
        action = _enum(
            manager["action"],
            {
                "accepted_worker_output",
                "rework_requested",
                "manager_override",
                "external_blocker",
                "retire_worker",
            },
            "manager.action",
        )
        root_artifact_sha = _sha(
            manager["root_artifact_sha256"],
            "manager.root_artifact_sha256",
            nullable=True,
        )
        override_sha = _sha(
            manager["override_receipt_sha256"],
            "manager.override_receipt_sha256",
            nullable=True,
        )

        delivery = _exact_keys(
            root["delivery"],
            {"ready", "user_safe_summary", "external_action_required"},
            "delivery",
        )
        delivery_ready = _boolean(delivery["ready"], "delivery.ready")
        summary = _text(
            delivery["user_safe_summary"],
            "delivery.user_safe_summary",
            allow_empty=not delivery_ready,
        )
        external_required = _boolean(
            delivery["external_action_required"],
            "delivery.external_action_required",
        )

        artifacts = _exact_keys(
            root["artifacts"],
            {
                "worker_artifact_path",
                "validation_receipt_path",
                "lifecycle_receipt_path",
                "root_artifact_path",
                "override_receipt_path",
            },
            "artifacts",
        )
        worker_path = _bind_artifact(
            worker_artifact_sha,
            artifacts["worker_artifact_path"],
            "worker_artifact",
            bindings,
        )
        validation_path = _bind_artifact(
            validation_sha,
            artifacts["validation_receipt_path"],
            "validation_receipt",
            bindings,
        )
        lifecycle_path = _bind_artifact(
            lifecycle_sha,
            artifacts["lifecycle_receipt_path"],
            "lifecycle_receipt",
            bindings,
        )
        root_artifact_path = _bind_artifact(
            root_artifact_sha,
            artifacts["root_artifact_path"],
            "root_artifact",
            bindings,
        )
        override_path = _bind_artifact(
            override_sha,
            artifacts["override_receipt_path"],
            "override_receipt",
            bindings,
        )
        if lifecycle_path is None or not _lifecycle_safe(
            lifecycle_path,
            task_id=task_id,
            profile=profile,
        ):
            errors.append("unsafe_worker_terminal_state")

        validation_status, inspected = _validation_state(
            validation_path,
            task_id=task_id,
            input_sha=input_sha,
            worker_artifact_sha=worker_artifact_sha,
        )
        override_valid = _override_valid(
            override_path,
            task_id=task_id,
            root_artifact_sha=root_artifact_sha,
        )

        lowered = summary.lower()
        if any(marker in lowered for marker in RAW_FAILURE_MARKERS):
            errors.append("raw_failure_text_in_user_summary")
        raw_overlap = _verbatim_worker_summary(summary, worker_path)
        if raw_overlap:
            errors.append("verbatim_worker_output_in_user_summary")

        if action == "accepted_worker_output":
            if verdict != "accepted":
                errors.append("accepted_action_requires_accepted_verdict")
            if outcome != "done" or worker_artifact_sha is None:
                errors.append("accepted_action_requires_done_worker_artifact")
            if validation_status != "valid" or validation_sha is None or not inspected:
                errors.append("accepted_action_requires_valid_inspected_receipt")
            if not delivery_ready or external_required:
                errors.append("accepted_action_delivery_contract")
            if root_artifact_path is not None or override_path is not None:
                errors.append("accepted_action_must_not_claim_override")

        elif action == "rework_requested":
            if verdict != "rework" or delivery_ready or external_required:
                errors.append("rework_must_remain_internal")
            if root_artifact_path is not None or override_path is not None:
                errors.append("rework_must_not_claim_override")

        elif action == "manager_override":
            if verdict not in {"rework", "retired"}:
                errors.append("override_requires_failed_worker_verdict")
            if root_artifact_path is None or override_path is None or not override_valid:
                errors.append("override_requires_valid_root_artifact_and_receipt")
            if not delivery_ready or external_required:
                errors.append("override_delivery_contract")

        elif action == "external_blocker":
            if verdict != "needs-human" or not delivery_ready or not external_required:
                errors.append("external_blocker_delivery_contract")
            if root_artifact_path is not None or override_path is not None:
                errors.append("external_blocker_must_not_claim_override")

        elif action == "retire_worker":
            if verdict != "retired" or delivery_ready or external_required:
                errors.append("retirement_must_remain_internal")
            if root_artifact_path is not None or override_path is not None:
                errors.append("retirement_must_not_claim_override")

    except GateError as exc:
        errors.append(str(exc))
    except Exception as exc:  # fail closed on malformed JSON types or filesystem races
        errors.append(f"internal_validation_error:{type(exc).__name__}")

    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        canonical = repr(payload).encode("utf-8", errors="replace")
        errors.append("input:not_json_serializable")
    unique = sorted(set(errors))
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "valid" if not unique else "invalid",
        "input_sha256": hashlib.sha256(canonical).hexdigest(),
        "delivery_permitted": not unique and delivery_ready,
        "raw_worker_overlap_detected": raw_overlap,
        "artifact_bindings": dict(sorted(bindings.items())),
        "errors": unique,
    }


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
    finally:
        if os.name != "nt":
            os.chmod(path.parent, stat.S_IRWXU)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _management_identifier(value: Any, label: str) -> str:
    raw = value if isinstance(value, str) else ""
    if MANAGEMENT_IDENTIFIER_RE.fullmatch(raw):
        return raw
    digest = hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"invalid-{label}-{digest}"


def _management_enum(value: Any, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "invalid"


def _management_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and SHA256_RE.fullmatch(value) else None


def _management_error_code(value: Any) -> str:
    """Keep the validator location/reason without retaining attacker-controlled values."""
    raw = value if isinstance(value, str) else "invalid_error"
    for marker in (":invalid:", ":keys:"):
        if marker in raw:
            raw = raw.split(marker, 1)[0] + marker[:-1]
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)[:160] or "invalid_error"


def management_event(payload: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    """Return one content-free management event from a terminal gate decision."""
    root = payload if isinstance(payload, dict) else {}
    worker = root.get("worker") if isinstance(root.get("worker"), dict) else {}
    manager = root.get("manager") if isinstance(root.get("manager"), dict) else {}
    validation = (
        root.get("validation") if isinstance(root.get("validation"), dict) else {}
    )
    delivery = root.get("delivery") if isinstance(root.get("delivery"), dict) else {}
    input_sha = str(receipt.get("input_sha256") or "")
    identity = "|".join(
        (
            input_sha,
            str(worker.get("task_id") or "unknown"),
            str(worker.get("profile") or "unknown"),
            str(manager.get("verdict") or "unknown"),
            str(manager.get("action") or "unknown"),
        )
    )
    return {
        "schema": MANAGEMENT_EVENT_SCHEMA,
        "event_id": "sme_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "task_id": _management_identifier(worker.get("task_id"), "task"),
        "profile": _management_identifier(worker.get("profile"), "profile"),
        "terminal_outcome": _management_enum(
            worker.get("terminal_outcome"), {"done", "blocked", "failed", "timeout"}
        ),
        "manager_verdict": _management_enum(
            manager.get("verdict"), {"accepted", "rework", "needs-human", "retired"}
        ),
        "manager_action": _management_enum(
            manager.get("action"),
            {
                "accepted_worker_output",
                "rework_requested",
                "manager_override",
                "external_blocker",
                "retire_worker",
            },
        ),
        "gate_status": _management_enum(receipt.get("status"), {"valid", "invalid"}),
        "delivery_permitted": receipt.get("delivery_permitted") is True,
        "external_action_required": delivery.get("external_action_required") is True,
        "raw_worker_overlap_detected": receipt.get("raw_worker_overlap_detected")
        is True,
        "error_codes": [_management_error_code(value) for value in receipt.get("errors") or []][
            :40
        ],
        "input_sha256": input_sha,
        "worker_artifact_sha256": _management_sha(worker.get("artifact_sha256")),
        "lifecycle_receipt_sha256": _management_sha(
            worker.get("lifecycle_receipt_sha256")
        ),
        "validation_receipt_sha256": _management_sha(validation.get("receipt_sha256")),
    }


def _append_private_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one bounded event without following a final symlink."""
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > 16_384:
        raise GateError("management_log:event_too_large")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if path.is_symlink():
            raise GateError("management_log:symlink")
    except OSError as exc:
        raise GateError(f"management_log:unreadable:{type(exc).__name__}") from exc
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise GateError("management_log:not_regular_file")
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(path.parent, stat.S_IRWXU)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--management-log", type=Path, default=DEFAULT_MANAGEMENT_LOG)
    parser.add_argument(
        "--no-management-log",
        action="store_true",
        help="disable the private staff event only for isolated tests",
    )
    args = parser.parse_args(argv)
    payload: Any = {}
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        receipt = evaluate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "status": "invalid",
            "delivery_permitted": False,
            "raw_worker_overlap_detected": False,
            "artifact_bindings": {},
            "errors": [f"input:{type(exc).__name__}"],
        }
    if not args.no_management_log:
        try:
            _append_private_jsonl(args.management_log.expanduser(), management_event(payload, receipt))
        except Exception as exc:
            receipt["status"] = "invalid"
            receipt["delivery_permitted"] = False
            receipt["errors"] = sorted(
                set(
                    [
                        *receipt.get("errors", []),
                        f"management_log:{type(exc).__name__}",
                    ]
                )
            )
    if args.output:
        _write_private(args.output, receipt)
    json.dump(receipt, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if receipt["status"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
