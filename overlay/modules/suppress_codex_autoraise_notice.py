#!/usr/bin/env python3
"""suppress_codex_autoraise_notice — a real, working patch module.

Kills a cosmetic startup notice at its source while keeping the behavior it announces.

The upstream runtime, on a large-context Codex lane, raises its compaction threshold and then
prints a one-time notice about it at startup, plus replays that notice to chat clients on the
first turn. The threshold raise is load-bearing (turning it off drops compaction to a fraction
of the window). The notice is pure noise. This module neutralizes the two emission sites and
leaves the threshold logic untouched.

This is also the reference for how a patch module is written. Copy _example_patch.py to start a
new one. The contract the engine expects:

    MARKER : str   a unique grep-able string this patch leaves behind (drives idempotency)
    TARGET : str   file to edit, relative to the runtime root
    apply(target_path, *, dry_run=False) -> "applied" | "anchor-miss"
"""
from __future__ import annotations

import ast
import shutil
import time
from pathlib import Path

MARKER = "# [suppress_codex_autoraise_notice]"
TARGET = "agent/agent_init.py"

# The two emission sites, exactly as they appear upstream, and what we replace them with.
# Anchors are the smallest stable substrings that uniquely identify each site. If an upstream
# version moves these lines, apply() returns "anchor-miss" and the rehearsal stage catches it.
_EDITS = [
    (
        "print(_build_codex_gpt55_autoraise_notice(_autoraise))",
        f"pass  {MARKER} startup notice suppressed (behavior kept)",
    ),
    (
        "agent._compression_warning = _build_codex_gpt55_autoraise_notice(_autoraise)",
        f"agent._compression_warning = None  {MARKER} client replay suppressed",
    ),
]


def apply(target_path: Path, *, dry_run: bool = False) -> str:
    src = target_path.read_text(encoding="utf-8")
    patched = src
    for anchor, replacement in _EDITS:
        if anchor not in patched:
            return "anchor-miss"
        patched = patched.replace(anchor, replacement, 1)

    # Never write code that does not parse.
    ast.parse(patched)

    if dry_run:
        return "applied"

    backup = target_path.with_suffix(target_path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target_path, backup)
    target_path.write_text(patched, encoding="utf-8")
    return "applied"
