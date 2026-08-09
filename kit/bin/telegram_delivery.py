#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or "~").expanduser()
HERMES = Path(os.environ.get("HERMES_HOME") or str(HOME / ".hermes")).expanduser()
PROOF_LOG = HERMES / "state" / "telegram-delivery-proof" / "proofs.jsonl"
OVERFLOW_DIR = HERMES / "state" / "telegram-overflow"
REPEAT_SUPPRESS_LEDGER = HERMES / "state" / "telegram-delivery-proof" / "repeat-suppression-ledger.json"
REPEAT_SUPPRESS_TTL_SEC = 6 * 3600

# --- Readability guard -------------------------------------------------------
# Policy (see agent-standards.md "human-readable surface rule"): anything a
# human can read must be human-written and glanceable. Raw tool output — tracebacks,
# result-status dumps, JSON blobs, walls of text — must never land in a human
# lane. This guard is the single enforcement point: every text send funnels
# through _send_payload, so a raw dump cannot reach a readable lane even if the
# calling script is sloppy. Machine-only surfaces opt out explicitly via
# allow_raw=True or by listing their chat[:thread] in TELEGRAM_RAW_OK_LANES.

MAX_READABLE_CHARS = int(os.environ.get("TELEGRAM_MAX_READABLE_CHARS", "1200"))
MAX_READABLE_LINES = int(os.environ.get("TELEGRAM_MAX_READABLE_LINES", "24"))
NATIVE_RICH_ENABLED = os.environ.get("HERMES_NATIVE_RICH_MESSAGES", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_STATUS_PREFIXES = {
    "done": "DONE",
    "fixed": "FIXED",
    "warn": "WARN",
    "warning": "WARN",
    "failed": "FAIL",
    "fail": "FAIL",
    "blocked": "BLOCKED",
    "check": "CHECK",
    "checking": "CHECK",
    "watch": "WATCH",
    "wip": "WIP",
}

# Substrings that signal raw machine output rather than a written message.
_RAW_DUMP_MARKERS = (
    "Traceback (most recent call last)",
    "RESULT_STATUS:",
    "DONE_WHEN_VERIFICATION",
    'File "',  # python frame: File "x.py", line N
    "\x1b[",  # ANSI escape / color codes
    "Error: Exceeded",
    "\u26a0\ufe0f  Primary auth failed",  # gateway fallback banner
    "[System note:",  # runtime/system context must stay internal
    "Codex gpt-5.5 caps context",
    "auto-compaction was raised",
    "compression.codex_gpt55_autoraise",
    "hermes config set",
    "Worker telemetry:",
    "tokens_in=",
    "tokens_out=",
    "provider=",
    "fallback_provider=",
    "request_id=",
    "tool_call:",
    "stdout_tail:",
)

_RAW_ARTIFACT_SUFFIXES = {
    ".db",
    ".json",
    ".jsonl",
    ".lock",
    ".log",
    ".ndjson",
    ".plist",
    ".sqlite",
    ".sqlite3",
    ".toml",
    ".yaml",
    ".yml",
}

_HUMAN_DELIVERABLE_SUFFIXES = {
    ".jpeg",
    ".jpg",
    ".mp4",
    ".ogg",
    ".pdf",
    ".png",
    ".webp",
}

_RAW_ARTIFACT_NAME_MARKERS = (
    "config",
    "debug",
    "ledger",
    "raw",
    "runbook-registry",
    "state",
    "trace",
)



class UnreadableMessageError(RuntimeError):
    """Raised when a message destined for a human-readable lane fails the
    readability guard (raw dump signature or over-length). The full text is
    persisted to OVERFLOW_DIR and a blocked_unreadable proof is recorded."""


def _raw_ok_lanes() -> set[str]:
    raw = os.environ.get("TELEGRAM_RAW_OK_LANES", "") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def _lane_key(chat_id: object, thread_id: object) -> str:
    cid = str(chat_id or "").strip()
    tid = str(thread_id).strip() if thread_id not in {None, "", "None"} else ""
    return f"{cid}:{tid}" if tid else cid


def _looks_like_raw_dump(text: str, allow_long: bool = False) -> str | None:
    """Return a short reason string if the text reads like raw machine output,
    else None. Conservative: only trips on strong signals so normal prose and
    short status lines pass cleanly.

    Raw-dump markers (tracebacks, RESULT_STATUS, JSON blobs, ANSI) always trip —
    those are never legitimate in a human-readable lane. Length/line limits are
    a softer signal that intentional long reports skip via allow_long=True."""
    if any(marker in text for marker in _RAW_DUMP_MARKERS):
        hit = next(m for m in _RAW_DUMP_MARKERS if m in text)
        label = "ANSI-codes" if hit == "\x1b[" else hit.strip().strip('"')[:40]
        return f"raw-dump marker: {label}"
    # JSON blob heuristic: starts like a serialized object/array and is sizeable.
    stripped = text.lstrip()
    if stripped[:1] in "{[" and len(text) > 300 and text.count('"') >= 8:
        return "looks like a JSON/dict blob"
    if allow_long:
        return None
    if len(text) > MAX_READABLE_CHARS:
        return f"over length: {len(text)} > {MAX_READABLE_CHARS} chars"
    line_count = text.count("\n") + 1
    if line_count > MAX_READABLE_LINES:
        return f"too many lines: {line_count} > {MAX_READABLE_LINES}"
    return None


def _persist_overflow(text: str, lane: str, reason: str) -> Path:
    OVERFLOW_DIR.mkdir(parents=True, exist_ok=True)
    safe_lane = lane.replace("/", "_").replace(":", "_") or "unknown"
    path = OVERFLOW_DIR / f"{iso_now().replace(':', '').replace('-', '')}-{safe_lane}-{uuid.uuid4().hex[:8]}.txt"
    path.write_text(
        f"# blocked by readability guard\n# lane: {lane}\n# reason: {reason}\n"
        f"# ts: {iso_now()}\n\n{text}",
        encoding="utf-8",
    )
    return path


# Delivery-only callback namespaces. The receiving Telegram gateway must own
# handlers for callback_data actions before callers expose these presets.
_BUTTON_PRESETS = {
    "approval": [
        [{"text": "Approve", "callback_data": "nrb:approve"}, {"text": "Hold", "callback_data": "nrb:hold"}],
        [{"text": "Details", "callback_data": "nrb:details"}],
    ],
    "blocked": [
        [
            {"text": "Details", "callback_data": "nrb:details"},
            {"text": "Dismiss", "callback_data": "nrb:dismiss"},
        ]
    ],
    "progress": [
        [
            {"text": "Details", "callback_data": "nrb:details"},
            {"text": "Dismiss", "callback_data": "nrb:dismiss"},
        ]
    ],
}


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _coerce_button_rows(buttons: object) -> list[list[dict[str, str]]]:
    if not buttons:
        return []
    if isinstance(buttons, str):
        return [[dict(button) for button in row] for row in _BUTTON_PRESETS.get(buttons, [])]
    if isinstance(buttons, dict):
        if "preset" in buttons:
            rows = _coerce_button_rows(str(buttons.get("preset") or ""))
            prefix = str(buttons.get("callback_prefix") or "nrb").strip() or "nrb"
            if prefix != "nrb":
                for row in rows:
                    for button in row:
                        data = str(button.get("callback_data") or "")
                        if data.startswith("nrb:"):
                            suffix = data[3:]
                            prefix_limit = max(0, 64 - len(suffix.encode("utf-8")))
                            button["callback_data"] = _truncate_utf8(prefix, prefix_limit) + suffix
            return rows
        buttons = [[buttons]]
    if isinstance(buttons, list):
        rows = buttons
    else:
        return []
    normalized: list[list[dict[str, str]]] = []
    for row in rows:
        items = row if isinstance(row, list) else [row]
        out_row: list[dict[str, str]] = []
        for item in items[:4]:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or item.get("label") or "").split())[:32]
            if not text:
                continue
            button: dict[str, str] = {"text": text}
            url = str(item.get("url") or "").strip()
            callback = str(item.get("callback_data") or item.get("callback") or "").strip()
            if url.startswith(("https://", "http://")):
                button["url"] = url
            elif callback:
                button["callback_data"] = _truncate_utf8(callback, 64)
            else:
                continue
            out_row.append(button)
        if out_row:
            normalized.append(out_row)
    return normalized[:4]


def native_rich_reply_markup(buttons: object) -> dict[str, object] | None:
    rows = _coerce_button_rows(buttons)
    return {"inline_keyboard": rows} if rows else None


def _normalize_status_prefix(line: str) -> str:
    stripped = line.strip()
    if not stripped or ":" not in stripped:
        return line.rstrip()
    prefix, rest = stripped.split(":", 1)
    normalized = _STATUS_PREFIXES.get(prefix.strip().lower())
    if not normalized:
        return line.rstrip()
    return f"{normalized}: {rest.strip()}" if rest.strip() else f"{normalized}:"


def _label_for_line(line: str) -> tuple[str | None, str]:
    stripped = line.strip()
    if not stripped or ":" not in stripped:
        return None, stripped
    prefix, rest = stripped.split(":", 1)
    key = prefix.strip().lower()
    normalized = _STATUS_PREFIXES.get(key)
    if normalized:
        return normalized, rest.strip()
    if key in {
        "result",
        "done",
        "fixed",
        "sent",
        "delivered",
        "completed",
        "why",
        "blocked",
        "failed",
        "fail",
        "error",
        "warning",
        "warn",
        "action",
        "next",
        "todo",
        "now",
        "right now",
        "so far",
        "progress",
    }:
        return key, rest.strip()
    return None, stripped


def _message_kind(label: str | None, text: str) -> str | None:
    lowered = text.lower()
    label_key = (label or "").upper()
    if label_key in {"DONE", "FIXED"} or lowered.startswith(
        ("done:", "fixed:", "sent:", "delivered:", "completed:")
    ):
        return "DONE"
    if label_key in {"FAIL", "BLOCKED"} or lowered.startswith(("blocked:", "failed:", "fail:", "error:")):
        return "BLOCKED"
    if label_key in {"WARN", "WATCH"} or lowered.startswith(("warn:", "warning:", "watch:")):
        return label_key or "WARN"
    if "minutes in" in lowered or lowered.startswith(("progress:", "still running:")):
        return "WATCH"
    return None


def _section_for_label(raw_label: str | None, kind: str) -> str:
    label = (raw_label or "").strip().lower()
    if label in {"result", "done", "fixed", "sent", "delivered", "completed"}:
        return "What changed"
    if label in {"why", "blocked", "failed", "fail", "error", "warning", "warn"}:
        return "Why"
    if label in {"action", "next", "todo"}:
        return "Next"
    if label in {"now", "right now"}:
        return "Now"
    if label in {"so far", "progress"}:
        return "So far"
    if kind == "WATCH":
        return "So far"
    return "Details"


def _render_premium_card(
    kind: str,
    title: str,
    sections: list[tuple[str, list[str]]],
    *,
    markdown: bool = False,
) -> str:
    clean_title = " ".join((title or "update").split())
    if len(clean_title) > 96:
        clean_title = clean_title[:93].rstrip() + "..."
    if markdown:
        lines = [f"**{kind}**  {clean_title}"]
    else:
        lines = [f"{kind} · {clean_title}"]
    for heading, items in sections:
        clean_items = [" ".join(str(item or "").split()) for item in items]
        clean_items = [item for item in clean_items if item]
        if not clean_items:
            continue
        lines.append("")
        lines.append(f"**{heading}**" if markdown else heading)
        lines.extend(f"• {item}" for item in clean_items[:4])
    return "\n".join(lines).strip()


def _premium_card_text(text: str, *, markdown: bool = False) -> tuple[str, str | None]:
    if "```" in text or "`" in text:
        return text, None
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return text, None
    first_label, first_body = _label_for_line(lines[0])
    kind = _message_kind(first_label, lines[0])
    if not kind:
        return text, None
    title = first_body or lines[0]
    sections_by_name: list[tuple[str, list[str]]] = []

    def add_item(section: str, item: str) -> None:
        item = item.strip()
        if section == "Next" and item.lower() in {"none", "no action", "n/a", "na"}:
            item = "No action needed"
        if not item:
            return
        if sections_by_name and sections_by_name[-1][0] == section:
            sections_by_name[-1][1].append(item)
        else:
            sections_by_name.append((section, [item]))

    for line in lines[1:]:
        raw_prefix = line.split(":", 1)[0] if ":" in line else None
        _label, body = _label_for_line(line)
        section = _section_for_label(raw_prefix, kind)
        add_item(section, body)

    if kind == "DONE" and not any(section == "Next" for section, _ in sections_by_name):
        add_item("Next", "No action needed")
    elif kind == "BLOCKED" and not any(section == "Next" for section, _ in sections_by_name):
        add_item("Next", "Review when you have a minute")
    elif kind == "WATCH" and not any(section == "Next" for section, _ in sections_by_name):
        add_item("Next", "I’ll report finish or blocker")
    return _render_premium_card(kind, title, sections_by_name, markdown=markdown), "native-rich premium card"


def shape_readable_text(text: str, *, lane: str = "", allow_long: bool = False) -> tuple[str, str | None]:
    """Return a Hermes-native readable message and an optional shaping detail."""
    original = text or ""
    if not NATIVE_RICH_ENABLED:
        return original, None
    if any(marker in original for marker in _RAW_DUMP_MARKERS):
        return original, None
    normalized = original.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return original, None
    lines = [line.rstrip() for line in normalized.split("\n")]
    compacted: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if not blank:
                compacted.append("")
            blank = True
            continue
        compacted.append(_normalize_status_prefix(line))
        blank = False
    shaped = "\n".join(compacted).strip()
    premium, premium_detail = _premium_card_text(shaped, markdown=False)
    if premium_detail:
        shaped = premium
    reason = _looks_like_raw_dump(shaped, allow_long=allow_long)
    if reason and not allow_long and not reason.startswith("raw-dump marker") and "JSON" not in reason:
        overflow_path = _persist_overflow(original, lane or "unknown", f"native-rich compacted: {reason}")
        shaped = (
            "SUMMARY: The full update was too long for a readable chat message.\n\n"
            f"DETAIL: Saved internally at `{overflow_path}`.\n\n"
            "NEXT: Ask for the full artifact if you need exact output."
        )
        return shaped, f"native-rich compacted overlong message: {reason}; saved {overflow_path}"
    if premium_detail:
        return shaped, premium_detail
    if shaped != original:
        return shaped, "native-rich normalized spacing/status labels"
    return shaped, None


def enforce_readability(
    *,
    text: str,
    chat_id: object,
    thread_id: object = None,
    sender: str | None = None,
    allow_raw: bool = False,
    allow_long: bool = False,
) -> None:
    """Block raw/over-long messages headed to a human-readable lane.

    No-op when allow_raw=True or the lane is in TELEGRAM_RAW_OK_LANES. Raw-dump
    signatures (tracebacks, RESULT_STATUS, JSON blobs, ANSI) always block; pass
    allow_long=True for intentional long-but-clean reports (e.g. chunked digests)
    to skip only the length/line caps. On a violation, persists the full text to
    OVERFLOW_DIR, records a blocked_unreadable proof, and raises
    UnreadableMessageError."""
    if allow_raw:
        return
    lane = _lane_key(chat_id, thread_id)
    if lane in _raw_ok_lanes() or str(chat_id or "") in _raw_ok_lanes():
        return
    reason = _looks_like_raw_dump(text or "", allow_long=allow_long)
    if not reason:
        return
    overflow_path = _persist_overflow(text or "", lane, reason)
    record_delivery_proof(
        sender=sender or "unknown",
        status="blocked_unreadable",
        chat_id=chat_id,
        thread_id=thread_id,
        summary=f"blocked unreadable message: {reason}",
        detail=f"full text saved: {overflow_path}",
    )
    raise UnreadableMessageError(
        f"message to lane {lane} blocked by readability guard ({reason}). "
        f"Full text saved to {overflow_path}. Compose a short, human-written "
        f"summary for human-readable lanes, or pass allow_raw=True for a "
        f"machine-only surface."
    )


def _looks_like_raw_artifact(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _RAW_ARTIFACT_SUFFIXES:
        return f"raw machine artifact suffix: {suffix}"
    name = path.name.lower()
    if suffix not in _HUMAN_DELIVERABLE_SUFFIXES:
        for marker in _RAW_ARTIFACT_NAME_MARKERS:
            if marker in name:
                return f"internal artifact name marker: {marker}"
    return None


def enforce_document_readability(
    *,
    file_path: str | Path,
    chat_id: object,
    thread_id: object = None,
    caption: str = "",
    sender: str | None = None,
    allow_raw: bool = False,
    allow_long_caption: bool = False,
) -> None:
    """Block raw machine artifacts headed to human-readable Telegram lanes.

    Artifact QA checks whether a file is valid. This guard checks whether the
    file is fit for a human-readable lane. Internal JSON/log/state/config files
    stay internal unless the caller explicitly targets a machine-only lane or
    passes allow_raw=True. Captions go through the same readability gate as text.
    """
    path = Path(file_path)
    enforce_readability(
        text=caption or "",
        chat_id=chat_id,
        thread_id=thread_id,
        sender=sender,
        allow_raw=allow_raw,
        allow_long=allow_long_caption,
    )
    if allow_raw:
        return
    lane = _lane_key(chat_id, thread_id)
    if lane in _raw_ok_lanes() or str(chat_id or "") in _raw_ok_lanes():
        return
    reason = _looks_like_raw_artifact(path)
    if not reason:
        return
    record_delivery_proof(
        sender=sender or "unknown",
        status="blocked_unreadable_artifact",
        chat_id=chat_id,
        thread_id=thread_id,
        summary=f"blocked unreadable artifact: {reason}",
        detail=str(path),
    )
    raise UnreadableMessageError(
        f"document to lane {lane} blocked by readability guard ({reason}). "
        "Send a polished report/deliverable or a short actionable summary; keep "
        "raw machine artifacts on machine-only surfaces."
    )


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def record_delivery_proof(
    *,
    sender: str,
    status: str,
    chat_id: str | int,
    thread_id: str | int | None = None,
    message_id: str | int | None = None,
    summary: str = "",
    detail: str = "",
) -> None:
    PROOF_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": iso_now(),
        "sender": sender,
        "status": status,
        "chat_id": str(chat_id),
        "thread_id": None if thread_id in {None, "", "None"} else str(thread_id),
        "message_id": int(message_id) if message_id not in {None, "", "None"} else None,
        "summary": summary,
        "detail": detail,
    }
    with PROOF_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def _repeat_suppress_enabled() -> bool:
    return os.environ.get("HERMES_REPEAT_SUPPRESS", "").strip().lower() not in {"off", "0", "false", "no"}


def _repeat_suppression_key(*, chat_id: object, thread_id: object, sender: str | None, text: str) -> tuple[str, str]:
    digit_stripped = re.sub(r"\d+", "", text or "")
    digest = hashlib.sha256(digit_stripped.encode("utf-8")).hexdigest()
    tid = "" if thread_id in {None, "", "None"} else str(thread_id)
    return "|".join([str(chat_id or ""), tid, str(sender or ""), digest]), digest


def _load_repeat_ledger() -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(REPEAT_SUPPRESS_LEDGER.read_text(encoding="utf-8"))
        ledger = {str(k): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    except Exception:
        ledger = {}
    cutoff = time.time() - REPEAT_SUPPRESS_TTL_SEC
    return {k: v for k, v in ledger.items() if float(v.get("last_seen", 0) or 0) >= cutoff}


def _save_repeat_ledger(ledger: dict[str, dict[str, object]]) -> None:
    try:
        REPEAT_SUPPRESS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = REPEAT_SUPPRESS_LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(REPEAT_SUPPRESS_LEDGER)
    except Exception:
        pass


def _should_suppress_repeat(
    *,
    chat_id: object,
    thread_id: object,
    sender: str | None,
    text: str,
    summary: str = "",
) -> bool:
    if not _repeat_suppress_enabled() or not text:
        return False
    key, digest = _repeat_suppression_key(chat_id=chat_id, thread_id=thread_id, sender=sender, text=text)
    hit = key in _load_repeat_ledger()
    if hit:
        record_delivery_proof(
            sender=sender or "unknown",
            status="suppressed_repeat",
            chat_id=chat_id,
            thread_id=thread_id,
            summary=summary or "repeat telegram send suppressed",
            detail=f"sha256_digit_stripped={digest}",
        )
    return hit


def _remember_successful_repeat_candidate(
    *,
    chat_id: object,
    thread_id: object,
    sender: str | None,
    text: str,
    summary: str = "",
) -> None:
    if not _repeat_suppress_enabled() or not text:
        return
    now = time.time()
    key, digest = _repeat_suppression_key(chat_id=chat_id, thread_id=thread_id, sender=sender, text=text)
    ledger = _load_repeat_ledger()
    ledger[key] = {
        "last_seen": now,
        "last_seen_iso": iso_now(),
        "chat_id": str(chat_id or ""),
        "thread_id": None if thread_id in {None, "", "None"} else str(thread_id),
        "sender": str(sender or ""),
        "text_sha256_digit_stripped": digest,
        "summary": summary,
    }
    _save_repeat_ledger(ledger)


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_gateway_process_env() -> dict[str, str]:
    state_path = HERMES / "gateway_state.json"
    if not state_path.exists():
        return {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    pid = state.get("pid")
    if not pid:
        return {}
    try:
        proc = subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    env: dict[str, str] = {}
    for part in lines[-1].split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.isupper():
            env[key] = value
    return env


def resolve_telegram_bot_token(explicit_token: str | None = None) -> str:
    if explicit_token:
        value = str(explicit_token).strip()
        if value:
            return value
    dotenv_vars = load_dotenv(HERMES / ".env")
    for source in (os.environ, dotenv_vars, load_gateway_process_env()):
        token = str(source.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if token:
            return token
    return ""


def _send_payload(
    *,
    token: str,
    payload: dict[str, object],
    sender: str | None = None,
    chat_id: str | int,
    thread_id: str | int | None = None,
    summary: str = "",
    detail: str = "",
    timeout: int = 30,
    allow_raw: bool = False,
    allow_long: bool = False,
    buttons: object = None,
) -> dict:
    if NATIVE_RICH_ENABLED:
        payload = dict(payload)
        buttons = buttons or payload.pop("native_rich_buttons", None)
        if buttons and "reply_markup" not in payload:
            reply_markup = native_rich_reply_markup(buttons)
            if reply_markup:
                payload["reply_markup"] = reply_markup
        if not allow_raw and "text" in payload and "parse_mode" not in payload and "entities" not in payload:
            lane = _lane_key(chat_id, thread_id)
            shaped_text, shaping_detail = shape_readable_text(
                str(payload.get("text") or ""),
                lane=lane,
                allow_long=allow_long,
            )
            payload["text"] = shaped_text
            if shaping_detail and sender:
                record_delivery_proof(
                    sender=sender,
                    status="shaped_readable",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    summary="native-rich message shaping applied",
                    detail=shaping_detail,
                )
    enforce_readability(
        text=str(payload.get("text") or ""),
        chat_id=chat_id,
        thread_id=thread_id,
        sender=sender,
        allow_raw=allow_raw,
        allow_long=allow_long,
    )
    if "text" in payload and _should_suppress_repeat(
        chat_id=chat_id,
        thread_id=thread_id,
        sender=sender,
        text=str(payload.get("text") or ""),
        summary=summary,
    ):
        return {"message_id": None, "suppressed": True, "status": "suppressed_repeat"}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        if sender:
            record_delivery_proof(
                sender=sender,
                status="failed",
                chat_id=chat_id,
                thread_id=thread_id,
                summary=summary or "telegram send failed",
                detail=str(exc) if not detail else f"{detail} | {exc}",
            )
        raise
    if not body.get("ok"):
        err = RuntimeError(f"telegram send failed: {body}")
        if sender:
            record_delivery_proof(
                sender=sender,
                status="failed",
                chat_id=chat_id,
                thread_id=thread_id,
                summary=summary or "telegram send failed",
                detail=str(body) if not detail else f"{detail} | {body}",
            )
        raise err
    result = body.get("result") or {}
    if "text" in payload:
        _remember_successful_repeat_candidate(
            chat_id=chat_id,
            thread_id=thread_id,
            sender=sender,
            text=str(payload.get("text") or ""),
            summary=summary,
        )
    if sender:
        record_delivery_proof(
            sender=sender,
            status="delivered",
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=result.get("message_id"),
            summary=summary or "telegram message delivered",
            detail=detail,
        )
    return result


def _send_multipart(
    *,
    token: str,
    method: str,
    fields: dict[str, object],
    files: dict[str, Path],
    timeout: int = 60,
) -> dict:
    boundary = f"----------------{uuid.uuid4().hex}"
    body = bytearray()

    for key, value in fields.items():
        if value is None or value == "":
            continue
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b"\r\n")

    for field, path in files.items():
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'.encode())
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
        body.extend(path.read_bytes())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_text_message(
    *,
    token: str,
    chat_id: str | int,
    text: str,
    thread_id: str | int | None = None,
    reply_to_message_id: str | int | None = None,
    sender: str | None = None,
    summary: str = "",
    detail: str = "",
    timeout: int = 30,
    allow_raw: bool = False,
    allow_long: bool = False,
    buttons: object = None,
) -> dict:
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
    }
    if thread_id not in {None, "", "None"}:
        payload["message_thread_id"] = int(thread_id)
    if reply_to_message_id not in {None, "", "None"}:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    if NATIVE_RICH_ENABLED and buttons:
        payload["native_rich_buttons"] = buttons
    return _send_payload(
        token=token,
        payload=payload,
        sender=sender,
        chat_id=chat_id,
        thread_id=thread_id,
        summary=summary,
        detail=detail,
        timeout=timeout,
        allow_raw=allow_raw,
        allow_long=allow_long,
    )


def send_message_payload(
    *,
    token: str,
    payload: dict[str, object],
    sender: str | None = None,
    summary: str = "",
    detail: str = "",
    timeout: int = 30,
    allow_raw: bool = False,
    allow_long: bool = False,
) -> dict:
    chat_id = payload.get("chat_id", "")
    thread_id = payload.get("message_thread_id")
    return _send_payload(
        token=token,
        payload=payload,
        sender=sender,
        chat_id=str(chat_id),
        thread_id=thread_id,
        summary=summary,
        detail=detail,
        timeout=timeout,
        allow_raw=allow_raw,
        allow_long=allow_long,
    )


def send_document_message(
    *,
    token: str,
    chat_id: str | int,
    file_path: str | Path,
    caption: str = "",
    thread_id: str | int | None = None,
    sender: str | None = None,
    summary: str = "",
    detail: str = "",
    timeout: int = 60,
    allow_raw: bool = False,
    allow_long_caption: bool = False,
) -> dict:
    path = Path(file_path)
    enforce_document_readability(
        file_path=path,
        chat_id=chat_id,
        thread_id=thread_id,
        caption=caption,
        sender=sender,
        allow_raw=allow_raw,
        allow_long_caption=allow_long_caption,
    )
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "caption": caption,
    }
    if thread_id not in {None, "", "None"}:
        fields["message_thread_id"] = int(thread_id)
    try:
        body = _send_multipart(
            token=token,
            method="sendDocument",
            fields=fields,
            files={"document": path},
            timeout=timeout,
        )
    except Exception as exc:
        record_delivery_proof(
            sender=sender or "unknown",
            status="failed",
            chat_id=chat_id,
            thread_id=thread_id,
            summary=summary or "telegram document send failed",
            detail=str(exc) if not detail else f"{detail} | {exc}",
        )
        raise
    if not body.get("ok"):
        err = RuntimeError(f"telegram document send failed: {body}")
        record_delivery_proof(
            sender=sender or "unknown",
            status="failed",
            chat_id=chat_id,
            thread_id=thread_id,
            summary=summary or "telegram document send failed",
            detail=str(body) if not detail else f"{detail} | {body}",
        )
        raise err
    result = body.get("result") or {}
    record_delivery_proof(
        sender=sender or "unknown",
        status="delivered",
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=result.get("message_id"),
        summary=summary or "telegram document delivered",
        detail=detail or str(path),
    )
    return result
