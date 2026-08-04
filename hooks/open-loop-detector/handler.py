# ruff: noqa: E501
"""
open-loop-detector — find unresolved conversations and re-engage agents.

Runs on gateway:startup after native recovery settles, then repeats every 2 hours
while the gateway is running. Uses the configured auxiliary model (from config.yaml) for classification.
Re-injects into the original chat so the agent picks up naturally.
Channel exclusions and review routes come from the active runtime's channel
registry, loaded without changing process-wide import resolution.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger("hooks.open-loop-detector")

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_ROOT = (
    HERMES_HOME.parent.parent
    if HERMES_HOME.name != ".hermes" and HERMES_HOME.parent.name == "profiles"
    else HERMES_HOME
)


def _load_channel_registry():
    """Load this runtime's registry without mutating global import state."""
    registry_path = HERMES_HOME / "bin" / "channel_registry.py"
    spec = importlib.util.spec_from_file_location("_hermes_runtime_channel_registry", registry_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    cr = _load_channel_registry()
except Exception:
    cr = None
TRANSCRIPT_DB = HERMES_HOME / "data" / "telegram-transcript.db"
DURABLE_DB = HERMES_HOME / "data" / "durable-threads.db"
TASK_LEDGER_DB = HERMES_HOME / "data" / "task-ledger.db"
BREADCRUMBS_FILE = HERMES_HOME / "state" / "active-breadcrumbs.json"
STATE_FILE = HERMES_HOME / "state" / "open-loop-last-run.json"

# Classification model — reads from config.yaml auxiliary or falls back to gateway default
CLASSIFY_MODEL = None  # resolved at runtime from config
CLASSIFY_BASE_URL = None  # resolved at runtime from config

MAX_AGE_HOURS = 24
STARTUP_DELAY = 120  # seconds — leave more room after startup and avoid immediate provider bursts
REPEAT_INTERVAL = 7200  # 2 hours
MAX_CHATS = 100
MSGS_PER_CHAT = 12

# Deliver-only chats: never re-engage these — they are inbound-only topics
# Deliver-only / quarantined chats sourced from the runtime channel registry
# (key: open_loop_excluded_chats). Format matches transcript DB chat_id.
EXCLUDED_CHATS = set(cr.named_list("open_loop_excluded_chats")) if cr else set()
INTERNAL_REVIEW_CHAT_ID = (cr.chat("botdoctor-internal") if cr else "") or ""


# (client_group, client_thread) -> internal review thread, rebuilt from the
# registry's "group:thread" string-keyed maps into the tuple-keyed form the
# rest of this module expects.
def _tuple_routes(name):
    out = {}
    for k, v in (cr.named_map(name) if cr else {}).items():
        grp, _, thr = str(k).partition(":")
        out[(grp, thr)] = v
    return out


CLIENT_REVIEW_ROUTES = _tuple_routes("open_loop_client_routes")
NONCLIENT_REPORT_ROUTES = _tuple_routes("open_loop_nonclient_routes")
CHANNEL_DIR_FILES = [
    HERMES_HOME / "channel_directory.json",
    HERMES_ROOT / "channel_directory.json",
    HERMES_ROOT / "profiles" / "doc" / "channel_directory.json",
]
# Direct group-topic re-engagement is disabled by default.
# If a group/topic needs automated follow-up, add an explicit internal review route above.
DIRECT_GROUP_REENGAGE_CHAT_IDS = set()
DIRECT_GROUP_REENGAGE_TOPICS = set()
_CHANNEL_CACHE = None


def _load_telegram_directory_index():
    global _CHANNEL_CACHE
    if _CHANNEL_CACHE is not None:
        return _CHANNEL_CACHE

    index = {}
    for path in CHANNEL_DIR_FILES:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data.get("platforms", {}).get("telegram", []):
                entry_id = str(entry.get("id") or "").strip()
                if entry_id:
                    index[entry_id] = entry
        except Exception as e:
            logger.warning("open-loop-detector: failed loading %s: %s", path, e)

    _CHANNEL_CACHE = index
    return index


def _lookup_channel_entry(raw_chat_id, thread_id=None):
    target = f"{raw_chat_id}:{thread_id}" if thread_id else str(raw_chat_id)
    return _load_telegram_directory_index().get(target)


def _is_system_text(text):
    text = (text or "").strip()
    return text.startswith("[SYSTEM]")


def _is_x_auto_research_system_text(text):
    return "[SYSTEM: Auto-triggered x-link-auto-research" in (text or "")


def _is_watchdog_alert_reply(text):
    text = (text or "").strip()
    return "Announce Watchdog Alert" in text or "Unauthorized message attempt" in text or "possible flapping" in text


_NONFINAL_ASSISTANT_MESSAGES = {
    "working...",
    "working…",
    "let me check if i already have boone and logan details anywhere before asking you again.",
}

_ACTIONABLE_PHRASES = (
    "can you",
    "could you",
    "would you",
    "please",
    "help",
    "check",
    "look",
    "fix",
    "review",
    "send",
    "schedule",
    "set up",
    "setup",
    "why",
    "what",
    "how",
    "when",
    "where",
    "let me know",
    "are you able",
    "i need",
    "need you",
    "can we",
    "could we",
)

_AGENT_WAIT_PATTERNS = (
    "let me know",
    "send me",
    "i need",
    "need the",
    "need your",
    "what's the",
    "what is the",
    "can you send",
    "could you send",
    "want me to",
)

_CLOSURE_PHRASES = (
    "we're good",
    "were good",
    "we are good",
    "all good",
    "looks good",
    "look good",
    "sounds good",
    "sound good",
    "got it",
    "understood",
    "no worries",
    "never mind",
    "nevermind",
    "resolved",
    "fixed now",
    "works now",
    "working now",
    "thank you",
    "thanks",
    "thx",
    "perfect",
    "awesome",
)


def _looks_like_ack_or_closure(text):
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _CLOSURE_PHRASES):
        return True
    if len(normalized) <= 24 and normalized in {"ok", "okay", "kk", "gotcha", "cool", "great"}:
        return True
    return False


def _normalized_text(text):
    return " ".join((text or "").strip().lower().split())


def _looks_actionable_message(text):
    normalized = _normalized_text(text)
    if not normalized:
        return False
    if (_is_system_text(text) and not _is_x_auto_research_system_text(text)) or _is_watchdog_alert_reply(text):
        return False
    if _looks_like_ack_or_closure(text):
        return False
    if "?" in (text or ""):
        return True
    if any(phrase in normalized for phrase in _ACTIONABLE_PHRASES):
        return True
    return len(normalized.split()) >= 6


def _is_nonfinal_assistant_message(text):
    return _normalized_text(text) in _NONFINAL_ASSISTANT_MESSAGES


def _assistant_waiting_on_user(text):
    normalized = _normalized_text(text)
    if not normalized:
        return False
    if "?" in (text or ""):
        return True
    return any(pattern in normalized for pattern in _AGENT_WAIT_PATTERNS)


def _find_latest_unresolved_followup(messages):
    meaningful = [
        m
        for m in messages
        if not (
            (
                _is_system_text(_row_value(m, "text", ""))
                and not _is_x_auto_research_system_text(_row_value(m, "text", ""))
            )
            or _is_watchdog_alert_reply(_row_value(m, "text", ""))
        )
    ]
    if not meaningful:
        return None
    saw_final_assistant = False
    for msg in reversed(meaningful[-MSGS_PER_CHAT:]):
        role = _row_value(msg, "role", "")
        text = _row_value(msg, "text", "") or ""
        if role == "assistant":
            if _is_nonfinal_assistant_message(text):
                continue
            saw_final_assistant = True
            continue
        if not _looks_actionable_message(text):
            continue
        if saw_final_assistant:
            return None
        return msg
    return None


def _load_telegram_scope_config() -> tuple[bool, set[str]]:
    config_path = HERMES_HOME / "config.yaml"
    try:
        if not config_path.exists() and "HERMES_ROOT" in globals():
            fallback = HERMES_ROOT / "config.yaml"
            if fallback.exists():
                config_path = fallback
        if not config_path.exists():
            return False, set()
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        telegram_cfg = data.get("telegram") or {}
        require_mention = bool(telegram_cfg.get("require_mention", False))
        raw = telegram_cfg.get("free_response_chats")
        if raw is None:
            raw = telegram_cfg.get("free_response_channels")
        if isinstance(raw, list):
            allowed = {str(part).strip() for part in raw if str(part).strip()}
        else:
            allowed = {part.strip() for part in str(raw or "").split(",") if part.strip()}
        return require_mention, allowed
    except Exception as e:
        logger.warning("open-loop-detector: failed loading telegram scope config: %s", e)
        return False, set()


def _has_profile_breadcrumb_for(chat_id: str, thread_id: str | None) -> bool:
    """Return True when this profile's active-breadcrumbs file has any entry
    for the given chat — proof that the profile has engaged here even if the
    chat isn't listed in free_response_channels (mention-gated groups like
    Bot Doctor). Added 2026-04-18 to catch stalls in out-of-allowlist chats."""
    try:
        bf = HERMES_HOME / "state" / "active-breadcrumbs.json"
        if not bf.exists():
            return False
        data = json.loads(bf.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        key = f"telegram:{chat_id}"
        if thread_id:
            key += f":{thread_id}"
        return isinstance(data.get(key), dict)
    except Exception:
        return False


def _target_in_profile_scope(chat_id: str, thread_id: str | None) -> bool:
    if not str(chat_id).startswith("-"):
        return True
    require_mention, allowed = _load_telegram_scope_config()
    if thread_id:
        thread_key = f"{chat_id}:{thread_id}"
        if thread_key in allowed:
            return True
    if str(chat_id) in allowed:
        return True
    # Breadcrumb override: if this profile has engaged in the chat (breadcrumb
    # file has an entry), treat as in-scope regardless of free_response_channels.
    if _has_profile_breadcrumb_for(chat_id, thread_id):
        return True
    if require_mention and not allowed:
        return False
    if allowed:
        return False
    return True


def _is_automated_target(raw_chat_id, thread_id=None):
    transcript_id = f"telegram:{raw_chat_id}"
    if thread_id:
        transcript_id += f":{thread_id}"
    if transcript_id in EXCLUDED_CHATS:
        return False

    if _target_in_profile_scope(raw_chat_id, thread_id):
        return True

    entry = _lookup_channel_entry(raw_chat_id, thread_id)
    if str(raw_chat_id).startswith("-") and entry is None:
        return False
    if entry and "INTERNAL" in str(entry.get("name") or "").upper():
        return False
    return True


# ── DB queries ──────────────────────────────────────────────────────


def _session_key_to_transcript_id(session_key):
    if not session_key:
        return None
    parts = str(session_key).split(":")
    try:
        idx = parts.index("telegram")
    except ValueError:
        return None
    if idx + 2 >= len(parts):
        return None
    kind = parts[idx + 1]
    if kind == "dm":
        return f"telegram:{parts[idx + 2]}"
    if kind == "group":
        chat_id = parts[idx + 2]
        thread_id = parts[idx + 3] if idx + 3 < len(parts) else None
        return f"telegram:{chat_id}:{thread_id}" if thread_id else f"telegram:{chat_id}"
    return None


def _content_to_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return " ".join(p for p in parts if p).strip()
    return str(content)


def _messages_need_attention(messages):
    last_user_idx = None
    last_answer_idx = None
    for idx, msg in enumerate(messages or []):
        role = msg.get("role") or msg.get("type")
        text = _content_to_text(msg.get("content") or msg.get("text")).strip()
        if not text:
            continue
        if role == "user" and not (_is_system_text(text) and not _is_x_auto_research_system_text(text)):
            last_user_idx = idx
        elif role == "assistant" and text not in _NONFINAL_ASSISTANT_MESSAGES:
            last_answer_idx = idx
    return last_user_idx is not None and (last_answer_idx is None or last_answer_idx < last_user_idx)


def _durable_rows_by_chat(cutoff_epoch=None, limit=MAX_CHATS):
    if not DURABLE_DB.exists():
        return {}
    rows_by_chat = {}
    try:
        conn = sqlite3.connect(str(DURABLE_DB))
        conn.row_factory = sqlite3.Row
        params = []
        where = "WHERE th.source LIKE '%TELEGRAM%' AND th.session_key IS NOT NULL"
        if cutoff_epoch is not None:
            where += " AND t.created_at >= ?"
            params.append(int(cutoff_epoch))
        rows = conn.execute(
            f"""
            SELECT th.session_key, t.state_json, t.created_at
            FROM agent_threads th
            JOIN agent_turns t ON t.resume_chain_id = th.resume_chain_id
            {where}
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (*params, int(limit) * 8),
        ).fetchall()
        conn.close()
        for row in rows:
            transcript_id = _session_key_to_transcript_id(row["session_key"])
            if not transcript_id or transcript_id in rows_by_chat:
                continue
            try:
                state = json.loads(row["state_json"] or "{}")
            except Exception:
                continue
            messages = state.get("messages") or []
            if _messages_need_attention(messages):
                rows_by_chat[transcript_id] = {"messages": messages, "created_at": row["created_at"]}
            if len(rows_by_chat) >= limit:
                break
    except Exception as e:
        logger.error("open-loop-detector: durable query failed: %s", e)
    return rows_by_chat


def _durable_last_messages(chat_id, limit=MSGS_PER_CHAT):
    item = _durable_rows_by_chat(cutoff_epoch=None, limit=MAX_CHATS).get(chat_id)
    if not item:
        return []
    created_at = datetime.fromtimestamp(int(item.get("created_at") or time.time()), timezone.utc).isoformat()
    out = []
    for msg in item.get("messages") or []:
        role = msg.get("role") or msg.get("type") or ""
        text = _content_to_text(msg.get("content") or msg.get("text")).strip()
        if not text or role == "tool":
            continue
        out.append(
            {
                "timestamp": created_at,
                "sender_name": msg.get("name") or "",
                "role": role,
                "text": text,
            }
        )
    return out[-limit:]


def _get_recent_chats():
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    cutoff = cutoff_dt.isoformat()
    chat_ids = []
    if TRANSCRIPT_DB.exists():
        try:
            conn = sqlite3.connect(str(TRANSCRIPT_DB))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT chat_id, MAX(timestamp) AS last_ts "
                "FROM telegram_messages WHERE timestamp >= ? "
                "GROUP BY chat_id ORDER BY last_ts DESC LIMIT ?",
                (cutoff, MAX_CHATS),
            ).fetchall()
            conn.close()
            chat_ids.extend(r["chat_id"] for r in rows)
        except Exception as e:
            logger.error("open-loop-detector: query chats failed: %s", e)
    durable = _durable_rows_by_chat(cutoff_epoch=cutoff_dt.timestamp(), limit=MAX_CHATS)
    for cid in durable:
        if cid not in chat_ids:
            chat_ids.append(cid)
    return chat_ids[:MAX_CHATS]


def _get_last_messages(chat_id, limit=MSGS_PER_CHAT):
    if TRANSCRIPT_DB.exists():
        try:
            conn = sqlite3.connect(str(TRANSCRIPT_DB))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, sender_name, role, text FROM telegram_messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
            conn.close()
            if rows:
                return list(reversed(rows))
        except Exception:
            pass
    return _durable_last_messages(chat_id, limit=limit)


def _should_skip(chat_id, messages):
    """Skip chats that are empty, system-only, or not safe to automate."""
    if not messages:
        return True

    raw_chat_id, thread_id = _parse_chat_id(chat_id)
    if not raw_chat_id or not _is_automated_target(raw_chat_id, thread_id):
        return True

    latest_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
    if latest_user and (
        (_is_system_text(latest_user["text"]) and not _is_x_auto_research_system_text(latest_user["text"]))
        or _is_watchdog_alert_reply(latest_user["text"])
        or _looks_like_ack_or_closure(latest_user["text"])
    ):
        return True

    non_system_user_messages = [
        m
        for m in messages
        if m["role"] == "user"
        and not (_is_system_text(m["text"]) and not _is_x_auto_research_system_text(m["text"]))
        and not _is_watchdog_alert_reply(m["text"])
        and not _looks_like_ack_or_closure(m["text"])
    ]
    if not non_system_user_messages:
        return True
    return False


# ── LLM classification ──────────────────────────────────────────────


def _format_for_llm(chat_id, messages):
    lines = [f"### Chat: {chat_id}"]
    for m in messages:
        label = m["sender_name"] or ("User" if m["role"] == "user" else "Agent")
        text = (m["text"] or "")[:2000]
        lines.append(f"[{m['role']}] {label}: {text}")
    return "\n".join(lines)


def _resolve_env_key(key):
    """Resolve an env var, falling back to HERMES_HOME/.env file."""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    env_file = HERMES_HOME / ".env"
    if not env_file.exists():
        env_file = HERMES_ROOT / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip("'").strip('"')
        except Exception:
            pass
    return ""


def _get_classify_config():
    """Read model, base_url, and api_key from config.yaml auxiliary.approval or gateway default."""
    try:
        config_path = HERMES_HOME / "config.yaml"
        if not config_path.exists():
            config_path = HERMES_ROOT / "config.yaml"
        if not config_path.exists():
            return None, None, None
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

        # Try auxiliary.approval first (cheap classification model)
        aux = cfg.get("auxiliary", {})
        for key in ("approval", "compression", "session_search"):
            section = aux.get(key, {})
            if section.get("model") and section.get("base_url"):
                model = section["model"]
                base_url = section["base_url"].rstrip("/")
                api_key = section.get("api_key", "")
                if not api_key:
                    env_key = section.get("api_key_env", "")
                    if env_key:
                        api_key = _resolve_env_key(env_key)
                if model and base_url:
                    return model, base_url, api_key or "no-key"
                break

        # Fall back to gateway default model
        model = cfg.get("model", {})
        default_model = model.get("default", "")
        base_url = model.get("base_url", "")
        api_key = ""
        api_key_env = model.get("api_key_env", "")
        if api_key_env:
            api_key = _resolve_env_key(api_key_env)
        if not api_key:
            api_key = model.get("api_key", "no-key")
        if default_model and base_url:
            return default_model, base_url.rstrip("/"), api_key
    except Exception as e:
        logger.warning("open-loop-detector: failed reading config: %s", e)
    return None, None, None


def _extract_text_blocks(payload):
    # OpenAI chat completions format
    choices = payload.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return (msg.get("content") or "").strip()
    # Fallback: try Anthropic format for backwards compat
    parts = []
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            value = (block.get("text") or "").strip()
            if value:
                parts.append(value)
    return "\n".join(parts).strip()


def _extract_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return ""


async def _classify(chat_data):
    """Classify recent chats via configured model with a repair retry for non-JSON replies."""
    if not chat_data:
        return {}

    model, base_url, api_key = _get_classify_config()
    if not model or not base_url:
        logger.warning("open-loop-detector: no classification model configured, skipping")
        return {}
    if not api_key or api_key == "no-key":
        logger.info("open-loop-detector: classification API key missing, using heuristic fallback")
        return {}

    chat_ids = [cid for cid, _ in chat_data]
    allowed_statuses = ["RESOLVED", "WAITING_ON_USER", "WAITING_ON_AGENT", "STUCK"]
    chats_text = "\n\n".join(_format_for_llm(cid, msgs) for cid, msgs in chat_data)
    system_prompt = (
        "You are a strict JSON classifier for conversation state. "
        "Never ask follow-up questions. Never explain. Never emit markdown. "
        "Return exactly one JSON object and nothing else. "
        "Every key must be one of the provided chat IDs. Every value must be exactly one of "
        "RESOLVED, WAITING_ON_USER, WAITING_ON_AGENT, or STUCK. "
        "Default to RESOLVED when a substantive recent assistant message addressed the latest user ask. "
        "Use STUCK ONLY when there is clear evidence of an error, exception, repeated failure, or stall — never as a fallback for ambiguity. "
        "If you cannot confidently distinguish between RESOLVED and WAITING_ON_AGENT, choose RESOLVED."
    )
    prompt = f"""Analyze these Telegram conversations between users and an AI agent.
For each conversation, classify as exactly ONE of:
- RESOLVED — the most recent substantive assistant message addressed the user's latest ask, OR the last user message is a short acknowledgement/closure ("thanks", "got it", "ok"), OR the user has not replied to a substantive agent answer. A thorough reply on-target closes the loop; the absence of a user follow-up is NOT an open loop.
- WAITING_ON_USER — the agent's most recent substantive message asks a specific question, lists options for the user to choose, or otherwise puts the ball in the user's court.
- WAITING_ON_AGENT — the user's most recent message contains a concrete request, question, or follow-up that the agent has NOT addressed with a substantive reply. A placeholder/non-final assistant line like "Working..." does NOT count as addressing it.
- STUCK — the conversation contains a clear error, exception, repeated failure, or visible stall (e.g., "Connection error", "auth failure", repeated retries with no progress, conflicting status updates). Do not use STUCK as a generic ambiguity bucket.

Decision procedure:
1. Find the most recent user message in the last 12 messages.
2. If it is acknowledgement/closure (short "ok / thanks / got it / sounds good / nice") → RESOLVED.
3. Otherwise, find the most recent substantive assistant message (>1 sentence, not "Working..."). If it materially addresses the user's last ask → RESOLVED.
4. If the user's ask is unanswered or only met with placeholders → WAITING_ON_AGENT.
5. If the most recent substantive message is the agent asking the user something → WAITING_ON_USER.
6. STUCK only when the transcript shows actual errors/failures/loops, not just a long reply you can't fully parse.

Required chat IDs:
{json.dumps(chat_ids)}

Return exactly one JSON object that maps each required chat_id to exactly one allowed status. No prose.
Example: {{"telegram:123": "RESOLVED", "telegram:456:789": "WAITING_ON_AGENT"}}

{chats_text}"""

    def _normalize(result):
        if not isinstance(result, dict):
            return {}
        normalized = {}
        for cid in chat_ids:
            value = result.get(cid)
            if isinstance(value, str) and value in allowed_statuses:
                normalized[cid] = value
        return normalized

    async def _post(messages, max_tokens=1024):
        # OpenAI-compatible chat completions API
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": full_messages,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                },
            )

    try:
        resp = await _post([{"role": "user", "content": prompt}])
        if resp.status_code in (401, 403):
            logger.warning("open-loop-detector: auth error %s, skipping audit", resp.status_code)
            return None
        if resp.status_code == 429:
            logger.info("open-loop-detector: rate-limited, skipping this audit cycle")
            return None
        if resp.status_code != 200:
            logger.warning("open-loop-detector: API %s: %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        content = _extract_text_blocks(data)
        json_text = _extract_json_object(content)
        if json_text:
            normalized = _normalize(json.loads(json_text))
            if normalized:
                return normalized

        logger.info("open-loop-detector: classifier returned non-JSON output; attempting repair pass")

        repair_prompt = (
            "Convert the following classifier output into exactly one JSON object with the required chat IDs and allowed statuses only. "
            "Do not ask questions. If the text is ambiguous, use RESOLVED for the affected chat IDs (a substantive reply that may or may not have closed the loop is more likely resolved than stuck — never default to STUCK on ambiguity).\n\n"
            f"Required chat IDs: {json.dumps(chat_ids)}\n"
            f"Allowed statuses: {json.dumps(allowed_statuses)}\n"
            f"Text to convert:\n{content}"
        )
        repair = await _post([{"role": "user", "content": repair_prompt}], max_tokens=512)
        if repair.status_code != 200:
            logger.warning("open-loop-detector: repair pass API %s: %s", repair.status_code, repair.text[:200])
            return {}
        repair_data = repair.json()
        repair_content = _extract_text_blocks(repair_data)
        repair_json = _extract_json_object(repair_content)
        if not repair_json:
            logger.info("open-loop-detector: repair pass also returned non-JSON output; using heuristic fallback")
            return {}
        normalized = _normalize(json.loads(repair_json))
        if normalized:
            return normalized
        logger.info("open-loop-detector: repair pass returned invalid classifications; using heuristic fallback")
        return {}
    except Exception as e:
        logger.error("open-loop-detector: classify failed: %s", e)
        return {}


# ── Re-engagement ───────────────────────────────────────────────────


def _parse_chat_id(transcript_id):
    parts = transcript_id.split(":")
    if len(parts) < 2:
        return None, None
    return parts[1], parts[2] if len(parts) > 2 else None


def _row_value(message, key, default=None):
    if isinstance(message, sqlite3.Row):
        try:
            return message[key]
        except Exception:
            return default
    if isinstance(message, dict):
        return message.get(key, default)
    return default


def _heuristic_classify(chat_data):
    classifications = {}
    for chat_id, messages in chat_data:
        meaningful = [
            m
            for m in messages
            if not (
                (
                    _is_system_text(_row_value(m, "text", ""))
                    and not _is_x_auto_research_system_text(_row_value(m, "text", ""))
                )
                or _is_watchdog_alert_reply(_row_value(m, "text", ""))
            )
        ]
        if not meaningful:
            continue

        window = meaningful[-MSGS_PER_CHAT:]
        unresolved = _find_latest_unresolved_followup(window)
        if unresolved is not None:
            classifications[chat_id] = "WAITING_ON_AGENT"
            continue

        last = window[-1]
        last_role = _row_value(last, "role")
        last_text = _row_value(last, "text", "") or ""
        recent_texts = [(_row_value(m, "text", "") or "").lower() for m in window[-3:]]

        if last_role == "assistant" and _assistant_waiting_on_user(last_text):
            classifications[chat_id] = "WAITING_ON_USER"
        elif any("error" in text or "failed" in text or "exception" in text for text in recent_texts):
            classifications[chat_id] = "STUCK"
        else:
            classifications[chat_id] = "RESOLVED"
    return classifications


def _client_review_route(raw_chat_id, thread_id=None):
    if not thread_id:
        return None
    internal_thread = CLIENT_REVIEW_ROUTES.get((str(raw_chat_id), str(thread_id)))
    if not internal_thread:
        return None
    return INTERNAL_REVIEW_CHAT_ID, internal_thread


def _report_review_route(raw_chat_id, thread_id=None):
    if not thread_id:
        return None
    report_thread = NONCLIENT_REPORT_ROUTES.get((str(raw_chat_id), str(thread_id)))
    if not report_thread:
        return None
    return str(raw_chat_id), report_thread


def _latest_message_timestamp(transcript_chat_id):
    try:
        conn = sqlite3.connect(str(TRANSCRIPT_DB))
        row = conn.execute(
            "SELECT MAX(timestamp) FROM telegram_messages WHERE chat_id = ?",
            (transcript_chat_id,),
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        return str(row[0])
    except Exception:
        return None


def _hours_since_latest_message(transcript_chat_id):
    latest = _latest_message_timestamp(transcript_chat_id)
    if not latest:
        return None
    try:
        last_ts = datetime.fromisoformat(latest)
        return max(0.0, (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0)
    except Exception:
        return None


async def _resolve_delivery_target(adapter, transcript_chat_id, chat_id, thread_id=None):
    target_chat_id = str(chat_id)
    target_thread_id = str(thread_id) if thread_id else None
    info = await adapter.get_chat_info(target_chat_id)
    if not info.get("error"):
        return target_chat_id, target_thread_id

    age_hours = _hours_since_latest_message(transcript_chat_id)
    suffix = f":{target_thread_id}" if target_thread_id else ""
    if age_hours is not None and age_hours > MAX_AGE_HOURS:
        logger.info(
            "open-loop-detector: skipping stale inaccessible target telegram:%s%s (%s, last activity %.1fh ago)",
            target_chat_id,
            suffix,
            info.get("error"),
            age_hours,
        )
        return None, None

    if age_hours is None:
        logger.warning(
            "open-loop-detector: target telegram:%s%s inaccessible (%s) and no transcript age is available; skipping until an explicit route exists",
            target_chat_id,
            suffix,
            info.get("error"),
        )
    else:
        logger.warning(
            "open-loop-detector: target telegram:%s%s inaccessible (%s, last activity %.1fh ago); skipping until an explicit route exists",
            target_chat_id,
            suffix,
            info.get("error"),
            age_hours,
        )
    return None, None


# ── Content-aware re-engagement helpers ────────────────────────────


def _open_tasks_for_chat(raw_chat_id, thread_id=None, limit=5):
    """Return active task-ledger rows tied to this chat."""
    if not TASK_LEDGER_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(TASK_LEDGER_DB))
        conn.row_factory = sqlite3.Row
        query = (
            "SELECT id, ask, expected_artifact, opened_at, status FROM tasks "
            "WHERE status IN ('open','in_progress','blocked') AND chat_id = ?"
        )
        params = [str(raw_chat_id)]
        if thread_id:
            query += " AND (thread_id = ? OR thread_id IS NULL OR thread_id = '')"
            params.append(str(thread_id))
        query += " ORDER BY opened_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("open-loop-detector: task-ledger query failed for %s: %s", raw_chat_id, e)
        return []


def _active_breadcrumb_for_chat(raw_chat_id, thread_id=None):
    """Return the active-breadcrumb dict for this chat, if any."""
    try:
        if not BREADCRUMBS_FILE.exists():
            return None
        data = json.loads(BREADCRUMBS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        key = f"telegram:{raw_chat_id}"
        if thread_id:
            key += f":{thread_id}"
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("status") == "active":
            return entry
    except Exception as e:
        logger.warning("open-loop-detector: breadcrumb read failed for %s: %s", raw_chat_id, e)
    return None


def _last_user_ask_from_messages(messages):
    """Return the most recent substantive user message text (skips system/watchdog/closure)."""
    if not messages:
        return None
    for msg in reversed(messages):
        role = _row_value(msg, "role", "")
        text = _row_value(msg, "text", "") or ""
        if role != "user":
            continue
        if (_is_system_text(text) and not _is_x_auto_research_system_text(text)) or _is_watchdog_alert_reply(text):
            continue
        if _looks_like_ack_or_closure(text):
            continue
        return text.strip()
    return None


def _build_reengage_payload(
    classification, raw_chat_id, thread_id, messages, route_kind=None, original_chat_id=None, original_thread_id=None
):
    """Build a content-aware [SYSTEM] re-engagement payload.

    Replaces the old generic 'review your history' boilerplate that produced
    placeholder 'I owe you a follow-up' messages. Carries the actual owed work
    so the agent executes it instead of narrating about it.
    """
    last_ask = _last_user_ask_from_messages(messages)
    breadcrumb = _active_breadcrumb_for_chat(raw_chat_id, thread_id)
    open_tasks = _open_tasks_for_chat(raw_chat_id, thread_id)

    # Internal-review routes are different: a meta-topic asking the agent to
    # review a peer thread, not resume the work itself. Keep their original
    # framing but tighten the silence rules.
    if route_kind == "client_review":
        verb = "stalled or hit an error" if classification == "STUCK" else "has unfinished work"
        parts = [
            f"[SYSTEM] Client thread telegram:{original_chat_id}:{original_thread_id} {verb}.",
            "Review that client thread's recent history and prepare any needed client-facing follow-up in this internal topic for Deacon approval. Do not post directly to the client thread.",
        ]
        if last_ask:
            parts.append(f"Client's latest message:\n> {last_ask[:1000]}")
        parts.append(
            "If the client thread already has a substantive on-target reply and the client has not pinged for more, respond with exactly [SILENT]."
        )
        return "\n\n".join(parts)

    if route_kind == "report_review":
        verb = "stalled or hit an error" if classification == "STUCK" else "has unfinished work"
        parts = [
            f"[SYSTEM] Conversation telegram:{original_chat_id}:{original_thread_id} {verb}.",
            "Review the source thread's recent history here and emit at most one short operator note only if there is a concrete unresolved delta right now.",
            "If the source thread is already complete, delivered, acknowledged, or the only issue is transcript/history indexing lag, respond with exactly [SILENT].",
            "Do not restate prior completion claims and do not retry delivery.",
        ]
        if last_ask:
            parts.append(f"Latest user message in source thread:\n> {last_ask[:1000]}")
        return "\n\n".join(parts)

    # Direct re-engagement in the original chat — the "drive work forward" path.
    header = (
        "[SYSTEM] Open-loop detector flagged this conversation as needing action "
        f"(classification={classification}). "
        "Execute the unfinished work below. Do NOT reply with status, "
        "acknowledgement, or 'I owe you a follow-up' — produce the artifact "
        "or fix the issue and reply with the actual result."
    )
    parts = [header]

    if last_ask:
        parts.append(f"User's latest unanswered ask:\n> {last_ask[:1500]}")

    if breadcrumb:
        prompt_text = (breadcrumb.get("last_user_message") or "").strip()
        started = breadcrumb.get("started_at") or ""
        bc_lines = ["In-flight work you already started on this chat:"]
        if started:
            bc_lines.append(f"- Started at: {started}")
        if prompt_text and prompt_text != last_ask:
            bc_lines.append(f"- Original prompt: {prompt_text[:1500]}")
        if len(bc_lines) > 1:
            parts.append("\n".join(bc_lines))

    if open_tasks:
        task_lines = ["Open task-ledger items for this chat:"]
        for t in open_tasks:
            tid = t.get("id", "")
            ask = (t.get("ask") or "").strip()
            artifact = (t.get("expected_artifact") or "").strip()
            line = f"- [{tid}] {ask[:300]}"
            if artifact:
                line += f"  (expects: {artifact[:200]})"
            task_lines.append(line)
        parts.append("\n".join(task_lines))

    closure = (
        "Self-correction rule: if you check this conversation and find that "
        "you already delivered a substantive on-target reply that addressed the "
        "user's last ask, and the user has not pinged again, respond with exactly "
        "[SILENT] to close the loop. A thorough reply CLOSES the loop — do not "
        "generate a placeholder nudge or 'follow-up incoming' message."
    )
    parts.append(closure)

    return "\n\n".join(parts)


async def _reengage(runner, transcript_chat_id, classification, messages=None):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    raw_chat_id, thread_id = _parse_chat_id(transcript_chat_id)
    if not raw_chat_id:
        return

    # Read-only mode: classify and write state but never inject [SYSTEM]
    # messages. Honored when HERMES_OPEN_LOOP_READ_ONLY=1 in the environment.
    if os.environ.get("HERMES_OPEN_LOOP_READ_ONLY", "").strip() in ("1", "true", "TRUE", "yes"):
        logger.info(
            "open-loop-detector: READ_ONLY — would re-engage %s (%s); skipping send",
            transcript_chat_id,
            classification,
        )
        return

    original_chat_id = str(raw_chat_id)
    original_thread_id = str(thread_id) if thread_id else None
    client_review_route = _client_review_route(original_chat_id, original_thread_id)
    report_review_route = _report_review_route(original_chat_id, original_thread_id)
    route_kind = None

    if client_review_route:
        routed_chat_id, routed_thread_id = client_review_route
        route_kind = "client_review"
    elif report_review_route:
        routed_chat_id, routed_thread_id = report_review_route
        route_kind = "report_review"
    else:
        routed_chat_id = routed_thread_id = None

    if route_kind:
        if not _target_in_profile_scope(routed_chat_id, routed_thread_id):
            logger.info(
                "open-loop-detector: skipping reroute outside profile scope %s -> telegram:%s:%s",
                transcript_chat_id,
                routed_chat_id,
                routed_thread_id,
            )
            return
        logger.info(
            "open-loop-detector: rerouting %s to %s topic telegram:%s:%s",
            transcript_chat_id,
            route_kind,
            routed_chat_id,
            routed_thread_id,
        )
        raw_chat_id = routed_chat_id
        thread_id = routed_thread_id
    elif not _is_automated_target(raw_chat_id, thread_id):
        logger.info("open-loop-detector: skipping invalid/internal target %s", transcript_chat_id)
        return

    adapter = runner.adapters.get(Platform("telegram"))
    if not adapter:
        return

    delivery_chat_id, delivery_thread_id = await _resolve_delivery_target(
        adapter,
        transcript_chat_id,
        raw_chat_id,
        thread_id,
    )
    if not delivery_chat_id:
        return

    is_group = delivery_thread_id or str(delivery_chat_id).startswith("-")

    source = SessionSource(
        platform=Platform("telegram"),
        chat_id=str(delivery_chat_id),
        chat_type="group" if is_group else "dm",
        user_id=str(delivery_chat_id),
        user_name="system" if is_group else None,
        thread_id=str(delivery_thread_id) if delivery_thread_id else None,
    )

    text = _build_reengage_payload(
        classification,
        raw_chat_id,
        thread_id,
        messages or [],
        route_kind=route_kind,
        original_chat_id=original_chat_id,
        original_thread_id=original_thread_id,
    )

    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
    )

    try:
        await adapter.handle_message(event)
        logger.info("open-loop-detector: re-engaged %s (%s)", transcript_chat_id, classification)
    except Exception as e:
        logger.error("open-loop-detector: re-engage %s failed: %s", transcript_chat_id, e)


# ── Main audit ──────────────────────────────────────────────────────


async def _run_audit(runner):
    logger.info("open-loop-detector: starting audit")

    chat_ids = _get_recent_chats()
    if not chat_ids:
        logger.info("open-loop-detector: no recent chats")
        return

    chat_data = []
    for cid in chat_ids:
        if cid in EXCLUDED_CHATS:
            logger.debug("open-loop-detector: skipping excluded chat %s", cid)
            continue
        msgs = _get_last_messages(cid)
        if _should_skip(cid, msgs):
            logger.debug("open-loop-detector: skipping non-actionable chat %s", cid)
            continue
        chat_data.append((cid, msgs))

    if not chat_data:
        logger.info("open-loop-detector: all %d chats resolved or non-actionable", len(chat_ids))
        return

    logger.info("open-loop-detector: classifying %d chats", len(chat_data))
    classifications = await _classify(chat_data)

    if classifications is None:
        return

    if not classifications:
        logger.warning("open-loop-detector: empty classification result; using heuristic fallback")
        classifications = _heuristic_classify(chat_data)
        if not classifications:
            return

    actionable = {cid: s for cid, s in classifications.items() if s in ("WAITING_ON_AGENT", "STUCK")}

    previous_state = {}
    try:
        if STATE_FILE.exists():
            previous_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        previous_state = {}
    previous_reengaged = previous_state.get("reengaged") or {}

    if not actionable:
        logger.info("open-loop-detector: no open loops (all resolved or waiting on user)")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "classifications": classifications,
                    "actionable": {},
                    "suppressed": {},
                    "reengaged": previous_reengaged,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return

    next_reengaged = {}
    suppressed = {}

    for cid, status in list(actionable.items()):
        latest_ts = _latest_message_timestamp(cid)
        signature = f"{status}|{latest_ts or 'none'}"
        prior = previous_reengaged.get(cid) or {}
        if prior.get("signature") == signature:
            suppressed[cid] = status
            logger.info(
                "open-loop-detector: suppressing duplicate re-engagement for %s (%s, no new transcript activity)",
                cid,
                status,
            )
            continue
        next_reengaged[cid] = {
            "signature": signature,
            "classification": status,
            "latest_message_ts": latest_ts,
        }

    actionable_to_send = {cid: status for cid, status in actionable.items() if cid not in suppressed}

    if not actionable_to_send:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "classifications": classifications,
                    "actionable": actionable,
                    "suppressed": suppressed,
                    "reengaged": previous_reengaged,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("open-loop-detector: all actionable loops suppressed as duplicates")
        return

    logger.info(
        "open-loop-detector: %d open loops: %s",
        len(actionable_to_send),
        ", ".join(f"{c}={s}" for c, s in actionable_to_send.items()),
    )

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "classifications": classifications,
                "actionable": actionable,
                "suppressed": suppressed,
                "reengaged": next_reengaged,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    chat_data_by_id = {cid: msgs for cid, msgs in chat_data}
    for cid, status in actionable_to_send.items():
        await _reengage(runner, cid, status, messages=chat_data_by_id.get(cid))
        await asyncio.sleep(2)


# ── Hook entry point ────────────────────────────────────────────────


async def handle(event_type: str, context: dict) -> None:
    if event_type != "gateway:startup":
        return

    runner = context.get("runner")
    if not runner:
        logger.debug("open-loop-detector: no runner in context")
        return

    async def _loop():
        # Initial delay — let startup and native recovery settle first
        await asyncio.sleep(STARTUP_DELAY)

        while True:
            try:
                await _run_audit(runner)
            except Exception as e:
                logger.error("open-loop-detector: audit error: %s", e)

            # Wait for next cycle
            await asyncio.sleep(REPEAT_INTERVAL)

    asyncio.create_task(_loop())
    logger.info("open-loop-detector: scheduled (first run in %ds, repeat every %ds)", STARTUP_DELAY, REPEAT_INTERVAL)
