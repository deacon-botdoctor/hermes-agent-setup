#!/usr/bin/env python3
"""Audit Hermes SQLite databases against the safe journal mode for this build.

SQLite's WAL-reset corruption bug affects ordinary releases from 3.7.0 through
3.51.2. Fixed backports exist for 3.44.6 and 3.50.7. Vulnerable builds should
use rollback journaling; fixed builds may use WAL.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable


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


def safe_journal_mode(version: Iterable[int]) -> str:
    return "wal" if wal_reset_bug_fixed(version) else "delete"


def check_dbs(base_dirs, *, apply: bool = False, version=None):
    sqlite_version = version_tuple(version or sqlite3.sqlite_version_info)
    target_mode = safe_journal_mode(sqlite_version)
    results = []
    for base in base_dirs:
        base = Path(base).expanduser()
        if not base.exists():
            continue
        for db_path in sorted(base.rglob("*.db")):
            if any(
                part in str(db_path)
                for part in ("venv", "node_modules", "firecrawl", "snapshot", "__pycache__")
            ):
                continue
            try:
                if db_path.stat().st_size == 0:
                    continue
                with sqlite3.connect(str(db_path)) as connection:
                    before = str(
                        connection.execute("PRAGMA journal_mode").fetchone()[0]
                    ).lower()
                    mode = before
                    if apply and before != target_mode:
                        pragma = f"PRAGMA journal_mode={target_mode.upper()}"
                        mode = str(connection.execute(pragma).fetchone()[0]).lower()
                results.append(
                    {
                        "db": str(db_path),
                        "mode": mode,
                        "previous_mode": before,
                        "target_mode": target_mode,
                        "ok": mode == target_mode,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "db": str(db_path),
                        "mode": "error",
                        "target_mode": target_mode,
                        "ok": False,
                        "error": str(exc),
                    }
                )
    return results


if __name__ == "__main__":
    dirs = sys.argv[1:] or ["~/.hermes", "~/.anamnesis"]
    version = version_tuple(sqlite3.sqlite_version_info)
    target = safe_journal_mode(version)
    results = check_dbs(dirs, version=version)
    bad = [row for row in results if not row["ok"]]
    summary = {
        "sqlite_version": ".".join(str(item) for item in version),
        "wal_reset_bug_fixed": wal_reset_bug_fixed(version),
        "target_mode": target,
        "total": len(results),
        "wal": sum(row.get("mode") == "wal" for row in results),
        "non_wal": sum(row.get("mode") != "wal" for row in results),
        "findings": bad,
        "status": "PASS" if not bad else "WARN",
    }
    print(json.dumps(summary))
    raise SystemExit(0 if not bad else 1)
