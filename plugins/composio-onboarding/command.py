"""Telegram-facing commands for client-scoped Composio onboarding."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import core


@dataclass(frozen=True)
class ConnectRequest:
    toolkit: str
    alias: str
    expected_email: str | None
    label: str


TOOL_EXAMPLES = (
    ("gmail", "read, search, draft, and send email"),
    ("googlecalendar", "read availability and create events"),
    ("googledrive", "find and summarize Drive files"),
    ("googlesheets", "read and update spreadsheets"),
    ("slack", "search channels and draft replies"),
    ("hubspot", "look up contacts, companies, and deals"),
)


def _telegram_context_error(hook_ctx: dict[str, Any] | None, command: str) -> str | None:
    context = hook_ctx or {}
    platform = str(context.get("platform") or "").lower()
    if platform and platform != "telegram":
        return "Composio connection commands are currently Telegram-only."
    if not str(context.get("user_id") or "").strip() or not str(context.get("chat_id") or "").strip():
        return f"Blocked: /{command} requires the authenticated Telegram command context."
    return None


def parse_connect_args(raw_args: str | None) -> ConnectRequest | None:
    parts = (raw_args or "").strip().split()
    if not parts or parts[0].lower() in {"help", "?", "-h", "--help"}:
        return None
    toolkit = core.normalize_toolkit(parts[0])
    alias = "primary"
    expected_email = None
    if len(parts) >= 2:
        if "@" in parts[1]:
            expected_email = parts[1]
        else:
            alias = core.normalize_alias(parts[1])
    if len(parts) >= 3:
        expected_email = parts[2]
    return ConnectRequest(
        toolkit=toolkit,
        alias=alias,
        expected_email=expected_email,
        label=alias.replace("_", " ").title(),
    )


def help_text() -> str:
    return "\n".join(
        [
            "Composio connection setup",
            "Tell your agent what you want to connect, or use /connect directly:",
            *[f"- {tool}: {desc}" for tool, desc in TOOL_EXAMPLES],
            "",
            "Usage:",
            "- /connect gmail primary user@example.com",
            "- /connect googlecalendar primary",
            "- /connect googledrive files",
            "",
            "I will use this client's Composio project, request only this app, and remember Composio "
            "as the canonical route after verification.",
        ]
    )


def _client_slug() -> str:
    raw = (
        os.environ.get("HERMES_CLIENT_SLUG")
        or os.environ.get("COMPOSIO_CLIENT_SLUG")
        or os.environ.get("HERMES_PROFILE")
        or ""
    )
    if not raw.strip():
        raise core.OnboardingError("HERMES_CLIENT_SLUG or COMPOSIO_CLIENT_SLUG is required for /connect")
    return core.slugify(raw)


def _api_key() -> str:
    return (os.environ.get("COMPOSIO_API_KEY") or os.environ.get("HERMES_COMPOSIO_API_KEY") or "").strip()


def _api_base() -> str:
    base = os.environ.get("COMPOSIO_API_BASE_URL", "https://backend.composio.dev").strip().rstrip("/")
    if not base.startswith("https://"):
        raise core.OnboardingError("COMPOSIO_API_BASE_URL must be https")
    return base


def _composio_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    key = _api_key()
    if not key:
        raise core.OnboardingError("client Composio project API key is not available")
    request = Request(
        f"{_api_base()}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=int(os.environ.get("COMPOSIO_API_TIMEOUT", "20"))) as response:
            parsed = json.loads(response.read().decode() or "{}")
    except HTTPError as exc:
        raise core.OnboardingError(f"Composio API rejected the request (HTTP {exc.code})") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise core.OnboardingError(f"Composio API request failed: {type(exc).__name__}") from exc
    if not isinstance(parsed, dict):
        raise core.OnboardingError("Composio API returned an invalid response")
    return parsed


def _auth_config_id(toolkit: str) -> str:
    specific = f"COMPOSIO_AUTH_CONFIG_{toolkit.upper().replace('-', '_')}"
    configured = (os.environ.get(specific) or os.environ.get("COMPOSIO_AUTH_CONFIG_ID") or "").strip()
    if configured:
        return configured
    query = urlencode(
        {
            "toolkit_slug": toolkit,
            "is_composio_managed": "true",
            "show_disabled": "false",
            "limit": "10",
        }
    )
    response = _composio_request("GET", f"/api/v3/auth_configs?{query}")
    for item in response.get("items") or []:
        if not isinstance(item, dict):
            continue
        item_toolkit = item.get("toolkit") or {}
        if (
            str(item_toolkit.get("slug") or "").lower() == toolkit.lower()
            and str(item.get("status") or "").upper() == "ENABLED"
            and item.get("id")
        ):
            return str(item["id"])
    return ""


def _bootstrap_url(client_slug: str, toolkit: str) -> str:
    base = os.environ.get("COMPOSIO_PROJECT_BOOTSTRAP_URL", "https://app.composio.dev/").strip()
    if not base.startswith("https://"):
        raise core.OnboardingError("COMPOSIO_PROJECT_BOOTSTRAP_URL must be https")
    return f"{base.rstrip('/')}?{urlencode({'client': client_slug, 'toolkit': toolkit})}"


def _bootstrap_message(req: ConnectRequest, client_slug: str, reason: str) -> str:
    lines = [
        f"Composio setup needed for {client_slug}",
        f"Requested tool: {req.toolkit}",
        f"Account slot: {req.alias}",
    ]
    if req.expected_email:
        lines.append(f"Expected account: {req.expected_email}")
    lines.extend(
        [
            "",
            f"Open this client's Composio project: {_bootstrap_url(client_slug, req.toolkit)}",
            "Create or select the app's auth config there. If this client project key is not installed yet, "
            "the authorized client may send it here in this private lane; the agent should store it without "
            "echoing it or demanding rotation solely because chat was used.",
            "Then retry /connect.",
            "",
            f"Reason: {reason}",
        ]
    )
    return "\n".join(lines)


def _issue_api_link(context: dict[str, Any], auth_config_id: str) -> tuple[str, str]:
    payload = {
        "auth_config_id": auth_config_id,
        "user_id": context["composio_user_id"],
        "alias": context["alias"],
    }
    callback_url = os.environ.get("COMPOSIO_CALLBACK_URL", "").strip()
    if callback_url:
        if not callback_url.startswith("https://"):
            raise core.OnboardingError("COMPOSIO_CALLBACK_URL must be https")
        payload["callback_url"] = callback_url
    response = _composio_request("POST", "/api/v3/connected_accounts/link", payload)
    url = str(response.get("redirect_url") or "").strip()
    account_id = str(response.get("connected_account_id") or "").strip()
    if not url.startswith("https://") or not account_id:
        raise core.OnboardingError("Composio link response omitted the redirect URL or account id")
    return url, account_id


def _identity_from_account(account: dict[str, Any]) -> str | None:
    data = account.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("email", "email_address", "account_email", "username"):
        value = str(data.get(key) or "").strip()
        if "@" in value:
            return value
    return None


def _reconcile_connections(conn, client_slug: str) -> None:
    rows = conn.execute(
        """SELECT s.id, s.connected_account_id
           FROM composio_account_slots s
           JOIN composio_onboarding_sessions x ON x.id=s.session_id
           WHERE x.client_slug=? AND s.status='link_sent'
             AND s.connected_account_id IS NOT NULL""",
        (client_slug,),
    ).fetchall()
    for row in rows:
        account = _composio_request("GET", f"/api/v3/connected_accounts/{row['connected_account_id']}")
        if str(account.get("status") or "").upper() != "ACTIVE":
            continue
        toolkit = account.get("toolkit") or {}
        core.verify_provider_slot(
            conn,
            row["id"],
            connected_account_id=str(account.get("id") or row["connected_account_id"]),
            composio_user_id=str(account.get("user_id") or ""),
            toolkit=str(toolkit.get("slug") or ""),
            verified_email=_identity_from_account(account),
        )


def handle_connect_command(raw_args: str | None, hook_ctx: dict[str, Any] | None = None) -> str:
    req = parse_connect_args(raw_args)
    if req is None:
        return help_text()
    context_error = _telegram_context_error(hook_ctx, "connect")
    if context_error:
        return context_error

    client_slug = _client_slug()
    agent_profile = os.environ.get("HERMES_PROFILE") or client_slug
    boundary = core.boundary_from_env(client_slug, agent_profile)
    reason = None
    if not boundary.has_api_key:
        reason = "client Composio project API key is not available yet"
    elif not boundary.composio_client_slug:
        reason = "COMPOSIO_CLIENT_SLUG is not set for this client-owned Composio project"
    elif not boundary.matches:
        reason = f"Composio key owner {boundary.composio_client_slug!r} does not match client {client_slug!r}"
    if reason:
        return _bootstrap_message(req, client_slug, reason)

    auth_config_id = _auth_config_id(req.toolkit)
    if not auth_config_id:
        return _bootstrap_message(req, client_slug, f"no enabled auth config exists for {req.toolkit!r}")

    context = hook_ctx or {}
    conn = core.connect_db()
    session = core.start_session(
        conn,
        client_slug=client_slug,
        agent_profile=agent_profile,
        telegram_chat_id=str(context.get("chat_id") or ""),
        telegram_user_id=str(context.get("user_id") or ""),
    )
    slot = core.add_slot(
        conn,
        session_id=session["id"],
        toolkit=req.toolkit,
        alias=req.alias,
        label=req.label,
        expected_email=req.expected_email,
    )
    link_context = core.link_context(conn, slot["id"], boundary)
    link, connected_account_id = _issue_api_link(link_context, auth_config_id)
    core.mark_link_sent(conn, slot["id"], connected_account_id)

    lines = [
        f"Composio setup for {client_slug}",
        f"Tool: {link_context['toolkit']}",
        f"Account slot: {link_context['alias']}",
    ]
    if link_context.get("expected_email"):
        lines.append(f"Expected account: {link_context['expected_email']}")
    lines.extend(
        ["", f"Connect this account: {link}", "After OAuth, I will verify the account before using it."]
    )
    return "\n".join(lines)


def handle_connections_command(raw_args: str | None = None, hook_ctx: dict[str, Any] | None = None) -> str:
    context_error = _telegram_context_error(hook_ctx, "connections")
    if context_error:
        return context_error
    service = (raw_args or "").strip().split(maxsplit=1)[0] if (raw_args or "").strip() else None
    client_slug = _client_slug()
    conn = core.connect_db()
    _reconcile_connections(conn, client_slug)
    routes = core.active_connections(conn, service, client_slug=client_slug)
    if not routes:
        target = f" for {service}" if service else ""
        return f"No verified Composio connections{target}. Say what you want to connect or use /connect <service>."
    lines = ["Verified connections (canonical routes):"]
    for route in routes:
        identity = f" — {route['identity']}" if route.get("identity") else ""
        lines.append(f"- {route['service']} ({route['alias']}): Composio{identity}")
    lines.extend(
        ["", "Use these Composio routes. Do not start direct provider OAuth for the same services."]
    )
    return "\n".join(lines)


def handle_disconnect_command(raw_args: str | None = None, hook_ctx: dict[str, Any] | None = None) -> str:
    parts = (raw_args or "").strip().split()
    if not parts:
        return "Usage: /disconnect <service> [alias]"
    context_error = _telegram_context_error(hook_ctx, "disconnect")
    if context_error:
        return context_error
    service = core.normalize_toolkit(parts[0])
    alias = core.normalize_alias(parts[1]) if len(parts) > 1 else "primary"
    core.invalidate_connection(
        core.connect_db(),
        client_slug=_client_slug(),
        service=service,
        alias=alias,
        status="disconnected",
        reason="disconnected through the client command surface",
    )
    return (
        f"Disconnected {service} ({alias}) from the canonical Composio route. "
        f"Use /connect {service} {alias} to reconnect."
    )
