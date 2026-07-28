#!/usr/bin/env python3
"""Scope implicit session recall to the active Telegram topic.

Golden assembles from one exact upstream commit, so this patch intentionally
supports only that pristine source shape. Historical in-place upgrade logic
belongs in rollback artifacts, not in the runtime overlay.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

MARKER = "# [HERMES_SESSION_SEARCH_CURRENT_TOPIC_v2]"
IMPORT_ANCHOR = "from typing import Any, Dict, List, Optional, Union\n"
IMPORT_LINE = "from tools.golden_topic_recall import scoped_telegram_recall\n"
CALL_ANCHOR = "    # Browse shape: no query → recent sessions.\n"
CALL_BLOCK = f"""    {MARKER}
    topic_result, query = scoped_telegram_recall(
        query=query,
        limit=limit,
        db=db,
        current_session_id=current_session_id,
    )
    if topic_result is not None:
        return topic_result

"""


def _patch_target(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return False
    if IMPORT_LINE.strip() in source:
        raise RuntimeError("topic recall import exists without the v2 call marker")
    if source.count(IMPORT_ANCHOR) != 1:
        raise RuntimeError("session_search import anchor missing or ambiguous")
    if source.count(CALL_ANCHOR) != 1:
        raise RuntimeError("session_search browse anchor missing or ambiguous")

    source = source.replace(
        IMPORT_ANCHOR,
        f"{IMPORT_ANCHOR}{IMPORT_LINE}",
        1,
    ).replace(
        CALL_ANCHOR,
        f"{CALL_BLOCK}{CALL_ANCHOR}",
        1,
    )
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    return True


def patch_session_search_current_topic_v1(hermes_dir: Path) -> bool:
    hermes_dir = Path(hermes_dir)
    target = hermes_dir / "tools" / "session_search_tool.py"
    if not target.exists():
        return False

    payload = (
        Path(__file__).resolve().parent
        / "payloads"
        / "session-search-current-topic-v2"
        / "golden_topic_recall.py"
    )
    if not payload.exists():
        raise RuntimeError(f"topic recall payload missing: {payload}")

    changed = _patch_target(target)
    runtime_helper = hermes_dir / "tools" / "golden_topic_recall.py"
    helper_changed = (
        not runtime_helper.exists()
        or runtime_helper.read_bytes() != payload.read_bytes()
    )
    if helper_changed:
        runtime_helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(payload, runtime_helper)
    return changed or helper_changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    changed = patch_session_search_current_topic_v1(Path(args.target))
    print("OK: patched" if changed else "ALREADY_PATCHED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
