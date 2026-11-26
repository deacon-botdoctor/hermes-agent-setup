#!/usr/bin/env python3
"""Dry-run promotion wiring for self-editing v2 archives."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from self_editing_v2_archive import preflight_candidate, redact_json

PROMOTION_STATES = {
    "drafted",
    "acceptance_running",
    "accepted",
    "rejected",
    "approved_for_canary",
    "canary_passed",
    "promoted",
    "rolled_back",
    "retired",
}
APPROVAL_RUNGS = {"skill", "shared-rule", "config", "plugin", "sidecar", "upstream-pr", "patch"}
CANARY_PATH_BY_RUNG = {
    "skill": "mini-local-skill-draft",
    "shared-rule": "mini-local-shared-rule-draft",
    "config": "manual-config-canary-required",
    "plugin": "manual-plugin-canary-required",
    "sidecar": "manual-sidecar-canary-required",
    "upstream-pr": "upstream-review-required",
    "patch": "manual-patch-canary-required",
}


class PromotionError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def watch_until(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_accepted_archive(archive: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = archive.expanduser().resolve()
    metadata_path = root / "metadata.json"
    candidate_path = root / "candidate.json"
    decision_path = root / "decision.json"
    if not metadata_path.exists() or not candidate_path.exists() or not decision_path.exists():
        raise PromotionError(f"archive missing metadata, candidate, or decision: {root}")
    metadata = load_json(metadata_path)
    candidate = load_json(candidate_path)
    decision = load_json(decision_path)
    if decision.get("decision") != "accepted":
        raise PromotionError("promotion requires accepted decision.json")
    if metadata.get("decision") != "accepted" or metadata.get("state") not in {"eligible_for_approval", "accepted"}:
        raise PromotionError("promotion requires accepted archive metadata")
    preflight_candidate(candidate)
    return root, metadata, candidate, decision


def approval_required(candidate: dict[str, Any]) -> bool:
    return str(candidate.get("target_rung") or "").strip() in APPROVAL_RUNGS


def rollback_plan(candidate: dict[str, Any]) -> dict[str, Any]:
    rung = str(candidate.get("target_rung") or "unknown")
    return {
        "required": True,
        "plan": candidate.get("rollback_plan") or f"Retain archive, remove any staged {rung} draft/canary artifact, and mark promotion rolled_back before retry.",
        "retirement_condition": candidate.get("retirement_condition") or "Retire when the weakness cluster stops recurring for the watch window or a lower-rung fix supersedes it.",
    }


def render_approval_card(archive: Path, candidate: dict[str, Any], decision: dict[str, Any], promotion: dict[str, Any]) -> dict[str, Any]:
    safe_candidate = redact_json(candidate)
    title = safe_candidate.get("draft_skill_name") or safe_candidate.get("title") or safe_candidate.get("name") or archive.name
    rung = safe_candidate.get("target_rung")
    proposal = f"Approve canary for accepted self-edit candidate `{title}` at target rung `{rung}`. No live mutation has been performed."
    return {
        "id": f"self-edit-{promotion['candidate_id']}",
        "title": str(title),
        "proposal": proposal,
        "impact": f"Canary path: {promotion['canary_path']}; rollback: {promotion['rollback_plan']['plan']}",
        "requires_human_approval": True,
        "default_action": "hold",
        "schema_version": "self_edit_promotion_approval_card_v1",
        "created_at": utc_now(),
        "archive_path": str(archive),
        "candidate_id": promotion["candidate_id"],
        "target_rung": rung,
        "acceptance_summary": decision.get("summary") or {},
        "canary_path": promotion["canary_path"],
        "rollback_plan": promotion["rollback_plan"],
        "operator_actions": [
            "approve_for_canary",
            "reject",
            "retire",
        ],
        "text": f"Self-edit candidate accepted: {title}. Target rung: {rung}. Canary path: {promotion['canary_path']}. No live mutation has been performed.",
    }


def update_metadata(root: Path, promotion: dict[str, Any]) -> None:
    metadata_path = root / "metadata.json"
    metadata = load_json(metadata_path)
    metadata["promotion_state"] = promotion["state"]
    metadata["promotion_state_updated_at"] = promotion["created_at"]
    metadata["approval_card_path"] = promotion.get("approval_card_path")
    metadata["canary_path"] = promotion.get("canary_path")
    metadata["post_promotion_watch_until"] = promotion.get("post_promotion_watch_until")
    write_json(metadata_path, metadata)


def run_promotion_dry_run(archive: Path, *, force_approval: bool = False) -> dict[str, Any]:
    root, metadata, candidate, decision = load_accepted_archive(archive)
    candidate_id = str(metadata.get("candidate_id") or root.name)
    rung = str(candidate.get("target_rung") or "unknown")
    needs_approval = force_approval or approval_required(candidate)
    canary_path = CANARY_PATH_BY_RUNG.get(rung, "manual-review-required")
    promotion_dir = root / "promotion"
    promotion = {
        "schema_version": "self_edit_promotion_v1",
        "created_at": utc_now(),
        "candidate_id": candidate_id,
        "archive_path": str(root),
        "dry_run": True,
        "state": "accepted",
        "approval_required": needs_approval,
        "approval_card_path": None,
        "canary_path": canary_path if needs_approval else "silent-local-draft-retention",
        "rollback_plan": rollback_plan(candidate),
        "post_promotion_watch_until": watch_until(7),
        "live_mutation_performed": False,
        "notes": [],
    }
    if needs_approval:
        card = render_approval_card(root, candidate, decision, promotion)
        card_path = promotion_dir / "approval_card.json"
        write_json(card_path, card)
        promotion["approval_card_path"] = str(card_path)
        promotion["notes"].append("approval card rendered; waiting for operator approval before canary")
    else:
        promotion["notes"].append("low-risk candidate retained silently; no approval card emitted")
    write_json(promotion_dir / "state.json", promotion)
    update_metadata(root, promotion)
    return promotion


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run self-edit promotion wiring for accepted candidate archives")
    parser.add_argument("--archive", required=True, help="accepted candidate archive directory")
    parser.add_argument("--force-approval", action="store_true", help="render approval card even for normally silent low-rung candidates")
    args = parser.parse_args()
    try:
        result = run_promotion_dry_run(Path(args.archive), force_approval=args.force_approval)
    except PromotionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
