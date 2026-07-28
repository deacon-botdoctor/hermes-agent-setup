# ruff: noqa: E501
"""Immutable transaction and slash-confirm receipt ledger for Telegram clients."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

_CURRENT: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("telegram_transaction", default=None)
_GENERIC_AGENT_ERROR = "sorry, i encountered an unexpected error"


def _path() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state" / "telegram-transactions.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=0.2)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL, event_type TEXT NOT NULL,
          occurred_at REAL NOT NULL, run_id TEXT, inbound_update_id TEXT,
          outbound_message_id TEXT, chat_id TEXT, thread_id TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_receipt ON events(transaction_id) WHERE event_type='received';
        CREATE UNIQUE INDEX IF NOT EXISTS one_run_start ON events(transaction_id) WHERE event_type='run_started';
        CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_run ON events(transaction_id) WHERE event_type IN ('run_finished','failed');
        CREATE UNIQUE INDEX IF NOT EXISTS one_acceptance ON events(transaction_id) WHERE event_type='telegram_accepted';
        CREATE TRIGGER IF NOT EXISTS events_immutable_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
        CREATE TRIGGER IF NOT EXISTS events_immutable_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
    """)
    return db


def _append(tx: str, kind: str, **values: Any) -> bool:
    payload = values.pop("detail", {})
    event_id = hashlib.sha256(f"{tx}:{kind}".encode()).hexdigest()
    try:
        with _connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    tx,
                    kind,
                    time.time(),
                    values.get("run_id"),
                    values.get("inbound_update_id"),
                    values.get("outbound_message_id"),
                    values.get("chat_id"),
                    values.get("thread_id"),
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )
        return True
    except (OSError, sqlite3.Error):
        return False


def begin(event: Any, agent_id: str) -> str | None:
    _CURRENT.set(None)
    source = getattr(event, "source", None)
    platform = str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))).lower()
    update_id = getattr(event, "platform_update_id", None)
    if platform != "telegram" or update_id is None:
        return None
    tx = hashlib.sha256(f"telegram:{agent_id}:{update_id}".encode()).hexdigest()
    if not _append(
        tx,
        "received",
        inbound_update_id=str(update_id),
        chat_id=str(getattr(source, "chat_id", "")),
        thread_id=str(getattr(source, "thread_id", "") or ""),
        detail={
            "inbound_message_id": str(getattr(event, "message_id", "") or ""),
            "sender_user_id": str(getattr(source, "user_id", "") or ""),
        },
    ):
        return None
    run_id = uuid.uuid4().hex
    if not _append(tx, "run_started", run_id=run_id):
        return None
    _CURRENT.set((tx, run_id))
    return tx


def finish(*, failed: bool = False, error: Any = None) -> None:
    current = _CURRENT.get()
    if current:
        tx, run_id = current
        _append(
            tx,
            "failed" if failed else "run_finished",
            run_id=run_id,
            detail={"error": str(error)[:1000]} if error else {},
        )


def is_error_envelope(response: Any) -> bool:
    """Recognize Hermes's generic agent-failure response without storing text."""
    return str(response or "").strip().lower().startswith(_GENERIC_AGENT_ERROR)


def accepted(message_id: Any) -> None:
    current = _CURRENT.get()
    if current and message_id not in (None, "", "__no_edit__"):
        tx, run_id = current
        _append(tx, "telegram_accepted", run_id=run_id, outbound_message_id=str(message_id))


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
    _CURRENT.set(None)


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
    status = "Failed" if "failed" in kinds else "Replied" if "telegram_accepted" in kinds else "Pending"
    return {"transaction_id": transaction_id, "status": status, "events": rows}
