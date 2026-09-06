# ruff: noqa: E501
"""Immutable transaction and slash-confirm receipt ledger for Telegram clients."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_CURRENT: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("telegram_transaction", default=None)
_PROGRESS_CLEANUP_ALLOWED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "telegram_progress_cleanup_allowed",
    default=False,
)
_PROGRESS_CLEANUP_STATE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "telegram_progress_cleanup_state",
    default=None,
)
_DELIVERY_FAILURE: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "telegram_delivery_failure",
    default=None,
)
_GENERIC_AGENT_ERROR = "sorry, i encountered an unexpected error"


def wal_reset_bug_fixed(version: tuple[int, ...] = sqlite3.sqlite_version_info) -> bool:
    current = tuple((list(version) + [0, 0, 0])[:3])
    return (
        current >= (3, 51, 3)
        or (3, 50, 7) <= current < (3, 51, 0)
        or (3, 44, 6) <= current < (3, 45, 0)
    )


def safe_journal_mode(version: tuple[int, ...] = sqlite3.sqlite_version_info) -> str:
    if _is_linux_platform():
        return "DELETE"
    return "WAL" if wal_reset_bug_fixed(version) else "DELETE"


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def require_safe_journal_mode(connection: sqlite3.Connection, *, allow_initialize: bool = False) -> None:
    effective = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).upper()
    expected = safe_journal_mode()
    # Existing DELETE is safe on every supported version. Existing WAL must
    # satisfy the current safety policy; refuse unsafe state without migrating it.
    if not allow_initialize and (effective == "DELETE" or effective == expected):
        return
    if effective != expected and allow_initialize:
        effective = str(connection.execute(f"PRAGMA journal_mode={expected}").fetchone()[0]).upper()
    if effective != expected:
        connection.close()
        raise sqlite3.DatabaseError(f"unsafe journal mode: expected {expected}, found {effective}")


def _path() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state" / "telegram-transactions.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    allow_initialize = not path.exists() or path.stat().st_size == 0
    db = sqlite3.connect(path, timeout=0.2)
    require_safe_journal_mode(db, allow_initialize=allow_initialize)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL, event_type TEXT NOT NULL,
          occurred_at REAL NOT NULL, run_id TEXT, inbound_update_id TEXT,
          outbound_message_id TEXT, chat_id TEXT, thread_id TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_receipt ON events(transaction_id) WHERE event_type='received';
        CREATE UNIQUE INDEX IF NOT EXISTS one_run_start ON events(transaction_id) WHERE event_type='run_started';
        CREATE UNIQUE INDEX IF NOT EXISTS one_model_finish ON events(transaction_id) WHERE event_type='model_finished';
        CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_run ON events(transaction_id) WHERE event_type IN ('run_finished','failed');
        CREATE UNIQUE INDEX IF NOT EXISTS one_acceptance ON events(transaction_id) WHERE event_type='telegram_accepted';
        CREATE TRIGGER IF NOT EXISTS events_immutable_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        CREATE TRIGGER IF NOT EXISTS events_immutable_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
    """)
    return db


def _insert(db: sqlite3.Connection, tx: str, kind: str, **values: Any) -> bool:
    payload = values.pop("detail", {})
    event_id = hashlib.sha256(f"{tx}:{kind}".encode()).hexdigest()
    occurred_at = values.get("occurred_at")
    if occurred_at is None:
        occurred_at = time.time()
    inserted = db.execute(
        "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            tx,
            kind,
            occurred_at,
            values.get("run_id"),
            values.get("inbound_update_id"),
            values.get("outbound_message_id"),
            values.get("chat_id"),
            values.get("thread_id"),
            json.dumps(payload, sort_keys=True, default=str),
        ),
    )
    return inserted.rowcount == 1


def _append(tx: str, kind: str, **values: Any) -> bool:
    try:
        with _connect() as db:
            inserted = _insert(db, tx, kind, **values)
        # A new run needs newly recorded boundaries. Other receipts remain
        # idempotent, including external delivery acceptance acknowledgements.
        return inserted or kind not in {"received", "run_started"}
    except (OSError, sqlite3.Error):
        return False


def begin(event: Any, agent_id: str) -> str | None:
    _CURRENT.set(None)
    _PROGRESS_CLEANUP_ALLOWED.set(False)
    _PROGRESS_CLEANUP_STATE.set(None)
    _DELIVERY_FAILURE.set(None)
    source = getattr(event, "source", None)
    platform = str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))).lower()
    update_id = getattr(event, "platform_update_id", None)
    if platform != "telegram" or update_id is None:
        return None
    tx = hashlib.sha256(f"telegram:{agent_id}:{update_id}".encode()).hexdigest()
    run_id = uuid.uuid4().hex
    try:
        with _connect() as db:
            if not _insert(
                db,
                tx,
                "received",
                inbound_update_id=str(update_id),
                chat_id=str(getattr(source, "chat_id", "")),
                thread_id=str(getattr(source, "thread_id", "") or ""),
                detail={
                    "inbound_message_id": str(getattr(event, "message_id", "") or ""),
                    "sender_user_id": str(getattr(source, "user_id", "") or ""),
                },
            ) or not _insert(db, tx, "run_started", run_id=run_id):
                # Neither boundary may survive a duplicate or failed admission.
                db.rollback()
                return None
    except (OSError, sqlite3.Error):
        return None
    _CURRENT.set((tx, run_id))
    try:
        owner = asyncio.current_task()
    except RuntimeError:
        owner = None
    _PROGRESS_CLEANUP_STATE.set({"decision": None, "future": None, "owner": owner})
    return tx


def finish(*, failed: bool = False, error: Any = None) -> None:
    current = _CURRENT.get()
    if current:
        delivery_error = _DELIVERY_FAILURE.get()
        if not failed and delivery_error is not None:
            failed = True
            error = delivery_error
        tx, run_id = current
        if failed:
            # A processing hook can fail after the immutable terminal success
            # receipt was written. Keep that later failure monotonic instead of
            # letting the terminal uniqueness constraint hide it.
            _append(
                tx,
                "run_failure_observed",
                run_id=run_id,
                detail={"error": str(error)[:1000]} if error else {},
            )
        _append(
            tx,
            "failed" if failed else "run_finished",
            run_id=run_id,
            detail={"error": str(error)[:1000]} if error else {},
        )


def is_error_envelope(response: Any) -> bool:
    """Recognize Hermes's generic agent-failure response without storing text."""
    return str(response or "").strip().lower().startswith(_GENERIC_AGENT_ERROR)


def model_finished() -> None:
    """Record that the current run finished producing its response."""
    current = _CURRENT.get()
    if current:
        tx, run_id = current
        _append(tx, "model_finished", run_id=run_id)


def accepted(message_id: Any) -> None:
    current = _CURRENT.get()
    if current and message_id not in (None, "", "__no_edit__"):
        tx, run_id = current
        _append(tx, "telegram_accepted", run_id=run_id, outbound_message_id=str(message_id))
        # Delivery attempts are ordered: a later accepted result is the native
        # fallback/primary outcome and supersedes an earlier attempt failure.
        _DELIVERY_FAILURE.set(None)


def external_accepted(
    *,
    chat_id: Any,
    thread_id: Any,
    message_id: Any,
    source: str,
    occurred_at: Any,
) -> bool:
    """Record a Telegram send completed by a gateway-owned tool.

    This reuses the immutable transaction ledger so downstream closeout checks
    can prove the provider accepted the exact message. It is intentionally not
    a second delivery system and does not send or retry anything.
    """
    if chat_id in (None, "") or message_id in (None, "", "__no_edit__"):
        return False
    try:
        accepted_at = float(occurred_at)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(accepted_at) or accepted_at <= 0:
        return False
    normalized_thread = "" if thread_id in (None, "", "None") else str(thread_id)
    tx = hashlib.sha256(
        f"external:telegram:{chat_id}:{normalized_thread}:{message_id}".encode()
    ).hexdigest()
    return _append(
        tx,
        "external_telegram_accepted",
        outbound_message_id=str(message_id),
        chat_id=str(chat_id),
        thread_id=normalized_thread,
        occurred_at=accepted_at,
        detail={"source": str(source or "gateway_tool")[:120]},
    )


def delivery_failed(error: Any = None) -> None:
    """Hold an attempt failure until the native turn reaches its terminal boundary."""
    current = _CURRENT.get()
    if current:
        tx, run_id = current
        _DELIVERY_FAILURE.set(error or "delivery failed")
        _append(
            tx,
            "delivery_failure_observed",
            run_id=run_id,
            detail={"error": str(error)[:1000]} if error else {},
        )


def _resolve_progress_cleanup(state: dict[str, Any] | None, allowed: bool) -> None:
    if state is None:
        return
    if state["decision"] is None:
        state["decision"] = allowed
    future = state["future"]
    if future is not None and not future.done():
        future.set_result(bool(state["decision"]))


def defer_progress_cleanup() -> bool:
    state = _PROGRESS_CLEANUP_STATE.get()
    if _CURRENT.get() is None or state is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if state["future"] is None:
        state["future"] = loop.create_future()
    owner = state.pop("owner", None)
    if owner is not None:
        owner.add_done_callback(
            lambda _task, pending=state: _resolve_progress_cleanup(pending, False)
        )
    return True


async def wait_for_progress_cleanup() -> bool:
    state = _PROGRESS_CLEANUP_STATE.get()
    if state is None:
        return False
    decision = state["decision"]
    if decision is not None:
        return bool(decision)
    future = state["future"]
    if future is None:
        return False
    return bool(await asyncio.shield(future))


def abort_progress_cleanup() -> None:
    state = _PROGRESS_CLEANUP_STATE.get()
    _CURRENT.set(None)
    _PROGRESS_CLEANUP_ALLOWED.set(False)
    _resolve_progress_cleanup(state, False)


def finalize_progress_cleanup() -> bool:
    allowed = False
    current = _CURRENT.get()
    state = _PROGRESS_CLEANUP_STATE.get()
    if current:
        tx, run_id = current
        try:
            with _connect() as db:
                kinds = {
                    str(row[0])
                    for row in db.execute(
                        "SELECT event_type FROM events WHERE transaction_id=? AND run_id=?",
                        (tx, run_id),
                    )
                }
            allowed = (
                {"model_finished", "telegram_accepted", "run_finished"} <= kinds
                and not {"failed", "run_failure_observed"} & kinds
            )
        except (OSError, sqlite3.Error):
            allowed = False
    _CURRENT.set(None)
    _PROGRESS_CLEANUP_ALLOWED.set(allowed)
    _resolve_progress_cleanup(state, allowed)
    return allowed


def progress_cleanup_allowed() -> bool:
    return _PROGRESS_CLEANUP_ALLOWED.get()


def _session_snapshot(session_store: Any, session_key: str) -> dict[str, Any] | None:
    if session_store is None or not session_key:
        return None
    try:
        session_id = session_store.peek_session_id(session_key)
    except Exception:
        return None
    if session_id in (None, ""):
        return None
    return {"session_id": str(session_id)}


def slash_confirm_requested(
    message_id: Any,
    session_key: str,
    confirm_id: str,
    session_store: Any,
) -> bool:
    """Bind a native confirmation prompt to its current session generation.

    Return false without appending when the transaction or exact session ID is
    unavailable; sending the confirmation remains independent of the receipt.
    """
    current = _CURRENT.get()
    snapshot = _session_snapshot(session_store, session_key)
    if not current or message_id in (None, "") or snapshot is None:
        return False
    tx, run_id = current
    return _append(
        tx,
        "slash_confirm_requested",
        run_id=run_id,
        outbound_message_id=str(message_id),
        detail={
            "confirm_id_sha256": hashlib.sha256(str(confirm_id).encode()).hexdigest(),
            **snapshot,
        },
    )


def slash_confirm_resolved(
    message_id: Any,
    session_key: str,
    confirm_id: str,
    action: str,
    callback_data: str,
    *,
    session_store: Any,
    update_id: Any = None,
    chat_id: Any = None,
    thread_id: Any = None,
    sender_user_id: Any = None,
) -> bool:
    """Record the session generation observed after one exact callback resolves.

    The receipt fails closed unless one request matches the callback's chat,
    message, and hashed confirmation ID and the current session ID is available.
    """
    if action not in {"once", "always", "cancel"} or message_id in (None, ""):
        return False
    confirm_sha256 = hashlib.sha256(str(confirm_id).encode()).hexdigest()
    try:
        with _connect() as db:
            rows = db.execute(
                """SELECT requested.transaction_id, requested.detail_json
                   FROM events AS requested
                   JOIN events AS received
                     ON received.transaction_id=requested.transaction_id
                    AND received.event_type='received'
                   WHERE requested.event_type='slash_confirm_requested'
                     AND requested.outbound_message_id=?
                     AND received.chat_id=?
                   ORDER BY requested.occurred_at DESC""",
                (str(message_id), str(chat_id)),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return False
    matches = []
    for tx, detail_json in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except json.JSONDecodeError:
            continue
        if detail.get("confirm_id_sha256") == confirm_sha256:
            matches.append((tx, detail))
    snapshot = _session_snapshot(session_store, session_key)
    if len(matches) != 1 or snapshot is None:
        return False
    tx, requested = matches[0]
    return _append(
        tx,
        "slash_confirm_resolved",
        inbound_update_id=str(update_id) if update_id is not None else None,
        outbound_message_id=str(message_id),
        chat_id=str(chat_id) if chat_id is not None else None,
        thread_id=str(thread_id) if thread_id is not None else None,
        detail={
            "action": action,
            "callback_data_sha256": hashlib.sha256(str(callback_data).encode()).hexdigest(),
            "confirm_id_sha256": confirm_sha256,
            "sender_user_id": str(sender_user_id) if sender_user_id is not None else "",
            "old_session_id": requested.get("session_id"),
            "new_session_id": snapshot["session_id"],
        },
    )


def clear() -> None:
    abort_progress_cleanup()


def classify(transaction_id: str) -> dict[str, Any]:
    with _connect() as db:
        db.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM events WHERE transaction_id=? ORDER BY occurred_at,event_id", (transaction_id,)
            )
        ]
    kinds = {row["event_type"] for row in rows}
    status = (
        "Failed"
        if {"failed", "run_failure_observed"} & kinds
        else "Replied"
        if {"telegram_accepted", "external_telegram_accepted"} & kinds
        else "Pending"
    )
    return {"transaction_id": transaction_id, "status": status, "events": rows}
