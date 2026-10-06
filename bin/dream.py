#!/usr/bin/env python3
"""
dream.py — Session consolidation for Hermes (client-ready version).

Reads completed sessions from state.db, sends transcripts to the configured
model for fact extraction, appends results to MEMORY.md / USER.md.

Designed to run as a nightly cron job. Reads model config from config.yaml.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from native_memory_limits import char_limit_for_target, automatic_memory_review_enabled
except ModuleNotFoundError:  # Imported from the repository by tests.
    from bin.native_memory_limits import char_limit_for_target, automatic_memory_review_enabled

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DB = HERMES_HOME / "state.db"
MEMORY_DIR = HERMES_HOME / "memories"
CONSOLIDATED_FILE = HERMES_HOME / "state" / "consolidated.txt"
LOCK_FILE = HERMES_HOME / "state" / "dream.lock"
LOG_FILE = HERMES_HOME / "logs" / "dream.log"

def read_memory_entries(path):
    if not path.exists():
        return []
    return [e.strip() for e in path.read_text(encoding="utf-8").split("\n§\n") if e.strip()]


def render_memory_entries(entries):
    return "\n§\n".join(e.strip() for e in entries if e.strip())


def merge_entries_with_limit(existing, incoming, char_limit):
    entries = list(existing)
    added = 0
    for entry in incoming:
        if entry in entries:
            continue
        candidate = render_memory_entries(entries + [entry])
        if len(candidate) > char_limit:
            continue
        entries.append(entry)
        added += 1
    return entries, added


def write_memory_entries(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_memory_entries(entries), encoding="utf-8")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [dream] %(message)s",
)
logger = logging.getLogger("dream")

MAX_SESSIONS = 5
MAX_TRANSCRIPT_CHARS = 60000
MIN_MESSAGES = 3


def load_config():
    """Read model config from config.yaml for dream extraction."""
    import yaml
    config_path = HERMES_HOME / "config.yaml"
    if not config_path.exists():
        return None, None, None

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    providers = cfg.get("providers", {}) or {}
    env_file = HERMES_HOME / ".env"

    def resolve_section(section):
        if not isinstance(section, dict):
            return None, None, None
        model = str(section.get("model") or section.get("default") or "").strip()
        provider_name = str(section.get("provider") or "").strip()
        provider_cfg = providers.get(provider_name, {}) if provider_name else {}
        provider_cfg = provider_cfg if isinstance(provider_cfg, dict) else {}
        base_url = str(section.get("base_url") or section.get("api") or provider_cfg.get("api") or provider_cfg.get("base_url") or "").strip()
        if not base_url and provider_name == "openrouter":
            base_url = "https://openrouter.ai/api/v1"
        api_key = str(section.get("api_key") or provider_cfg.get("api_key") or "").strip()
        env_key = str(section.get("api_key_env") or provider_cfg.get("api_key_env") or "").strip()
        if not env_key and provider_name == "openrouter":
            env_key = "OPENROUTER_API_KEY"
        if not api_key and env_key:
            api_key = os.environ.get(env_key, "")
            if not api_key and env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith(f"{env_key}="):
                        api_key = line.split("=", 1)[1].strip().strip("'\"")
                        break
        if model and base_url and api_key:
            return model, base_url.rstrip("/"), api_key
        return None, None, None

    aux = cfg.get("auxiliary", {}) or {}
    for key in ("compression", "session_search", "approval"):
        model, base_url, api_key = resolve_section(aux.get(key, {}) or {})
        if model and base_url and api_key:
            return model, base_url, api_key

    model, base_url, api_key = resolve_section(cfg.get("model", {}) or {})
    return model, base_url, api_key


def get_consolidated_ids():
    """Read set of already-consolidated session IDs."""
    if not CONSOLIDATED_FILE.exists():
        return set()
    return set(CONSOLIDATED_FILE.read_text(encoding="utf-8").strip().splitlines())


def mark_consolidated(session_id):
    """Append session ID to consolidated list."""
    CONSOLIDATED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONSOLIDATED_FILE, "a", encoding="utf-8") as f:
        f.write(session_id + "\n")


def get_sessions_to_process():
    """Get recent sessions that haven't been consolidated yet."""
    if not STATE_DB.exists():
        return []

    db = sqlite3.connect(str(STATE_DB))
    db.row_factory = sqlite3.Row
    consolidated = get_consolidated_ids()

    cutoff = time.time() - 86400 * 3  # Last 3 days
    rows = db.execute(
        """SELECT id, source, started_at, ended_at, message_count
           FROM sessions
           WHERE started_at > ? AND source = 'telegram'
             AND message_count >= ?
           ORDER BY started_at DESC""",
        (cutoff, MIN_MESSAGES),
    ).fetchall()

    sessions = []
    for row in rows:
        if row["id"] in consolidated:
            continue
        if row["id"].startswith("cron_"):
            continue
        sessions.append(dict(row))
        if len(sessions) >= MAX_SESSIONS:
            break

    db.close()
    return sessions


def build_transcript(session_id):
    """Build a transcript string from session messages."""
    db = sqlite3.connect(str(STATE_DB))
    rows = db.execute(
        """SELECT role, content FROM messages
           WHERE session_id = ? AND role IN ('user', 'assistant')
             AND content IS NOT NULL AND content != ''
           ORDER BY timestamp""",
        (session_id,),
    ).fetchall()
    db.close()

    lines = []
    chars = 0
    for role, content in rows:
        text = content[:2000]
        line = f"[{role}] {text}"
        if chars + len(line) > MAX_TRANSCRIPT_CHARS:
            break
        lines.append(line)
        chars += len(line)

    return "\n\n".join(lines)


def extract_facts(transcript, model, base_url, api_key):
    """Call the configured model to extract facts from a transcript."""
    system_prompt = (
        "You are a memory extraction system. Read the conversation transcript "
        "and identify only explicit durable preferences or repeated corrections worth remembering. Return an empty array when none qualify. "
        "For each supported entry, classify it as either 'memory' (a compact pointer needed across tasks, "
        "not project facts) or 'user' (explicit durable preferences and repeated corrections). "
        "Do not infer habits, traits, or permanent rules from casual assent or frustration. "
        "Do not extract operating state, project updates, procedures, or completed tasks into native memory. "
        "Return ONLY a JSON array of objects with 'type' and 'fact' keys. No prose."
    )

    prompt = f"Extract facts from this conversation:\n\n{transcript}"

    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "temperature": 0.1,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code != 200:
        logger.warning("API error %d: %s", resp.status_code, resp.text[:200])
        return []

    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    # Parse JSON from response
    try:
        # Handle markdown code fences
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        facts = json.loads(content.strip())
        if isinstance(facts, list):
            return facts
    except (json.JSONDecodeError, IndexError):
        logger.warning("Failed to parse facts JSON: %s", content[:200])

    return []


def main():
    if not automatic_memory_review_enabled():
        print("Memory maintenance skipped: native automatic review is disabled or unavailable.")
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Dream cycle starting")

    model, base_url, api_key = load_config()
    if not model or not base_url or not api_key:
        logger.error("No model configured for dream extraction")
        return

    logger.info("Using model: %s at %s", model, base_url)

    sessions = get_sessions_to_process()
    if not sessions:
        CONSOLIDATED_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONSOLIDATED_FILE.touch()
        logger.info("No sessions to process")
        return

    logger.info("Processing %d sessions", len(sessions))

    date_prefix = datetime.now(timezone.utc).strftime("[%Y-%m-%d src:dream]")
    total_memory = 0
    total_user = 0

    for session in sessions:
        sid = session["id"]
        transcript = build_transcript(sid)
        if not transcript or len(transcript) < 100:
            mark_consolidated(sid)
            continue

        logger.info("Extracting facts from session %s (%d chars)", sid, len(transcript))
        facts = extract_facts(transcript, model, base_url, api_key)

        if not facts:
            mark_consolidated(sid)
            continue

        memory_facts = [f"{date_prefix} {f['fact']}" for f in facts if f.get("type") == "memory"]
        user_facts = [f"{date_prefix} {f['fact']}" for f in facts if f.get("type") == "user"]

        # Merge into MEMORY.md
        if memory_facts:
            mem_path = MEMORY_DIR / "MEMORY.md"
            existing = read_memory_entries(mem_path)
            merged, added = merge_entries_with_limit(
                existing, memory_facts, char_limit_for_target("memory")
            )
            if added > 0:
                write_memory_entries(mem_path, merged)
                total_memory += added
                logger.info("Session %s: +%d memory facts", sid, added)

        # Merge into USER.md
        if user_facts:
            user_path = MEMORY_DIR / "USER.md"
            existing = read_memory_entries(user_path)
            merged, added = merge_entries_with_limit(
                existing, user_facts, char_limit_for_target("user")
            )
            if added > 0:
                write_memory_entries(user_path, merged)
                total_user += added
                logger.info("Session %s: +%d user facts", sid, added)

        mark_consolidated(sid)

    logger.info("Dream cycle complete: +%d memory, +%d user facts", total_memory, total_user)


if __name__ == "__main__":
    main()
