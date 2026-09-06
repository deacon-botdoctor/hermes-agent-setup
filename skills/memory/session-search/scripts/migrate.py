#!/usr/bin/env python3
"""
session-search migrate.py -- Backfill indexer for agent session logs.

Scans known Hermes, Factory Droid, and legacy session-log locations and builds
an SQLite FTS5 index for full-text search across past conversations.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_PATTERNS = [
    "~/.factory/sessions/**/*.jsonl",
    "~/.factory/sessions/**/*.jsonl.reset.*",
    "~/.factory/sessions/**/*.jsonl.deleted.*",
    "~/.hermes/agents/*/sessions/**/*.jsonl",
    "~/.hermes/sessions/**/*.jsonl",
    "~/.openclaw/agents/*/sessions/**/*.jsonl",  # legacy compatibility
]

DB_PATH = os.path.expanduser(
    os.environ.get("SESSION_SEARCH_DB", "~/.hermes/state/session-search.sqlite")
)
QUIET = "--quiet" in sys.argv


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
    if not allow_initialize and (effective == "DELETE" or effective == expected):
        return
    if effective != expected and allow_initialize:
        effective = str(connection.execute(f"PRAGMA journal_mode={expected}").fetchone()[0]).upper()
    if effective != expected:
        connection.close()
        raise sqlite3.DatabaseError(f"unsafe journal mode: expected {expected}, found {effective}")


def log(msg: str) -> None:
    if not QUIET:
        print(msg)


def session_patterns() -> list[str]:
    raw = os.environ.get("SESSION_SEARCH_PATTERNS")
    if raw:
        return [part for part in raw.split(os.pathsep) if part]
    return DEFAULT_PATTERNS


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            agent_id TEXT,
            platform TEXT,
            started_at REAL,
            ended_at REAL,
            message_count INTEGER
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            role UNINDEXED,
            session_id UNINDEXED,
            agent_id UNINDEXED,
            created_at UNINDEXED
        );

        CREATE TABLE IF NOT EXISTS indexed_files (
            path TEXT PRIMARY KEY,
            indexed_at REAL,
            message_count INTEGER
        );
        """
    )
    conn.commit()


def find_session_files() -> list[str]:
    files: set[str] = set()
    for pattern in session_patterns():
        files.update(glob.glob(os.path.expanduser(pattern), recursive=True))
    return sorted(files)


def extract_agent_id(filepath: str) -> str:
    parts = Path(filepath).parts
    if "agents" in parts:
        idx = parts.index("agents")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if "sessions" in parts:
        idx = parts.index("sessions")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "unknown"


def parse_timestamp(ts_str: object) -> float:
    if not ts_str:
        return 0.0
    if isinstance(ts_str, (int, float)):
        return float(ts_str)
    try:
        from datetime import datetime, timezone

        text = str(ts_str).rstrip("Z")
        if "." in text:
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f")
        else:
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def extract_text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block.strip())
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(str(text).strip())
            elif block.get("type") in {"toolCall", "tool_use", "tool_result"}:
                name = block.get("name") or block.get("tool_name") or block.get("id") or "tool"
                tool_input = block.get("input") or block.get("content") or ""
                if isinstance(tool_input, dict):
                    tool_input = json.dumps(tool_input, ensure_ascii=False)
                parts.append(f"[tool: {name}] {tool_input}")
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return ""


def normalize_message(obj: dict) -> tuple[str, str, float, str | None]:
    obj_type = obj.get("type", "")
    timestamp = parse_timestamp(obj.get("timestamp") or obj.get("created_at"))
    session_id = obj.get("session_id") or obj.get("sessionId")

    if obj_type == "message" and isinstance(obj.get("message"), dict):
        msg = obj["message"]
        return str(msg.get("role", "")), extract_text_content(msg.get("content", "")), timestamp, session_id
    if "role" in obj and "content" in obj:
        return str(obj.get("role", "")), extract_text_content(obj.get("content", "")), timestamp, session_id
    if obj_type in {"user", "assistant", "tool", "toolResult"}:
        return obj_type, extract_text_content(obj.get("content", obj.get("text", ""))), timestamp, session_id
    return "", "", timestamp, session_id


def index_session_file(conn: sqlite3.Connection, filepath: str) -> tuple[str | None, int]:
    agent_id = extract_agent_id(filepath)
    session_id: str | None = None
    platform = None
    started_at = 0.0
    ended_at = 0.0
    messages: list[tuple[str, str, float]] = []

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") == "session":
                    session_id = obj.get("id", "") or session_id
                    platform = obj.get("platform", platform)
                    started_at = parse_timestamp(obj.get("timestamp")) or started_at
                    continue

                role, content, timestamp, row_session_id = normalize_message(obj)
                if row_session_id and not session_id:
                    session_id = row_session_id
                if role == "system" or not content or len(content) < 2:
                    continue
                ended_at = max(ended_at, timestamp)
                messages.append((content, role, timestamp))
    except OSError as exc:
        log(f"  WARN: Could not read {filepath}: {exc}")
        return None, 0

    if not session_id:
        session_id = os.path.basename(filepath).split(".jsonl")[0]
    if not messages:
        return session_id, 0

    conn.execute(
        """INSERT OR REPLACE INTO sessions
           (id, agent_id, platform, started_at, ended_at, message_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, agent_id, platform, started_at, ended_at, len(messages)),
    )
    for content, role, ts in messages:
        conn.execute(
            """INSERT INTO messages_fts
               (content, role, session_id, agent_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (content, role, session_id, agent_id, str(ts)),
        )
    return session_id, len(messages)


def main() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    allow_initialize = not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0
    conn = sqlite3.connect(DB_PATH)
    require_safe_journal_mode(conn, allow_initialize=allow_initialize)
    init_db(conn)

    already_indexed = {row[0] for row in conn.execute("SELECT path FROM indexed_files")}
    all_files = find_session_files()
    new_files = [f for f in all_files if f not in already_indexed]

    log(f"Session search index: {DB_PATH}")
    log(f"Found {len(all_files)} session files, {len(new_files)} new to index.")

    total_sessions = 0
    total_messages = 0
    errors = 0
    for idx, filepath in enumerate(new_files, 1):
        if not QUIET and idx % 25 == 0:
            log(f"  Indexing {idx}/{len(new_files)}...")
        try:
            session_id, msg_count = index_session_file(conn, filepath)
            conn.execute(
                "INSERT OR REPLACE INTO indexed_files (path, indexed_at, message_count) VALUES (?, ?, ?)",
                (filepath, time.time(), msg_count),
            )
            if session_id and msg_count > 0:
                total_sessions += 1
                total_messages += msg_count
        except Exception as exc:  # keep backfill best-effort across mixed log formats
            errors += 1
            log(f"  ERROR indexing {filepath}: {exc}")

    conn.commit()
    conn.close()
    if QUIET:
        print(json.dumps({"sessions_indexed": total_sessions, "messages_indexed": total_messages, "errors": errors}))
    else:
        log(f"\nDone. Indexed {total_sessions} sessions, {total_messages} messages.")
        if errors:
            log(f"  ({errors} files had errors)")


if __name__ == "__main__":
    main()
