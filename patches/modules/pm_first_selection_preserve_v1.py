"""Carry source-installed features into the first managed dependency generation."""
from pathlib import Path

MARKER = "HERMES_PM_FIRST_SELECTION_PRESERVE_v1"
ANCHOR = '''    # The first writable generation replaces, rather than layers on,
    # the payload. Retain its extras until a recorded selection owns them.
    enabled = sorted(set(_still_declared(package, fact.get("extras", shipped or []))) | set(extras or []))
'''
REPLACEMENT = '''    # HERMES_PM_FIRST_SELECTION_PRESERVE_v1: source venvs have no feature ledger.
    if not fact and shipped is None:
        from pm.extras import legacy_selection
        shipped = legacy_selection(paths.repo_root())
''' + ANCHOR


def patch_pm_first_selection_preserve_v1(hermes_dir):
    path = Path(hermes_dir) / "pm/install.py"
    source = path.read_text(encoding="utf-8")
    if REPLACEMENT in source:
        return False
    if MARKER in source or source.count(ANCHOR) != 1:
        raise RuntimeError("first PM selection anchor changed")
    updated = source.replace(ANCHOR, REPLACEMENT, 1)
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8")
    return True
