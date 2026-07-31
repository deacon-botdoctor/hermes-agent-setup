#!/usr/bin/env python3
# ruff: noqa: E501, F811
"""Apply all Hermes patches to a fresh install.

Usage:
  python3 apply-all-patches.py [--hermes-dir /path/to/hermes-agent]

All patches are idempotent — safe to run multiple times.
Skipped patches will print "already patched".
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _runtime_fingerprint_matches(runtime_dir: Path, fingerprint: dict) -> bool:
    def git_lines(*args: str) -> list[str] | None:
        proc = subprocess.run(
            ["git", "-C", str(runtime_dir), *args], capture_output=True, text=True, check=False, timeout=30
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()] if proc.returncode == 0 else None

    changed: set[str] = set()
    for args in (("diff", "--name-only"), ("diff", "--cached", "--name-only", "HEAD"), ("ls-files", "--others", "--exclude-standard")):
        paths = git_lines(*args)
        if paths is None:
            return False
        changed.update(paths)
    ignored = git_lines("ls-files", "--others", "--ignored", "--exclude-standard")
    if ignored is None:
        return False

    def is_state(path: str) -> bool:
        parts = path.split("/")
        name = parts[-1]
        return (
            name in {"config.yaml", ".env", "auth.json"}
            or "__pycache__" in parts[:-1]
            or name.endswith((".pyc", ".pyo", "~"))
            or ("data" in parts[:-1] and name.endswith(".db"))
            or any(part in {"memories", "projects", "skills", "state"} for part in parts[:-1])
            or bool(re.search(r"\.(?:bak|backup)(?:[-.].*)?$", name))
        )

    if any(not is_state(path) for path in ignored):
        return False
    files = fingerprint["files"]
    live_paths = {path for path in changed if not is_state(path)}
    if live_paths != set(files):
        return False
    for path in live_paths:
        target = runtime_dir / path
        if not target.is_file() and not target.is_symlink():
            return False
        mode = target.lstat().st_mode
        if stat.S_ISLNK(mode):
            content, git_mode = os.readlink(target).encode(), "120000"
        else:
            content = target.read_bytes()
            git_mode = "100755" if mode & stat.S_IXUSR else "100644"
        if files[path] != {"sha256": hashlib.sha256(content).hexdigest(), "mode": git_mode, "type": "blob"}:
            return False
    return True


def _validated_runtime_fingerprint(
    manifest: object, current_manifest: dict, runtime_head: str, runtime_dir: Path | None = None
) -> dict | None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        return None
    fingerprint = manifest.get("runtime_fingerprint")
    if not isinstance(fingerprint, dict) or fingerprint.get("verified") is not True:
        return None
    golden_sha = current_manifest.get("golden_sha")
    expected_upstream_sha = current_manifest.get("canonical_upstream_sha")
    if (
        manifest.get("kind") != "golden_runtime_payload_manifest"
        or manifest.get("golden_sha") != golden_sha
        or fingerprint.get("golden_sha") != golden_sha
        or fingerprint.get("upstream_sha") != runtime_head
        or fingerprint.get("expected_upstream_sha") != expected_upstream_sha
        or runtime_head != expected_upstream_sha
    ):
        return None
    files = fingerprint.get("files")
    file_count = fingerprint.get("file_count")
    digest = fingerprint.get("digest")
    if (
        not isinstance(files, dict)
        or not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count != len(files)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        return None
    if not all(isinstance(path, str) for path in files):
        return None
    canonical = []
    for path in sorted(files):
        identity = files[path]
        if (
            not path
            or "\t" in path
            or "\n" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(identity, dict)
            or identity.get("mode") not in {"100644", "100755", "120000"}
            or identity.get("type") != "blob"
            or not isinstance(identity.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is None
        ):
            return None
        canonical.append(f"{path}\t{identity['mode']}\t{identity['type']}\t{identity['sha256']}\n")
    if hashlib.sha256("".join(canonical).encode()).hexdigest() != digest:
        return None
    if runtime_dir is not None and not _runtime_fingerprint_matches(runtime_dir, fingerprint):
        return None
    return fingerprint


def _resolve_patch_list_via_registry(patches_dir: Path) -> list:
    """Try to load patches from registry.yaml via registry_loader.

    Returns a list of (name, callable) tuples ordered by registry, or None
    if registry loading is unavailable. Callers must fail closed.
    """
    try:
        from patches.modules.registry_loader import (
            build_ordered_patch_list,
            load_registry,
            load_skip_list,
        )
    except ImportError:
        # Fallback: try relative import when running as a script
        try:
            loader_path = patches_dir / "modules" / "registry_loader.py"
            if not loader_path.exists():
                return None
            spec = importlib.util.spec_from_file_location("registry_loader", loader_path)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            load_registry = mod.load_registry
            build_ordered_patch_list = mod.build_ordered_patch_list
            load_skip_list = mod.load_skip_list
        except Exception as e:
            print(f"[registry] WARN: could not import registry_loader: {e}")
            return None

    registry_entries = load_registry(patches_dir / "registry.yaml")
    if not registry_entries:
        return None

    skip_names = load_skip_list()

    ordered = build_ordered_patch_list(
        registry_entries,
        monolith_patches={},
        patches_dir=patches_dir,
        skip_names=skip_names,
    )

    if not ordered:
        return None

    expected_names = {
        str(entry.get("name") or "unknown")
        for entry in registry_entries
        if not (str(entry.get("name") or "unknown") in skip_names and not (entry.get("aliases") or []))
        and not bool(entry.get("optional"))
    }
    resolved_names = {name for name, _fn in ordered}
    unresolved = sorted(expected_names - resolved_names)
    if unresolved:
        raise RuntimeError("registry entries failed to resolve: " + ", ".join(unresolved))

    return ordered


PATCH_VERIFIER_VERSION = "anchor-miss-fatal-v2"

# Sentinel a patch function may RETURN (instead of True/False) to declare
# that it inspected the tree and its precondition is ABSENT: there was
# nothing to fix, so its idempotency marker is legitimately never written.
# Recorded as status NOT-APPLICABLE and exempt from marker verification —
# but ONLY because the module itself attested precondition-absent. A patch
# that returns True/False (APPLIED/IDEMPOTENT) with a missing marker is
# still a fatal ANCHOR-MISS (the IDEMPOTENT-lie failure mode stays dead).
PATCH_NOT_APPLICABLE = "not-applicable"
STATUS_NOT_APPLICABLE = "NOT-APPLICABLE"


def _host_level_target_absent(entry: dict, hermes_dir: Path) -> bool:
    """Return True for optional host-level registry targets absent from both roots.

    Covers host-level (non-agent-tree) artifact roots: bin/, crons/, hooks/,
    LaunchAgents, and MCP server trees (mcp-servers/, local-mcp-servers/).
    mcp-servers/ added 2026-07-02: anamnesis server.py is an Enoch-only
    artifact, so its recall patches must classify as env-skip (not
    ANCHOR-MISS) on client hosts where the server is absent.
    """
    target = str(entry.get("target") or "").strip()
    if not target:
        return False
    if not (
        target.startswith(("bin/", "crons/", "hooks/", "mcp-servers/", "local-mcp-servers/")) or "LaunchAgent" in target
    ):
        return False
    roots = [hermes_dir, hermes_dir.parent]
    parts = [p.strip() for chunk in target.split("+") for p in chunk.split(",") if p.strip()]
    if not parts:
        return False
    for part in parts:
        if "*" in part:
            if any(any(root.glob(part)) for root in roots):
                return False
        elif any((root / part).exists() for root in roots):
            return False
    return True


def _post_verify_markers(hermes_dir: Path, patches_dir: Path, results: list) -> list:
    """Marker-verify APPLIED/IDEMPOTENT outcomes so a silent anchor-miss can
    never be recorded as success again (2026-07-02 v0.18.0 bump lesson: 50
    anchor-misses were receipted as IDEMPOTENT). Best-effort: reuses the
    rehearsal harness's derive_markers when available. Annotates each result
    with marker_verified True/False/None and returns suspected anchor-misses."""
    for r in results:
        r.setdefault("marker_verified", None)
    try:
        import importlib.util
        import subprocess

        import yaml

        ubr_path = patches_dir.parent / "bin" / "upstream-bump-rehearsal.py"
        spec = importlib.util.spec_from_file_location("_ubr_verify", str(ubr_path))
        ubr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ubr)
        registry = yaml.safe_load((patches_dir / "registry.yaml").read_text())["patches"]
    except Exception as e:
        print(f"[post_verify] skipped (rehearsal harness/registry unavailable: {e})")
        return []
    entries = {e["name"]: e for e in registry}
    try:
        monolith_src = Path(__file__).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        monolith_src = ""
    env_skip = getattr(ubr, "ENV_SKIP_PATCHES", None) or getattr(ubr, "ENV_SKIP", set()) or set()
    suspects = []
    for r in results:
        name, status = r["patch"], r["status"]
        # NOT-APPLICABLE (module-attested precondition-absent) is exempt from
        # marker verification by design; ERROR/SKIPPED are handled elsewhere.
        if not (status == "APPLIED" or status.startswith("IDEMPOTENT")):
            continue
        entry = entries.get(name)
        if entry is None:
            continue
        src = ""
        module_rel = entry.get("module", "") or ""
        mod_path = patches_dir / module_rel
        if hasattr(ubr, "load_patcher_source"):
            try:
                src = ubr.load_patcher_source(entry, patches_dir, monolith_src)
            except Exception:
                src = ""
        elif module_rel == "monolith":
            src = monolith_src
        elif module_rel and mod_path.is_file():
            try:
                src = mod_path.read_text()
            except Exception:
                pass
        try:
            markers, how = ubr.derive_markers(entry, src)
        except Exception:
            markers, how = [], "error"
        if not markers or (isinstance(how, str) and how.endswith("unmatched")):
            r["marker_verified"] = None
            continue
        if status.startswith("IDEMPOTENT") and _host_level_target_absent(entry, hermes_dir):
            r["marker_verified"] = None
            continue
        if how == "tree-path":
            hermes_home = hermes_dir.parent
            present = all((hermes_dir / m).exists() or (hermes_home / m).exists() for m in markers)
        else:
            grep_roots = [str(hermes_dir)]
            hermes_home = hermes_dir.parent
            for rel in ("bin", "hooks", "crons", "mcp-servers", "local-mcp-servers"):
                scoped = hermes_home / rel
                if scoped.exists():
                    grep_roots.append(str(scoped))
            grep = shutil.which("grep")
            if grep:
                present = any(
                    subprocess.run(
                        [
                            grep,
                            "-rq",
                            "--exclude-dir=.git",
                            "--exclude-dir=venv",
                            "--exclude-dir=node_modules",
                            "--exclude=*.bak*",
                            "--exclude=*.orig",
                            "--exclude=*.rej",
                            "-F",
                            m,
                            *grep_roots,
                        ],
                    ).returncode
                    == 0
                    for m in markers
                )
            else:
                excluded_dirs = {".git", "venv", "node_modules", "__pycache__"}

                def marker_in_tree(marker: str) -> bool:
                    needle = marker.encode("utf-8")
                    for root in map(Path, grep_roots):
                        for path in root.rglob("*"):
                            if any(part in excluded_dirs for part in path.parts) or not path.is_file():
                                continue
                            if path.suffix in {".orig", ".rej"} or ".bak" in path.name:
                                continue
                            try:
                                if needle in path.read_bytes():
                                    return True
                            except (OSError, PermissionError):
                                continue
                    return False

                present = any(marker_in_tree(m) for m in markers)
        r["marker_verified"] = bool(present)
        if not present and name not in env_skip:
            suspects.append(name)
            if status == "APPLIED" or status.startswith("IDEMPOTENT"):
                r["status"] = "ANCHOR-MISS"
    if suspects:
        print(
            f"[post_verify] WARNING: {len(suspects)} patch(es) reported success but "
            f"their markers are ABSENT from the tree (possible silent anchor-miss): " + ", ".join(suspects)
        )
    else:
        print("[post_verify] all verifiable success markers present in tree")
    return suspects


_SNAPSHOT_EXCLUDE_PARTS = {
    "venv",
    "node_modules",
    "__pycache__",
    ".git",
    "apply-snapshots",
}
_SNAPSHOT_EXCLUDE_NAMES = {
    "durable-threads.db",
    "durable-threads.db-wal",
    "durable-threads.db-shm",
    "telegram-transcript.db",
    "telegram-transcript.db-wal",
    "telegram-transcript.db-shm",
    "memory.db",
    "memory.db-wal",
    "memory.db-shm",
}
_SNAPSHOT_FILE_SIZE_CAP = 5 * 1024 * 1024
_RETAINED_RUNTIME_BACKUP_SUFFIXES = (
    ".bak-pre-telegram-transaction-canary-v1",
)


def _hermes_home_for(hermes_dir: Path) -> Path:
    """Resolve the runtime home that owns a hermes-agent tree."""
    if hermes_dir.parent.name == ".hermes":
        return hermes_dir.parent
    candidate = hermes_dir.parent / ".hermes"
    return candidate if candidate.exists() else hermes_dir.parent


def _create_apply_snapshot(hermes_home: Path, patcher_sha: str) -> Path | None:
    """Create the pre-apply rollback artifact owned by this patcher."""
    import datetime
    import hashlib
    import io
    import json
    import socket
    import tarfile

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    snapshot = hermes_home / "state" / "apply-snapshots" / f"{stamp}-{socket.gethostname().split('.')[0]}"
    try:
        snapshot.mkdir(parents=True)
        manifest = {
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "hermes_home": str(hermes_home),
            "patcher_sha256": patcher_sha,
            "files": {},
            "skipped_large": [],
            "errors": [],
        }
        with tarfile.open(snapshot / "snapshot.tar.gz", "w:gz") as archive:
            for path in hermes_home.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(hermes_home)
                if _SNAPSHOT_EXCLUDE_PARTS.intersection(relative.parts):
                    continue
                if path.name in _SNAPSHOT_EXCLUDE_NAMES or path.suffix in {".pyc", ".pyo"}:
                    continue
                try:
                    stat = path.stat()
                    if stat.st_size > _SNAPSHOT_FILE_SIZE_CAP:
                        manifest["skipped_large"].append({"path": str(relative), "size": stat.st_size})
                        continue
                    content = path.read_bytes()
                    info = tarfile.TarInfo(str(relative))
                    info.size = len(content)
                    info.mtime = int(stat.st_mtime)
                    info.mode = stat.st_mode & 0o777
                    archive.addfile(info, io.BytesIO(content))
                    manifest["files"][str(relative)] = {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                except OSError as exc:
                    manifest["errors"].append({"path": str(relative), "error": str(exc)[:200]})
        (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return snapshot
    except Exception as exc:
        print(f"[snapshot] ERROR: {exc}")
        return None


def _prune_old_snapshots(hermes_home: Path, keep: int = 10, hold_days: int = 7) -> int:
    """Keep ten newest snapshots and every snapshot inside the safety hold."""
    import shutil
    import time

    root = hermes_home / "state" / "apply-snapshots"
    snapshots = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True) if root.exists() else []
    cutoff = time.time() - (hold_days * 86400)
    removable = [old for old in snapshots[keep:] if old.stat().st_mtime < cutoff]
    for old in removable:
        shutil.rmtree(old)
    return len(removable)


def _runtime_backup_paths(hermes_dir: Path) -> set[Path]:
    """Return in-tree patch backups relative to the runtime root."""
    backups = set()
    for path in hermes_dir.rglob("*"):
        if not path.is_file():
            continue
        source_name, separator, _label = path.name.rpartition(".bak-")
        if (
            separator
            and source_name
            and re.fullmatch(r"[A-Za-z0-9_-]+", _label)
            and path.with_name(source_name).is_file()
        ):
            backups.add(path.relative_to(hermes_dir))
    return backups


def _remove_new_runtime_backups(
    hermes_dir: Path, hermes_home: Path, snapshot: Path, preexisting: set[Path]
) -> list[str]:
    """Remove disposable backups from a verified run, preserving retained suffixes."""
    try:
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    snapshot_files = manifest.get("files")
    if not isinstance(snapshot_files, dict):
        return []

    created = sorted(_runtime_backup_paths(hermes_dir) - preexisting)
    removable = []
    for relative in created:
        if relative.name.endswith(_RETAINED_RUNTIME_BACKUP_SUFFIXES):
            continue
        source_name, separator, _label = relative.name.rpartition(".bak-")
        if not separator or not source_name:
            continue
        source = hermes_dir / relative.with_name(source_name)
        if not source.is_file():
            continue
        try:
            source_relative = source.relative_to(hermes_home)
        except ValueError:
            continue
        if str(source_relative) not in snapshot_files:
            continue
        (hermes_dir / relative).unlink()
        removable.append(str(relative))
    return removable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-dir", default=None)
    ap.add_argument(
        "--allow-anchor-miss",
        action="store_true",
        help="Permit ANCHOR-MISS outcomes to exit zero while still recording them",
    )
    args = ap.parse_args()

    if args.hermes_dir:
        hermes_dir = Path(args.hermes_dir).resolve()
    else:
        home = Path.home()
        candidates = [home / ".hermes" / "hermes-agent", home / "hermes-agent"]
        hermes_dir = next((c for c in candidates if c.exists()), None)
        if hermes_dir is None:
            print("ERROR: could not find hermes-agent dir. Use --hermes-dir")
            sys.exit(1)

    print(f"Applying patches to: {hermes_dir}")
    print()

    preexisting_runtime_backups = _runtime_backup_paths(hermes_dir)

    pre_apply_clean_base = False
    if (hermes_dir / ".git").exists():
        pre_status = subprocess.run(
            ["git", "-C", str(hermes_dir), "status", "--porcelain"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        pre_apply_clean_base = pre_status.returncode == 0 and not pre_status.stdout.strip()

    hermes_home = _hermes_home_for(hermes_dir)
    snapshot = None
    if os.environ.get("HERMES_APPLY_SKIP_SNAPSHOT"):
        print("[snapshot] SKIPPED (HERMES_APPLY_SKIP_SNAPSHOT set)")
    else:
        patcher_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        snapshot = _create_apply_snapshot(hermes_home, patcher_sha)
        if snapshot is None:
            print("[snapshot] FATAL: rollback snapshot was not created; refusing to patch")
            return 4
        print(f"[snapshot] CREATED {snapshot}")

    # --- Registry-driven execution; missing registry/modules fail closed ---
    patches_dir = Path(__file__).resolve().parent
    patch_list = _resolve_patch_list_via_registry(patches_dir)
    if patch_list is None:
        print("[dispatch] FATAL: registry unavailable; refusing legacy monolith fallback")
        return 2
    dispatch_mode = "registry"
    print(f"[dispatch] Using registry_loader ({len(patch_list)} patches resolved)")

    print()

    applied = 0
    results = []
    critical_failures = []
    for name, fn in patch_list:
        outcome = "SKIPPED"
        try:
            result = fn(hermes_dir)
            if isinstance(result, str) and result.strip().lower() == PATCH_NOT_APPLICABLE:
                # Module-attested precondition-absent no-op. Never inferred:
                # only the patch function's own return value produces this.
                outcome = STATUS_NOT_APPLICABLE
            elif result:
                applied += 1
                outcome = "APPLIED"
            else:
                outcome = "IDEMPOTENT"
        except Exception as e:
            print(f"[{name}] ERROR: {e}")
            outcome = f"ERROR: {e}"
        results.append({"patch": name, "status": outcome, "marker_verified": None})

    print()
    print(f"Done — {applied} patch(es) applied.")

    # Compile gate: never leave clients with syntactically broken runtime files.
    compile_targets = [
        hermes_dir / "agent" / "conversation_loop.py",
        hermes_dir / "gateway" / "run.py",
        hermes_dir / "gateway" / "platforms" / "base.py",
        hermes_dir / "gateway" / "platforms" / "telegram.py",
        hermes_dir / "plugins" / "platforms" / "telegram" / "adapter.py",
    ]
    compile_failures = []
    python_exe = hermes_dir / "venv" / "bin" / "python"
    if not python_exe.exists():
        python_exe = Path(sys.executable)
    for target in compile_targets:
        if not target.exists():
            continue
        try:
            proc = subprocess.run(
                [str(python_exe), "-m", "py_compile", str(target)],
                cwd=str(hermes_dir),
                text=True,
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:
            compile_failures.append(f"{target}: {exc}")
            continue
        if proc.returncode != 0:
            compile_failures.append(f"{target}: {(proc.stderr or proc.stdout).strip()}")
    if compile_failures:
        print("[compile_gate] ERROR: patched runtime failed py_compile")
        for failure in compile_failures:
            print(f"[compile_gate] {failure}")
        critical_failures.extend(compile_failures)
    else:
        print("[compile_gate] OK")

    try:
        try:
            from patches.modules.assembled_runtime_contract import (
                verify_conversation_loop_agent_contract,
            )
        except ImportError:
            verifier_path = patches_dir / "modules" / "assembled_runtime_contract.py"
            spec = importlib.util.spec_from_file_location("assembled_runtime_contract", verifier_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load assembly verifier: {verifier_path}")
            verifier = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(verifier)
            verify_conversation_loop_agent_contract = verifier.verify_conversation_loop_agent_contract

        verify_conversation_loop_agent_contract(hermes_dir)
        print("[assembly_gate] OK")
    except Exception as exc:
        failure = f"assembled runtime contract failed: {exc}"
        print(f"[assembly_gate] ERROR: {failure}")
        critical_failures.append(failure)

    anchor_miss_suspects = _post_verify_markers(hermes_dir, patches_dir, results)
    anchor_miss = [r["patch"] for r in results if r["status"] == "ANCHOR-MISS"]
    failures = [r for r in results if str(r.get("status", "")).startswith(("ERROR", "FAIL"))]
    if anchor_miss:
        print("[post_verify] FATAL: ANCHOR-MISS outcome(s) detected: " + ", ".join(anchor_miss))
        if args.allow_anchor_miss:
            print("[post_verify] WARNING: --allow-anchor-miss supplied; exiting zero despite ANCHOR-MISS outcome(s)")
    if failures:
        print(
            "[patches] FATAL: FAIL outcome(s) detected: " + ", ".join(f"{r['patch']}={r['status']}" for r in failures)
        )

    patch_run_verified = not critical_failures and not failures and not anchor_miss

    removed_runtime_backups = []
    if patch_run_verified and snapshot is not None:
        try:
            removed_runtime_backups = _remove_new_runtime_backups(
                hermes_dir, hermes_home, snapshot, preexisting_runtime_backups
            )
            if removed_runtime_backups:
                print(
                    f"[backup_cleanup] removed {len(removed_runtime_backups)} "
                    "new in-tree backup artifact(s); pre-apply snapshot retained"
                )
        except Exception as exc:
            failure = f"runtime backup cleanup failed: {exc}"
            print(f"[backup_cleanup] ERROR: {failure}")
            critical_failures.append(failure)
            patch_run_verified = False

    # Post-run verification receipt: drop a summary JSON so client-side
    # checks can confirm what landed.
    try:
        import datetime
        import socket

        parts_lower = {p.lower() for p in hermes_dir.parts}
        if "runtime-candidates" in parts_lower:
            # Candidate runtimes are independent live trees. Write the receipt
            # beside the candidate tree, not to the user's base ~/.hermes, so
            # truth probes can detect stale or unreceipted candidates honestly.
            receipt_home = hermes_dir.parent
        elif hermes_dir.parent.name == ".hermes":
            receipt_home = hermes_dir.parent
        else:
            # Spark/Linux client convention keeps hermes-agent as a peer of
            # ~/.hermes; preserve the established receipt location there.
            receipt_home = Path.home() / ".hermes"
        state_dir = receipt_home / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        runtime_manifest_path = state_dir / "runtime-payload-manifest.json"
        try:
            existing_runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            existing_runtime_manifest = {}
        self_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        provenance: dict = {"verified": False}
        manifest_script = Path(__file__).resolve().parent.parent / "bin" / "runtime-payload-manifest.py"
        if manifest_script.exists():
            golden_repo = manifest_script.parent.parent
            source_manifest = golden_repo / "runtime-payload-source-manifest.json"
            status_proc = None if source_manifest.exists() else subprocess.run(
                ["git", "-C", str(golden_repo), "status", "--porcelain"],
                text=True, capture_output=True, timeout=30,
            )
            if status_proc is not None and status_proc.returncode == 0 and status_proc.stdout.strip():
                provenance["error"] = "Golden source worktree is dirty; refusing exact provenance"
            else:
                manifest_cmd = [
                    sys.executable,
                    str(manifest_script),
                    "--repo",
                    str(golden_repo),
                    "--ref",
                    "HEAD",
                    "--compact",
                ]
                if source_manifest.exists():
                    manifest_cmd.extend(["--source-manifest", str(source_manifest)])
                if pre_apply_clean_base and patch_run_verified:
                    manifest_cmd.extend(["--runtime-dir", str(hermes_dir)])
                manifest_proc = subprocess.run(
                    manifest_cmd,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                if manifest_proc.returncode == 0:
                    manifest_payload = json.loads(manifest_proc.stdout)
                    provenance = {
                        "verified": True,
                        "golden_sha": manifest_payload.get("golden_sha"),
                        "deployment_digest": manifest_payload.get("deployment_digest"),
                        "components": {
                            name: {
                                "digest": component.get("digest"),
                                "file_count": component.get("file_count"),
                            }
                            for name, component in (manifest_payload.get("components") or {}).items()
                        },
                    }
                    runtime_fingerprint = manifest_payload.get("runtime_fingerprint")
                    if isinstance(runtime_fingerprint, dict) and runtime_fingerprint.get("verified") is True:
                        runtime_manifest_tmp = runtime_manifest_path.with_suffix(".json.tmp")
                        runtime_manifest_tmp.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")
                        os.replace(runtime_manifest_tmp, runtime_manifest_path)
                        provenance["runtime_fingerprint"] = {
                            "verified": True,
                            "upstream_sha": runtime_fingerprint.get("upstream_sha"),
                            "expected_upstream_sha": runtime_fingerprint.get("expected_upstream_sha"),
                            "digest": runtime_fingerprint.get("digest"),
                            "file_count": runtime_fingerprint.get("file_count"),
                            "manifest_path": str(runtime_manifest_path),
                        }
                    else:
                        runtime_head_proc = subprocess.run(
                            ["git", "-C", str(hermes_dir), "rev-parse", "HEAD"],
                            text=True,
                            capture_output=True,
                            timeout=30,
                        )
                        runtime_head = runtime_head_proc.stdout.strip() if runtime_head_proc.returncode == 0 else ""
                        existing_fingerprint = _validated_runtime_fingerprint(
                            existing_runtime_manifest, manifest_payload, runtime_head, hermes_dir
                        )
                        existing_is_current = patch_run_verified and existing_fingerprint is not None
                        if existing_is_current:
                            provenance["runtime_fingerprint"] = {
                                "verified": True,
                                "upstream_sha": existing_fingerprint.get("upstream_sha"),
                                "expected_upstream_sha": existing_fingerprint.get("expected_upstream_sha"),
                                "digest": existing_fingerprint.get("digest"),
                                "file_count": existing_fingerprint.get("file_count"),
                                "manifest_path": str(runtime_manifest_path),
                                "reused": True,
                            }
                        else:
                            provenance["runtime_fingerprint"] = {
                                "verified": False,
                                "reason": (
                                    runtime_fingerprint.get("reason")
                                    if isinstance(runtime_fingerprint, dict)
                                    else (
                                        "patch run did not pass every apply/compile/marker gate"
                                        if not patch_run_verified
                                        else "pre-apply runtime tree was not a clean upstream base"
                                    )
                                ),
                                "upstream_sha": (
                                    runtime_fingerprint.get("upstream_sha")
                                    if isinstance(runtime_fingerprint, dict)
                                    else None
                                ),
                                "expected_upstream_sha": (
                                    runtime_fingerprint.get("expected_upstream_sha")
                                    if isinstance(runtime_fingerprint, dict)
                                    else None
                                ),
                            }
                else:
                    provenance["error"] = (manifest_proc.stderr or manifest_proc.stdout)[-1000:]
        else:
            provenance["error"] = f"missing {manifest_script}"
        if not (provenance.get("runtime_fingerprint") or {}).get("verified"):
            runtime_manifest_path.unlink(missing_ok=True)
        receipt = {
            "schema_version": 2,
            "patches_script_sha": self_sha,
            "patches_script_path": str(Path(__file__).resolve()),
            "hermes_dir": str(hermes_dir),
            "applied_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "hostname": socket.gethostname(),
            "applied_count": applied,
            "results": results,
            "dispatch_mode": dispatch_mode,
            "pre_apply_clean_base": pre_apply_clean_base,
            "patch_run_verified": patch_run_verified,
            "anchor_miss": anchor_miss,
            "anchor_miss_suspects": anchor_miss_suspects,
            "verifier_version": PATCH_VERIFIER_VERSION,
            "removed_runtime_backups": removed_runtime_backups,
            "golden_provenance": provenance,
        }
        (state_dir / "last-applied-patches.json").write_text(json.dumps(receipt, indent=2))
        if snapshot is not None:
            (snapshot / "apply-result.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[receipt] warn: could not write last-applied-patches.json: {e}")

    try:
        pruned = _prune_old_snapshots(hermes_home, keep=10)
        if pruned:
            print(f"[snapshot] pruned {pruned} old snapshot(s)")
    except Exception as exc:
        print(f"[snapshot] WARN: prune failed: {exc}")

    if critical_failures or failures:
        return 2
    if anchor_miss and not args.allow_anchor_miss:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
