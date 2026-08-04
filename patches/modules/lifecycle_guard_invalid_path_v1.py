#!/usr/bin/env python3
"""Keep invalid command tokens from crashing the lifecycle safety guard."""

from __future__ import annotations

import shutil
from pathlib import Path

MARKER = "HERMES_LIFECYCLE_GUARD_INVALID_PATH_v1"
TARGET = Path("cron/lifecycle_guard.py")
ANCHOR = '''    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, False
'''
REPLACEMENT = f'''    try:
        descriptor = os.open(path, flags)
    # {MARKER}: PathLike values can carry an embedded NUL from a malformed
    # tool argument. os.open raises ValueError before the syscall in that
    # case; treat it exactly like an unreadable reference instead of crashing
    # the entire terminal tool and its active agent turn.
    except (OSError, ValueError):
        return None, False
'''


def patch_lifecycle_guard_invalid_path_v1(root: Path) -> bool:
    target = Path(root) / TARGET
    original = target.read_text(encoding="utf-8")
    if MARKER in original:
        return False
    if original.count(ANCHOR) != 1:
        raise RuntimeError("lifecycle guard invalid-path anchor drift")
    patched = original.replace(ANCHOR, REPLACEMENT, 1)
    backup = Path(str(target) + ".bak-pre-lifecycle-guard-invalid-path-v1")
    shutil.copy2(target, backup)
    try:
        target.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, target)
        backup.unlink(missing_ok=True)
        raise
    return True
