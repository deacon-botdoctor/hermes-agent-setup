"""Retain the validated X Search model override used by the shared research CLI.

The default model belongs to shared configuration. Native Hermes owns catalogs,
metadata and reasoning timeouts.
"""
from __future__ import annotations

import ast
from pathlib import Path

MARKER = "HERMES_GROK_46_X_SEARCH_v1"


class PatchError(RuntimeError):
    pass


def patch_x_search_source(source: str) -> str:
    resolver = '''

def _resolve_x_search_model(override: str = "") -> str:
    """Resolve an optional caller override without permitting non-Grok routes."""
    # HERMES_GROK_46_X_SEARCH_v1: keep explicit caller validation at the owner.
    model = str(override or "").strip() or str(_load_x_search_config().get("model") or "").strip()
    model = model or DEFAULT_X_SEARCH_MODEL
    if not model.lower().startswith("grok-"):
        raise ValueError("x_search model must be a Grok model ID")
    return model
'''
    replacements = (
        ('\n\ndef _get_x_search_reasoning_effort() -> Optional[str]:\n',
         resolver + '\n\ndef _get_x_search_reasoning_effort() -> Optional[str]:\n'),
        ('    enable_video_understanding: bool = False,\n) -> str:\n',
         '    enable_video_understanding: bool = False,\n    model: str = "",\n) -> str:\n'),
        ('        reasoning_effort = _get_x_search_reasoning_effort()\n',
         '        selected_model = _resolve_x_search_model(model)\n'
         '        reasoning_effort = _get_x_search_reasoning_effort()\n'),
        ('            "model": str(_load_x_search_config().get("model") or "").strip() or DEFAULT_X_SEARCH_MODEL,\n',
         '            "model": selected_model,\n'),
    )
    if MARKER in source:
        if not all(new in source for _, new in replacements):
            raise PatchError("marked X Search override is incomplete")
        return source
    for old, new in replacements:
        if source.count(old) != 1:
            raise PatchError("X Search model override anchor drift")
        source = source.replace(old, new, 1)
    ast.parse(source)
    return source


PATCHERS = {Path("tools/x_search_tool.py"): patch_x_search_source}


def patch_grok_46_x_search_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "tools/x_search_tool.py"
    source = target.read_text(encoding="utf-8")
    updated = patch_x_search_source(source)
    if updated == source:
        return False
    target.write_text(updated, encoding="utf-8")
    return True
