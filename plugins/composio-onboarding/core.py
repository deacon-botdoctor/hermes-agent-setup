"""Client-scoped Composio connection state and route truth."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOOLKIT_ALIASES = {
    "email": "gmail",
    "mail": "gmail",
    "google-mail": "gmail",
    "googlemail": "gmail",
    "calendar": "googlecalendar",
    "drive": "googledrive",
    "docs": "googledocs",
    "sheets": "googlesheets",
    "tasks": "googletasks",
    "sharepoint": "share_point",
    "share-point": "share_point",
    "microsoft-sharepoint": "share_point",
}


class OnboardingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Boundary:
    client_slug: str
    agent_profile: str
    composio_client_slug: str
    has_api_key: bool

    @property
    def matches(self) -> bool:
        return self.client_slug == self.composio_client_slug


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    slug = re.sub(r"-+", "-", slug)
    if not slug:
        raise OnboardingError("slug cannot be empty")
    return slug


def normalize_alias(value: str) -> str:
    return slugify(value).replace("-", "_").strip("_")


def normalize_toolkit(value: str) -> str:
    toolkit = slugify(value)
    return TOOLKIT_ALIASES.get(toolkit, toolkit)


def default_db_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return Path(os.environ.get("COMPOSIO_ONBOARDING_DB") or home / "state" / "composio_onboarding.db")


def connect_db(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS composio_onboarding_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS composio_onboarding_sessions (
            id TEXT PRIMARY KEY,
            client_slug TEXT NOT NULL,
            agent_profile TEXT NOT NULL,
            telegram_chat_id TEXT NOT NULL,
            telegram_user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS composio_account_slots (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES composio_onboarding_sessions(id) ON DELETE CASCADE,
            toolkit TEXT NOT NULL,
            alias TEXT NOT NULL,
            label TEXT NOT NULL,
            expected_email TEXT,
            verified_email TEXT,
            composio_user_id TEXT NOT NULL,
            connected_account_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            link_sent_at TEXT,
            verified_at TEXT,
            UNIQUE(session_id, toolkit, alias)
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO composio_onboarding_meta(key, value) VALUES(?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def boundary_from_env(client_slug: str, agent_profile: str | None = None) -> Boundary:
    client = slugify(client_slug)
    declared = os.environ.get("COMPOSIO_CLIENT_SLUG") or ""
    return Boundary(
        client_slug=client,
        agent_profile=agent_profile or os.environ.get("HERMES_PROFILE") or client,
        composio_client_slug=slugify(declared) if declared.strip() else "",
        has_api_key=bool(os.environ.get("COMPOSIO_API_KEY") or os.environ.get("HERMES_COMPOSIO_API_KEY")),
    )


def assert_boundary(boundary: Boundary) -> None:
    if not boundary.has_api_key:
        raise OnboardingError("a client-scoped Composio API key is required before creating OAuth links")
    if not boundary.composio_client_slug:
        raise OnboardingError("COMPOSIO_CLIENT_SLUG must identify the client that owns this key")
    if not boundary.matches:
        raise OnboardingError(
            "Composio boundary mismatch: "
            f"requested client={boundary.client_slug!r}, key owner={boundary.composio_client_slug!r}"
        )


def stable_composio_user_id(client_slug: str, telegram_user_id: str) -> str:
    return f"client:{slugify(client_slug)}:telegram:{str(telegram_user_id).strip()}"


def new_id(prefix: str, *parts: str) -> str:
    raw = "|".join([prefix, *parts, secrets.token_urlsafe(16)])
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _one(conn: sqlite3.Connection, table: str, row_id: str) -> dict[str, Any]:
    if table not in {"composio_onboarding_sessions", "composio_account_slots"}:
        raise OnboardingError("invalid onboarding table")
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise OnboardingError(f"unknown onboarding record: {row_id}")
    return dict(row)


def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    return _one(conn, "composio_onboarding_sessions", session_id)


def get_slot(conn: sqlite3.Connection, slot_id: str) -> dict[str, Any]:
    return _one(conn, "composio_account_slots", slot_id)


def start_session(
    conn: sqlite3.Connection,
    *,
    client_slug: str,
    agent_profile: str,
    telegram_chat_id: str,
    telegram_user_id: str,
) -> dict[str, Any]:
    client = slugify(client_slug)
    profile = slugify(agent_profile)
    sid = new_id("cos", client, profile, str(telegram_chat_id), str(telegram_user_id))
    conn.execute(
        """INSERT INTO composio_onboarding_sessions
           (id, client_slug, agent_profile, telegram_chat_id, telegram_user_id)
           VALUES (?, ?, ?, ?, ?)""",
        (sid, client, profile, str(telegram_chat_id), str(telegram_user_id)),
    )
    conn.commit()
    return get_session(conn, sid)


def add_slot(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    toolkit: str,
    alias: str,
    label: str,
    expected_email: str | None = None,
) -> dict[str, Any]:
    session = get_session(conn, session_id)
    toolkit_slug = normalize_toolkit(toolkit)
    alias_slug = normalize_alias(alias)
    if expected_email and not EMAIL_RE.match(expected_email):
        raise OnboardingError("expected_email does not look like an email address")
    slot_id = new_id("coa", session_id, toolkit_slug, alias_slug)
    conn.execute(
        """INSERT INTO composio_account_slots
           (id, session_id, toolkit, alias, label, expected_email, composio_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            slot_id,
            session_id,
            toolkit_slug,
            alias_slug,
            label.strip() or alias_slug,
            expected_email,
            stable_composio_user_id(session["client_slug"], session["telegram_user_id"]),
        ),
    )
    conn.commit()
    return get_slot(conn, slot_id)


def link_context(conn: sqlite3.Connection, slot_id: str, boundary: Boundary) -> dict[str, Any]:
    assert_boundary(boundary)
    slot = get_slot(conn, slot_id)
    session = get_session(conn, slot["session_id"])
    if session["client_slug"] != boundary.client_slug:
        raise OnboardingError("OAuth slot belongs to a different client")
    if slot["status"] != "pending":
        raise OnboardingError(f"slot status {slot['status']!r} cannot issue an OAuth link")
    return {
        "client_slug": session["client_slug"],
        "agent_profile": session["agent_profile"],
        "toolkit": slot["toolkit"],
        "alias": slot["alias"],
        "label": slot["label"],
        "expected_email": slot["expected_email"],
        "composio_user_id": slot["composio_user_id"],
    }


def _mark(conn: sqlite3.Connection, slot_id: str, *, status: str, error: str | None = None) -> dict[str, Any]:
    conn.execute(
        "UPDATE composio_account_slots SET status=?, last_error=? WHERE id=?",
        (status, error[:500] if error else None, slot_id),
    )
    conn.commit()
    return get_slot(conn, slot_id)


def mark_link_sent(conn: sqlite3.Connection, slot_id: str, connected_account_id: str) -> dict[str, Any]:
    if not connected_account_id.strip():
        raise OnboardingError("connected_account_id is required")
    conn.execute(
        """UPDATE composio_account_slots
           SET status='link_sent', connected_account_id=?, last_error=NULL,
               link_sent_at=CURRENT_TIMESTAMP WHERE id=?""",
        (connected_account_id.strip(), slot_id),
    )
    conn.commit()
    return get_slot(conn, slot_id)


def verify_provider_slot(
    conn: sqlite3.Connection,
    slot_id: str,
    *,
    connected_account_id: str,
    composio_user_id: str,
    toolkit: str,
    verified_email: str | None = None,
) -> dict[str, Any]:
    slot = get_slot(conn, slot_id)
    if slot["status"] != "link_sent":
        raise OnboardingError(f"slot status {slot['status']!r} cannot be verified")
    if str(composio_user_id).strip() != slot["composio_user_id"]:
        raise OnboardingError("connected account belongs to a different Composio user")
    if normalize_toolkit(toolkit) != slot["toolkit"]:
        raise OnboardingError("connected account belongs to a different toolkit")
    if str(connected_account_id).strip() != str(slot["connected_account_id"] or ""):
        raise OnboardingError("connected account id does not match the issued link")
    if verified_email and not EMAIL_RE.match(verified_email):
        raise OnboardingError("verified_email does not look like an email address")
    expected = (slot.get("expected_email") or "").lower()
    actual = (verified_email or "").lower()
    if expected and not actual:
        _mark(conn, slot_id, status="failed", error="verified account identity was not returned")
        raise OnboardingError("verified account identity is required for the requested account")
    if expected and expected != actual:
        _mark(conn, slot_id, status="failed", error="verified account did not match the requested account")
        raise OnboardingError("verified account did not match the requested account")

    session = get_session(conn, slot["session_id"])
    conn.execute(
        """UPDATE composio_account_slots
           SET status='superseded', last_error='superseded by a reverified account'
           WHERE id != ? AND toolkit=? AND alias=? AND status='verified'
             AND session_id IN (
                 SELECT id FROM composio_onboarding_sessions WHERE client_slug=?
             )""",
        (slot_id, slot["toolkit"], slot["alias"], session["client_slug"]),
    )
    conn.execute(
        """UPDATE composio_account_slots
           SET status='verified', verified_email=?, verified_at=CURRENT_TIMESTAMP,
               last_error=NULL WHERE id=?""",
        (verified_email, slot_id),
    )
    conn.commit()
    return get_slot(conn, slot_id)


def invalidate_slot(
    conn: sqlite3.Connection,
    slot_id: str,
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    if status not in {"disconnected", "revoked", "unhealthy"}:
        raise OnboardingError("invalid connection status")
    slot = get_slot(conn, slot_id)
    if slot["status"] == status:
        return slot
    if slot["status"] != "verified":
        raise OnboardingError("only a verified connection can be invalidated")
    if not reason.strip():
        raise OnboardingError("an invalidation reason is required")
    return _mark(conn, slot_id, status=status, error=reason)


def invalidate_connection(
    conn: sqlite3.Connection,
    *,
    client_slug: str,
    service: str,
    alias: str = "primary",
    status: str = "disconnected",
    reason: str,
) -> dict[str, Any]:
    row = conn.execute(
        """SELECT s.id
           FROM composio_account_slots s
           JOIN composio_onboarding_sessions x ON x.id=s.session_id
           WHERE x.client_slug=? AND s.toolkit=? AND s.alias=? AND s.status='verified'
           ORDER BY s.verified_at DESC LIMIT 1""",
        (slugify(client_slug), normalize_toolkit(service), normalize_alias(alias)),
    ).fetchone()
    if row is None:
        raise OnboardingError("no verified connection matches that client, service, and alias")
    return invalidate_slot(conn, row["id"], status=status, reason=reason)


def active_connections(
    conn: sqlite3.Connection,
    service: str | None = None,
    client_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Return safe route metadata; never expose keys, tokens, or account IDs."""
    params: list[str] = []
    where = "s.status='verified'"
    if service:
        where += " AND s.toolkit=?"
        params.append(normalize_toolkit(service))
    if client_slug:
        where += " AND x.client_slug=?"
        params.append(slugify(client_slug))
    rows = conn.execute(
        f"""SELECT x.client_slug, x.agent_profile, s.toolkit, s.alias, s.label,
                   s.verified_email, s.verified_at
            FROM composio_account_slots s
            JOIN composio_onboarding_sessions x ON x.id=s.session_id
            WHERE {where}
            ORDER BY s.toolkit, s.alias, s.verified_at DESC""",
        params,
    ).fetchall()
    return [
        {
            "client_slug": row["client_slug"],
            "agent_profile": row["agent_profile"],
            "service": row["toolkit"],
            "provider": "composio",
            "alias": row["alias"],
            "label": row["label"],
            "identity": row["verified_email"],
            "status": "verified",
            "verified_at": row["verified_at"],
            "route_family": "composio",
        }
        for row in rows
    ]
