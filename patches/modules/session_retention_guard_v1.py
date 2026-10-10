#!/usr/bin/env python3
"""Protect explicit keep-signals from Hermes' automatic session pruning."""

from __future__ import annotations

import ast
import shutil
import time
from pathlib import Path

MARKER = "HERMES_SESSION_RETENTION_GUARD_v1"


class PatchError(RuntimeError):
    pass


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one anchor, found {count}")
    return source.replace(old, new, 1)


def patch_hermes_state_maintenance_source(source: str) -> str:
    """Port the retention invariant to Hermes' extracted maintenance mixin.

    Hermes 0.21 moved pruning from ``hermes_state.py`` into this topical module.
    Pinned rows already have a native, opt-in-only delete lane; Golden retains the
    archived keep-signal by making it the common default at ``_prune_where``.
    Explicit ``archived=`` and ``include_pinned=`` filters still select their
    documented maintenance lanes.
    """
    if MARKER in source:
        required = (
            "def _prune_where(self, older_than_days, source, filters, *, whole_lineages: bool = False)",
            'filters.setdefault("archived", False)',
            "include_pinned: bool = False",
        )
        if not all(item in source for item in required):
            raise PatchError("marked hermes_state_maintenance.py is incomplete")
        return source
    source = _replace_once(
        source,
        "    def _prune_where(self, older_than_days, source, filters, *, whole_lineages: bool = False) -> Tuple[str, list]:\n"
        '        """Translate the legacy age window into the shared activity filter, then build WHERE.\n'
        '        ``whole_lineages`` (prune) keeps a compression ancestor while any continuation after it\n'
        '        is unmatched."""\n'
        "        if (older_than_days is not None and filters.get(\"last_active_before\") is None\n",
        "    def _prune_where(self, older_than_days, source, filters, *, whole_lineages: bool = False) -> Tuple[str, list]:\n"
        '        """Translate the legacy age window into the shared activity filter, then build WHERE.\n'
        '        ``whole_lineages`` (prune) keeps a compression ancestor while any continuation after it\n'
        '        is unmatched."""\n'
        "        # [HERMES_SESSION_RETENTION_GUARD_v1] Default prune previews,\n"
        "        # counts, and deletion preserve archived rows. Pinned rows are\n"
        "        # already native opt-in-only candidates via include_pinned.\n"
        "        filters.setdefault(\"archived\", False)\n"
        "        if (older_than_days is not None and filters.get(\"last_active_before\") is None\n",
        "refactored maintenance archived default",
    )
    ast.parse(source)
    return source


def patch_session_retention_guard_v1(hermes_dir: Path) -> bool:
    maintenance_path = hermes_dir / "hermes_state_maintenance.py"
    path = maintenance_path
    if not path.is_file():
        raise PatchError("Hermes maintenance module missing; expected pinned split layout")
    original = path.read_text(encoding="utf-8")
    patched = patch_hermes_state_maintenance_source(original)
    if patched == original:
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}-session-retention-guard-v1"))
    path.write_text(patched, encoding="utf-8")
    return True
