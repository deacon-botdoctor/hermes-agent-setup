"""
telegram-transcript hook — indexes Telegram messages into SQLite.

This hook is the CANONICAL WRITER for HERMES_HOME/data/telegram-transcript.db.
The companion plugin plugins/telegram-transcript is read-only tools
(telegram_history / telegram_topics / resolve_telegram_reply);
session_search_tool's current-topic mode reads the same DB keyed
telegram:<chat_id>:<thread_id>.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Bot Doctor overlay (2026-04-17): redact secrets before writing to telegram-transcript.db.
# The db feeds back into live LLM context via session_search — leaks persist indefinitely.
# Redaction masks secret VALUES only; it never restructures the message, so
# session-search LIKE matching on surrounding words is unaffected.
_bdr_re = re
# Hooks execute inside the active Hermes process. Do not prepend the nominal
# checkout: candidate runtimes may live elsewhere, and mutating sys.path here
# can combine incompatible source trees in one long-lived gateway process.
try:
    from agent.redact import redact_sensitive_text as _bdr_redact
except Exception:
    _TELEGRAM_TOKEN_RE = _bdr_re.compile(r"\b[0-9]{8,12}:AA[EFGH][A-Za-z0-9_-]{32}\b")
    _OPENROUTER_RE = _bdr_re.compile(r"\bsk-or-v1-[a-f0-9]{60,}\b")
    _ANTHROPIC_RE = _bdr_re.compile(r"\bsk-ant-(?:api|oat|admin)[0-9]+-[A-Za-z0-9_-]{60,}\b")
    _OPENAI_PROJ_RE = _bdr_re.compile(r"\bsk-proj-[A-Za-z0-9_-]{80,}\b")
    _GENERIC_SK_RE = _bdr_re.compile(r"\bsk-[A-Za-z0-9_-]{30,}\b")
    _COMPOSIO_RE = _bdr_re.compile(r"\bak_[A-Za-z0-9_-]{10,}\b")

    def _bdr_redact(text):
        if not isinstance(text, str):
            return text
        text = _TELEGRAM_TOKEN_RE.sub("[REDACTED_TELEGRAM_TOKEN]", text)
        text = _OPENROUTER_RE.sub("[REDACTED_OPENROUTER_KEY]", text)
        text = _ANTHROPIC_RE.sub("[REDACTED_ANTHROPIC_KEY]", text)
        text = _OPENAI_PROJ_RE.sub("[REDACTED_OPENAI_KEY]", text)
        text = _GENERIC_SK_RE.sub("[REDACTED_SECRET]", text)
        text = _COMPOSIO_RE.sub("[REDACTED_COMPOSIO_KEY]", text)
        return text


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
DB_PATH = HERMES_HOME / "data" / "telegram-transcript.db"
logger = logging.getLogger("telegram-transcript")
_FRESH_TOPIC_HISTORY_ROWS = 20
_FRESH_TOPIC_HISTORY_CHARS = 6000
_X_STATUS_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,20}/status/\d+(?:\?[^\s\]\)\}\"\'<>]*)?", re.IGNORECASE
)


def _normalize_x_status_url(url: str) -> str:
    return (url or "").rstrip("\"'])}>")


def _extract_x_status_urls(text: str) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for match in _X_STATUS_URL_RE.findall(text):
        normalized = _normalize_x_status_url(match)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


_LEGACY_CONTINUITY_PREFIXES = (
    "[SYSTEM] This Telegram topic may require continuity context before answering.",
    "[SYSTEM] Session was recently reset/compressed for this Telegram topic.",
    "[SYSTEM] Recent thread messages for this Telegram topic are included below as silent continuity support.",
    "[SYSTEM] Recent thread messages are included below as silent continuity support.",
    "Rehydrated.",
    "No current user request in the latest message.",
    "No current user request in that message.",
)


def _strip_internal_continuity_for_storage(text: str) -> str:
    """Strip session-resume continuity scaffolding before storage.

    Continuity-wrapped inbound messages carry the REAL user text either as a
    prefix (before an [INTERNAL_CONTINUITY] / [INTERNAL_THREAD_MEMORY] block)
    or after a current_inbound marker. Store only the user text — never the
    injected scaffolding — so transcript history and session_search stay clean.
    Pure-scaffolding messages (legacy continuity prefixes) store nothing.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    for marker in ("\n\n[INTERNAL_CONTINUITY]", "\n\n[INTERNAL_THREAD_MEMORY]"):
        if marker in raw:
            prefix = raw.split(marker, 1)[0].strip()
            if prefix:
                return prefix

    inbound_markers = (
        "current_inbound:\n",
        "Current inbound message (answer this first):\n",
        "Current inbound message:\n",
    )
    for marker in inbound_markers:
        if marker in raw:
            extracted = raw.rsplit(marker, 1)[-1].strip()
            if extracted:
                return extracted

    if any(raw.startswith(prefix) for prefix in _LEGACY_CONTINUITY_PREFIXES):
        return ""

    return raw


def _ensure_column(conn, column_name: str, ddl: str) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(telegram_messages)").fetchall()}
    if column_name not in cols:
        conn.execute(f"ALTER TABLE telegram_messages ADD COLUMN {column_name} {ddl}")


def _backfill_recent_missing_x_status_urls(conn, limit: int = 25) -> int:
    rows = conn.execute(
        """SELECT id, text
           FROM telegram_messages
           WHERE role = 'user'
             AND (x_status_urls IS NULL OR x_status_urls = '')
             AND (text LIKE '%x.com/%' OR text LIKE '%twitter.com/%')
           ORDER BY id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    updated = 0
    for row_id, text in rows:
        urls = _extract_x_status_urls(text or "")
        if not urls:
            continue
        conn.execute(
            "UPDATE telegram_messages SET x_status_urls = ? WHERE id = ?",
            (json.dumps(urls), row_id),
        )
        updated += 1
    return updated


def _get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            reply_to_message_id TEXT,
            thread_id TEXT,
            timestamp TEXT NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(chat_id, message_id)
        )
    """)
    _ensure_column(conn, "topic_name", "TEXT")
    _ensure_column(conn, "x_status_urls", "TEXT")
    # Reply/media provenance columns (additive). reply_to_message_id already
    # exists in the base schema. These let telegram_history / resolve_telegram_reply
    # reconstruct what a user replied to without re-asking.
    _ensure_column(conn, "reply_to_chat_id", "TEXT")
    _ensure_column(conn, "reply_to_thread_id", "TEXT")
    _ensure_column(conn, "reply_to_snapshot_json", "TEXT")
    _ensure_column(conn, "media_json", "TEXT")
    _ensure_column(conn, "outgoing_media_json", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_timestamp ON telegram_messages(chat_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_message ON telegram_messages(chat_id, message_id)")
    conn.commit()
    return conn


def _build_media_json(context: dict) -> str | None:
    direct = context.get("media_json")
    if direct:
        if isinstance(direct, str):
            return direct[:12000]
        try:
            return json.dumps(direct, ensure_ascii=False)[:12000]
        except TypeError:
            return json.dumps(str(direct), ensure_ascii=False)[:12000]

    media_urls = context.get("media_urls") or []
    media_types = context.get("media_types") or []
    if isinstance(media_urls, str):
        media_urls = [media_urls]
    if isinstance(media_types, str):
        media_types = [media_types]

    attachments = []
    for idx, url in enumerate(media_urls):
        if not url:
            continue
        media_type = media_types[idx] if idx < len(media_types) else ""
        attachments.append({"path": str(url), "media_type": str(media_type or "")})

    payload = {}
    if attachments:
        payload["attachments"] = attachments
    for key in ("message_type", "media_group_id", "telegram_media"):
        value = context.get(key)
        if value:
            payload[key] = value
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False)[:12000]
    except TypeError:
        payload["telegram_media"] = str(payload.get("telegram_media") or "")
        return json.dumps(payload, ensure_ascii=False)[:12000]


def _build_chat_id(raw_chat_id, thread_id):
    base = f"telegram:{raw_chat_id}" if raw_chat_id else "telegram:unknown"
    if thread_id:
        return f"{base}:{thread_id}"
    return base


def _parse_session_key(session_key: str) -> tuple[str, str | None]:
    parts = str(session_key or "").split(":")
    if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main" and parts[2] == "telegram":
        if parts[3] == "dm":
            chat_id = parts[4] if len(parts) >= 5 else ""
            thread_id = parts[5] if len(parts) >= 6 else None
            return chat_id, thread_id
        if parts[3] == "group":
            chat_id = parts[4] if len(parts) >= 5 else ""
            thread_id = parts[5] if len(parts) >= 6 else None
            return chat_id, thread_id
    return "", None


def _load_session_origin(session_id: str) -> dict:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {}
    sessions_index = HERMES_HOME / "sessions" / "sessions.json"
    try:
        if not sessions_index.exists():
            return {}
        data = json.loads(sessions_index.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        for session_key, meta in data.items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("session_id") or "").strip() != session_id:
                continue
            origin = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
            result = dict(origin)
            result["session_key"] = session_key
            return result
    except Exception:
        return {}
    return {}


def _load_latest_assistant_from_session(session_id: str) -> tuple[str, str] | tuple[None, None]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return None, None
    path = HERMES_HOME / "sessions" / f"{session_id}.jsonl"
    try:
        if not path.exists():
            return None, None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if str(row.get("role") or "").strip() != "assistant":
                continue
            content = row.get("content")
            if isinstance(content, str):
                content = content.strip()
            else:
                content = ""
            if not content:
                continue
            timestamp = str(row.get("timestamp") or "").strip() or datetime.now(timezone.utc).isoformat()
            return content, timestamp
    except Exception:
        return None, None
    return None, None


def _coerce_routing(context: dict) -> tuple[str, str | None, str | None, str | None, str]:
    raw_chat_id = context.get("chat_id", "")
    thread_id = context.get("thread_id") or None
    user_id = context.get("user_id") or None
    chat_type = (context.get("chat_type") or "").strip().lower()
    session_key = str(context.get("session_key") or "").strip()
    session_id = str(context.get("session_id") or "").strip()
    if (not raw_chat_id) and session_key:
        raw_chat_id, parsed_thread_id = _parse_session_key(session_key)
        if not thread_id:
            thread_id = parsed_thread_id
    if (not raw_chat_id or not user_id or not chat_type) and session_id:
        origin = _load_session_origin(session_id)
        if not raw_chat_id:
            raw_chat_id = origin.get("chat_id") or raw_chat_id
        if not thread_id:
            thread_id = origin.get("thread_id") or thread_id
        if not user_id:
            user_id = origin.get("user_id") or user_id
        if not chat_type:
            chat_type = str(origin.get("chat_type") or "").strip().lower()
        if not session_key:
            session_key = str(origin.get("session_key") or "").strip()
    if not raw_chat_id and not thread_id and chat_type == "dm" and user_id:
        raw_chat_id = user_id
    chat_id = _build_chat_id(raw_chat_id, thread_id)
    return raw_chat_id, thread_id, user_id, chat_type, chat_id


_INTERNAL_TRANSCRIPT_PREFIXES = (
    "[SYSTEM] There is unfinished work in this conversation",
    "[SYSTEM] This Telegram topic may require continuity context",
    "[SYSTEM] Session was recently reset/compressed for this Telegram topic.",
    "[SYSTEM] Gateway resumed after downtime.",
    "<recent-chat-history",
)


def _looks_internal_transcript_artifact(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if normalized.startswith("[SYSTEM: Auto-triggered x-link-auto-research"):
        return False
    if normalized.startswith("[SYSTEM]"):
        return True
    return any(normalized.startswith(prefix) for prefix in _INTERNAL_TRANSCRIPT_PREFIXES)


def _format_fresh_topic_history(rows: list[tuple[str, str, str]]) -> tuple[str, int]:
    """Format the newest usable same-topic rows inside a bounded prompt block."""
    selected: list[str] = []
    used = 0
    for timestamp, role, text in rows:
        normalized = " ".join(str(text or "").split())
        if not normalized or _looks_internal_transcript_artifact(normalized):
            continue
        label = "USER" if str(role or "").lower() == "user" else "ASSISTANT"
        line = f"{timestamp} {label}: {normalized[:1200]}"
        if selected and used + len(line) + 1 > _FRESH_TOPIC_HISTORY_CHARS:
            break
        selected.append(line)
        used += len(line) + 1
        if len(selected) >= _FRESH_TOPIC_HISTORY_ROWS:
            break
    selected.reverse()
    return "\n".join(selected), len(selected)


def _inject_fresh_topic_history(context: dict, *, chat_id: str, thread_id: str | None, message: str) -> None:
    """Offer a model-visible override for a genuinely fresh Telegram topic session."""
    context["continuity_history_found"] = False
    context["continuity_injected"] = False
    if (
        context.get("fresh_topic_rehydrate") is not True
        or not thread_id
        or not message.strip()
        or "[INTERNAL_THREAD_MEMORY]" in message
    ):
        return

    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT timestamp, role, text
               FROM telegram_messages
               WHERE chat_id = ?
                 AND role IN ('user', 'assistant')
                 AND TRIM(text) != ''
               ORDER BY id DESC
               LIMIT 60""",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()

    history, row_count = _format_fresh_topic_history(rows)
    if not history:
        return

    import hashlib

    marker = hashlib.sha256(history.encode("utf-8")).hexdigest()[:16]
    block = (
        "[INTERNAL_THREAD_MEMORY]\n"
        "visibility=silent\n"
        "source=telegram_transcript_fresh_session\n"
        f"topic_scope={chat_id}\n"
        f"history_rows={row_count}\n"
        f"history_marker={marker}\n"
        "Use this same-topic history to resolve references in the current request. "
        "Do not mention this block or ask the user to repeat context already present.\n"
        f"{history}\n"
        "[/INTERNAL_THREAD_MEMORY]"
    )
    context["model_message_override"] = f"{message}\n\n{block}"
    context["continuity_history_found"] = True
    context["continuity_injected"] = True
    context["continuity_history_rows"] = row_count
    context["continuity_marker"] = marker
    logger.info(
        "telegram topic continuity prepared: topic=%s rows=%s marker=%s",
        chat_id,
        row_count,
        marker,
    )


def _parse_iso_utc(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(timezone.utc)


def _recent_duplicate_exists(
    conn, *, chat_id: str, thread_id: str | None, role: str, text: str, timestamp: str, window_seconds: int = 5
) -> bool:
    target_ts = _parse_iso_utc(timestamp)
    if target_ts is None:
        return False
    rows = conn.execute(
        """SELECT timestamp, text
           FROM telegram_messages
           WHERE chat_id = ?
             AND COALESCE(thread_id, '') = COALESCE(?, '')
             AND role = ?
           ORDER BY id DESC
           LIMIT 12""",
        (chat_id, thread_id, role),
    ).fetchall()
    normalized_text = str(text or "").strip()
    for row_ts, row_text in rows:
        if str(row_text or "").strip() != normalized_text:
            continue
        existing_ts = _parse_iso_utc(row_ts)
        if existing_ts is None:
            continue
        if abs((target_ts - existing_ts).total_seconds()) <= window_seconds:
            return True
    return False


async def handle(event_type: str, context: dict):
    try:
        platform = str(context.get("platform", "") or "").strip().lower()
        raw_chat_id, thread_id, user_id, chat_type, chat_id = _coerce_routing(context)
        if platform != "telegram":
            if str(context.get("session_key") or "").startswith("agent:main:telegram:"):
                platform = "telegram"
            elif raw_chat_id:
                platform = "telegram"
        if platform != "telegram":
            return
        session_id = context.get("session_id", "")
        sender_name = context.get("sender_name") or context.get("user_name") or None
        timestamp = datetime.now(timezone.utc).isoformat()

        if event_type == "agent:start":
            # Golden continuity-strip: store the real user text, never the
            # session-resume scaffolding wrapped around it. Runs BEFORE the
            # artifact check so a continuity-wrapped real message is kept
            # (stripped) while pure scaffolding is skipped entirely.
            message = _strip_internal_continuity_for_storage(
                context.get("full_message") or context.get("message") or ""
            )
            if not message or _looks_internal_transcript_artifact(message):
                return
            _inject_fresh_topic_history(
                context,
                chat_id=chat_id,
                thread_id=thread_id,
                message=message,
            )
            x_status_urls = _extract_x_status_urls(message)
            x_status_urls_json = json.dumps(x_status_urls) if x_status_urls else None
            import hashlib

            inbound_message_id = str(context.get("message_id") or "").strip()
            message_id = (
                inbound_message_id
                or f"user-{session_id}-{hashlib.md5(f'{session_id}-{timestamp}'.encode()).hexdigest()[:8]}"
            )
            redacted_message = _bdr_redact(message)[:8000]
            reply_to_message_id = str(context.get("reply_to_message_id") or "").strip() or None
            reply_to_snapshot_json = context.get("reply_to_snapshot_json") or None
            reply_to_chat_id = None
            reply_to_thread_id = None
            if reply_to_snapshot_json:
                try:
                    reply_to_snapshot_json = _bdr_redact(reply_to_snapshot_json)
                    _snap = json.loads(reply_to_snapshot_json)
                    if isinstance(_snap, dict):
                        reply_to_chat_id = _snap.get("chat_id")
                        reply_to_thread_id = _snap.get("thread_id")
                except Exception:
                    pass
            media_json = _build_media_json(context)
            conn = _get_db()
            try:
                if (not inbound_message_id) and _recent_duplicate_exists(
                    conn,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    role="user",
                    text=redacted_message,
                    timestamp=timestamp,
                    window_seconds=300,
                ):
                    return
                conn.execute(
                    """INSERT OR IGNORE INTO telegram_messages
                       (chat_id, message_id, reply_to_message_id, thread_id,
                        timestamp, sender_id, sender_name, role, text, x_status_urls,
                        reply_to_chat_id, reply_to_thread_id, reply_to_snapshot_json,
                        media_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chat_id,
                        message_id,
                        reply_to_message_id,
                        thread_id,
                        timestamp,
                        user_id,
                        sender_name,
                        "user",
                        redacted_message,
                        x_status_urls_json,
                        reply_to_chat_id,
                        reply_to_thread_id,
                        reply_to_snapshot_json,
                        media_json,
                    ),
                )
                if media_json:
                    conn.execute(
                        """UPDATE telegram_messages
                           SET media_json = CASE
                               WHEN media_json IS NULL OR media_json = '' THEN ?
                               ELSE media_json
                           END
                           WHERE chat_id = ? AND message_id = ?""",
                        (media_json, chat_id, message_id),
                    )
                _backfill_recent_missing_x_status_urls(conn)
                conn.commit()
            finally:
                conn.close()

        elif event_type in ("processing:complete", "agent:end"):
            if context.get("delivery_attempted") and not context.get("delivery_succeeded"):
                return
            response = ""
            response_ts = timestamp
            # PHANTOM_ASSISTANT_FIX_v1: removed "message" + "text" - both can hold user content
            for key in ("response", "final", "reply"):
                value = context.get(key)
                if isinstance(value, str) and value.strip():
                    response = value.strip()
                    break
            if not response:
                response, session_ts = _load_latest_assistant_from_session(session_id)
                if session_ts:
                    response_ts = session_ts
            if not response:
                return
            import hashlib

            h = hashlib.md5(f"{session_id}-{response}".encode()).hexdigest()[:12]
            message_id = f"assistant-{session_id}-{h}"
            redacted_response = _bdr_redact(response)[:8000]
            conn = _get_db()
            try:
                if _recent_duplicate_exists(
                    conn,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    role="assistant",
                    text=redacted_response,
                    timestamp=response_ts,
                ):
                    return
                conn.execute(
                    """INSERT OR IGNORE INTO telegram_messages
                       (chat_id, message_id, reply_to_message_id, thread_id,
                        timestamp, sender_id, sender_name, role, text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chat_id, message_id, None, thread_id, response_ts, None, None, "assistant", redacted_response),
                )
                conn.commit()
            finally:
                conn.close()

    except Exception as e:
        logger.warning("telegram-transcript hook error: %s", e)
