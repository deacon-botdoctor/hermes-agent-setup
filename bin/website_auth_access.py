#!/usr/bin/env python3
"""Choose a website-login strategy without exposing it to browser callers.

The public interface intentionally remains ``handoff_auth(site, reason, lane)``.
Human takeover and the 1Password capability broker are separate adapters.  The
agent-facing browser code never receives a credential, item reference, vault
name, selector, or Telegram detail.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import stat
from pathlib import Path
from urllib.parse import urlparse

from human_auth_handoff import handoff_auth as _human_handoff

_RESULTS = {"done", "skip", "timeout"}
_LANES = {"computer_use", "browser_lane"}
_BROKER_STRATEGY = "onepassword_broker"
_HUMAN_STRATEGY = "human_handoff"
_MAX_FRAME_BYTES = 4096


class BrokerUnavailable(RuntimeError):
    """The configured broker cannot safely accept this request."""


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _policy_path() -> Path:
    configured = os.environ.get("HERMES_WEBSITE_AUTH_POLICY", "").strip()
    return Path(configured) if configured else _hermes_home() / "config/website-auth.json"


def _broker_socket_path() -> Path:
    configured = os.environ.get("HERMES_WEBSITE_AUTH_BROKER_SOCKET", "").strip()
    return Path(configured) if configured else _hermes_home() / "state/website-auth-broker.sock"


def _broker_uid() -> int | None:
    configured = os.environ.get("HERMES_WEBSITE_AUTH_BROKER_UID", "").strip()
    if not configured:
        return None
    try:
        uid = int(configured)
    except ValueError as exc:
        raise BrokerUnavailable("website auth broker UID is invalid") from exc
    if uid < 0:
        raise BrokerUnavailable("website auth broker UID is invalid")
    return uid


def _site_host(site: str) -> str:
    value = str(site or "").strip().lower()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").rstrip(".")


def _strategy_for(site: str) -> str:
    """Return an exact-host strategy; malformed or absent policy stays human."""
    host = _site_host(site)
    if not host:
        return _HUMAN_STRATEGY
    try:
        policy = json.loads(_policy_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _HUMAN_STRATEGY
    if not isinstance(policy, dict) or policy.get("version") != 1:
        return _HUMAN_STRATEGY
    routes = policy.get("routes")
    if not isinstance(routes, list):
        return _HUMAN_STRATEGY
    for route in routes:
        if not isinstance(route, dict):
            continue
        if _site_host(str(route.get("site") or "")) != host:
            continue
        strategy = route.get("strategy")
        return strategy if strategy in {_HUMAN_STRATEGY, _BROKER_STRATEGY} else _HUMAN_STRATEGY
    return _HUMAN_STRATEGY


def _trusted_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BrokerUnavailable("website auth broker socket is unavailable") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise BrokerUnavailable("website auth broker endpoint is not a Unix socket")
    if info.st_mode & 0o002:
        raise BrokerUnavailable("website auth broker socket is writable by everyone")
    configured_uid = _broker_uid()
    trusted_uids = {0, configured_uid} if configured_uid is not None else {0, os.geteuid()}
    if info.st_uid not in trusted_uids:
        raise BrokerUnavailable("website auth broker socket has an unexpected owner")


def _read_frame(channel: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = channel.recv(min(1024, _MAX_FRAME_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_FRAME_BYTES:
            raise BrokerUnavailable("website auth broker response exceeded its limit")
        if b"\n" in chunk:
            break
    payload = b"".join(chunks).split(b"\n", 1)[0]
    if not payload:
        raise BrokerUnavailable("website auth broker returned an empty response")
    return payload


def _broker_handoff(site: str, reason: str, lane: str) -> str:
    session_handle = os.environ.get("HERMES_AUTH_SESSION_HANDLE", "").strip()
    if not session_handle:
        raise BrokerUnavailable("website auth broker requires an opaque browser-session handle")
    path = _broker_socket_path()
    _trusted_socket(path)
    request = {
        "version": 1,
        "request_id": secrets.token_hex(16),
        "site": _site_host(site),
        "reason": str(reason)[:240],
        "lane": lane,
        "session_handle": session_handle,
    }
    encoded = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_FRAME_BYTES:
        raise BrokerUnavailable("website auth broker request exceeded its limit")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(10)
            channel.connect(str(path))
            channel.sendall(encoded)
            response = json.loads(_read_frame(channel))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrokerUnavailable("website auth broker request failed") from exc
    if not isinstance(response, dict) or set(response) - {"status", "receipt_id"}:
        raise BrokerUnavailable("website auth broker returned an invalid response")
    status = response.get("status")
    if status not in _RESULTS:
        raise BrokerUnavailable("website auth broker returned an invalid status")
    return status


def handoff_auth(site: str, reason: str, lane: str) -> str:
    """Return done/skip/timeout through the configured private adapter."""
    if lane not in _LANES:
        raise ValueError(f"unsupported lane: {lane}")
    if _strategy_for(site) == _BROKER_STRATEGY:
        try:
            return _broker_handoff(site, reason, lane)
        except BrokerUnavailable:
            # The operator can still complete the same frozen session.  A
            # broker's explicit Skip/Timeout above remains terminal and never
            # falls through to a second authentication attempt.
            pass
    return _human_handoff(site, reason, lane)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="website-auth-access")
    sub = parser.add_subparsers(dest="command", required=True)
    handoff = sub.add_parser("handoff")
    handoff.add_argument("--site", required=True)
    handoff.add_argument("--reason", required=True)
    handoff.add_argument("--lane", required=True, choices=sorted(_LANES))
    dry_run = sub.add_parser("dry-run")
    dry_run.add_argument("--site", default="example.com")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "handoff":
        print(json.dumps({"status": handoff_auth(args.site, args.reason, args.lane)}))
        return 0
    print(json.dumps({"status": "dry_run", "site": _site_host(args.site), "strategy": _strategy_for(args.site)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
