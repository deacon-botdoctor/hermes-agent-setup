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


def _replace_in_method_once(
    source: str, method_name: str, old: str, new: str, label: str
) -> str:
    start_anchor = f"\n    def {method_name}("
    start = source.find(start_anchor)
    if start < 0:
        raise PatchError(f"{label}: method not found")
    next_method = source.find("\n    def ", start + len(start_anchor))
    next_static = source.find("\n    @staticmethod", start + len(start_anchor))
    stops = [item for item in (next_method, next_static) if item >= 0]
    end = min(stops) if stops else len(source)
    section = source[start:end]
    if section.count(old) != 1:
        raise PatchError(f"{label}: expected one anchor, found {section.count(old)}")
    return source[:start] + section.replace(old, new, 1) + source[end:]


def patch_hermes_state_source(source: str) -> str:
    required = (
        "COALESCE(archived, 0) = 0",
        "COALESCE(pinned, 0) = 0",
    )
    if MARKER in source:
        archived_defaults = source.count('filters.setdefault("archived", False)')
        pinned_defaults = source.count('filters.setdefault("pinned", False)')
        modern_shape = (
            "    def _prune_filter_where(" in source
            and 'clauses.append("s.pinned = 0")' in source
        )
        modern_complete = modern_shape and archived_defaults >= 2 and pinned_defaults >= 2
        legacy_complete = (
            "retention_columns = {" in source
            and "keep_predicates = [\"COALESCE(archived, 0) = 0\"]" in source
            and "if \"pinned\" in retention_columns:" in source
        ) or all(source.count(item) >= 2 for item in required)
        if modern_complete or legacy_complete:
            return source
        native_complete = (
            "include_pinned: bool = False" in source
            and source.count('filters.setdefault("archived", False)') >= 5
        )
        if native_complete:
            return source
        if modern_shape and archived_defaults >= 1 and pinned_defaults == 1:
            # Upgrade the first canary shape, which guarded prune_sessions but
            # predated aligned list_prune_candidates previews.
            source = _replace_once(
                source,
                "        if filters.get(\"started_before\") is None and older_than_days is not None:\n"
                "            filters[\"started_before\"] = time.time() - (older_than_days * 86400)\n"
                "        where, params = self._prune_filter_where(source=source, **filters)\n"
                "        with self._lock:\n",
                "        # Default candidate previews to the same protected set used by\n"
                "        # prune_sessions; callers can request an explicit keep lane.\n"
                "        filters.setdefault(\"archived\", False)\n"
                "        filters.setdefault(\"pinned\", False)\n"
                "        if filters.get(\"started_before\") is None and older_than_days is not None:\n"
                "            filters[\"started_before\"] = time.time() - (older_than_days * 86400)\n"
                "        where, params = self._prune_filter_where(source=source, **filters)\n"
                "        with self._lock:\n",
                "modern candidate preview upgrade",
            )
            ast.parse(source)
            return source
        raise PatchError("marked hermes_state.py is missing retention guards")

    if (
        "include_pinned: bool = False" in source
        and "    def _apply_prune_age_filter(" in source
    ):
        age_anchor = "        self._apply_prune_age_filter(older_than_days, filters)\n"
        preview = (
            "        # Default previews/counts to the protected automatic-retention set.\n"
            "        filters.setdefault(\"archived\", False)\n"
            + age_anchor
        )
        for method_name in (
            "list_prune_candidates",
            "count_prune_matches",
            "count_open_prune_matches",
        ):
            source = _replace_in_method_once(
                source,
                method_name,
                age_anchor,
                preview,
                f"native {method_name} archived default",
            )
        source = _replace_in_method_once(
            source,
            "prune_sessions",
            age_anchor,
            "        # [HERMES_SESSION_RETENTION_GUARD_v1] Archived and pinned\n"
            "        # sessions are explicit keep-signals for default/automatic retention.\n"
            "        filters.setdefault(\"archived\", False)\n"
            + age_anchor,
            "native prune archived default",
        )
        ast.parse(source)
        return source

    if "    def _prune_filter_where(" in source:
        source = _replace_once(
            source,
            "        archived: Optional[bool] = None,\n"
            "        model_like: Optional[str] = None,\n",
            "        archived: Optional[bool] = None,\n"
            "        pinned: Optional[bool] = None,\n"
            "        model_like: Optional[str] = None,\n",
            "modern pinned filter signature",
        )
        source = _replace_once(
            source,
            "        elif archived is False:\n"
            '            clauses.append("s.archived = 0")\n'
            '        return " AND ".join(clauses), params\n',
            "        elif archived is False:\n"
            '            clauses.append("s.archived = 0")\n'
            "        if pinned is True:\n"
            '            clauses.append("s.pinned = 1")\n'
            "        elif pinned is False:\n"
            '            clauses.append("s.pinned = 0")\n'
            '        return " AND ".join(clauses), params\n',
            "modern pinned filter clause",
        )
        source = _replace_once(
            source,
            "        if filters.get(\"started_before\") is None and older_than_days is not None:\n"
            "            filters[\"started_before\"] = time.time() - (older_than_days * 86400)\n"
            "        where, params = self._prune_filter_where(source=source, **filters)\n"
            "        with self._lock:\n",
            "        # Default candidate previews to the same protected set used by\n"
            "        # prune_sessions; callers can request an explicit keep lane.\n"
            "        filters.setdefault(\"archived\", False)\n"
            "        filters.setdefault(\"pinned\", False)\n"
            "        if filters.get(\"started_before\") is None and older_than_days is not None:\n"
            "            filters[\"started_before\"] = time.time() - (older_than_days * 86400)\n"
            "        where, params = self._prune_filter_where(source=source, **filters)\n"
            "        with self._lock:\n",
            "modern candidate preview defaults",
        )
        source = _replace_once(
            source,
            "        if filters.get(\"started_before\") is None and older_than_days is not None:\n"
            "            filters[\"started_before\"] = time.time() - (older_than_days * 86400)\n"
            "        where, where_params = self._prune_filter_where(source=source, **filters)\n",
            "        # [HERMES_SESSION_RETENTION_GUARD_v1] Explicit keep-signals\n"
            "        # survive both automatic and default manual retention.\n"
            "        filters.setdefault(\"archived\", False)\n"
            "        filters.setdefault(\"pinned\", False)\n"
            "        if filters.get(\"started_before\") is None and older_than_days is not None:\n"
            "            filters[\"started_before\"] = time.time() - (older_than_days * 86400)\n"
            "        where, where_params = self._prune_filter_where(source=source, **filters)\n",
            "modern prune defaults",
        )
        source = _replace_once(
            source,
            "        Default behavior (no keyword filters) is unchanged: delete ended\n"
            "        sessions older than ``older_than_days`` days, optionally restricted\n"
            "        to ``source``. Additional keyword filters AND together — the full\n",
            "        Default behavior deletes ended, unarchived, unpinned sessions older\n"
            "        than ``older_than_days`` days, optionally restricted to ``source``.\n"
            "        Explicit keyword filters may override those keep-signal defaults.\n"
            "        Additional keyword filters AND together — the full\n",
            "modern prune contract",
        )
        ast.parse(source)
        return source

    legacy_if_indent = (
        "            "
        if "\n            if source:\n                cursor = conn.execute(" in source
        else "        "
    )
    source = _replace_in_method_once(
        source,
        "prune_sessions",
        f"{legacy_if_indent}if source:\n",
        f"{legacy_if_indent}# [HERMES_SESSION_RETENTION_GUARD_v1] Legacy databases may\n"
        f"{legacy_if_indent}# predate the pinned feature. Build the keep predicate from\n"
        f"{legacy_if_indent}# columns that actually exist so the guard fails safely on both\n"
        f"{legacy_if_indent}# schema generations.\n"
        f"{legacy_if_indent}retention_columns = {{\n"
        f"{legacy_if_indent}    row[1] for row in conn.execute(\"PRAGMA table_info(sessions)\")\n"
        f"{legacy_if_indent}}}\n"
        f"{legacy_if_indent}keep_predicates = [\"COALESCE(archived, 0) = 0\"]\n"
        f"{legacy_if_indent}if \"pinned\" in retention_columns:\n"
        f"{legacy_if_indent}    keep_predicates.append(\"COALESCE(pinned, 0) = 0\")\n"
        f"{legacy_if_indent}keep_where = \" AND \".join(keep_predicates)\n"
        f"{legacy_if_indent}if source:\n",
        "legacy schema-aware keep predicates",
    )
    source = _replace_once(
        source,
        '"""SELECT id FROM sessions\n'
        '                       WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?"""',
        '"SELECT id FROM sessions "\n'
        '                    "WHERE started_at < ? AND ended_at IS NOT NULL AND "\n'
        '                    + keep_where + " AND source = ?"',
        "source-scoped prune query",
    )
    source = _replace_once(
        source,
        '"SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL"',
        '"SELECT id FROM sessions "\n'
        '                    "WHERE started_at < ? AND ended_at IS NOT NULL AND "\n'
        '                    + keep_where',
        "all-source prune query",
    )
    source = _replace_once(
        source,
        "        Only prunes ended sessions (not active ones).  Child sessions outside\n",
        "        Only prunes ended, unarchived, unpinned sessions.\n"
        f"        # [{MARKER}] Explicit keep-signals survive automatic retention.\n"
        "        Child sessions outside\n",
        "prune contract documentation",
    )
    ast.parse(source)
    return source


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
            "def _prune_where(self, older_than_days, source, filters)",
            'filters.setdefault("archived", False)',
            "include_pinned: bool = False",
        )
        if not all(item in source for item in required):
            raise PatchError("marked hermes_state_maintenance.py is incomplete")
        return source
    source = _replace_once(
        source,
        "    def _prune_where(self, older_than_days, source, filters) -> Tuple[str, list]:\n"
        '        """Translate the legacy age window into the shared activity filter, then build WHERE."""\n'
        "        if (older_than_days is not None and filters.get(\"last_active_before\") is None\n",
        "    def _prune_where(self, older_than_days, source, filters) -> Tuple[str, list]:\n"
        '        """Translate the legacy age window into the shared activity filter, then build WHERE."""\n'
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
    path = maintenance_path if maintenance_path.is_file() else hermes_dir / "hermes_state.py"
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    patched = (
        patch_hermes_state_maintenance_source(original)
        if path == maintenance_path
        else patch_hermes_state_source(original)
    )
    if patched == original:
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}-session-retention-guard-v1"))
    path.write_text(patched, encoding="utf-8")
    return True
