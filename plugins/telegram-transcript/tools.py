"""
telegram-transcript read tools (write path owned by the gateway hook)

The transcript WRITER is hooks/telegram-transcript/handler.py (events:
agent:start / agent:end / processing:complete). This plugin only registers
read tools over HERMES_HOME/data/telegram-transcript.db:
telegram_history, telegram_topics, resolve_telegram_reply.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HERMES_HOME / "data" / "telegram-transcript.db"

# Agent display name — read from env so each client uses their own bot name
AGENT_NAME = os.environ.get("HERMES_AGENT_NAME") or os.environ.get("AGENT_NAME") or "Assistant"

_db_conn: Optional[sqlite3.Connection] = None


_INTERNAL_HISTORY_PREFIXES = (
    "[SYSTEM] There is unfinished work in this conversation",
    "[SYSTEM] This Telegram topic may require continuity context",
    "[SYSTEM] Session was recently reset/compressed for this Telegram topic.",
    "[SYSTEM] Gateway resumed after downtime.",
    "[INTERNAL_CONTINUITY]",
    "[INTERNAL_THREAD_MEMORY]",
    "Rehydrated.",
    "No current user request in the latest message.",
    "No current user request in that message.",
    "<recent-chat-history",
)


def _looks_internal_history_artifact(text: Any) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return any(normalized.startswith(prefix) for prefix in _INTERNAL_HISTORY_PREFIXES)


def _filter_history_rows(rows: list[tuple]) -> list[tuple]:
    filtered = []
    for row in rows:
        try:
            text = row[3]
        except Exception:
            text = ""
        if _looks_internal_history_artifact(text):
            continue
        filtered.append(row)
    return filtered


def get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            message_id TEXT,
            thread_id TEXT,
            timestamp TEXT NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            topic_name TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_ts ON telegram_messages(chat_id, timestamp)")
    # Migration: add topic_name if not present
    try:
        conn.execute("ALTER TABLE telegram_messages ADD COLUMN topic_name TEXT")
        conn.commit()
    except Exception:
        pass
    # Topics index table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_topics (
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            topic_name TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, thread_id)
        )
    """)
    conn.commit()
    _db_conn = conn
    return conn


# --- Tool ---

TELEGRAM_HISTORY_SCHEMA = {
    "name": "telegram_history",
    "description": "Read recent Telegram chat history from the transcript database. Use this to recall what was discussed in any Telegram chat or topic.",
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "The chat ID to read. Format: 'telegram:{chat_id}' for a DM or bare group, or 'telegram:{chat_id}:{thread_id}' for a specific forum topic. IMPORTANT: when you are inside a Telegram group topic (the Source context shows 'thread: N'), you MUST use the topic-scoped form 'telegram:{chat_id}:{thread_id}' — never the bare group ID. The bare group ID crosses topic boundaries and will return messages from unrelated topics."
            },
            "limit": {
                "type": "integer",
                "description": "Number of messages to return (default 20)",
                "default": 20
            },
            "format": {
                "type": "string",
                "enum": ["text", "json"],
                "description": "Output format: 'text' for human-readable, 'json' for structured",
                "default": "text"
            }
        },
        "required": ["chat_id"]
    }
}


def _normalize_chat_id(chat_id: Any) -> str:
    """Accept either canonical string chat IDs or structured dict payloads."""
    if isinstance(chat_id, str):
        return chat_id.strip()
    if isinstance(chat_id, dict):
        platform = str(chat_id.get("platform") or "telegram").strip()
        raw_chat_id = chat_id.get("chat_id") or chat_id.get("id")
        thread_id = chat_id.get("thread_id") or chat_id.get("thread") or chat_id.get("topic_id")
        if raw_chat_id is None:
            return ""
        canonical = f"{platform}:{raw_chat_id}"
        if thread_id not in (None, "", 0, "0"):
            canonical = f"{canonical}:{thread_id}"
        return canonical
    return str(chat_id or "").strip()


def _resolve_lookup_chat_ids(db: sqlite3.Connection, normalized_chat_id: str) -> list[str]:
    """Return safe lookup candidates without crossing Telegram topic boundaries.

    Topic history is isolated by the full canonical key
    telegram:<chat_id>:<thread_id>. Older fallback logic searched by only
    the numeric thread id or by a bare numeric chat fragment; in a forum group
    that could remap a bare group lookup to whichever topic was most recent, or
    match the same thread id in another group. Keep fallbacks exact for topics
    and never expand a bare negative Telegram group id into topic rows.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: Optional[str]) -> None:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    add(normalized_chat_id)

    if not normalized_chat_id.startswith("telegram:"):
        return candidates

    parts = normalized_chat_id.split(":")
    base_chat_id = parts[1] if len(parts) >= 2 else ""
    thread_id = parts[2] if len(parts) >= 3 else None

    if thread_id:
        # Only same-chat + same-thread variants are safe. Do not search by
        # thread_id alone; Telegram forum thread ids are not globally unique.
        add(f"telegram:{base_chat_id}:{thread_id}")
        rows = db.execute(
            """SELECT DISTINCT chat_id
               FROM telegram_messages
               WHERE chat_id = ?
                  OR (chat_id = ? AND thread_id = ?)
               ORDER BY chat_id""",
            (f"telegram:{base_chat_id}:{thread_id}", f"telegram:{base_chat_id}", thread_id),
        ).fetchall()
        for row in rows:
            add(row[0])
        return candidates

    # Bare negative Telegram ids are groups/channels. A bare group lookup is not
    # topic-scoped, so do not helpfully remap it to a random topic row.
    if base_chat_id.startswith("-"):
        return candidates

    # Positive ids are DMs; keep the legacy DM format fallback only.
    rows = db.execute(
        """SELECT DISTINCT chat_id FROM telegram_messages
           WHERE chat_id IN (?, ?)
           ORDER BY chat_id""",
        (f"telegram:{base_chat_id}", f"telegram:dm:{base_chat_id}"),
    ).fetchall()
    for row in rows:
        add(row[0])
    return candidates


def _table_columns(db: sqlite3.Connection) -> set:
    try:
        return {row[1] for row in db.execute("PRAGMA table_info(telegram_messages)").fetchall()}
    except Exception:
        return set()


def _split_chat_thread(resolved_chat_id: str) -> tuple[str, Optional[str]]:
    parts = str(resolved_chat_id or "").split(":")
    base = f"telegram:{parts[1]}" if len(parts) >= 2 else resolved_chat_id
    thread = parts[2] if len(parts) >= 3 else None
    return base, thread


def _provenance_lookup(chat_id: str, thread_id: Optional[str], message_id: Optional[str]) -> list:
    """Best-effort join into the media-provenance store. Returns [] on any failure.

    The provenance store (gateway/media_provenance.py) maps a Telegram
    message_id -> the local artifact that produced or received it. Importing it
    lazily keeps this plugin importable on stacks where the gateway package is
    not on the path.
    """
    if not message_id:
        return []
    try:
        from gateway import media_provenance  # type: ignore
    except Exception:
        return []
    try:
        rows = media_provenance.lookup_by_message(chat_id, thread_id, message_id)
        return rows or []
    except Exception:
        return []


def _lookup_local_reply_row(db: sqlite3.Connection, resolved_chat_id: str,
                            reply_to_message_id: str) -> Optional[dict]:
    """Look up a replied-to message in the SAME topic-scoped chat_id only.

    Telegram forum thread ids are not globally unique, so this never widens the
    search beyond the exact canonical key to preserve topic isolation.
    """
    if not reply_to_message_id:
        return None
    try:
        cols = _table_columns(db)
        select_cols = ["timestamp", "sender_name", "role", "text"]
        if "media_json" in cols:
            select_cols.append("media_json")
        row = db.execute(
            f"""SELECT {', '.join(select_cols)}
                FROM telegram_messages
                WHERE chat_id = ? AND message_id = ?
                ORDER BY timestamp DESC LIMIT 1""",
            (resolved_chat_id, str(reply_to_message_id)),
        ).fetchone()
        if not row:
            return None
        data = dict(zip(select_cols, row))
        return {
            "sender": data.get("sender_name") or data.get("role"),
            "role": data.get("role"),
            "text": data.get("text"),
            "media_json": data.get("media_json"),
        }
    except Exception:
        return None


def _build_reply_to_object(db: sqlite3.Connection, resolved_chat_id: str,
                           reply_to_message_id: Optional[str],
                           reply_to_snapshot_json: Optional[str]) -> Optional[dict]:
    """Hydrate a reply_to object from snapshot, then local DB, then provenance."""
    reply_obj: dict = {}

    # (a) Stored snapshot from the gateway (richest source).
    if reply_to_snapshot_json:
        try:
            snap = json.loads(reply_to_snapshot_json)
            if isinstance(snap, dict):
                reply_obj.update(snap)
        except Exception:
            pass

    base_chat, thread_id = _split_chat_thread(resolved_chat_id)

    # (b) Fall back to a local transcript row in the same topic.
    if not reply_obj.get("text") and not reply_obj.get("media"):
        local = _lookup_local_reply_row(db, resolved_chat_id, reply_to_message_id or "")
        if local:
            reply_obj.setdefault("sender", local.get("sender"))
            if local.get("text") and not reply_obj.get("text"):
                reply_obj["text"] = local["text"]
            if local.get("media_json") and not reply_obj.get("media"):
                try:
                    media = json.loads(local["media_json"])
                    if media:
                        reply_obj["media"] = media
                except Exception:
                    pass

    # (c) Join the provenance store to attach origin_path/local_cached_path/sha256.
    prov = _provenance_lookup(base_chat, thread_id, reply_to_message_id)
    if prov:
        existing = reply_obj.get("media")
        if not isinstance(existing, list):
            existing = []
        # Merge provenance into the snapshot's media descriptor by
        # file_unique_id so we enrich the existing entry (adding origin_path,
        # local_path, sha256) instead of emitting a phantom duplicate that
        # lacks a path.  Only append a fresh descriptor when no snapshot media
        # matches.
        by_fuid = {
            m.get("file_unique_id"): m
            for m in existing
            if isinstance(m, dict) and m.get("file_unique_id")
        }
        for p in prov:
            fuid = p.get("file_unique_id")
            target = by_fuid.get(fuid) if fuid else None
            if target is not None:
                target.setdefault("kind", p.get("kind"))
                target.setdefault("file_id", p.get("file_id"))
                if p.get("origin_path"):
                    target["origin_path"] = p.get("origin_path")
                if p.get("local_cached_path"):
                    target["local_path"] = p.get("local_cached_path")
                if p.get("sha256"):
                    target["sha256"] = p.get("sha256")
                if p.get("caption") and not target.get("caption"):
                    target["caption"] = p.get("caption")
                if p.get("filename"):
                    target["filename"] = p.get("filename")
            else:
                new_desc = {
                    "kind": p.get("kind"),
                    "file_id": p.get("file_id"),
                    "file_unique_id": fuid,
                    "origin_path": p.get("origin_path"),
                    "local_path": p.get("local_cached_path"),
                    "sha256": p.get("sha256"),
                    "caption": p.get("caption"),
                    "filename": p.get("filename"),
                }
                existing.append(new_desc)
                if fuid:
                    by_fuid[fuid] = new_desc
        reply_obj["media"] = existing

    if not reply_obj:
        return None
    if reply_to_message_id and not reply_obj.get("message_id"):
        reply_obj["message_id"] = str(reply_to_message_id)
    return reply_obj


def _reply_to_text_line(reply_obj: Optional[dict]) -> str:
    """Compact one-line reply summary for format=text."""
    if not reply_obj:
        return ""
    mid = reply_obj.get("message_id")
    media = reply_obj.get("media") or []
    if media:
        m0 = media[0]
        origin = m0.get("origin_path") or m0.get("local_path")
        fname = m0.get("filename")
        caption = reply_obj.get("caption") or m0.get("caption")
        if not fname and origin:
            fname = os.path.basename(origin)
        if caption and origin:
            return f"[Replying to media: {caption} — {origin}]"
        bits = [f"reply_to message_id={mid}"]
        if fname:
            bits.append(f"media={fname}")
        if origin:
            bits.append(f"origin_path={origin}")
        return "[" + " ".join(bits) + "]"
    snippet = (reply_obj.get("text") or "").strip().replace("\n", " ")
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    if snippet:
        return f'[reply_to message_id={mid}: "{snippet}"]'
    if mid:
        return f"[reply_to message_id={mid}]"
    return ""


def telegram_history_handler(chat_id: str, limit: int = 20, format: str = "text", **kwargs) -> str:
    try:
        normalized_chat_id = _normalize_chat_id(chat_id)
        if not normalized_chat_id:
            return "Error reading history: missing chat_id"

        db = get_db()
        cols = _table_columns(db)
        # Build a column list that degrades gracefully on older DBs.
        base_select = ["timestamp", "sender_name", "role", "text", "message_id", "thread_id"]
        opt_select = [c for c in ("reply_to_message_id", "reply_to_snapshot_json", "media_json")
                      if c in cols]
        select_cols = [c for c in base_select if c in cols] + opt_select
        rows = []
        resolved_chat_id = normalized_chat_id
        for candidate in _resolve_lookup_chat_ids(db, normalized_chat_id):
            candidate_rows = db.execute(
                f"""SELECT {', '.join(select_cols)}
                   FROM telegram_messages
                   WHERE chat_id = ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (candidate, max(int(limit) * 4, int(limit)))
            ).fetchall()
            candidate_rows = _filter_history_rows(candidate_rows)
            if candidate_rows:
                rows = candidate_rows
                resolved_chat_id = candidate
                break
        rows = list(reversed(rows))[-int(limit):]  # chronological order

        if not rows:
            return f"No messages found for {normalized_chat_id}"

        base_chat, scoped_thread = _split_chat_thread(resolved_chat_id)
        dict_rows = [dict(zip(select_cols, r)) for r in rows]

        if format == "json":
            payload = []
            for d in dict_rows:
                item = {
                    "timestamp": d.get("timestamp"),
                    "sender": d.get("sender_name") or d.get("role"),
                    "role": d.get("role"),
                    "text": d.get("text"),
                    "message_id": d.get("message_id"),
                    "chat_id": resolved_chat_id,
                    "thread_id": d.get("thread_id") or scoped_thread,
                }
                # Current-message media.
                if d.get("media_json"):
                    try:
                        item["media"] = json.loads(d["media_json"])
                    except Exception:
                        pass
                reply_id = d.get("reply_to_message_id")
                if reply_id:
                    item["reply_to_message_id"] = reply_id
                    reply_obj = _build_reply_to_object(
                        db, resolved_chat_id, reply_id, d.get("reply_to_snapshot_json")
                    )
                    if reply_obj:
                        item["reply_to"] = reply_obj
                payload.append(item)
            return json.dumps({"chat_id": resolved_chat_id, "messages": payload}, indent=2)

        lines = []
        if resolved_chat_id != normalized_chat_id:
            lines.append(f"[history lookup remapped to {resolved_chat_id}]")
        for d in dict_rows:
            label = d.get("sender_name") or ("You" if d.get("role") == "user" else AGENT_NAME)
            lines.append(f"[{d.get('timestamp')}] {label}: {d.get('text')}")
            reply_id = d.get("reply_to_message_id")
            if reply_id:
                reply_obj = _build_reply_to_object(
                    db, resolved_chat_id, reply_id, d.get("reply_to_snapshot_json")
                )
                line = _reply_to_text_line(reply_obj)
                if line:
                    lines.append("  " + line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading history: {e}"



RESOLVE_REPLY_SCHEMA = {
    "name": "resolve_telegram_reply",
    "description": (
        "Resolve what a Telegram message replied to, including the exact local "
        "or generated artifact path of any replied-to image/file. Use this when "
        "the user says things like 'I like this one', 'use this background', or "
        "'this photo' in reply to a previously sent image, so you can identify "
        "the exact file without re-asking. "
        "The returned origin_paths are AUTHORITATIVE: they come from a stored "
        "provenance map keyed by the exact Telegram message_id that was "
        "delivered, NOT a guess based on filenames. When 'authoritative' is "
        "true and a media item has verified=true, trust that path and use it "
        "directly as the subject/background/source — do not second-guess it as "
        "a 'local filename mapping' and do not re-ask which image was meant. "
        "Returns: current_message, reply_target (text/caption/media), "
        "resolved_media (each with verified/path_exists), origin_paths, "
        "verified_origin_paths, authoritative (bool), and guidance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Topic-scoped chat id: 'telegram:{chat_id}:{thread_id}' inside a forum topic, or 'telegram:{chat_id}' for a DM/bare group.",
            },
            "message_id": {
                "type": "string",
                "description": "Optional. The message_id of the user reply to resolve. If omitted, the most recent message in the chat/topic that is itself a reply is used.",
            },
            "thread_id": {
                "type": "string",
                "description": "Optional. Forum topic thread id, if not already encoded in chat_id.",
            },
        },
        "required": ["chat_id"],
    },
}


def resolve_telegram_reply_handler(chat_id: str, message_id: str = None,
                                   thread_id: str = None, **kwargs) -> str:
    try:
        normalized_chat_id = _normalize_chat_id(chat_id)
        if not normalized_chat_id:
            return json.dumps({"error": "missing chat_id"})
        if thread_id and ":" not in normalized_chat_id.split("telegram:", 1)[-1]:
            normalized_chat_id = f"{normalized_chat_id}:{thread_id}"

        db = get_db()
        cols = _table_columns(db)
        select_cols = [c for c in ("timestamp", "sender_name", "role", "text", "message_id",
                                   "thread_id", "reply_to_message_id", "reply_to_snapshot_json",
                                   "media_json") if c in cols]

        resolved_chat_id = normalized_chat_id
        row = None
        for candidate in _resolve_lookup_chat_ids(db, normalized_chat_id):
            if message_id:
                r = db.execute(
                    f"""SELECT {', '.join(select_cols)} FROM telegram_messages
                        WHERE chat_id = ? AND message_id = ? LIMIT 1""",
                    (candidate, str(message_id)),
                ).fetchone()
            else:
                r = db.execute(
                    f"""SELECT {', '.join(select_cols)} FROM telegram_messages
                        WHERE chat_id = ? AND reply_to_message_id IS NOT NULL
                          AND reply_to_message_id != ''
                        ORDER BY timestamp DESC LIMIT 1""",
                    (candidate,),
                ).fetchone()
            if r:
                row = dict(zip(select_cols, r))
                resolved_chat_id = candidate
                break

        if not row:
            return json.dumps({
                "chat_id": resolved_chat_id,
                "error": "no matching message found (or it was not a reply)",
            })

        current = {
            "timestamp": row.get("timestamp"),
            "sender": row.get("sender_name") or row.get("role"),
            "role": row.get("role"),
            "text": row.get("text"),
            "message_id": row.get("message_id"),
            "chat_id": resolved_chat_id,
        }
        reply_id = row.get("reply_to_message_id")
        reply_obj = None
        if reply_id:
            reply_obj = _build_reply_to_object(
                db, resolved_chat_id, reply_id, row.get("reply_to_snapshot_json")
            )
        origin_paths = []
        verified_paths = []
        resolved_media = []
        if reply_obj:
            for m in (reply_obj.get("media") or []):
                # Verify the resolved artifact actually exists on disk and
                # stamp the media descriptor with an authority signal.  This
                # is what lets the agent trust the path: it is an exact
                # Telegram message_id -> artifact resolution, NOT a filename
                # guess.  Prefer origin_path (generator source) over the
                # ephemeral local_cached_path.
                op = m.get("origin_path") or m.get("local_path")
                exists = False
                size = None
                if op:
                    try:
                        exists = os.path.isfile(op)
                        if exists:
                            size = os.path.getsize(op)
                    except Exception:
                        exists = False
                m["path_exists"] = exists
                if size is not None:
                    m["file_size_on_disk"] = size
                # An artifact is authoritative when we have a concrete path
                # resolved by message_id and that file is present on disk.
                m["verified"] = bool(op and exists)
                resolved_media.append(m)
                if op and op not in origin_paths:
                    origin_paths.append(op)
                    if exists:
                        verified_paths.append(op)

        any_verified = bool(verified_paths)
        if any_verified:
            guidance = (
                "AUTHORITATIVE RESOLUTION. The origin_paths below are resolved "
                "from the exact Telegram message_id the user replied to (a "
                "stored provenance map of message_id -> the file that was "
                "actually sent), NOT a guess based on filenames. Each verified "
                "path was confirmed to exist on disk. Use these paths directly "
                "as the subject/background/source for the requested operation. "
                "Do NOT second-guess them as 'local filename mappings', do NOT "
                "re-ask the user which image they meant, and do NOT substitute "
                "a different file because its name looks related."
            )
        elif origin_paths:
            guidance = (
                "PARTIAL RESOLUTION. The replied-to message was resolved and "
                "origin_paths were found, but one or more files are missing on "
                "disk (path_exists=false). Re-generate or ask the user to "
                "re-send only if a verified path is unavailable."
            )
        else:
            guidance = (
                "NO LOCAL ARTIFACT. The reply target was resolved but carries "
                "no recorded origin_path (e.g. media predates provenance, or a "
                "user upload not yet cached). Ask the user to send/forward the "
                "image directly rather than guessing a local file by name."
            )

        return json.dumps({
            "chat_id": resolved_chat_id,
            "current_message": current,
            "reply_target": reply_obj,
            "resolved_media": resolved_media,
            "origin_paths": origin_paths,
            "verified_origin_paths": verified_paths,
            "authoritative": any_verified,
            "guidance": guidance,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": f"resolve_telegram_reply failed: {e}"})


TELEGRAM_TOPICS_SCHEMA = {
    "name": "telegram_topics",
    "description": "List all known Telegram topics/threads with their names and IDs. Use this to discover topic names before sending or routing messages.",
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Optional. Filter results to a specific group chat_id (e.g. '-100XXXXXXXXXX')."
            }
        },
        "required": []
    }
}


def telegram_topics_handler(chat_id: str = None, **kwargs) -> str:
    import json
    try:
        db = get_db()
        rows = []
        if chat_id:
            rows = db.execute(
                "SELECT chat_id, thread_id, topic_name FROM telegram_topics WHERE chat_id = ? ORDER BY thread_id",
                (chat_id,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT chat_id, thread_id, topic_name FROM telegram_topics ORDER BY chat_id, thread_id"
            ).fetchall()

        # Backfill from message history if the explicit topics index is empty or stale.
        # This keeps topic discovery useful even on older stacks that recorded thread-scoped
        # chat_ids in telegram_messages but never populated telegram_topics.
        if not rows:
            raw_rows = db.execute(
                "SELECT chat_id, thread_id, topic_name FROM telegram_messages WHERE thread_id IS NOT NULL OR chat_id LIKE 'telegram:%:%'"
            ).fetchall()
            recovered = {}
            for raw_chat_id, raw_thread_id, raw_topic_name in raw_rows:
                raw_chat_id = str(raw_chat_id or '').strip()
                if not raw_chat_id.startswith('telegram:'):
                    continue
                parts = raw_chat_id.split(':')
                base_chat_id = parts[1] if len(parts) >= 2 else ''
                thread_value = str(raw_thread_id or '').strip()
                if not thread_value and len(parts) >= 3:
                    thread_value = parts[2].strip()
                if thread_value in ('', '0'):
                    continue
                topic_value = str(raw_topic_name or '').strip() or f'Topic {thread_value}'
                key = (base_chat_id, thread_value)
                recovered.setdefault(key, topic_value)

            rows = [
                (base_chat_id, thread_value, topic_value)
                for (base_chat_id, thread_value), topic_value in sorted(recovered.items(), key=lambda item: (item[0][0], item[0][1]))
                if (not chat_id) or base_chat_id == chat_id
            ]

        return json.dumps([
            {"chat_id": r[0], "thread_id": r[1], "topic_name": r[2]}
            for r in rows
        ], indent=2)
    except Exception as e:
        return f"Error reading topics: {e}"


# --- Register ---

def register(ctx):
    ctx.register_tool(
        "telegram_history",
        "telegram-transcript",
        TELEGRAM_HISTORY_SCHEMA,
        lambda args, **kwargs: telegram_history_handler(
            chat_id=args.get("chat_id", ""),
            limit=args.get("limit", 20),
            format=args.get("format", "text"),
            **kwargs,
        ),
    )
    ctx.register_tool(
        "telegram_topics",
        "telegram-transcript",
        TELEGRAM_TOPICS_SCHEMA,
        lambda args, **kwargs: telegram_topics_handler(
            chat_id=args.get("chat_id"),
            **kwargs,
        ),
    )
    ctx.register_tool(
        "resolve_telegram_reply",
        "telegram-transcript",
        RESOLVE_REPLY_SCHEMA,
        lambda args, **kwargs: resolve_telegram_reply_handler(
            chat_id=args.get("chat_id", ""),
            message_id=args.get("message_id"),
            thread_id=args.get("thread_id"),
            **kwargs,
        ),
    )
    logger.info("telegram-transcript: plugin registered (read-only)")
