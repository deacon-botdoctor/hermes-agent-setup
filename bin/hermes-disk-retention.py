#!/usr/bin/env python3
"""Fail-closed runtime and snapshot retention for one Hermes profile.

Only explicit runtime-candidate, rollback-backup, snapshot, and regenerable
cache roots are eligible.  Memories, sessions, workspaces, credentials, and
client artifacts are never candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

UTC = timezone.utc
PROTECTED_HERMES_NAMES = {
    "config.yaml",
    "data",
    "kanban.db",
    "lcm.db",
    "memories",
    "profiles",
    "sessions",
    "state.db",
    "workspace",
    "workspaces",
}
CACHE_REL_PATHS = (
    ".npm",
    ".cache/qmd",
    ".cache/whisper",
    "Library/Caches/Google",
    "Library/Caches/BraveSoftware",
    "Library/Caches/camoufox",
    "Library/Caches/pnpm",
)
BACKUP_PATTERNS = ("*-swap-*", "*-migration-*", "*-upgrade-*")


@dataclass(frozen=True)
class Candidate:
    path: Path
    kind: str
    size_bytes: int
    mtime: float
    reason: str


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_size(value: str) -> int:
    text = str(value).strip().lower()
    suffixes = {
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
    }
    for suffix, multiplier in suffixes.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)].strip()) * multiplier)
    return int(float(text))


def path_size(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum(
            child.stat().st_size
            for child in path.rglob("*")
            if child.is_file() and not child.is_symlink()
        )
    except OSError:
        return 0


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def read_json(path: Path, description: str) -> tuple[object, bytes]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"unsafe {description}: {path}")
    try:
        raw = path.read_bytes()
        return json.loads(raw), raw
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {description}: {path}") from exc


def runtime_root(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = Path(raw)
    return Path(os.path.normpath(raw)) if root.is_absolute() else None


def receipt_runtime_root(payload: object) -> Path | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("runtime_root")
    if payload.get("kind") != "botdoctor_runtime_binding":
        contract = payload.get("runtime_contract")
        raw = contract.get("runtime_root") if isinstance(contract, dict) else None
    return runtime_root(raw)


def candidate_roots(hermes_home: Path) -> tuple[Path, ...]:
    homes = [hermes_home]
    if hermes_home.parent.name.lower() == "profiles":
        homes.append(hermes_home.parent.parent)
    roots: list[Path] = []
    for home in homes:
        roots.extend((home / "state/runtime-candidates", home / "runtime-candidates"))
    return tuple(dict.fromkeys(roots))


def rollout_roots(hermes_home: Path) -> tuple[Path, ...]:
    homes = [hermes_home]
    if hermes_home.parent.name.lower() == "profiles":
        homes.append(hermes_home.parent.parent)
    return tuple(dict.fromkeys(home / "state/fleet-rollouts" for home in homes))


def validate_generation(root: Path, allowed: tuple[Path, ...], role: str) -> Path:
    generation = root
    if root.is_absolute() and root.parent in allowed:
        allowed_root = root.parent
    elif (
        root.is_absolute()
        and root.name == "hermes-agent"
        and root.parent.parent in allowed
    ):
        # Older profile bindings identify the code root inside the generation,
        # while retention operates on the enclosing direct child.  Normalize
        # this one exact historical layout; never accept arbitrary descendants.
        generation = root.parent
        allowed_root = root.parent.parent
    else:
        raise ValueError(f"unsafe {role} runtime root: {root}")
    if (
        root.is_symlink()
        or generation.is_symlink()
        or allowed_root.is_symlink()
        or not root.is_dir()
        or not generation.is_dir()
    ):
        raise ValueError(f"unsafe {role} runtime root: {root}")
    resolved = generation.resolve(strict=True)
    if resolved.parent != allowed_root.resolve(strict=True):
        raise ValueError(f"unsafe {role} runtime root: {root}")
    return resolved


def active_runtime_root(hermes_home: Path) -> tuple[Path, list[Path]]:
    state = hermes_home / "state"
    binding = state / "runtime-binding.json"
    if not os.path.lexists(binding):
        raise ValueError("active runtime evidence missing")
    payload, _ = read_json(binding, "runtime binding receipt")
    if not isinstance(payload, dict) or payload.get("kind") != "botdoctor_runtime_binding":
        raise ValueError(f"invalid runtime binding receipt: {binding}")
    root = receipt_runtime_root(payload)
    if root is None:
        raise ValueError(f"invalid runtime binding receipt: {binding}")
    receipt_roots: set[Path] = set()
    receipt_paths: list[Path] = []
    for path in sorted((state / "host-receipts").glob("latest-*.json")):
        # Operators preserve superseded receipts beside the canonical latest
        # receipt as ``latest-<host>.before-<change>.json``.  Those historical
        # snapshots are rollback evidence, not current runtime evidence, and
        # may predate the binding-receipt schema.
        if ".before-" in path.name:
            continue
        receipt, _ = read_json(path, "runtime binding receipt")
        receipt_root = receipt_runtime_root(receipt)
        # The central operator host retains canonical receipts for every OS.
        # Foreign Windows paths are not absolute on POSIX (and vice versa), so
        # they cannot validate or threaten this host's local runtime binding.
        if receipt_root is None:
            continue
        receipt_roots.add(receipt_root)
        receipt_paths.append(path)
    stale_receipts: list[Path] = []
    if receipt_roots and root not in receipt_roots:
        try:
            binding_mtime = binding.stat().st_mtime
            conflicting = [path for path in receipt_paths if path.stat().st_mtime >= binding_mtime]
        except OSError as exc:
            raise ValueError("active runtime receipt freshness unreadable") from exc
        if conflicting:
            raise ValueError("active runtime binding conflicts with a newer host receipt")
        stale_receipts = receipt_paths
    return root, stale_receipts


def rollback_runtime_root(hermes_home: Path) -> tuple[Path, list[Path]]:
    state = hermes_home / "state"
    pointer = state / "current-rollback.json"
    payload, _ = read_json(pointer, "current rollback pointer")
    if not isinstance(payload, dict):
        raise ValueError(f"invalid current rollback pointer: {pointer}")
    source_raw = payload.get("source_path")
    source_digest = payload.get("source_sha256")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "botdoctor_current_rollback"
        or not isinstance(source_raw, str)
        or not isinstance(source_digest, str)
        or len(source_digest) != 64
    ):
        raise ValueError(f"invalid current rollback pointer: {pointer}")
    source = Path(source_raw)
    relative: Path | None = None
    for rollout_root in rollout_roots(hermes_home):
        try:
            relative = source.relative_to(rollout_root)
            break
        except ValueError:
            continue
    if relative is None:
        raise ValueError(f"unsafe current rollback source: {source}")
    if len(relative.parts) != 2 or relative.parts[1] not in {
        "receipt.before",
        "runtime-binding.before",
    }:
        raise ValueError(f"unsafe current rollback source: {source}")
    source_payload, source_bytes = read_json(source, "current rollback source")
    if hashlib.sha256(source_bytes).hexdigest() != source_digest.lower():
        raise ValueError(f"invalid current rollback source digest: {source}")
    source_root = receipt_runtime_root(source_payload)
    pointer_root = runtime_root(payload.get("runtime_root"))
    if source_root is None or pointer_root != source_root:
        raise ValueError(f"invalid current rollback pointer: {pointer}")
    superseded_pending: list[Path] = []
    try:
        pointer_mtime = pointer.stat().st_mtime
    except OSError as exc:
        raise ValueError(f"current rollback pointer freshness unreadable: {pointer}") from exc
    pending_transactions = {
        pending
        for rollout_root in rollout_roots(hermes_home)
        for pending in rollout_root.glob("*/rollback-transaction.pending")
    }
    for pending in sorted(pending_transactions):
        try:
            superseded = pending.parent != source.parent and pending.stat().st_mtime < pointer_mtime
        except OSError as exc:
            raise ValueError(f"rollback transaction evidence unreadable: {pending}") from exc
        if not superseded:
            raise ValueError(f"rollback transaction pending: {pending}")
        superseded_pending.append(pending)
    return source_root, superseded_pending


def extract_candidate_roots(text: str) -> set[Path]:
    patterns = (
        (
            r"[A-Za-z]:[\\/]"
            r"(?:[^\\/\s\"']+[\\/])*"
            r"runtime-candidates[\\/][A-Za-z0-9._-]+",
        )
        if os.name == "nt"
        else (r"/(?:[^/:<>\s\"']+/)*runtime-candidates/[A-Za-z0-9._-]+",)
    )
    found: set[Path] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            found.add(Path(os.path.normpath(match.group(0).rstrip("\\/"))))
    return found


def nominal_runtime_references(hermes_home: Path) -> set[Path]:
    homes = [hermes_home]
    if hermes_home.parent.name.lower() == "profiles":
        homes.append(hermes_home.parent.parent)
    referenced: set[Path] = set()
    for home in homes:
        for name in ("hermes-agent", "venv", ".venv"):
            path = home / name
            if not os.path.lexists(path) or not path.is_symlink():
                continue
            try:
                target = path.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"nominal runtime link broken: {path}") from exc
            roots = extract_candidate_roots(str(target))
            if not roots:
                continue
            referenced.update(roots)
    return referenced


def reference_texts(hermes_home: Path, active: Path) -> list[str]:
    paths = [
        active / "venv/pyvenv.cfg",
        active / ".venv/pyvenv.cfg",
        hermes_home / "config.yaml",
        hermes_home / "config.yml",
        hermes_home / "gateway_state.json",
        hermes_home / "state/gateway.json",
        hermes_home / "state/gateway-state.json",
    ]
    texts: list[str] = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace")[:1_000_000])
        except OSError as exc:
            raise ValueError(f"runtime reference evidence unreadable: {path}") from exc
    return texts


def live_reference_text() -> str:
    if os.name == "nt":
        script = (
            "$v=@();"
            "Get-CimInstance Win32_Process|%{$v+=[string]$_.ExecutablePath;$v+=[string]$_.CommandLine};"
            "Get-ScheduledTask|%{$_.Actions|%{$v+=[string]$_.Execute;$v+=[string]$_.Arguments}};"
            "$v|?{$_}|ConvertTo-Json -Compress"
        )
        probe = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if probe.returncode:
            raise ValueError("Windows runtime reference inventory failed")
        try:
            values = json.loads(probe.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Windows runtime reference inventory invalid") from exc
        if isinstance(values, str):
            values = [values]
        return "\n".join(str(value) for value in values if value)
    probe = subprocess.run(
        ["ps", "-axo", "command="],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode:
        raise ValueError("process runtime reference inventory failed")
    chunks = [probe.stdout]
    cron = subprocess.run(
        ["crontab", "-l"], text=True, capture_output=True, timeout=10, check=False
    )
    if cron.returncode == 0:
        chunks.append(cron.stdout)
    for directory in (
        Path.home() / ".config/systemd/user",
        Path.home() / "Library/LaunchAgents",
    ):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink():
                try:
                    chunks.append(path.read_text(encoding="utf-8", errors="replace")[:500_000])
                except OSError:
                    continue
    return "\n".join(chunks)


def protected_runtime_roots(hermes_home: Path) -> tuple[set[Path], dict[str, list[str]]]:
    allowed = candidate_roots(hermes_home)
    has_generations = False
    for root in allowed:
        if not root.exists():
            continue
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"unsafe runtime candidate root: {root}")
        try:
            has_generations = has_generations or any(
                (path.is_dir() or path.is_file()) and not path.is_symlink()
                for path in root.iterdir()
            )
        except OSError as exc:
            raise ValueError(f"runtime candidate root unreadable: {root}") from exc
    if not has_generations:
        # Operator/control profiles may own backups and snapshots without ever
        # hosting a switchable runtime.  With no generation below an eligible
        # runtime-candidates root there is nothing that active/rollback
        # evidence could protect, so requiring a fabricated binding only makes
        # the safe no-op path fail.  Any real generation still takes the full
        # fail-closed evidence path below.
        return set(), {
            "active": [],
            "rollback": [],
            "referenced": [],
            "stale_referenced": [],
            "stale_host_receipts": [],
            "superseded_pending_transactions": [],
            "foreign_live_referenced": [],
            "not_applicable": ["no_runtime_generations"],
        }
    active_root, stale_receipts = active_runtime_root(hermes_home)
    rollback_root, superseded_pending = rollback_runtime_root(hermes_home)
    active = validate_generation(active_root, allowed, "active")
    rollback = validate_generation(rollback_root, allowed, "rollback")
    if active == rollback:
        raise ValueError("active and rollback runtime evidence are not distinct")
    configured_references = nominal_runtime_references(hermes_home)
    for text in reference_texts(hermes_home, active):
        configured_references.update(extract_candidate_roots(text))
    live_references = extract_candidate_roots(live_reference_text())
    referenced = configured_references | live_references
    validated_references: set[Path] = set()
    stale_references: set[Path] = set()
    foreign_live_references: set[Path] = set()
    for root in referenced:
        if not root.is_absolute():
            raise ValueError(f"unsafe referenced runtime root: {root}")
        if root.parent not in allowed:
            if root in configured_references:
                raise ValueError(f"unsafe referenced runtime root: {root}")
            foreign_live_references.add(root)
            continue
        if not os.path.lexists(root):
            if root in configured_references:
                raise ValueError(f"unsafe referenced runtime root: {root}")
            stale_references.add(root)
            continue
        validated_references.add(validate_generation(root, allowed, "referenced"))
    protected = {active, rollback, *validated_references}
    return protected, {
        "active": [str(active)],
        "rollback": [str(rollback)],
        "referenced": sorted(str(path) for path in validated_references - {active, rollback}),
        "stale_referenced": sorted(str(path) for path in stale_references),
        "stale_host_receipts": sorted(str(path) for path in stale_receipts),
        "superseded_pending_transactions": sorted(str(path) for path in superseded_pending),
        "foreign_live_referenced": sorted(str(path) for path in foreign_live_references),
    }


@contextmanager
def mutation_lease(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\n")
            handle.flush()
        handle.seek(0)
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise RuntimeError("no supported mutation lease backend")
        except (BlockingIOError, OSError) as exc:
            raise ValueError(f"fleet mutation lease busy: {path}") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


def record(candidate: Candidate, action: str) -> dict[str, object]:
    return {
        "path": str(candidate.path),
        "kind": candidate.kind,
        "size_bytes": candidate.size_bytes,
        "mtime": datetime.fromtimestamp(candidate.mtime, UTC).isoformat().replace("+00:00", "Z")
        if candidate.mtime
        else None,
        "reason": candidate.reason,
        "action": action,
    }


def generations(root: Path, kind: str, reason: str) -> list[Candidate]:
    if not root.is_dir() or root.is_symlink():
        return []
    return sorted(
        (
            Candidate(path, kind, path_size(path), mtime(path), reason)
            for path in root.iterdir()
            if (path.is_dir() or path.is_file()) and not path.is_symlink()
        ),
        key=lambda item: item.mtime,
        reverse=True,
    )


def collect_plan(
    home: Path,
    hermes_home: Path,
    protected: set[Path],
    keep_backups: int,
    keep_snapshots: int,
    prune_caches: bool,
    min_cache_bytes: int,
) -> tuple[list[Candidate], list[dict[str, object]]]:
    candidates: list[Candidate] = []
    inventory: list[dict[str, object]] = []
    for root in candidate_roots(hermes_home):
        for candidate in generations(root, "runtime_candidate", f"direct child of {root}"):
            target = candidate.path.resolve()
            keep = any(
                target == item or is_relative_to(target, item) or is_relative_to(item, target)
                for item in protected
            )
            inventory.append(record(candidate, "keep_protected" if keep else "prune"))
            if not keep:
                candidates.append(candidate)
    backups: list[Candidate] = []
    for pattern in ("upgrade-backup-*", "embed-standardize-backup-*", "targeted-overlay-backup-*"):
        backups.extend(
            Candidate(path, "rollback_backup", path_size(path), mtime(path), f"matches {pattern}")
            for path in home.glob(pattern)
            if path.is_dir() and not path.is_symlink()
        )
    backup_root = hermes_home / "backups"
    for pattern in BACKUP_PATTERNS:
        backups.extend(
            Candidate(path, "rollback_backup", path_size(path), mtime(path), f"matches {pattern}")
            for path in backup_root.glob(pattern)
            if path.is_dir() and not path.is_symlink()
        )
    for index, candidate in enumerate(sorted(backups, key=lambda item: item.mtime, reverse=True)):
        action = "keep" if index < keep_backups else "prune"
        inventory.append(record(candidate, action))
        if action == "prune":
            candidates.append(candidate)
    for rel in ("apply-snapshots", "patch-snapshots", "fleet-stage"):
        snapshot_root = hermes_home / "state" / rel
        items = generations(snapshot_root, "snapshot", f"generation under {snapshot_root}")
        for index, candidate in enumerate(items):
            action = "keep" if index < keep_snapshots else "prune"
            inventory.append(record(candidate, action))
            if action == "prune":
                candidates.append(candidate)
    if prune_caches:
        for rel in CACHE_REL_PATHS:
            path = home / rel
            candidate = Candidate(path, "cache", path_size(path), mtime(path), "regenerable cache allowlist")
            if path.exists() and not path.is_symlink() and candidate.size_bytes >= min_cache_bytes:
                candidates.append(candidate)
                inventory.append(record(candidate, "prune"))
    home_resolved = home.resolve()
    hermes_resolved = hermes_home.resolve()
    for candidate in candidates:
        target = candidate.path.resolve()
        if not is_relative_to(target, home_resolved):
            raise ValueError(f"refusing path outside home: {candidate.path}")
        if is_relative_to(target, hermes_resolved):
            relative = target.relative_to(hermes_resolved)
            if relative.parts and relative.parts[0] in PROTECTED_HERMES_NAMES:
                raise ValueError(f"refusing protected Hermes path: {candidate.path}")
    return candidates, inventory


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clear_platform_flags(path: Path) -> None:
    if os.name == "nt":
        return
    command_name = "chflags" if sys.platform == "darwin" else "chattr"
    command_path = shutil.which(command_name)
    if command_path is None:
        return
    command = (
        [command_path, "nouchg", str(path)]
        if sys.platform == "darwin"
        else [command_path, "-i", "--", str(path)]
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode and shutil.which("sudo"):
        subprocess.run(
            [shutil.which("sudo") or "sudo", "-n", *command],
            capture_output=True,
            text=True,
            check=False,
        )


def chmod_without_following_symlinks(path: Path, mode: int) -> None:
    """Make a validated path writable across Python platform capabilities."""
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except NotImplementedError:
        # Windows Python does not implement follow_symlinks=False for chmod.
        # The caller already confines the path to the exact removal root; keep
        # the no-follow invariant explicit before using the supported call.
        if path.is_symlink():
            raise
        os.chmod(path, mode)


def windows_extended_path(value: str) -> str:
    """Return a Win32 extended-length path without changing its target."""
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def filesystem_removal_path(path: Path) -> Path:
    resolved = str(path.resolve(strict=True))
    return Path(windows_extended_path(resolved) if os.name == "nt" else resolved)


def removal_error_handler(root: Path, clear_flags: bool):
    root_resolved = root.resolve(strict=True)

    def handle(function, raw_path: str, exc_info) -> None:
        path = Path(raw_path)
        if path.is_symlink():
            raise exc_info[1]
        resolved = path.resolve(strict=False)
        if resolved != root_resolved and not is_relative_to(resolved, root_resolved):
            raise ValueError(f"refusing removal recovery outside candidate: {path}")
        if clear_flags:
            clear_platform_flags(path)
        for item in (path.parent, path):
            resolved_item = item.resolve(strict=False)
            if resolved_item != root_resolved and not is_relative_to(
                resolved_item, root_resolved
            ):
                continue
            try:
                mode = item.stat(follow_symlinks=False).st_mode
                additions = stat.S_IWUSR
                if item.is_dir():
                    additions |= stat.S_IRUSR | stat.S_IXUSR
                chmod_without_following_symlinks(item, mode | additions)
            except (OSError, NotImplementedError):
                continue
        function(raw_path)

    return handle


def run_retention(args: argparse.Namespace, home: Path, hermes_home: Path) -> int:
    before = shutil.disk_usage(home).free
    protected, protection = protected_runtime_roots(hermes_home)
    candidates, inventory = collect_plan(
        home,
        hermes_home,
        protected,
        args.keep_backups,
        args.keep_snapshots,
        args.prune_caches,
        args.min_cache_bytes,
    )
    deleted: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    if args.apply:
        for candidate in candidates:
            try:
                if candidate.path.is_dir() and not candidate.path.is_symlink():
                    removal_path = filesystem_removal_path(candidate.path)
                    shutil.rmtree(
                        removal_path,
                        onerror=removal_error_handler(removal_path, args.clear_flags),
                    )
                else:
                    candidate.path.unlink()
                deleted.append(record(candidate, "deleted"))
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": str(candidate.path), "error": f"{type(exc).__name__}: {exc}"})
    after = shutil.disk_usage(home).free
    status = "error" if errors else "pass"
    if status == "pass" and after < args.block_free_bytes:
        status = "block"
    elif status == "pass" and after < args.warn_free_bytes:
        status = "warn"
    checked_at = now_iso()
    manifest = {
        "schema_version": 2,
        "kind": "hermes_disk_retention",
        "checked_at": checked_at,
        "mode": "apply" if args.apply else "dry-run",
        "status": status,
        "home": str(home),
        "hermes_home": str(hermes_home),
        "policy": {
            "keep_backups": args.keep_backups,
            "keep_snapshots": args.keep_snapshots,
            "runtime_candidate_policy": "active_plus_one_rollback_plus_referenced_dependencies",
            "memory_policy": "never_eligible",
            "clear_flags": args.clear_flags,
        },
        "free_before_bytes": before,
        "free_after_bytes": after,
        "planned_reclaim_bytes": sum(item.size_bytes for item in candidates),
        "deleted_reclaim_bytes": sum(int(item["size_bytes"]) for item in deleted),
        "runtime_protection": protection,
        "inventory": inventory,
        "planned": [record(item, "delete" if args.apply else "would_delete") for item in candidates],
        "deleted": deleted,
        "errors": errors,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    atomic_json(hermes_home / f"state/disk-retention/retention-{stamp}.json", manifest)
    atomic_json(hermes_home / "state/disk-retention-last.json", manifest)
    summary = {
        "ok": status not in {"error", "block"},
        "status": status,
        "mode": manifest["mode"],
        "planned_count": len(candidates),
        "deleted_count": len(deleted),
        "planned_reclaim_bytes": manifest["planned_reclaim_bytes"],
        "free_after_bytes": after,
    }
    print(json.dumps(summary, sort_keys=True) if args.json else "hermes-disk-retention: " + str(summary))
    return 2 if status == "block" else (1 if status == "error" else 0)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--keep-backups", type=int, default=int(os.environ.get("HERMES_DISK_RETENTION_KEEP_BACKUPS", "1")))
    parser.add_argument("--keep-snapshots", type=int, default=int(os.environ.get("HERMES_DISK_RETENTION_KEEP_SNAPSHOTS", "1")))
    parser.add_argument("--min-cache-bytes", type=parse_size, default=parse_size(os.environ.get("HERMES_DISK_RETENTION_MIN_CACHE", "256M")))
    parser.add_argument("--warn-free-bytes", type=parse_size, default=parse_size(os.environ.get("HERMES_DISK_RETENTION_WARN_FREE", "30G")))
    parser.add_argument("--block-free-bytes", type=parse_size, default=parse_size(os.environ.get("HERMES_DISK_RETENTION_BLOCK_FREE", "15G")))
    parser.add_argument("--prune-caches", dest="prune_caches", action="store_true")
    parser.add_argument("--no-prune-caches", dest="prune_caches", action="store_false")
    parser.add_argument("--clear-flags", dest="clear_flags", action="store_true")
    parser.add_argument("--no-clear-flags", dest="clear_flags", action="store_false")
    parser.set_defaults(
        prune_caches=os.environ.get("HERMES_DISK_RETENTION_PRUNE_CACHES") == "1",
        clear_flags=os.environ.get("HERMES_DISK_RETENTION_CLEAR_FLAGS") == "1",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.keep_backups != 1 or args.keep_snapshots != 1:
        raise SystemExit("fleet policy requires exactly one rollback backup and one snapshot")
    home = Path(args.home).expanduser().resolve()
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    if not home.is_dir() or not hermes_home.is_dir():
        raise SystemExit("home or Hermes home not found")
    state = hermes_home / "state/promotion/executor"
    with mutation_lease(state / "active-rollout.lock"):
        with mutation_lease(state / "runtime-mutation.lock"):
            return run_retention(args, home, hermes_home)


if __name__ == "__main__":
    raise SystemExit(main())
