#!/usr/bin/env python3
"""Scope implicit session recall to the active Telegram topic.

Golden assembles from one exact upstream commit, so this patch intentionally
supports known pristine source and exact installed call/helper upgrades.
Unknown installed call shapes fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import ast
import shutil
from pathlib import Path

MARKER = "# [HERMES_SESSION_SEARCH_CURRENT_TOPIC_v2]"
IMPORT_ANCHOR = "from typing import Any, Dict, List, Optional, Union\n"
IMPORT_LINE = "from tools.golden_topic_recall import scoped_telegram_recall\n"
CALL_ANCHOR = "    # Browse shape: no query → recent sessions.\n"
NATIVE_CALL_ANCHOR = "    limit = _clamp_int(limit, 3, 1, 10)\n"
CALL_BLOCK = f"""    {MARKER}
    topic_result, query = scoped_telegram_recall(
        query=query,
        limit=limit,
        db=db,
        current_session_id=current_session_id,
        role_filter=role_filter,
        sort=sort,
        detail=detail,
    )
    if topic_result is not None:
        return topic_result

"""


_PRE_PARAMETER_HELPER_SHA256 = 'd8b3cfdb5442ace59d80542b13c98e0287cda998ee23e53e18c8db3e1112d95c'
_PRE_PARAMETER_CALL_BLOCK = CALL_BLOCK.replace(
    "        role_filter=role_filter,\n        sort=sort,\n        detail=detail,\n", "", 1)


def _guarded_call(block):
    return "    if not profile:\n" + "".join(
        "    " + line if line.strip() else line for line in block.splitlines(keepends=True))


def _patch_target(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        if source.count(MARKER) != 1 or source.count(IMPORT_LINE) != 1:
            raise RuntimeError("topic recall installed marker/import drift")
        current = _guarded_call(CALL_BLOCK)
        if source.count(current) == 1:
            return False
        previous = (_guarded_call(_PRE_PARAMETER_CALL_BLOCK), _PRE_PARAMETER_CALL_BLOCK)
        matches = [block for block in previous if source.count(block) == 1]
        if len(matches) != 1:
            raise RuntimeError("topic recall installed call shape drift")
        updated = source.replace(matches[0], current, 1)
        compile(updated, str(path), "exec")
        path.write_text(updated, encoding="utf-8")
        return True
    if IMPORT_LINE.strip() in source:
        raise RuntimeError("topic recall import exists without the v2 call marker")
    if source.count(IMPORT_ANCHOR) != 1:
        raise RuntimeError("session_search import anchor missing or ambiguous")
    native = NATIVE_CALL_ANCHOR in source
    call_anchor = NATIVE_CALL_ANCHOR if native else CALL_ANCHOR
    if source.count(call_anchor) != 1:
        raise RuntimeError("session_search browse anchor missing or ambiguous")

    source = source.replace(
        IMPORT_ANCHOR,
        f"{IMPORT_ANCHOR}{IMPORT_LINE}",
        1,
    ).replace(
        call_anchor,
        (call_anchor + _guarded_call(CALL_BLOCK))
        if native else f"{_guarded_call(CALL_BLOCK)}{CALL_ANCHOR}",
        1,
    )
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    return True



def _patch_lineage_tests(path: Path) -> bool:
    """Keep upstream lineage assertions using explicitly requested global recall."""
    if not path.exists():
        return False
    source = path.read_text(encoding="utf-8")
    updated = source
    for name, before, after in (
        ("test_session_reset_parent_discoverable_from_child", 'query="ibuprofen"', 'query="global: ibuprofen"'),
        ("test_title_match_reset_parent_not_dropped", 'query="Night ibuprofen plan"', 'query="global: Night ibuprofen plan"'),
        ("test_scroll_into_reset_parent_is_allowed", 'query="ibuprofen"', 'query="global: ibuprofen"'),
        ("test_reset_parent_appears_in_browse", 'session_search(db=db, current_session_id="s_today")', 'session_search(query="global:", db=db, current_session_id="s_today")'),
    ):
        tree = ast.parse(updated)
        matches = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]
        if not matches:
            continue
        if len(matches) != 1:
            raise RuntimeError(f"topic recall lineage test ambiguous: {name}")
        node = matches[0]
        lines = updated.splitlines(keepends=True)
        block = "".join(lines[node.lineno - 1:node.end_lineno])
        if before not in block:
            if after in block:
                continue
            raise RuntimeError(f"topic recall lineage test drift: {name}")
        block = block.replace(before, after, 1)
        updated = "".join(lines[:node.lineno - 1]) + block + "".join(lines[node.end_lineno:])
    if updated == source:
        return False
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8")
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

    runtime_helper = hermes_dir / "tools" / "golden_topic_recall.py"
    if runtime_helper.exists():
        existing = runtime_helper.read_bytes()
        if existing != payload.read_bytes() and hashlib.sha256(existing).hexdigest() != _PRE_PARAMETER_HELPER_SHA256:
            raise RuntimeError("topic recall installed helper drift")
    changed = _patch_target(target)
    changed = _patch_lineage_tests(hermes_dir / "tests/tools/test_session_search.py") or changed
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
