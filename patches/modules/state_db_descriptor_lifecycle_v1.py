#!/usr/bin/env python3
"""Bound state.db read handles and classify local descriptor exhaustion."""

from __future__ import annotations

import argparse
import ast
import shutil
import time
from pathlib import Path

MARKER = "HERMES_STATE_DB_DESCRIPTOR_LIFECYCLE_v1"


class PatchError(RuntimeError):
    pass


def _route_replay_marker_read_through_pool(source: str) -> str:
    """Keep the durable-drain replay lookup off the long-lived writer handle."""
    method = "def has_platform_message_id_for_session_key("
    if method not in source:
        return source
    if "def _read_ctx(" not in source:
        raise PatchError("native replay-marker pooled read context is unavailable")
    native_read = """        with self._read_ctx() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM sessions s "
                "JOIN messages m ON m.session_id = s.id "
                "WHERE s.session_key = ? AND m.platform_message_id = ? LIMIT 1",
                (session_key, platform_message_id),
            )
            return cursor.fetchone() is not None
"""
    replacement = native_read
    if replacement in source:
        return source
    direct = """        with self._lock:
            cursor = self._conn.execute(  # ty:ignore[unresolved-attribute]
                "SELECT 1 FROM sessions s "
                "JOIN messages m ON m.session_id = s.id "
                "WHERE s.session_key = ? AND m.platform_message_id = ? LIMIT 1",
                (session_key, platform_message_id),
            )
            return cursor.fetchone() is not None
"""
    return _replace_once(source, direct, replacement, "durable-drain replay marker pooled read")


def _native_refactored_pool_shape(
    state_source: str, readpool_source: str
) -> tuple[bool, bool]:
    """Recognize the 0.21 split read-pool contract without re-implementing it.

    The old pool lived entirely in ``hermes_state.py``.  The refactor owns the
    process and path permits in ``hermes_state_readpool.py`` while ``SessionDB``
    owns checkout, fallback, and teardown.  Both halves are required; a partial
    split is unsafe to treat as native coverage.
    """
    state_required = (
        "from hermes_state_readpool import _READ_POOL_MAX, _proc_fd_targets, _read_budget_for",
        "self._read_budget = _read_budget_for(self.db_path)",
        "self._read_budget.register(self)",
        "def _checkout_read_conn(",
        "def _read_ctx(",
        "def _close_read_conn(",
        "self._read_budget.release()",
        "self._read_conns_closed = True",
    )
    readpool_required = (
        "_READ_POOL_MAX =",
        "_READ_POOL_PROCESS_MAX =",
        "_process_read_permits = threading.BoundedSemaphore(",
        "class _PathReadBudget:",
        "def _read_budget_for(",
        "def acquire(self, requester",
        "def release(self)",
    )
    state_present = any(item in state_source for item in state_required[1:])
    readpool_present = any(item in readpool_source for item in readpool_required[2:])
    return (
        all(item in state_source for item in state_required)
        and all(item in readpool_source for item in readpool_required),
        state_present or readpool_present,
    )


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one anchor, found {count}")
    return source.replace(old, new, 1)


def patch_gateway_source(source: str) -> str:
    if MARKER in source:
        required = (
            '_store_db = getattr(self.session_store, "_db", None)',
            "_closed_db_ids: set[int] = set()",
        )
        if not all(item in source for item in required):
            raise PatchError("marked gateway/run.py is incomplete")
        return source

    # Current Hermes owns one recoverable AsyncSessionDB handle per resolved
    # profile path and deterministically sweeps every handle at shutdown. That
    # is stronger than the old single-root SessionStore alias in multiplexed
    # gateways, so preserve it and retain only Golden's local-resource error
    # classification in the conversation loop.
    native_handle_contract = (
        "def _open_session_db_for_active_scope(",
        "def close_all_session_db_handles(",
        "_session_db_handle_cache.close_all(_close)",
    )
    native_handle_opener = (
        "return AsyncSessionDB(SessionDB())" in source
        or "return AsyncSessionDB(acquire())" in source
    )
    if all(token in source for token in native_handle_contract) and native_handle_opener:
        return source

    source = _replace_once(
        source,
        """            from hermes_state import AsyncSessionDB, SessionDB
            self._session_db = AsyncSessionDB(SessionDB())
""",
        """            from hermes_state import AsyncSessionDB, SessionDB
            # HERMES_STATE_DB_DESCRIPTOR_LIFECYCLE_v1
            # SessionStore already owns the process-wide state.db handle. Reuse
            # it for agent recall instead of doubling writers and per-thread
            # WAL readers. Fall back only when SessionStore could not open it.
            _store_db = getattr(self.session_store, "_db", None)
            self._session_db = AsyncSessionDB(
                _store_db if _store_db is not None else SessionDB()
            )
""",
        "gateway shared SessionDB",
    )
    source = _replace_once(
        source,
        """            _self_db = getattr(self, "_session_db", None)
            _self_db = getattr(_self_db, "_db", _self_db)
            for _db in (_self_db, getattr(getattr(self, "session_store", None), "_db", None)):
                if _db is None or not hasattr(_db, "close"):
                    continue
                try:
                    _db.close()
""",
        """            _self_db = getattr(self, "_session_db", None)
            _self_db = getattr(_self_db, "_db", _self_db)
            _closed_db_ids: set[int] = set()
            for _db in (_self_db, getattr(getattr(self, "session_store", None), "_db", None)):
                if (
                    _db is None
                    or not hasattr(_db, "close")
                    or id(_db) in _closed_db_ids
                ):
                    continue
                _closed_db_ids.add(id(_db))
                try:
                    _db.close()
""",
        "gateway SessionDB close deduplication",
    )
    ast.parse(source)
    return source


def patch_turn_api_error_source(source: str) -> str:
    """Classify descriptor exhaustion at the refactored turn-error seam."""
    if MARKER in source:
        required = (
            "import errno",
            "errno.EMFILE",
            '"failure_reason": "local_resource_exhaustion"',
            '"local_runtime_error": True',
        )
        if not all(item in source for item in required):
            raise PatchError("marked agent/turn_api_error.py is incomplete")
        return source
    source = _replace_once(
        source,
        "from dataclasses import dataclass\nimport json\n",
        "from dataclasses import dataclass\nimport errno\nimport json\n",
        "turn-error errno import",
    )
    source = _replace_once(
        source,
        "    if _recovered:\n"
        "        return _verdict(\"continue\")\n\n"
        "    status_code = getattr(api_error, \"status_code\", None)\n",
        "    if _recovered:\n"
        "        return _verdict(\"continue\")\n\n"
        "    # HERMES_STATE_DB_DESCRIPTOR_LIFECYCLE_v1\n"
        "    # EMFILE/ENFILE are host resource failures, not provider failures.\n"
        "    # Retrying can repeat a paid request while descriptors remain full.\n"
        "    if (\n"
        "        isinstance(api_error, OSError)\n"
        "        and getattr(api_error, \"errno\", None) in {errno.EMFILE, errno.ENFILE}\n"
        "    ):\n"
        "        _local_summary = agent._summarize_api_error(api_error)\n"
        "        logger.error(\n"
        "            \"%sLocal runtime file-descriptor exhaustion; not retrying the model provider. %s\",\n"
        "            agent.log_prefix, _local_summary,\n"
        "        )\n"
        "        try:\n"
        "            agent._persist_session(messages, conversation_history)\n"
        "        except Exception as _persist_error:\n"
        "            logger.error(\n"
        "                \"%sCould not persist the interrupted turn during local descriptor exhaustion: %s\",\n"
        "                agent.log_prefix, _persist_error,\n"
        "            )\n"
        "        _local_response = (\n"
        "            \"The agent's local runtime hit a file-handle limit before it could complete this turn. \"\n"
        "            \"This was not a model-provider failure; the local gateway needs recovery.\"\n"
        "        )\n"
        "        return _verdict(\"return\", {\n"
        "            \"final_response\": _local_response, \"messages\": messages, \"api_calls\": api_call_count,\n"
        "            \"completed\": False, \"failed\": True, \"error\": _local_summary,\n"
        "            \"failure_reason\": \"local_resource_exhaustion\", \"local_runtime_error\": True,\n"
        "        })\n\n"
        "    status_code = getattr(api_error, \"status_code\", None)\n",
        "turn-error local exhaustion branch",
    )
    ast.parse(source)
    return source


def patch_state_db_descriptor_lifecycle_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    state_path = root / "hermes_state.py"
    readpool_path = root / "hermes_state_readpool.py"
    if not state_path.is_file():
        raise PatchError(f"required file missing: {state_path}")
    state_source = state_path.read_text(encoding="utf-8")
    if not readpool_path.is_file():
        raise PatchError(f"required file missing: {readpool_path}")
    native_complete, _ = _native_refactored_pool_shape(
        state_source, readpool_path.read_text(encoding="utf-8")
    )
    if not native_complete:
        raise PatchError("native refactored SessionDB descriptor contract is incomplete")
    error_path = root / "agent/turn_api_error.py"
    transforms = (
        (state_path, _route_replay_marker_read_through_pool),
        (root / "gateway/run.py", patch_gateway_source),
        (error_path, patch_turn_api_error_source),
    )
    originals: dict[Path, str] = {}
    updates: dict[Path, str] = {}
    for path, transform in transforms:
        if not path.is_file():
            raise PatchError(f"required file missing: {path}")
        originals[path] = path.read_text(encoding="utf-8")
        updates[path] = transform(originals[path])

    changed = [path for path in originals if originals[path] != updates[path]]
    if not changed:
        return False

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups: list[Path] = []
    try:
        for path in changed:
            backup = path.with_name(f"{path.name}.bak-{stamp}-state-db-descriptor-lifecycle-v1")
            shutil.copy2(path, backup)
            backups.append(backup)
        for path in changed:
            path.write_text(updates[path], encoding="utf-8")
    except Exception:
        for path, original in originals.items():
            if path in changed:
                path.write_text(original, encoding="utf-8")
        for backup in backups:
            backup.unlink(missing_ok=True)
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-dir", type=Path, required=True)
    args = parser.parse_args()
    changed = patch_state_db_descriptor_lifecycle_v1(args.hermes_dir)
    print("patched" if changed else "already-applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
