#!/usr/bin/env python3
"""hermes-self-reflect.py — agent-led nightly self-reflection.

Runs ON the agent's own host during its dream cycle. Steps:
  1. Gather deterministic signals: run hermes-client-day-review against this
     agent's own home (hand-tuned contract if present, else auto-derive a
     generic one), plus `hermes insights`.
  2. Feed those signals to the agent's OWN loaded model via `hermes -z` and ask
     it to self-assess: did today's client work suck? where did I leak/fail/
     over-escalate? what repeated class-level weaknesses deserve proposal-first
     review? what promises are still open?
  3. Merge the model's judgment into the deterministic report (superset schema)
     and write it to <home>/workspace/ops/reports/client-day-review/.

Read + report only. Never restarts, never touches other agents. If the model
call fails, the deterministic day-review report is still written (no blank).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SELF_SOURCE_BIN = Path(__file__).resolve().parent
if str(SELF_SOURCE_BIN) not in sys.path:
    sys.path.insert(0, str(SELF_SOURCE_BIN))
try:
    import papercut_inbox
except ImportError:  # Backward-compatible until the papercuts runtime payload lands.
    papercut_inbox = None

REPORTS_DIR = "workspace/ops/reports/client-day-review"
STAFF_EVENTS_PATH = "state/specialist-management/events.jsonl"
LEGACY_STAFF_EVENTS_PATH = "state/client-teams/specialist-management-events.jsonl"
STAFF_EVENT_PATHS = (STAFF_EVENTS_PATH, LEGACY_STAFF_EVENTS_PATH)
STAFF_EVENT_SCHEMA = "specialist-management-event/v1"
STAFF_REFLECTION_SCHEMA = "specialist-staff-reflection/v1"
MAX_STAFF_EVENT_BYTES = 8 * 1024 * 1024
MAX_STAFF_EVENTS = 10_000
STAFF_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
STAFF_EVENT_ID_PATTERN = re.compile(r"^sme_[0-9a-f]{20}$")
MAX_RECENT_SKILLS = 24
MAX_SKILL_USAGE_BYTES = 1_000_000
MAX_SKILL_BYTES = 1_000_000
SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
SKILL_EVIDENCE_CLASSES = {
    "client_rework",
    "operator_correction",
    "repeated_failure",
    "repeated_success",
}
SCHEDULE_PROVENANCE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|private[_-]?key)", re.IGNORECASE)
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?im)((?:\\?[\"'])?(?:api[_-]?key|token|secret|password|authorization|private[_-]?key)(?:\\?[\"'])?\s*[:=]\s*)(?:\\\"(?:\\.|[^\"\\])*\\\"|\\'(?:\\.|[^'\\])*\\'|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n,;}\]]+)"
)
AUTH_CREDENTIAL_PATTERN = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN (?P<label>[^-\r\n]*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*?-----END (?P=label)-----",
    re.IGNORECASE,
)


def default_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def find_day_review(home: Path):
    """Locate the day-review runner; it may be deployed to bin/ or live in a
    golden checkout, or be shipped alongside this script."""
    here = Path(__file__).resolve().parent
    for c in [
        home / "bin" / "hermes-client-day-review",
        here / "hermes-client-day-review",
        home / "repos/hermes-golden/bin/hermes-client-day-review",
    ]:
        if c.exists():
            return c
    return home / "bin" / "hermes-client-day-review"  # default (may not exist)


def find_hermes_cli(home: Path):
    """Resolve the selected runtime's CLI before interpreter or PATH fallbacks."""
    bound_python = active_runtime_python(home)
    if bound_python is not None:
        hermes = bound_python.parent / ("hermes.exe" if IS_WIN else "hermes")
        return [str(hermes)] if hermes.is_file() else [str(bound_python), "-m", "hermes_cli.main"]
    for bin_dir in _venv_bin_dirs(home):
        hermes = bin_dir / ("hermes.exe" if IS_WIN else "hermes")
        if hermes.exists():
            return [str(hermes)]
    for py in _venv_pythons(home):
        if py.exists():
            return [str(py), "-m", "hermes_cli.main"]
    try:
        import importlib.util

        if importlib.util.find_spec("hermes_cli") is not None:
            return [sys.executable, "-m", "hermes_cli.main"]
    except Exception:
        pass
    from shutil import which

    w = which("hermes")
    if w:
        return [w]
    return ["hermes"]


HERMES_CLI = None  # resolved in main()


def iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_regular_bytes(path: Path, maximum_bytes: int):
    """Read one bounded regular file without following a final symlink."""
    try:
        before = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow:
        flags |= no_follow
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > maximum_bytes:
            return None
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(maximum_bytes + 1)
        return content if len(content) <= maximum_bytes else None
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _skill_identity(content: bytes):
    try:
        text = content[:16_384].decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    name = ""
    version = "unversioned"
    closed = False
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            closed = True
            break
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        value = value.strip().strip('"\'')
        if key == "name":
            name = value
        elif key == "version" and value:
            version = value[:64]
    if not closed or not SKILL_ID_PATTERN.fullmatch(name) or len(version) > 64:
        return None
    return name, version


def recent_skill_usage(home: Path, hours: int, now=None):
    """Return bounded, exact identities for skills successfully loaded recently.

    The Hermes-owned sidecar is treated only as load evidence. This function
    neither infers outcome causality nor mutates skill state.
    """
    usage_path = home / "skills" / ".usage.json"
    content = _read_regular_bytes(usage_path, MAX_SKILL_USAGE_BYTES)
    if content is None:
        return []
    try:
        usage = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(usage, dict):
        return []
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).timestamp() - max(1, hours) * 3600
    candidates = {}
    for raw_name, record in usage.items():
        name = str(raw_name)
        if not SKILL_ID_PATTERN.fullmatch(name) or not isinstance(record, dict):
            continue
        used_at = _parse_timestamp(record.get("last_used_at"))
        try:
            use_count = int(record.get("use_count") or 0)
        except (TypeError, ValueError):
            continue
        if used_at is None or used_at.timestamp() < cutoff or use_count < 1:
            continue
        candidates[name] = (record, used_at, use_count)
    if not candidates:
        return []

    resolved = {}
    skills_root = home / "skills"
    try:
        skill_paths = skills_root.rglob("SKILL.md")
        for index, skill_path in enumerate(skill_paths):
            if index >= 2_000:
                break
            try:
                relative_parts = skill_path.relative_to(skills_root).parts
            except ValueError:
                continue
            if any(part.startswith(".") for part in relative_parts[:-1]):
                continue
            skill_content = _read_regular_bytes(skill_path, MAX_SKILL_BYTES)
            if skill_content is None:
                continue
            identity = _skill_identity(skill_content)
            if identity is None or identity[0] not in candidates:
                continue
            name, version = identity
            if name in resolved:  # ambiguous identities fail closed
                resolved[name] = None
                continue
            record, used_at, use_count = candidates[name]
            provenance = str(record.get("created_by") or "local")
            if provenance not in {"agent", "installed", "local"}:
                provenance = "local"
            resolved[name] = {
                "skill_id": name,
                "skill_version": version,
                "skill_sha256": hashlib.sha256(skill_content).hexdigest(),
                "last_used_at": used_at.isoformat().replace("+00:00", "Z"),
                "use_count": min(use_count, 1_000_000_000),
                "provenance": provenance,
                "evidence": "successful_load_within_window",
            }
    except OSError:
        return []
    rows = [value for value in resolved.values() if isinstance(value, dict)]
    return sorted(rows, key=lambda row: (row["last_used_at"], row["skill_id"]), reverse=True)[:MAX_RECENT_SKILLS]


IS_WIN = os.name == "nt"


def active_runtime_python(home: Path) -> Path | None:
    """Resolve the immutable active runtime from its target-bound binding receipt."""
    binding_path = home / "state" / "runtime-binding.json"
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        if binding.get("status") != "active":
            return None
        declared_home = Path(str(binding.get("hermes_home") or "")).expanduser().resolve()
        runtime_root = Path(str(binding.get("runtime_root") or "")).expanduser().resolve()
        runtime_python = Path(str(binding.get("runtime_python") or "")).expanduser().resolve()
        expected_candidates = (home / "state" / "runtime-candidates").resolve()
        if declared_home != home.resolve():
            return None
        if runtime_root != expected_candidates and expected_candidates not in runtime_root.parents:
            return None
        if runtime_python != runtime_root and runtime_root not in runtime_python.parents:
            return None
        if not runtime_python.is_file():
            return None
        return runtime_python
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _venv_bin_dirs(home: Path):
    runtime_parents = list(home.parents)
    if IS_WIN:
        return [
            home / "venv/Scripts",
            home / ".venv/Scripts",
            home / "hermes-agent/venv/Scripts",
            home / "hermes-agent/.venv/Scripts",
            *[parent / "venv/Scripts" for parent in runtime_parents],
            *[parent / ".venv/Scripts" for parent in runtime_parents],
        ]
    return [
        home / "venv/bin",
        home / ".venv/bin",
        home / "hermes-agent/venv/bin",
        home / "hermes-agent/.venv/bin",
        *[parent / "venv/bin" for parent in runtime_parents],
        *[parent / ".venv/bin" for parent in runtime_parents],
    ]


def _venv_pythons(home: Path):
    """Candidate venv python interpreters across known layouts (Mini/Spark/Windows)."""
    runtime_parents = list(home.parents)
    if IS_WIN:
        return [
            home / "venv/Scripts/python.exe",
            home / ".venv/Scripts/python.exe",
            home / "hermes-agent/venv/Scripts/python.exe",
            home / "hermes-agent/.venv/Scripts/python.exe",
            *[parent / "venv/Scripts/python.exe" for parent in runtime_parents],
            *[parent / ".venv/Scripts/python.exe" for parent in runtime_parents],
        ]
    cands = [
        home / "venv/bin/python3",
        home / "venv/bin/python",
        home / ".venv/bin/python3",
        home / ".venv/bin/python",
        home / "hermes-agent/venv/bin/python3",
        home / "hermes-agent/.venv/bin/python3",
        *[parent / "venv/bin/python3" for parent in runtime_parents],
        *[parent / "venv/bin/python" for parent in runtime_parents],
        *[parent / ".venv/bin/python3" for parent in runtime_parents],
    ]
    # discover any *venv*/bin/python under home (one level) as a fallback
    for g in list(home.parent.glob("*/bin/python3")) + list(home.parent.glob("*/bin/python")):
        if "venv" in str(g).lower():
            cands.append(g)
    return cands


def find_python(home: Path):
    for c in _venv_pythons(home):
        if c.exists():
            return str(c)
    if not IS_WIN and Path("/usr/bin/python3").exists():
        return "/usr/bin/python3"
    return sys.executable


def read_config(home: Path):
    cfg = {}
    p = home / "config.yaml"
    if not p.exists():
        # profile layout: newest profiles/*/config.yaml
        cands = sorted(home.glob("profiles/*/config.yaml"), key=lambda x: x.stat().st_mtime, reverse=True)
        if cands:
            p = cands[0]
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"^(agent_name|agent_id|client_id|client_name|host)\s*:\s*(.+)$", line.strip())
            if m:
                cfg[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except Exception:
        pass
    return cfg


def soul_name(home: Path):
    for f in [home / "SOUL.md"]:
        try:
            first = f.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            m = re.search(r"SOUL(?:\.md)?\s*[\-\u2014:]+\s*([A-Za-z][A-Za-z0-9'’ ]{1,40})", first)
            if m:
                return m.group(1).strip()
            m2 = re.search(r"#\s*([A-Za-z][A-Za-z0-9'’]+)\s+Client Context", first)
            if m2:
                return m2.group(1).strip()
        except Exception:
            pass
    return ""


def existing_files(home, candidates):
    return [c for c in candidates if (home / c).exists()]


def find_contract(home: Path, agent_id, client_id):
    """Return path to a hand-tuned contract if one matches this agent/client."""
    keys = [k for k in [client_id, agent_id] if k]
    for d in [
        home / "workspace/shared-context/client-day-review/contracts",
        home / "repos/hermes-golden/shared-context/client-day-review/contracts",
    ]:
        if not d.exists():
            continue
        for y in sorted(d.glob("*.yaml")):
            stem = y.stem.lower()
            if any(k and k.lower() in stem for k in keys):
                return y
    return None


def derive_generic_contract(home, cfg):
    """Synthesize a sane default contract for agents without a hand-tuned one.
    Identity from config/soul; signal sources limited to files that exist."""
    agent_name = cfg.get("agent_name") or soul_name(home) or "Agent"
    agent_id = cfg.get("agent_id") or re.sub(r"[^a-z0-9]+", "", agent_name.lower()) or "agent"
    client_id = cfg.get("client_id") or agent_id
    client_name = cfg.get("client_name") or agent_name
    host = cfg.get("host") or __import__("socket").gethostname()

    candidate_sources = [
        "data/telegram-transcript.db",
        "gateway_state.json",
        "logs/gateway.log",
        "logs/agent.log",
        "logs/errors.log",
        "state/telegram-delivery-proof/proofs.jsonl",
        "state/send-audit.jsonl",
        "state.db",
        "lcm.db",
    ]
    optional_sources = [
        "data/durable-threads.db",
        "artifacts",
        "output",
        "state/model-usage.jsonl",
        "state/openrouter-usage.jsonl",
        "cron/jobs.json",
    ]
    required = existing_files(home, candidate_sources)
    optional = existing_files(home, optional_sources)

    return {
        "schema_version": 1,
        "contract_id": f"{client_id}-{agent_id}-day-review",
        "contract_version": 1,
        "client": {
            "client_id": client_id,
            "client_name": client_name,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "host": host,
            "runtime_home": str(home),
        },
        "review_policy": {
            "window_hours": 24,
            "green_reports": "store_only",
            "yellow_report_threshold": (
                "any unresolved client ask, degraded expected capability, repeated "
                "fallback storm, or smoothness score under 85"
            ),
            "red_report_threshold": (
                "client-visible leak, delivery failure after completion claim, gateway "
                "down, Telegram disconnected, or credential/secret exposure"
            ),
        },
        "expected_workflows": [
            {
                "id": "client-chat-help",
                "name": f"Respond to {client_name} client requests in Telegram",
                "success_criteria": [
                    "actionable client asks receive a useful response or explicit blocker",
                    "no internal stack traces, provider chatter, raw errors, or secret material are client-visible",
                    "promised work is either delivered with proof or tracked as an open promise",
                ],
                "required_capabilities": ["telegram", "conversation_memory"],
                "task_contract": {
                    "intended_outcome": "actionable client request receives a useful response or explicit blocker",
                    "required_tools": ["telegram", "conversation_memory"],
                    "deliverable": "client_response_or_explicit_blocker",
                    "independent_certifier": "client_acceptance_or_doc_verdict",
                },
                "skillify_when": [
                    "same client request pattern appears at least three times in seven days",
                    "same manual tool sequence is repeated twice in one day",
                ],
                "employee_when": [
                    "recurring work consumes multiple delegated runtimes or exceeds one hour/day for three days",
                ],
            }
        ],
        "capabilities": {
            "expected": [
                "telegram",
                "local_conversation_memory",
                "gateway_health",
                "delivery_proof_for_client_visible_sends",
            ],
            "conditional": ["browser_access_when_client_requests_web_work", "file_generation_when_artifact_requested"],
            "ignored": ["composio", "gmail", "google_calendar", "google_tasks", "crm", "mls", "retell", "linkedin"],
        },
        "visibility_policy": {
            "allowed_client_visible_internal_terms": [agent_name],
            "allowed_path_prefixes": [],
            "forbidden_client_visible_patterns": [
                "Traceback",
                "API call failed",
                "provider failed",
                "Missing Authentication",
                r"\b(secret|api[_-]?key|access[_-]?token|auth[_-]?token|bearer)\s*[:=]",
                r"sk-[a-z0-9_-]{12,}",
                r"HTTP 4\d\d",
                r"more credits",
                r"\bCanary Lane\b",
                r"\blocal self-check\b",
                r"\bhelper-script\b",
                r"\bpackaging-sync\b",
                r"Gateway is running",
                r"Telegram is connected",
            ],
        },
        "signals": {
            "required_sources": required,
            "optional_sources": optional,
            "ignored_sources": [],
        },
        "escalation_policy": {
            "red": [
                "gateway down or Telegram disconnected during review window",
                "client-visible secret, stack trace, provider/tool internals, or unrequested local path leak",
                "completion or delivery claim without delivery proof",
            ],
            "yellow": [
                "fallback storm that self-recovers but affects latency or quality",
                "repeated negative feedback from the client",
                "open promise without evidence of tracking",
                "repeated manual workflow suitable for Skillify",
            ],
            "suppressions": [],
        },
    }


def run_day_review(day_review: Path, contract_path: Path, home: Path, hours: int, py: str):
    cmd = [py, str(day_review), "--contract", str(contract_path), "--home", str(home), "--hours", str(hours), "--json"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"day-review failed rc={res.returncode}: {res.stderr.strip()[:400]}")
    return json.loads(res.stdout.strip())


def gather_insights():
    out = {}
    for sub, key in [(["insights", "--days", "1"], "insights_1d")]:
        try:
            r = subprocess.run(HERMES_CLI + sub, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                out[key] = r.stdout.strip()[:4000]
        except Exception:
            pass
    return out


def load_external_context():
    path = os.environ.get("HERMES_SELF_REFLECTION_CONTEXT_FILE", "").strip()
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        return {"status": "unreadable", "source": path}
    return None


def load_papercut_context(home: Path) -> dict:
    if papercut_inbox is None:
        return {"status": "unavailable", "pending_count": 0, "event_ids": [], "patterns": [], "events": []}
    try:
        return {"status": "ok", **papercut_inbox.snapshot(home)}
    except Exception as exc:
        return {
            "status": "unreadable",
            "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
            "pending_count": 0,
            "event_ids": [],
            "patterns": [],
            "events": [],
        }


def _staff_recommendation(counts: Counter) -> tuple[str, list[str]]:
    reasons = []
    if counts["retired"]:
        return "retire", ["manager_recorded_retirement"]
    if counts["invalid_gate"]:
        reasons.append("invalid_manager_gate")
    if counts["failed_or_timeout"]:
        reasons.append("worker_failed_or_timed_out")
    if counts["manager_override"]:
        reasons.append("manager_override_required")
    if counts["rework"]:
        reasons.append("manager_rework_required")
    issue_count = (
        counts["invalid_gate"]
        + counts["failed_or_timeout"]
        + counts["manager_override"]
        + counts["rework"]
    )
    if counts["invalid_gate"] or issue_count >= 2:
        return "redesign", reasons
    if issue_count:
        return "watch", reasons
    return "retain", []


def load_staff_management_context(home: Path, hours: int, now=None) -> dict:
    """Aggregate content-free manager events for private daily staff review."""
    sources = []
    unavailable = False
    for source_rank, relative_path in enumerate(STAFF_EVENT_PATHS):
        path = home / relative_path
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            unavailable = True
            continue
        raw = _read_regular_bytes(path, MAX_STAFF_EVENT_BYTES)
        if raw is not None:
            sources.append((source_rank, raw))
        else:
            unavailable = True
    if unavailable or not sources:
        return {
            "schema": STAFF_REFLECTION_SCHEMA,
            "status": "unavailable" if unavailable else "no_events",
            "window_hours": max(1, hours),
            "events_observed": 0,
            "alert": False,
            "profiles": [],
        }
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current.timestamp() - max(1, hours) * 3600
    required = {
        "schema",
        "event_id",
        "occurred_at",
        "task_id",
        "profile",
        "terminal_outcome",
        "manager_verdict",
        "manager_action",
        "gate_status",
        "delivery_permitted",
        "external_action_required",
        "raw_worker_overlap_detected",
        "error_codes",
        "input_sha256",
        "worker_artifact_sha256",
        "lifecycle_receipt_sha256",
        "validation_receipt_sha256",
    }
    events = {}
    lines = []
    for source_rank, raw in sources:
        lines.extend(
            (source_rank, line)
            for line in raw.decode("utf-8", errors="ignore").splitlines()[-MAX_STAFF_EVENTS:]
        )
    for source_rank, line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or set(event) != required:
            continue
        event_id = str(event.get("event_id") or "")
        profile = str(event.get("profile") or "")
        occurred_at = _parse_timestamp(event.get("occurred_at"))
        if (
            event.get("schema") != STAFF_EVENT_SCHEMA
            or STAFF_EVENT_ID_PATTERN.fullmatch(event_id) is None
            or STAFF_PROFILE_PATTERN.fullmatch(profile) is None
            or occurred_at is None
            or occurred_at.timestamp() < cutoff
            or occurred_at > current
            or not isinstance(event.get("delivery_permitted"), bool)
            or not isinstance(event.get("external_action_required"), bool)
            or not isinstance(event.get("raw_worker_overlap_detected"), bool)
            or not isinstance(event.get("error_codes"), list)
            or any(not isinstance(value, str) for value in event["error_codes"])
        ):
            continue
        previous = events.get(event_id)
        if (
            previous is None
            or occurred_at > previous[1]
            or (occurred_at == previous[1] and source_rank < previous[2])
        ):
            events[event_id] = (event, occurred_at, source_rank)
    selected_events = sorted(
        (row[0] for row in events.values()),
        key=lambda event: (_parse_timestamp(event["occurred_at"]), event["event_id"]),
        reverse=True,
    )[:MAX_STAFF_EVENTS]
    grouped = {}
    for event in selected_events:
        grouped.setdefault(event["profile"], []).append(event)
    profiles = []
    for profile, rows in sorted(grouped.items()):
        counts = Counter()
        for event in rows:
            verdict = event["manager_verdict"]
            action = event["manager_action"]
            outcome = event["terminal_outcome"]
            if verdict == "accepted":
                counts["accepted"] += 1
            elif verdict == "rework":
                counts["rework"] += 1
            elif verdict == "needs-human":
                counts["needs_human"] += 1
            elif verdict == "retired":
                counts["retired"] += 1
            if action == "manager_override":
                counts["manager_override"] += 1
            if outcome in {"failed", "timeout"}:
                counts["failed_or_timeout"] += 1
            if event["gate_status"] != "valid":
                counts["invalid_gate"] += 1
        recommendation, reasons = _staff_recommendation(counts)
        profiles.append(
            {
                "profile": profile,
                "runs": len(rows),
                "accepted": counts["accepted"],
                "rework": counts["rework"],
                "needs_human": counts["needs_human"],
                "retired": counts["retired"],
                "manager_overrides": counts["manager_override"],
                "failed_or_timeout": counts["failed_or_timeout"],
                "invalid_gates": counts["invalid_gate"],
                "recommendation": recommendation,
                "reason_codes": reasons,
                "event_ids": sorted(event["event_id"] for event in rows),
            }
        )
    return {
        "schema": STAFF_REFLECTION_SCHEMA,
        "status": "ok" if profiles else "no_events",
        "window_hours": max(1, hours),
        "events_observed": len(selected_events),
        "alert": any(
            row["recommendation"] in {"redesign", "retire"} for row in profiles
        ),
        "profiles": profiles,
    }


def redact_papercut_value(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact_papercut_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_papercut_value(item) for item in value]
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(redact_papercut_value(parsed), separators=(",", ":"))
    redacted = PRIVATE_KEY_BLOCK_PATTERN.sub("[REDACTED]", value)
    redacted = AUTH_CREDENTIAL_PATTERN.sub(r"\1 [REDACTED]", redacted)
    return SENSITIVE_FIELD_PATTERN.sub(r"\1[REDACTED]", redacted)


def build_prompt(deterministic, insights, agent_name, external_context=None, recent_skills=None):
    """Compact reflection prompt for the agent's own model."""
    d = deterministic
    scores = d.get("scores", {})
    esc = d.get("escalations", [])
    skill = d.get("skillify_candidates", [])
    leaks = d.get("verdict", {}).get("client_visible_harm")
    outcomes = d.get("outcomes", [])
    open_asks = [o for o in outcomes if o.get("status") in ("unanswered", "open", "promised", "in_progress")]

    summary = {
        "scores": scores,
        "verdict_level": d.get("verdict", {}).get("level"),
        "client_visible_harm": leaks,
        "escalations": [{"severity": e.get("severity"), "summary": e.get("summary")} for e in esc][:8],
        "deterministic_skillify": [s.get("summary") if isinstance(s, dict) else s for s in skill][:8],
        "open_or_promised_asks": [o.get("summary") for o in open_asks][:10],
    }
    if recent_skills:
        summary["recent_skill_usage"] = recent_skills[:MAX_RECENT_SKILLS]
    staff_context = d.get("staff_reflection") or {}
    if staff_context.get("events_observed"):
        summary["staff_management"] = staff_context
    papercuts = (d.get("self_reflection_meta") or {}).get("papercut_inbox") or {}
    if papercuts.get("pending_count"):
        summary["papercut_inbox"] = {
            "pending_count": papercuts.get("pending_count"),
            "event_ids": papercuts.get("event_ids", [])[:30],
            "patterns": [redact_papercut_value(pattern) for pattern in papercuts.get("patterns", [])[:12]],
            "events": [
                {
                    key: redact_papercut_value(event.get(key))
                    for key in ("id", "created_at", "kind", "operation", "route", "target", "summary", "evidence")
                    if event.get(key) not in (None, "")
                }
                for event in papercuts.get("events", [])[:30]
                if isinstance(event, dict)
            ],
        }
    external_block = ""
    if external_context:
        external_block = "Tool readiness context:\n" + json.dumps(external_context, indent=2)[:3000] + "\n\n"
    prompt = (
        f"You are {agent_name}. This is your private nightly self-reflection on the last 24h of "
        f"your own client work. A deterministic day-review already computed these signals:\n\n"
        f"{json.dumps(summary, indent=2)}\n\n"
        + external_block
        + (f"Usage insights:\n{insights.get('insights_1d', '')[:1500]}\n\n" if insights.get("insights_1d") else "")
        + "Reflect honestly and answer ONLY with a single JSON object (no prose, no code fence) with keys:\n"
        '  "did_it_suck": one of "no"|"somewhat"|"yes",\n'
        '  "narrative": 2-4 sentence honest self-assessment of how you served the client today,\n'
        '  "failures": [short strings: where you leaked, failed, or over/under-escalated],\n'
        '  "proposal_inputs": [{"pattern": str, "why": str, '
        '"draft_skill_name": str, "target_rung": "memory"|"skill", '
        '"skill_id": optional str, "skill_version": optional str, '
        '"skill_sha256": optional 64-char hex, "evidence_class": optional '
        '"repeated_failure"|"operator_correction"|"client_rework"|"repeated_success"}],\n'
        '  "open_promises": [{"summary": str, "owed_to_client": true|false}],\n'
        '  "operator_systems_lessons": [short strings: durable operational lessons from today],\n'
        '  "papercut_actions": [{"papercut_ids": [str], "lesson": str, "next_action": str, '
        '"disposition": "monitor"|"repair"|"skill_candidate"|"escalate"}],\n'
        '  "staff_actions": [{"profile": str, "decision": "retain"|"redesign"|"retire", '
        '"rationale": str, "evidence_event_ids": [str], "escalate_to_doc": true|false}],\n'
        '  "escalate_to_doc": true|false  (true only if a human operator should look).\n'
        "For proposal_inputs, prefer no proposal. Emit one only for repeated "
        "class-level evidence, not ordinary one-off sessions. "
        "Skill creation is proposal-first: target memory for single durable "
        "facts/preferences, skill only for repeated reusable procedures. "
        "When proposing a change to an existing skill, copy skill_id, skill_version, "
        "and skill_sha256 exactly from recent_skill_usage and select one evidence_class. "
        "A successful load proves use, not causality; connect it only to repeated day-review "
        "evidence. Never propose or perform a direct skill edit. "
        "For staff_actions, use only profiles and event IDs present in staff_management. "
        "Do not downgrade a deterministic redesign or retirement recommendation. "
        "A redesign changes the worker contract, prompt, tools, model, or validation before more work; "
        "retire means stop dispatching that profile pending operator review. "
        "Be specific and self-critical. If the day was clean, say so plainly."
    )
    return prompt


def bind_scheduled_model_provenance() -> str | None:
    """Fail closed when a scheduled model call lacks its accountable cron identity."""
    job_id = os.environ.get("HERMES_CRON_JOB_ID", "").strip()
    run_id = os.environ.get("HERMES_CRON_RUN_ID", "").strip()
    source = os.environ.get("HERMES_CRON_SCHEDULE_SOURCE", "").strip()
    configured = os.environ.get("HERMES_ONESHOT_SESSION_ID", "").strip()
    if not any((job_id, run_id, source, configured)):
        return None
    if source != "operator-control":
        raise RuntimeError("scheduled reflection model call requires Operator Control provenance")
    if not SCHEDULE_PROVENANCE_PART.fullmatch(job_id) or not SCHEDULE_PROVENANCE_PART.fullmatch(run_id):
        raise RuntimeError("scheduled reflection model call has invalid job/run provenance")
    expected = f"cron_{job_id}_{run_id}"
    if configured != expected:
        raise RuntimeError("scheduled reflection model call has mismatched session provenance")
    os.environ["HERMES_ONESHOT_SESSION_ID"] = expected
    return expected


def call_own_model(prompt):
    """Call the agent's own model via hermes -z. Returns (parsed_or_none, raw, model_hint)."""
    bind_scheduled_model_provenance()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(prompt)
        pf = tf.name
    try:
        # pass prompt via arg; hermes -z prints ONLY final response
        r = subprocess.run(HERMES_CLI + ["-z", prompt], capture_output=True, text=True, timeout=420)
        raw = (r.stdout or "").strip()
        if r.returncode != 0 and not raw:
            return None, (r.stderr or "").strip()[:400], None
        parsed = extract_json(raw)
        return parsed, raw, None
    finally:
        try:
            os.unlink(pf)
        except Exception:
            pass


def extract_json(text):
    if not text:
        return None
    # strip code fences
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    # find first balanced {...}
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start : i + 1])
                except Exception:
                    return None
    return None


def merge_report(deterministic, reflection_obj, raw, model_used, used_fallback, recent_skills=None):
    rep = dict(deterministic)
    rep["schema_version"] = max(2, int(rep.get("schema_version", 1)))
    # self_reflection block
    sr = {
        "model_used": model_used or "agent_default",
        "narrative": "",
        "did_it_suck": None,
        "failures": [],
        "escalate_to_doc": False,
        "raw_available": bool(raw),
        "fallback_no_model": used_fallback,
        "operator_systems_lessons": [],
        "papercut_actions": [],
        "staff_actions": [],
    }
    open_promises = []
    if isinstance(reflection_obj, dict):
        sr["narrative"] = str(reflection_obj.get("narrative", ""))[:1200]
        sr["did_it_suck"] = reflection_obj.get("did_it_suck")
        sr["failures"] = (
            reflection_obj.get("failures", [])[:12] if isinstance(reflection_obj.get("failures"), list) else []
        )
        sr["escalate_to_doc"] = bool(reflection_obj.get("escalate_to_doc"))
        lessons = reflection_obj.get("operator_systems_lessons", [])
        if isinstance(lessons, list):
            sr["operator_systems_lessons"] = [str(item)[:400] for item in lessons[:12]]
        papercut_actions = reflection_obj.get("papercut_actions", [])
        if isinstance(papercut_actions, list):
            sr["papercut_actions"] = [item for item in papercut_actions[:20] if isinstance(item, dict)]
        staff_context = rep.get("staff_reflection") or {}
        profile_events = {
            str(row.get("profile")): set(str(value) for value in row.get("event_ids") or [])
            for row in staff_context.get("profiles") or []
            if isinstance(row, dict) and row.get("profile")
        }
        staff_actions = reflection_obj.get("staff_actions", [])
        if isinstance(staff_actions, list):
            for item in staff_actions[:20]:
                if not isinstance(item, dict):
                    continue
                profile = str(item.get("profile") or "")
                decision = str(item.get("decision") or "")
                evidence_ids = item.get("evidence_event_ids")
                if (
                    profile not in profile_events
                    or decision not in {"retain", "redesign", "retire"}
                    or not isinstance(evidence_ids, list)
                ):
                    continue
                evidence_ids = [str(value) for value in evidence_ids]
                if not evidence_ids or not set(evidence_ids).issubset(profile_events[profile]):
                    continue
                sr["staff_actions"].append(
                    {
                        "profile": profile,
                        "decision": decision,
                        "rationale": str(item.get("rationale") or "")[:600],
                        "evidence_event_ids": sorted(set(evidence_ids)),
                        "escalate_to_doc": bool(item.get("escalate_to_doc")),
                    }
                )
        proposals = []
        model_proposals = reflection_obj.get("proposal_inputs", [])
        if isinstance(model_proposals, list):
            proposals.extend(("self_reflection", item) for item in model_proposals)
        legacy_skillify = reflection_obj.get("skillify_candidates", [])
        if isinstance(legacy_skillify, list):
            proposals.extend(("self_reflection_legacy_skillify", item) for item in legacy_skillify)
        if proposals:
            rep.setdefault("proposal_inputs", [])
            trusted_skills = {
                (item.get("skill_id"), item.get("skill_version"), item.get("skill_sha256")): item
                for item in (recent_skills or [])
                if isinstance(item, dict)
            }
            for source, item in proposals:
                proposal = item if isinstance(item, dict) else {"pattern": str(item)}
                proposal = {**proposal, "source": source}
                attribution_fields = ("skill_id", "skill_version", "skill_sha256")
                has_attribution = any(proposal.get(field) not in (None, "") for field in attribution_fields)
                if has_attribution:
                    key = tuple(proposal.get(field) for field in attribution_fields)
                    trusted = trusted_skills.get(key)
                    if trusted is None or proposal.get("evidence_class") not in SKILL_EVIDENCE_CLASSES:
                        continue
                    proposal.update(
                        {
                            "skill_id": trusted["skill_id"],
                            "skill_version": trusted["skill_version"],
                            "skill_sha256": trusted["skill_sha256"],
                            "skill_use_evidence": trusted["evidence"],
                        }
                    )
                proposal.setdefault("target_rung", "skill")
                proposal.setdefault("held_out_status", "not_run")
                rep["proposal_inputs"].append(proposal)
        else:
            rep.setdefault("proposal_inputs", [])
        for action in sr["papercut_actions"]:
            if action.get("disposition") != "skill_candidate" or not str(action.get("next_action") or "").strip():
                continue
            rep["proposal_inputs"].append(
                {
                    "source": "papercut_reflection",
                    "pattern": str(action.get("lesson") or action.get("next_action"))[:600],
                    "why": str(action.get("next_action"))[:600],
                    "papercut_ids": [str(value) for value in action.get("papercut_ids") or []][:30],
                    "target_rung": "skill",
                    "held_out_status": "not_run",
                }
            )
        op = reflection_obj.get("open_promises", [])
        if isinstance(op, list):
            open_promises = op[:20]
    rep["open_promises"] = open_promises
    staff_context = rep.get("staff_reflection")
    if isinstance(staff_context, dict):
        action_by_profile = {}
        rank = {"retain": 0, "watch": 1, "redesign": 2, "retire": 3}
        for action in sr["staff_actions"]:
            profile = action["profile"]
            previous = action_by_profile.get(profile)
            if previous is None or rank[action["decision"]] > rank[previous["decision"]]:
                action_by_profile[profile] = action
        for row in staff_context.get("profiles") or []:
            action = action_by_profile.get(row.get("profile"))
            model_decision = action.get("decision") if action else None
            row["manager_decision"] = model_decision
            row["effective_decision"] = max(
                (row.get("recommendation") or "retain", model_decision or "retain"),
                key=lambda value: rank[value],
            )
        staff_context["manager_actions"] = sr["staff_actions"]
        staff_context["alert"] = bool(staff_context.get("alert")) or any(
            row.get("effective_decision") in {"redesign", "retire"}
            for row in staff_context.get("profiles") or []
        )
        if staff_context["alert"] or any(
            action["escalate_to_doc"] for action in sr["staff_actions"]
        ):
            sr["escalate_to_doc"] = True
    rep["self_reflection"] = sr
    # let an honest self-escalation bump a green/yellow verdict note (not override red)
    if sr["escalate_to_doc"] and rep.get("verdict", {}).get("level") == "green":
        rep["verdict"]["self_escalated"] = True
    return rep


def reviewed_papercut_ids(reflection_obj: dict | None, pending_ids: list[str]) -> list[str]:
    if not isinstance(reflection_obj, dict):
        return []
    actions = reflection_obj.get("papercut_actions")
    if not isinstance(actions, list):
        return []
    referenced = set()
    valid_dispositions = {"monitor", "repair", "skill_candidate", "escalate"}
    for action in actions:
        if (
            not isinstance(action, dict)
            or not str(action.get("next_action") or "").strip()
            or action.get("disposition") not in valid_dispositions
        ):
            continue
        ids = action.get("papercut_ids")
        if isinstance(ids, list):
            referenced.update(str(event_id) for event_id in ids)
    return [event_id for event_id in pending_ids if event_id in referenced]


def unique_report_id(report_id: object) -> str:
    return f"{str(report_id or 'client-day-review')}-{uuid.uuid4().hex}"


def write_report_atomic(path: Path, report: dict) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description="Agent-led nightly self-reflection")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--home")
    ap.add_argument("--no-model", action="store_true", help="skip hermes -z; deterministic only")
    ap.add_argument("--print", action="store_true", help="print report to stdout too")
    ap.add_argument("--dry-run", action="store_true", help="do not write report file")
    args = ap.parse_args()

    home = Path(args.home).expanduser() if args.home else default_home()
    global HERMES_CLI
    HERMES_CLI = find_hermes_cli(home)
    py = find_python(home)
    cfg = read_config(home)

    # derive a stable agent_id from agent_name when config omits it
    if not cfg.get("agent_id"):
        nm = cfg.get("agent_name") or soul_name(home)
        if nm:
            cfg["agent_id"] = re.sub(r"[^a-z0-9]+", "", nm.split("'")[0].lower())

    # contract: hand-tuned override, else generic auto-derived
    hand = find_contract(home, cfg.get("agent_id", ""), cfg.get("client_id", ""))
    if hand:
        contract_path = hand
        contract_src = f"hand-tuned:{hand.name}"
    else:
        contract = derive_generic_contract(home, cfg)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            import yaml

            tmp.write(yaml.safe_dump(contract, sort_keys=False))
        except Exception:
            # minimal: day-review needs yaml; if unavailable, dump JSON (yaml superset)
            tmp.write(json.dumps(contract))
        tmp.close()
        contract_path = Path(tmp.name)
        contract_src = "auto-derived-generic"

    # 1. deterministic signals
    deterministic = run_day_review(find_day_review(home), contract_path, home, args.hours, py)
    deterministic.setdefault("self_reflection_meta", {})["contract_source"] = contract_src

    papercut_context = load_papercut_context(home)
    papercut_event_ids = papercut_context.get("event_ids", [])
    deterministic.setdefault("self_reflection_meta", {})["papercut_inbox"] = redact_papercut_value(papercut_context)

    external_context = load_external_context()
    if external_context:
        deterministic.setdefault("self_reflection_meta", {})["external_context"] = external_context

    skill_usage = recent_skill_usage(home, args.hours)
    deterministic.setdefault("self_reflection_meta", {})["recent_skill_usage"] = skill_usage

    deterministic["staff_reflection"] = load_staff_management_context(home, args.hours)

    # 2. own-model reflection
    reflection_obj, raw, used_fallback = None, "", True
    if not args.no_model:
        agent_name = deterministic.get("client", {}).get("agent_name") or cfg.get("agent_name") or "this agent"
        insights = gather_insights()
        prompt = build_prompt(deterministic, insights, agent_name, external_context, skill_usage)
        reflection_obj, raw, _ = call_own_model(prompt)
        used_fallback = reflection_obj is None

    # 3. merge + write
    report = merge_report(deterministic, reflection_obj, raw, None, used_fallback, skill_usage)
    report["report_id"] = unique_report_id(report.get("report_id"))

    out_dir = home / REPORTS_DIR
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{report['report_id']}.json"
        write_report_atomic(out_path, report)
        report.setdefault("coverage", {}).setdefault("sources_checked", []).append(str(out_path))
        reviewed_ids = reviewed_papercut_ids(reflection_obj, papercut_event_ids)
        if reviewed_ids and papercut_inbox is not None:
            try:
                ack = papercut_inbox.acknowledge(home, reviewed_ids, report_id=report["report_id"])
                report.setdefault("self_reflection_meta", {})["papercut_inbox_ack"] = ack
                write_report_atomic(out_path, report)
            except Exception as exc:
                report.setdefault("self_reflection_meta", {})["papercut_inbox_ack"] = {
                    "acknowledged": 0,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
                write_report_atomic(out_path, report)
        elif papercut_event_ids:
            report.setdefault("self_reflection_meta", {})["papercut_inbox_ack"] = {
                "acknowledged": 0,
                "reason": "reflection did not return an actionable disposition for a pending papercut",
            }
            write_report_atomic(out_path, report)

    if args.print or args.dry_run:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sr = report.get("self_reflection", {})
        print(
            json.dumps(
                {
                    "report_id": report["report_id"],
                    "verdict": report.get("verdict", {}).get("level"),
                    "did_it_suck": sr.get("did_it_suck"),
                    "escalate_to_doc": sr.get("escalate_to_doc"),
                    "fallback_no_model": sr.get("fallback_no_model"),
                    "contract_source": contract_src,
                    "written": None if args.dry_run else str(out_dir / f"{report['report_id']}.json"),
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
