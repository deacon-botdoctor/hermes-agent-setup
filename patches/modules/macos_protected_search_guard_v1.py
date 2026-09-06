#!/usr/bin/env python3
"""Prevent local macOS file searches from accidentally crossing TCC boundaries."""

from __future__ import annotations

from pathlib import Path

LEGACY_MARKER = "HERMES_MACOS_PROTECTED_SEARCH_GUARD_v1"
MARKER = "HERMES_MACOS_PROTECTED_SEARCH_GUARD_v1_r2"
TARGET = Path("tools/file_operations.py")


class PatchError(RuntimeError):
    pass


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise PatchError(f"required unique macOS protected-search anchor missing for {label}: found {count}")
    return source.replace(old, new, 1)


_LEGACY_HELPER = f'''\n\ndef _macos_protected_search_error(path: str) -> Optional[str]:
    """Return a fail-closed error before a search can reach protected app data.

    {LEGACY_MARKER}: a recursive search rooted at the macOS user home can traverse
    ``~/Library`` and cause a surprise "access data from other apps" prompt.
    The opt-in is host-owned process configuration, not a tool argument.
    """
    if sys.platform != "darwin":
        return None
    if os.environ.get("HERMES_ALLOW_MACOS_PROTECTED_SEARCH", "").strip().lower() in {{
        "1", "true", "yes", "on"
    }}:
        return None

    try:
        search_path = Path(os.path.expanduser(path)).resolve(strict=False)
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None

    library = home / "Library"
    broad_roots = {{Path("/"), home.parent, home}}
    in_library = search_path == library or library in search_path.parents
    if search_path not in broad_roots and not in_library:
        return None

    return (
        "macOS privacy guard blocked a broad or app-data search root before "
        "it could trigger a system permission prompt. Search a specific "
        "project folder instead. Operators may explicitly opt in with "
        "HERMES_ALLOW_MACOS_PROTECTED_SEARCH=1."
    )
'''

_LEGACY_ENTRY = """        privacy_error = _macos_protected_search_error(path)
        if privacy_error:
            return SearchResult(error=privacy_error, total_count=0)

"""

_METHODS = f'''    def _macos_protected_search_path(self, path: str) -> tuple[Optional[str], str]:
        """Reject local macOS searches that can cross protected app-data roots.

        {MARKER}: resolve relative paths from the terminal backend's live cwd,
        not the gateway process cwd. Reject protected lexical paths before any
        filesystem lookup, then inspect symlinks component by component without
        stepping into protected targets. Remote/sandbox backends remain unchanged.
        """
        if sys.platform != "darwin" or not self._lsp_local_only():
            return None, path
        if os.environ.get("HERMES_ALLOW_MACOS_PROTECTED_SEARCH", "").strip().lower() in {{
            "1", "true", "yes", "on"
        }}:
            return None, path

        def protected(candidate: str) -> tuple[bool, str]:
            def normalize(value) -> str:
                normalized = os.path.normpath(str(value))
                if normalized.startswith("//"):
                    normalized = os.sep + normalized.lstrip(os.sep)
                normalized = normalized.casefold()
                data_root = os.path.normpath("/System/Volumes/Data").casefold()
                if normalized == data_root:
                    return os.sep
                if normalized.startswith(data_root + os.sep):
                    return normalized[len(data_root):]
                return normalized

            try:
                home = Path(os.path.abspath(os.path.expanduser("~")))
                expanded = os.path.expanduser(str(candidate))
                if not os.path.isabs(expanded):
                    effective_cwd = getattr(self.env, "cwd", None) or self.cwd
                    expanded = os.path.join(str(effective_cwd), expanded)
                lexical_path = expanded
            except (OSError, RuntimeError, TypeError, ValueError):
                return True, str(candidate)

            def lexically_protected(value, *, traversal: bool = False) -> bool:
                normalized_path = normalize(value)
                normalized_home = normalize(home)
                normalized_users = normalize(home.parent)
                normalized_library = normalize(home / "Library")
                users_root = normalize(Path("/Users"))
                broad_roots = {{
                    normalize(Path("/")),
                    users_root,
                    normalize(Path("/System")),
                    normalize(Path("/System/Volumes")),
                    normalized_users,
                    normalized_home,
                }}
                if normalized_path in broad_roots:
                    return not traversal
                if (
                    normalized_path == normalized_library
                    or normalized_path.startswith(normalized_library + os.sep)
                ):
                    return True
                if normalized_path.startswith(users_root + os.sep):
                    user_parts = normalized_path[len(users_root + os.sep):].split(os.sep)
                    if len(user_parts) == 1:
                        return not traversal
                    if len(user_parts) >= 2 and user_parts[1] == "library":
                        return True
                return False

            if lexically_protected(lexical_path):
                return True, lexical_path

            try:
                pending = list(Path(lexical_path).parts)
                if not pending:
                    return True, lexical_path
                resolved = Path(pending.pop(0))
                symlink_hops = 0
                while pending:
                    component = pending.pop(0)
                    if component in {{"", "."}}:
                        continue
                    if component == "..":
                        resolved = resolved.parent
                        continue
                    next_path = resolved / component
                    if lexically_protected(next_path, traversal=True):
                        return True, str(next_path.joinpath(*pending))
                    try:
                        mode = os.lstat(next_path).st_mode
                    except FileNotFoundError:
                        safe_path = next_path.joinpath(*pending)
                        return False, str(safe_path)
                    if not stat.S_ISLNK(mode):
                        resolved = next_path
                        continue

                    symlink_hops += 1
                    if symlink_hops > 40:
                        return True, lexical_path
                    target = os.readlink(next_path)
                    target_path = Path(target)
                    if not target_path.is_absolute():
                        target_path = next_path.parent / target_path
                    substituted_path = target_path.joinpath(*pending)
                    if lexically_protected(substituted_path):
                        return True, str(substituted_path)
                    target_parts = list(target_path.parts)
                    if not target_parts:
                        return True, lexical_path
                    resolved = Path(target_parts.pop(0))
                    pending = target_parts + pending
                return lexically_protected(resolved), str(resolved)
            except (OSError, RuntimeError, TypeError, ValueError):
                return True, lexical_path

        blocked, safe_path = protected(path)
        if not blocked:
            return None, safe_path

        return (
            "macOS privacy guard blocked a broad or app-data search root before "
            "it could trigger a system permission prompt. Search a specific "
            "project folder instead. Operators may explicitly opt in with "
            "HERMES_ALLOW_MACOS_PROTECTED_SEARCH=1.",
            safe_path,
        )

'''

_SEARCH_ENTRY_GUARD = """        privacy_error, path = self._macos_protected_search_path(path)
        if privacy_error:
            return SearchResult(error=privacy_error, total_count=0)

"""

_MULTI_PATH_GUARD = """        expanded_parts = [self._expand_path(p) for p in parts]
        for index, expanded in enumerate(expanded_parts):
            privacy_error, expanded_parts[index] = self._macos_protected_search_path(expanded)
            if privacy_error:
                return SearchResult(error=privacy_error, total_count=0)

        existing, missing = [], []
        for expanded in expanded_parts:
"""

_METHODS_021_ANCHOR = "    def _apply_file_search_policy(self, path: str) -> tuple[Optional[str], str]:\n"
_SEARCH_ENTRY_021 = """        offset, limit = normalize_search_pagination(offset, limit)

        policy_error, path = self._apply_file_search_policy(path)
"""
_SEARCH_ENTRY_021_GUARDED = """        offset, limit = normalize_search_pagination(offset, limit)

        privacy_error, path = self._macos_protected_search_path(path)
        if privacy_error:
            return SearchResult(error=privacy_error, total_count=0)

        policy_error, path = self._apply_file_search_policy(path)
"""
_MULTI_PATH_021 = """        existing, missing = [], []
        for p in parts:
            policy_error, p = self._apply_file_search_policy(p)
"""
_MULTI_PATH_021_GUARDED = """        existing, missing = [], []
        for p in parts:
            privacy_error, p = self._macos_protected_search_path(p)
            if privacy_error:
                return SearchResult(error=privacy_error, total_count=0)
            policy_error, p = self._apply_file_search_policy(p)
"""


def patch_source(source: str) -> str:
    if MARKER in source:
        required = {
            "privacy methods": _METHODS,
            "search entry guard": _SEARCH_ENTRY_GUARD,
            "multi-path component guard": (
                _MULTI_PATH_GUARD if _MULTI_PATH_GUARD in source else _MULTI_PATH_021_GUARDED
            ),
        }
        missing = [label for label, seam in required.items() if seam not in source]
        if missing:
            raise PatchError("incomplete installed macOS protected-search guard: missing " + ", ".join(missing))
        return source

    if LEGACY_MARKER in source:
        source = _replace_once(source, _LEGACY_HELPER, "", "legacy privacy helper")
        source = _replace_once(source, _LEGACY_ENTRY, "", "legacy search entry guard")

    if "import sys\n" not in source:
        source = _replace_once(
            source,
            "import os\n",
            "import os\nimport sys\n",
            "sys import",
        )
    if "import stat\n" not in source:
        source = _replace_once(
            source,
            "import os\n",
            "import os\nimport stat\n",
            "stat import",
        )
    legacy_method_anchor = """    # =========================================================================
    # SEARCH Implementation
    # =========================================================================

    def search"""
    legacy_method_anchor_with_indented_blank = legacy_method_anchor.replace(
        "\n\n    def search", "\n    \n    def search"
    )
    legacy_method_replacement = (
        """    # =========================================================================
    # SEARCH Implementation
    # =========================================================================

"""
        + _METHODS
        + """    def search"""
    )
    installed_legacy_method_anchor = next(
        (
            anchor
            for anchor in (legacy_method_anchor, legacy_method_anchor_with_indented_blank)
            if anchor in source
        ),
        None,
    )
    if installed_legacy_method_anchor is not None:
        source = _replace_once(
            source,
            installed_legacy_method_anchor,
            legacy_method_replacement,
            "privacy methods",
        )
    else:
        source = _replace_once(
            source,
            _METHODS_021_ANCHOR,
            _METHODS + _METHODS_021_ANCHOR,
            "privacy methods",
        )
    legacy_search_entry = """        offset, limit = normalize_search_pagination(offset, limit)

        # Expand ~ and other shell paths
"""
    if legacy_search_entry in source:
        source = _replace_once(
            source,
            legacy_search_entry,
            """        offset, limit = normalize_search_pagination(offset, limit)

"""
            + _SEARCH_ENTRY_GUARD
            + """        # Expand ~ and other shell paths
""",
            "search entry guard",
        )
    else:
        source = _replace_once(
            source,
            _SEARCH_ENTRY_021,
            _SEARCH_ENTRY_021_GUARDED,
            "search entry guard",
        )
    legacy_multi_path = """        existing, missing = [], []
        for p in parts:
            expanded = self._expand_path(p)
            chk = self._exec(
"""
    if legacy_multi_path in source:
        source = _replace_once(
            source,
            legacy_multi_path,
            _MULTI_PATH_GUARD
            + """            chk = self._exec(
""",
            "multi-path component guard",
        )
    else:
        source = _replace_once(
            source,
            _MULTI_PATH_021,
            _MULTI_PATH_021_GUARDED,
            "multi-path component guard",
        )
    return source


def patch_macos_protected_search_guard_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / TARGET
    if not target.is_file():
        raise PatchError(f"required file missing: {target}")
    before = target.read_text(encoding="utf-8")
    search_target = target.with_name("file_operations_search.py")
    if search_target.is_file():
        search_before = search_target.read_text(encoding="utf-8")
        after, search_after = patch_refactored_sources(before, search_before)
        if (before, search_before) == (after, search_after):
            return False
        target.write_text(after, encoding="utf-8")
        search_target.write_text(search_after, encoding="utf-8")
        return True
    after = patch_source(before)
    if after == before:
        return False
    target.write_text(after, encoding="utf-8")
    return True


def patch_refactored_sources(source: str, search_source: str) -> tuple[str, str]:
    """Keep admission before filesystem probes in both extracted search owners."""
    search_anchor = "    # --- SEARCH -------------------------------------------------------------\n"
    entry_anchor = "        offset, limit = normalize_search_pagination(offset, limit)\n"
    multi_anchor = '''        existing, missing = [], []
        for p in parts:
            expanded = self._expand_path(p)
'''
    multi_guarded = f'''        # {MARKER}: admit every component before probing existence.
        expanded_parts = [self._expand_path(p) for p in parts]
        for index, expanded in enumerate(expanded_parts):
            privacy_error, expanded_parts[index] = self._macos_protected_search_path(expanded)
            if privacy_error:
                return SearchResult(error=privacy_error, total_count=0)
        existing, missing = [], []
        for expanded in expanded_parts:
'''
    if MARKER in source or MARKER in search_source:
        if _METHODS not in source or _SEARCH_ENTRY_GUARD not in source or multi_guarded not in search_source:
            raise PatchError("incomplete refactored macOS protected-search guard")
        return source, search_source
    for module_name in ("sys", "stat"):
        if f"import {module_name}\n" not in source:
            source = _replace_once(source, "import os\n", f"import os\nimport {module_name}\n", module_name + " import")
    source = _replace_once(source, search_anchor, _METHODS + search_anchor, "refactored privacy method")
    source = _replace_once(source, entry_anchor, entry_anchor + _SEARCH_ENTRY_GUARD, "refactored search admission")
    search_source = _replace_once(search_source, multi_anchor, multi_guarded, "refactored multi-path admission")
    compile(source, str(TARGET), "exec")
    compile(search_source, "tools/file_operations_search.py", "exec")
    return source, search_source


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_macos_protected_search_guard_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
