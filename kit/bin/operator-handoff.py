#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except Exception as exc:  # pragma: no cover - operator runtime check
    raise SystemExit(f"PyYAML is required for operator-handoff.py: {exc}")

HOME = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or "~").expanduser()
HERMES = Path(os.environ.get("HERMES_HOME") or str(HOME / '.hermes')).expanduser()
BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from telegram_delivery import MAX_READABLE_CHARS, send_document_message, send_text_message  # noqa: E402


UTC = timezone.utc


def iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    return data if isinstance(data, dict) else {}


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def default_identity(config: dict) -> tuple[str, str]:
    client_id = str(config.get('client_identity') or '').strip() or 'unknown-client'
    agent_name = str(config.get('agent_name') or config.get('assistant_name') or client_id).strip() or client_id
    return client_id, agent_name


def _short(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_message(args: argparse.Namespace, client_id: str, agent_name: str, machine: str) -> str:
    label = _short((args.label or 'NOW').strip().upper(), 12)
    client_id = _short(client_id, 48)
    agent_name = _short(agent_name, 48)
    machine = _short(machine, 48)
    lines = [f"{label}: operator escalation from {agent_name} ({client_id})"]
    lines.append(f"Client: {client_id}")
    lines.append(f"Agent: {agent_name}")
    lines.append(f"Machine: {machine}")
    lines.append(f"Issue: {_short(args.summary, 210)}")
    if args.need:
        lines.append(f"Need: {_short(args.need, 190)}")
    if args.why:
        lines.append(f"Why escalated: {_short(args.why, 190)}")
    if args.client_status:
        lines.append(f"Client status: {_short(args.client_status, 130)}")
    if args.refs:
        ref_count = sum(1 for ref in args.refs if str(ref).strip())
        if ref_count:
            lines.append(f"Evidence: {ref_count} refs in the structured packet and attached report.")
    text = "\n".join(lines)
    return text if len(text) <= MAX_READABLE_CHARS else text[:MAX_READABLE_CHARS - 1].rstrip() + "…"


def priority_from_label(label: str) -> str:
    value = (label or "").strip().upper()
    if value in {"BLOCKED", "NOW"}:
        return "P1"
    if value == "WATCH":
        return "P2"
    return "P3"


def write_direct_inbox(
    *,
    inbox_path: str,
    args: argparse.Namespace,
    client_id: str,
    agent_name: str,
    machine: str,
    text: str,
) -> dict:
    root = Path(inbox_path).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha1(f"{client_id}|{agent_name}|{args.summary}|{stamp}|{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:12]
    packet_id = f"handoff-{stamp}-{digest}"
    file_path = root / f"{stamp}-{priority_from_label(args.label).lower()}-remediation-{digest}.json"
    refs = [str(ref).strip() for ref in (args.refs or []) if str(ref).strip()]
    payload = {
        "schema_version": 2,
        "transport": "direct_inbox",
        "id": packet_id,
        "type": "remediation_packet",
        "classification": "business_internal",
        "scope": f"client:{client_id}",
        "from": client_id,
        "to": "doc",
        "sender": client_id,
        "receiver": "doc",
        "priority": priority_from_label(args.label),
        "subject": f"{(args.label or 'WATCH').strip().upper()}: {agent_name} auto-remediation triggered",
        "body": text,
        "action": args.need.strip(),
        "why_you": "You are Doc, the client operator running from Spark. Handle remediation, pattern flagging, and prevention planning here rather than pushing this directly into BotDoctor ops.",
        "constraint": "Do not send a user-visible report until remediation is understood. If Deacon needs to know, send only the after-report: what was received, what happened, what was fixed, and how we will prevent it next time.",
        "done_when": "Issue is remediated or clearly bounded, prevention notes are captured, and any needed after-report to Deacon is ready.",
        "refs": refs,
        "document": str(args.document).strip() if args.document else None,
        "document_caption": str(args.document_caption).strip() if args.document_caption else None,
        "client_id": client_id,
        "agent_name": agent_name,
        "machine": machine,
        "summary": args.summary.strip(),
        "need": args.need.strip(),
        "why": args.why.strip(),
        "client_status": args.client_status.strip(),
        "client_message": args.client_message.strip(),
        "generated_at": iso_now(),
    }
    atomic_json(file_path, payload)
    return {"ok": True, "transport": "direct_inbox", "packet_path": str(file_path), "packet_id": packet_id}


def write_local_outbox(*, outbox_path: str, args: argparse.Namespace, client_id: str, agent_name: str, machine: str, text: str) -> dict:
    root = Path(outbox_path).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha1(f"{client_id}|{agent_name}|{args.summary}|{stamp}|{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:12]
    packet_id = f"handoff-{stamp}-{digest}"
    file_path = root / f"{stamp}-{priority_from_label(args.label).lower()}-remediation-{digest}.json"
    payload = {
        "schema_version": 2,
        "transport": "local_outbox_relay",
        "id": packet_id,
        "type": "remediation_packet",
        "classification": "business_internal",
        "scope": f"client:{client_id}",
        "from": client_id,
        "to": "doc",
        "priority": priority_from_label(args.label),
        "body": text,
        "action": args.need.strip(),
        "refs": [str(ref).strip() for ref in args.refs if str(ref).strip()],
        "client_id": client_id,
        "agent_name": agent_name,
        "machine": machine,
        "summary": args.summary.strip(),
        "generated_at": iso_now(),
    }
    atomic_json(file_path, payload)
    return {"ok": True, "transport": "local_outbox_relay", "packet_path": str(file_path), "packet_id": packet_id}


def local_outbox_fallback(outbox_path: str, args: argparse.Namespace, client_id: str, agent_name: str, machine: str, text: str, reason: str) -> None:
    result = write_local_outbox(outbox_path=outbox_path, args=args, client_id=client_id, agent_name=agent_name, machine=machine, text=text)
    print(json.dumps({**result, "fallback_from": "telegram", "fallback_reason": _short(reason, 240), "client_id": client_id, "agent_name": agent_name}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description='Send a structured client-agent escalation to the Bot Doctor operator thread.')
    parser.add_argument('--summary', required=True, help='One-line description of the issue.')
    parser.add_argument('--need', default='Doc and Deacon should inspect this operator-side issue.', help='Requested operator action.')
    parser.add_argument('--why', default='This appears to require shared infrastructure, routing, or operator intervention beyond client-agent authority.', help='Why the agent is escalating instead of changing it directly.')
    parser.add_argument('--client-status', default='Client-visible issue is contained and has been escalated to the operator lane.', help='What the client is experiencing right now.')
    parser.add_argument('--client-message', default="This looks like an operator-side issue. I'm flagging it to Doc and Deacon now.", help='Plain-English line the agent can tell the client.')
    parser.add_argument('--label', default='NOW', help='Operator status label: NOW|WATCH|BLOCKED|DONE.')
    parser.add_argument('--client-id', default='', help='Override client identifier.')
    parser.add_argument('--agent-name', default='', help='Override agent name.')
    parser.add_argument('--machine', default='', help='Override machine/hostname.')
    parser.add_argument('--chat-id', default='', help='Override operator chat id instead of config.yaml operator_alerts.chat_id.')
    parser.add_argument('--thread-id', default='', help='Override operator thread id instead of config.yaml operator_alerts.thread_id.')
    parser.add_argument('--sender', default='operator-handoff', help='Proof sender label recorded in telegram-delivery proof log.')
    parser.add_argument('--ref', dest='refs', action='append', default=[], help='Optional supporting reference line. Repeat as needed.')
    parser.add_argument('--document', default='', help='Optional document path to attach after the text handoff.')
    parser.add_argument('--document-caption', default='', help='Optional caption for the attached document.')
    parser.add_argument('--dry-run', action='store_true', help='Print the resolved payload instead of sending it.')
    args = parser.parse_args()

    config = load_yaml(HERMES / 'config.yaml')
    operator = config.get('operator_alerts') if isinstance(config.get('operator_alerts'), dict) else {}
    inbox_path = str(operator.get('inbox_path') or '').strip()
    transport = str(operator.get('transport') or '').strip()
    outbox_path = str(operator.get('outbox_path') or '').strip() or str(HERMES / 'state' / 'operator-outbox')
    chat_id = str(args.chat_id or operator.get('chat_id') or '').strip()
    thread_id = str(args.thread_id or operator.get('thread_id') or '').strip()

    default_client_id, default_agent_name = default_identity(config)
    client_id = (args.client_id or default_client_id).strip()
    agent_name = (args.agent_name or default_agent_name).strip()
    machine = (args.machine or socket.gethostname()).strip()
    text = build_message(args, client_id, agent_name, machine)

    if transport == 'direct_inbox' or inbox_path:
        if not inbox_path:
            raise SystemExit('operator_alerts.inbox_path is not configured for direct_inbox transport')
        result = write_direct_inbox(
            inbox_path=inbox_path,
            args=args,
            client_id=client_id,
            agent_name=agent_name,
            machine=machine,
            text=text,
        )
        print(json.dumps({
            'ok': True,
            'transport': 'direct_inbox',
            'client_id': client_id,
            'agent_name': agent_name,
            'packet_path': result.get('packet_path'),
            'packet_id': result.get('packet_id'),
        }, indent=2))
        return

    if not chat_id:
        local_outbox_fallback(outbox_path, args, client_id, agent_name, machine, text, 'operator_alerts.chat_id is not configured')
        return

    env_vars = load_dotenv(HERMES / '.env')
    token = os.environ.get('TELEGRAM_BOT_TOKEN') or env_vars.get('TELEGRAM_BOT_TOKEN')
    if not token:
        local_outbox_fallback(outbox_path, args, client_id, agent_name, machine, text, 'TELEGRAM_BOT_TOKEN is unavailable')
        return

    payload = {
        'chat_id': chat_id,
        'thread_id': thread_id or None,
        'sender': args.sender,
        'text': text,
        'summary': args.summary.strip(),
    }
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return

    try:
        result = send_text_message(
            token=token,
            chat_id=chat_id,
            text=text,
            thread_id=thread_id or None,
            sender=args.sender,
            summary=f"operator escalation: {client_id}",
            detail=args.summary.strip(),
        )
        document_result = None
        if args.document:
            document_result = send_document_message(token=token, chat_id=chat_id, file_path=args.document, caption=(args.document_caption or args.summary or '').strip(), thread_id=thread_id or None, sender=args.sender, summary=f"operator escalation attachment: {client_id}", detail=str(args.document))
    except Exception as exc:
        local_outbox_fallback(outbox_path, args, client_id, agent_name, machine, text, f"{type(exc).__name__}: {exc}")
        return
    print(json.dumps({
        'ok': True,
        'chat_id': str(chat_id),
        'thread_id': str(thread_id) if thread_id else None,
        'message_id': result.get('message_id'),
        'document_message_id': (document_result or {}).get('message_id'),
        'client_id': client_id,
        'agent_name': agent_name,
    }, indent=2))


if __name__ == '__main__':
    main()
