#!/usr/bin/env python3
"""Adversarial lint for Telegram email cards and send bodies.

Fails closed on Telegram/HTML spacing bugs that break email cards:
  - two trailing spaces (Markdown hard-break → extra blank line)
  - <email@domain> (Telegram HTML strips the tag and leaves a hole)
  - blank line between consecutive labeled fields
  - markdown tables, ### headers, 2+ consecutive blank lines

Usage:
  python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --file PATH
  python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --stdin
  python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --text '...'
  python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --mime --file PATH

Exit 0 = pass. Exit 1 = fail. Prints one violation per line.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable

LABEL_RE = re.compile(
    r"^(?:\*\*)?(To|Cc|Bcc|From|Subject|Ask|Details|Reply-To)(?:\*\*)?:\s*(.*)$",
    re.IGNORECASE,
)
ANGLE_EMAIL_RE = re.compile(r"<[^>\s]*@[^>\s]+>")
EMAIL_TOKEN_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
H3_RE = re.compile(r"^#{3,}\s+")
LABEL_NAMES = {"to", "cc", "bcc", "from", "subject", "ask", "details", "reply-to"}


def _strip_code_fences(text: str) -> list[tuple[int, str, bool]]:
    """Return (lineno, line, in_fence) for each original line. 1-indexed."""
    lines = text.splitlines()
    in_fence = False
    out: list[tuple[int, str, bool]] = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            out.append((i, line, True))
            in_fence = not in_fence
            continue
        out.append((i, line, in_fence))
    return out


def looks_like_card(text: str) -> bool:
    labels = set()
    for _, line, in_fence in _strip_code_fences(text):
        if in_fence:
            continue
        m = LABEL_RE.match(line.rstrip())
        if m:
            labels.add(m.group(1).lower())
    return len(labels & {"to", "from", "subject", "cc"}) >= 2


_looks_like_card = looks_like_card


def simulate_html_strip(line: str) -> str:
    """Telegram HTML parse_mode drops unknown tags such as <addr@host>."""
    return re.sub(r"<[^>]+>", "", line)


def lint(text: str, *, mime: bool = False) -> list[str]:
    violations: list[str] = []
    rows = _strip_code_fences(text)
    blank_run = 0
    prev_was_label = False
    table_prev = False

    for lineno, line, in_fence in rows:
        if in_fence:
            blank_run = 0
            prev_was_label = False
            table_prev = False
            continue

        raw = line
        if raw.endswith("\r"):
            raw = raw[:-1]

        if raw.strip() == "":
            blank_run += 1
            if blank_run >= 2:
                violations.append(f"L{lineno}: two or more consecutive blank lines")
            if prev_was_label:
                # defer: extra gap is confirmed if the next non-empty line is also a label
                pass
            table_prev = False
            continue

        if blank_run >= 1 and prev_was_label:
            m_next = LABEL_RE.match(raw.rstrip())
            if m_next:
                violations.append(
                    f"L{lineno}: blank line between labeled fields ({m_next.group(1)})"
                )
        blank_run = 0

        if len(raw) >= 2 and raw.endswith("  "):
            violations.append(
                f"L{lineno}: trailing two spaces (Telegram hard-break / extra gap)"
            )

        if ANGLE_EMAIL_RE.search(raw):
            violations.append(f"L{lineno}: angle-bracket email (Telegram can strip it)")

        m = LABEL_RE.match(raw.rstrip())
        is_label = bool(m)
        if is_label:
            label = m.group(1)
            rest = m.group(2) or ""
            rest_stripped = simulate_html_strip(rest)
            if label.lower() in {"to", "cc", "bcc", "from", "reply-to"}:
                if ANGLE_EMAIL_RE.search(rest) and not EMAIL_TOKEN_RE.search(rest_stripped):
                    violations.append(
                        f"L{lineno}: {label} address vanished after Telegram HTML strip"
                    )
        prev_was_label = is_label

        if H3_RE.match(raw.lstrip()):
            violations.append(f"L{lineno}: ### header (Telegram will not render this)")

        is_table = bool(TABLE_SEP_RE.match(raw) or TABLE_ROW_RE.match(raw))
        if is_table and table_prev:
            violations.append(f"L{lineno}: markdown table (Telegram has no table syntax)")
        table_prev = is_table

        if mime and "=\n" in text and re.search(r"=\n[a-z]", text):
            pass  # checked once below

    if mime:
        if re.search(r"=\r?\n[a-z]", text):
            violations.append("quoted-printable mid-word wrap is visible in source")
        # Gmail API + extra blank lines render as huge gaps
        if re.search(r"(?:\r?\n[ \t]*){3,}", text):
            violations.append("MIME body has 2+ consecutive blank lines")

    # de-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _is_blank_line(line: str) -> bool:
    core = line[:-1] if line.endswith("\n") else line
    if core.endswith("\r"):
        core = core[:-1]
    return core.strip() == ""


def fix(text: str) -> str:
    """Deterministic repair: strip hard-breaks, unwrap <email>, drop blank-between-labels."""
    if not text:
        return text
    ended_with_nl = text.endswith("\n")
    lines = text.splitlines()
    out: list[str] = []
    in_fence = False
    pending_blank = False
    last_was_label = False
    last_out_blank = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            pending_blank = False
            last_was_label = False
            out.append(line)
            last_out_blank = False
            continue
        if in_fence:
            out.append(line)
            last_out_blank = False
            continue
        core = line.rstrip(" ")
        core = ANGLE_EMAIL_RE.sub(lambda m: m.group(0)[1:-1], core)
        is_blank = core.strip() == ""
        is_label = bool(LABEL_RE.match(core))
        if is_blank:
            if last_was_label:
                pending_blank = True
                continue
            if last_out_blank:
                continue
            pending_blank = False
            last_was_label = False
            out.append("")
            last_out_blank = True
            continue
        if pending_blank:
            if not is_label and not last_out_blank:
                out.append("")
                last_out_blank = True
            pending_blank = False
        last_was_label = is_label
        out.append(core)
        last_out_blank = False
    result = "\n".join(out)
    if ended_with_nl and (not result.endswith("\n")):
        result += "\n"
    return result


def gate_response(text: str) -> str | None:
    """Return replacement text, or None to leave the reply unchanged."""
    if not text or not looks_like_card(text):
        return None
    if not lint(text):
        return None
    fixed = fix(text)
    if fixed != text and not lint(fixed):
        return fixed
    remaining = lint(fixed)
    reasons = "\n".join(f"- {v}" for v in remaining[:8])
    return (
        "Email card failed spacing QA. Not sent.\n\n"
        f"{reasons}\n\n"
        "Fix: single newlines on To/Cc/From/Subject, no trailing spaces, "
        "no <email@domain>."
    )


SEND_TOOL_MARKERS = (
    "SEND_EMAIL",
    "SEND_DRAFT",
    "CREATE_EMAIL_DRAFT",
    "GMAIL_SEND",
    "send_email",
    "send_draft",
    "create_email_draft",
    "create_draft",
)
TERMINAL_SEND_RE = re.compile(
    r"(?:gog|gogcli)\b.*\b(?:send|drafts?)\b|\bgmail[_-]?(?:send|drafts?)\b|GOOGLESUPER_(?:SEND|CREATE)",
    re.IGNORECASE,
)
BODY_FLAG_RE = re.compile(
    r"(?:--(?:body|message|text|html)(?:-file)?|--file)\s+(\S+)",
    re.IGNORECASE,
)
BODY_KEYS = (
    "body",
    "message_body",
    "message",
    "html_body",
    "text",
    "body_text",
    "messageText",
    "email_body",
    "message_text",
    "content",
)


def is_email_send_tool(tool_name: str) -> bool:
    name = tool_name or ""
    return any(m in name for m in SEND_TOOL_MARKERS)


def extract_email_body(args: dict | None) -> str:
    if not isinstance(args, dict):
        return ""
    inner = args.get("arguments")
    if isinstance(inner, dict):
        args = {**args, **inner}
    for key in BODY_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _command_from_args(args: dict) -> str:
    for key in ("command", "cmd", "code"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _terminal_send_body(command: str) -> tuple[str | None, bool]:
    """Return (body, inspectable). inspectable False means send-like with no body."""
    if not TERMINAL_SEND_RE.search(command or ""):
        return None, True
    quoted = re.search(
        r"--(?:body|message|text|html)\s+(['\"])(.*?)\1",
        command,
        re.IGNORECASE | re.DOTALL,
    )
    if quoted:
        body = quoted.group(2)
        if body.strip() in {"-", "/dev/stdin"} or body.lstrip().startswith("$"):
            return None, False
        return body, True
    flagged = BODY_FLAG_RE.search(command)
    if flagged:
        token = flagged.group(1).strip("'\"")
        if token in {"-", "/dev/stdin"} or token.startswith("$"):
            return None, False
        path = Path(token).expanduser()
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8"), True
            except OSError:
                return None, False
        return token, True
    return None, False


def _block_send(shown: str, reasons: str) -> dict:
    return {
        "action": "block",
        "message": (
            f"Blocked {shown}: email body failed spacing QA ({reasons}). "
            "Strip trailing spaces, unwrap <email>, and collapse extra blank lines, then retry."
        ),
    }


def gate_send_tool(tool_name: str, args: dict | None) -> dict | None:
    """pre_tool_call directive, or None to allow."""
    name = tool_name or ""
    call_args = args if isinstance(args, dict) else {}
    if name == "tool_call":
        name = str(call_args.get("name") or "")
        inner_args = call_args.get("arguments")
        call_args = inner_args if isinstance(inner_args, dict) else call_args

    if name in {"terminal", "execute_code"}:
        command = _command_from_args(call_args)
        body, inspectable = _terminal_send_body(command)
        if not inspectable:
            return _block_send(name, "email send via terminal with no lintable body")
        if body is None:
            return None
        shown = name
    elif is_email_send_tool(name):
        body = extract_email_body(call_args)
        shown = name
        if not body:
            return _block_send(name, "email send/draft with no lintable body")
    else:
        return None

    violations = lint(body, mime=True)
    if not violations:
        return None
    return _block_send(shown, "; ".join(violations[:6]))


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--stdin", action="store_true")
    src.add_argument("--text")
    p.add_argument("--mime", action="store_true")
    p.add_argument("--fix", action="store_true", help="Print the repaired text instead of linting")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.file:
        text = open(args.file, encoding="utf-8").read()
    elif args.stdin:
        text = sys.stdin.read()
    else:
        text = args.text or ""

    if args.fix:
        sys.stdout.write(fix(text))
        return 0

    violations = lint(text, mime=args.mime)
    if violations:
        print("FAIL")
        for v in violations:
            print(v)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
