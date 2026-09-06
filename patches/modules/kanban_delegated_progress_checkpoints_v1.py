#!/usr/bin/env python3
"""Add durable five-minute Telegram progress for delegated Kanban work."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

MARKER = "HERMES_KANBAN_DELEGATED_PROGRESS_CHECKPOINTS_v1"

DB_SCHEMA_ANCHOR = """    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
"""
DB_SCHEMA_REPLACEMENT = """    delivery_metadata TEXT,
    -- Durable edit-in-place state for Telegram delegated-work checkpoints.
    progress_message_id TEXT,
    progress_last_sent_at INTEGER,
    progress_last_event_id INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
"""

DB_MIGRATION_ANCHOR = """        if "delivery_metadata" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "delivery_metadata", "delivery_metadata TEXT"
            )

"""
DB_MIGRATION_REPLACEMENT = DB_MIGRATION_ANCHOR + """        if "progress_message_id" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "progress_message_id", "progress_message_id TEXT"
            )
        if "progress_last_sent_at" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "progress_last_sent_at", "progress_last_sent_at INTEGER"
            )
        if "progress_last_event_id" not in notify_cols:
            _add_column_if_missing(
                conn,
                "kanban_notify_subs",
                "progress_last_event_id",
                "progress_last_event_id INTEGER NOT NULL DEFAULT 0",
            )

"""

DB_REBUILD_ANCHOR = """        " delivery_metadata TEXT, created_at INTEGER NOT NULL,"
        " last_event_id INTEGER NOT NULL DEFAULT 0,"
"""
DB_REBUILD_REPLACEMENT = """        " delivery_metadata TEXT, progress_message_id TEXT,"
        " progress_last_sent_at INTEGER,"
        " progress_last_event_id INTEGER NOT NULL DEFAULT 0,"
        " created_at INTEGER NOT NULL,"
        " last_event_id INTEGER NOT NULL DEFAULT 0,"
"""

DB_HELPER_ANCHOR = """# ---------------------------------------------------------------------------
# Retention + garbage collection
# ---------------------------------------------------------------------------
"""

DB_HELPERS = r'''# HERMES_KANBAN_DELEGATED_PROGRESS_CHECKPOINTS_v1
def claim_due_notify_progress(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    now: Optional[int] = None,
    interval_seconds: int = 300,
    freshness_seconds: int = 180,
) -> Optional[dict]:
    """Claim one due, heartbeat-backed delegated progress milestone.

    Progress has its own cursor so consuming a heartbeat can never hide a
    terminal event from ``last_event_id``. The active run id fences retries:
    a heartbeat from a prior crashed/reclaimed run is never evidence that the
    current worker is still active.
    """
    now_value = int(time.time()) if now is None else int(now)
    interval = max(300, int(interval_seconds))
    freshness = max(60, int(freshness_seconds))
    if str(platform or "").lower() != "telegram":
        return None

    with write_txn(conn):
        sub = conn.execute(
            "SELECT progress_last_event_id, progress_last_sent_at, "
            "progress_message_id, created_at FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        ).fetchone()
        if sub is None:
            return None
        task = conn.execute(
            "SELECT t.status, t.current_run_id, r.started_at, r.last_heartbeat_at "
            "FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ? AND r.status = 'running'",
            (task_id,),
        ).fetchone()
        if (
            task is None
            or task["status"] != "running"
            or task["current_run_id"] is None
            or task["started_at"] is None
            or task["last_heartbeat_at"] is None
        ):
            return None

        # Never replay progress to a subscriber that joined an already-running
        # task. The first milestone is measured from the later of the active
        # run start and the subscription handoff.
        started_at = max(int(task["started_at"]), int(sub["created_at"] or 0))
        elapsed = max(0, now_value - started_at)
        milestone_seconds = (elapsed // interval) * interval
        if milestone_seconds < interval:
            return None
        scheduled_at = started_at + milestone_seconds
        last_sent_at = int(sub["progress_last_sent_at"] or 0)
        if str(sub["progress_message_id"] or "").startswith("untracked:"):
            # The platform accepted the first send without returning an id.
            # Editing is impossible, so suppress later milestones rather than
            # create a second progress bubble.
            return None
        if last_sent_at >= scheduled_at:
            return None
        heartbeat_at = int(task["last_heartbeat_at"])
        if heartbeat_at < now_value - freshness or heartbeat_at > now_value + 5:
            return None

        old_cursor = int(sub["progress_last_event_id"] or 0)
        # Negative means delivery is in flight or its outcome is ambiguous.
        # Never expire this reservation into a duplicate first send.
        if old_cursor < 0:
            return None
        event = conn.execute(
            "SELECT id, payload, created_at FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'heartbeat' "
            "AND id > ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (task_id, int(task["current_run_id"]), old_cursor, started_at),
        ).fetchone()
        if event is None:
            return None
        event_id = int(event["id"])
        updated = conn.execute(
            "UPDATE kanban_notify_subs SET progress_last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND progress_last_event_id = ?",
            (
                -event_id, task_id, platform, chat_id, thread_id or "", old_cursor,
            ),
        )
        if updated.rowcount != 1:
            return None
        try:
            payload = json.loads(event["payload"]) if event["payload"] else {}
        except Exception:
            payload = {}
        note = payload.get("note") if isinstance(payload, dict) else None
        return {
            "old_cursor": old_cursor,
            "event_id": event_id,
            "run_id": int(task["current_run_id"]),
            "milestone_seconds": milestone_seconds,
            "scheduled_at": scheduled_at,
            "heartbeat_at": int(event["created_at"]),
            "message_id": sub["progress_message_id"],
            "note": str(note) if note else None,
        }


def complete_notify_progress(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_event_id: int,
    message_id: str,
    sent_at: int,
) -> bool:
    """Durably acknowledge a successfully sent/edited progress card."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_notify_subs SET progress_message_id = ?, "
            "progress_last_sent_at = ?, progress_last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND progress_last_event_id = ?",
            (
                str(message_id), int(sent_at), int(claimed_event_id), task_id, platform, chat_id,
                thread_id or "", -int(claimed_event_id),
            ),
        )
    return cur.rowcount == 1


def notify_progress_claim_is_current(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    run_id: int,
    event_id: int,
) -> bool:
    """Revalidate the active run immediately before visible delivery."""
    row = conn.execute(
        "SELECT 1 FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
        "JOIN task_events e ON e.id = ? AND e.task_id = t.id "
        "JOIN kanban_notify_subs s ON s.task_id = t.id "
        "WHERE s.platform = ? AND s.chat_id = ? AND s.thread_id = ? "
        "AND s.progress_last_event_id = ? AND t.id = ? AND t.status = 'running' AND t.current_run_id = ? "
        "AND r.status = 'running' AND e.run_id = ? AND e.kind = 'heartbeat'",
        (int(event_id), platform, chat_id, thread_id or "", -int(event_id),
         task_id, int(run_id), int(run_id)),
    ).fetchone()
    return row is not None


def rewind_notify_progress(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_event_id: int,
    old_cursor: int,
) -> bool:
    """Release a progress heartbeat claim after a failed send or edit."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_notify_subs SET progress_last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND progress_last_event_id = ?",
            (
                int(old_cursor), task_id, platform, chat_id, thread_id or "",
                -int(claimed_event_id),
            ),
        )
    return cur.rowcount == 1


'''

WATCHER_HELPER_ANCHOR = """class GatewayKanbanWatchersMixin:
"""

WATCHER_HELPERS = r'''# HERMES_KANBAN_DELEGATED_PROGRESS_CHECKPOINTS_v1
_KANBAN_DELEGATED_PROGRESS_INTERVAL_SECONDS = 300
_KANBAN_DELEGATED_PROGRESS_FRESHNESS_SECONDS = 180


def _safe_delegated_progress_note(value: Any) -> str:
    """Return bounded client-safe heartbeat prose, or an empty string."""
    import re

    text = " ".join(str(value or "").split()).strip()
    if not text or len(text) > 400:
        return ""
    unsafe = (
        r"```|`[^`]+`",
        r"(?:^|\s)(?:~?/|\.{1,2}/|[A-Za-z]:\\)\S+",
        r"\b(?:exec_command|tool[_ -]?(?:call|name|output|result)|stdout|stderr|"
        r"argv|system prompt|developer message|chain[- ]of[- ]thought|hidden reasoning)\b",
        r"\b(?:python3?|bash|zsh|pwsh|powershell|curl|wget|ssh)\s+[-\w]",
        r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}\b",
        r"^(?:analysis|reasoning|thought process)\s*:",
    )
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in unsafe):
        return ""
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:
        # The strict local filters above remain the fail-soft backstop.
        pass
    return text[:240].rstrip()


def _format_delegated_progress_card(task: Any, progress: dict, now: int) -> str:
    """Render factual progress from persisted task/run heartbeat state."""
    import re

    title = " ".join(str(getattr(task, "title", "") or "delegated work").split())
    title = title[:120] or "delegated work"
    assignee = re.sub(
        r"[^A-Za-z0-9_.-]", "",
        str(getattr(task, "assignee", "") or "specialist"),
    )[:64] or "specialist"
    minutes = max(5, int(progress.get("milestone_seconds") or 300) // 60)
    heartbeat_age = max(0, int(now) - int(progress.get("heartbeat_at") or now))
    note = _safe_delegated_progress_note(progress.get("note"))
    lines = [
        f"⏳ {minutes} minutes in — {title}",
        f"@{assignee} is still working; verified activity {heartbeat_age}s ago.",
    ]
    if note:
        lines.append(f"Latest checkpoint: {note}")
    return "\n".join(lines)


'''

WATCHER_COLLECT_ANCHOR = (
    "                                    old_cursor, cursor, events = "
    "_kb.claim_unseen_events_for_sub(\n"
    """                                        conn,
                                        task_id=sub["task_id"],
                                        platform=sub["platform"],
                                        chat_id=sub["chat_id"],
                                        thread_id=sub.get("thread_id") or "",
                                        kinds=TERMINAL_KINDS,
                                    )
                                    if not events:
                                        continue
                                    task = _kb.get_task(conn, sub["task_id"])
"""
)

WATCHER_COLLECT_REPLACEMENT = (
    "                                    old_cursor, cursor, events = "
    "_kb.claim_unseen_events_for_sub(\n"
    """                                        conn,
                                        task_id=sub["task_id"],
                                        platform=sub["platform"],
                                        chat_id=sub["chat_id"],
                                        thread_id=sub.get("thread_id") or "",
                                        kinds=TERMINAL_KINDS,
                                    )
                                    task = _kb.get_task(conn, sub["task_id"])
                                    progress = None
                                    if platform == "telegram":
                                        progress = _kb.claim_due_notify_progress(
                                            conn,
                                            task_id=sub["task_id"],
                                            platform=sub["platform"],
                                            chat_id=sub["chat_id"],
                                            thread_id=sub.get("thread_id") or "",
                                            interval_seconds=_KANBAN_DELEGATED_PROGRESS_INTERVAL_SECONDS,
                                            freshness_seconds=_KANBAN_DELEGATED_PROGRESS_FRESHNESS_SECONDS,
                                        )
                                    if not events and not progress:
                                        continue
"""
)

WATCHER_DELIVERY_DICT_ANCHOR = """                                        "events": events,
                                        "task": task,
                                        "board": slug,
"""
WATCHER_DELIVERY_DICT_REPLACEMENT = """                                        "events": events,
                                        "task": task,
                                        "progress": progress,
                                        "board": slug,
"""

WATCHER_ADAPTER_MISSING_ANCHOR = """                        await _to_thread_process_service(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
"""
WATCHER_ADAPTER_MISSING_REPLACEMENT = """                        await _to_thread_process_service(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        if d.get("progress"):
                            await _to_thread_process_service(
                                self._kanban_rewind_progress,
                                sub,
                                d["progress"],
                                board_slug,
                            )
                        continue
"""

WATCHER_PROGRESS_DELIVERY_ANCHOR = """                    wake_handoff = ""
                    for ev in d["events"]:
"""
WATCHER_PROGRESS_DELIVERY_LATEST_ANCHOR = """                    wake_handoff = ""
                    wake_review_detail = ""
                    for ev in d["events"]:
"""
WATCHER_PROGRESS_DELIVERY_REPLACEMENT = """                    wake_handoff = ""
                    wake_review_detail = ""
                    if d.get("progress") and send_passive:
                        try:
                            await self._deliver_kanban_progress(
                                adapter=adapter,
                                sub=sub,
                                task=task,
                                progress=d["progress"],
                                board=board_slug,
                            )
                        except Exception as progress_exc:
                            logger.warning(
                                "kanban notifier: delegated progress delivery failed for %s: %s",
                                sub["task_id"], progress_exc,
                            )
                            await _to_thread_process_service(
                                self._kanban_rewind_progress,
                                sub,
                                d["progress"],
                                board_slug,
                            )
                    elif d.get("progress"):
                        # A wake-only subscription explicitly suppresses
                        # passive chat output. Consume no heartbeat claim so a
                        # later mode change can still deliver the milestone.
                        await _to_thread_process_service(
                            self._kanban_rewind_progress,
                            sub,
                            d["progress"],
                            board_slug,
                        )
                    for ev in d["events"]:
"""

WATCHER_METHOD_ANCHOR = """    def _kanban_advance(
"""
WATCHER_METHODS = r'''    async def _deliver_kanban_progress(
        self,
        *,
        adapter: Any,
        sub: dict,
        task: Any,
        progress: dict,
        board: Optional[str],
    ) -> None:
        """Send once, then edit one durable Telegram progress card."""
        current = await _to_thread_process_service(
            self._kanban_progress_claim_current,
            sub,
            progress,
            board,
        )
        if not current:
            raise RuntimeError("progress claim is no longer current")
        metadata = dict(sub.get("delivery_metadata") or {})
        if sub.get("thread_id") and not metadata.get("thread_id"):
            metadata["thread_id"] = sub["thread_id"]
        text = _format_delegated_progress_card(task, progress, int(time.time()))
        message_id = str(progress.get("message_id") or "").strip()
        if message_id:
            result = await adapter.edit_message(
                sub["chat_id"], message_id, text,
            )
            if getattr(result, "success", False) is not True:
                raise RuntimeError(
                    "edit failed; retaining the existing progress message id"
                )
            delivered_id = str(getattr(result, "message_id", None) or message_id)
        else:
            result = await adapter.send(sub["chat_id"], text, metadata=metadata)
            if getattr(result, "success", False) is not True:
                raise RuntimeError("initial progress send failed")
            delivered_id = str(
                getattr(result, "message_id", None)
                or f"untracked:{progress['event_id']}"
            )
        progress["delivery_attempt_succeeded"] = True
        ok = await _to_thread_process_service(
            self._kanban_complete_progress,
            sub,
            progress,
            delivered_id,
            board,
        )
        if not ok:
            raise RuntimeError("progress delivery acknowledgement lost its claim")

    def _kanban_complete_progress(
        self,
        sub: dict,
        progress: dict,
        message_id: str,
        board: Optional[str] = None,
    ) -> bool:
        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        try:
            return _kb.complete_notify_progress(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_event_id=progress["event_id"],
                message_id=message_id,
                sent_at=progress["scheduled_at"],
            )
        finally:
            conn.close()

    def _kanban_progress_claim_current(
        self,
        sub: dict,
        progress: dict,
        board: Optional[str] = None,
    ) -> bool:
        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        try:
            return _kb.notify_progress_claim_is_current(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                run_id=progress["run_id"],
                event_id=progress["event_id"],
            )
        finally:
            conn.close()

    def _kanban_rewind_progress(
        self,
        sub: dict,
        progress: dict,
        board: Optional[str] = None,
    ) -> bool:
        if progress.get("delivery_attempt_succeeded"):
            # Visible send with missing durable acknowledgement is ambiguous.
            return False
        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        try:
            return _kb.rewind_notify_progress(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_event_id=progress["event_id"],
                old_cursor=progress["old_cursor"],
            )
        finally:
            conn.close()

'''


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise RuntimeError(
            f"Kanban delegated progress {label} anchor drift (found {count})"
        )
    return source.replace(anchor, replacement, 1)


_DB_RESERVATION_UPGRADES = [('        old_cursor = int(sub["progress_last_event_id"] or 0)\n', '        old_cursor = int(sub["progress_last_event_id"] or 0)\n        # Negative means delivery is in flight or its outcome is ambiguous.\n        # Never expire this reservation into a duplicate first send.\n        if old_cursor < 0:\n            return None\n'), ('                event_id, task_id, platform, chat_id, thread_id or "", old_cursor,', '                -event_id, task_id, platform, chat_id, thread_id or "", old_cursor,'), ('            "progress_last_sent_at = ? "', '            "progress_last_sent_at = ?, progress_last_event_id = ? "'), ('                str(message_id), int(sent_at), task_id, platform, chat_id,\n                thread_id or "", int(claimed_event_id),', '                str(message_id), int(sent_at), int(claimed_event_id), task_id, platform, chat_id,\n                thread_id or "", -int(claimed_event_id),'), ('                int(claimed_event_id),\n', '                -int(claimed_event_id),\n'), ('def notify_progress_claim_is_current(\n    conn: sqlite3.Connection,\n    *,\n    task_id: str,\n    run_id: int,\n    event_id: int,\n) -> bool:\n    """Revalidate the active run immediately before visible delivery."""\n    row = conn.execute(\n        "SELECT 1 FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "\n        "JOIN task_events e ON e.id = ? AND e.task_id = t.id "\n        "WHERE t.id = ? AND t.status = \'running\' AND t.current_run_id = ? "\n        "AND r.status = \'running\' AND e.run_id = ? AND e.kind = \'heartbeat\'",\n        (int(event_id), task_id, int(run_id), int(run_id)),\n    ).fetchone()\n    return row is not None\n\n', 'def notify_progress_claim_is_current(\n    conn: sqlite3.Connection,\n    *,\n    task_id: str,\n    platform: str,\n    chat_id: str,\n    thread_id: Optional[str] = None,\n    run_id: int,\n    event_id: int,\n) -> bool:\n    """Revalidate the active run immediately before visible delivery."""\n    row = conn.execute(\n        "SELECT 1 FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "\n        "JOIN task_events e ON e.id = ? AND e.task_id = t.id "\n        "JOIN kanban_notify_subs s ON s.task_id = t.id "\n        "WHERE s.platform = ? AND s.chat_id = ? AND s.thread_id = ? "\n        "AND s.progress_last_event_id = ? AND t.id = ? AND t.status = \'running\' AND t.current_run_id = ? "\n        "AND r.status = \'running\' AND e.run_id = ? AND e.kind = \'heartbeat\'",\n        (int(event_id), platform, chat_id, thread_id or "", -int(event_id),\n         task_id, int(run_id), int(run_id)),\n    ).fetchone()\n    return row is not None\n\n')]
_WATCHER_RESERVATION_UPGRADES = [('        ok = await _to_thread_process_service(\n', '        progress["delivery_attempt_succeeded"] = True\n        ok = await _to_thread_process_service(\n'), ('                run_id=progress["run_id"],', '                platform=sub["platform"],\n                chat_id=sub["chat_id"],\n                thread_id=sub.get("thread_id") or "",\n                run_id=progress["run_id"],'), ('    def _kanban_rewind_progress(\n        self,\n        sub: dict,\n        progress: dict,\n        board: Optional[str] = None,\n    ) -> bool:\n        from hermes_cli import kanban_db as _kb\n\n', '    def _kanban_rewind_progress(\n        self,\n        sub: dict,\n        progress: dict,\n        board: Optional[str] = None,\n    ) -> bool:\n        if progress.get("delivery_attempt_succeeded"):\n            # Visible send with missing durable acknowledgement is ambiguous.\n            return False\n        from hermes_cli import kanban_db as _kb\n\n')]

def _upgrade_exact_helper(source, current, upgrades):
    previous = current
    for before, after in reversed(upgrades):
        if previous.count(after) != 1:
            raise RuntimeError("Kanban reservation template drift")
        previous = previous.replace(after, before, 1)
    if source.count(current) == 1:
        return source
    if source.count(previous) != 1:
        raise RuntimeError("Kanban installed reservation helper drift")
    return source.replace(previous, current, 1)


def patch_kanban_db_text(source: str) -> str:
    if MARKER in source:
        return _upgrade_exact_helper(source, DB_HELPERS, _DB_RESERVATION_UPGRADES)
    patched = source
    patched = _replace_once(patched, DB_SCHEMA_ANCHOR, DB_SCHEMA_REPLACEMENT, "schema")
    patched = _replace_once(
        patched, DB_MIGRATION_ANCHOR, DB_MIGRATION_REPLACEMENT, "migration"
    )
    patched = _replace_once(
        patched, DB_REBUILD_ANCHOR, DB_REBUILD_REPLACEMENT, "rebuild schema"
    )
    patched = _replace_once(
        patched, DB_HELPER_ANCHOR, DB_HELPERS + DB_HELPER_ANCHOR, "DB helpers"
    )
    ast.parse(patched)
    return patched


def patch_watcher_text(source: str) -> str:
    if MARKER in source:
        return _upgrade_exact_helper(source, WATCHER_METHODS, _WATCHER_RESERVATION_UPGRADES)
    patched = source
    patched = _replace_once(
        patched, WATCHER_HELPER_ANCHOR, WATCHER_HELPERS + WATCHER_HELPER_ANCHOR,
        "watcher helpers",
    )
    patched = _replace_once(
        patched, WATCHER_COLLECT_ANCHOR, WATCHER_COLLECT_REPLACEMENT,
        "progress claim",
    )
    patched = _replace_once(
        patched, WATCHER_DELIVERY_DICT_ANCHOR, WATCHER_DELIVERY_DICT_REPLACEMENT,
        "delivery state",
    )
    patched = _replace_once(
        patched, WATCHER_ADAPTER_MISSING_ANCHOR, WATCHER_ADAPTER_MISSING_REPLACEMENT,
        "adapter rewind",
    )
    progress_anchors = (
        WATCHER_PROGRESS_DELIVERY_ANCHOR,
        WATCHER_PROGRESS_DELIVERY_LATEST_ANCHOR,
    )
    progress_counts = tuple(patched.count(anchor) for anchor in progress_anchors)
    if sum(progress_counts) != 1:
        raise RuntimeError(
            "Kanban delegated progress progress delivery anchor drift "
            f"(found {sum(progress_counts)})"
        )
    patched = patched.replace(
        progress_anchors[progress_counts.index(1)],
        WATCHER_PROGRESS_DELIVERY_REPLACEMENT,
        1,
    )
    patched = _replace_once(
        patched, WATCHER_METHOD_ANCHOR, WATCHER_METHODS + WATCHER_METHOD_ANCHOR,
        "watcher methods",
    )
    ast.parse(patched)
    return patched



_NATIVE_COMMIT = "d3630f853239e8c41ce7201e09fbdf39bcbc5431"


def _native_replacements() -> dict[str, list[tuple[str, str]]]:
    """Residual progress only, attached to d363's split phase owners."""
    helpers = DB_HELPERS.replace("with write_txn(conn):", "with _kb.write_txn(conn):")
    methods = WATCHER_METHODS.replace(
        "from hermes_cli import kanban_db as _kb",
        "from hermes_cli import kanban_db_notify as _kb\n        from hermes_cli import kanban_db_connect as _kbc",
    ).replace("conn = _kb.connect(board=board)", "conn = _kbc.connect(board=board)")
    # Treat worker-provided titles as untrusted prose, just like notes.
    formatter = WATCHER_HELPERS.replace(
        'title = " ".join(str(getattr(task, "title", "") or "delegated work").split())',
        'title = _safe_delegated_progress_note(getattr(task, "title", "")) or "delegated work"',
    ).replace('redact_sensitive_text(text, force=True)',
              'redact_sensitive_text(text, force=True, redact_url_credentials=True)')
    collect = '        if not events:\n            return None\n        task = self.kb.get_task(conn, sub["task_id"])\n'
    collect_new = '        progress = None\n        if platform == "telegram" and sub.get("delivery_mode") != "wake":\n            progress = _kbn().claim_due_notify_progress(\n                conn, task_id=sub["task_id"], platform=sub["platform"],\n                chat_id=sub["chat_id"], thread_id=sub.get("thread_id") or "",\n            )\n        if not events and not progress:\n            return None\n        task = self.kb.get_task(conn, sub["task_id"])\n'
    deliver_anchor = "        if not await self._send_pings():\n"
    deliver_new = '        if self.d.get("progress"):\n            try:\n                if not self.send_passive:\n                    raise RuntimeError("passive progress disabled")\n                await self.runner._deliver_kanban_progress(\n                    adapter=adapter, sub=self.sub, task=self.task,\n                    progress=self.d["progress"], board=self.board_slug,\n                )\n            except Exception:\n                logger.warning("kanban notifier: delegated progress delivery failed; retaining retry")\n                await _to_thread_process_service(\n                    self.runner._kanban_rewind_progress,\n                    self.sub, self.d["progress"], self.board_slug,\n                )\n        if not await self._send_pings():\n'
    rewind = "    async def rewind(self) -> None:\n"
    rewind_new = rewind + '        if self.d.get("progress"):\n            await _to_thread_process_service(\n                self.runner._kanban_rewind_progress,\n                self.sub, self.d["progress"], self.board_slug,\n            )\n'
    migration = '    ("delivery_metadata", "delivery_metadata TEXT"),\n'
    migration_new = migration + '    ("progress_message_id", "progress_message_id TEXT"),\n    ("progress_last_sent_at", "progress_last_sent_at INTEGER"),\n    ("progress_last_event_id", "progress_last_event_id INTEGER NOT NULL DEFAULT 0"),\n'
    late_import = "# Late-bound origin namespace (see module docstring); imported LAST so this\n"
    return {
        "hermes_cli/kanban_db.py": [(DB_SCHEMA_ANCHOR, DB_SCHEMA_REPLACEMENT)],
        "hermes_cli/kanban_db_connect.py": [
            (migration, migration_new), (DB_REBUILD_ANCHOR, DB_REBUILD_REPLACEMENT),
        ],
        "hermes_cli/kanban_db_notify.py": [(late_import, helpers + late_import)],
        "gateway/kanban_watchers.py": [
            (WATCHER_HELPER_ANCHOR, formatter + WATCHER_HELPER_ANCHOR),
            ("    def _kanban_advance(self,", methods + "    def _kanban_advance(self,"),
        ],
        "gateway/kanban_watchers_notifier.py": [
            (collect, collect_new),
            ('"events": events, "task": task, "board": slug}',
             '"events": events, "task": task, "progress": progress, "board": slug}'),
            (rewind, rewind_new), (deliver_anchor, deliver_new),
        ],
    }


def _native_previous_reservation(source):
    for before, after in reversed(_DB_RESERVATION_UPGRADES + _WATCHER_RESERVATION_UPGRADES):
        def native(text):
            return text.replace("with write_txn(conn):", "with _kb.write_txn(conn):").replace(
                "from hermes_cli import kanban_db as _kb",
                "from hermes_cli import kanban_db_notify as _kb\n        from hermes_cli import kanban_db_connect as _kbc",
            ).replace("conn = _kb.connect(board=board)", "conn = _kbc.connect(board=board)")
        after, before = native(after), native(before)
        if after in source:
            source = source.replace(after, before, 1)
    return source


def _patch_native(root: Path) -> bool:
    # Exact revision plus every touched pre/post body: unrelated ordered edits
    # can compose, while partial application and marker-only states fail closed.
    originals = {}
    proposed = {}
    states = set()
    for relative, replacements in _native_replacements().items():
        path = root / relative
        if path.is_symlink():
            raise RuntimeError(f"Kanban native source is a symlink: {relative}")
        source = path.read_text(encoding="utf-8")
        originals[path] = source
        for before, after in replacements:
            if source.count(after) == 1:
                states.add("post")
            elif (previous := _native_previous_reservation(after)) != after and source.count(previous) == 1:
                states.add("upgrade")
                source = source.replace(previous, after, 1)
            elif source.count(before) == 1:
                states.add("pre")
                source = source.replace(before, after, 1)
            else:
                raise RuntimeError(f"Kanban native source drift: {relative}")
        ast.parse(source)
        proposed[path] = source
    if states == {"post"}:
        return False
    if states != {"pre"} and not ("upgrade" in states and states <= {"post", "upgrade"}):
        raise RuntimeError("Kanban native partial application")
    try:
        for path, source in proposed.items():
            path.write_text(source, encoding="utf-8")
        for path, source in proposed.items():
            if path.read_text(encoding="utf-8") != source:
                raise RuntimeError("Kanban native postimage mismatch")
    except Exception:
        for path, source in originals.items():
            path.write_text(source, encoding="utf-8")
        raise
    return True


def patch_kanban_delegated_progress_checkpoints_v1(hermes_dir: Path) -> bool:
    """Patch the pinned Hermes Kanban DB and gateway watcher atomically."""
    root = Path(hermes_dir)
    import subprocess
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if revision.returncode == 0 and revision.stdout.strip() == _NATIVE_COMMIT:
        return _patch_native(root)
    targets = {
        root / "hermes_cli/kanban_db.py": patch_kanban_db_text,
        root / "gateway/kanban_watchers.py": patch_watcher_text,
    }
    originals: dict[Path, str] = {}
    patched: dict[Path, str] = {}
    for path, transform in targets.items():
        originals[path] = path.read_text(encoding="utf-8")
        patched[path] = transform(originals[path])
    changed = [path for path in targets if patched[path] != originals[path]]
    if not changed:
        return False

    backups: dict[Path, Path] = {}
    try:
        for path in changed:
            backup = Path(str(path) + ".bak-pre-kanban-delegated-progress-v1")
            shutil.copy2(path, backup)
            backups[path] = backup
            path.write_text(patched[path], encoding="utf-8")
    except Exception:
        for path, backup in backups.items():
            shutil.copy2(backup, path)
        raise
    return True
