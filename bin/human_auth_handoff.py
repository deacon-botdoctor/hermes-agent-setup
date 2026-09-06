#!/usr/bin/env python3
"""Human-only website authentication handoff for Hermes v0.

This module intentionally has no credential input.  It renders a Telegram
card, freezes the calling lane while it waits for an operator decision, and
returns only ``done``, ``skip``, or ``timeout``.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import secrets
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

try:  # POSIX record lock
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    _fcntl = None

try:  # Windows record lock
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    _msvcrt = None

HandoffResult = Literal["done", "skip", "timeout"]
Lane = Literal["computer_use", "browser_lane"]

_HANDOFF_ID = re.compile(r"^[a-f0-9]{24}$")
_RESULTS = {"done", "skip"}
_LANES = {"computer_use", "browser_lane"}


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _state_dir() -> Path:
    configured = os.environ.get("HERMES_AUTH_HANDOFF_STATE_DIR")
    return Path(configured).expanduser() if configured else _hermes_home() / "state/auth-handoff"


def _timeout_seconds() -> float:
    try:
        value = float(os.environ.get("HERMES_AUTH_HANDOFF_TIMEOUT_S", "600"))
    except ValueError:
        value = 600.0
    return max(1.0, value) if math.isfinite(value) else 600.0


def _clean_label(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] or "unknown"


def _record_path(handoff_id: str) -> Path:
    if not _HANDOFF_ID.fullmatch(handoff_id):
        raise ValueError("invalid handoff id")
    return _state_dir() / f"{handoff_id}.json"


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _read_record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def _exclusive_record_lock(handle):
    """Hold a one-byte interprocess lock on POSIX or Windows."""
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is None:  # pragma: no cover - unsupported Python platform
        raise RuntimeError("no supported interprocess record lock is available")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("\0")
        handle.flush()
    handle.seek(0)
    _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
    try:
        yield
    finally:
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


def _transition_pending(
    path: Path,
    result: HandoffResult,
    *,
    caller_id: str = "",
    capability: str = "",
) -> bool:
    """Atomically resolve a pending record; exactly one terminal result wins."""
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        with _exclusive_record_lock(lock):
            record = _read_record(path)
            if record.get("status") != "pending":
                return False
            if result in _RESULTS:
                expected = str(record.get("resolution_capability_sha256") or "")
                observed = sha256(capability.encode("utf-8")).hexdigest()
                if not expected or not secrets.compare_digest(expected, observed):
                    return False
            initiator = str(record.get("initiator_user_id") or "")
            if initiator and result in _RESULTS and caller_id != initiator:
                return False
            record["status"] = result
            record["resolved_at"] = time.time()
            _write_record(path, record)
            return True


def _target() -> tuple[str, str | None]:
    chat = os.environ.get("HERMES_AUTH_HANDOFF_CHAT_ID", "").strip()
    thread = os.environ.get("HERMES_AUTH_HANDOFF_THREAD_ID", "").strip() or None
    if chat:
        return chat, thread

    if os.environ.get("HERMES_SESSION_PLATFORM", "").strip().lower() == "telegram":
        chat = os.environ.get("HERMES_SESSION_CHAT_ID", "").strip()
        thread = os.environ.get("HERMES_SESSION_THREAD_ID", "").strip() or None
        if chat:
            return chat, thread

    target_name = os.environ.get("HERMES_AUTH_HANDOFF_TARGET", "").strip()
    registry = _hermes_home() / "config/channel-registry.json"
    if target_name:
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            target = data["targets"][target_name]
            return str(target["chat"]), str(target.get("thread") or "") or None
        except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"auth handoff target {target_name!r} is not configured") from exc

    home_channel = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if not home_channel:
        home_channel = _config_scalar("TELEGRAM_HOME_CHANNEL")
    if home_channel:
        return _parse_telegram_target(home_channel)
    raise RuntimeError(
        "local Telegram target is unavailable; set HERMES_AUTH_HANDOFF_CHAT_ID, "
        "HERMES_AUTH_HANDOFF_TARGET, or TELEGRAM_HOME_CHANNEL"
    )


def _config_scalar(key: str) -> str:
    """Read a top-level scalar without making the coordinator depend on YAML."""
    config = _hermes_home() / "config.yaml"
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{key}:"
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value.strip()
    return ""


def _parse_telegram_target(value: str) -> tuple[str, str | None]:
    target = value.strip()
    if target.startswith("telegram:"):
        target = target[len("telegram:"):]
    chat, separator, thread = target.partition(":")
    if not chat:
        raise RuntimeError("TELEGRAM_HOME_CHANNEL is empty")
    return chat, thread or None if separator else None


def _human_window_reachable(lane: Lane) -> bool:
    """Fail closed when browser auth has no existing human-reachable window."""
    if lane == "computer_use":
        return True
    override = os.environ.get("HERMES_AUTH_HANDOFF_WINDOW_REACHABLE", "").strip().lower()
    if override:
        return override in {"1", "true", "yes", "on"}
    return False


def _remove_terminal_record(path: Path) -> None:
    for candidate in (path, path.with_suffix(".lock")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            if candidate != path.with_suffix(".lock"):
                raise
            # Windows may still have the resolver's one-byte lock open after
            # the terminal JSON becomes visible. The empty lock file is safe
            # to leave behind and will be reused by the next transition.
            pass


def render_card(
    site: str,
    reason: str,
    lane: Lane,
    handoff_id: str,
    capability: str,
    takeover_url: str = "",
) -> dict:
    if lane not in _LANES:
        raise ValueError(f"unsupported lane: {lane}")
    site_label = _clean_label(site, limit=120)
    reason_label = _clean_label(reason, limit=240)
    waiting = (
        "The window is waiting on the controlled computer."
        if lane == "computer_use"
        else "The browser_lane profile is waiting."
    )
    buttons = []
    if takeover_url:
        if lane != "browser_lane" or not takeover_url.startswith("https://"):
            raise ValueError("takeover URL is invalid")
        buttons.append(
            [{"text": "Open Login", "web_app": {"url": takeover_url}}]
        )
    buttons.append([
        {"text": "Done", "callback_data": f"hah:done:{handoff_id}:{capability}"},
        {"text": "Skip", "callback_data": f"hah:skip:{handoff_id}:{capability}"},
    ])
    return {
        "text": f"Login wall: {site_label}\nWhy: {reason_label}\n{waiting}",
        "buttons": buttons,
    }


def _send_card(card: dict, chat_override: str | None = None) -> None:
    chat_id, thread_id = (chat_override, None) if chat_override else _target()
    bin_dir = _hermes_home() / "bin"
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    from telegram_delivery import resolve_telegram_bot_token, send_message_payload

    token = resolve_telegram_bot_token()
    if not token:
        raise RuntimeError("Telegram bot token is unavailable")
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": str(card["text"]),
        "reply_markup": {"inline_keyboard": card["buttons"]},
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    send_message_payload(
        token=token,
        payload=payload,
        sender="human-auth-handoff",
        summary="website login handoff",
        timeout=30,
    )


def _takeover_rpc(method: str, params: dict) -> dict:
    socket_path = Path(
        os.environ.get("BROWSER_LANE_SOCKET")
        or (_hermes_home() / "browser-lane/daemon.sock")
    )
    request = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(8),
        "method": method,
        "params": params,
    }
    encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > 4096:
        raise RuntimeError("browser takeover request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(5)
        channel.connect(str(socket_path))
        channel.sendall(encoded)
        response = b""
        while b"\n" not in response and len(response) <= 16384:
            chunk = channel.recv(4096)
            if not chunk:
                break
            response += chunk
    payload = json.loads(response.split(b"\n", 1)[0])
    if not isinstance(payload, dict) or "error" in payload:
        raise RuntimeError("browser takeover request failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("browser takeover response is invalid")
    return result


def _create_takeover(handoff_id: str, telegram_user_id: str, ttl_s: int) -> str:
    session_handle = os.environ.get("HERMES_AUTH_SESSION_HANDLE", "").strip()
    if not session_handle or not telegram_user_id.isdigit():
        return ""
    try:
        result = _takeover_rpc(
            "takeover.create",
            {
                "handoff_id": handoff_id,
                "telegram_user_id": telegram_user_id,
                "session_handle": session_handle,
                "ttl_s": min(900, max(1, ttl_s)),
            },
        )
    except Exception:
        return ""
    url = result.get("url")
    return url if result.get("status") == "ok" and isinstance(url, str) else ""


def _takeover_status(handoff_id: str) -> str:
    try:
        status = _takeover_rpc("takeover.status", {"handoff_id": handoff_id}).get("status")
    except Exception:
        return "unavailable"
    return status if status in {"pending", "done", "skip", "timeout"} else "unavailable"


def _revoke_takeover(handoff_id: str) -> None:
    try:
        _takeover_rpc("takeover.revoke", {"handoff_id": handoff_id})
    except Exception:
        pass


def validate_webapp_init_data(
    handoff_id: str,
    init_data: str,
    bot_token: str,
    *,
    now: float | None = None,
) -> str:
    """Validate Telegram Mini App identity and bind it to a pending handoff."""
    if not _HANDOFF_ID.fullmatch(handoff_id) or not init_data or not bot_token:
        raise ValueError("Telegram Mini App data is incomplete")
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    observed_hash = fields.pop("hash", "")
    if not re.fullmatch(r"[a-f0-9]{64}", observed_hash):
        raise ValueError("Telegram Mini App signature is invalid")
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), sha256).digest()
    expected_hash = hmac.new(secret, check.encode(), sha256).hexdigest()
    if not secrets.compare_digest(expected_hash, observed_hash):
        raise ValueError("Telegram Mini App signature is invalid")
    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        user_id = str(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram Mini App identity is invalid") from exc
    current = time.time() if now is None else now
    if auth_date > current + 30 or current - auth_date > 900:
        raise ValueError("Telegram Mini App identity expired")
    record = _read_record(_record_path(handoff_id))
    if (
        record.get("status") != "pending"
        or not user_id.isdigit()
        or not secrets.compare_digest(
            str(record.get("initiator_user_id") or ""), user_id
        )
    ):
        raise ValueError("Telegram Mini App user is not authorized")
    return user_id


def handoff_auth(site: str, reason: str, lane: Lane) -> HandoffResult:
    """Freeze the caller until the local principal chooses Done/Skip or timeout."""
    if lane not in _LANES:
        raise ValueError(f"unsupported lane: {lane}")

    handoff_id = uuid.uuid4().hex[:24]
    resolution_capability = secrets.token_urlsafe(16)
    path = _record_path(handoff_id)
    now = time.time()
    telegram_initiator = (
        os.environ.get("HERMES_SESSION_USER_ID", "").strip()
        if os.environ.get("HERMES_SESSION_PLATFORM", "").strip().lower() == "telegram"
        else ""
    )
    record = {
        "id": handoff_id,
        "lane": lane,
        "site": _clean_label(site, limit=120),
        "reason": _clean_label(reason, limit=240),
        "status": "pending",
        "created_at": now,
        "resolution_capability_sha256": sha256(
            resolution_capability.encode("utf-8")
        ).hexdigest(),
        "initiator_user_id": telegram_initiator,
        "resolver_policy": (
            "initiating_user" if telegram_initiator else "authorized_operator"
        ),
    }
    _write_record(path, record)
    takeover_url = ""
    if lane == "browser_lane" and telegram_initiator:
        takeover_url = _create_takeover(
            handoff_id,
            telegram_initiator,
            int(_timeout_seconds()),
        )
    if not takeover_url and not _human_window_reachable(lane):
        _transition_pending(path, "timeout")
        _remove_terminal_record(path)
        return "timeout"
    card = render_card(
        record["site"],
        record["reason"],
        lane,
        handoff_id,
        resolution_capability,
        takeover_url,
    )
    try:
        if takeover_url:
            _send_card(card, telegram_initiator)
        else:
            _send_card(card)
    except Exception:
        if takeover_url:
            _revoke_takeover(handoff_id)
        _transition_pending(path, "timeout")
        _remove_terminal_record(path)
        return "timeout"

    deadline = time.monotonic() + _timeout_seconds()
    while time.monotonic() < deadline:
        current = _read_record(path)
        status = current.get("status")
        if status in _RESULTS:
            if takeover_url:
                _revoke_takeover(handoff_id)
            _remove_terminal_record(path)
            return status
        if takeover_url:
            takeover_status = _takeover_status(handoff_id)
            if takeover_status in _RESULTS:
                _transition_pending(
                    path,
                    takeover_status,
                    caller_id=telegram_initiator,
                    capability=resolution_capability,
                )
        time.sleep(0.2)

    if not _transition_pending(path, "timeout"):
        final_status = _read_record(path).get("status")
        if final_status in _RESULTS:
            if takeover_url:
                _revoke_takeover(handoff_id)
            _remove_terminal_record(path)
            return final_status
    if takeover_url:
        _revoke_takeover(handoff_id)
    _remove_terminal_record(path)
    return "timeout"


def resolve_handoff(
    handoff_id: str,
    result: str,
    caller_id: str = "",
    capability: str = "",
) -> bool:
    if result not in _RESULTS:
        raise ValueError("result must be done or skip")
    path = _record_path(handoff_id)
    return _transition_pending(
        path,
        result,
        caller_id=caller_id,
        capability=capability,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="human-auth-handoff")
    sub = parser.add_subparsers(dest="command", required=True)

    handoff = sub.add_parser("handoff")
    handoff.add_argument("--site", required=True)
    handoff.add_argument("--reason", required=True)
    handoff.add_argument("--lane", required=True, choices=sorted(_LANES))

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--handoff-id", required=True)
    resolve.add_argument("--result", required=True, choices=sorted(_RESULTS))
    resolve.add_argument("--caller-id", default="")

    validate = sub.add_parser("validate-webapp")
    validate.add_argument("--handoff-id", required=True)

    dry_run = sub.add_parser("dry-run")
    dry_run.add_argument("--site", default="example.com")
    dry_run.add_argument("--reason", default="session requires login")
    dry_run.add_argument("--lane", default="browser_lane", choices=sorted(_LANES))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "handoff":
        print(json.dumps({"status": handoff_auth(args.site, args.reason, args.lane)}))
        return 0
    if args.command == "resolve":
        capability = sys.stdin.readline(128).strip()
        resolved = resolve_handoff(
            args.handoff_id,
            args.result,
            args.caller_id,
            capability,
        )
        print(json.dumps({"status": "ok" if resolved else "stale"}))
        return 0 if resolved else 1
    if args.command == "validate-webapp":
        bin_dir = _hermes_home() / "bin"
        if str(bin_dir) not in sys.path:
            sys.path.insert(0, str(bin_dir))
        from telegram_delivery import resolve_telegram_bot_token

        token = resolve_telegram_bot_token()
        init_data = sys.stdin.readline(8193).strip()
        if not token or len(init_data) > 8192:
            return 1
        try:
            user_id = validate_webapp_init_data(args.handoff_id, init_data, token)
        except ValueError:
            return 1
        print(json.dumps({"user_id": user_id}))
        return 0
    card = render_card(args.site, args.reason, args.lane, "0" * 24, "A" * 22)
    print(json.dumps({"status": "dry_run", "card": card}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
