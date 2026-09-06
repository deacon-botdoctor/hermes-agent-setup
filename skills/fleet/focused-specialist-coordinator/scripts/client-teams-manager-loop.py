#!/usr/bin/env python3
"""Fail-closed join for one versioned client Teams manager decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

MANAGER_GATE = runpy.run_path(str(Path(__file__).with_name("specialist-manager-gate.py")))

STATE_SCHEMA = "teams.state/v1"
PACKET_SCHEMA = "teams.packet/v1"
WORKER_RECEIPT_SCHEMA = "teams.receipt/v1"
CLOSEOUT_SCHEMA = "teams.closeout/v1"
CLOSEOUT_RECEIPT_SCHEMA = "teams.closeout-receipt/v1"
PACKET_PREFLIGHT_RECEIPT_SCHEMA = "teams.packet-preflight-receipt/v1"

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")

STATE_KEYS = {
    "schema", "tenant_id", "decision_id", "version", "supersedes", "manager",
    "issued_by", "issued_at", "effective_at", "intent", "canonical_facts",
    "invariants", "non_goals", "risk_tier", "affected_lanes", "required_receipts",
    "approval_boundaries", "status",
}
FINGERPRINT_KEYS = STATE_KEYS - {"status"}
PACKET_KEYS = {
    "schema", "packet_id", "tenant_id", "decision_id", "decision_version",
    "decision_fingerprint", "lane", "assigned_role", "execution_kind", "scope",
    "bounded_inputs", "allowed_actions", "prohibited_actions", "acceptance_criteria",
    "required_receipts", "idempotency_key", "retry_limit", "max_runtime_seconds",
    "rollback_handle", "status",
}
WORKER_RECEIPT_KEYS = {
    "schema", "receipt_id", "packet_id", "tenant_id", "decision_id",
    "decision_version", "decision_fingerprint", "lane", "worker_role",
    "execution_kind", "outcome", "actions", "artifacts", "checks", "side_effects",
    "rollback", "observed_state_fingerprint", "manager_gate_receipt_sha256",
    "started_at", "completed_at",
}
REVIEW_KEYS = {
    "receipt_id", "verdict", "actual_artifacts_inspected", "evidence_sha256",
}
CHECK_KEYS = {"check_id", "status", "evidence_sha256"}
SURFACE_KEYS = {
    "surface_id", "lane", "kind", "decision_version", "decision_fingerprint",
    "status", "evidence_sha256",
}
SUPERSEDED_KEYS = {"packet_id", "status", "evidence_sha256"}
APPROVAL_KEYS = {"boundary", "status", "evidence_sha256"}
ROLLBACK_KEYS = {"rollback_handle", "status", "evidence_sha256"}
VERIFICATION_KEYS = {
    "checks", "deterministic_surfaces", "superseded_work", "approvals",
    "rollback_proofs", "source_live_agree",
}
MANAGER_CLOSEOUT_KEYS = {
    "author", "decision_id", "decision_version", "decision_fingerprint", "ready",
    "delivery_requested", "raw_worker_output_forwarded", "summary_sha256",
    "deliberate_non_changes", "rollback_handles",
}
ROOT_KEYS = {
    "schema", "state", "packets", "receipts", "manager_reviews", "verification",
    "manager_closeout",
}


class ContractError(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label}:not_object")
    actual = set(value)
    if actual != keys:
        raise ContractError(
            f"{label}:keys:missing={sorted(keys - actual)}:extra={sorted(actual - keys)}"
        )
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise ContractError(f"{label}:invalid_identifier")
    return value


def _text(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ContractError(f"{label}:invalid_text")
    return value


def _sha(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ContractError(f"{label}:invalid_sha256")
    return value


def _integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ContractError(f"{label}:invalid_integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label}:not_boolean")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{label}:invalid:{value!r}")
    return value


def _strings(
    value: Any,
    label: str,
    *,
    identifiers: bool = False,
    shas: bool = False,
    nonempty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ContractError(f"{label}:invalid_list")
    result: list[str] = []
    for index, item in enumerate(value):
        if identifiers:
            result.append(_identifier(item, f"{label}[{index}]"))
        elif shas:
            result.append(str(_sha(item, f"{label}[{index}]")))
        else:
            result.append(str(_text(item, f"{label}[{index}]")))
    if len(result) != len(set(result)):
        raise ContractError(f"{label}:duplicates")
    return result


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    assert isinstance(text, str)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label}:invalid_timestamp") from exc
    return text


def decision_fingerprint(state: Any) -> str:
    """Validate a Team State Card and fingerprint only its decision fields."""
    root = _object(state, STATE_KEYS, "state")
    if root["schema"] != STATE_SCHEMA:
        raise ContractError("state.schema:invalid")
    _identifier(root["tenant_id"], "state.tenant_id")
    _identifier(root["decision_id"], "state.decision_id")
    _integer(root["version"], "state.version", minimum=1, maximum=2_147_483_647)
    _strings(root["supersedes"], "state.supersedes", identifiers=True)
    _identifier(root["manager"], "state.manager")
    _text(root["issued_by"], "state.issued_by")
    _timestamp(root["issued_at"], "state.issued_at")
    _text(root["effective_at"], "state.effective_at")
    _text(root["intent"], "state.intent")
    _strings(root["canonical_facts"], "state.canonical_facts", nonempty=True)
    _strings(root["invariants"], "state.invariants", nonempty=True)
    _strings(root["non_goals"], "state.non_goals")
    _integer(root["risk_tier"], "state.risk_tier", minimum=0, maximum=3)
    _strings(root["affected_lanes"], "state.affected_lanes", identifiers=True, nonempty=True)
    _strings(root["required_receipts"], "state.required_receipts", identifiers=True, nonempty=True)
    _strings(root["approval_boundaries"], "state.approval_boundaries", identifiers=True)
    _enum(
        root["status"],
        {
            "draft", "impact_mapped", "dispatched", "reviewing",
            "verification_pending", "ready_to_close", "blocked", "closed",
            "superseded",
        },
        "state.status",
    )
    return _canonical_sha({key: root[key] for key in sorted(FINGERPRINT_KEYS)})


def _validate_packet(
    value: Any, index: int, state: dict[str, Any], fingerprint: str
) -> dict[str, Any]:
    label = f"packets[{index}]"
    packet = _object(value, PACKET_KEYS, label)
    if packet["schema"] != PACKET_SCHEMA:
        raise ContractError(f"{label}.schema:invalid")
    _identifier(packet["packet_id"], f"{label}.packet_id")
    _identifier(packet["tenant_id"], f"{label}.tenant_id")
    _identifier(packet["decision_id"], f"{label}.decision_id")
    _integer(packet["decision_version"], f"{label}.decision_version", minimum=1, maximum=2_147_483_647)
    _sha(packet["decision_fingerprint"], f"{label}.decision_fingerprint")
    _identifier(packet["lane"], f"{label}.lane")
    _identifier(packet["assigned_role"], f"{label}.assigned_role")
    _enum(packet["execution_kind"], {"worker", "deterministic"}, f"{label}.execution_kind")
    _text(packet["scope"], f"{label}.scope")
    _strings(packet["bounded_inputs"], f"{label}.bounded_inputs", identifiers=True)
    _strings(packet["allowed_actions"], f"{label}.allowed_actions", identifiers=True)
    _strings(packet["prohibited_actions"], f"{label}.prohibited_actions", identifiers=True, nonempty=True)
    _strings(packet["acceptance_criteria"], f"{label}.acceptance_criteria", nonempty=True)
    _strings(packet["required_receipts"], f"{label}.required_receipts", identifiers=True, nonempty=True)
    _identifier(packet["idempotency_key"], f"{label}.idempotency_key")
    _integer(packet["retry_limit"], f"{label}.retry_limit", minimum=0, maximum=1)
    _integer(packet["max_runtime_seconds"], f"{label}.max_runtime_seconds", minimum=1, maximum=86_400)
    _text(packet["rollback_handle"], f"{label}.rollback_handle", nullable=True)
    _enum(packet["status"], {"queued", "running", "terminal", "superseded"}, f"{label}.status")
    if (
        packet["tenant_id"] != state["tenant_id"]
        or packet["decision_id"] != state["decision_id"]
        or packet["decision_version"] != state["version"]
        or packet["decision_fingerprint"] != fingerprint
    ):
        raise ContractError(f"{label}:stale_or_cross_tenant")
    if packet["lane"] not in state["affected_lanes"]:
        raise ContractError(f"{label}.lane:not_affected")
    return packet


def preflight_packet(state: Any, packet: Any) -> dict[str, Any]:
    """Fail closed unless a packet still matches the current mutable decision."""
    errors: list[str] = []
    tenant_id = "invalid"
    decision_id = "invalid"
    decision_version = 0
    decision_fingerprint_value = "0" * 64
    packet_id = "invalid"
    idempotency_key = "invalid"
    try:
        state_value = _object(state, STATE_KEYS, "state")
        decision_fingerprint_value = decision_fingerprint(state_value)
        tenant_id = state_value["tenant_id"]
        decision_id = state_value["decision_id"]
        decision_version = state_value["version"]
        packet_value = _validate_packet(
            packet, 0, state_value, decision_fingerprint_value
        )
        packet_id = packet_value["packet_id"]
        idempotency_key = packet_value["idempotency_key"]
        if state_value["status"] not in {
            "impact_mapped", "dispatched", "reviewing", "verification_pending",
        }:
            errors.append("state.status:not_mutable")
        if packet_value["status"] not in {"queued", "running"}:
            errors.append("packet.status:not_runnable")
    except ContractError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"internal_validation_error:{type(exc).__name__}")
    unique = sorted(set(errors))
    return {
        "schema": PACKET_PREFLIGHT_RECEIPT_SCHEMA,
        "status": "pass" if not unique else "block",
        "mutation_permitted": not unique,
        "tenant_id": tenant_id,
        "decision_id": decision_id,
        "decision_version": decision_version,
        "decision_fingerprint": decision_fingerprint_value,
        "packet_id": packet_id,
        "idempotency_key": idempotency_key,
        "errors": unique,
    }


def _validate_worker_receipt(
    value: Any,
    index: int,
    state: dict[str, Any],
    fingerprint: str,
    packets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    label = f"receipts[{index}]"
    receipt = _object(value, WORKER_RECEIPT_KEYS, label)
    if receipt["schema"] != WORKER_RECEIPT_SCHEMA:
        raise ContractError(f"{label}.schema:invalid")
    _identifier(receipt["receipt_id"], f"{label}.receipt_id")
    packet_id = _identifier(receipt["packet_id"], f"{label}.packet_id")
    packet = packets.get(packet_id)
    if packet is None:
        raise ContractError(f"{label}.packet_id:unknown")
    _identifier(receipt["tenant_id"], f"{label}.tenant_id")
    _identifier(receipt["decision_id"], f"{label}.decision_id")
    _integer(receipt["decision_version"], f"{label}.decision_version", minimum=1, maximum=2_147_483_647)
    _sha(receipt["decision_fingerprint"], f"{label}.decision_fingerprint")
    _identifier(receipt["lane"], f"{label}.lane")
    _identifier(receipt["worker_role"], f"{label}.worker_role")
    _enum(receipt["execution_kind"], {"worker", "deterministic"}, f"{label}.execution_kind")
    _enum(
        receipt["outcome"],
        {"accepted_candidate", "rework", "needs_human", "failed_safe", "superseded"},
        f"{label}.outcome",
    )
    _strings(receipt["actions"], f"{label}.actions", identifiers=True)
    _strings(receipt["artifacts"], f"{label}.artifacts", shas=True)
    _strings(receipt["checks"], f"{label}.checks", identifiers=True, nonempty=True)
    _strings(receipt["side_effects"], f"{label}.side_effects", identifiers=True)
    _text(receipt["rollback"], f"{label}.rollback", nullable=True)
    _sha(receipt["observed_state_fingerprint"], f"{label}.observed_state_fingerprint")
    _sha(receipt["manager_gate_receipt_sha256"], f"{label}.manager_gate_receipt_sha256", nullable=True)
    started = _timestamp(receipt["started_at"], f"{label}.started_at")
    completed = _timestamp(receipt["completed_at"], f"{label}.completed_at")
    completed_at = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
    if completed_at < started_at:
        raise ContractError(f"{label}:negative_runtime")
    if (
        receipt["tenant_id"] != state["tenant_id"]
        or receipt["decision_id"] != state["decision_id"]
        or receipt["decision_version"] != state["version"]
        or receipt["decision_fingerprint"] != fingerprint
        or receipt["observed_state_fingerprint"] != fingerprint
    ):
        raise ContractError(f"{label}:stale_contradictory_or_cross_tenant")
    if (
        receipt["lane"] != packet["lane"]
        or receipt["worker_role"] != packet["assigned_role"]
        or receipt["execution_kind"] != packet["execution_kind"]
    ):
        raise ContractError(f"{label}:packet_binding_mismatch")
    if receipt["receipt_id"] not in packet["required_receipts"]:
        raise ContractError(f"{label}:not_required_by_bound_packet")
    if receipt["execution_kind"] == "worker" and receipt["manager_gate_receipt_sha256"] is None:
        raise ContractError(f"{label}.manager_gate_receipt_sha256:required_for_worker")
    return receipt


def _validate_verification(value: Any, state: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    root = _object(value, VERIFICATION_KEYS, "verification")
    _boolean(root["source_live_agree"], "verification.source_live_agree")
    for index, item in enumerate(root["checks"] if isinstance(root["checks"], list) else []):
        label = f"verification.checks[{index}]"
        check = _object(item, CHECK_KEYS, label)
        _identifier(check["check_id"], f"{label}.check_id")
        _enum(check["status"], {"pass", "fail", "unverified"}, f"{label}.status")
        _sha(check["evidence_sha256"], f"{label}.evidence_sha256")
    if not isinstance(root["checks"], list) or not root["checks"]:
        raise ContractError("verification.checks:invalid_list")
    deterministic_surfaces = (
        root["deterministic_surfaces"]
        if isinstance(root["deterministic_surfaces"], list)
        else []
    )
    for index, item in enumerate(deterministic_surfaces):
        label = f"verification.deterministic_surfaces[{index}]"
        surface = _object(item, SURFACE_KEYS, label)
        _identifier(surface["surface_id"], f"{label}.surface_id")
        if _identifier(surface["lane"], f"{label}.lane") not in state["affected_lanes"]:
            raise ContractError(f"{label}.lane:not_affected")
        _enum(
            surface["kind"],
            {"script", "timer", "classifier", "publisher", "queue", "public_surface"},
            f"{label}.kind",
        )
        _integer(surface["decision_version"], f"{label}.decision_version", minimum=1, maximum=2_147_483_647)
        _sha(surface["decision_fingerprint"], f"{label}.decision_fingerprint")
        _enum(surface["status"], {"aligned", "quiesced", "stale", "contradictory", "unverified"}, f"{label}.status")
        _sha(surface["evidence_sha256"], f"{label}.evidence_sha256")
        if surface["decision_version"] != state["version"] or surface["decision_fingerprint"] != fingerprint:
            raise ContractError(f"{label}:stale_version")
    for index, item in enumerate(root["superseded_work"] if isinstance(root["superseded_work"], list) else []):
        label = f"verification.superseded_work[{index}]"
        work = _object(item, SUPERSEDED_KEYS, label)
        _identifier(work["packet_id"], f"{label}.packet_id")
        _enum(work["status"], {"stopped", "quiesced", "running", "mutated"}, f"{label}.status")
        _sha(work["evidence_sha256"], f"{label}.evidence_sha256")
    for index, item in enumerate(root["approvals"] if isinstance(root["approvals"], list) else []):
        label = f"verification.approvals[{index}]"
        approval = _object(item, APPROVAL_KEYS, label)
        _identifier(approval["boundary"], f"{label}.boundary")
        _enum(approval["status"], {"granted", "not_required", "pending", "denied"}, f"{label}.status")
        _sha(approval["evidence_sha256"], f"{label}.evidence_sha256")
    for index, item in enumerate(root["rollback_proofs"] if isinstance(root["rollback_proofs"], list) else []):
        label = f"verification.rollback_proofs[{index}]"
        proof = _object(item, ROLLBACK_KEYS, label)
        _identifier(proof["rollback_handle"], f"{label}.rollback_handle")
        _enum(proof["status"], {"proven", "executed", "restored", "missing", "failed"}, f"{label}.status")
        _sha(proof["evidence_sha256"], f"{label}.evidence_sha256")
    for key in ("deterministic_surfaces", "superseded_work", "approvals", "rollback_proofs"):
        if not isinstance(root[key], list):
            raise ContractError(f"verification.{key}:invalid_list")
    return root


def _verified_manager_gates(pairs):
    verified = {}
    for payload_path, receipt_path in pairs:
        payload_path = MANAGER_GATE["_artifact_path"](str(payload_path), "manager_gate.payload", nullable=False)
        receipt_path = MANAGER_GATE["_artifact_path"](str(receipt_path), "manager_gate.receipt", nullable=False)
        gate = MANAGER_GATE["_read_json"](payload_path, "manager_gate.payload")
        receipt = MANAGER_GATE["_read_json"](receipt_path, "manager_gate.receipt")
        digest = MANAGER_GATE["_file_sha256"](receipt_path, "manager_gate.receipt")
        if digest in verified:
            raise ContractError("manager_gates:duplicate_receipt")
        if (MANAGER_GATE["evaluate"](gate) != receipt
                or receipt.get("status") != "valid"
                or receipt.get("delivery_permitted") is not True):
            raise ContractError("manager_gates:revalidation_failed")
        verified[digest] = gate
    return verified


def evaluate(payload: Any, *, manager_gates=()) -> dict[str, Any]:
    errors: list[str] = []
    bindings: dict[str, str] = {}
    expected_receipts: list[str] = []
    actual_receipts: list[str] = []
    fingerprint = "0" * 64
    version = 0
    try:
        verified_gates = _verified_manager_gates(manager_gates)
        root = _object(payload, ROOT_KEYS, "root")
        if root["schema"] != CLOSEOUT_SCHEMA:
            raise ContractError("root.schema:invalid")
        state = _object(root["state"], STATE_KEYS, "state")
        fingerprint = decision_fingerprint(state)
        version = state["version"]
        expected_receipts = list(state["required_receipts"])
        if state["status"] not in {"ready_to_close", "closed"}:
            errors.append("state.status:not_ready_to_close")

        if not isinstance(root["packets"], list) or not root["packets"]:
            raise ContractError("packets:invalid_list")
        packets_list = [
            _validate_packet(value, index, state, fingerprint)
            for index, value in enumerate(root["packets"])
        ]
        packet_ids = [packet["packet_id"] for packet in packets_list]
        if len(packet_ids) != len(set(packet_ids)):
            raise ContractError("packets:duplicate_packet_id")
        idempotency_keys = [packet["idempotency_key"] for packet in packets_list]
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ContractError("packets:duplicate_idempotency_key")
        packets = {packet["packet_id"]: packet for packet in packets_list}
        if set(packet["lane"] for packet in packets_list) != set(state["affected_lanes"]):
            errors.append("packets:affected_lane_coverage_mismatch")
        if any(packet["status"] != "terminal" for packet in packets_list):
            errors.append("packets:nonterminal_or_superseded")
        packet_required_list = [
            receipt_id for packet in packets_list for receipt_id in packet["required_receipts"]
        ]
        packet_required = set(packet_required_list)
        if len(packet_required_list) != len(packet_required):
            errors.append("packets:receipt_assigned_to_multiple_packets")
        if packet_required != set(expected_receipts):
            errors.append("packets:required_receipt_coverage_mismatch")

        if not isinstance(root["receipts"], list):
            raise ContractError("receipts:invalid_list")
        receipts_list = [
            _validate_worker_receipt(value, index, state, fingerprint, packets)
            for index, value in enumerate(root["receipts"])
        ]
        actual_receipts = [receipt["receipt_id"] for receipt in receipts_list]
        if len(actual_receipts) != len(set(actual_receipts)):
            raise ContractError("receipts:duplicate_receipt_id")
        if set(actual_receipts) != set(expected_receipts):
            errors.append("receipts:required_set_mismatch")
        if any(receipt["outcome"] != "accepted_candidate" for receipt in receipts_list):
            errors.append("receipts:non_candidate_outcome")
        if any(
            receipt["execution_kind"] == "worker"
            and not {"credentialless_idle", "cross_tenant_isolated"}.issubset(
                set(receipt["checks"])
            )
            for receipt in receipts_list
        ):
            errors.append("receipts:worker_lifecycle_or_isolation_check_missing")
        for receipt in receipts_list:
            if receipt["execution_kind"] == "worker":
                gate_hash = receipt["manager_gate_receipt_sha256"]
                gate = verified_gates.get(gate_hash)
                if gate is None:
                    errors.append("manager_gates:required_receipt_missing")
                    continue
                worker = gate["worker"]
                if (worker["task_id"] != receipt["packet_id"]
                        or worker["profile"] != receipt["worker_role"]
                        or gate["validation"]["input_sha256"] != fingerprint
                        or worker["artifact_sha256"] not in receipt["artifacts"]):
                    errors.append("manager_gates:worker_binding_mismatch")
                bindings[f"manager_gate:{receipt['receipt_id']}"] = gate_hash
            bindings[f"receipt:{receipt['receipt_id']}"] = _canonical_sha(receipt)

        if not isinstance(root["manager_reviews"], list):
            raise ContractError("manager_reviews:invalid_list")
        reviews: dict[str, dict[str, Any]] = {}
        for index, value in enumerate(root["manager_reviews"]):
            label = f"manager_reviews[{index}]"
            review = _object(value, REVIEW_KEYS, label)
            receipt_id = _identifier(review["receipt_id"], f"{label}.receipt_id")
            if receipt_id in reviews:
                raise ContractError("manager_reviews:duplicate_receipt_id")
            _enum(review["verdict"], {"accepted", "rework", "needs-human", "retired"}, f"{label}.verdict")
            _boolean(review["actual_artifacts_inspected"], f"{label}.actual_artifacts_inspected")
            _sha(review["evidence_sha256"], f"{label}.evidence_sha256")
            reviews[receipt_id] = review
        if set(reviews) != set(expected_receipts):
            errors.append("manager_reviews:required_set_mismatch")
        if any(
            review["verdict"] != "accepted" or review["actual_artifacts_inspected"] is not True
            for review in reviews.values()
        ):
            errors.append("manager_reviews:not_all_accepted_and_inspected")

        verification = _validate_verification(root["verification"], state, fingerprint)
        for collection, key, error in (
            (verification["checks"], "check_id", "verification:duplicate_check_id"),
            (
                verification["deterministic_surfaces"],
                "surface_id",
                "verification:duplicate_surface_id",
            ),
            (
                verification["superseded_work"],
                "packet_id",
                "verification:duplicate_superseded_packet",
            ),
            (
                verification["approvals"],
                "boundary",
                "verification:duplicate_approval_boundary",
            ),
            (
                verification["rollback_proofs"],
                "rollback_handle",
                "verification:duplicate_rollback_handle",
            ),
        ):
            values = [item[key] for item in collection]
            if len(values) != len(set(values)):
                errors.append(error)
        if verification["source_live_agree"] is not True:
            errors.append("verification:source_live_disagree")
        if any(check["status"] != "pass" for check in verification["checks"]):
            errors.append("verification:check_not_pass")
        if any(
            surface["status"] not in {"aligned", "quiesced"}
            for surface in verification["deterministic_surfaces"]
        ):
            errors.append("verification:stale_or_unverified_surface")
        deterministic_lanes = {
            packet["lane"]
            for packet in packets_list
            if packet["execution_kind"] == "deterministic"
        }
        verified_surface_lanes = {
            surface["lane"] for surface in verification["deterministic_surfaces"]
        }
        if not deterministic_lanes.issubset(verified_surface_lanes):
            errors.append("verification:deterministic_lane_missing")
        if any(
            work["status"] not in {"stopped", "quiesced"}
            for work in verification["superseded_work"]
        ):
            errors.append("verification:superseded_work_not_stopped")
        approvals = {item["boundary"]: item for item in verification["approvals"]}
        if set(approvals) != set(state["approval_boundaries"]):
            errors.append("verification:approval_boundary_coverage_mismatch")
        if any(item["status"] not in {"granted", "not_required"} for item in approvals.values()):
            errors.append("verification:approval_unresolved")

        rollback_proofs = {
            item["rollback_handle"]: item for item in verification["rollback_proofs"]
        }
        mutating = [receipt for receipt in receipts_list if receipt["side_effects"]]
        required_handles: set[str] = set()
        for receipt in mutating:
            if receipt["rollback"] is None:
                errors.append(f"receipts:{receipt['receipt_id']}:rollback_missing")
            else:
                required_handles.add(receipt["rollback"])
        if not required_handles.issubset(rollback_proofs):
            errors.append("verification:rollback_proof_missing")
        if any(
            rollback_proofs[handle]["status"] not in {"proven", "executed", "restored"}
            for handle in required_handles & set(rollback_proofs)
        ):
            errors.append("verification:rollback_not_proven")

        closeout = _object(root["manager_closeout"], MANAGER_CLOSEOUT_KEYS, "manager_closeout")
        _identifier(closeout["author"], "manager_closeout.author")
        _identifier(closeout["decision_id"], "manager_closeout.decision_id")
        _integer(closeout["decision_version"], "manager_closeout.decision_version", minimum=1, maximum=2_147_483_647)
        _sha(closeout["decision_fingerprint"], "manager_closeout.decision_fingerprint")
        _boolean(closeout["ready"], "manager_closeout.ready")
        _boolean(closeout["delivery_requested"], "manager_closeout.delivery_requested")
        _boolean(closeout["raw_worker_output_forwarded"], "manager_closeout.raw_worker_output_forwarded")
        _sha(closeout["summary_sha256"], "manager_closeout.summary_sha256")
        _strings(closeout["deliberate_non_changes"], "manager_closeout.deliberate_non_changes")
        _strings(closeout["rollback_handles"], "manager_closeout.rollback_handles", identifiers=True)
        if (
            closeout["author"] != state["manager"]
            or closeout["decision_id"] != state["decision_id"]
            or closeout["decision_version"] != state["version"]
            or closeout["decision_fingerprint"] != fingerprint
        ):
            errors.append("manager_closeout:stale_or_wrong_owner")
        if closeout["ready"] is not True or closeout["delivery_requested"] is not True:
            errors.append("manager_closeout:not_ready_for_delivery")
        if closeout["raw_worker_output_forwarded"] is not False:
            errors.append("manager_closeout:raw_worker_output_forwarded")
        if set(closeout["rollback_handles"]) != required_handles:
            errors.append("manager_closeout:rollback_handle_mismatch")
        bindings["state"] = _canonical_sha(state)
        bindings["manager_closeout"] = _canonical_sha(closeout)
    except ContractError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"internal_validation_error:{type(exc).__name__}")

    unique = sorted(set(errors))
    return {
        "schema": CLOSEOUT_RECEIPT_SCHEMA,
        "status": "pass" if not unique else "block",
        "closure_permitted": not unique,
        "manager_loop_closeout_evidence": not unique,
        "decision_version": version,
        "decision_fingerprint": fingerprint,
        "expected_receipts": sorted(expected_receipts),
        "actual_receipts": sorted(actual_receipts),
        "artifact_bindings": dict(sorted(bindings.items())),
        "errors": unique,
    }


def _write_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    if os.name != "nt":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fingerprint-state", action="store_true")
    mode.add_argument("--preflight-packet", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--manager-gate", action="append", nargs=2, type=Path, default=[],
                        metavar=("PAYLOAD", "RECEIPT"), help="Exact worker manager-gate evidence; repeat for each worker")
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.input.read_text(encoding="utf-8"))
        if args.preflight_packet:
            if args.state is None:
                raise ContractError("--preflight-packet requires --state")
            state = json.loads(args.state.read_text(encoding="utf-8"))
            result = preflight_packet(state, value)
        elif args.fingerprint_state:
            result: dict[str, Any] = {
                "schema": "teams.state-fingerprint-receipt/v1",
                "status": "pass",
                "decision_fingerprint": decision_fingerprint(value),
                "errors": [],
            }
        else:
            result = evaluate(value, manager_gates=args.manager_gate)
    except Exception as exc:
        if args.preflight_packet:
            result = {
                "schema": PACKET_PREFLIGHT_RECEIPT_SCHEMA,
                "status": "block",
                "mutation_permitted": False,
                "tenant_id": "invalid",
                "decision_id": "invalid",
                "decision_version": 0,
                "decision_fingerprint": "0" * 64,
                "packet_id": "invalid",
                "idempotency_key": "invalid",
                "errors": [f"input:{type(exc).__name__}:{exc}"],
            }
        else:
            result = {
                "schema": CLOSEOUT_RECEIPT_SCHEMA,
                "status": "block",
                "closure_permitted": False,
                "manager_loop_closeout_evidence": False,
                "decision_version": 0,
                "decision_fingerprint": "0" * 64,
                "expected_receipts": [],
                "actual_receipts": [],
                "artifact_bindings": {},
                "errors": [f"input:{type(exc).__name__}:{exc}"],
            }
    if args.output:
        _write_private(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
