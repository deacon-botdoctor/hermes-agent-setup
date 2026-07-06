#!/usr/bin/env python3
"""_example_patch — copy this to start a new patch module.

Rename the file to your patch's name (matching the registry `module:` field), set MARKER and
TARGET, and write the anchor edits. Keep anchors small and unique so they survive minor upstream
churn; when they don't, apply() returns "anchor-miss" and the rehearsal stage flags it before
anything deploys.

Rules of a good patch module:
  - MARKER is unique and grep-able. The engine uses it for idempotency, so a re-apply is a no-op.
  - Anchor on the smallest stable substring, not a whole version-fragile block.
  - ast.parse the result so you never write code that doesn't compile.
  - Back up before writing.
  - Do the minimum. If a config knob or plugin seam exists, you're on the wrong ladder rung.
"""
from __future__ import annotations

import ast
import shutil
import time
from pathlib import Path

MARKER = "# [example_patch]"           # change me — must be unique across all modules
TARGET = "path/relative/to/runtime.py"  # change me

_ANCHOR = "some_stable_upstream_substring"
_REPLACEMENT = f"some_stable_upstream_substring  {MARKER}"


def apply(target_path: Path, *, dry_run: bool = False) -> str:
    src = target_path.read_text(encoding="utf-8")
    if _ANCHOR not in src:
        return "anchor-miss"
    patched = src.replace(_ANCHOR, _REPLACEMENT, 1)
    ast.parse(patched)  # drop this line if TARGET is not Python
    if dry_run:
        return "applied"
    backup = target_path.with_suffix(target_path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target_path, backup)
    target_path.write_text(patched, encoding="utf-8")
    return "applied"
