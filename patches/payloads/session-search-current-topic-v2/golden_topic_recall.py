"""Owner-scoped Telegram transcript recall for the native session_search tool."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BROAD_PREFIXES = ("global:", "all:", "broad:", "cross-topic:", "cross topic:")
_LOW_SIGNAL = re.compile(
    r"^\s*(?:\?|ok|okay|yes|yep|sure|go|do it|continue|resume|proceed|thanks|thank you)"
    r"\s*[.!?]*\s*\Z",
    re.I,
)
_STOP_WORDS = {
    "the",
    "and",
    "for",
    "that",
    "this",
    "with",
    "what",
    "when",
    "where",
    "about",
}
_SESSION_KEY = re.compile(
    r"agent:[^:]+:telegram:(?:group|forum):(-?\d+):([^:]+)$"
)


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "").strip()
    except Exception:
        return ""


def _topic_tuple(source: Any, chat_id: Any, thread_id: Any, session_key: Any):
    source = str(source or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    thread_id = str(thread_id or "").strip()
    if (not chat_id or not thread_id) and session_key:
        match = _SESSION_KEY.search(str(session_key).strip())
        if match:
            source = "telegram"
            chat_id, thread_id = match.groups()
    if source != "telegram" or not chat_id or not thread_id:
        return None
    return f"telegram:{chat_id}:{thread_id}", chat_id, thread_id


def _session_row(db, session_id: str) -> dict:
    if not db or not session_id:
        return {}
    try:
        row = db.get_session(session_id) or {}
    except Exception:
        row = {}
    if row:
        return row
    try:
        from hermes_state import SessionDB

        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        return SessionDB(
            db_path=home / "state.db",
            read_only=True,
        ).get_session(session_id) or {}
    except Exception:
        return {}


def _owner(db, session_id: str):
    topic = _topic_tuple(
        _session_env("HERMES_SESSION_PLATFORM"),
        _session_env("HERMES_SESSION_CHAT_ID"),
        _session_env("HERMES_SESSION_THREAD_ID"),
        _session_env("HERMES_SESSION_KEY"),
    )
    row = _session_row(db, session_id)
    if not topic:
        topic = _topic_tuple(
            row.get("source") or row.get("platform"),
            row.get("chat_id"),
            row.get("thread_id"),
            row.get("session_key"),
        )
    source = str(
        row.get("source")
        or row.get("platform")
        or _session_env("HERMES_SESSION_PLATFORM")
        or ""
    ).strip().lower()
    return topic, source == "telegram", row


def _started_at(db, session_id: str, row: dict):
    try:
        messages = db.get_messages(session_id) if db and session_id else []
    except Exception:
        messages = []
    starts = [
        message.get("timestamp")
        for message in messages or []
        if isinstance(message, dict) and message.get("timestamp") is not None
    ]
    value = min(starts) if starts else row.get("started_at")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    return str(value)


def _split_query(query: Any):
    raw = str(query or "").strip()
    lowered = raw.lower()
    for prefix in _BROAD_PREFIXES:
        if lowered.startswith(prefix):
            return True, raw[len(prefix) :].strip()
    return False, raw


def _terms(query: str):
    result = []
    for term in re.findall(r"[A-Za-z0-9_./:-]{2,}", query):
        lowered = term.lower()
        if lowered not in _STOP_WORDS and lowered not in result:
            result.append(lowered)
    return result[:8]


def _empty(mode: str, query: str, topic=None):
    response = {
        "success": True,
        "mode": mode,
        "query": query,
        "results": [],
        "count": 0,
    }
    if topic:
        response.update(
            topic_key=topic[0],
            chat_id=topic[1],
            thread_id=topic[2],
        )
    if mode == "current_topic_unavailable":
        response["message"] = (
            "Current Telegram topic metadata was unavailable; global search was "
            "blocked to preserve topic isolation. Prefix with 'global:' for an "
            "explicit cross-topic search."
        )
    elif mode == "current_topic_low_signal_guard":
        response["message"] = (
            "Low-signal follow-up stayed inside the current Telegram topic. No "
            "local antecedent was found; ask a specific question or prefix with "
            "'global:' for cross-topic search."
        )
    else:
        response["message"] = (
            "No matching messages found in the current Telegram topic. Prefix "
            "with 'global:' to search across all sessions/topics."
        )
    return json.dumps(response, ensure_ascii=False)


def _search(topic, query: str, limit: int, min_timestamp, roles, sort):
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    db_path = home / "data" / "telegram-transcript.db"
    if not db_path.exists():
        return _empty(
            "current_topic_low_signal_guard" if _LOW_SIGNAL.match(query) else "current_topic",
            query,
            topic,
        )

    limit = max(1, min(int(limit or 8), 20))
    filters = ["chat_id = ?", "role IN (" + ",".join("?" for _ in roles) + ")"]
    params: list[Any] = [topic[0], *roles]
    if min_timestamp:
        filters.append("timestamp >= ?")
        params.append(min_timestamp)
    terms = _terms(query)
    if terms:
        filters.extend("LOWER(text) LIKE ?" for _ in terms)
        params.extend(f"%{term}%" for term in terms)
    params.append(max(limit * (3 if terms else 2), limit))
    direction = "ASC" if sort == "oldest" else "DESC"
    sql = (
        "SELECT timestamp, sender_name, role, text FROM telegram_messages "
        f"WHERE {' AND '.join(filters)} ORDER BY timestamp {direction} LIMIT ?"
    )
    try:
        with sqlite3.connect(str(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, params).fetchall()
    except (sqlite3.Error, OSError):
        rows = []
    if not rows:
        mode = (
            "current_topic_low_signal_guard"
            if _LOW_SIGNAL.match(query)
            else "current_topic"
        )
        return _empty(mode, query, topic)

    results = []
    for row in rows[:limit]:
        text = str(row["text"] or "")
        if len(text) > 700:
            text = f"{text[:350]}\n…[truncated]…\n{text[-250:]}"
        results.append(
            {
                "timestamp": row["timestamp"],
                "sender": row["sender_name"] or row["role"],
                "role": row["role"],
                "text": text,
            }
        )
    return json.dumps(
        {
            "success": True,
            "mode": "current_topic",
            "query": query,
            "topic_key": topic[0],
            "chat_id": topic[1],
            "thread_id": topic[2],
            "results": results,
            "count": len(results),
            "message": (
                "Searched only the current Telegram topic. Prefix with 'global:' "
                "to search across all sessions/topics."
            ),
        },
        ensure_ascii=False,
    )


def scoped_telegram_recall(*, query, limit, db, current_session_id, role_filter=None, sort=None, detail="adaptive"):
    """Return ``(response, query)``; response is None for native/global paths."""
    broad, query = _split_query(query)
    if broad:
        return None, query

    session_id = str(
        current_session_id or _session_env("HERMES_SESSION_ID")
    ).strip()
    topic, is_telegram, row = _owner(db, session_id)
    if not topic:
        if is_telegram:
            return _empty("current_topic_unavailable", query), query
        return None, query
    if isinstance(detail, str) and detail.strip().lower() == "full":
        return json.dumps({
            "success": False,
            "mode": "current_topic_unsupported_detail",
            "query": query,
            "topic_key": topic[0], "chat_id": topic[1], "thread_id": topic[2],
            "results": [], "count": 0,
            "message": (
                "Full session hydration is unavailable for current-topic transcript recall. "
                "Use detail='adaptive' for scoped snippets, or explicitly prefix the query "
                "with 'global:' for native session hydration across topics."
            ),
        }, ensure_ascii=False), query
    roles = ([role.strip() for role in role_filter.split(",") if role.strip()]
             if isinstance(role_filter, str) else []) or ["user", "assistant"]
    sort_norm = sort.strip().lower() if isinstance(sort, str) else None
    return _search(topic, query, limit, _started_at(db, session_id, row), roles, sort_norm), query
