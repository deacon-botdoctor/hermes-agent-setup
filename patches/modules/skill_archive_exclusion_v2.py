#!/usr/bin/env python3
"""Keep retained skill-package archives out of live skill discovery."""
from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Optional

MARKER = "HERMES_SKILL_ARCHIVE_EXCLUSION_v2"
BACKUP_SUFFIX = ".bak-pre-skill-archive-exclusion-v2"

FUNCTION_ANCHOR = "def is_excluded_skill_path(path, *, root: Optional[Path] = None) -> bool:\n"
LEGACY_PREDICATE = '    return value.startswith(("drafts-v2.shipped-", ".bak-fleet-package-")) or value == ".bak-fleet-package"\n'
ARCHIVE_PREDICATE = '    return value.startswith(("drafts-v2.shipped-", "drafts-v2.pulled-", "drafts-v2.stale-")) or ".bak-fleet-package-" in value or value.endswith(".bak-fleet-package")\n'
FUNCTION_REPLACEMENT = f'''def is_excluded_skill_archive_dir_name(name: str) -> bool:
    """Return True for preserved package snapshots that are not live skills.

    # HERMES_SKILL_ARCHIVE_EXCLUSION_v2
    """
    value = str(name)
{ARCHIVE_PREDICATE.rstrip()}


def is_excluded_skill_path(path, *, root: Optional[Path] = None) -> bool:
'''

PATH_OLD = '''    return any(part in EXCLUDED_SKILL_DIRS for part in parts) or is_skill_support_path(
        path, root=root
    )
'''
PATH_NEW = '''    return any(
        part in EXCLUDED_SKILL_DIRS or is_excluded_skill_archive_dir_name(part)
        for part in parts
    ) or is_skill_support_path(path, root=root)
'''

WALK_OLD = '''            if d not in EXCLUDED_SKILL_DIRS
            and not (has_skill_md and d in SKILL_SUPPORT_DIRS)
'''
WALK_NEW = '''            if d not in EXCLUDED_SKILL_DIRS
            and not is_excluded_skill_archive_dir_name(d)
            and not (has_skill_md and d in SKILL_SUPPORT_DIRS)
'''

COMPACT_PATH_OLD = "    return any(part in EXCLUDED_SKILL_DIRS for part in parts) or is_skill_support_path(path, root=root)\n"
COMPACT_WALK_OLD = "        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS and not (has_skill_md and d in SKILL_SUPPORT_DIRS)]\n"
COMPACT_WALK_NEW = "        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS and not is_excluded_skill_archive_dir_name(d) and not (has_skill_md and d in SKILL_SUPPORT_DIRS)]\n"


def patch_source(source: str) -> Optional[str]:
    if MARKER in source:
        if source.count(ARCHIVE_PREDICATE) == 1:
            return None
        if source.count(LEGACY_PREDICATE) != 1:
            raise RuntimeError("[skill_archive_exclusion] marked predicate drift")
        patched = source.replace(LEGACY_PREDICATE, ARCHIVE_PREDICATE, 1)
        ast.parse(patched)
        return patched
    path_old = PATH_OLD if PATH_OLD in source else COMPACT_PATH_OLD
    walk_old, walk_new = (WALK_OLD, WALK_NEW) if WALK_OLD in source else (COMPACT_WALK_OLD, COMPACT_WALK_NEW)
    for label, anchor in (("function", FUNCTION_ANCHOR), ("path", path_old), ("walk", walk_old)):
        if source.count(anchor) != 1:
            raise RuntimeError(f"[skill_archive_exclusion] {label} anchor mismatch")
    patched = source.replace(FUNCTION_ANCHOR, FUNCTION_REPLACEMENT, 1)
    patched = patched.replace(path_old, PATH_NEW, 1).replace(walk_old, walk_new, 1)
    ast.parse(patched)
    return patched


def patch_skill_archive_exclusion_v2(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "agent" / "skill_utils.py"
    if not target.exists():
        raise RuntimeError(f"[skill_archive_exclusion] runtime target missing: {target}")
    source = target.read_text(encoding="utf-8")
    patched = patch_source(source)
    if patched is None:
        return False
    backup = Path(str(target) + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(patched, encoding="utf-8")
    return True
