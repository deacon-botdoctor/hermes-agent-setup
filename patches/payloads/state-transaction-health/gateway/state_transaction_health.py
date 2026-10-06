"""Gateway-owned state probes and durable admission after a store failure."""
from __future__ import annotations

import os
import json
import re
import sqlite3
import time
import sys
import inspect
import asyncio
from contextlib import contextmanager
from pathlib import Path

_failure: str | None = None
_owner_pid: int | None = None
_owner_home: Path | None = None
_started_at = time.time()
_PROBE_SQL_BUDGET_SECONDS = 5.0


def _protected_home(db_path: Path) -> Path | None:
    db_path = Path(db_path).expanduser().resolve()
    if db_path.name != "state.db":
        return None
    policy = db_path.parent / "state/service-ownership.json"
    try:
        contract = json.loads(policy.read_text())
    except FileNotFoundError:
        return None
    if contract != {"schema_version": 1, "mode": "gateway-only", "database": str(db_path)}:
        raise RuntimeError("invalid gateway-only state ownership contract")
    return db_path.parent


def claim_owner_if_required(home: Path) -> None:
    global _owner_pid, _owner_home
    protected = _protected_home(Path(home) / "state.db")
    if protected is None:
        return
    if _owner_pid is not None and _owner_pid != os.getpid():
        raise RuntimeError("forked process cannot inherit state ownership")
    import atexit
    from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock
    if not acquire_gateway_runtime_lock():
        raise RuntimeError("gateway-only state root already has an owner")
    _owner_pid, _owner_home = os.getpid(), protected
    atexit.register(release_gateway_runtime_lock)


def require_owner(db_path: Path) -> None:
    protected = _protected_home(db_path)
    if protected is None:
        return
    from gateway.status import owns_gateway_runtime_lock
    if _owner_pid != os.getpid() or _owner_home != protected or not owns_gateway_runtime_lock():
        raise RuntimeError("state.db belongs to the gateway; use its API or an offline snapshot")


def is_store_failure(error: Exception) -> bool:
    return isinstance(error, sqlite3.DatabaseError) or (
        isinstance(error, RuntimeError)
        and str(error) == "canonical session database is unavailable"
    )


def mark_failed(error: Exception) -> None:
    global _failure
    # Latch until a new process: a later successful handle lookup cannot erase
    # an observed transaction failure. Never publish query text or user data.
    _failure = type(error).__name__
    try:
        from gateway.status import write_runtime_status
        write_runtime_status(session_store={"status": "unavailable"})
    except Exception:
        pass  # A diagnostic write must never prevent durable admission.


async def retain_failed_admission(owner, event, session_key, error):
    mark_failed(error)
    if not (event.internal or owner._is_user_authorized_for_source(event.source)):
        return None, "unauthorized"
    # This existing private, fsynced file inbox is independent of state.db.
    # Its queued/claimed/completed states also prevent ambiguous tool replay.
    legacy_persist = getattr(owner, "_persist_drain_event_result", None)
    if legacy_persist is not None:
        return await legacy_persist(event, session_key, reason="session-store-unavailable")
    from gateway import drain_inbox
    owner._durable_setup()
    producer = owner._durable_producer_token
    return await asyncio.to_thread(
        drain_inbox.persist_event_result, event, session_key,
        reason="session-store-unavailable", path=owner._durable_inbox_path,
        producer_token=producer,
    )


def probe(owner, *, diagnostics=False) -> dict:
    """Use only the owning gateway's existing SessionDB connection."""
    result = {"status": "fail", "database_status": "fail", "diagnostics_complete": False,
              "pid": os.getpid(), "checked_at": time.time()}
    result["runtime_files"] = {
        name: str(Path(module.__file__).resolve())
        for name in ("gateway.run", "gateway.session", "hermes_state")
        if (module := sys.modules.get(name)) is not None and getattr(module, "__file__", None)
    }
    registry_module = sys.modules.get("tools.registry")
    registry = getattr(registry_module, "registry", None)
    entry = getattr(registry, "_tools", {}).get("session_search")
    handler = getattr(entry, "handler", None)
    try:
        sourcefile = inspect.getsourcefile(handler) if handler else None
        result["session_search_handler"] = str(Path(sourcefile).resolve()) if sourcefile else None
    except (TypeError, OSError):
        result["session_search_handler"] = None
    try:
        store = getattr(owner, "session_store", None)
        if store is None:
            raise RuntimeError("canonical session database is unavailable")
        db = store._db
        if db is None:
            raise RuntimeError("canonical session database is unavailable")
        with db._read_ctx() as connection, _sql_budget(connection):
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            counts = {}
            for table in ("sessions", "messages", "delivery_obligations"):
                if table in tables:
                    counts[table] = connection.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0]
            if not {"sessions", "messages"}.issubset(counts):
                raise sqlite3.DatabaseError("required state tables unavailable")
            if diagnostics:
                usage_columns = ("billing_provider", "source", "model", "input_tokens", "output_tokens",
                                 "reasoning_tokens", "api_call_count", "started_at", "title")
                session_columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
                if set(usage_columns).issubset(session_columns):
                    usage_rows = connection.execute(
                        "SELECT " + ",".join(usage_columns)
                        + " FROM sessions WHERE started_at >= ?", (time.time() - 7 * 86400,)
                    ).fetchall()
                    result["session_usage"] = [dict(zip(usage_columns, row)) for row in usage_rows]
                if "telegram_dm_topic_bindings" in tables:
                    result["topic_bindings"] = connection.execute(
                        "SELECT count(*) FROM telegram_dm_topic_bindings"
                    ).fetchone()[0]
                else:
                    result["topic_bindings"] = connection.execute(
                        "SELECT count(*) FROM sessions WHERE source='telegram' AND thread_id IS NOT NULL"
                    ).fetchone()[0]
                result["credential_tool_errors"] = _credential_tool_errors(connection)
                result["activity"] = _activity_counts(connection)
        result["counts"] = counts
        result["database_status"] = "fail" if _failure else "pass"
        result["diagnostics_complete"] = bool(diagnostics)
        result["status"] = result["database_status"]
        platforms = getattr(owner, "adapters", {})
        if any(getattr(platform, "value", platform) == "telegram" for platform in platforms):
            from gateway.telegram_transaction_ledger import health_since
            result["telegram"] = health_since(_started_at)
            if result["telegram"]["status"] != "pass":
                result["status"] = result["telegram"]["status"]
    except DiagnosticBudgetExceeded:
        result["diagnostics_complete"] = False
        result["error_type"] = "DiagnosticBudgetExceeded"
    except Exception as error:
        mark_failed(error)
    if _failure:
        result["database_status"] = "fail"
        result["error_type"] = _failure
    return result


class DiagnosticBudgetExceeded(RuntimeError):
    """Incomplete optional evidence is not a broken canonical store."""


@contextmanager
def _sql_budget(connection):
    # _read_ctx gives this probe exclusive use of the handle. Interrupt SQLite
    # itself: an HTTP timeout does not stop a worker already inside a query.
    deadline = time.monotonic() + _PROBE_SQL_BUDGET_SECONDS
    expired = False

    def progress():
        nonlocal expired
        expired = time.monotonic() >= deadline
        return int(expired)

    connection.set_progress_handler(progress, 1000)
    try:
        yield
        if progress():
            raise DiagnosticBudgetExceeded()
    except sqlite3.OperationalError as error:
        if expired and getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise DiagnosticBudgetExceeded() from error
        raise
    finally:
        connection.set_progress_handler(None, 0)


def _activity_counts(connection):
    cutoffs = tuple(time.time() - days * 86400 for days in (1, 7, 30))
    totals = connection.execute(
        "SELECT coalesce(sum(timestamp > ?),0), coalesce(sum(timestamp > ?),0), "
        "coalesce(sum(timestamp > ?),0) FROM messages WHERE timestamp > ?",
        (*cutoffs, cutoffs[-1]),
    ).fetchone()
    # Keep sessions outermost so the existing (session_id,timestamp) index
    # excludes CLI/cron history BEFORE reading message bodies for role.
    human = connection.execute(
        "SELECT coalesce(sum(m.timestamp > ?),0), coalesce(sum(m.timestamp > ?),0), "
        "coalesce(sum(m.timestamp > ?),0) FROM sessions s CROSS JOIN messages m "
        "ON m.session_id=s.id WHERE s.source IS NOT NULL "
        "AND s.source NOT IN ('cli','cron') AND m.timestamp > ? AND m.role='user'",
        (*cutoffs, cutoffs[-1]),
    ).fetchone()
    return {key: value for i, days in enumerate((1, 7, 30))
            for key, value in ((f"msgs_{days}d", totals[i]), (f"human_msgs_{days}d", human[i]))}


def _credential_tool_errors(connection, *, budget_seconds=3):
    # Sort narrow metadata through the existing covering index before touching
    # large message bodies. Preserve the original latest-12 *matching* rows,
    # including non-tool matches and exclusions; never substitute a sample zero.
    deadline = time.monotonic() + budget_seconds
    patterns = ("api key", "connected account", "connect your", "not authenticated",
                "authentication required", "no connected account", "credentials")
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(messages)")}
    index = " INDEXED BY idx_messages_session" if "idx_messages_session" in indexes else ""
    cursor = connection.execute(
        "SELECT rowid FROM messages" + index + " ORDER BY coalesce(timestamp,0) DESC, rowid DESC")
    matches = []
    try:
        while True:
            if time.monotonic() >= deadline:
                raise DiagnosticBudgetExceeded()
            ids = [row[0] for row in cursor.fetchmany(256)]
            if not ids:
                break
            where = " OR ".join("lower(content) LIKE ?" for _ in patterns)
            rows = connection.execute(
                "SELECT rowid,role,tool_name,content FROM messages WHERE rowid IN ("
                + ",".join("?" for _ in ids) + ") AND (" + where + ")",
                tuple(ids) + tuple(f"%{pattern}%" for pattern in patterns),
            ).fetchall()
            order = {rowid: rank for rank, rowid in enumerate(ids)}
            matches.extend(sorted(rows, key=lambda row: order[row[0]]))
            if time.monotonic() >= deadline:
                raise DiagnosticBudgetExceeded()
            if len(matches) >= 12:
                break
    finally:
        cursor.close()
    return sum(role == "tool" and _credential_error(tool, content)
               for _, role, tool, content in matches[:12])


def _credential_error(tool, content):
    if str(tool or "") in {"read_file", "search_files", "session_search", "skill_view", "web_extract"}:
        return False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    evidence = " ".join(str(payload.get(key) or "") for key in ("error", "stderr", "output"))
    failed = (bool(payload.get("error")) or payload.get("exit_code") not in (None, 0)
              or str(payload.get("status") or "").lower() in {"error", "fail", "failed"})
    return bool(failed and re.search(
        r"(no connected account|not authenticated|authentication required|"
        r"(?:invalid|missing|expired|unauthorized|forbidden)[^\n]{0,80}(?:api[ _-]?key|credential|token)|"
        r"(?:api[ _-]?key|credential|token)[^\n]{0,80}(?:invalid|missing|expired|unauthorized|forbidden))",
        evidence, re.I,
    ))
