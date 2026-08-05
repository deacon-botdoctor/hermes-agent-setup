#!/usr/bin/env python3
"""Keep invalid command tokens from crashing the lifecycle safety guard."""

from __future__ import annotations

import shutil
from pathlib import Path

MARKER = "HERMES_LIFECYCLE_GUARD_INVALID_PATH_v1"
RESOLVE_MARKER = "HERMES_LIFECYCLE_GUARD_INVALID_PATH_RESOLVE_v2"
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
RESOLVE_ANCHOR = '''def _resolve_terminal_script_path(candidate: str, cwd: Optional[str]) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = Path(cwd or Path.cwd()) / path
    return path
'''
RESOLVE_REPLACEMENT = f'''def _resolve_terminal_script_path(candidate: str, cwd: Optional[str]) -> Optional[Path]:
    # {RESOLVE_MARKER}: malformed nested shell payloads can surface an embedded
    # NUL before the referenced-script reader reaches os.open(). Treat that
    # token as unresolvable and let the caller ignore it.
    if "\\x00" in candidate:
        return None
    try:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = Path(cwd or Path.cwd()) / path
    except (OSError, ValueError):
        return None
    return path
'''
YIELD_REPLACEMENTS = {
    "                yield _resolve_terminal_script_path(segment[index + 1], cwd)\n": (
        "                resolved = _resolve_terminal_script_path(segment[index + 1], cwd)\n"
        "                if resolved is not None:\n"
        "                    yield resolved\n"
    ),
    "                yield _resolve_terminal_script_path(arguments[arg_index], cwd)\n": (
        "                resolved = _resolve_terminal_script_path(arguments[arg_index], cwd)\n"
        "                if resolved is not None:\n"
        "                    yield resolved\n"
    ),
    "                yield _resolve_terminal_script_path(executable, cwd)\n": (
        "                resolved = _resolve_terminal_script_path(executable, cwd)\n"
        "                if resolved is not None:\n"
        "                    yield resolved\n"
    ),
}


def patch_lifecycle_guard_invalid_path_v1(root: Path) -> bool:
    target = Path(root) / TARGET
    original = target.read_text(encoding="utf-8")
    if MARKER in original and RESOLVE_MARKER in original:
        return False
    patched = original
    if MARKER not in patched:
        if patched.count(ANCHOR) != 1:
            raise RuntimeError("lifecycle guard invalid-path reader anchor drift")
        patched = patched.replace(ANCHOR, REPLACEMENT, 1)
    if RESOLVE_MARKER not in patched:
        if patched.count(RESOLVE_ANCHOR) != 1:
            raise RuntimeError("lifecycle guard invalid-path resolver anchor drift")
        patched = patched.replace(RESOLVE_ANCHOR, RESOLVE_REPLACEMENT, 1)
        for anchor, replacement in YIELD_REPLACEMENTS.items():
            if patched.count(anchor) != 1:
                raise RuntimeError("lifecycle guard invalid-path yield anchor drift")
            patched = patched.replace(anchor, replacement, 1)
    backup = Path(str(target) + ".bak-pre-lifecycle-guard-invalid-path-v1")
    shutil.copy2(target, backup)
    try:
        target.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, target)
        backup.unlink(missing_ok=True)
        raise
    return True
