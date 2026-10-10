#!/usr/bin/env python3
"""doctrine-sync.py — Sync MEMORY.md / USER.md rules into Anamnesis.

Runs after dream consolidation. Reads §-delimited entries from the markdown
memory files, checks which ones aren't in Anamnesis yet, and ingests missing
ones as memory_type='decision', priority='critical'.

Also ingests bullet-point rules from family/BEHAVIORAL-RULES.md.

Idempotent: uses deterministic IDs based on content hash.

Secret-redaction layer (added 2026-05-13 per agent-standards §12e): high-
precision provider-key/token patterns are masked with [REDACTED:type] before
INSERT. deterministic_id() still hashes RAW content so idempotency is
preserved across runs even after the redaction layer changes what gets
stored. Existing leaked rows are NOT cleaned by this script — soft-delete
them separately.

Usage:
    python3 ~/.hermes/scripts/doctrine-sync.py
    python3 ~/.hermes/scripts/doctrine-sync.py --verbose

Designed to run as a post-dream-sweep step in the daily cron.
Anamnesis is optional: if its database is absent, or its existing ``memories``
table lacks the doctrine columns this writer requires, the sync succeeds as a
bounded no-op without creating or migrating a database.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
MEMORY_MD = HERMES_HOME / "memories" / "MEMORY.md"
USER_MD = HERMES_HOME / "memories" / "USER.md"
BEHAVIORAL_RULES = HERMES_HOME / "memories" / "family" / "BEHAVIORAL-RULES.md"
ANAMNESIS_DB = Path.home() / ".anamnesis" / "memory.db"
LOG = HERMES_HOME / "logs" / "doctrine-sync.log"

CONTAINER = "work"
CLIENT_ID = "doctrine-sync"

_REQUIRED_MEMORY_COLUMNS = {
    "id",
    "container_id",
    "content",
    "document_date",
    "confidence",
    "qdrant_id",
    "metadata",
    "client_id",
    "priority",
    "memory_type",
    "vector_status",
    "vector_status_changed_at",
    "vector_attempt_count",
}


def wal_reset_bug_fixed(version: tuple[int, ...] = sqlite3.sqlite_version_info) -> bool:
    current = tuple((list(version) + [0, 0, 0])[:3])
    return (
        current >= (3, 51, 3)
        or (3, 50, 7) <= current < (3, 51, 0)
        or (3, 44, 6) <= current < (3, 45, 0)
    )


def safe_journal_mode(version: tuple[int, ...] = sqlite3.sqlite_version_info) -> str:
    if _is_linux_platform():
        return "DELETE"
    return "WAL" if wal_reset_bug_fixed(version) else "DELETE"


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def require_safe_journal_mode(connection: sqlite3.Connection, *, allow_initialize: bool = False) -> None:
    effective = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).upper()
    expected = safe_journal_mode()
    if not allow_initialize and (effective == "DELETE" or effective == expected):
        return
    if effective != expected and allow_initialize:
        effective = str(connection.execute(f"PRAGMA journal_mode={expected}").fetchone()[0]).upper()
    if effective != expected:
        connection.close()
        raise sqlite3.DatabaseError(f"unsafe journal mode: expected {expected}, found {effective}")


# ── Secret redaction (§12e mask-not-drop, defense at write boundary) ─────
# High-precision patterns ONLY. False positives in this layer would corrupt
# legitimate doctrine; keep it conservative. Broader/ambiguous patterns
# belong in a separate audit/scrub script, not this nightly insert path.
_SECRET_PATTERNS = {
    "openrouter_key": re.compile(r"sk-or-v1-[a-f0-9]{16,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai_proj_key": re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    "openai_legacy_key": re.compile(r"sk-[A-Za-z0-9]{40,}"),
    "telegram_bot_token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{35}\b"),
    "github_personal": re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    "github_oauth": re.compile(r"\bgho_[A-Za-z0-9]{20,}"),
    "github_app": re.compile(r"\bghs_[A-Za-z0-9]{20,}"),
    "github_refresh": re.compile(r"\bghr_[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "slack_token": re.compile(r"\bxox[bpoaresu]-[A-Za-z0-9\-]{20,}"),
    "stripe_live_secret": re.compile(r"\bsk_live_[A-Za-z0-9]{20,}"),
    "stripe_test_secret": re.compile(r"\bsk_test_[A-Za-z0-9]{20,}"),
    "stripe_publishable": re.compile(r"\bpk_(?:live|test)_[A-Za-z0-9]{20,}"),
    "stripe_restricted": re.compile(r"\brk_(?:live|test)_[A-Za-z0-9]{20,}"),
    "google_api_key": re.compile(r"\bAIza[A-Za-z0-9_\-]{35}"),
    "discord_webhook": re.compile(r"https?://(?:discord(?:app)?\.com|discord\.com)/api/webhooks/\d+/[A-Za-z0-9_\-]+"),
    "notion_secret": re.compile(r"\bsecret_[A-Za-z0-9]{40,}"),
    # 2026-05-13: prose-credential patterns. Caught by the targeted audit
    # that the structured-token patterns above missed — natural-language
    # labels like "Keychain pw 1234", "Zoom Passcode: 482915", etc.
    # Mask-not-drop: matches replace ONLY the captured value, not the label.
    "keychain_pw": re.compile(r"\b[Kk]eychain\s+(?:pw|password|pass)\s+([^\s,.;]+)"),
    "bare_pw": re.compile(r"\bpw\s+([A-Za-z0-9_!@#$%^&*\-]{3,})\b"),
    "password_label": re.compile(r"(?i)\bpassword\s*(?:is\s+|=\s*|:\s*)([^\s,.;\"']+)"),
    "passcode_label": re.compile(r"(?i)\bpasscode\s*(?:is\s+|=\s*|:\s*)?([^\s,.;\"']{3,})"),
    "pin_label": re.compile(r"(?i)\bpin\s*(?:is\s+|=\s*|:\s*|#\s*)?(\d{3,8})\b"),
    "two_fa_code": re.compile(
        r"(?i)\b(?:2fa|two.?factor|otp|one.?time)\s*(?:code\s+|password\s+)?(?:is\s+|=\s*|:\s*)?(\d{4,8})\b"
    ),
    "recovery_code": re.compile(r"(?i)\brecovery\s+code\s*(?:is\s+|=\s*|:\s*)?([A-Za-z0-9\-]{6,})"),
    "bridge_code": re.compile(r"(?i)\bbridge\s+(\d{2,5}[^\w\s]\d{2,8})"),
    "account_number": re.compile(r"(?i)\baccount\s*(?:#|number|num|no\.?)\s*(?::|=|is)?\s*([\d\-]{6,})"),
    "routing_number": re.compile(r"(?i)\brouting\s*(?:#|number)?\s*(?::|=|is)?\s*(\d{9})\b"),
    "credit_card_label": re.compile(r"(?i)\b(?:credit|debit)\s+card\s*(?:#|number)?\s*(?::|=|is)?\s*([\d\s\-]{13,19})"),
    "ssn_explicit": re.compile(r"(?i)\bssn\s*(?::|=|is)?\s*(\d{3}[\s\-]?\d{2}[\s\-]?\d{4})"),
    "dob_explicit": re.compile(r"(?i)\b(?:dob|date of birth)\s*(?::|=|is)?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})"),
    "secret_assignment": re.compile(r"(?i)\b(?:secret|private[ _-]?key)\s+(?:is\s+|=\s*|:\s*)([^\s,.;\"']{8,})"),
}


# Keep the historical redaction without retaining a credential literal.
_KNOWN_SECRET_HASHES = {"29c0bdea386594ce9bf586b86870357f490b756afafc0a7f324eddbaee64bcf8": "groupme_known"}
_KNOWN_SECRET_WINDOWS = re.compile(r"(?=([A-Za-z0-9]{40}))")


def _redact_secrets(content: str) -> tuple[str, list[str]]:
    """Mask high-precision secret matches with [REDACTED:type].

    Returns (sanitized_content, list_of_pattern_names_matched).
    Empty list means content was clean. Never returns the matched bytes.

    Replacement strategy:
    - Pattern with no capture group: whole match → [REDACTED:type]
    - Pattern with group(1): only the captured value → [REDACTED:type]
      (preserves the surrounding label text — e.g., "Keychain pw [REDACTED:keychain_pw]"
       stays readable while masking the actual credential).
    """
    matched: list[str] = []
    sanitized = content
    for match in _KNOWN_SECRET_WINDOWS.finditer(content):
        value = match.group(1)
        name = _KNOWN_SECRET_HASHES.get(hashlib.sha256(value.encode()).hexdigest())
        if name:
            sanitized = sanitized.replace(value, f"[REDACTED:{name}]")
            if name not in matched:
                matched.append(name)
    for name, pat in _SECRET_PATTERNS.items():

        def replacer(m, n=name):
            if m.groups():
                return m.group(0).replace(m.group(1), f"[REDACTED:{n}]")
            return f"[REDACTED:{n}]"

        new_sanitized, count = pat.subn(replacer, sanitized)
        if count > 0:
            matched.append(name)
            sanitized = new_sanitized
    return sanitized, matched


verbose = "--verbose" in sys.argv or "-v" in sys.argv


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} {msg}"
    if verbose:
        print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_sections(filepath: Path) -> list[str]:
    if not filepath.exists():
        return []
    text = filepath.read_text()
    return [s.strip() for s in text.split("§") if s.strip()]


def parse_behavioral_rules(filepath: Path) -> list[str]:
    if not filepath.exists():
        return []
    rules = []
    for line in filepath.read_text().split("\n"):
        line = line.strip()
        if line.startswith("- ") and len(line) > 10:
            cleaned = line[2:].strip()
            if cleaned.startswith("**") and cleaned.endswith("**"):
                continue
            if len(cleaned) > 15:
                rules.append(cleaned)
    return rules


def deterministic_id(content: str) -> str:
    h = hashlib.sha256(f"doctrine:{content}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def main() -> int:
    if not ANAMNESIS_DB.exists():
        log("SKIP: Anamnesis DB not installed")
        return 0

    # Anamnesis is optional on lean runtimes, and older installations may use
    # a memories schema that predates client-scoped doctrine rows. Probe the
    # existing database read-only so this compatibility check cannot create or
    # migrate local state. Unsupported schemas are a successful bounded no-op.
    with sqlite3.connect(f"{ANAMNESIS_DB.resolve().as_uri()}?mode=ro", uri=True) as probe:
        memory_columns = {row[1] for row in probe.execute("PRAGMA table_info(memories)").fetchall()}
    missing_columns = _REQUIRED_MEMORY_COLUMNS - memory_columns
    if missing_columns:
        log("SKIP: Anamnesis memories schema lacks doctrine-sync columns: " + ", ".join(sorted(missing_columns)))
        return 0

    # Collect rules
    all_rules: list[tuple[str, str]] = []
    for fp in [MEMORY_MD, USER_MD]:
        for section in parse_sections(fp):
            all_rules.append((fp.name, section))
    for rule in parse_behavioral_rules(BEHAVIORAL_RULES):
        all_rules.append(("BEHAVIORAL-RULES.md", rule))

    if not all_rules:
        log("No rules found in markdown files")
        return 0

    # Check which already exist
    conn = sqlite3.connect(str(ANAMNESIS_DB))
    require_safe_journal_mode(conn)
    conn.execute("PRAGMA busy_timeout=5000")

    existing = set(
        row[0]
        for row in conn.execute(
            "SELECT id FROM memories WHERE client_id = ? OR client_id = ?",
            ("doctrine-ingest", "doctrine-seed"),
        ).fetchall()
    )
    # Also check doctrine-sync authored rows
    existing |= set(
        row[0]
        for row in conn.execute(
            "SELECT id FROM memories WHERE client_id = ?",
            (CLIENT_ID,),
        ).fetchall()
    )

    to_insert = []
    for source, content in all_rules:
        # ID hashes RAW content for idempotency across redaction-policy changes.
        mid = deterministic_id(content)
        if mid not in existing:
            to_insert.append((source, content, mid))

    if not to_insert:
        log(f"All {len(all_rules)} rules already in Anamnesis")
        conn.close()
        return 0

    log(f"Syncing {len(to_insert)} new rules into Anamnesis")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    inserted = 0
    skipped_dedupe = 0
    redacted_count = 0

    for source, content, memory_id in to_insert:
        # Mask any high-precision secret matches before INSERT.
        content_safe, redacted = _redact_secrets(content)
        if redacted:
            redacted_count += 1
            log(f"  REDACTED [{','.join(redacted)}] in rule from {source} (id={memory_id[:8]}..)")

        qdrant_id = str(uuid.uuid4())
        metadata_obj = {
            "source": "doctrine-sync",
            "source_file": source,
            "ingested_at": now,
        }
        if redacted:
            metadata_obj["redacted_patterns"] = redacted
        metadata = json.dumps(metadata_obj, separators=(",", ":"))

        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO memories
                    (id, container_id, content, document_date, confidence,
                     qdrant_id, metadata, client_id, priority, memory_type,
                     vector_status, vector_status_changed_at, vector_attempt_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', strftime('%s','now'), 0)
                """,
                (memory_id, CONTAINER, content_safe, now, 1.0, qdrant_id, metadata, CLIENT_ID, "critical", "decision"),
            )
            conn.execute("COMMIT")
            if cur.rowcount > 0:
                inserted += 1
                log(f"  + {content_safe[:80]}...")
            else:
                skipped_dedupe += 1
        except sqlite3.OperationalError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            log(f"  WARN: {exc}")

    conn.close()
    log(
        f"Done. Inserted {inserted} new, skipped {skipped_dedupe} duplicates, "
        f"redacted {redacted_count} (of {len(to_insert)} attempted)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
