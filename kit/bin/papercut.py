#!/usr/bin/env python3
"""Record small operational failures without blocking the current task."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
KINDS = ("routing", "update", "tool", "auth", "dependency", "other")
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[^\s,;]+"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),  # Telegram-like token
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def redact(value: str) -> str:
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value[:1200]


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def build_event(args: argparse.Namespace) -> dict:
    created_at = now()
    raw = "|".join((created_at, socket.gethostname(), args.kind, args.operation, args.summary))
    event_id = "pc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {
        "schema_version": 1,
        "id": event_id,
        "created_at": created_at,
        "kind": args.kind,
        "severity": args.severity,
        "operation": redact(args.operation),
        "summary": redact(args.summary),
        "route": redact(args.route),
        "target": redact(args.target),
        "evidence": redact(args.evidence),
        "host": socket.gethostname(),
        "platform": platform.system().lower(),
        "agent": os.environ.get("HERMES_AGENT_ID") or os.environ.get("AGENT_ID") or "",
        "session": redact(os.environ.get("CODEX_THREAD_ID") or os.environ.get("HERMES_SESSION_ID") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a small agent papercut for fleet review.")
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument(
        "--summary", required=True, help="What failed or was needlessly difficult; never include secrets."
    )
    parser.add_argument("--operation", default="agent-work", help="Examples: fleet-update, Windows-rollout, tool-call")
    parser.add_argument("--route", default="", help="Route/host/path that failed, if known")
    parser.add_argument("--target", default="", help="Client/runtime affected, if known")
    parser.add_argument("--evidence", default="", help="Short sanitized error or observation")
    parser.add_argument("--severity", choices=("info", "warning", "error"), default="warning")
    parser.add_argument("--no-submit", action="store_true", help="Backward-compatible no-op; capture is always local.")
    args = parser.parse_args()

    event = build_event(args)
    append_jsonl(hermes_home() / "state" / "papercuts" / "events.jsonl", event)
    print(json.dumps({"ok": True, "id": event["id"], "delivered": False, "detail": "local-only"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
