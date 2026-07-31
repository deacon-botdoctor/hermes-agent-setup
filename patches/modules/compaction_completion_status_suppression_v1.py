#!/usr/bin/env python3
"""Suppress the transcript-visible terminal compaction status.

Compaction start remains available to supported status surfaces.  The terminal
``compacted`` callback is just a human-readable completion sentence and is
rendered as a transcript item by some managed clients, so it must not be sent.
"""
from __future__ import annotations

import argparse
import ast
import shutil
import time
from pathlib import Path


MARKER = "HERMES_SUPPRESS_COMPACTION_COMPLETION_STATUS_v1"

HELPER_OLD = '''def _emit_compaction_done(agent: Any) -> None:
    """Emit the structured terminal edge for a started compaction."""
    status_callback = getattr(agent, "status_callback", None)
    if not status_callback:
        return
    try:
        status_callback("compacted", COMPACTION_DONE_STATUS)
    except Exception:
        logger.debug("status_callback error in compaction completion", exc_info=True)
'''

HELPER_NEW = '''def _emit_compaction_done(agent: Any) -> None:
    """Keep compaction completion internal; never create a transcript event."""
    # HERMES_SUPPRESS_COMPACTION_COMPLETION_STATUS_v1
    return
'''


def patch(path: Path, *, dry_run: bool = False) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return False
    if HELPER_OLD not in source:
        if "def _emit_compaction_done" not in source:
            return False
        raise RuntimeError("compaction completion helper anchor missing")

    patched = source.replace(HELPER_OLD, HELPER_NEW, 1)
    if 'status_callback("compacted", COMPACTION_DONE_STATUS)' in patched:
        raise RuntimeError("compaction completion status remained after patch")
    ast.parse(patched)
    if dry_run:
        return True

    backup = path.with_suffix(
        path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}-pre-compaction-completion-status-v1"
    )
    shutil.copy2(path, backup)
    path.write_text(patched, encoding="utf-8")
    return True


def patch_compaction_completion_status_suppression_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "agent" / "conversation_compression.py"
    if not target.exists():
        target = Path(hermes_dir) / "hermes-agent" / "agent" / "conversation_compression.py"
    return patch(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        changed = patch(Path(args.target), dry_run=args.dry_run)
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    print("DRY_RUN OK" if args.dry_run else ("OK: patched" if changed else "OK: already patched"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
