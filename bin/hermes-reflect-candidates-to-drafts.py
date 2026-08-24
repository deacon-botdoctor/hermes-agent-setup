#!/usr/bin/env python3
"""Queue content-free reflection envelopes for the central lesson router.

The historical filename is retained because the active reflection trigger
already invokes it. It no longer creates, archives, or promotes Skillify
drafts. Free-form reflection remains in the runtime-local day-review report;
only allowlisted hashes and enums enter the bounded proposal outbox.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "hermes-lesson-candidate-envelope/v1"
PENDING_BATCH_SCHEMA = "hermes-lesson-pending-batch/v1"
PENDING_ACK_SCHEMA = "hermes-lesson-pending-ack/v1"
PENDING_ITEM_SCHEMA = "hermes-lesson-pending-item/v1"
PENDING_PROTOCOL_VERSION = "v3"
HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
REPORTS = HOME / "workspace/ops/reports/client-day-review"
MAX_PENDING = min(50, max(1, int(os.environ.get("HERMES_LESSON_OUTBOX_MAX_PENDING", "50"))))
MAX_CANDIDATES = min(50, max(1, int(os.environ.get("HERMES_LESSON_MAX_CANDIDATES", "50"))))
MAX_REFLECTION_CANDIDATES = 40
MAX_TASK_CONTRACT_CANDIDATES = 10
MAX_PENDING_ITEM_BYTES = 8192
HISTORICAL_METADATA_FILES = (
    "skillify-v2-candidates.jsonl",
    "skillify-v2-drafts.jsonl",
    "skillify-drafts.jsonl",
    "skillify-drafts-v2.jsonl",
)
MAX_HISTORICAL_LINE_BYTES = 1_000_000
MAX_HISTORICAL_FILE_BYTES = 10_000_000
ENVELOPE_KEYS = {
    "schema",
    "source_id",
    "original_timestamp",
    "claimed_scope_id",
    "source_kind",
    "evidence_type",
    "behavior_signals",
    "route_signals",
    "requested_rung",
    "report_id_hash",
    "finding_hash",
    "task_signature_hash",
    "evidence_reference_id",
    "content_hashes",
}
TASK_CONTRACT_ENVELOPE_KEYS = ENVELOPE_KEYS | {"task_contract_signature"}
TASK_CONTRACT_SIGNATURE_SCHEMA = "hermes-task-contract-signature/v1"
TASK_CONTRACT_REQUEST_HASH_FIELD = "request_id_hash"
TASK_CONTRACT_FIELDS = (
    "named_workflow",
    "intended_outcome",
    "required_tools",
    "deliverable",
    "independent_certifier",
    "success_contract_reference",
)
GENERIC_TASK_CONTRACT = {
    "named_workflow": "client-chat-help",
    "intended_outcome": "actionable client request receives a useful response or explicit blocker",
    "required_tools": ["telegram", "conversation_memory"],
    "deliverable": "client_response_or_explicit_blocker",
    "independent_certifier": "client_acceptance_or_doc_verdict",
}
GENERIC_SUCCESS_CONTRACT_REFERENCE = "generic-client-chat@1:client-chat-help"

TARGET_RUNGS = {
    "memory",
    "skill",
    "shared-rule",
    "test",
    "validator",
    "guard",
    "code",
    "config",
    "runtime",
    "reject",
    "defer",
}
EVIDENCE_TYPES = {
    "deterministic_review",
    "legacy_skillify",
    "self_reflection",
    "weakness_miner",
}
SKILL_EVIDENCE_CLASSES = {
    "client_rework",
    "operator_correction",
    "repeated_failure",
    "repeated_success",
}

BEHAVIOR_HINTS: dict[str, tuple[str, ...]] = {
    "response_integrity": (
        "client-visible",
        "cleanroom",
        "format",
        "leak",
        "message",
        "redact",
        "response",
        "sanitize",
        "scrub",
    ),
    "task_closeout": (
        "closeout",
        "completion",
        "follow-up",
        "open ask",
        "open promise",
        "proof",
        "reportback",
        "status update",
    ),
    "context_resolution": (
        "context",
        "continuity",
        "grounding",
        "mismatch",
        "rehydrat",
        "reply",
        "source of truth",
        "thread",
        "topic",
    ),
    "tool_reliability": (
        "api",
        "auth",
        "browser",
        "capability",
        "fallback",
        "model",
        "provider",
        "token",
        "tool",
    ),
    "runtime_operations": (
        "canary",
        "cutover",
        "fleet",
        "gateway",
        "incident",
        "restart",
        "rollout",
        "runtime",
        "service",
        "watchdog",
    ),
    "knowledge_routing": (
        "decision",
        "fact",
        "gbrain",
        "knowledge",
        "memory",
        "preference",
        "remember",
        "source of truth",
    ),
    "security_privacy": (
        "credential",
        "cross-client",
        "password",
        "permission",
        "pii",
        "privacy",
        "secret",
        "security",
    ),
    "workflow_execution": (
        "build",
        "campaign",
        "digest",
        "intake",
        "prepare",
        "process",
        "research",
        "review",
        "visual",
        "workflow",
    ),
}

ROUTE_HINTS: dict[str, tuple[str, ...]] = {
    "deterministic": (
        "check",
        "duplicate",
        "format",
        "guard",
        "idempot",
        "leak",
        "preflight",
        "redact",
        "sanitize",
        "scrub",
        "test",
        "validate",
        "validator",
    ),
    "knowledge": (
        "decision",
        "fact",
        "gbrain",
        "memory",
        "preference",
        "remember",
        "source of truth",
    ),
    "runtime_defect": (
        "api failure",
        "auth",
        "bug",
        "config",
        "crash",
        "dependency",
        "fallback",
        "gateway",
        "outage",
        "provider",
        "runtime",
        "service",
        "traceback",
    ),
    "workflow": (
        "campaign",
        "digest",
        "intake",
        "process",
        "production",
        "reconciliation",
        "research",
        "review",
        "steps",
        "workflow",
    ),
}


def _hash(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(str(value or "").encode()).hexdigest()


def _timestamp(report: dict[str, Any], path: Path) -> str:
    for key in ("generated_at", "timestamp", "ts", "created_at"):
        value = report.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")


def _config_scope(home: Path) -> str:
    return _hash(f"runtime_home={home}")[:24]


def _candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in (
            "draft_skill_name",
            "title",
            "pattern",
            "recommendation",
            "summary",
            "trigger",
            "why",
            "reason",
        )
    ).lower()


def _task_contract_signature(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    signature = {field: candidate.get(field) for field in TASK_CONTRACT_FIELDS}
    if (
        any(
            not isinstance(signature[field], str) or not signature[field].strip()
            for field in TASK_CONTRACT_FIELDS
            if field != "required_tools"
        )
        or not isinstance(signature["required_tools"], list)
        or not signature["required_tools"]
        or any(not isinstance(tool, str) or not tool.strip() for tool in signature["required_tools"])
    ):
        return None
    signature["required_tools"] = [tool.strip() for tool in signature["required_tools"]]
    if (
        any(signature[field] != value for field, value in GENERIC_TASK_CONTRACT.items() if field != "required_tools")
        or signature["required_tools"] != GENERIC_TASK_CONTRACT["required_tools"]
        or signature["success_contract_reference"] != GENERIC_SUCCESS_CONTRACT_REFERENCE
    ):
        return None
    return {
        "schema": TASK_CONTRACT_SIGNATURE_SCHEMA,
        "named_workflow": signature["named_workflow"],
        "intended_outcome_hash": _hash(signature["intended_outcome"]),
        "required_tools": signature["required_tools"],
        "deliverable": signature["deliverable"],
        "independent_certifier": signature["independent_certifier"],
        "success_contract_reference": signature["success_contract_reference"],
    }


def _task_contract_candidate(outcome: Any) -> dict[str, Any] | None:
    signature = _task_contract_signature(outcome)
    request_id = outcome.get("request_id") if isinstance(outcome, dict) else None
    if (
        signature is None
        or str(outcome.get("status") or "").lower() != "accomplished"
        or not isinstance(request_id, str)
        or not request_id.strip()
    ):
        return None
    return {
        **{field: outcome[field] for field in TASK_CONTRACT_FIELDS},
        "request_id": request_id,
        "source": "deterministic_review",
        "target_rung": "defer",
        "title": "Independent task-contract certification",
        "pattern": signature["named_workflow"],
        "why": outcome["intended_outcome"],
    }


def _matched_hints(text: str, hints: dict[str, tuple[str, ...]]) -> list[str]:
    return sorted(name for name, terms in hints.items() if any(term in text for term in terms))


def _evidence_type(source: Any) -> str:
    value = str(source or "").lower()
    if "weakness" in value or "miner" in value:
        return "weakness_miner"
    if "legacy" in value or "skillify" in value:
        return "legacy_skillify"
    if "determin" in value:
        return "deterministic_review"
    return "self_reflection"


def _has_trusted_skill_attribution(report: dict[str, Any], candidate: dict[str, Any]) -> bool:
    fields = ("skill_id", "skill_version", "skill_sha256")
    if not any(candidate.get(field) not in (None, "") for field in fields):
        return True
    if candidate.get("evidence_class") not in SKILL_EVIDENCE_CLASSES:
        return False
    if candidate.get("skill_use_evidence") != "successful_load_within_window":
        return False
    metadata = report.get("self_reflection_meta")
    inventory = metadata.get("recent_skill_usage") if isinstance(metadata, dict) else None
    if not isinstance(inventory, list):
        return False
    expected = tuple(candidate.get(field) for field in fields)
    return any(
        isinstance(item, dict)
        and tuple(item.get(field) for field in fields) == expected
        and item.get("evidence") == "successful_load_within_window"
        for item in inventory[:24]
    )


def build_envelope(
    report: dict[str, Any],
    candidate: dict[str, Any],
    index: int,
    path: Path,
    *,
    scope_id: str | None = None,
) -> dict[str, Any]:
    target_rung = str(candidate.get("target_rung") or "skill").lower()
    if target_rung not in TARGET_RUNGS:
        raise ValueError("unsupported target_rung")
    text = _candidate_text(candidate)
    behavior_signals = _matched_hints(text, BEHAVIOR_HINTS)
    route_signals = _matched_hints(text, ROUTE_HINTS)
    report_id_hash = _hash(report.get("report_id") or path.name)
    finding_fields = {
        key: candidate.get(key)
        for key in (
            "draft_skill_name",
            "title",
            "pattern",
            "recommendation",
            "summary",
            "trigger",
            "why",
            "reason",
        )
    }
    if candidate.get("skill_id"):
        finding_fields["skill_attribution"] = {
            "id": candidate.get("skill_id"),
            "version": candidate.get("skill_version"),
            "sha256": candidate.get("skill_sha256"),
            "evidence_class": candidate.get("evidence_class"),
        }
    finding_hash = _hash(finding_fields)
    task_contract_signature = _task_contract_signature(candidate)
    claimed_scope_id = scope_id or _config_scope(HOME)
    task_signature = {
        "workflow": candidate.get("named_workflow") or candidate.get("workflow") or candidate.get("pattern"),
        "outcome": candidate.get("intended_outcome") or candidate.get("recommendation"),
        "tools": candidate.get("required_tools") or candidate.get("tools"),
        "deliverable": candidate.get("deliverable") or candidate.get("deliverable_type"),
    }
    if candidate.get("skill_id"):
        task_signature["skill_attribution"] = {
            "id": candidate.get("skill_id"),
            "version": candidate.get("skill_version"),
            "sha256": candidate.get("skill_sha256"),
            "evidence_class": candidate.get("evidence_class"),
            "use_evidence": candidate.get("skill_use_evidence"),
        }
    if task_contract_signature is not None:
        request_id = candidate.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("task contract observation requires request_id")
        task_contract_signature[TASK_CONTRACT_REQUEST_HASH_FIELD] = _hash(
            f"task-contract-observation/v1\0{claimed_scope_id}\0{request_id}"
        )
        task_signature.update(
            {
                "independent_certifier": candidate["independent_certifier"],
                "success_contract_reference": candidate["success_contract_reference"],
                TASK_CONTRACT_REQUEST_HASH_FIELD: task_contract_signature[
                    TASK_CONTRACT_REQUEST_HASH_FIELD
                ],
            }
        )
    task_signature_hash = _hash(task_signature)
    source_id = _hash(f"{SCHEMA}|{claimed_scope_id}|{report_id_hash}|{index}|{finding_hash}|{target_rung}")[:32]
    evidence_type = _evidence_type(candidate.get("source"))
    assert evidence_type in EVIDENCE_TYPES
    envelope = {
        "schema": SCHEMA,
        "source_id": source_id,
        "original_timestamp": _timestamp(report, path),
        "claimed_scope_id": claimed_scope_id,
        "source_kind": "reflection_finding",
        "evidence_type": evidence_type,
        "behavior_signals": behavior_signals,
        "route_signals": route_signals,
        "requested_rung": target_rung,
        "report_id_hash": report_id_hash,
        "finding_hash": finding_hash,
        "task_signature_hash": task_signature_hash,
        "evidence_reference_id": _hash(f"report|{report_id_hash}|{index}")[:24],
        "content_hashes": {
            "name": _hash(candidate.get("skill_id") or candidate.get("draft_skill_name") or candidate.get("title")),
            "pattern": _hash(candidate.get("pattern") or candidate.get("recommendation") or candidate.get("summary")),
            "reason": _hash(candidate.get("why") or candidate.get("trigger") or candidate.get("reason")),
        },
    }
    if task_contract_signature is not None:
        envelope["task_contract_signature"] = task_contract_signature
    return envelope


def load_report(explicit: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        reports_root = REPORTS.resolve()
        try:
            path.relative_to(reports_root)
        except ValueError as exc:
            raise ValueError("report must be inside the day-review report directory") from exc
        if path.suffix != ".json" or not path.is_file():
            raise ValueError("report must be an existing JSON file")
        return path, json.loads(path.read_text(encoding="utf-8"))
    files = sorted(glob.glob(str(REPORTS / "*.json")), key=os.path.getmtime, reverse=True)
    if not files:
        return None
    path = Path(files[0])
    return path, json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate_pending_envelope(envelope: Any, scope_id: str | None = None) -> dict[str, Any]:
    if not isinstance(envelope, dict) or frozenset(envelope) not in {
        frozenset(ENVELOPE_KEYS),
        frozenset(TASK_CONTRACT_ENVELOPE_KEYS),
    }:
        raise ValueError("invalid envelope fields")
    if envelope.get("schema") != SCHEMA:
        raise ValueError("unsupported envelope schema")
    for field, length in (
        ("source_id", 32),
        ("claimed_scope_id", 24),
        ("report_id_hash", 64),
        ("finding_hash", 64),
        ("task_signature_hash", 64),
        ("evidence_reference_id", 24),
    ):
        value = envelope.get(field)
        if not isinstance(value, str) or re.fullmatch(f"[a-f0-9]{{{length}}}", value) is None:
            raise ValueError(f"invalid {field}")
    expected_scope = scope_id or _config_scope(HOME)
    if envelope.get("claimed_scope_id") != expected_scope:
        raise ValueError("envelope scope does not match authenticated scope")
    if envelope.get("source_kind") != "reflection_finding":
        raise ValueError("unsupported source_kind")
    if envelope.get("evidence_type") not in EVIDENCE_TYPES:
        raise ValueError("unsupported evidence_type")
    if envelope.get("requested_rung") not in TARGET_RUNGS:
        raise ValueError("unsupported requested_rung")
    if "task_contract_signature" in envelope:
        task_contract_signature = envelope["task_contract_signature"]
        if (
            not isinstance(task_contract_signature, dict)
            or set(task_contract_signature)
            != {
                "schema",
                "named_workflow",
                "intended_outcome_hash",
                "required_tools",
                "deliverable",
                "independent_certifier",
                "success_contract_reference",
                TASK_CONTRACT_REQUEST_HASH_FIELD,
            }
            or task_contract_signature.get("schema") != TASK_CONTRACT_SIGNATURE_SCHEMA
            or task_contract_signature.get("named_workflow") != GENERIC_TASK_CONTRACT["named_workflow"]
            or task_contract_signature.get("intended_outcome_hash") != _hash(GENERIC_TASK_CONTRACT["intended_outcome"])
            or task_contract_signature.get("required_tools") != GENERIC_TASK_CONTRACT["required_tools"]
            or task_contract_signature.get("deliverable") != GENERIC_TASK_CONTRACT["deliverable"]
            or task_contract_signature.get("independent_certifier") != GENERIC_TASK_CONTRACT["independent_certifier"]
            or not isinstance(task_contract_signature.get("intended_outcome_hash"), str)
            or re.fullmatch(r"[a-f0-9]{64}", task_contract_signature["intended_outcome_hash"]) is None
            or not isinstance(task_contract_signature.get("success_contract_reference"), str)
            or task_contract_signature["success_contract_reference"] != GENERIC_SUCCESS_CONTRACT_REFERENCE
            or not isinstance(task_contract_signature.get(TASK_CONTRACT_REQUEST_HASH_FIELD), str)
            or re.fullmatch(
                r"[a-f0-9]{64}", task_contract_signature[TASK_CONTRACT_REQUEST_HASH_FIELD]
            )
            is None
        ):
            raise ValueError("invalid task_contract_signature")
    for field, allowed, maximum in (
        ("behavior_signals", set(BEHAVIOR_HINTS), 8),
        ("route_signals", set(ROUTE_HINTS), 4),
    ):
        values = envelope.get(field)
        if (
            not isinstance(values, list)
            or len(values) > maximum
            or len(values) != len(set(values))
            or any(value not in allowed for value in values)
        ):
            raise ValueError(f"invalid {field}")
    hashes = envelope.get("content_hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"name", "pattern", "reason"}:
        raise ValueError("invalid content_hashes")
    if any(not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None for value in hashes.values()):
        raise ValueError("invalid content hash")
    timestamp = envelope.get("original_timestamp")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("invalid original_timestamp")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid original_timestamp") from exc
    return envelope


def _pending_root(scope_id: str) -> Path:
    return HOME / "state/lesson-pending-envelopes" / scope_id


def _pending_item(envelope: dict[str, Any]) -> dict[str, Any]:
    binding = _hash(json.dumps(envelope, indent=2, sort_keys=True) + "\n")
    return {
        "schema": PENDING_ITEM_SCHEMA,
        "binding_sha256": binding,
        "envelope": envelope,
    }


def _read_regular_bytes(path: Path, *, maximum_bytes: int | None = None) -> bytes | None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    flags = os.O_RDONLY | os.O_NONBLOCK | no_follow
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return None
        if maximum_bytes is not None and file_stat.st_size > maximum_bytes:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read() if maximum_bytes is None else handle.read(maximum_bytes + 1)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_pending(path: Path, scope_id: str) -> dict[str, Any] | None:
    content = _read_regular_bytes(path, maximum_bytes=MAX_PENDING_ITEM_BYTES)
    if content is None or len(content) > MAX_PENDING_ITEM_BYTES:
        return None
    try:
        item = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(item, dict) or set(item) != {
        "schema",
        "binding_sha256",
        "envelope",
    }:
        return None
    if item.get("schema") != PENDING_ITEM_SCHEMA:
        return None
    envelope = item.get("envelope")
    binding = item.get("binding_sha256")
    if not isinstance(envelope, dict) or not isinstance(binding, str):
        return None
    if binding != _pending_item(envelope)["binding_sha256"]:
        return None
    try:
        _validate_pending_envelope(envelope, scope_id)
    except (TypeError, ValueError):
        return None
    return item


def _open_rejected_directory(rejected: Path) -> int | None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | no_follow
    try:
        parent_descriptor = os.open(rejected.parent, flags)
    except OSError:
        return None
    try:
        try:
            rejected_stat = os.lstat(rejected.name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            try:
                os.mkdir(rejected.name, mode=0o700, dir_fd=parent_descriptor)
            except OSError:
                return None
        except OSError:
            return None
        else:
            if stat.S_ISLNK(rejected_stat.st_mode):
                try:
                    os.unlink(rejected.name, dir_fd=parent_descriptor)
                    os.mkdir(rejected.name, mode=0o700, dir_fd=parent_descriptor)
                except OSError:
                    return None
            elif not stat.S_ISDIR(rejected_stat.st_mode):
                return None
        try:
            descriptor = os.open(rejected.name, flags, dir_fd=parent_descriptor)
        except OSError:
            return None
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise NotADirectoryError(rejected)
            os.fchmod(descriptor, 0o700)
        except OSError:
            os.close(descriptor)
            return None
        return descriptor
    finally:
        os.close(parent_descriptor)


def _quarantine_pending(path: Path, rejected: Path, reason: str) -> bool:
    rejected_descriptor = _open_rejected_directory(rejected)
    if rejected_descriptor is None:
        return False
    try:
        target_name = f"{path.stem}-{reason}.json"
        suffix = 2
        while True:
            try:
                os.lstat(target_name, dir_fd=rejected_descriptor)
            except FileNotFoundError:
                break
            except OSError:
                return False
            target_name = f"{path.stem}-{reason}-{suffix}.json"
            suffix += 1
        try:
            os.replace(path, target_name, dst_dir_fd=rejected_descriptor)
        except OSError:
            return False
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW")
        try:
            target_descriptor = os.open(target_name, flags, dir_fd=rejected_descriptor)
        except OSError:
            try:
                os.unlink(target_name, dir_fd=rejected_descriptor)
            except OSError:
                pass
            return False
        try:
            if stat.S_ISREG(os.fstat(target_descriptor).st_mode):
                os.fchmod(target_descriptor, 0o600)
                return True
            else:
                os.unlink(target_name, dir_fd=rejected_descriptor)
        except OSError:
            try:
                os.unlink(target_name, dir_fd=rejected_descriptor)
            except OSError:
                pass
        finally:
            os.close(target_descriptor)
        return False
    finally:
        os.close(rejected_descriptor)


def _valid_pending_paths(
    pending: Path, rejected: Path, scope_id: str, *, quarantine: bool
) -> tuple[list[Path], int, bool]:
    valid: list[Path] = []
    invalid_count = 0
    mutated = False
    for path in sorted(pending.glob("*.json")):
        item = _load_pending(path, scope_id)
        envelope = item.get("envelope") if item else None
        reason = "invalid"
        if item and path.name != f"{envelope['source_id']}.json":
            item = None
            reason = "filename-mismatch"
        if item is None:
            invalid_count += 1
            if quarantine:
                mutated = _quarantine_pending(path, rejected, reason) or mutated
            continue
        valid.append(path)
    return valid, invalid_count, mutated


def _report_envelopes(scope_id: str):
    report_paths: list[tuple[float, str, Path]] = []
    for report_path in REPORTS.glob("*.json"):
        try:
            report_stat = report_path.lstat()
            if not stat.S_ISREG(report_stat.st_mode):
                continue
            report_paths.append((report_stat.st_mtime, report_path.name, report_path))
        except OSError:
            continue
    for _mtime, _name, report_path in sorted(report_paths):
        content = _read_regular_bytes(report_path)
        if content is None:
            continue
        try:
            report = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        reflection_candidates: list[Any] = []
        task_contract_candidates: list[dict[str, Any]] = []
        malformed_candidates = False
        for field in ("skillify_candidates", "proposal_inputs"):
            values = report.get(field)
            if values is None:
                continue
            if not isinstance(values, list):
                malformed_candidates = True
                break
            reflection_candidates.extend(values)
        outcomes = report.get("outcomes")
        if outcomes is not None:
            if not isinstance(outcomes, list):
                malformed_candidates = True
            else:
                task_contract_candidates = [
                    candidate for outcome in outcomes if (candidate := _task_contract_candidate(outcome)) is not None
                ]
        if malformed_candidates:
            continue
        task_contract_limit = min(MAX_TASK_CONTRACT_CANDIDATES, max(1, (MAX_CANDIDATES + 4) // 5))
        reflection_candidates = reflection_candidates[
            : min(MAX_REFLECTION_CANDIDATES, MAX_CANDIDATES - task_contract_limit)
        ]
        task_contract_candidates = task_contract_candidates[:task_contract_limit]
        candidates = reflection_candidates + task_contract_candidates
        for index, candidate in enumerate(candidates):
            if isinstance(candidate, dict) and _has_trusted_skill_attribution(report, candidate):
                try:
                    yield build_envelope(
                        report,
                        candidate,
                        index,
                        report_path,
                        scope_id=scope_id,
                    )
                except (OSError, TypeError, ValueError):
                    continue


def _report_pending_item(scope_id: str, source_id: str) -> dict[str, Any] | None:
    for envelope in _report_envelopes(scope_id):
        if envelope["source_id"] == source_id:
            return _pending_item(envelope)
    return None


def _enqueue_reports(scope_id: str) -> tuple[int, int, bool]:
    root = _pending_root(scope_id)
    pending = root / "pending"
    acknowledged = root / "acknowledged"
    rejected = root / "rejected"
    pending_existed = pending.exists()
    pending.mkdir(parents=True, exist_ok=True)
    valid_paths, invalid_count, mutated = _valid_pending_paths(pending, rejected, scope_id, quarantine=True)
    mutated = mutated or not pending_existed
    pending_count = len(valid_paths)
    queued = 0
    for envelope in _report_envelopes(scope_id):
        if pending_count >= MAX_PENDING:
            break
        source_id = envelope["source_id"]
        if (
            (pending / f"{source_id}.json").exists()
            or (acknowledged / f"{source_id}.json").exists()
            or (rejected / f"{source_id}-central-rejection.json").exists()
        ):
            continue
        _write_atomic(pending / f"{source_id}.json", _pending_item(envelope))
        queued += 1
        pending_count += 1
    return queued, invalid_count, mutated or bool(queued)


def emit_pending_envelopes(scope_id: str, *, read_only: bool = False) -> dict[str, Any]:
    if re.fullmatch(r"[a-f0-9]{24}", scope_id or "") is None:
        raise ValueError("scope_id must be a 24-character lowercase hash")
    root = _pending_root(scope_id)
    pending = root / "pending"
    acknowledged = root / "acknowledged"
    rejected = root / "rejected"
    quarantined_count = 0
    mutated = False
    if not read_only:
        _, quarantined_count, mutated = _enqueue_reports(scope_id)
    items: list[dict[str, Any]] = []
    valid_paths, invalid_count, quarantined = _valid_pending_paths(
        pending, rejected, scope_id, quarantine=not read_only
    )
    mutated = mutated or quarantined
    known_source_ids: set[str] = set()
    for path in valid_paths:
        item = _load_pending(path, scope_id)
        if item is None:
            continue
        envelope = item["envelope"]
        known_source_ids.add(envelope["source_id"])
        items.append({"binding_sha256": item["binding_sha256"], "envelope": envelope})
    if read_only:
        for envelope in _report_envelopes(scope_id):
            if len(items) >= MAX_PENDING:
                break
            source_id = envelope["source_id"]
            if (
                source_id in known_source_ids
                or (acknowledged / f"{source_id}.json").exists()
                or (rejected / f"{source_id}-central-rejection.json").exists()
            ):
                continue
            known_source_ids.add(source_id)
            item = _pending_item(envelope)
            items.append(
                {
                    "binding_sha256": item["binding_sha256"],
                    "envelope": envelope,
                }
            )
    return {
        "schema": PENDING_BATCH_SCHEMA,
        "scope_id": scope_id,
        "items": items[:MAX_PENDING],
        "invalid_count": invalid_count + quarantined_count,
        "mutated": mutated,
    }


def _settle_pending(
    scope_id: str,
    source_id: str,
    binding_sha256: str,
    *,
    reject: bool,
) -> dict[str, Any]:
    if (
        re.fullmatch(r"[a-f0-9]{24}", scope_id or "") is None
        or re.fullmatch(r"[a-f0-9]{32}", source_id or "") is None
        or re.fullmatch(r"[a-f0-9]{64}", binding_sha256 or "") is None
    ):
        raise ValueError("invalid pending settlement")
    root = _pending_root(scope_id)
    pending_path = root / "pending" / f"{source_id}.json"
    suffix = "-central-rejection" if reject else ""
    settled_dir = "rejected" if reject else "acknowledged"
    settled_path = root / settled_dir / f"{source_id}{suffix}.json"
    item = _load_pending(pending_path, scope_id)
    if item is not None:
        if item["binding_sha256"] != binding_sha256:
            raise ValueError("pending settlement binding mismatch")
        settled_path.parent.mkdir(parents=True, exist_ok=True)
        if settled_path.exists():
            existing = _load_pending(settled_path, scope_id)
            if existing is None or existing["binding_sha256"] != binding_sha256:
                raise ValueError("pending settlement target conflict")
            pending_path.unlink()
            moved = False
        else:
            os.replace(pending_path, settled_path)
            moved = True
    else:
        existing = _load_pending(settled_path, scope_id)
        if existing is not None:
            if existing["binding_sha256"] != binding_sha256:
                raise ValueError("pending settlement target conflict")
            moved = False
        else:
            item = _report_pending_item(scope_id, source_id)
            if item is None:
                raise ValueError("pending settlement source is unavailable")
            if item["binding_sha256"] != binding_sha256:
                raise ValueError("pending settlement binding mismatch")
            _write_atomic(settled_path, item)
            moved = True
    return {
        "schema": PENDING_ACK_SCHEMA,
        "source_id": source_id,
        "rejected" if reject else "acked": moved,
        "idempotent": not moved,
    }


def inventory_skillify_metadata(scope_id: str | None = None) -> dict[str, Any]:
    """Return a content-free, non-mutating inventory of allowlisted Skillify JSONL.

    Artifact and row limits bound the scan, and v2 mirror occurrences reconcile
    independently of legacy metadata.
    """
    inventory_scope = scope_id or _config_scope(HOME)
    if re.fullmatch(r"[a-f0-9]{24}", inventory_scope) is None:
        raise ValueError("scope_id must be a 24-character lowercase hash")
    state_root = (HOME / "state").resolve()
    artifacts: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for name in HISTORICAL_METADATA_FILES:
        candidate = state_root / name
        if not candidate.exists():
            artifacts.append({"source_artifact": name, "status": "missing", "row_count": 0})
            continue
        try:
            if candidate.is_symlink():
                raise ValueError("metadata source may not be a symlink")
            path = candidate.resolve(strict=True)
            path.relative_to(state_root)
        except (OSError, ValueError):
            artifacts.append({"source_artifact": name, "status": "unsafe_path", "row_count": 0})
            continue
        if not path.is_file() or path.is_symlink():
            artifacts.append({"source_artifact": name, "status": "unsafe_path", "row_count": 0})
            continue
        try:
            file_size = path.stat().st_size
        except OSError:
            artifacts.append({"source_artifact": name, "status": "unreadable", "row_count": 0})
            continue
        if file_size > MAX_HISTORICAL_FILE_BYTES:
            artifacts.append(
                {
                    "source_artifact": name,
                    "status": "oversized",
                    "row_count": 0,
                }
            )
            malformed.append(
                {
                    "source_artifact": name,
                    "status": "oversized_artifact",
                }
            )
            continue
        artifact_hash = hashlib.sha256()
        row_count = 0
        artifact_rows: list[dict[str, Any]] = []
        artifact_malformed: list[dict[str, Any]] = []
        total_bytes = 0
        line_index = 0
        line_buffer = bytearray()
        line_hash = hashlib.sha256()
        line_length = 0
        oversized_line = False
        exceeded_limit = False
        artifact_v2_occurrences: dict[tuple[str, str], int] = {}

        def process_line() -> None:
            nonlocal row_count
            if not line_length or not line_buffer:
                return
            line = bytes(line_buffer)
            if not line.strip():
                return
            row_count += 1
            row_binding = line_hash.hexdigest()
            if oversized_line:
                artifact_malformed.append(
                    {
                        "source_artifact": name,
                        "row_index": line_index,
                        "row_sha256": row_binding,
                        "status": "oversized",
                    }
                )
                return
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = None
            if not isinstance(row, dict):
                artifact_malformed.append(
                    {
                        "source_artifact": name,
                        "row_index": line_index,
                        "row_sha256": row_binding,
                        "status": "malformed",
                    }
                )
                return
            if name in {"skillify-v2-candidates.jsonl", "skillify-v2-drafts.jsonl"}:
                base_identity = "|".join(
                    str(row.get(key) or "") for key in ("schema_version", "signature_hash", "signature", "name")
                )
                ordinal_key = (name, base_identity)
                ordinal = artifact_v2_occurrences.get(ordinal_key, 0)
                artifact_v2_occurrences[ordinal_key] = ordinal + 1
                identity_seed = f"v2|{base_identity}|occurrence={ordinal}"
            else:
                identity_seed = f"legacy|{name}|{line_index}|{row_binding}"
            identity = _hash(identity_seed)
            artifact_rows.append(
                {
                    "source_artifact": name,
                    "row_index": line_index,
                    "row_sha256": row_binding,
                    "identity": identity,
                    "timestamp": str(row.get("ts") or row.get("timestamp") or ""),
                    "row": row,
                    "path": path,
                }
            )

        try:
            with path.open("rb") as handle:
                while chunk := handle.read(65536):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_HISTORICAL_FILE_BYTES:
                        exceeded_limit = True
                        break
                    artifact_hash.update(chunk)
                    start = 0
                    while start < len(chunk):
                        newline = chunk.find(b"\n", start)
                        end = len(chunk) if newline < 0 else newline
                        segment = chunk[start:end]
                        if newline >= 0 and segment.endswith(b"\r"):
                            segment = segment[:-1]
                        line_hash.update(segment)
                        line_length += len(segment)
                        if line_length <= MAX_HISTORICAL_LINE_BYTES:
                            line_buffer.extend(segment)
                        else:
                            oversized_line = True
                        if newline < 0:
                            break
                        process_line()
                        line_index += 1
                        line_buffer.clear()
                        line_hash = hashlib.sha256()
                        line_length = 0
                        oversized_line = False
                        start = newline + 1
                if not exceeded_limit:
                    process_line()
        except OSError:
            artifacts.append({"source_artifact": name, "status": "unreadable", "row_count": 0})
            continue
        if exceeded_limit:
            artifacts.append(
                {
                    "source_artifact": name,
                    "status": "oversized",
                    "row_count": 0,
                }
            )
            malformed.append(
                {
                    "source_artifact": name,
                    "status": "oversized_artifact",
                }
            )
            continue
        artifact_sha = artifact_hash.hexdigest()
        for row in artifact_rows:
            row["source_artifact_sha256"] = artifact_sha
        raw_rows.extend(artifact_rows)
        malformed.extend(artifact_malformed)
        artifacts.append(
            {
                "source_artifact": name,
                "source_artifact_sha256": artifact_sha,
                "status": "read",
                "row_count": row_count,
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        grouped.setdefault(row["identity"], []).append(row)
    entries: list[dict[str, Any]] = []
    for identity in sorted(grouped):
        rows = grouped[identity]
        preferred = next(
            (row for row in rows if row["source_artifact"] == "skillify-v2-candidates.jsonl"),
            rows[0],
        )
        source_row = preferred["row"]
        timestamps = sorted(row["timestamp"] for row in rows if row["timestamp"])
        report = {
            "report_id": f"historical-skillify:{identity}",
            "timestamp": timestamps[0] if timestamps else None,
        }
        proposal = {
            "draft_skill_name": source_row.get("name"),
            "pattern": source_row.get("signature") or source_row.get("category"),
            "why": "",
            "target_rung": "skill",
            "source": "legacy_skillify",
        }
        envelope = build_envelope(report, proposal, 0, preferred["path"], scope_id=inventory_scope)
        for offset, row in enumerate(sorted(rows, key=lambda item: (item["source_artifact"], item["row_index"]))):
            entry = {
                "source_artifact": row["source_artifact"],
                "source_artifact_sha256": row["source_artifact_sha256"],
                "row_index": row["row_index"],
                "row_sha256": row["row_sha256"],
                "source_id": envelope["source_id"],
                "original_timestamp": envelope["original_timestamp"],
                "role": "primary" if offset == 0 else "alias",
            }
            if offset == 0:
                entry["envelope"] = envelope
            entries.append(entry)

    v2_mirror_mismatches = sum(
        sum(row["source_artifact"] == "skillify-v2-candidates.jsonl" for row in rows)
        != sum(row["source_artifact"] == "skillify-v2-drafts.jsonl" for row in rows)
        for rows in grouped.values()
        if any(
            row["source_artifact"]
            in {
                "skillify-v2-candidates.jsonl",
                "skillify-v2-drafts.jsonl",
            }
            for row in rows
        )
    )
    incomplete_v2_metadata = any(
        row["source_artifact"]
        in {
            "skillify-v2-candidates.jsonl",
            "skillify-v2-drafts.jsonl",
        }
        for row in malformed
    ) or any(
        artifact["source_artifact"]
        in {
            "skillify-v2-candidates.jsonl",
            "skillify-v2-drafts.jsonl",
        }
        and artifact["status"] in {"oversized", "unsafe_path", "unreadable"}
        for artifact in artifacts
    )
    return {
        "schema": "hermes-skillify-metadata-inventory/v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mutated": False,
        "scope_id": inventory_scope,
        "artifacts": artifacts,
        "entries": sorted(
            entries,
            key=lambda item: (
                item["original_timestamp"],
                item["source_id"],
                item["source_artifact"],
                item["row_index"],
            ),
        ),
        "malformed": malformed,
        "reconciliation": {
            "raw_rows": len(raw_rows) + len(malformed),
            "primary_events": sum(entry["role"] == "primary" for entry in entries),
            "aliases": sum(entry["role"] == "alias" for entry in entries),
            "malformed": len(malformed),
            "v2_mirror_mismatches": v2_mirror_mismatches,
            "balanced": not v2_mirror_mismatches and not incomplete_v2_metadata,
        },
        "excluded_sources": ["tar_archives", "transcripts", "context_logs", "evidence_bodies"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue reflection findings for the central lesson router")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--inventory-skillify-metadata", action="store_true")
    parser.add_argument("--emit-pending-envelopes", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--scope-id")
    parser.add_argument("--ack-source-id")
    parser.add_argument("--reject-source-id")
    parser.add_argument("--binding-sha256")
    args = parser.parse_args()
    selected_modes = sum(
        bool(value)
        for value in (
            args.report,
            args.inventory_skillify_metadata,
            args.emit_pending_envelopes,
            args.ack_source_id,
            args.reject_source_id,
        )
    )
    if selected_modes > 1:
        parser.error("choose only one operating mode")
    if args.inventory_skillify_metadata:
        print(json.dumps(inventory_skillify_metadata(args.scope_id), sort_keys=True))
        return 0
    if args.emit_pending_envelopes:
        if not args.scope_id or args.ack_source_id or args.reject_source_id or args.binding_sha256:
            parser.error("--emit-pending-envelopes requires only --scope-id")
        print(
            json.dumps(
                emit_pending_envelopes(args.scope_id, read_only=args.read_only),
                sort_keys=True,
            )
        )
        return 0
    if args.read_only:
        parser.error("--read-only requires --emit-pending-envelopes")
    if args.ack_source_id or args.reject_source_id or args.binding_sha256:
        if bool(args.ack_source_id) == bool(args.reject_source_id) or not (args.scope_id and args.binding_sha256):
            parser.error("settlement requires one action plus --scope-id and --binding-sha256")
        try:
            result = _settle_pending(
                args.scope_id,
                args.ack_source_id or args.reject_source_id,
                args.binding_sha256,
                reject=bool(args.reject_source_id),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"schema": "hermes-lesson-pending-ack/v1", "acked": False, "error": str(exc)[:120]}))
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.scope_id:
        parser.error("--scope-id requires an explicit inventory or pending mode")
    summary: dict[str, Any] = {
        "schema": "hermes-lesson-envelope-outbox-run/v1",
        "status": "collector_owned",
        "report": None,
        "queued": [],
        "idempotent": [],
        "rejected": [],
        "created": [],
        "archived": [],
        "skillify_invoked": False,
        "drafts_created": False,
    }
    if args.report:
        try:
            loaded = load_report(args.report)
            if loaded:
                path, report = loaded
                summary["report"] = _hash(report.get("report_id") or path.name)[:24]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            summary["rejected"].append({"index": -1, "reason": str(exc)[:120]})
            print(json.dumps(summary, sort_keys=True))
            return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
