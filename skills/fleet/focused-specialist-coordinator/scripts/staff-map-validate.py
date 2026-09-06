#!/usr/bin/env python3
"""Validate one tenant-local specialist staff and job ownership map."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "specialist-staff-map/v1"
ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ROLE_STATUSES = {"manual", "shadow", "on-demand", "retired"}
AUTHORITIES = {"read-only", "proposal-only", "draft-only"}
JOB_SOURCES = {"hermes-cron", "os-scheduler", "manual"}
DISPOSITIONS = {
    "primary",
    "deterministic-service",
    "specialist",
    "operator-service",
    "retire",
}


class MapError(ValueError):
    pass


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MapError(f"{label}:not_object")
    actual = set(value)
    if actual != keys:
        raise MapError(
            f"{label}:keys:missing={sorted(keys - actual)}:extra={sorted(actual - keys)}"
        )
    return value


def _string(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MapError(f"{label}:invalid_string")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MapError(f"{label}:invalid:{value!r}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise MapError(f"{label}:not_boolean")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise MapError(f"{label}:invalid_integer")
    return value


def evaluate(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    role_ids: set[str] = set()
    jobs_seen: set[tuple[str, str]] = set()
    role_count = 0
    job_count = 0
    enabled_job_count = 0
    try:
        root = _exact(payload, {"schema", "runtime", "roles", "jobs", "dispatch"}, "root")
        if root["schema"] != SCHEMA:
            raise MapError("schema:invalid")
        runtime = _exact(root["runtime"], {"id", "primary_manager", "platform"}, "runtime")
        _string(runtime["id"], "runtime.id")
        primary_manager = _string(runtime["primary_manager"], "runtime.primary_manager")
        _enum(runtime["platform"], {"linux", "mac", "windows"}, "runtime.platform")

        dispatch = _exact(root["dispatch"], {"shared_dispatcher", "new_schedule"}, "dispatch")
        if _boolean(dispatch["shared_dispatcher"], "dispatch.shared_dispatcher"):
            errors.append("shared_dispatcher_must_remain_false")
        if _boolean(dispatch["new_schedule"], "dispatch.new_schedule"):
            errors.append("new_schedule_must_remain_false")

        roles = root["roles"]
        if not isinstance(roles, list):
            raise MapError("roles:not_array")
        profiles: set[str] = set()
        boards: set[str] = set()
        role_status: dict[str, str] = {}
        for index, value in enumerate(roles):
            label = f"roles[{index}]"
            role = _exact(
                value,
                {
                    "id",
                    "display_name",
                    "profile",
                    "board",
                    "one_job",
                    "status",
                    "skill_bundle",
                    "connector_allowlist",
                    "authority",
                    "input_schema",
                    "output_schema",
                    "max_runtime_seconds",
                    "retry_limit",
                },
                label,
            )
            role_id = _string(role["id"], f"{label}.id")
            if not ROLE_ID_RE.fullmatch(role_id):
                errors.append(f"{label}.id:invalid_format")
            if role_id in role_ids:
                errors.append(f"{label}.id:duplicate")
            role_ids.add(role_id)
            profile = _string(role["profile"], f"{label}.profile")
            board = _string(role["board"], f"{label}.board")
            if profile in profiles:
                errors.append(f"{label}.profile:duplicate")
            if board in boards:
                errors.append(f"{label}.board:duplicate")
            profiles.add(profile)
            boards.add(board)
            _string(role["display_name"], f"{label}.display_name")
            _string(role["one_job"], f"{label}.one_job")
            status = _enum(role["status"], ROLE_STATUSES, f"{label}.status")
            role_status[role_id] = status
            _string(role["skill_bundle"], f"{label}.skill_bundle")
            connectors = role["connector_allowlist"]
            if (
                not isinstance(connectors, list)
                or any(not isinstance(item, str) or not item.strip() for item in connectors)
                or len(connectors) != len(set(connectors))
            ):
                errors.append(f"{label}.connector_allowlist:invalid")
            _enum(role["authority"], AUTHORITIES, f"{label}.authority")
            _string(role["input_schema"], f"{label}.input_schema")
            _string(role["output_schema"], f"{label}.output_schema")
            _integer(role["max_runtime_seconds"], f"{label}.max_runtime_seconds", 1, 3600)
            _integer(role["retry_limit"], f"{label}.retry_limit", 0, 1)
        role_count = len(roles)

        jobs = root["jobs"]
        if not isinstance(jobs, list):
            raise MapError("jobs:not_array")
        for index, value in enumerate(jobs):
            label = f"jobs[{index}]"
            job = _exact(
                value,
                {
                    "source",
                    "id",
                    "enabled",
                    "disposition",
                    "deterministic_executor",
                    "specialist_role",
                    "side_effect_owner",
                    "review_owner",
                },
                label,
            )
            source = _enum(job["source"], JOB_SOURCES, f"{label}.source")
            job_id = _string(job["id"], f"{label}.id")
            key = (source, job_id)
            if key in jobs_seen:
                errors.append(f"{label}:duplicate_job")
            jobs_seen.add(key)
            enabled = _boolean(job["enabled"], f"{label}.enabled")
            disposition = _enum(job["disposition"], DISPOSITIONS, f"{label}.disposition")
            executor = _string(
                job["deterministic_executor"],
                f"{label}.deterministic_executor",
                nullable=True,
            )
            specialist = _string(
                job["specialist_role"],
                f"{label}.specialist_role",
                nullable=True,
            )
            side_effect_owner = _string(job["side_effect_owner"], f"{label}.side_effect_owner")
            review_owner = _string(job["review_owner"], f"{label}.review_owner")
            if side_effect_owner in role_ids or side_effect_owner in profiles:
                errors.append(f"{label}:specialist_cannot_own_side_effects")
            if enabled:
                enabled_job_count += 1
                if disposition == "retire":
                    errors.append(f"{label}:enabled_job_cannot_retire")
            elif disposition != "retire":
                errors.append(f"{label}:disabled_job_must_remain_retire_only")

            if disposition == "specialist":
                if specialist not in role_ids:
                    errors.append(f"{label}:unknown_specialist_role")
                elif role_status.get(specialist) == "retired":
                    errors.append(f"{label}:retired_specialist_role")
                if executor is None or side_effect_owner != executor:
                    errors.append(f"{label}:specialist_side_effects_must_stay_deterministic")
                if review_owner != primary_manager:
                    errors.append(f"{label}:specialist_review_owner_must_be_primary")
            elif specialist is not None:
                errors.append(f"{label}:specialist_role_requires_specialist_disposition")

            if disposition == "deterministic-service":
                if executor is None or side_effect_owner != executor:
                    errors.append(f"{label}:deterministic_owner_mismatch")
            elif disposition == "primary":
                if side_effect_owner != primary_manager:
                    errors.append(f"{label}:primary_side_effect_owner_mismatch")
                if review_owner != primary_manager:
                    errors.append(f"{label}:primary_review_owner_mismatch")
            elif disposition == "operator-service":
                if executor is None or side_effect_owner != executor:
                    errors.append(f"{label}:operator_service_owner_mismatch")
                if review_owner != primary_manager:
                    errors.append(f"{label}:operator_service_review_owner_mismatch")
            elif disposition == "retire":
                if enabled or executor is not None or side_effect_owner != "none":
                    errors.append(f"{label}:retire_binding_must_be_inert")
        job_count = len(jobs)
    except MapError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"internal_validation_error:{type(exc).__name__}")

    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        canonical = repr(payload).encode("utf-8", errors="replace")
        errors.append("input:not_json_serializable")
    unique = sorted(set(errors))
    return {
        "schema": "specialist-staff-map-validation/v1",
        "status": "valid" if not unique else "invalid",
        "input_sha256": hashlib.sha256(canonical).hexdigest(),
        "role_count": role_count,
        "job_count": job_count,
        "enabled_job_count": enabled_job_count,
        "errors": unique,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        receipt = evaluate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        receipt = {
            "schema": "specialist-staff-map-validation/v1",
            "status": "invalid",
            "errors": [f"input:{type(exc).__name__}"],
        }
    json.dump(receipt, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if receipt["status"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
