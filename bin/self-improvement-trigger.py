#!/usr/bin/env python3
"""Activity-triggered self-improvement cycle.

Replaces the fixed nightly cron. Runs every 2 hours via cron, checks each
agent's message count since last reflection, and fires the cycle when:
- Agent has processed >= MSG_THRESHOLD messages since last reflection
- OR agent has had >= ERROR_THRESHOLD fallback/error events
- OR it has been > MAX_INTERVAL_HOURS since last cycle (safety net)

This means high-volume agents reflect more often, quiet agents don't waste
model calls, and reflection is always triggered by having enough signal.

Usage (cron every 2h on Mini):
    python3 self-improvement-trigger.py [--dry-run]
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SELF_SOURCE_BIN = Path(__file__).resolve().parent
if str(SELF_SOURCE_BIN) not in sys.path:
    sys.path.insert(0, str(SELF_SOURCE_BIN))
try:
    import papercut_inbox
except ImportError:  # Backward-compatible until the papercuts runtime payload lands.
    papercut_inbox = None

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_FILE = HERMES_HOME / "state" / "self-improvement-trigger.json"
TRIGGER_LOG = HERMES_HOME / "logs" / "self-improvement-trigger.log"
TOOL_READINESS_FILE = HERMES_HOME / "state" / "tool-readiness-probe-latest.json"
TOOL_READINESS_CONTEXT_FILE = HERMES_HOME / "state" / "tool-readiness-reflection-context.json"
REPAIR_ENVELOPE_DIR = HERMES_HOME / "state" / "repair-envelopes"
SPARK_SSH_HOST = os.environ.get("HERMES_SPARK_SSH_HOST", "")

# Thresholds
MSG_THRESHOLD = int(os.environ.get("SI_MSG_THRESHOLD", "100"))
ERROR_THRESHOLD = int(os.environ.get("SI_ERROR_THRESHOLD", "5"))
MAX_INTERVAL_HOURS = int(os.environ.get("SI_MAX_INTERVAL_HOURS", "72"))
TOOL_READINESS_MAX_AGE_SECONDS = int(os.environ.get("SI_TOOL_READINESS_MAX_AGE_SECONDS", "7200"))
TOOL_READINESS_ENABLED = os.environ.get("SI_TOOL_READINESS_ENABLED", "1").lower() not in ("0", "false", "no")
TOOL_READINESS_COOLDOWN_SECONDS = int(os.environ.get("SI_TOOL_READINESS_COOLDOWN_SECONDS", "21600"))
ERROR_SIG_COOLDOWN_SECONDS = int(os.environ.get("SI_ERROR_SIG_COOLDOWN", "86400"))

# Optional local-agent filter via env: SI_AGENTS=<agent_id>
_agent_filter = os.environ.get("SI_AGENTS", "").strip()


def _local_agent_id() -> str:
    for key in ("HERMES_AGENT_ID", "AGENT_ID"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    cfg_path = HERMES_HOME / "config.yaml"
    try:
        for line in cfg_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("agent_id:"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    except Exception:
        pass
    user = os.environ.get("USER", "agent").strip()
    return user.replace("spark-", "") or "agent"


ALL_MINI_AGENTS = [_local_agent_id()]
ALL_SPARK_AGENT_USERS: list[str] = []
AGENT_ALIASES: dict[str, str] = {}

if _agent_filter:
    _filtered = set(a.strip().lower() for a in _agent_filter.split(","))
    MINI_AGENTS = [a for a in ALL_MINI_AGENTS if a.lower() in _filtered]
    SPARK_AGENT_USERS: list[str] = []
else:
    MINI_AGENTS = ALL_MINI_AGENTS
    SPARK_AGENT_USERS: list[str] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_repair_envelope(packet: dict[str, Any], agent_id: str, report_id: object) -> Path:
    REPAIR_ENVELOPE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now().replace(":", "").replace("-", "")
    report_hash = hashlib.sha256(str(report_id or "").encode("utf-8")).hexdigest()[:12]
    safe_agent_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent_id).strip(".-") or "agent"
    packet_path = REPAIR_ENVELOPE_DIR / f"{timestamp}-{safe_agent_id}-{report_hash}-{uuid.uuid4().hex}.json"
    fd, temp_path = tempfile.mkstemp(prefix=".repair-envelope-", suffix=".tmp", dir=REPAIR_ENVELOPE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(packet, indent=2, sort_keys=True) + "\n")
        os.replace(temp_path, packet_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return packet_path


def log(msg: str) -> None:
    line = f"[{utc_now()}] {msg}"
    print(line, flush=True)
    TRIGGER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(TRIGGER_LOG, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"agents": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def _parse_probe_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def load_tool_readiness_context() -> dict[str, Any] | None:
    """Return a compact, redacted readiness summary for reflection, or None."""
    if not TOOL_READINESS_ENABLED or not TOOL_READINESS_FILE.exists():
        return None
    try:
        data = json.loads(TOOL_READINESS_FILE.read_text())
    except Exception as exc:
        return {"status": "unreadable", "detail": str(exc)[:160]}

    ts = _parse_probe_timestamp(str(data.get("timestamp", "")))
    age_seconds = time.time() - ts if ts else None
    if age_seconds is None or age_seconds > TOOL_READINESS_MAX_AGE_SECONDS:
        return {
            "status": "stale",
            "timestamp": data.get("timestamp"),
            "age_seconds": age_seconds,
            "max_age_seconds": TOOL_READINESS_MAX_AGE_SECONDS,
        }

    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    actionable = []
    for name, item in tools.items():
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).lower()
        if status not in {"broken", "degraded", "error"}:
            continue
        actionable.append(
            {
                "tool": name,
                "status": status,
                "plumbing": item.get("plumbing"),
                "detail": str(item.get("detail", ""))[:240],
                "fix_hint": str(item.get("fix_hint", ""))[:240],
            }
        )

    anti_patterns = []
    raw_patterns = data.get("anti_patterns", [])
    if isinstance(raw_patterns, list):
        for item in raw_patterns:
            if not isinstance(item, dict):
                continue
            anti_patterns.append(
                {
                    "pattern": item.get("pattern"),
                    "location": item.get("location"),
                    "detail": str(item.get("detail", ""))[:200],
                    "fix": str(item.get("fix", ""))[:200],
                }
            )

    broken = int(summary.get("broken") or 0)
    degraded = int(summary.get("degraded") or 0)
    return {
        "status": "ok",
        "source": str(TOOL_READINESS_FILE),
        "timestamp": data.get("timestamp"),
        "summary": summary,
        "actionable": actionable[:12],
        "anti_patterns": anti_patterns[:12],
        "reflection_instruction": (
            "Treat broken/degraded tool readiness as operational friction. "
            "Recommend a repair packet using fix_hint data, but do not mutate secrets, "
            "primary provider/model routing, or client runtimes without approval."
        ),
        "fingerprint": json.dumps(
            {
                "broken": broken,
                "degraded": degraded,
                "items": [(x.get("tool"), x.get("status"), x.get("detail"), x.get("fix_hint")) for x in actionable],
            },
            sort_keys=True,
        ),
    }


def tool_readiness_trigger_reason(state: dict, context: dict[str, Any] | None) -> str | None:
    if not context or context.get("status") != "ok":
        return None
    summary = context.get("summary") if isinstance(context.get("summary"), dict) else {}
    broken = int(summary.get("broken") or 0)
    degraded = int(summary.get("degraded") or 0)
    if broken <= 0 and degraded <= 0:
        return None
    tool_state = state.setdefault("tool_readiness", {})
    fingerprint = context.get("fingerprint")
    last_seen = tool_state.get("last_seen_fingerprint")
    last_ts = float(tool_state.get("last_trigger_ts") or 0)
    if fingerprint == last_seen:
        return None
    if time.time() - last_ts < TOOL_READINESS_COOLDOWN_SECONDS:
        return None
    return f"tool_health: broken={broken} degraded={degraded}"


def remember_tool_readiness_seen(state: dict, context: dict[str, Any] | None) -> None:
    if not context or context.get("status") != "ok":
        return
    state.setdefault("tool_readiness", {})["last_seen_fingerprint"] = context.get("fingerprint")


def persist_tool_readiness_context(context: dict[str, Any]) -> None:
    TOOL_READINESS_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in context.items() if k != "fingerprint"}
    TOOL_READINESS_CONTEXT_FILE.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n")


ERROR_LINE_RE = re.compile(r"\b(error|fallback|failed)\b", re.IGNORECASE)
PROVIDER_RE = re.compile(
    r"\b(codex|openrouter|openai|anthropic|claude|gemini|ollama|vllm|telegram|maton|composio)\b", re.IGNORECASE
)


def error_signature(path: Path | str, line: str) -> str:
    """Return a stable, coarse signature for an error/fallback log line."""
    basename = Path(str(path)).name
    keyword_match = ERROR_LINE_RE.search(line)
    keyword = keyword_match.group(1).lower() if keyword_match else "error"
    provider_match = PROVIDER_RE.search(line)
    if provider_match:
        token = provider_match.group(1).lower()
    else:
        compact = re.sub(r"[^a-z0-9]+", " ", line.lower())
        words = [w for w in compact.split() if not w.isdigit()]
        token = "-".join(words[:6]) or "generic"
    return f"{basename}:{keyword}:{token[:80]}"


def _count_new_error_lines(log_dir: Path, state: dict, since_ts: float, *, prefix: str = "") -> int:
    """Count novel error/fallback/failed lines appended since the previous run.

    State is additive and backward-compatible:
      - log_offsets[path] = last byte offset read
      - error_signatures[signature] = last counted unix timestamp
    """
    if not log_dir.exists():
        return 0
    offsets = state.setdefault("log_offsets", {})
    signatures = state.setdefault("error_signatures", {})
    now_ts = time.time()
    count = 0
    for path in sorted(log_dir.glob("*.log")):
        key = f"{prefix}{path}"
        try:
            st = path.stat()
            old_offset = int(offsets.get(key, 0) or 0)
            offset = old_offset if old_offset <= st.st_size else 0
            if st.st_mtime <= since_ts and offset >= st.st_size:
                continue
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
            offsets[key] = st.st_size
            if not chunk:
                continue
            text = chunk.decode("utf-8", errors="ignore")
            for line in text.splitlines():
                if not ERROR_LINE_RE.search(line):
                    continue
                sig = error_signature(path, line)
                last_counted = float(signatures.get(sig) or 0)
                if now_ts - last_counted <= ERROR_SIG_COOLDOWN_SECONDS:
                    continue
                signatures[sig] = now_ts
                count += 1
        except Exception:
            continue
    return count


def count_mini_messages(agent_id: str, since_ts: float, state: dict | None = None) -> tuple[int, int]:
    """Count messages and errors for a Mini agent since timestamp."""
    msg_count = 0
    error_count = 0

    # Check state.db for message count
    db_path = HERMES_HOME / "state.db"
    if db_path.exists():
        try:
            result = subprocess.run(
                ["sqlite3", str(db_path), f"SELECT COUNT(*) FROM messages WHERE timestamp > {since_ts}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                msg_count = int(result.stdout.strip() or "0")
        except Exception:
            pass

    error_count = _count_new_error_lines(HERMES_HOME / "logs", state if state is not None else {}, since_ts)

    return msg_count, error_count


def count_spark_messages(user: str, since_ts: float) -> tuple[int, int]:
    """Count messages and errors for a Spark agent via SSH."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=10",
                f"{user}@{SPARK_SSH_HOST}",
                (
                    'sqlite3 ~/.hermes/state.db "SELECT COUNT(*) FROM messages '
                    f'WHERE timestamp > {since_ts}" 2>/dev/null || echo 0'
                ),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        msg_count = int(result.stdout.strip() or "0") if result.returncode == 0 else 0

        result2 = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=10",
                f"{user}@{SPARK_SSH_HOST}",
                "python3 - <<'PY'\n"
                "from pathlib import Path\n"
                "import os, re, time\n"
                f"since_ts = {float(since_ts)!r}\n"
                "cooldown = int(os.environ.get('SI_ERROR_SIG_COOLDOWN', '86400'))\n"
                "home = Path.home() / '.hermes'\n"
                "state_file = home / 'state' / 'self-improvement-trigger.json'\n"
                "try:\n"
                "    import json\n"
                "    state = json.loads(state_file.read_text()) if state_file.exists() else {}\n"
                "except Exception:\n"
                "    state = {}\n"
                "offsets = state.setdefault('log_offsets', {})\n"
                "sigs = state.setdefault('error_signatures', {})\n"
                "err = re.compile(r'\\b(error|fallback|failed)\\b', re.I)\n"
                "prov = re.compile(r'\\b(codex|openrouter|openai|anthropic|claude|"
                "gemini|ollama|vllm|telegram|maton|composio)\\b', re.I)\n"
                "now = time.time(); count = 0\n"
                "def sig(path, line):\n"
                "    m = err.search(line); p = prov.search(line)\n"
                "    keyword = m.group(1).lower() if m else 'error'\n"
                "    words = re.sub(r'[^a-z0-9]+', ' ', line.lower()).split()\n"
                "    token = p.group(1).lower() if p else "
                "'-'.join([w for w in words if not w.isdigit()][:6]) or 'generic'\n"
                "    return f'{Path(path).name}:{keyword}:{token[:80]}'\n"
                "for path in sorted((home / 'logs').glob('*.log')):\n"
                "    try:\n"
                "        st = path.stat(); key = str(path)\n"
                "        old = int(offsets.get(key, 0) or 0)\n"
                "        off = old if old <= st.st_size else 0\n"
                "        if st.st_mtime <= since_ts and off >= st.st_size:\n"
                "            continue\n"
                "        with path.open('rb') as f:\n"
                "            f.seek(off); data = f.read()\n"
                "        offsets[key] = st.st_size\n"
                "        for line in data.decode('utf-8', errors='ignore').splitlines():\n"
                "            if not err.search(line):\n"
                "                continue\n"
                "            s = sig(path, line); last = float(sigs.get(s) or 0)\n"
                "            if now - last <= cooldown:\n"
                "                continue\n"
                "            sigs[s] = now; count += 1\n"
                "    except Exception:\n"
                "        pass\n"
                "try:\n"
                "    state_file.parent.mkdir(parents=True, exist_ok=True)\n"
                "    state_file.write_text(json.dumps(state, indent=2) + '\\n')\n"
                "except Exception:\n"
                "    pass\n"
                "print(count)\n"
                "PY",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        error_count = int(result2.stdout.strip() or "0") if result2.returncode == 0 else 0

        return msg_count, error_count
    except Exception:
        return 0, 0


def pending_papercut_count(home: Path = HERMES_HOME) -> int:
    if papercut_inbox is None:
        return 0
    try:
        return int(papercut_inbox.snapshot(home).get("pending_count") or 0)
    except Exception:
        return 0


def should_trigger(
    agent_id: str,
    state: dict,
    msg_count: int,
    error_count: int,
    papercut_count: int = 0,
) -> tuple[bool, str]:
    """Decide if self-improvement should fire for this agent."""
    agent_state = state["agents"].get(agent_id, {})
    last_run = agent_state.get("last_run_ts", 0)
    hours_since = (time.time() - last_run) / 3600

    if papercut_count > 0:
        return True, f"papercut_inbox={papercut_count} pending"
    if msg_count >= MSG_THRESHOLD:
        return True, f"msg_count={msg_count} >= {MSG_THRESHOLD}"
    if error_count >= ERROR_THRESHOLD:
        return True, f"error_count={error_count} >= {ERROR_THRESHOLD}"
    if hours_since > MAX_INTERVAL_HOURS:
        return True, f"safety_net: {hours_since:.1f}h > {MAX_INTERVAL_HOURS}h"

    return False, f"msgs={msg_count}, errors={error_count}, papercuts={papercut_count}, hours={hours_since:.1f}"


def find_latest_report(agent_id: str, host: str, user: str | None) -> Path | str | None:
    """Find the latest reflection report file for an agent."""
    if host == "localhost":
        report_dir = HERMES_HOME / "workspace" / "ops" / "reports" / "client-day-review"
        if report_dir.exists():
            files = sorted(report_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            return files[0] if files else None
    else:
        ssh_target = f"{user}@{host}"
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=10",
                ssh_target,
                "ls -t ~/.hermes/workspace/ops/reports/client-day-review/*.json 2>/dev/null | head -1",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def read_report(agent_id: str, host: str, user: str | None, report_path: Path | str | None) -> dict:
    """Read a reflection report from local path or remote host."""
    if not report_path:
        return {}
    if host == "localhost" and isinstance(report_path, Path):
        try:
            return json.loads(report_path.read_text())
        except Exception:
            return {}
    elif isinstance(report_path, str):
        ssh_target = f"{user}@{host}"
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", ssh_target, f"cat {report_path}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            return json.loads(result.stdout) if result.returncode == 0 else {}
        except Exception:
            return {}
    return {}


def papercut_repair_actions(report: dict, self_reflection: dict) -> list[dict]:
    metadata = report.get("self_reflection_meta") or {}
    inbox = metadata.get("papercut_inbox") if isinstance(metadata, dict) else {}
    event_ids = inbox.get("event_ids", []) if isinstance(inbox, dict) else []
    pending_ids = {str(event_id) for event_id in event_ids} if isinstance(event_ids, list) else set()
    actions = self_reflection.get("papercut_actions") or []
    repairs = []
    for action in actions:
        if (
            not isinstance(action, dict)
            or action.get("disposition") not in {"repair", "escalate"}
            or not str(action.get("next_action") or "").strip()
            or not isinstance(action.get("papercut_ids"), list)
        ):
            continue
        event_ids = [str(event_id) for event_id in action["papercut_ids"] if str(event_id) in pending_ids]
        if event_ids:
            repairs.append({**action, "papercut_ids": event_ids})
    return repairs


def act_on_reflection(
    agent_id: str,
    report: dict,
    host: str,
    user: str | None,
    report_path: Path | str | None = None,
) -> dict[str, Any]:
    """Queue reflection findings and capture operational escalation signals."""
    if not report:
        log(f"no report to act on for {agent_id}")
        return {"status": "no_report", "queued": 0, "idempotent": 0, "rejected": 0}

    candidates = list(report.get("skillify_candidates") or [])
    candidates.extend(report.get("proposal_inputs") or [])
    friction = report.get("friction", {})
    self_reflection = report.get("self_reflection", {})
    actions_taken = []
    outbox_status: dict[str, Any] = {
        "status": "no_candidates",
        "queued": 0,
        "idempotent": 0,
        "rejected": 0,
    }

    # 1. Reflection findings become content-free proposal envelopes, never drafts.
    if candidates:
        if host == "localhost":
            drafts_script = HERMES_HOME / "bin" / "hermes-reflect-candidates-to-drafts.py"
            command = [sys.executable, str(drafts_script)]
            if report_path:
                command.extend(["--report", str(report_path)])
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                try:
                    envelope_result = json.loads(result.stdout or "{}")
                except Exception:
                    envelope_result = {}
                for source_id in envelope_result.get("queued", []):
                    actions_taken.append(f"proposal queued: {source_id}")
                for source_id in envelope_result.get("idempotent", []):
                    actions_taken.append(f"proposal already queued: {source_id}")
                outbox_status = {
                    "status": str(envelope_result.get("status") or "queued"),
                    "queued": len(envelope_result.get("queued", [])),
                    "idempotent": len(envelope_result.get("idempotent", [])),
                    "rejected": len(envelope_result.get("rejected", [])),
                }
                log(f"  reflection proposals queued: {result.stdout.strip()[:240]}")
            else:
                outbox_status = {"status": "rejected", "queued": 0, "idempotent": 0, "rejected": 1}
                log(f"  reflection proposal queue failed: {(result.stdout or result.stderr)[:240]}")
        else:
            ssh_target = f"{user}@{host}"
            remote_command = "python3 ~/.hermes/bin/hermes-reflect-candidates-to-drafts.py"
            if report_path:
                remote_command += f" --report {shlex.quote(str(report_path))}"
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=15", ssh_target, remote_command + " 2>&1"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                try:
                    envelope_result = json.loads(result.stdout or "{}")
                except Exception:
                    envelope_result = {}
                for source_id in envelope_result.get("queued", []):
                    actions_taken.append(f"proposal queued: {source_id}")
                for source_id in envelope_result.get("idempotent", []):
                    actions_taken.append(f"proposal already queued: {source_id}")
                outbox_status = {
                    "status": str(envelope_result.get("status") or "queued"),
                    "queued": len(envelope_result.get("queued", [])),
                    "idempotent": len(envelope_result.get("idempotent", [])),
                    "rejected": len(envelope_result.get("rejected", [])),
                }
                log(f"  reflection proposals queued on {host}: {result.stdout.strip()[:240]}")
            else:
                outbox_status = {"status": "rejected", "queued": 0, "idempotent": 0, "rejected": 1}
                log(f"  reflection proposal queue failed on {host}: {(result.stdout or result.stderr)[:240]}")

    # 2. Capture friction as fleet learning candidates
    friction_summary = []
    if isinstance(friction, dict):
        for ftype, fdata in friction.items():
            count = fdata.get("count", 0) if isinstance(fdata, dict) else 0
            if count > 0:
                friction_summary.append(f"{ftype}: {count}")

    if friction_summary and self_reflection:
        # Durable synthesis belongs to the central router/Doc path. Do not let a
        # fleet runtime rewrite its own shared behavior or strategy from a
        # reflection result.
        log(f"reflection friction retained in local report for central review: {', '.join(friction_summary)}")

    external_context = (report.get("self_reflection_meta", {}) or {}).get("external_context", {})
    readiness_items = external_context.get("actionable", []) if isinstance(external_context, dict) else []
    readiness_needs_doc = any(
        isinstance(item, dict) and str(item.get("status", "")).lower() in {"broken", "degraded", "error"}
        for item in readiness_items
    )
    model_escalates = isinstance(self_reflection, dict) and self_reflection.get("escalate_to_doc")
    papercut_repairs = papercut_repair_actions(report, self_reflection) if isinstance(self_reflection, dict) else []
    if readiness_needs_doc or model_escalates or papercut_repairs:
        log(f"ESCALATION: {agent_id} self-assessment flagged escalation needed")
        packet = {
            "ts": utc_now(),
            "agent_id": agent_id,
            "host": host,
            "runtime_home": str(HERMES_HOME),
            "source": "self-improvement-trigger",
            "reason": (
                "papercut_remediation"
                if papercut_repairs
                else "tool_readiness_degraded"
                if readiness_needs_doc
                else "self_reflection_escalate_to_doc"
            ),
            "tool_readiness_context": external_context,
            "papercut_actions": papercut_repairs,
            "report_id": report.get("report_id"),
            "verdict": report.get("verdict", {}),
            "self_reflection": {
                "did_it_suck": self_reflection.get("did_it_suck"),
                "narrative": self_reflection.get("narrative"),
                "failures": self_reflection.get("failures", []),
                "open_promises": self_reflection.get("open_promises", []),
                "skillify_candidates": self_reflection.get("skillify_candidates", []),
            },
            "expected_operator_action": (
                "Inspect the cited runtime and papercuts, apply only bounded safe repairs, and request approval for "
                "credentials, restarts, client/fleet mutation, destructive actions, or other hard stops."
            ),
        }
        packet_path = write_repair_envelope(packet, agent_id, report.get("report_id"))
        log(f"LOCAL_REPAIR_ENVELOPE: {packet_path}")

    log(
        f"reflection actions for {agent_id}: {len(actions_taken)} taken, "
        f"{len(candidates)} candidates, friction={', '.join(friction_summary) or 'none'}"
    )
    return outbox_status


def launch_reflection(
    agent_id: str, host: str, user: str | None = None, tool_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Launch self-reflection, read report, and act on results."""
    log(f"launching self-reflection for {agent_id} on {host}")

    # Find pre-reflection latest report (to compare after)
    pre_report = find_latest_report(agent_id, host, user)

    if host == "localhost":
        env = os.environ.copy()
        if tool_context:
            persist_tool_readiness_context(tool_context)
            env["HERMES_SELF_REFLECTION_CONTEXT_FILE"] = str(TOOL_READINESS_CONTEXT_FILE)
        result = subprocess.run(
            [
                sys.executable,
                str(HERMES_HOME / "bin" / "hermes-self-reflect.py"),
                "--print",
                "--home",
                str(HERMES_HOME),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        if result.returncode != 0:
            log(f"reflection for {agent_id} failed: {result.stderr[:200]}")
            return {"reflection_status": "failed", "proposal_outbox_status": "not_run"}
    else:
        ssh_target = f"{user}@{host}"
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=15", ssh_target, "python3 ~/.hermes/bin/hermes-self-reflect.py --print 2>&1"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            log(f"reflection for {agent_id} on {host} failed: {result.stderr[:200]}")
            return {"reflection_status": "failed", "proposal_outbox_status": "not_run"}

    log(f"reflection completed for {agent_id}")

    # Find the NEW report (after reflection)
    post_report = find_latest_report(agent_id, host, user)
    if post_report and post_report != pre_report:
        report = read_report(agent_id, host, user, post_report)
        outbox = act_on_reflection(agent_id, report, host, user, post_report)
        return {"reflection_status": "completed", "proposal_outbox_status": outbox["status"], **outbox}
    else:
        log(f"no new report generated for {agent_id}")
        return {"reflection_status": "no_new_report", "proposal_outbox_status": "not_run"}


def main():
    dry_run = "--dry-run" in sys.argv
    state = load_state()
    now_ts = time.time()
    triggered = []
    skipped = []
    tool_context = load_tool_readiness_context()
    tool_reason = tool_readiness_trigger_reason(state, tool_context)
    papercut_count = pending_papercut_count(HERMES_HOME)
    if tool_context and tool_context.get("status") != "ok":
        log(f"Tool readiness context skipped: {tool_context.get('status')} {tool_context.get('detail', '')}")

    for agent_id in MINI_AGENTS:
        agent_state = state["agents"].get(agent_id, {})
        since_ts = agent_state.get("last_run_ts", now_ts - 7 * 86400)  # default 7 days

        msg_count, error_count = count_mini_messages(agent_id, since_ts, state)
        should, reason = should_trigger(agent_id, state, msg_count, error_count, papercut_count)
        if tool_reason:
            should, reason = True, tool_reason

        if should:
            log(f"TRIGGER {agent_id}: {reason}")
            triggered.append(agent_id)
            if not dry_run:
                run_result = launch_reflection(
                    agent_id, "localhost", tool_context=tool_context if reason == tool_reason else None
                )
                state["agents"][agent_id] = {
                    "last_run_ts": now_ts,
                    "last_reason": reason,
                    "last_msgs": msg_count,
                    "last_errors": error_count,
                    "last_papercuts": papercut_count,
                    **run_result,
                }
                if reason == tool_reason and tool_context:
                    state.setdefault("tool_readiness", {})["last_trigger_ts"] = now_ts
                    state.setdefault("tool_readiness", {})["last_trigger_fingerprint"] = tool_context.get("fingerprint")
        else:
            skipped.append(f"{agent_id} ({reason})")
        remember_tool_readiness_seen(state, tool_context)

    for user in SPARK_AGENT_USERS:
        agent_id = user.replace("spark-", "")
        agent_state = state["agents"].get(agent_id, {})
        since_ts = agent_state.get("last_run_ts", now_ts - 7 * 86400)

        msg_count, error_count = count_spark_messages(user, since_ts)
        should, reason = should_trigger(agent_id, state, msg_count, error_count)

        if should:
            log(f"TRIGGER {agent_id} ({user}): {reason}")
            triggered.append(agent_id)
            if not dry_run:
                run_result = launch_reflection(agent_id, SPARK_SSH_HOST, user)
                state["agents"][agent_id] = {
                    "last_run_ts": now_ts,
                    "last_reason": reason,
                    "last_msgs": msg_count,
                    "last_errors": error_count,
                    **run_result,
                }
        else:
            skipped.append(f"{agent_id} ({reason})")

    if not dry_run:
        save_state(state)

    if dry_run and tool_reason:
        log(f"DRY-RUN tool readiness would trigger local reflection: {tool_reason}")
    log(f"Cycle complete: {len(triggered)} triggered, {len(skipped)} skipped")
    if skipped:
        log(f"  Skipped: {', '.join(skipped[:10])}")


if __name__ == "__main__":
    main()
