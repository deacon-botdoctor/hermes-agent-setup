"""
task-ledger tools
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

HERMES_HOME = Path(
    os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
).expanduser()
DB_PATH = HERMES_HOME / "data" / "task-ledger.db"
CHANGE_RECORDS_PATH = HERMES_HOME / "state" / "task-ledger-change-records.jsonl"
AGENT_NAME = os.environ.get("HERMES_AGENT_NAME") or os.environ.get("AGENT_NAME") or "agent"
CHANGELOG_DIR = Path.home() / ".shared-agent-memory"

_db_conn: Optional[sqlite3.Connection] = None
_record_change = None
_new_reflection_entry = None
_changelog_load_attempted = False
_db_lock = threading.RLock()

_GATEWAY_SOURCE: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "task_ledger_gateway_source",
    default={},
)
_turn_lock = threading.RLock()
_MAX_TURN_CONTEXTS = 256
_TURN_CONTEXT_TTL_SECONDS = 4 * 60 * 60
_LEDGER_TOOLS = {"task_open", "task_update", "task_done", "task_block", "task_list"}
_ASYNC_COMPLETION_PREFIX = "[ASYNC DELEGATION"
_ASYNC_DISPATCHED_RE = re.compile(
    r"^Dispatched:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(?:\s|$)",
    re.MULTILINE,
)


@dataclass
class _TurnContext:
    turn_id: str
    created_monotonic: float = field(default_factory=time.monotonic)
    task_id: str = ""
    async_completion_bound: bool = False


_turn_contexts: dict[str, _TurnContext] = {}
_session_sources: dict[str, tuple[float, dict[str, str]]] = {}


def _ensure_task_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    wanted = {
        "client_slug": "TEXT",
        "change_record_required": "INTEGER DEFAULT 0",
        "change_recorded": "INTEGER DEFAULT 0",
        "change_record_status": "TEXT DEFAULT 'not_required'",
        "change_record_ref": "TEXT",
        "change_record_error": "TEXT",
        "capture_key": "TEXT",
    }
    for column, ddl in wanted.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
    conn.execute("UPDATE tasks SET change_record_required=0 WHERE change_record_required IS NULL")
    conn.execute("UPDATE tasks SET change_recorded=0 WHERE change_recorded IS NULL")
    conn.execute(
        "UPDATE tasks SET change_record_status='not_required' "
        "WHERE change_record_status IS NULL OR TRIM(change_record_status)=''"
    )


def get_db() -> sqlite3.Connection:
    global _db_conn
    with _db_lock:
        if _db_conn is not None:
            return _db_conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                chat_id TEXT,
                thread_id TEXT,
                platform TEXT,
                requested_by TEXT,
                ask TEXT NOT NULL,
                expected_artifact TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                status_note TEXT,
                artifact_path TEXT,
                artifact_verified INTEGER DEFAULT 0,
                blocker_reason TEXT,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                session_id TEXT
            )
            """
        )
        _ensure_task_columns(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat ON tasks(chat_id, thread_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON tasks(agent, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_slug ON tasks(client_slug)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_change_record_status "
            "ON tasks(change_record_required, change_recorded, status)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_capture_key "
            "ON tasks(capture_key) WHERE capture_key IS NOT NULL"
        )
        conn.commit()
        _db_conn = conn
        return conn


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _clean_text(value: str | None) -> str:
    return str(value or "").strip()


def _normalize_lines(items) -> list[str]:
    if not items:
        return []
    if isinstance(items, str):
        items = [items]
    normalized: list[str] = []
    for item in items:
        value = _clean_text(item)
        if value:
            normalized.append(value)
    return normalized


def _extract_chat_context(session_id: str, platform: str) -> dict:
    if isinstance(session_id, dict):
        session_id = session_id.get("session_id") or session_id.get("id") or ""
    if isinstance(platform, dict):
        platform = platform.get("platform") or platform.get("name") or ""
    session_id = str(session_id or "")
    platform = str(platform or "")
    if not session_id:
        return {}
    parts = session_id.split(":")
    ctx = {"platform": "unknown"}
    if "telegram" in session_id.lower() or "telegram" in platform.lower():
        ctx["platform"] = "telegram"
        try:
            tg_idx = parts.index("telegram")
            chat_type = parts[tg_idx + 1] if tg_idx + 1 < len(parts) else None
            chat_id = parts[tg_idx + 2] if tg_idx + 2 < len(parts) else None
            thread_id = parts[tg_idx + 3] if tg_idx + 3 < len(parts) else None
            if chat_type and chat_id:
                ctx["chat_id"] = str(chat_id)
                if thread_id and str(thread_id).lstrip("-").isdigit():
                    ctx["thread_id"] = str(thread_id)
        except (ValueError, IndexError):
            pass
    return ctx


def _platform_name(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _capture_gateway_context(*, event=None, **_) -> None:
    """Hold routing only; this hook runs before gateway authorization."""
    source = getattr(event, "source", None)
    _GATEWAY_SOURCE.set(
        {
            "platform": _platform_name(getattr(source, "platform", "")),
            "chat_id": _clean_text(getattr(source, "chat_id", "")),
            "thread_id": _clean_text(getattr(source, "thread_id", "")),
            "user_id": _clean_text(getattr(source, "user_id", "")),
            "user_name": _clean_text(getattr(source, "user_name", "")),
        }
    )
    return None


def _prune_turn_contexts(now: float) -> None:
    expired = [
        key
        for key, state in _turn_contexts.items()
        if now - state.created_monotonic > _TURN_CONTEXT_TTL_SECONDS
    ]
    for key in expired:
        _turn_contexts.pop(key, None)
    expired_sessions = [
        key
        for key, (created, _) in _session_sources.items()
        if now - created > _TURN_CONTEXT_TTL_SECONDS
    ]
    for key in expired_sessions:
        _session_sources.pop(key, None)
    if len(_turn_contexts) > _MAX_TURN_CONTEXTS:
        oldest = sorted(_turn_contexts.values(), key=lambda item: item.created_monotonic)
        for state in oldest[: len(_turn_contexts) - _MAX_TURN_CONTEXTS]:
            _turn_contexts.pop(state.turn_id, None)
    if len(_session_sources) > _MAX_TURN_CONTEXTS:
        oldest_sessions = sorted(_session_sources.items(), key=lambda item: item[1][0])
        for key, _ in oldest_sessions[: len(_session_sources) - _MAX_TURN_CONTEXTS]:
            _session_sources.pop(key, None)


def _source_for_session(session_id: str, platform: str = "") -> dict[str, str]:
    with _turn_lock:
        cached = _session_sources.get(_clean_text(session_id))
        if cached:
            return dict(cached[1])
    parsed = _extract_chat_context(session_id, platform)
    return {key: _clean_text(value) for key, value in parsed.items() if value is not None}


def _is_user_turn(user_message: str, source: dict[str, str], parent_session_id: str) -> bool:
    text = _clean_text(user_message)
    if not text or text.startswith("[SYSTEM") or parent_session_id:
        return False
    return bool(source.get("chat_id") and source.get("platform"))


def _is_async_completion(user_message: str) -> bool:
    return _clean_text(user_message).upper().startswith(_ASYNC_COMPLETION_PREFIX)


def _async_dispatched_at(user_message: str) -> str:
    """Convert the watcher's local dispatch timestamp to ledger UTC."""
    match = _ASYNC_DISPATCHED_RE.search(_clean_text(user_message))
    if not match:
        return ""
    try:
        local_struct = time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.mktime(local_struct)))
    except (OverflowError, ValueError):
        return ""


def _active_task_for_source(source: dict[str, str], *, active_at: str = "") -> str:
    """Return the newest active task in this exact authenticated chat topic."""
    chat_id = _clean_text(source.get("chat_id"))
    thread_id = _clean_text(source.get("thread_id"))
    platform = _clean_text(source.get("platform"))
    if not chat_id:
        return ""

    predicates = ["chat_id=?", "status IN ('open','in_progress')"]
    values: list[str] = [chat_id]
    if thread_id:
        predicates.append("thread_id=?")
        values.append(thread_id)
    else:
        predicates.append("(thread_id IS NULL OR TRIM(thread_id)='')")
    if platform:
        predicates.append("platform=?")
        values.append(platform)
    if active_at:
        # A completion may arrive after newer work has started in the same
        # topic. Exclude rows that did not exist when this batch dispatched.
        predicates.append("opened_at<=?")
        values.append(active_at)

    db = get_db()
    with _db_lock:
        row = db.execute(
            "SELECT id FROM tasks WHERE "
            + " AND ".join(predicates)
            + " ORDER BY opened_at DESC, updated_at DESC, id DESC LIMIT 1",
            tuple(values),
        ).fetchone()
    return str(row["id"]) if row else ""


def _before_llm_call(
    *,
    session_id: str = "",
    turn_id: str = "",
    user_message: str = "",
    platform: str = "",
    parent_session_id: str = "",
    sender_id: str = "",
    **_,
) -> dict[str, str] | None:
    """Bind only explicit durable completions after gateway auth has passed.

    Ordinary foreground work belongs to Hermes' native turn lifecycle.  The
    ledger must not inject policy, infer a task from tool count, or create a
    second lifecycle for work that remains inside the current turn.
    """
    source = dict(_GATEWAY_SOURCE.get() or {})
    _GATEWAY_SOURCE.set({})
    source["platform"] = source.get("platform") or _platform_name(platform)
    if not _is_user_turn(user_message, source, _clean_text(parent_session_id)):
        return None

    clean_turn_id = _clean_text(turn_id)
    clean_session_id = _clean_text(session_id)
    if not clean_turn_id or not clean_session_id:
        return None

    now = time.monotonic()
    with _turn_lock:
        _prune_turn_contexts(now)
        _session_sources[clean_session_id] = (now, dict(source))

    async_completion = _is_async_completion(user_message)
    if not async_completion:
        return None

    task_id = _active_task_for_source(
        source,
        active_at=_async_dispatched_at(user_message),
    )
    state = _TurnContext(
        turn_id=clean_turn_id,
        task_id=task_id,
        async_completion_bound=bool(task_id),
    )
    with _turn_lock:
        _turn_contexts[clean_turn_id] = state

    if task_id:
        return {
            "context": (
                f"Async delegation completion belongs to existing task {task_id} in this exact chat topic. "
                "Integrate the returned work instead of opening a duplicate task. Before replying, call task_update "
                "with concrete completed/target counts, blockers, and the verified artifact_path when one exists. "
                "Call task_done only if the user's full request—not merely this worker batch—is actually complete."
            )
        }

    return None


def _before_tool_call(*, tool_name: str = "", turn_id: str = "", **_) -> dict[str, str] | None:
    """Prevent an async completion from opening a duplicate durable row."""
    if _clean_text(tool_name) != "task_open":
        return None
    with _turn_lock:
        state = _turn_contexts.get(_clean_text(turn_id))
        if state is None or not state.task_id:
            return None
        if state.async_completion_bound:
            return {
                "action": "block",
                "message": (
                    f"Async completion is already bound to active task {state.task_id}. "
                    "Use task_update, task_done, or task_block on that task; do not open a duplicate."
                ),
            }
        return None


def _after_llm_call(*, turn_id: str = "", **_) -> None:
    with _turn_lock:
        _turn_contexts.pop(_clean_text(turn_id), None)
    return None


def _fallback_reflection_entry(**kwargs) -> dict:
    """Build the portable subset of an optional operator reflection."""
    return {
        key: value
        for key, value in kwargs.items()
        if value not in (None, "", [], {})
    }


def _fallback_record_change(**kwargs) -> dict[str, str]:
    """Persist a required record without an operator-only Python module.

    The task ledger is fleet-wide; ``~/.shared-agent-memory/changelog.py`` is
    not. A client runtime must therefore retain its completion evidence in its
    own Hermes state when that optional richer backend is absent.
    """
    record = {
        "schema_version": 1,
        "recorded_at": _now_iso(),
        **{
            key: value
            for key, value in kwargs.items()
            if value not in (None, "", [], {})
        },
    }
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode(
        "utf-8"
    )
    CHANGE_RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        descriptor = os.open(
            CHANGE_RECORDS_PATH,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    key = "client_changelog" if kwargs.get("client_slug") else "stack_changelog"
    return {key: str(CHANGE_RECORDS_PATH), "local_task_ledger_changelog": str(CHANGE_RECORDS_PATH)}


def _load_changelog_backend():
    global _record_change, _new_reflection_entry, _changelog_load_attempted
    if _changelog_load_attempted:
        return _record_change, _new_reflection_entry
    _changelog_load_attempted = True
    try:
        sys.path.insert(0, str(CHANGELOG_DIR))
        from changelog import new_reflection_entry, record_change  # type: ignore

        _record_change = record_change
        _new_reflection_entry = new_reflection_entry
    except Exception as exc:
        # The central changelog helper is an optional operator-control
        # enrichment, not a fleet runtime dependency. Fall back to the
        # profile-local append-only record so task completion never deadlocks.
        if not isinstance(exc, ModuleNotFoundError) or exc.name != "changelog":
            logger.warning(
                "task-ledger: optional changelog backend unavailable; using local record: %s",
                type(exc).__name__,
            )
        _record_change = _fallback_record_change
        _new_reflection_entry = _fallback_reflection_entry
    return _record_change, _new_reflection_entry


TASK_OPEN_SCHEMA = {
    "name": "task_open",
    "description": (
        "Register explicit durable work that must outlive the current foreground turn, "
        "such as an overnight job, monitoring run, or restart-surviving operation. "
        "Ordinary foreground work does not need a task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ask": {
                "type": "string",
                "description": "The actual ask from the user, paraphrased in one sentence.",
            },
            "expected_artifact": {
                "type": "string",
                "description": "What concrete thing will prove this is done.",
            },
            "client_slug": {
                "type": "string",
                "description": (
                    "Client slug when this task changes a client system. "
                    "If set, a change record is required by default."
                ),
            },
            "change_record_required": {
                "type": "boolean",
                "description": "Override whether this task must write a structured change record before it can close.",
            },
        },
        "required": ["ask", "expected_artifact"],
    },
}


def task_open_handler(
    ask: str = "",
    expected_artifact: str = "",
    client_slug: str = "",
    change_record_required=None,
    **kwargs,
) -> str:
    try:
        db = get_db()
        task_id = f"t_{uuid.uuid4().hex[:12]}"
        session_id = kwargs.get("session_id", "") or os.environ.get("HERMES_SESSION_ID", "")
        platform = kwargs.get("platform", "") or os.environ.get("HERMES_SESSION_PLATFORM", "")
        ctx = _source_for_session(session_id, platform)
        requested_by = (
            kwargs.get("sender_name")
            or ctx.get("user_name")
            or kwargs.get("sender_id")
            or ctx.get("user_id")
            or "operator"
        )
        ask = _clean_text(ask)
        expected_artifact = _clean_text(expected_artifact)
        client_slug = _clean_text(client_slug)
        if not ask:
            return "Error opening task: missing ask"
        if not expected_artifact:
            expected_artifact = "UNSPECIFIED_ARTIFACT"
        required = _truthy(change_record_required) if change_record_required is not None else bool(client_slug)
        change_status = "pending" if required else "not_required"
        now = _now_iso()

        with _db_lock:
            db.execute(
                """
                INSERT INTO tasks
                    (id, agent, chat_id, thread_id, platform, requested_by, ask, expected_artifact,
                     status, opened_at, updated_at, session_id, client_slug, change_record_required,
                     change_recorded, change_record_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    task_id,
                    AGENT_NAME,
                    ctx.get("chat_id"),
                    ctx.get("thread_id"),
                    ctx.get("platform") or _platform_name(platform) or None,
                    requested_by,
                    ask,
                    expected_artifact,
                    now,
                    now,
                    session_id,
                    client_slug or None,
                    1 if required else 0,
                    change_status,
                ),
            )
            db.commit()
        suffix = "\nChange record: required" if required else ""
        return (
            f"Task opened: {task_id}\n"
            f"Ask: {ask}\n"
            f"Expected artifact: {expected_artifact}{suffix}\n\n"
            f"Call task_done({task_id!r}, artifact_path=...) when complete."
        )
    except Exception as e:
        return f"Error opening task: {e}"


TASK_UPDATE_SCHEMA = {
    "name": "task_update",
    "description": "Update an in-flight task with a status note.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID from task_open"},
            "note": {"type": "string", "description": "Brief progress note"},
            "artifact_path": {
                "type": "string",
                "description": (
                    "Optional existing non-empty file, http(s) URL, or Telegram receipt "
                    "that proves current progress. This does not close the task."
                ),
            },
        },
        "required": ["task_id", "note"],
    },
}


def task_update_handler(task_id: str, note: str, artifact_path: str = "", **kwargs) -> str:
    try:
        db = get_db()
        artifact_path = _clean_text(artifact_path)
        if artifact_path and not _verify_artifact(artifact_path):
            return (
                f"Error updating task: artifact verification failed for {task_id}. "
                "Provide an existing non-empty file, an http(s) URL, or a telegram receipt."
            )
        with _db_lock:
            if artifact_path:
                cur = db.execute(
                    "UPDATE tasks SET status='in_progress', status_note=?, artifact_path=?, "
                    "artifact_verified=1, updated_at=? WHERE id=? "
                    "AND status NOT IN ('done','abandoned')",
                    (note, artifact_path, _now_iso(), task_id),
                )
            else:
                cur = db.execute(
                    "UPDATE tasks SET status='in_progress', status_note=?, updated_at=? "
                    "WHERE id=? AND status NOT IN ('done','abandoned')",
                    (note, _now_iso(), task_id),
                )
            db.commit()
        if cur.rowcount == 0:
            return f"Task {task_id} not found or already closed."
        suffix = f"\nArtifact verified: {artifact_path}" if artifact_path else ""
        return f"Task {task_id} updated: {note}{suffix}"
    except Exception as e:
        return f"Error updating task: {e}"


TASK_DONE_SCHEMA = {
    "name": "task_done",
    "description": (
        "Mark a task as done. Provide the artifact path. If the task is client-facing or change-record-required, "
        "this call must also write the structured changelog/reflection record before it can close."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID from task_open"},
            "artifact_path": {
                "type": "string",
                "description": "Concrete proof of completion: file path, delivered message ID, URL, etc.",
            },
            "summary": {"type": "string", "description": "One-sentence summary of what was delivered"},
            "record_change": {
                "type": "boolean",
                "description": "Force a structured change record on close. Defaults to required for client tasks.",
            },
            "client_slug": {
                "type": "string",
                "description": "Client slug if this done task changed a client system.",
            },
            "change_type": {"type": "string", "description": "Structured change type for the changelog entry."},
            "change_title": {"type": "string", "description": "Short title for the structured change entry."},
            "change_description": {
                "type": "string",
                "description": "Detailed description for the structured change entry.",
            },
            "severity": {"type": "string", "description": "critical, warning, or info"},
            "client_summary": {
                "type": "string",
                "description": "Client-facing summary sentence for BDR drafting.",
            },
            "client_impact": {
                "type": "string",
                "description": "Client-facing expectation or action needed.",
            },
            "ops_title": {"type": "string", "description": "Ops changelog section title."},
            "ops_fixes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Internal fix bullets for the stack changelog.",
            },
            "verification_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Verification bullets for the stack changelog.",
            },
            "reflection_title": {"type": "string", "description": "Reflection title."},
            "reflection_problem": {"type": "string", "description": "Reflection problem statement."},
            "reflection_fix": {"type": "string", "description": "Reflection fix statement."},
            "reflection_root_cause": {"type": "string", "description": "Reflection root cause."},
            "reflection_rules": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reusable rules learned from this fix.",
            },
        },
        "required": ["task_id", "artifact_path", "summary"],
    },
}


def _verify_artifact(artifact_path: str) -> bool:
    if not artifact_path:
        return False
    if artifact_path.startswith("/") or artifact_path.startswith("~"):
        expanded = Path(artifact_path).expanduser()
        return expanded.exists() and expanded.stat().st_size > 0
    if artifact_path.startswith("telegram:"):
        return True
    if artifact_path.startswith("http"):
        return True
    return False


def _normalize_severity(raw: str | None) -> str:
    mapping = {
        "1": "critical",
        "2": "warning",
        "3": "info",
        "4": "info",
        "critical": "critical",
        "high": "critical",
        "warning": "warning",
        "warn": "warning",
        "medium": "warning",
        "info": "info",
        "low": "info",
    }
    return mapping.get(_clean_text(raw).lower(), "info")


def _build_reflection(kwargs: dict) -> dict | None:
    title = _clean_text(kwargs.get("reflection_title"))
    problem = _clean_text(kwargs.get("reflection_problem"))
    fix = _clean_text(kwargs.get("reflection_fix"))
    root_cause = _clean_text(kwargs.get("reflection_root_cause"))
    rules = _normalize_lines(kwargs.get("reflection_rules"))
    if not any([title, problem, fix, root_cause, rules]):
        return None
    _, reflection_builder = _load_changelog_backend()
    if reflection_builder is None:
        raise RuntimeError("changelog reflection backend unavailable")
    return reflection_builder(
        scope=_clean_text(kwargs.get("client_slug")) or "stack",
        title=title or _clean_text(kwargs.get("change_title")) or _clean_text(kwargs.get("summary")),
        problem=problem or _clean_text(kwargs.get("change_description")) or _clean_text(kwargs.get("summary")),
        fix=fix or _clean_text(kwargs.get("summary")) or _clean_text(kwargs.get("change_title")),
        root_cause=root_cause,
        rules=rules,
        tags=[_clean_text(kwargs.get("change_type")) or "ops"],
        author=AGENT_NAME,
    )


def task_done_handler(task_id: str, artifact_path: str, summary: str, **kwargs) -> str:
    try:
        db = get_db()
        with _db_lock:
            task_row = db.execute(
                "SELECT id, ask, client_slug, change_record_required FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        if task_row is None:
            return f"Task {task_id} not found or already closed."

        artifact_path = _clean_text(artifact_path)
        summary = _clean_text(summary)
        if not _verify_artifact(artifact_path):
            return (
                f"Error closing task: artifact verification failed for {task_id}. "
                "Provide an existing non-empty file, an http(s) URL, or a telegram receipt."
            )
        final_client_slug = _clean_text(kwargs.get("client_slug")) or _clean_text(task_row["client_slug"])
        required = (
            _truthy(task_row["change_record_required"])
            or _truthy(kwargs.get("change_record_required"))
            or bool(final_client_slug)
        )
        explicit_record = (
            _truthy(kwargs.get("record_change"))
            or bool(final_client_slug)
            or any(
                _clean_text(kwargs.get(key))
                for key in [
                    "change_type",
                    "change_title",
                    "change_description",
                    "client_summary",
                    "client_impact",
                    "ops_title",
                    "reflection_title",
                    "reflection_problem",
                    "reflection_fix",
                    "reflection_root_cause",
                ]
            )
            or bool(_normalize_lines(kwargs.get("ops_fixes")))
            or bool(_normalize_lines(kwargs.get("verification_notes")))
            or bool(_normalize_lines(kwargs.get("reflection_rules")))
        )
        should_record = required or explicit_record
        written = {}
        change_status = "pending" if required else "not_required"
        change_error = None

        if should_record:
            record_fn, _ = _load_changelog_backend()
            if record_fn is None:
                return f"Error closing task: required change record backend unavailable for {task_id}."
            try:
                change_title = _clean_text(kwargs.get("change_title")) or summary or _clean_text(task_row["ask"])
                change_description = (
                    _clean_text(kwargs.get("change_description")) or summary or _clean_text(task_row["ask"])
                )
                reflection = _build_reflection(
                    {
                        **kwargs,
                        "client_slug": final_client_slug,
                        "summary": summary,
                        "change_title": change_title,
                        "change_description": change_description,
                    }
                )
                written = record_fn(
                    client_slug=final_client_slug or None,
                    change_type=_clean_text(kwargs.get("change_type")) or "ops",
                    title=change_title,
                    description=change_description,
                    severity=_normalize_severity(kwargs.get("severity")),
                    author=AGENT_NAME,
                    tags=[item for item in ["task-ledger", final_client_slug] if item],
                    verified=True,
                    client_summary=_clean_text(kwargs.get("client_summary")) or summary or None,
                    client_impact=_clean_text(kwargs.get("client_impact")) or None,
                    ops_title=_clean_text(kwargs.get("ops_title")) or None,
                    ops_fixes=_normalize_lines(kwargs.get("ops_fixes")),
                    verification_notes=_normalize_lines(kwargs.get("verification_notes")),
                    reflection=reflection,
                )
                if required and not written.get("client_changelog"):
                    return f"Error closing task: required client change record was not written for {task_id}."
                change_status = "recorded" if written else ("pending" if required else "not_required")
            except Exception as exc:
                logger.exception("task-ledger: change record write failed for %s", task_id)
                change_error = str(exc)
                change_status = "failed"
                if required:
                    return f"Error closing task: required change record failed for {task_id}: {exc}"

        verified = 1
        now = _now_iso()
        with _db_lock:
            cur = db.execute(
                """
                UPDATE tasks
                   SET status='done',
                       artifact_path=?,
                       artifact_verified=?,
                       status_note=?,
                       updated_at=?,
                       closed_at=?,
                       client_slug=?,
                       change_record_required=?,
                       change_recorded=?,
                       change_record_status=?,
                       change_record_ref=?,
                       change_record_error=?
                 WHERE id=? AND status NOT IN ('done','abandoned')
                """,
                (
                    artifact_path,
                    verified,
                    summary,
                    now,
                    now,
                    final_client_slug or None,
                    1 if required else 0,
                    1 if change_status == "recorded" else 0,
                    change_status,
                    json.dumps(written, sort_keys=True) if written else None,
                    change_error,
                    task_id,
                ),
            )
            db.commit()
        if cur.rowcount == 0:
            return f"Task {task_id} not found or already closed."
        verify_msg = "artifact verified"
        record_msg = f"change record: {change_status}"
        if change_error:
            record_msg += f" ({change_error})"
        return (
            f"Task {task_id} done.\n"
            f"{verify_msg}\n"
            f"{record_msg}\n"
            f"Artifact: {artifact_path}\n"
            f"Summary: {summary}"
        )
    except Exception as e:
        return f"Error closing task: {e}"


TASK_BLOCK_SCHEMA = {
    "name": "task_block",
    "description": "Mark a task as blocked when you cannot proceed.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID from task_open"},
            "blocker_reason": {"type": "string", "description": "Why you are blocked."},
        },
        "required": ["task_id", "blocker_reason"],
    },
}


def task_block_handler(task_id: str, blocker_reason: str, **kwargs) -> str:
    try:
        db = get_db()
        with _db_lock:
            cur = db.execute(
                "UPDATE tasks SET status='blocked', blocker_reason=?, updated_at=? "
                "WHERE id=? AND status NOT IN ('done','abandoned')",
                (blocker_reason, _now_iso(), task_id),
            )
            db.commit()
        if cur.rowcount == 0:
            return f"Task {task_id} not found or already closed."
        return f"Task {task_id} blocked: {blocker_reason}"
    except Exception as e:
        return f"Error blocking task: {e}"


TASK_LIST_SCHEMA = {
    "name": "task_list",
    "description": "List tasks from the ledger.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "in_progress", "blocked", "done", "all"],
                "description": "Filter by status (default: open,in_progress,blocked)",
            },
            "chat_scope": {
                "type": "boolean",
                "description": "If true, only show tasks from the current chat/topic",
            },
            "limit": {"type": "integer", "description": "Max results (default 20)"},
        },
        "required": [],
    },
}


def task_list_handler(status: str = None, chat_scope: bool = False, limit: int = 20, **kwargs) -> str:
    try:
        db = get_db()
        params = []
        where_clauses = []
        if status == "all":
            pass
        elif status:
            where_clauses.append("status = ?")
            params.append(status)
        else:
            where_clauses.append("status IN ('open','in_progress','blocked')")

        if chat_scope:
            session_id = kwargs.get("session_id", "") or os.environ.get("HERMES_SESSION_ID", "")
            platform = kwargs.get("platform", "") or os.environ.get("HERMES_SESSION_PLATFORM", "")
            ctx = _source_for_session(session_id, platform)
            if ctx.get("chat_id"):
                where_clauses.append("chat_id = ?")
                params.append(ctx["chat_id"])
                if ctx.get("thread_id"):
                    where_clauses.append("thread_id = ?")
                    params.append(ctx["thread_id"])

        where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        with _db_lock:
            rows = db.execute(
                f"""
                SELECT id, agent, ask, status, expected_artifact, artifact_path,
                       artifact_verified, blocker_reason, opened_at, updated_at,
                       change_record_required, change_recorded, change_record_status
                  FROM tasks{where_sql}
                 ORDER BY opened_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()

        if not rows:
            return "No tasks found."
        lines = []
        for r in rows:
            line = f"[{r['status']:11}] {r['id']}  {r['ask'][:80]}"
            if r["status"] == "done":
                verify_label = "OK" if r["artifact_verified"] else "UNVERIFIED"
                line += f"\n              {verify_label} {r['artifact_path'] or '(no artifact)'}"
                if r["change_record_required"]:
                    record_label = "RECORDED" if r["change_recorded"] else f"MISSING ({r['change_record_status']})"
                    line += f"\n              change-record: {record_label}"
            elif r["status"] == "blocked":
                line += f"\n              BLOCKED {r['blocker_reason']}"
            elif r["status"] in ("open", "in_progress"):
                line += f"\n              expected: {(r['expected_artifact'] or '')[:100]}"
                if r["change_record_required"]:
                    line += "\n              change-record: required"
                line += f"\n              opened: {r['opened_at']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing tasks: {e}"


def register(ctx):
    def _clean_kwargs(kwargs: dict) -> dict:
        cleaned = dict(kwargs)
        cleaned.pop("task_id", None)
        return cleaned

    ctx.register_tool(
        "task_open",
        "task-ledger",
        TASK_OPEN_SCHEMA,
        lambda args, **kwargs: task_open_handler(
            ask=args.get("ask", ""),
            expected_artifact=args.get("expected_artifact", ""),
            client_slug=args.get("client_slug", ""),
            change_record_required=args.get("change_record_required"),
            **_clean_kwargs(kwargs),
        ),
    )
    ctx.register_tool(
        "task_update",
        "task-ledger",
        TASK_UPDATE_SCHEMA,
        lambda args, **kwargs: task_update_handler(
            task_id=args.get("task_id", ""),
            note=args.get("note", ""),
            artifact_path=args.get("artifact_path", ""),
            **_clean_kwargs(kwargs),
        ),
    )
    ctx.register_tool(
        "task_done",
        "task-ledger",
        TASK_DONE_SCHEMA,
        lambda args, **kwargs: task_done_handler(
            task_id=args.get("task_id", ""),
            artifact_path=args.get("artifact_path", ""),
            summary=args.get("summary", ""),
            record_change=args.get("record_change"),
            client_slug=args.get("client_slug", ""),
            change_type=args.get("change_type", ""),
            change_title=args.get("change_title", ""),
            change_description=args.get("change_description", ""),
            severity=args.get("severity", ""),
            client_summary=args.get("client_summary", ""),
            client_impact=args.get("client_impact", ""),
            ops_title=args.get("ops_title", ""),
            ops_fixes=args.get("ops_fixes", []) or [],
            verification_notes=args.get("verification_notes", []) or [],
            reflection_title=args.get("reflection_title", ""),
            reflection_problem=args.get("reflection_problem", ""),
            reflection_fix=args.get("reflection_fix", ""),
            reflection_root_cause=args.get("reflection_root_cause", ""),
            reflection_rules=args.get("reflection_rules", []) or [],
            **_clean_kwargs(kwargs),
        ),
    )
    ctx.register_tool(
        "task_block",
        "task-ledger",
        TASK_BLOCK_SCHEMA,
        lambda args, **kwargs: task_block_handler(
            task_id=args.get("task_id", ""),
            blocker_reason=args.get("reason", "") or args.get("blocker_reason", ""),
            **_clean_kwargs(kwargs),
        ),
    )
    ctx.register_tool(
        "task_list",
        "task-ledger",
        TASK_LIST_SCHEMA,
        lambda args, **kwargs: task_list_handler(
            status=args.get("status"),
            chat_scope=args.get("chat_scope", False),
            limit=args.get("limit", 20),
            **_clean_kwargs(kwargs),
        ),
    )
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_gateway_dispatch", _capture_gateway_context)
        register_hook("pre_llm_call", _before_llm_call)
        register_hook("pre_tool_call", _before_tool_call)
        register_hook("post_llm_call", _after_llm_call)
    logger.info("task-ledger: plugin registered")
