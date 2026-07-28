#!/usr/bin/env python3
"""Emit deterministic provenance for Golden's deployable components.

The manifest intentionally separates code that executes inside an agent from
host/baseline wiring. Operator-control and documentation files are excluded, so
advancing Golden main does not by itself imply an agent-runtime rollout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

COMPONENT_PATHS = {
    "runtime_payload": (
        "bin/hermes-client-day-review",
        "bin/hermes-reflect-candidates-to-drafts.py",
        "bin/hermes-self-reflect.py",
        "bin/hermes-skillify-autopromote.py",
        "bin/client-local-evidence-query.py",
        "bin/papercut_inbox.py",
        "bin/registry-sync.py",
        "bin/self-improvement-trigger.py",
        "bin/telegram-transaction-canary.py",
        "hooks/gbrain-capture",
        "hooks/telegram-transcript",
        "kit/bin/papercut.py",
        "mcp-servers/capability-router",
        "patches/apply-all-patches.py",
        "patches/apply-session-search-current-topic.py",
        "patches/apply-skill-index-allowlist.py",
        "patches/apply-telegram-immediate-typing-receipt.py",
        "patches/modules/__init__.py",
        "patches/modules/assembled_runtime_contract.py",
        "patches/modules/auto_resume_contextual_reset_v1.py",
        "patches/modules/codex_401_paid_fallback_circuit_v1.py",
        "patches/modules/cron_scheduler_can_dispatch_compat_v1.py",
        "patches/modules/durable_drain_compatibility_v1.py",
        "patches/modules/durable_drain_inbox_carrier_v1.py",
        "patches/modules/gateway_runtime_root_guard_v1.py",
        "patches/modules/mcp_in_turn_refresh_v1.py",
        "patches/modules/mcp_legacy_alias_dispatch_v1.py",
        "patches/modules/mcp_legacy_cold_alias_activation_v1.py",
        "patches/modules/registry_loader.py",
        "patches/modules/restart_interruption_checkin_v1.py",
        "patches/modules/telegram_dm_topic_recovery_root_guard_v1.py",
        "patches/modules/telegram_fresh_topic_continuity_v1.py",
        "patches/modules/telegram_organic_long_running_checkpoints_v1.py",
        "patches/modules/telegram_poll_liveness_writer_v1.py",
        "patches/modules/telegram_transaction_canary_v1.py",
        "patches/modules/windows_gateway_task_identity_v1.py",
        "patches/payloads/durable-drain-inbox-v1",
        "patches/payloads/session-search-current-topic-v2",
        "patches/payloads/telegram-transaction-canary/gateway/telegram_transaction_ledger.py",
        "patches/registry.yaml",
        "kit/systemd/hermes-gateway.service.d/30-media-allow-dirs.conf",
        "plugins/botdoctor-immersion",
        "plugins/mcp-on-demand-control",
        "plugins/task-ledger",
        "plugins/telegram-transcript",
        "scripts/merge-shared-defaults.py",
        "shared-defaults",
        "shared-rules/client-isolation.md",
        "shared-rules/content-policy.md",
        "shared-rules/file-delivery.md",
        "shared-rules/westminster-marque.md",
        "shared-rules/truth-over-comfort.md",
        "skills/fleet/nightly-client-reflection-default",
        "skills/fleet/papercuts",
        "upstream.lock",
    ),
    "baseline_wiring": (
        "bin/tool-doctor.py",
        "kit/bin/auth-preflight.py",
        "kit/bin/probe-client-health.py",
        "kit/bin/probe-client-health.ps1",
        "kit/bin/start-hermes.ps1",
        "kit/bin/start-hermes.sh",
    ),
}

COMPONENT_EXCLUDES = {
    "runtime_payload": (),
    "baseline_wiring": (),
}

ANALYSIS_ONLY_PATHS = {
    "agent-standards.md",
    "bin/tool-readiness-probe.py",
    "bin/runtime-payload-manifest.py",
    "bin/test/test_patch_registry.py",
    "bin/test/test_apply_session_search_current_topic.py",
    "bin/test/test_botdoctor_immersion_operating_floor.py",
    "bin/test/test_runtime_payload_manifest.py",
    "bin/test/test_telegram_fresh_topic_continuity_v1.py",
    "bin/test/test_telegram_transcript_reconciled.py",
    "docs/plans/golden-overlay-dry-v2.md",
    "docs/wiki/patch-registry.md",
    "scripts/tests/test_merge_shared_defaults.py",
    "kit/bin/hermes-overlay-acceptance.py",
    "kit/bin/install-mcp-call-classifier.sh",
    "kit/runtime/mcp_call_classifier.py",
    "patches/modules/bounded_context_compression_v1.py",
    "patches/modules/client_local_evidence_optin_v1.py",
    "patches/modules/explicit_compression_threshold_v1.py",
    "patches/modules/identity_trust_invariant_v1.py",
    "patches/modules/mcp_call_tool_doctor_recovery_v2.py",
}

ANALYSIS_ONLY_PREFIXES = (
    "bin/test/",
    "docs/",
    "kit/docs/",
    "tests/",
)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or f"git {' '.join(args)} failed").strip())
    return proc.stdout


def resolve_ref(repo: Path, ref: str) -> str:
    return str(git(repo, "rev-parse", f"{ref}^{{commit}}")).strip()


def canonical_upstream_sha(repo: Path, ref: str) -> str:
    """Read the exact upstream commit pinned by Golden at ``ref``."""
    try:
        lock = str(git(repo, "show", f"{ref}:upstream.lock"))
    except RuntimeError:
        return ""
    match = re.search(r"(?m)^commit:\s*([0-9a-f]{40})\s*$", lock)
    return match.group(1) if match else ""


def component_for_path(path: str) -> str | None:
    path = path.strip("/")
    for component, prefixes in COMPONENT_PATHS.items():
        if is_component_excluded(path, component):
            continue
        for prefix in prefixes:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return component
    return None


def is_analysis_only_path(path: str) -> bool:
    path = path.strip("/")
    return path in ANALYSIS_ONLY_PATHS or path.startswith(ANALYSIS_ONLY_PREFIXES)


def is_component_excluded(path: str, component: str) -> bool:
    return any(path == excluded.rstrip("/") or path.startswith(excluded) for excluded in COMPONENT_EXCLUDES[component])


def component_manifest(repo: Path, ref: str, component: str) -> dict[str, Any]:
    pathspecs = COMPONENT_PATHS[component]
    raw = str(git(repo, "ls-tree", "-r", ref, "--", *pathspecs))
    entries: list[dict[str, str]] = []
    canonical: list[str] = []
    for line in raw.splitlines():
        metadata, path = line.split("\t", 1)
        if is_component_excluded(path, component):
            continue
        mode, kind, blob = metadata.split()
        entries.append({"path": path, "mode": mode, "type": kind, "blob": blob})
        canonical.append(f"{mode} {kind} {blob}\t{path}\n")
    digest = hashlib.sha256("".join(canonical).encode()).hexdigest()
    return {
        "component": component,
        "digest": digest,
        "file_count": len(entries),
        "paths": list(pathspecs),
        "excluded_paths": list(COMPONENT_EXCLUDES[component]),
        "files": entries,
    }


def build_manifest(repo: Path, ref: str) -> dict[str, Any]:
    sha = resolve_ref(repo, ref)
    components = {name: component_manifest(repo, sha, name) for name in sorted(COMPONENT_PATHS)}
    combined = "".join(f"{name}:{components[name]['digest']}\n" for name in sorted(components))
    return {
        "schema_version": 1,
        "kind": "golden_runtime_payload_manifest",
        "golden_sha": sha,
        "canonical_upstream_sha": canonical_upstream_sha(repo, sha),
        "deployment_digest": hashlib.sha256(combined.encode()).hexdigest(),
        "components": components,
    }


def load_source_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != "golden_runtime_payload_manifest"
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("golden_sha") or "")) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("canonical_upstream_sha") or "")) is None
        or not isinstance(payload.get("components"), dict)
    ):
        raise ValueError("invalid precomputed Golden source manifest")
    return payload


def changed_paths(repo: Path, base_sha: str, target_sha: str) -> list[str]:
    raw = str(git(repo, "diff", "--name-only", base_sha, target_sha))
    return [line.strip() for line in raw.splitlines() if line.strip()]


def is_runtime_state_or_artifact(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if any(
        part in {"venv", ".venv", "node_modules"} or part.endswith(".egg-info")
        for part in parts
    ):
        return True
    if name in {"config.yaml", ".env", "auth.json", ".install_method"}:
        return True
    if "__pycache__" in parts[:-1] or name.endswith((".pyc", ".pyo")):
        return True
    if "data" in parts[:-1] and name.endswith(".db"):
        return True
    if any(part in {"memories", "projects", "skills", "state"} for part in parts[:-1]):
        return True
    return bool(re.search(r"\.(?:bak|backup)(?:[-.].*)?$", name)) or name.endswith("~")


def _runtime_file_identity(
    target: Path, declared: dict[str, str] | None, windows: bool
) -> tuple[dict[str, str] | None, str | None]:
    mode = target.lstat().st_mode
    if stat.S_ISLNK(mode):
        content = os.readlink(target).encode()
        actual_mode = "120000"
    else:
        content = target.read_bytes()
        actual_mode = "100755" if mode & stat.S_IXUSR else "100644"
    actual_sha = hashlib.sha256(content).hexdigest()
    if declared is None:
        return {
            "sha256": actual_sha,
            "mode": actual_mode,
            "type": "blob",
        }, None
    declared_sha = str(declared.get("sha256") or "")
    declared_mode = str(declared.get("mode") or "")
    if declared.get("type") != "blob":
        return None, "declared object type is not a blob"
    if windows:
        if actual_sha != declared_sha:
            if stat.S_ISLNK(mode) or b"\r\n" not in content:
                return None, "content hash does not match the declared runtime file"
            normalized = content.replace(b"\r\n", b"\n")
            if hashlib.sha256(normalized).hexdigest() != declared_sha:
                return None, "content hash does not match the declared runtime file"
        return {
            "sha256": declared_sha,
            "mode": declared_mode,
            "type": "blob",
        }, None
    if actual_sha != declared_sha:
        return None, "content hash does not match the declared runtime file"
    if actual_mode != declared_mode:
        return None, "mode does not match the declared runtime file"
    return {
        "sha256": actual_sha,
        "mode": actual_mode,
        "type": "blob",
    }, None


def runtime_fingerprint(
    runtime_dir: Path,
    expected_upstream_sha: str,
    expected_golden_sha: str,
    expected_files: dict[str, dict[str, str]] | None = None,
    windows: bool | None = None,
) -> dict[str, Any]:
    """Fingerprint deviations in a clean-base runtime after Golden is applied.

    Callers must build the runtime from a clean upstream checkout. The resulting
    exact hashes can then be supplied to the live-tree classifier; no marker or
    path heuristic is needed.
    """
    upstream_sha = resolve_ref(runtime_dir, "HEAD")
    if not expected_upstream_sha:
        return {
            "verified": False,
            "upstream_sha": upstream_sha,
            "expected_upstream_sha": "",
            "golden_sha": expected_golden_sha,
            "reason": "Golden upstream.lock lacks an exact canonical commit",
        }
    if upstream_sha != expected_upstream_sha:
        return {
            "verified": False,
            "upstream_sha": upstream_sha,
            "expected_upstream_sha": expected_upstream_sha,
            "golden_sha": expected_golden_sha,
            "reason": "runtime HEAD does not equal Golden's canonical upstream pin",
        }
    unstaged = str(git(runtime_dir, "diff", "--name-only")).splitlines()
    staged = str(git(runtime_dir, "diff", "--cached", "--name-only", "HEAD")).splitlines()
    untracked = str(git(runtime_dir, "ls-files", "--others", "--exclude-standard")).splitlines()
    ignored = str(git(runtime_dir, "ls-files", "--others", "--ignored", "--exclude-standard")).splitlines()
    unsafe_ignored = sorted(
        path.strip() for path in ignored if path.strip() and not is_runtime_state_or_artifact(path.strip())
    )
    if unsafe_ignored:
        return {
            "verified": False,
            "upstream_sha": upstream_sha,
            "expected_upstream_sha": expected_upstream_sha,
            "golden_sha": expected_golden_sha,
            "reason": "ignored non-data runtime paths prevent complete provenance",
            "ignored_non_data_paths": unsafe_ignored,
        }
    windows = os.name == "nt" if windows is None else windows
    observed_paths = {
        path.strip() for path in unstaged + staged + untracked if path.strip()
    }
    expected_paths = set(expected_files or {})
    unexpected = sorted(
        path
        for path in observed_paths - expected_paths
        if not is_runtime_state_or_artifact(path)
    )
    if expected_files is not None and unexpected:
        return {
            "verified": False,
            "upstream_sha": upstream_sha,
            "expected_upstream_sha": expected_upstream_sha,
            "golden_sha": expected_golden_sha,
            "reason": "runtime contains undeclared non-data changes",
            "unexpected_paths": unexpected,
        }
    paths = sorted(expected_paths if expected_files is not None else observed_paths)
    files: dict[str, dict[str, str]] = {}
    excluded: list[str] = []
    for path in paths:
        if is_runtime_state_or_artifact(path):
            excluded.append(path)
            continue
        target = runtime_dir / path
        if not target.is_file() and not target.is_symlink():
            return {
                "verified": False,
                "upstream_sha": upstream_sha,
                "expected_upstream_sha": expected_upstream_sha,
                "golden_sha": expected_golden_sha,
                "reason": f"declared runtime file is missing: {path}",
            }
        identity, reason = _runtime_file_identity(
            target, (expected_files or {}).get(path), windows
        )
        if reason:
            return {
                "verified": False,
                "upstream_sha": upstream_sha,
                "expected_upstream_sha": expected_upstream_sha,
                "golden_sha": expected_golden_sha,
                "reason": f"{path}: {reason}",
            }
        assert identity is not None
        files[path] = identity
    canonical = "".join(
        f"{path}\t{files[path]['mode']}\t{files[path]['type']}\t{files[path]['sha256']}\n" for path in sorted(files)
    )
    return {
        "verified": True,
        "upstream_sha": upstream_sha,
        "expected_upstream_sha": expected_upstream_sha,
        "golden_sha": expected_golden_sha,
        "digest": hashlib.sha256(canonical.encode()).hexdigest(),
        "file_count": len(files),
        "files": files,
        "excluded_state_or_artifacts": excluded,
    }


def compare_manifests(repo: Path, base_ref: str, target_ref: str) -> dict[str, Any]:
    base = build_manifest(repo, base_ref)
    target = build_manifest(repo, target_ref)
    changed_components = [
        name
        for name in sorted(COMPONENT_PATHS)
        if base["components"][name]["digest"] != target["components"][name]["digest"]
    ]
    paths = changed_paths(repo, base["golden_sha"], target["golden_sha"])

    def summary(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "golden_sha": manifest["golden_sha"],
            "canonical_upstream_sha": manifest["canonical_upstream_sha"],
            "deployment_digest": manifest["deployment_digest"],
            "components": {
                name: {
                    "digest": component["digest"],
                    "file_count": component["file_count"],
                }
                for name, component in manifest["components"].items()
            },
        }

    return {
        "schema_version": 1,
        "kind": "golden_candidate_impact",
        "base_sha": base["golden_sha"],
        "target_sha": target["golden_sha"],
        "changed_components": changed_components,
        "requires_runtime_rollout": "runtime_payload" in changed_components,
        "requires_baseline_rollout": "baseline_wiring" in changed_components,
        "requires_runtime_mutation": bool(changed_components),
        "changed_paths": paths,
        "unowned_changed_paths": [
            path for path in paths if component_for_path(path) is None and not is_analysis_only_path(path)
        ],
        "base": summary(base),
        "target": summary(target),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--base-ref", help="Compare this ref with --ref")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Clean upstream checkout after Golden apply; adds exact generated runtime fingerprints",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    repo = args.repo.expanduser().resolve()
    if args.source_manifest:
        if args.base_ref:
            parser.error("--source-manifest cannot be combined with --base-ref")
        payload = load_source_manifest(args.source_manifest.expanduser().resolve())
    else:
        payload = compare_manifests(repo, args.base_ref, args.ref) if args.base_ref else build_manifest(repo, args.ref)
    if args.runtime_dir:
        target = payload["target"] if args.base_ref else payload
        payload["runtime_fingerprint"] = runtime_fingerprint(
            args.runtime_dir.expanduser().resolve(),
            str(target.get("canonical_upstream_sha") or ""),
            str(target.get("golden_sha") or ""),
            (
                target.get("assembled_runtime_fingerprint", {}).get("files")
                if isinstance(target.get("assembled_runtime_fingerprint"), dict)
                else None
            ),
        )
    text = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if args.compact
        else json.dumps(payload, indent=2, sort_keys=True)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
