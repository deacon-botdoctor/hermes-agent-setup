#!/usr/bin/env python3
"""Self-editing v2 candidate archive and outside-loop preflight helpers."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*[^\s'\"`]+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[a-z0-9._\-]+"),
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-(?:api|oat|admin)[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"[0-9]{8,12}:AA[A-Za-z0-9_-]{30,}"),
]

ALLOWED_TARGET_RUNGS = {"memory", "skill", "shared-rule", "config", "plugin", "sidecar", "upstream-pr", "patch"}
HELD_OUT_STATUSES = {"not_run", "sealed", "pending", "passed", "failed", "waived"}
LOCKED_SURFACE_GLOBS = [
    "bin/self_editing_v2_archive.py",
    "bin/self_editing_v2_acceptance.py",
    "bin/self_editing_v2_miner.py",
    "bin/self_editing_v2_promotion.py",
    "bin/self_editing_v2_lifecycle.py",
    "bin/hermes-reflect-candidates-to-drafts.py",
    "bin/self-improvement-trigger.py",
    "shared-rules/self-edit-outside-loop.md",
    "shared-rules/golden-boundary*.md",
    "schemas/harness/approval-card.schema.json",
    "crons/*self-improvement*",
    "crons/operator-crons.json",
]


class ProposalValidationError(ValueError):
    pass


class LockedSurfaceError(ProposalValidationError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "candidate").strip().lower()).strip("-")
    return text[:72] or "candidate"


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]" if m.groups() else "[REDACTED]", redacted)
    return redacted


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if re.search(r"(?i)(api[_-]?key|token|secret|password|authorization)", str(key)):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_json(item)
        return out
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def stable_id(candidate: dict[str, Any]) -> str:
    basis = json.dumps({
        "name": candidate.get("draft_skill_name") or candidate.get("title") or candidate.get("name"),
        "pattern": candidate.get("pattern") or candidate.get("trigger") or candidate.get("summary"),
        "target_rung": candidate.get("target_rung"),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def validate_proposal(candidate: dict[str, Any]) -> None:
    target_rung = str(candidate.get("target_rung") or "").strip()
    if target_rung not in ALLOWED_TARGET_RUNGS:
        raise ProposalValidationError(f"target_rung required and must be one of {sorted(ALLOWED_TARGET_RUNGS)}")
    held_out_status = str(candidate.get("held_out_status") or "not_run")
    if held_out_status not in HELD_OUT_STATUSES:
        raise ProposalValidationError(f"held_out_status must be one of {sorted(HELD_OUT_STATUSES)}")
    if target_rung in {"sidecar", "upstream-pr", "patch"} and not candidate.get("change_ladder_reason"):
        raise ProposalValidationError("code/harness proposals require change_ladder_reason")
    if target_rung == "patch":
        for key in ("route", "reason", "retirement_condition"):
            if not candidate.get(key):
                raise ProposalValidationError(f"patch proposals require {key}")


def canonical_repo_path(path: str) -> str:
    value = str(path)
    raw = value.strip().replace("\\", "/")
    if raw != value.replace("\\", "/"):
        raise LockedSurfaceError("whitespace-padded proposed paths are blocked: " + str(path))
    if any(ord(ch) < 32 for ch in raw):
        raise LockedSurfaceError("control characters are blocked in proposed paths: " + str(path))
    if Path(raw).is_absolute() or raw.startswith("/"):
        raise LockedSurfaceError("absolute proposed paths are blocked; use repo-relative paths: " + str(path))
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise LockedSurfaceError("parent traversal is blocked in proposed paths: " + str(path))
        parts.append(part)
    return "/".join(parts)


def is_locked_path(path: str) -> bool:
    normalized = canonical_repo_path(path)
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in LOCKED_SURFACE_GLOBS)


def preflight_proposal_changes(paths: list[str], *, override: bool = False) -> None:
    canonical_paths = [canonical_repo_path(path) for path in paths]
    locked = sorted({path for path in canonical_paths if is_locked_path(path)})
    if locked and not override:
        raise LockedSurfaceError("locked outside-loop surface edit blocked: " + ", ".join(locked))


def preflight_candidate(candidate: dict[str, Any], *, override_locked_surfaces: bool = False) -> None:
    validate_proposal(candidate)
    paths = []
    for key in ("proposed_paths", "changed_paths"):
        value = candidate.get(key) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise ProposalValidationError(f"{key} must be a list of repo-relative paths")
        paths.extend(value)
    normalized_paths = []
    for path in paths:
        if not isinstance(path, (str, Path)):
            raise ProposalValidationError("proposed path entries must be strings")
        normalized_paths.append(str(path))
    preflight_proposal_changes(normalized_paths, override=override_locked_surfaces)


def append_archive_index(archive_root: Path, metadata: dict[str, Any], candidate: dict[str, Any]) -> None:
    row = {
        "candidate_id": metadata.get("candidate_id"),
        "created_at": metadata.get("created_at"),
        "state": metadata.get("state"),
        "target_rung": metadata.get("target_rung"),
        "held_out_status": metadata.get("held_out_status"),
        "archive_path": metadata.get("archive_path"),
        "name": candidate.get("draft_skill_name") or candidate.get("title") or candidate.get("name"),
        "source": candidate.get("source"),
    }
    index_path = archive_root / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    fd, tmp_name = tempfile.mkstemp(prefix="index-", suffix=".jsonl", dir=str(index_path.parent))
    tmp = Path(tmp_name)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(existing)
        f.write(json.dumps(row, sort_keys=True) + "\n")
    tmp.replace(index_path)



def _candidate_archive_id(candidate: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = slug(str(candidate.get("draft_skill_name") or candidate.get("title") or candidate.get("name") or "candidate"))
    return f"{timestamp}-{name}-{stable_id(candidate)}"


def _write_archive_record(
    candidate: dict[str, Any],
    *,
    hermes_home: Path,
    state: str,
    report: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    decision: str | None = None,
    decision_reason: str | None = None,
) -> Path:
    archive_root = hermes_home / "state" / "self-edit-archive"
    candidate_id = _candidate_archive_id(candidate)
    dest = archive_root / candidate_id
    dest.mkdir(parents=True, exist_ok=False)

    sanitized_candidate = redact_json(candidate)
    sanitized_report = redact_json(report or {})
    sanitized_evidence = redact_json(evidence or {})

    metadata = {
        "schema_version": "self_edit_archive_v1",
        "candidate_id": candidate_id,
        "created_at": utc_now(),
        "state": state,
        "target_rung": sanitized_candidate.get("target_rung"),
        "held_out_status": sanitized_candidate.get("held_out_status", "not_run"),
        "archive_path": str(dest),
        "supersedes": sanitized_candidate.get("supersedes"),
        "superseded_by": None,
        "decision": decision,
        "decision_reason": decision_reason,
    }
    (dest / "candidate.json").write_text(json.dumps(sanitized_candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (dest / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (dest / "evidence.json").write_text(json.dumps({"report": sanitized_report, "evidence": sanitized_evidence}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (dest / "README.DO_NOT_EXECUTE").write_text("Self-edit candidate archive only. Do not execute or promote without acceptance runner receipt and approval gate.\n", encoding="utf-8")
    append_archive_index(archive_root, metadata, sanitized_candidate)
    return dest


def write_rejected_candidate_archive(
    candidate: dict[str, Any],
    *,
    hermes_home: Path,
    reason: str,
    report: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> Path:
    normalized = dict(candidate)
    normalized.setdefault("held_out_status", "not_run")
    return _write_archive_record(
        normalized,
        hermes_home=hermes_home,
        state="rejected",
        report=report,
        evidence=evidence,
        decision="rejected",
        decision_reason=reason,
    )

def write_candidate_archive(
    candidate: dict[str, Any],
    *,
    hermes_home: Path,
    report: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> Path:
    normalized = dict(candidate)
    normalized.setdefault("held_out_status", "not_run")
    preflight_candidate(normalized)

    dest = _write_archive_record(
        normalized,
        hermes_home=hermes_home,
        state="drafted",
        report=report,
        evidence=evidence,
    )
    normalized["archive_path"] = str(dest)
    return dest
