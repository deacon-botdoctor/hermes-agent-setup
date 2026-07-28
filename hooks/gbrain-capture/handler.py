"""gbrain-capture hook — real-time agent turn outcomes into gBrain.

Fires after response delivery on the gateway's bounded agent:end callback.
For each event, consolidates the recent topic conversation (last 200
messages) from telegram-transcript.db
and writes the topic to gBrain as a single page with slug
`topic/<safe-name>-<chat_id>[-<thread_id>]`. The write is idempotent:
re-firing on the same topic just rewrites the page with the latest
conversation history.

This replaces the hourly cron-based ingestion (kit/bin/gbrain-transcript-
ingest.py) with event-driven real-time capture. The cron can stay as a
backstop for hosts where the hook is disabled, but the hook is the
primary path.

PII redaction matches hooks/telegram-transcript/handler.py: Telegram tokens,
OpenRouter/Anthropic/OpenAI keys, generic sk-* secrets all redacted before
content lands in gBrain.

Privacy boundary: ONLY runs for platform=telegram events with a valid
chat_id. Other platforms / event shapes are no-ops.

Performance note: the `gbrain put` subprocess is called with timeout=10
and runs synchronously inside the hook. Empirically <500ms on warm brains;
acceptable for the post-response window. If this becomes a perf concern,
spawn as background subprocess (the page write is fire-and-forget — no
agent decision depends on it).
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# === redaction (mirrors hooks/telegram-transcript/handler.py) ===
# Hooks execute inside the active Hermes process. Never prepend the nominal
# mutable checkout: immutable candidates may be running from a
# different root, and changing sys.path here can split one process across two
# incompatible runtime trees. Import from the active process path or use the
# bounded fallback below.
try:
    from agent.redact import redact_sensitive_text as _redact  # type: ignore
except Exception:
    _TELEGRAM_TOKEN_RE = re.compile(r"\b[0-9]{8,12}:AA[EFGH][A-Za-z0-9_-]{32}\b")
    _OPENROUTER_RE = re.compile(r"\bsk-or-v1-[a-f0-9]{60,}\b")
    _ANTHROPIC_RE = re.compile(r"\bsk-ant-(?:api|oat|admin)[0-9]+-[A-Za-z0-9_-]{60,}\b")
    _OPENAI_PROJ_RE = re.compile(r"\bsk-proj-[A-Za-z0-9_-]{80,}\b")
    _GENERIC_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{30,}\b")

    def _redact(text):
        if not isinstance(text, str):
            return text
        text = _TELEGRAM_TOKEN_RE.sub("[REDACTED_TELEGRAM_TOKEN]", text)
        text = _OPENROUTER_RE.sub("[REDACTED_OPENROUTER_KEY]", text)
        text = _ANTHROPIC_RE.sub("[REDACTED_ANTHROPIC_KEY]", text)
        text = _OPENAI_PROJ_RE.sub("[REDACTED_OPENAI_KEY]", text)
        text = _GENERIC_SK_RE.sub("[REDACTED_SECRET]", text)
        return text


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
TRANSCRIPT_DB = HERMES_HOME / "data" / "telegram-transcript.db"


def _find_gbrain_bin() -> Path:
    """Locate the gbrain executable — handles unix bare-name shim and Windows .cmd."""
    base = HERMES_HOME / "bin" / "gbrain"
    for candidate in (base, base.with_suffix(".cmd"), base.with_suffix(".exe"), base.with_suffix(".bat")):
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return base  # fall back; caller logs if missing


GBRAIN_BIN = _find_gbrain_bin()
LOG = HERMES_HOME / "logs" / "gbrain-capture.log"

# Max chars per gBrain page to bound write size (LLM context tolerance: ~30k chars per topic is plenty).
MAX_PAGE_CHARS = 60000
# Max messages pulled per topic for rendering
MAX_MESSAGES_PER_TOPIC = 200


def _capture_enabled() -> bool:
    return os.environ.get("HERMES_ENABLE_GBRAIN_CAPTURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def _slug_for(chat_id: str, thread_id: str | None, topic_name: str | None) -> str:
    """Generate a gBrain slug from telegram routing identifiers.

    Pattern: `topic/<safe-topic-name>-<chat_id>[-<thread_id>]`. Safe name is
    lowercased, non-alphanumerics → hyphens, truncated to 50 chars. If no
    topic name, falls back to `topic/<chat_id>[-<thread_id>]`.
    """
    safe = ""
    if topic_name:
        safe = re.sub(r"[^A-Za-z0-9]+", "-", topic_name).strip("-").lower()[:50]
    # chat_id can contain colons (e.g. "telegram:-100..."); gBrain handles them
    # but we normalize for cleaner slugs.
    cid = re.sub(r"[^A-Za-z0-9\-]+", "-", chat_id).strip("-")
    tail = cid + (f"-{thread_id}" if thread_id else "")
    return f"topic/{safe}-{tail}" if safe else f"topic/{tail}"


def _render_topic(rows: list[dict], chat_id: str, thread_id: str | None, topic_name: str | None) -> str:
    """Render a topic's conversation as a single markdown page."""
    if not rows:
        return ""
    out: list[str] = []
    out.append(f"# Topic: {topic_name or chat_id}")
    out.append("")
    out.append(f"- chat_id: `{chat_id}`")
    if thread_id:
        out.append(f"- thread_id: `{thread_id}`")
    if topic_name:
        out.append(f"- topic_name: {topic_name}")
    out.append(f"- first message: {rows[0]['timestamp']}")
    out.append(f"- last message: {rows[-1]['timestamp']}")
    out.append(f"- message count: {len(rows)}")
    out.append(f"- captured at: {datetime.now(timezone.utc).isoformat()}")
    out.append("")
    out.append("## Conversation")
    out.append("")
    for r in rows:
        ts = (r.get("timestamp") or "")[:19].replace("T", " ")
        sender = r.get("sender_name") or r.get("sender_id") or r["role"]
        text = (r.get("text") or "").strip()
        if not text:
            continue
        out.append(f"**{ts} — {sender} ({r['role']}):** {text}")
        out.append("")
    content = "\n".join(out)
    if len(content) > MAX_PAGE_CHARS:
        # Truncate the OLDEST messages, keep the newest. Add a marker.
        truncated_marker = (
            "\n\n*(older messages truncated for bounded page size; the full "
            "conversation history remains in telegram-transcript.db)*\n"
        )
        # Approximate: keep last ~MAX_PAGE_CHARS of content
        content = content[-(MAX_PAGE_CHARS - len(truncated_marker)) :]
        # Re-anchor on a sensible line boundary
        first_nl = content.find("\n")
        if first_nl > 0:
            content = truncated_marker.lstrip() + content[first_nl + 1 :]
    return content


def _gbrain_put(slug: str, content: str) -> bool:
    """Write a page to gBrain. Returns True on success.

    On Linux/macOS uses `gbrain put <slug>` with content piped via stdin
    (proven path, overwrites existing). On Windows the gbrain CLI tries
    to open /dev/stdin which doesn't exist, so we stage to a temp file
    and use shell redirect: `gbrain put <slug> < <tmpfile>`.
    """
    if not (GBRAIN_BIN.exists() or GBRAIN_BIN.is_symlink()):
        _log(f"gbrain bin not found at {GBRAIN_BIN}")
        return False
    try:
        if sys.platform == "win32":
            # Windows workaround for gbrain put's hardcoded /dev/stdin open.
            # Use `gbrain capture --slug X --file Y` which reads the file
            # path directly. Same overwrite semantics as put.
            import tempfile

            tmpfile = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".md",
                    delete=False,
                ) as f:
                    f.write(content)
                    tmpfile = f.name
                result = subprocess.run(
                    [str(GBRAIN_BIN), "capture", "--slug", slug, "--file", tmpfile, "--type", "concept", "--quiet"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )
            finally:
                if tmpfile:
                    try:
                        os.unlink(tmpfile)
                    except OSError:
                        pass
        else:
            result = subprocess.run(
                [str(GBRAIN_BIN), "put", slug],
                input=content,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        if result.returncode != 0:
            _log(f"gbrain put failed slug={slug}: rc={result.returncode} stderr={result.stderr.strip()[:300]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        _log(f"gbrain put TIMEOUT slug={slug}")
        return False
    except subprocess.SubprocessError as e:
        _log(f"gbrain put exception slug={slug}: {e}")
        return False


def _build_chat_id(raw_chat_id: str, thread_id: str | None) -> str:
    """Mirror hooks/telegram-transcript/_build_chat_id — keeps slug namespace
    aligned across hermes systems (chat_id stored in DB is the prefixed form).
    """
    base = f"telegram:{raw_chat_id}" if raw_chat_id else "telegram:unknown"
    return f"{base}:{thread_id}" if thread_id else base


def _parse_session_key(session_key: str) -> tuple[str, str | None]:
    parts = str(session_key or "").split(":")
    if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main" and parts[2] == "telegram":
        if parts[3] in ("dm", "group", "forum"):
            chat_id = parts[4] if len(parts) >= 5 else ""
            thread_id = parts[5] if len(parts) >= 6 else None
            return chat_id, thread_id
    return "", None


def _coerce_chat_routing(context: dict) -> tuple[str, str | None]:
    """Return the DB-form (prefixed) chat_id + thread_id.

    Mirrors hooks/telegram-transcript/_coerce_routing — the context emitted
    by the gateway carries `raw_chat_id` (e.g. `-100XXXXXXXXXX`) and a
    separate `thread_id`. The DB stores the prefixed form
    (`telegram:<raw>:<thread>`), so we apply _build_chat_id to match.
    """
    raw_chat_id = str(context.get("chat_id") or "").strip()
    thread_id = str(context.get("thread_id") or "").strip() or None
    if not raw_chat_id:
        raw_chat_id, parsed_thread = _parse_session_key(context.get("session_key") or "")
        if not thread_id:
            thread_id = parsed_thread
    if not raw_chat_id:
        return "", None
    return _build_chat_id(raw_chat_id, thread_id), thread_id


def _fetch_topic_rows(chat_id: str, thread_id: str | None) -> list[dict]:
    """Pull the most recent MAX_MESSAGES_PER_TOPIC messages for a topic."""
    if not TRANSCRIPT_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{TRANSCRIPT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if thread_id:
            cur.execute(
                """SELECT * FROM telegram_messages
                   WHERE chat_id = ? AND thread_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (chat_id, thread_id, MAX_MESSAGES_PER_TOPIC),
            )
        else:
            cur.execute(
                """SELECT * FROM telegram_messages
                   WHERE chat_id = ? AND (thread_id IS NULL OR thread_id = '')
                   ORDER BY id DESC LIMIT ?""",
                (chat_id, MAX_MESSAGES_PER_TOPIC),
            )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        rows.reverse()  # chronological
        # Redact each row's text
        for r in rows:
            if r.get("text"):
                r["text"] = _redact(r["text"])
        return rows
    except sqlite3.Error as e:
        _log(f"sqlite error: {e}")
        return []


DURABLE_DB = HERMES_HOME / "data" / "durable-threads.db"
MAX_RUNS_PER_THREAD_PAGE = 100
MAX_TOOLS_PER_RUN = 30


def _fetch_durable_thread(session_key: str) -> tuple[list[dict], dict[str, list[dict]]]:
    """Pull recent agent_threads + pending_commits for one session_key.

    Returns (runs, commits_by_run). Empty lists/dict on no-data or error.
    """
    if not DURABLE_DB.exists():
        return [], {}
    try:
        conn = sqlite3.connect(f"file:{DURABLE_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM agent_threads
            WHERE thread_id = ?
            ORDER BY started_at DESC
            LIMIT ?
        """,
            (session_key, MAX_RUNS_PER_THREAD_PAGE),
        )
        runs = [dict(r) for r in cur.fetchall()]
        commits_by_run: dict[str, list[dict]] = {r["run_id"]: [] for r in runs}
        if runs:
            placeholders = ",".join("?" * len(runs))
            cur.execute(
                f"""SELECT * FROM pending_commits
                    WHERE run_id IN ({placeholders})
                    ORDER BY created_at DESC""",
                [r["run_id"] for r in runs],
            )
            for c in cur.fetchall():
                d = dict(c)
                commits_by_run.setdefault(d["run_id"], []).append(d)
        conn.close()
        return runs, commits_by_run
    except sqlite3.Error as e:
        _log(f"durable sqlite error: {e}")
        return [], {}


def _fmt_epoch(epoch) -> str:
    if not epoch:
        return "—"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return str(epoch)


def _render_thread_page(thread_id: str, runs: list[dict], commits_by_run: dict[str, list[dict]]) -> str:
    """Render agent run history for a thread (same logic as gbrain-durable-sync)."""
    if not runs:
        return ""
    runs_sorted = sorted(runs, key=lambda r: r.get("started_at") or 0)
    out: list[str] = []
    out.append(f"# Agent history: {thread_id}")
    out.append("")
    out.append(f"- thread_id: `{thread_id}`")
    out.append(f"- total runs: {len(runs_sorted)}")
    out.append(f"- first run: {_fmt_epoch(runs_sorted[0].get('started_at'))}")
    out.append(f"- last run: {_fmt_epoch(runs_sorted[-1].get('started_at'))}")
    successes = sum(1 for r in runs_sorted if r.get("terminal_state") == "SUCCESS")
    out.append(f"- successes: {successes}/{len(runs_sorted)}")
    out.append(f"- captured at: {datetime.now(timezone.utc).isoformat()}")
    out.append("")

    tool_counts: dict[str, int] = {}
    for run in runs_sorted:
        for c in commits_by_run.get(run["run_id"], []):
            name = c.get("tool_name") or "?"
            tool_counts[name] = tool_counts.get(name, 0) + 1
    if tool_counts:
        out.append("## Tool usage (all runs)")
        out.append("")
        for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            out.append(f"- `{name}`: {count}")
        out.append("")

    out.append(f"## Run history (newest first, max {MAX_RUNS_PER_THREAD_PAGE})")
    out.append("")
    for run in reversed(runs_sorted[-MAX_RUNS_PER_THREAD_PAGE:]):
        rid = run["run_id"]
        started = _fmt_epoch(run.get("started_at"))
        ended = _fmt_epoch(run.get("ended_at"))
        ts = run.get("terminal_state") or "—"
        profile = run.get("agent_profile") or "—"
        source = run.get("source") or "—"
        out.append(f"### `{rid[:12]}…` ({started} → {ended}) — {ts}")
        out.append("")
        out.append(f"- profile: `{profile}` · source: `{source}`")
        if run.get("note"):
            out.append(f"- note: {run['note']}")
        commits = commits_by_run.get(rid, [])
        if commits:
            sorted_c = sorted(commits, key=lambda c: c.get("created_at") or 0, reverse=True)[:MAX_TOOLS_PER_RUN]
            out.append(f"- tool calls ({len(commits)} total, showing {len(sorted_c)}):")
            for c in sorted_c:
                status = c.get("status") or "?"
                tool = c.get("tool_name") or "?"
                err = c.get("error_message")
                err_suffix = f" — {err[:120]}" if err else ""
                out.append(f"  - `{tool}` [{status}]{err_suffix}")
        out.append("")
    content = "\n".join(out)
    if len(content) > MAX_PAGE_CHARS:
        truncation = "\n\n*(page truncated to bound size; older runs/tools elided)*\n"
        content = content[: MAX_PAGE_CHARS - len(truncation)] + truncation
    return content


def _slug_for_thread(session_key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9\-]+", "-", session_key).strip("-").lower()[:64]
    return f"thread/{safe}"


def _do_capture(context: dict) -> None:
    """Synchronous body — separated so async wrapper can offload it.

    Writes TWO complementary pages on every fire:
      topic/<safe>-<chat_id>[-<thread_id>]  — conversation (telegram-transcript.db)
      thread/<safe-session-key>             — agent run history (durable-threads.db)
    """
    chat_id, thread_id = _coerce_chat_routing(context)
    if not chat_id:
        return

    # Only act for telegram events
    platform = (context.get("platform") or "").lower()
    session_key = str(context.get("session_key") or "")
    if platform != "telegram" and not session_key.startswith("agent:main:telegram:"):
        return

    # --- topic page (conversation) ---
    rows = _fetch_topic_rows(chat_id, thread_id)
    if rows:
        topic_name = None
        for r in reversed(rows):
            if r.get("topic_name"):
                topic_name = r["topic_name"]
                break

        slug = _slug_for(chat_id, thread_id, topic_name)
        content = _render_topic(rows, chat_id, thread_id, topic_name)
        if content and _gbrain_put(slug, content):
            _log(f"WROTE topic slug={slug} msgs={len(rows)} chars={len(content)}")

    # --- thread page (agent run history) ---
    if session_key:
        runs, commits = _fetch_durable_thread(session_key)
        if runs:
            t_slug = _slug_for_thread(session_key)
            t_content = _render_thread_page(session_key, runs, commits)
            if t_content and _gbrain_put(t_slug, t_content):
                _log(f"WROTE thread slug={t_slug} runs={len(runs)} chars={len(t_content)}")


async def handle(event_type: str, context: dict):
    """Async hook entry point — required signature per Hermes hook contract."""
    if not _capture_enabled() or event_type not in ("processing:complete", "agent:end"):
        return
    try:
        # Offload to thread executor so the synchronous DB + subprocess work
        # doesn't block the asyncio loop the gateway runs on.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_capture, context)
    except Exception as e:
        _log(f"handle exception: {e}")
