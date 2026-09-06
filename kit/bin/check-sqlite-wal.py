#!/usr/bin/env python3
"""Audit Hermes SQLite databases against the safe journal mode for this build.

SQLite's WAL-reset corruption bug affects ordinary releases from 3.7.0 through
3.51.2. Fixed backports exist for 3.44.6 and 3.50.7. Managed stores use
rollback journaling because gateway and system interpreters can bundle
different SQLite patch levels.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shlex
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Iterable, Optional


SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
HERMES_GATEWAY_DB_PATHS = {
    "cron/executions.db",
    "cron/notepad.db",
    "data/durable-threads.db",
    "data/media-provenance.db",
    "data/task-ledger.db",
    "data/telegram-transcript.db",
    "gateway/discord_message_recovery.db",
    "kanban.db",
    "lcm.db",
    "memory_store.db",
    "projects.db",
    "retaindb_queue.db",
    "response_store.db",
    "state.db",
    "state/anamnesis.db",
    "state/capability-router-usage.db",
    "state/composio_onboarding.db",
    "state/session-search.sqlite",
    "state/telegram-transcript.db",
    "state/telegram-transactions.sqlite3",
    "telemetry/shared_metrics/metrics.sqlite3",
    "telegram-transcript.db",
    "verification_evidence.db",
}
ANAMNESIS_GATEWAY_DB_PATHS = {"memory.db"}
INACTIVE_DB_RELATIVE_PATHS = {"data/fleet-completion-integrity-v1-test.db"}
INACTIVE_TREE_PARTS = {
    ".cache",
    ".git",
    ".tmp",
    ".venv",
    "archive",
    "archives",
    "backup",
    "backups",
    "browser-chromium-profile",
    "browser-lane",
    "cache",
    "firecrawl",
    "node_modules",
    "profiles",
    "quarantine",
    "rollback",
    "rollbacks",
    "runtime-candidates",
    "runtime_candidates",
    "sqlite-journal-rollbacks",
    "state-snapshots",
    "snapshot",
    "snapshots",
    "tmp",
    "venv",
    "__pycache__",
}
def version_tuple(version: Iterable[int]) -> tuple[int, int, int]:
    values = [int(item) for item in version]
    return tuple((values + [0, 0, 0])[:3])


def wal_reset_bug_fixed(version: Iterable[int]) -> bool:
    current = version_tuple(version)
    return (
        current >= (3, 51, 3)
        or (3, 50, 7) <= current < (3, 51, 0)
        or (3, 44, 6) <= current < (3, 45, 0)
    )


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def safe_journal_mode(version: Iterable[int]) -> str:
    # Managed Linux stores are accessed by multiple Python interpreters whose
    # bundled SQLite patch levels may differ. Rollback journaling is the only
    # mode safe for every accessor without upgrading them as one atomic unit.
    if _is_linux_platform():
        return "delete"
    return "wal" if wal_reset_bug_fixed(version) else "delete"


def _inactive_tree_part(parts: Iterable[str]) -> Optional[str]:
    for part in parts:
        lowered = part.lower()
        if lowered in INACTIVE_TREE_PARTS:
            return part
    return None


def _gateway_owned_relative_path(relative: Path, *, anamnesis_root: bool) -> bool:
    key = relative.as_posix()
    if anamnesis_root:
        return key in ANAMNESIS_GATEWAY_DB_PATHS
    if key in HERMES_GATEWAY_DB_PATHS:
        return True
    parts = relative.parts
    if (
        len(parts) == 4
        and parts[0] == "kanban"
        and parts[1] == "boards"
        and parts[3] == "kanban.db"
    ):
        return True
    if (
        len(parts) == 3
        and parts[0] == "plugin-data"
        and parts[1] not in {"", ".", ".."}
        and relative.suffix.lower() in SQLITE_SUFFIXES
    ):
        return True
    return False


def _may_contain_gateway_db(relative_dir: Path, *, anamnesis_root: bool) -> bool:
    prefix = relative_dir.as_posix().rstrip("/") + "/"
    allowlist = (
        ANAMNESIS_GATEWAY_DB_PATHS
        if anamnesis_root
        else HERMES_GATEWAY_DB_PATHS
    )
    if any(path.startswith(prefix) for path in allowlist):
        return True
    parts = relative_dir.parts
    return bool(
        not anamnesis_root
        and (
            parts in {("kanban",), ("kanban", "boards")}
            or (len(parts) == 3 and parts[:2] == ("kanban", "boards"))
            or parts == ("plugin-data",)
            or (len(parts) == 2 and parts[0] == "plugin-data")
        )
    )


def active_db_candidates(base_dirs, explicit_databases=None):
    """Yield only regular SQLite files with a proven gateway owner."""
    seen = set()
    candidates = []
    excluded = []
    for raw_base in base_dirs:
        logical_base = Path(raw_base).expanduser()
        anamnesis_root = logical_base.name == ".anamnesis"
        base = logical_base.resolve()
        if not base.is_dir():
            continue
        def record_walk_error(error: OSError) -> None:
            unresolved = Path(error.filename) if error.filename else base
            excluded.append(
                {"db": str(unresolved), "reason": "gateway_owned_unresolved"}
            )

        for root, dirnames, filenames in os.walk(
            base,
            topdown=True,
            onerror=record_walk_error,
            followlinks=False,
        ):
            root_path = Path(root)
            root_relative = root_path.relative_to(base)
            kept_dirs = []
            for dirname in sorted(dirnames):
                relative_dir = root_relative / dirname
                candidate_dir = root_path / dirname
                if candidate_dir.is_symlink():
                    if _may_contain_gateway_db(relative_dir, anamnesis_root=anamnesis_root):
                        excluded.append(
                            {"db": str(candidate_dir), "reason": "gateway_owned_unresolved"}
                        )
                    continue
                inactive_part = _inactive_tree_part(relative_dir.parts)
                if inactive_part is not None and not _may_contain_gateway_db(
                    relative_dir,
                    anamnesis_root=anamnesis_root,
                ):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in sorted(filenames):
                db_path = root_path / filename
                if db_path.suffix.lower() not in SQLITE_SUFFIXES:
                    continue
                relative = db_path.relative_to(base)
                gateway_owned = _gateway_owned_relative_path(
                    relative,
                    anamnesis_root=anamnesis_root,
                )
                if (
                    relative.as_posix() in INACTIVE_DB_RELATIVE_PATHS
                    and not gateway_owned
                ):
                    excluded.append({"db": str(db_path), "reason": "inactive_artifact"})
                    continue
                if not gateway_owned:
                    excluded.append({"db": str(db_path), "reason": "owner_not_gateway_proven"})
                    continue
                current = base
                linked = False
                for part in relative.parts:
                    current /= part
                    if current.is_symlink():
                        linked = True
                        break
                if linked or not db_path.is_file():
                    excluded.append({"db": str(db_path), "reason": "gateway_owned_unresolved"})
                    continue
                key = str(db_path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((base, db_path))
    for raw_database in explicit_databases or []:
        logical_path = Path(os.path.expandvars(str(raw_database))).expanduser()
        db_path = logical_path.resolve()
        if not logical_path.exists():
            excluded.append({"db": str(logical_path), "reason": "gateway_owned_unresolved"})
            continue
        if logical_path.is_symlink() or not db_path.is_file():
            excluded.append({"db": str(logical_path), "reason": "gateway_owned_unresolved"})
            continue
        if db_path.suffix.lower() not in SQLITE_SUFFIXES:
            excluded.append({"db": str(logical_path), "reason": "owner_not_gateway_proven"})
            continue
        key = str(db_path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((db_path.parent, db_path))
    return candidates, excluded


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pid_is_live(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5  # access denied still proves existence
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _read_gateway_record(path: Path) -> Optional[dict]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _canonical_path(path: object) -> str:
    return os.path.normcase(str(Path(str(path)).expanduser().resolve(strict=False)))


def _gateway_command_subcommand(command: str) -> Optional[str]:
    try:
        raw_tokens = shlex.split(command, posix=False)
    except ValueError:
        raw_tokens = command.split()
    tokens = [token.strip("\"'").replace("\\", "/").lower() for token in raw_tokens]
    for token in tokens:
        basename = token.rsplit("/", 1)[-1]
        if token.endswith("/gateway/run.py") or basename in {
            "hermes-gateway",
            "hermes-gateway.exe",
        }:
            return "run"
    joined = " ".join(tokens)
    if not (
        "hermes_cli.main" in joined
        or "hermes_cli/main.py" in joined
        or any(token.rsplit("/", 1)[-1] in {"hermes", "hermes.exe"} for token in tokens)
    ):
        return None
    filtered = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in {"--profile", "-p"}:
            skip_next = True
            continue
        if token.startswith("--profile=") or token.startswith("-p="):
            continue
        filtered.append(token)
    for index, token in enumerate(filtered):
        if token == "gateway":
            return filtered[index + 1] if index + 1 < len(filtered) else "run"
    return None


def _process_start_time(pid: int) -> Optional[int]:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def _read_process_cmdline(pid: int) -> Optional[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not raw:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip() or None


def _read_process_hermes_home(pid: int) -> Optional[str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    for entry in raw.split(b"\x00"):
        if entry.startswith(b"HERMES_HOME="):
            value = entry.split(b"=", 1)[1].decode("utf-8", errors="ignore").strip()
            return value or None
    return None


def _gateway_record_live_state(record: dict, hermes_home: Path) -> Optional[bool]:
    try:
        pid = int(record.get("pid") or 0)
        recorded_start = int(record.get("start_time"))
    except (TypeError, ValueError):
        return None
    argv = record.get("argv")
    recorded_home = record.get("hermes_home")
    if (
        pid <= 0
        or recorded_start <= 0
        or record.get("kind") != "hermes-gateway"
        or not isinstance(argv, list)
        or not argv
        or not isinstance(recorded_home, str)
        or _canonical_path(recorded_home) != _canonical_path(hermes_home)
        or _gateway_command_subcommand(" ".join(str(part) for part in argv))
        not in {"run", "restart"}
    ):
        return None
    if not _pid_is_live(pid):
        return False
    live_start = _process_start_time(pid)
    live_command = _read_process_cmdline(pid)
    live_home = _read_process_hermes_home(pid)
    if live_start is None or live_command is None or live_home is None:
        return None
    if live_start != recorded_start:
        return False
    if _gateway_command_subcommand(live_command) not in {"run", "restart"}:
        return False
    if _canonical_path(live_home) != _canonical_path(hermes_home):
        return False
    return True


def _gateway_pid_is_live(hermes_home: Path) -> Optional[bool]:
    """Bind liveness to Hermes' canonical PID record and process identity."""
    hermes_home = hermes_home.expanduser().resolve(strict=False)
    pid_path = hermes_home / "gateway.pid"
    if pid_path.exists():
        record = _read_gateway_record(pid_path)
        return None if record is None else _gateway_record_live_state(record, hermes_home)

    found_stopped = False
    found_uncertain = False
    for state_path in (
        hermes_home / "gateway_state.json",
        hermes_home / "state" / "gateway_state.json",
    ):
        if not state_path.is_file():
            continue
        record = _read_gateway_record(state_path)
        if record is None:
            found_uncertain = True
            continue
        state = _gateway_record_live_state(record, hermes_home)
        if state is True:
            return True
        if state is False:
            found_stopped = True
        else:
            found_uncertain = True
    if found_uncertain:
        return None
    return False if found_stopped else None


def _linux_open_owner_pids(db_path: Path) -> list[int]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise RuntimeError("database_owner_probe_unavailable")
    target = db_path.stat()
    current_uid = os.geteuid()
    if target.st_uid != current_uid:
        raise RuntimeError("database_owner_probe_incomplete:database_not_owned_by_runtime_user")
    owners = []
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == os.getpid():
            continue
        fd_dir = process_dir / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except PermissionError as exc:
            raise RuntimeError(
                f"database_owner_probe_incomplete:{process_dir.name}:{exc}"
            ) from exc
        except FileNotFoundError:
            continue
        for descriptor in descriptors:
            try:
                opened = descriptor.stat()
            except PermissionError as exc:
                raise RuntimeError(
                    f"database_owner_probe_incomplete:{process_dir.name}:{exc}"
                ) from exc
            except FileNotFoundError:
                continue
            if opened.st_dev == target.st_dev and opened.st_ino == target.st_ino:
                owners.append(int(process_dir.name))
                break
    return sorted(set(owners))


def _validate_owner_probe_receipt(receipt: dict) -> None:
    if not receipt.get("privileged"):
        raise RuntimeError("database_owner_probe_incomplete:receipt_not_privileged")
    try:
        observed_at = float(receipt["observed_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("database_owner_probe_incomplete:receipt_timestamp_invalid") from exc
    if time.time() - observed_at > 30 or observed_at - time.time() > 5:
        raise RuntimeError("database_owner_probe_incomplete:receipt_stale")


def _validated_owner_probe_pids(db_path: Path, receipt: dict) -> list[int]:
    target = db_path.stat()
    for row in receipt.get("databases", []):
        if row.get("db") != str(db_path):
            continue
        if int(row.get("st_dev", -1)) != target.st_dev or int(row.get("st_ino", -1)) != target.st_ino:
            raise RuntimeError("database_owner_probe_incomplete:receipt_inode_changed")
        return sorted({int(pid) for pid in row.get("owners", [])})
    raise RuntimeError("database_owner_probe_incomplete:database_missing_from_receipt")


def _cli_base_dirs(arguments: Iterable[str]):
    supplied = list(arguments)
    if supplied:
        return supplied, supplied
    return ["~/.hermes", "~/.anamnesis"], ["~/.hermes"]


def _header_journal_mode(db_path: Path) -> str:
    """Observe the file format without joining SQLite's live WAL lifecycle.

    Format 1 identifies the rollback family, not DELETE versus TRUNCATE/PERSIST.
    This is journal metadata only; it cannot establish database integrity.
    """
    with db_path.open("rb") as stream:
        header = stream.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise ValueError("sqlite_header_invalid")
    formats = tuple(header[18:20])
    if formats == (2, 2):
        return "wal"
    if formats == (1, 1):
        return "rollback"
    raise ValueError(f"sqlite_header_journal_format_invalid:{formats}")


def check_dbs(
    base_dirs,
    *,
    apply: bool = False,
    version=None,
    backup_root: Optional[Path] = None,
    require_stopped_home: Optional[Path] = None,
    require_unowned: bool = False,
    owner_probe_receipt: Optional[dict] = None,
    selected_databases: Optional[Iterable[str]] = None,
    check_integrity: bool = False,
    full_integrity: bool = False,
    explicit_databases: Optional[Iterable[str]] = None,
    required_base_dirs: Optional[Iterable[str]] = None,
):
    if apply:
        full_integrity = True
    sqlite_version = version_tuple(version or sqlite3.sqlite_version_info)
    target_mode = safe_journal_mode(sqlite_version)
    sqlite_access = apply or check_integrity or full_integrity
    operation = "apply" if apply else "integrity"
    if sqlite_access and require_stopped_home is None:
        raise RuntimeError(f"require_stopped_home_is_mandatory_for_{operation}")
    if sqlite_access and not require_unowned:
        raise RuntimeError(f"require_unowned_is_mandatory_for_{operation}")
    if sqlite_access:
        gateway_live = _gateway_pid_is_live(Path(require_stopped_home).expanduser())
        if gateway_live is True:
            raise RuntimeError("gateway_must_be_stopped_before_sqlite_journal_change")
        if gateway_live is None:
            raise RuntimeError("gateway_stop_state_unavailable")
    if apply and backup_root is None:
        raise ValueError("backup_root is required when applying journal changes")
    backup_root = Path(backup_root).expanduser() if backup_root is not None else None
    results = []
    candidates, excluded = active_db_candidates(
        base_dirs,
        explicit_databases=explicit_databases,
    )
    for item in required_base_dirs or []:
        required_path = Path(item).expanduser()
        try:
            resolved_required = required_path.resolve(strict=True)
        except OSError:
            resolved_required = required_path
        if not resolved_required.is_dir():
            excluded.append(
                {"db": str(required_path), "reason": "required_base_unresolved"}
            )
    blocking_exclusion_reasons = {
        "gateway_owned_unresolved",
        "owner_not_gateway_proven",
        "required_base_unresolved",
    }
    unexpected = [
        row for row in excluded if row.get("reason") in blocking_exclusion_reasons
    ]
    if selected_databases is not None:
        selected = {str(Path(item).expanduser()) for item in selected_databases}
        available = {str(path) for _, path in candidates}
        missing = sorted(selected - available)
        if missing:
            raise RuntimeError(f"selected_database_not_active:{missing}")
        candidates = [(base, path) for base, path in candidates if str(path) in selected]
    apply_changes = apply and not unexpected
    sqlite_access_allowed = sqlite_access and not unexpected
    if sqlite_access_allowed and owner_probe_receipt is not None:
        _validate_owner_probe_receipt(owner_probe_receipt)
    for base, db_path in candidates:
        backup_path = None
        backup_sha256 = None
        temporary = None
        integrity_check_result = None
        backup_integrity_check_result = None
        postchange_integrity_check_result = None
        try:
            if db_path.stat().st_size == 0:
                continue
            if not sqlite_access_allowed:
                mode = _header_journal_mode(db_path)
                results.append({
                    "db": str(db_path), "mode": mode, "previous_mode": mode,
                    "target_mode": target_mode,
                    "ok": mode == ("rollback" if target_mode == "delete" else target_mode),
                    "journal_evidence": "sqlite_header", "integrity_check": None,
                    "backup": None, "backup_sha256": None,
                    "exclusive_lock": False, "restored": False,
                })
                continue
            if require_unowned:
                if owner_probe_receipt is not None:
                    _validate_owner_probe_receipt(owner_probe_receipt)
                owners = (
                    _validated_owner_probe_pids(db_path, owner_probe_receipt)
                    if owner_probe_receipt is not None
                    else _linux_open_owner_pids(db_path)
                )
                if owners:
                    raise RuntimeError(f"database_owner_still_active:{owners}")
            with closing(sqlite3.connect(str(db_path), timeout=30)) as connection:
                exclusive_lock = False
                if apply_changes:
                    lock_mode = str(
                        connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0]
                    ).lower()
                    if lock_mode != "exclusive":
                        raise sqlite3.DatabaseError(
                            f"exclusive_lock_mode_unavailable:{lock_mode}"
                        )
                    connection.execute("BEGIN EXCLUSIVE")
                    connection.commit()
                    exclusive_lock = True
                if full_integrity:
                    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
                    if integrity_rows != [("ok",)]:
                        raise sqlite3.DatabaseError(
                            f"integrity_check={integrity_rows[:8]}"
                        )
                    integrity_check_result = "ok"
                elif apply_changes or check_integrity:
                    before_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                    if before_check != "ok":
                        raise sqlite3.DatabaseError(f"prechange quick_check={before_check}")
                before = str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                mode = before
                if apply_changes and before != target_mode:
                    relative = db_path.relative_to(base)
                    canonical_base = str(base.resolve()).encode("utf-8")
                    base_key = hashlib.sha256(canonical_base).hexdigest()[:16]
                    candidate_backup = backup_root / f"{base.name}-{base_key}" / relative
                    candidate_backup = candidate_backup.with_name(
                        candidate_backup.name + ".sqlite-backup"
                    )
                    candidate_backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    temporary = candidate_backup.with_name(
                        f".{candidate_backup.name}.{uuid.uuid4().hex}.tmp"
                    )
                    with closing(sqlite3.connect(str(temporary))) as backup:
                        connection.backup(backup)
                        if full_integrity:
                            backup_rows = backup.execute("PRAGMA integrity_check").fetchall()
                            if backup_rows != [("ok",)]:
                                raise sqlite3.DatabaseError(
                                    f"backup integrity_check={backup_rows[:8]}"
                                )
                            backup_integrity_check_result = "ok"
                        else:
                            backup_check = backup.execute("PRAGMA quick_check").fetchone()[0]
                            if backup_check != "ok":
                                raise sqlite3.DatabaseError(
                                    f"backup quick_check={backup_check}"
                                )
                    os.chmod(temporary, 0o600)
                    backup_sha256 = _sha256(temporary)
                    try:
                        os.link(temporary, candidate_backup)
                    except FileExistsError as exc:
                        raise RuntimeError(
                            f"backup_destination_already_exists:{candidate_backup}"
                        ) from exc
                    temporary.unlink()
                    temporary = None
                    backup_path = candidate_backup
                    pragma = f"PRAGMA journal_mode={target_mode.upper()}"
                    mode = str(connection.execute(pragma).fetchone()[0]).lower()
                    if full_integrity:
                        after_rows = connection.execute("PRAGMA integrity_check").fetchall()
                        if mode != target_mode or after_rows != [("ok",)]:
                            raise sqlite3.DatabaseError(
                                f"postchange mode={mode} integrity_check={after_rows[:8]}"
                            )
                        postchange_integrity_check_result = "ok"
                    else:
                        after_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                        if mode != target_mode or after_check != "ok":
                            raise sqlite3.DatabaseError(
                                f"postchange mode={mode} quick_check={after_check}"
                            )
            results.append(
                {
                    "db": str(db_path),
                    "mode": mode,
                    "previous_mode": before,
                    "target_mode": target_mode,
                    "ok": mode == target_mode,
                    "backup": str(backup_path) if backup_path else None,
                    "backup_sha256": backup_sha256,
                    "exclusive_lock": exclusive_lock,
                    "integrity_check": (
                        postchange_integrity_check_result or integrity_check_result
                    ),
                    "prechange_integrity_check": integrity_check_result,
                    "backup_integrity_check": backup_integrity_check_result,
                    "postchange_integrity_check": postchange_integrity_check_result,
                    "restored": False,
                }
            )
        except Exception as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            results.append(
                {
                    "db": str(db_path),
                    "mode": "error",
                    "target_mode": target_mode,
                    "ok": False,
                    "error": str(exc),
                    "backup": str(backup_path) if backup_path else None,
                    "restored": False,
                    "restore_error": "automatic_restore_prohibited_without_owner_quiescence",
                }
            )
    results.extend(
        {
            "db": row["db"],
            "mode": "unclassified",
            "target_mode": target_mode,
            "ok": False,
            "error": (
                "required_base_unresolved"
                if row.get("reason") == "required_base_unresolved"
                else "unexpected_active_tree_sqlite_owner_unclassified"
            ),
            "backup": None,
            "restored": False,
            "restore_error": None,
        }
        for row in unexpected
    )
    return results, excluded


if __name__ == "__main__":
    dirs, required_dirs = _cli_base_dirs(sys.argv[1:])
    version = version_tuple(sqlite3.sqlite_version_info)
    target = safe_journal_mode(version)
    results, excluded = check_dbs(
        dirs,
        version=version,
        required_base_dirs=required_dirs,
    )
    bad = [row for row in results if not row["ok"]]
    summary = {
        "sqlite_version": ".".join(str(item) for item in version),
        "wal_reset_bug_fixed": wal_reset_bug_fixed(version),
        "target_mode": target,
        "total": len(results),
        "wal": sum(row.get("mode") == "wal" for row in results),
        "non_wal": sum(row.get("mode") != "wal" for row in results),
        "findings": bad,
        "excluded_count": len(excluded),
        "status": "PASS" if not bad else "WARN",
    }
    print(json.dumps(summary))
    raise SystemExit(0 if not bad else 1)
