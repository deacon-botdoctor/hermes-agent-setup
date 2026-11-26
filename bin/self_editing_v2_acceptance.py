#!/usr/bin/env python3
"""Two-split acceptance runner for self-editing v2 candidate archives."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from self_editing_v2_archive import ProposalValidationError, preflight_candidate


class AcceptanceError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def candidate_archive_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not (root / "metadata.json").exists() or not (root / "candidate.json").exists():
        raise AcceptanceError(f"not a candidate archive: {root}")
    metadata = load_json(root / "metadata.json")
    if metadata.get("state") != "drafted":
        raise AcceptanceError(f"acceptance requires drafted archive state, got {metadata.get('state')}")
    candidate = load_json(root / "candidate.json")
    try:
        preflight_candidate(candidate)
    except ProposalValidationError as exc:
        raise AcceptanceError("candidate preflight failed: " + str(exc)) from exc
    return root


def load_cases(path: Path | None, archive: Path) -> list[dict[str, Any]]:
    source = path.expanduser() if path else archive / "acceptance-cases.json"
    if not source.exists():
        raise AcceptanceError(f"missing acceptance cases: {source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        cases = data.get("cases")
    else:
        cases = data
    if not isinstance(cases, list):
        raise AcceptanceError("acceptance cases must be a list or {'cases': [...]} object")
    return cases


def canonical_source_path(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip().replace("\\", "/")
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return str(PurePosixPath(*parts)) if parts else None

def boolish(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "pass", "passed", "ok"}


def score_value(value: Any, *, fallback: float, field: str) -> float:
    if value is None:
        return fallback
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(f"{field} must be numeric") from exc
    if not math.isfinite(score):
        raise AcceptanceError(f"{field} must be finite")
    return score


def normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise AcceptanceError("each acceptance case must be an object")
    split = str(case.get("split") or "").strip()
    if split not in {"held_in", "held_out"}:
        raise AcceptanceError("acceptance case split must be held_in or held_out")
    check = case.get("expected_check") if isinstance(case.get("expected_check"), dict) else {}
    if "before_passed" not in case and "before_passed" not in check:
        raise AcceptanceError("acceptance case missing before_passed: " + str(case.get("id") or case.get("name") or "case"))
    if "after_passed" not in case and "after_passed" not in check:
        raise AcceptanceError("acceptance case missing after_passed: " + str(case.get("id") or case.get("name") or "case"))
    before_passed = boolish(case.get("before_passed", check.get("before_passed")), default=False)
    after_passed = boolish(case.get("after_passed", check.get("after_passed")), default=False)
    before_score = score_value(case.get("before_score", check.get("before_score")), fallback=1.0 if before_passed else 0.0, field="before_score")
    after_score = score_value(case.get("after_score", check.get("after_score")), fallback=1.0 if after_passed else 0.0, field="after_score")
    improved = after_passed and ((not before_passed) or after_score > before_score)
    regressed = (before_passed and not after_passed) or after_score < before_score
    return {
        "id": str(case.get("id") or case.get("name") or "case"),
        "split": split,
        "source_path": canonical_source_path(case.get("source_path")),
        "before_passed": before_passed,
        "after_passed": after_passed,
        "before_score": before_score,
        "after_score": after_score,
        "improved": improved,
        "regressed": regressed,
    }


def evaluate_cases(cases: list[dict[str, Any]], *, min_held_out: int = 1) -> dict[str, Any]:
    if min_held_out < 1:
        raise AcceptanceError("min_held_out must be at least 1")
    normalized = [normalize_case(case) for case in cases]
    held_in = [case for case in normalized if case["split"] == "held_in"]
    held_out = [case for case in normalized if case["split"] == "held_out"]
    reasons: list[str] = []
    held_in_ids = {case["id"] for case in held_in if case.get("id")}
    held_out_ids = {case["id"] for case in held_out if case.get("id")}
    duplicate_ids = sorted(held_in_ids & held_out_ids)
    held_in_sources = {case["source_path"] for case in held_in if case.get("source_path")}
    held_out_sources = {case["source_path"] for case in held_out if case.get("source_path")}
    duplicate_sources = sorted(held_in_sources & held_out_sources)
    if duplicate_ids:
        reasons.append("held-in/held-out case ids overlap: " + ", ".join(duplicate_ids))
    if duplicate_sources:
        reasons.append("held-in/held-out source paths overlap: " + ", ".join(duplicate_sources))
    if not held_in:
        reasons.append("missing held-in cases")
    if len(held_out) < min_held_out:
        reasons.append(f"missing held-out cases: need {min_held_out}, got {len(held_out)}")
    held_in_improved = bool(held_in) and any(case["improved"] for case in held_in) and all(case["after_passed"] for case in held_in)
    held_out_regressed = any(case["regressed"] for case in held_out)
    held_out_failed = any(not case["after_passed"] for case in held_out)
    if held_in and not held_in_improved:
        reasons.append("held-in did not strictly improve")
    if held_out_regressed:
        reasons.append("held-out regressed")
    if held_out_failed:
        reasons.append("held-out failed")
    accepted = not reasons
    return {
        "accepted": accepted,
        "state": "eligible_for_approval" if accepted else "rejected",
        "reasons": reasons,
        "cases": normalized,
        "summary": {
            "held_in": len(held_in),
            "held_out": len(held_out),
            "held_in_improved": held_in_improved,
            "held_out_regressed": held_out_regressed,
            "held_out_failed": held_out_failed,
        },
    }


def update_metadata(archive: Path, state: str, decision: str, reason: str) -> None:
    metadata_path = archive / "metadata.json"
    metadata = load_json(metadata_path)
    metadata["state"] = state
    metadata["decision"] = decision
    metadata["decision_reason"] = reason
    metadata["accepted_at"] = utc_now() if decision == "accepted" else None
    metadata["rejected_at"] = utc_now() if decision == "rejected" else None
    write_json(metadata_path, metadata)


def run_acceptance(archive: Path, *, cases_path: Path | None = None, min_held_out: int = 1) -> dict[str, Any]:
    root = candidate_archive_root(archive)
    cases = load_cases(cases_path, root)
    result = evaluate_cases(cases, min_held_out=min_held_out)
    decision = "accepted" if result["accepted"] else "rejected"
    reason = "two-split acceptance passed" if result["accepted"] else "; ".join(result["reasons"])
    payload = {
        "schema_version": "self_edit_acceptance_v1",
        "created_at": utc_now(),
        "archive_path": str(root),
        "decision": decision,
        "state": result["state"],
        "decision_reason": reason,
        "summary": result["summary"],
        "cases": result["cases"],
    }
    write_json(root / "decision.json", payload)
    update_metadata(root, result["state"], decision, reason)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run two-split acceptance over a self-edit candidate archive")
    parser.add_argument("--archive", required=True, help="candidate archive directory")
    parser.add_argument("--cases", help="acceptance cases JSON path; defaults to archive/acceptance-cases.json")
    parser.add_argument("--min-held-out", type=int, default=1, help="minimum held-out cases required")
    args = parser.parse_args()
    if args.min_held_out < 1:
        print(json.dumps({"ok": False, "error": "min-held-out must be at least 1"}, indent=2, sort_keys=True))
        return 2
    try:
        result = run_acceptance(
            Path(args.archive),
            cases_path=Path(args.cases).expanduser() if args.cases else None,
            min_held_out=args.min_held_out,
        )
    except AcceptanceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps({"ok": result["decision"] == "accepted", **result}, indent=2, sort_keys=True))
    return 0 if result["decision"] == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
