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
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
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


IS_WIN = os.name == "nt"


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
    """Candidate venv python interpreters across known POSIX and Windows layouts."""
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
                    "independent_certifier": "client_acceptance_or_operator_verdict",
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


def build_prompt(deterministic, insights, agent_name, external_context=None):
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
        '"draft_skill_name": str, "target_rung": "memory"|"skill"}],\n'
        '  "open_promises": [{"summary": str, "owed_to_client": true|false}],\n'
        '  "operator_systems_lessons": [short strings: durable operational lessons from today],\n'
        '  "papercut_actions": [{"papercut_ids": [str], "lesson": str, "next_action": str, '
        '"disposition": "monitor"|"repair"|"skill_candidate"|"escalate"}],\n'
        '  "escalate_to_operator": true|false  (true only if a human operator should look).\n'
        "For proposal_inputs, prefer no proposal. Emit one only for repeated "
        "class-level evidence, not ordinary one-off sessions. "
        "Skill creation is proposal-first: target memory for single durable "
        "facts/preferences, skill only for repeated reusable procedures. "
        "Be specific and self-critical. If the day was clean, say so plainly."
    )
    return prompt


def call_own_model(prompt):
    """Call the agent's own model via hermes -z. Returns (parsed_or_none, raw, model_hint)."""
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


def merge_report(deterministic, reflection_obj, raw, model_used, used_fallback):
    rep = dict(deterministic)
    rep["schema_version"] = max(2, int(rep.get("schema_version", 1)))
    # self_reflection block
    sr = {
        "model_used": model_used or "agent_default",
        "narrative": "",
        "did_it_suck": None,
        "failures": [],
        "escalate_to_operator": False,
        "raw_available": bool(raw),
        "fallback_no_model": used_fallback,
        "operator_systems_lessons": [],
        "papercut_actions": [],
    }
    open_promises = []
    if isinstance(reflection_obj, dict):
        sr["narrative"] = str(reflection_obj.get("narrative", ""))[:1200]
        sr["did_it_suck"] = reflection_obj.get("did_it_suck")
        sr["failures"] = (
            reflection_obj.get("failures", [])[:12] if isinstance(reflection_obj.get("failures"), list) else []
        )
        sr["escalate_to_operator"] = bool(
            reflection_obj.get("escalate_to_operator")
        )
        lessons = reflection_obj.get("operator_systems_lessons", [])
        if isinstance(lessons, list):
            sr["operator_systems_lessons"] = [str(item)[:400] for item in lessons[:12]]
        papercut_actions = reflection_obj.get("papercut_actions", [])
        if isinstance(papercut_actions, list):
            sr["papercut_actions"] = [item for item in papercut_actions[:20] if isinstance(item, dict)]
        proposals = []
        model_proposals = reflection_obj.get("proposal_inputs", [])
        if isinstance(model_proposals, list):
            proposals.extend(("self_reflection", item) for item in model_proposals)
        legacy_skillify = reflection_obj.get("skillify_candidates", [])
        if isinstance(legacy_skillify, list):
            proposals.extend(("self_reflection_legacy_skillify", item) for item in legacy_skillify)
        if proposals:
            rep.setdefault("proposal_inputs", [])
            for source, item in proposals:
                proposal = item if isinstance(item, dict) else {"pattern": str(item)}
                proposal = {**proposal, "source": source}
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
    rep["self_reflection"] = sr
    # let an honest self-escalation bump a green/yellow verdict note (not override red)
    if sr["escalate_to_operator"] and rep.get("verdict", {}).get("level") == "green":
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

    # 2. own-model reflection
    reflection_obj, raw, used_fallback = None, "", True
    if not args.no_model:
        agent_name = deterministic.get("client", {}).get("agent_name") or cfg.get("agent_name") or "this agent"
        insights = gather_insights()
        prompt = build_prompt(deterministic, insights, agent_name, external_context)
        reflection_obj, raw, _ = call_own_model(prompt)
        used_fallback = reflection_obj is None

    # 3. merge + write
    report = merge_report(deterministic, reflection_obj, raw, None, used_fallback)
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
                    "escalate_to_operator": sr.get("escalate_to_operator"),
                    "fallback_no_model": sr.get("fallback_no_model"),
                    "contract_source": contract_src,
                    "written": None if args.dry_run else str(out_dir / f"{report['report_id']}.json"),
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
