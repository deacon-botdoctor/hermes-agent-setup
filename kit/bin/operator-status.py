#!/usr/bin/env python3
# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import pwd
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None


UTC = timezone.utc
OWNER_CHAT_ID = "0"
OWNER_SESSION_KEY = f"agent:main:telegram:dm:{OWNER_CHAT_ID}"
OWNER_DM_STALL_GRACE_SEC = 180
OWNER_DM_LOG_WINDOW_LINES = 1600

RUNBOOK_REGISTRY_CANDIDATES = (
    "workspace/ops/specs/hermes-runbook-registry.json",
    "workspace/shared-context/hermes-runbook-registry.json",
)


def iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def age_seconds(value: str | None) -> float | None:
    dt = parse_iso(value)
    if not dt:
        return None
    return round((datetime.now(UTC) - dt.astimezone(UTC)).total_seconds(), 1)


def compact_text(text: str, limit: int = 180) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def read_yaml(path: Path):
    if not path.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_runbook_registry(home: Path) -> dict:
    for rel in RUNBOOK_REGISTRY_CANDIDATES:
        path = home / rel
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if isinstance(data, dict):
            incidents = data.get("incidents") or {}
            if isinstance(incidents, dict):
                data["_path"] = str(path)
                return data
    return {"schema_version": 1, "incidents": {}, "_path": None}


def resolve_home(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg).expanduser().resolve()
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def resolve_lane_base(home: Path) -> Path:
    active_profile = home / "active_profile"
    if active_profile.exists():
        try:
            profile = active_profile.read_text(encoding="utf-8").strip()
            if profile:
                base = home / "profiles" / profile
                if base.exists():
                    return base
        except Exception:
            pass
    profiles_dir = home / "profiles"
    if profiles_dir.exists():
        for child in sorted(profiles_dir.iterdir()):
            if (child / "gateway_state.json").exists():
                return child
    return home


def remediation_summary(home: Path) -> dict:
    handoff_path = home / "state" / "handoff-status.json"
    legacy_path = home / "state" / "operator-remediation-state.json"
    info = {
        "exists": False,
        "path": str(handoff_path),
        "source": None,
        "decision": None,
        "attempted": False,
        "handoff_ok": None,
        "transport": None,
        "updated_at": None,
        "updated_age_seconds": None,
        "error": None,
    }
    data = read_json(handoff_path)
    source_path = handoff_path
    source = "handoff-status"
    if not isinstance(data, dict):
        data = read_json(legacy_path)
        source_path = legacy_path
        source = "operator-remediation-state"
    if not isinstance(data, dict):
        return info
    info["exists"] = True
    info["path"] = str(source_path)
    info["source"] = source
    info["decision"] = str(data.get("decision") or "") or None
    updated_at = str(data.get("updated_at") or data.get("last_sent_at") or "") or None
    info["updated_at"] = updated_at
    info["updated_age_seconds"] = age_seconds(updated_at)
    if source == "handoff-status":
        info["attempted"] = bool(data.get("attempted"))
        ok = data.get("ok")
        info["handoff_ok"] = bool(ok) if isinstance(ok, bool) else None
        info["transport"] = str(data.get("transport") or "") or None
        info["error"] = compact_text(str(data.get("error") or "")) or None
        return info
    handoff = data.get("handoff")
    info["attempted"] = isinstance(handoff, dict)
    if isinstance(handoff, dict):
        ok = handoff.get("ok")
        info["handoff_ok"] = bool(ok) if isinstance(ok, bool) else None
        info["transport"] = str(handoff.get("transport") or "") or None
        info["error"] = compact_text(str(handoff.get("error") or "")) or None
    return info


def transcript_summary(home: Path) -> dict:
    db_path = home / "data" / "telegram-transcript.db"
    info = {
        "exists": db_path.exists(),
        "path": str(db_path),
        "freshness": "checkpointed",
        "live_freshness": False,
        "rows": 0,
        "latest_timestamp": None,
        "recent_24h_rows": 0,
        "age_seconds": None,
    }
    session_meta = session_continuity_meta_summary(home)
    if not db_path.exists():
        info.update(session_meta)
        return info
    try:
        before = db_path.stat()
        # This report intentionally reads checkpointed historical data only;
        # it must not join or alter a live gateway WAL or claim live freshness.
        conn = sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro&immutable=1",
            uri=True,
            timeout=3,
        )
        info["rows"] = int(conn.execute("select count(*) from telegram_messages").fetchone()[0])
        latest = conn.execute("select max(timestamp) from telegram_messages").fetchone()[0]
        info["latest_timestamp"] = latest
        info["age_seconds"] = age_seconds(latest)
        cutoff = datetime.now(UTC).timestamp() - 86400
        info["recent_24h_rows"] = int(
            conn.execute(
                "select count(*) from telegram_messages where timestamp >= datetime(?, 'unixepoch')",
                (cutoff,),
            ).fetchone()[0]
        )
        info.update(transcript_friction_summary(conn, home))
        conn.close()
        after = db_path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            info = {"exists": True, "path": str(db_path), "freshness": "unavailable",
                    "live_freshness": False, "error": "checkpoint changed during read"}
    except Exception as exc:
        info["error"] = str(exc)
    transcript_hits = info.get("continuity_meta_reply_hits", []) or []
    session_hits = session_meta.get("continuity_meta_reply_hits", []) or []
    merged_hits = []
    seen = set()
    for hit in transcript_hits + session_hits:
        key = (str(hit.get("timestamp") or ""), str(hit.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged_hits.append(hit)
    info["continuity_meta_reply_hits"] = merged_hits[-6:]
    info["continuity_meta_reply_count"] = max(
        int(info.get("continuity_meta_reply_count", 0) or 0),
        int(session_meta.get("continuity_meta_reply_count", 0) or 0),
    )
    return info


def parse_log_timestamp(line: str) -> datetime | None:
    text = str(line or "").strip()
    if not text:
        return None
    candidates = []
    if len(text) >= 20:
        candidates.append(text[:20])
    if len(text) >= 23:
        candidates.append(text[:23])
    if len(text) >= 19:
        candidates.append(text[:19])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if candidate.endswith("Z"):
                return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except Exception:
            pass
        for fmt, tz in (("%Y-%m-%d %H:%M:%S,%f", UTC), ("%Y-%m-%d %H:%M:%S", UTC)):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=tz)
            except Exception:
                continue
    return None


def session_continuity_meta_summary(home: Path) -> dict:
    sessions_dir = home / "sessions"
    info = {
        "continuity_meta_reply_count": 0,
        "continuity_meta_reply_hits": [],
    }
    if not sessions_dir.exists():
        return info

    continuity_meta_patterns = [
        re.compile(
            r"same replay as before\.|nothing new\. last user input|thread is current\b|thread is clean\b|observed:\s*no unresolved work\b",
            re.IGNORECASE,
        ),
    ]
    recent_cutoff = datetime.now(UTC) - timedelta(minutes=90)
    hits = []
    try:
        candidates = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    except Exception:
        return info

    for path in candidates:
        try:
            path_dt = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except Exception:
            path_dt = None
        if path_dt and path_dt < recent_cutoff:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]
        except Exception:
            continue
        for raw_line in lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            line_dt = path_dt
            body = raw_line
            role = ""
            try:
                record = json.loads(raw_line)
            except Exception:
                record = None
            if isinstance(record, dict):
                role = str(record.get("role") or record.get("speaker") or record.get("type") or "").strip().lower()
                body = str(
                    record.get("content")
                    or record.get("text")
                    or record.get("message")
                    or record.get("output_text")
                    or raw_line
                )
                rec_ts = record.get("timestamp") or record.get("created_at") or record.get("ts")
                parsed = parse_iso(str(rec_ts)) if rec_ts else None
                if parsed is not None:
                    line_dt = parsed.astimezone(UTC)
            if role and role != "assistant":
                continue
            if line_dt and line_dt < recent_cutoff:
                continue
            if not any(pattern.search(body) for pattern in continuity_meta_patterns):
                continue
            hits.append(
                {
                    "timestamp": (
                        line_dt.isoformat().replace("+00:00", "Z") if isinstance(line_dt, datetime) else None
                    ),
                    "sender_name": "session-assistant",
                    "text": body[:280],
                    "source": "session",
                    "path": str(path),
                }
            )
    if hits:
        info["continuity_meta_reply_hits"] = hits[-6:]
        info["continuity_meta_reply_count"] = len(hits)
    return info


def parse_telegram_target(target: str | None) -> tuple[str | None, str | None]:
    if not target:
        return None, None
    parts = str(target).split(":")
    if len(parts) < 2 or parts[0] != "telegram":
        return None, None
    chat_id = f"telegram:{parts[1]}" if parts[1] else None
    thread_id = parts[2] if len(parts) >= 3 and parts[2] else None
    return chat_id, thread_id


def transcript_friction_summary(conn: sqlite3.Connection, home: Path) -> dict:
    recent_limit = 160
    home_channel = None
    auth = read_json(home / "auth.json") or {}
    if isinstance(auth, dict):
        home_channel = auth.get("telegram_home_channel")
    if not home_channel:
        env_path = home / ".env"
        if env_path.exists():
            try:
                for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() == "TELEGRAM_HOME_CHANNEL":
                        home_channel = value.strip().strip('"').strip("'")
                        break
            except Exception:
                pass
    home_chat_id, home_thread_id = parse_telegram_target(home_channel)
    query = """
        select timestamp, sender_name, text, coalesce(role, ''), coalesce(chat_id, ''), coalesce(thread_id, '')
        from telegram_messages
        where text is not null and trim(text) != ''
    """
    params: list[object] = []
    if home_chat_id:
        query += " and chat_id = ?"
        params.append(home_chat_id)
        if home_thread_id:
            query += " and coalesce(thread_id, '') = ?"
            params.append(home_thread_id)
    query += " order by timestamp desc limit ?"
    params.append(recent_limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    friction_patterns = [
        re.compile(r"\b(can you|could you|please|why|what happened|fix this|help)\b", re.IGNORECASE),
        re.compile(r"\b(timeout|not working|issue|problem|broken|failing|failed|error)\b", re.IGNORECASE),
    ]
    anger_patterns = [
        re.compile(r"\b(frustrat(?:ed|ing)|annoy(?:ed|ing)|upset|angry|ridiculous|unacceptable)\b", re.IGNORECASE),
        re.compile(r"\b(what the hell|wtf|come on|jesus|damn|bro)\b", re.IGNORECASE),
        re.compile(r"\b(still not|again|why is this|this sucks)\b", re.IGNORECASE),
    ]
    ignore_prefixes = (
        "[SYSTEM] This Telegram topic may require continuity context before answering.",
        "[SYSTEM] Gateway restarted mid-task.",
    )
    continuity_meta_patterns = [
        re.compile(
            r"^(same replay as before\.|nothing new\. last user input\b|thread is clean\b|observed:\s*no unresolved work\b)",
            re.IGNORECASE,
        ),
    ]
    recent_cutoff = datetime.now(UTC) - timedelta(minutes=20)
    hits: list[dict] = []
    friction_score = 0
    anger_score = 0
    duplicate_hits: list[dict] = []
    duplicate_streak_max = 0
    continuity_meta_hits: list[dict] = []
    continuity_meta_count = 0
    last_norm = ""
    last_role = ""
    last_sender = ""
    streak = 0
    latest_frustration_at = None
    for timestamp, sender_name, text, role in rows:
        body = str(text or "").strip()
        if not body:
            continue
        sender = str(sender_name or "")
        role = str(role or "")
        norm = re.sub(r"\s+", " ", body)
        hit_dt = parse_iso(str(timestamp))
        if role == "assistant":
            if norm == last_norm and sender == last_sender and last_role == role and len(norm) >= 40:
                streak += 1
            else:
                streak = 1
            if streak >= 3:
                duplicate_streak_max = max(duplicate_streak_max, streak)
                duplicate_hits.append(
                    {
                        "timestamp": timestamp,
                        "sender_name": sender,
                        "text": body[:280],
                        "streak": streak,
                    }
                )
            if any(pattern.search(body) for pattern in continuity_meta_patterns):
                if hit_dt is None or hit_dt.astimezone(UTC) >= recent_cutoff:
                    continuity_meta_count += 1
                    continuity_meta_hits.append(
                        {
                            "timestamp": timestamp,
                            "sender_name": sender,
                            "text": body[:280],
                        }
                    )
        else:
            streak = 0
        last_norm = norm
        last_role = role
        last_sender = sender

        if body.startswith(ignore_prefixes):
            continue
        if role == "assistant":
            continue
        if not sender.strip():
            continue

        matched: list[str] = []
        if any(pattern.search(body) for pattern in friction_patterns):
            matched.append("friction")
            if hit_dt is None or hit_dt.astimezone(UTC) >= recent_cutoff:
                friction_score += 1
        anger_matched = sum(1 for pattern in anger_patterns if pattern.search(body))
        if anger_matched:
            matched.append("anger")
            if hit_dt is None or hit_dt.astimezone(UTC) >= recent_cutoff:
                friction_score += anger_matched
                anger_score += anger_matched
        if matched:
            latest_frustration_at = timestamp
            hits.append(
                {
                    "timestamp": timestamp,
                    "sender_name": sender,
                    "text": body[:280],
                    "signals": matched,
                }
            )
    hits.reverse()
    friction_index = min(friction_score, 10)
    anger_index = min(anger_score, 10)
    return {
        "friction_index": friction_index,
        "anger_index": anger_index,
        "frustration_hits": hits[-8:],
        "latest_frustration_at": latest_frustration_at,
        "duplicate_assistant_streak_max": duplicate_streak_max,
        "duplicate_assistant_hits": duplicate_hits[-6:],
        "continuity_meta_reply_count": continuity_meta_count,
        "continuity_meta_reply_hits": continuity_meta_hits[-6:],
    }


def auth_summary(home: Path, fallback_home: Path | None = None) -> dict:
    auth = read_json(home / "auth.json") or {}
    recognized = auth.get("recognized_users") or []
    if not isinstance(recognized, list):
        recognized = [recognized]
    providers = auth.get("providers") or {}
    if not isinstance(providers, dict):
        providers = {}
    credential_pool = auth.get("credential_pool") or {}
    if not isinstance(credential_pool, dict):
        credential_pool = {}
    credential_pool_entries = 0
    for value in credential_pool.values():
        if isinstance(value, list):
            credential_pool_entries += len(value)
        elif value:
            credential_pool_entries += 1
    env_home_channel = None
    env_paths = [home / ".env"]
    if fallback_home is not None and fallback_home != home:
        env_paths.append(fallback_home / ".env")
    for env_path in env_paths:
        if not env_path.exists():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "TELEGRAM_HOME_CHANNEL":
                    env_home_channel = value.strip().strip('"').strip("'")
                    break
        except Exception:
            pass
        if env_home_channel:
            break
    return {
        "exists": bool(auth),
        "recognized_users": len(recognized),
        "operator_user": auth.get("operator_user"),
        "telegram_user": auth.get("telegram_user"),
        "telegram_home_channel": auth.get("telegram_home_channel") or env_home_channel,
        "env_telegram_home_channel": env_home_channel,
        "provider_count": len(providers),
        "credential_pool_entries": credential_pool_entries,
    }


def _fresh_canary_proof(home: Path, job_id: str | None) -> bool:
    ident = str(job_id or "").strip().lower()
    if ident != "canary-codex-main":
        return False
    log_path = home / "logs" / "com.hermes.cron.canary-main-shell-proof.log"
    if not log_path.exists():
        return False
    try:
        lines = [
            line.strip() for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()
        ]
    except Exception:
        return False
    created_at = None
    for line in reversed(lines[-40:]):
        if '"created_at"' not in line:
            continue
        _, _, tail = line.partition(":")
        created_at = tail.strip().strip(",").strip().strip('"')
        if created_at:
            break
    if not created_at:
        return False
    age = age_seconds(created_at)
    return age is not None and age <= 4 * 3600


def cron_summary(home: Path, lane_base: Path) -> dict:
    jobs = read_json(lane_base / "cron" / "jobs.json") or read_json(home / "cron" / "jobs.json") or {}
    rows = jobs.get("jobs") if isinstance(jobs, dict) else []
    if not isinstance(rows, list):
        rows = []
    delivery_jobs = []
    delivery_failures = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        deliver = str(row.get("deliver") or "")
        if not deliver.startswith("telegram:"):
            continue
        delivery_jobs.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "last_status": row.get("last_status"),
                "last_error": row.get("last_error") or row.get("last_delivery_error"),
                "last_run_at": row.get("last_run_at"),
                "next_run_at": row.get("next_run_at"),
            }
        )
        if row.get("last_delivery_error") or (row.get("last_status") and row.get("last_status") != "ok"):
            delivery_failures.append(row.get("id") or row.get("name") or "unknown-job")
    stale_jobs = []
    slow_jobs = []
    now = datetime.now(UTC)
    for row in rows:
        if not isinstance(row, dict):
            continue
        enabled = bool(row.get("enabled", True))
        state = str(row.get("state") or "").strip().lower()
        if not enabled or state in {"paused", "disabled"}:
            continue
        last_run = parse_iso(row.get("last_run_at"))
        next_run = parse_iso(row.get("next_run_at"))
        if last_run and next_run:
            gap = (next_run - last_run).total_seconds()
            if gap > 0 and gap <= 7200:
                overdue = (now - next_run.astimezone(UTC)).total_seconds()
                if overdue > max(900, gap * 2):
                    job_id = row.get("id") or row.get("name") or "unknown-job"
                    if not _fresh_canary_proof(home, job_id):
                        stale_jobs.append(job_id)
        runtime_ms = row.get("last_duration_ms") or row.get("duration_ms")
        try:
            runtime_ms = float(runtime_ms) if runtime_ms is not None else None
        except Exception:
            runtime_ms = None
        if runtime_ms and runtime_ms >= 30000:
            slow_jobs.append(
                {
                    "id": row.get("id") or row.get("name") or "unknown-job",
                    "duration_ms": runtime_ms,
                }
            )
    return {
        "jobs_path": str(
            (lane_base / "cron" / "jobs.json")
            if (lane_base / "cron" / "jobs.json").exists()
            else (home / "cron" / "jobs.json")
        ),
        "delivery_jobs_total": len(delivery_jobs),
        "delivery_failures": delivery_failures,
        "stale_jobs": stale_jobs,
        "slow_jobs": slow_jobs,
    }


def gateway_summary(lane_base: Path) -> dict:
    state = read_json(lane_base / "gateway_state.json") or {}
    platforms = state.get("platforms") if isinstance(state, dict) else {}
    telegram = platforms.get("telegram") if isinstance(platforms, dict) else {}
    last_successful_poll_at = telegram.get("last_successful_poll_at") if isinstance(telegram, dict) else None
    return {
        "path": str(lane_base / "gateway_state.json"),
        "exists": bool(state),
        "gateway_state": state.get("gateway_state"),
        "updated_at": state.get("updated_at"),
        "updated_age_seconds": age_seconds(state.get("updated_at")),
        "telegram_state": telegram.get("state") if isinstance(telegram, dict) else None,
        "telegram_updated_at": telegram.get("updated_at") if isinstance(telegram, dict) else None,
        "telegram_updated_age_seconds": age_seconds(telegram.get("updated_at") if isinstance(telegram, dict) else None),
        "telegram_last_successful_poll_at": last_successful_poll_at,
        "telegram_poll_age_seconds": age_seconds(last_successful_poll_at),
        "pid": state.get("pid"),
        "active_agents": state.get("active_agents"),
        "latency_ms": state.get("latency_ms") or telegram.get("latency_ms") if isinstance(telegram, dict) else None,
        "provider_failures": state.get("provider_failures") or [],
        "provider_retries": state.get("provider_retries") or [],
    }


def _blessed_gateway_wrapper(home: Path) -> str | None:
    plist_path = home.parent / "Library" / "LaunchAgents" / "ai.hermes.gateway.plist"
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except Exception:
        return None
    arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
    if not isinstance(arguments, list) or not arguments:
        return None
    program = Path(str(arguments[0])).expanduser()
    try:
        program.relative_to(home / "bin")
    except ValueError:
        return None
    if re.fullmatch(r"(?:start-hermes(?:-golden-[0-9a-f]+)?|hermes-[\w.-]+)\.sh", program.name):
        return str(program)
    return None


def gateway_service_runtime_summary(home: Path) -> dict:
    info = {
        "checked": False,
        "service_definition_stale": False,
        "service_not_loaded": False,
        "manual_process_only": False,
        "blessed_wrapper": None,
        "detail": None,
    }
    hermes_bin = home / "hermes-agent" / "venv" / "bin" / "hermes"
    if not hermes_bin.exists():
        info["detail"] = f"missing_hermes_cli:{hermes_bin}"
        return info
    try:
        proc = subprocess.run(
            [str(hermes_bin), "gateway", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception as exc:
        info["detail"] = f"gateway_status_failed:{exc}"
        return info
    text = "\n".join(part for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if part)
    info["checked"] = True
    info["service_definition_stale"] = "Service definition is stale relative to the current Hermes install" in text
    # Blessed-wrapper exception (agent-standards §24): when the loaded plist runs a
    # host-preserved launch wrapper,
    # `hermes gateway status` flags definition-stale against the stock command form,
    # but the wrapper IS the desired state (preserves keychain-loaded auth). Suppress.
    blessed_wrapper = _blessed_gateway_wrapper(home)
    if info["service_definition_stale"] and (
        blessed_wrapper or re.search(r"\.hermes/(start-hermes\.sh|bin/hermes-[\w.-]+\.sh)", text)
    ):
        info["service_definition_stale"] = False
        info["blessed_wrapper"] = blessed_wrapper or "status-reported-wrapper"
    info["service_not_loaded"] = "Gateway service is not loaded" in text
    info["manual_process_only"] = "Gateway process is running for this profile, but the service is not active" in text
    detail_bits = []
    if info["service_definition_stale"]:
        detail_bits.append("definition_stale")
    if info["service_not_loaded"]:
        detail_bits.append("service_not_loaded")
    if info["manual_process_only"]:
        detail_bits.append("manual_process_only")
    if info["blessed_wrapper"]:
        detail_bits.append(f"blessed_wrapper:{info['blessed_wrapper']}")
    if not detail_bits and text:
        detail_bits.append(compact_text(text, 220))
    info["detail"] = "; ".join(detail_bits) if detail_bits else None
    return info


def owner_dm_flow_summary(home: Path, lane_base: Path) -> dict:
    info = {
        "session_key": OWNER_SESSION_KEY,
        "session_updated_at": None,
        "session_updated_age_seconds": None,
        "latest_inbound_at": None,
        "latest_inbound_age_seconds": None,
        "latest_inbound_snippet": None,
        "lag_seconds": None,
        "pending_owner_dm": False,
    }
    sessions_path = home / "sessions" / "sessions.json"
    session_updated = None
    if sessions_path.exists():
        data = read_json(sessions_path) or {}
        if isinstance(data, dict):
            meta = data.get(OWNER_SESSION_KEY) or {}
            if isinstance(meta, dict):
                info["session_updated_at"] = str(meta.get("updated_at") or "") or None
                session_updated = parse_iso(info["session_updated_at"])
                if session_updated is not None:
                    info["session_updated_age_seconds"] = age_seconds(info["session_updated_at"])

    log_path = lane_base / "logs" / "gateway.log"
    latest_inbound = None
    if log_path.exists():
        pattern = re.compile(rf"inbound message: platform=telegram .* chat={re.escape(OWNER_CHAT_ID)} .*msg='(.*)'")
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-OWNER_DM_LOG_WINDOW_LINES:]
        except Exception:
            lines = []
        for line in reversed(lines):
            ts = parse_log_timestamp(line)
            if ts is None:
                continue
            match = pattern.search(line)
            if not match:
                continue
            latest_inbound = ts
            info["latest_inbound_at"] = ts.astimezone(UTC).isoformat().replace("+00:00", "Z")
            info["latest_inbound_age_seconds"] = round((datetime.now(UTC) - ts.astimezone(UTC)).total_seconds(), 1)
            info["latest_inbound_snippet"] = match.group(1)[:160]
            break
    if latest_inbound is not None:
        if session_updated is None:
            info["pending_owner_dm"] = True
        else:
            lag = round((latest_inbound.astimezone(UTC) - session_updated.astimezone(UTC)).total_seconds(), 1)
            info["lag_seconds"] = lag
            if lag >= OWNER_DM_STALL_GRACE_SEC:
                info["pending_owner_dm"] = True
    return info


def selfheal_summary(home: Path) -> dict:
    data = read_json(home / "state" / "client-selfheal-heartbeat-state.json") or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "exists": bool(data),
        "path": str(home / "state" / "client-selfheal-heartbeat-state.json"),
        "healthy": data.get("healthy"),
        "updated_at": data.get("updated_at"),
        "updated_age_seconds": age_seconds(data.get("updated_at")),
        "actions_total": len(data.get("actions") or []),
    }


def windows_runtime_summary(home: Path) -> dict:
    data = read_json(home / "state" / "windows-runtime-summary.json") or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "exists": bool(data),
        "path": str(home / "state" / "windows-runtime-summary.json"),
        "health_status": data.get("health_status"),
        "generated_at": data.get("generated_at"),
        "generated_age_seconds": age_seconds(data.get("generated_at")),
    }


def operator_alerts_summary(home: Path) -> dict:
    config = read_yaml(home / "config.yaml")
    alerts = config.get("operator_alerts") if isinstance(config, dict) else {}
    if not isinstance(alerts, dict):
        alerts = {}
    return {
        "configured": bool(alerts),
        "chat_id": alerts.get("chat_id"),
        "thread_id": alerts.get("thread_id"),
        "owner": alerts.get("owner"),
        "mode": alerts.get("mode"),
    }


def memory_summary(home: Path) -> dict:
    memory = home / "memories" / "MEMORY.md"
    return {
        "exists": memory.exists(),
        "path": str(memory),
        "has_botdoctor_updates": (
            "## Bot Doctor Capability Updates" in memory.read_text(encoding="utf-8", errors="ignore")
        )
        if memory.exists()
        else False,
    }


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def soul_identity(home: Path) -> dict:
    path = home / "SOUL.md"
    info = {"client_identity": "", "client_label": "", "agent_name": ""}
    if not path.exists():
        return info
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^##\s+([A-Z][A-Z0-9' -]+)\s+PROFILE\b", text, re.MULTILINE)
    if m:
        label = m.group(1).title().strip()
        info["client_label"] = label
        info["client_identity"] = slugify(label)
    m = re.search(r"^You are\s+\*\*?([A-Za-z0-9' ._-]+)\*\*?", text, re.MULTILINE)
    if m:
        info["agent_name"] = m.group(1).strip()
    return info


def log_paths(home: Path, lane_base: Path) -> list[str]:
    candidates = [
        lane_base / "logs" / "gateway.log",
        lane_base / "logs" / "gateway.err.log",
        lane_base / "logs" / "node.err.log",
        home / "logs" / "gateway.log",
        home / "logs" / "gateway.err.log",
        home / "logs" / "node.err.log",
        home / "logs" / "client-selfheal-heartbeat.log",
        home / "logs" / "windows-runtime-summary.log",
    ]
    seen: set[str] = set()
    rows: list[str] = []
    for path in candidates:
        text = str(path)
        if text in seen:
            continue
        seen.add(text)
        rows.append(text)
    return rows


def scan_log_signatures(paths: list[str], lines: int = 240, recent_window_seconds: int = 2 * 3600) -> list[dict]:
    patterns = [
        (
            "http_401",
            re.compile(
                r"(?:http\s*401|error code:\s*401|status\s*401|\b401\b[^\n]{0,40}(?:unauthorized|authentication|invalid x-api-key|missing authentication header))",
                re.IGNORECASE,
            ),
        ),
        ("telegram_bad_request", re.compile(r"bad request", re.IGNORECASE)),
        (
            "telegram_polling_conflict",
            re.compile(
                r"terminated by other getupdates request|only one bot instance|telegram polling conflict", re.IGNORECASE
            ),
        ),
        ("home_channel_prompt", re.compile(r"no home channel is set for telegram|/sethome", re.IGNORECASE)),
        ("permission_denied", re.compile(r"permission denied|publickey|not authorized", re.IGNORECASE)),
        (
            "provider_timeout",
            re.compile(
                r"provider timeout|provider .*timed out|timed out.*provider|timeout while.*provider|request timed out.*provider|deadline exceeded.*provider",
                re.IGNORECASE,
            ),
        ),
        (
            "provider_request_invalid",
            re.compile(r"error code:\s*400|no models provided|invalid request", re.IGNORECASE),
        ),
        (
            "channel_prompt_missing",
            re.compile(r"AttributeError: .*channel_prompt|has no attribute 'channel_prompt'", re.IGNORECASE),
        ),
        (
            "session_resume_event_shape_mismatch",
            re.compile(
                r"AttributeError: .*MessageEvent|has no attribute 'channel_prompt'|has no attribute '[A-Za-z_]+'.*MessageEvent",
                re.IGNORECASE,
            ),
        ),
        (
            "hook_api_mismatch",
            re.compile(r"hook.*(AttributeError|TypeError)|Loaded hook .* but .* failed", re.IGNORECASE),
        ),
        ("clarify_error", re.compile(r"clarify .*?\[error\]", re.IGNORECASE)),
        ("context_compaction", re.compile(r"compacting context", re.IGNORECASE)),
        (
            "session_reset_noise",
            re.compile(r"gateway restarted mid-task|continue the interrupted work|session-resume", re.IGNORECASE),
        ),
        (
            "selfheal_restart_loop",
            re.compile(
                r"client-selfheal-heartbeat.*unhealthy; actions=.*started-task|restart cooldown active|restarted via task",
                re.IGNORECASE,
            ),
        ),
        (
            "limitation_language",
            re.compile(
                r"\b(i can[' ]?t do that|i wasn[' ]?t able to|i do not know|i don't know|systems? are limiting me|my systems? are limiting me)\b",
                re.IGNORECASE,
            ),
        ),
    ]
    recent_only = {
        "http_401",
        "clarify_error",
        "context_compaction",
        "session_reset_noise",
        "selfheal_restart_loop",
        "limitation_language",
        "provider_request_invalid",
        "provider_timeout",
    }
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    cutoff = datetime.now(UTC) - timedelta(seconds=recent_window_seconds)
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-lines:]
        except Exception:
            continue
        text = "\n".join(raw_lines)
        effective_text = text
        loaded_hook_names = set(re.findall(r"Loaded hook '([^']+)' for events:", effective_text))
        if loaded_hook_names:
            for hook_name in loaded_hook_names:
                effective_text = effective_text.replace(f"Skipping {hook_name}: no events declared", "")
        line_times: list[datetime | None] = []
        last_ts: datetime | None = None
        for line in raw_lines:
            parsed = parse_log_timestamp(line)
            if parsed is not None:
                last_ts = parsed.astimezone(UTC)
            line_times.append(last_ts)
        last_connected_ts = None
        last_gateway_start_idx = 0
        for idx, line in enumerate(raw_lines):
            if "Connected to Telegram (polling mode)" in line:
                last_connected_ts = line_times[idx]
            if "Starting Hermes Gateway..." in line or "Hermes Gateway Starting..." in line:
                last_gateway_start_idx = idx
        for code, pattern in patterns:
            excerpt = None
            hit_count = 0
            if code in recent_only or code == "telegram_polling_conflict":
                last_match_ts = None
                for idx, line in enumerate(raw_lines):
                    if not pattern.search(line):
                        continue
                    effective_ts = line_times[idx]
                    if effective_ts is None or effective_ts < cutoff:
                        continue
                    hit_count += 1
                    last_match_ts = effective_ts
                    if excerpt is None:
                        excerpt = line.strip()[:320]
                if (
                    code == "telegram_polling_conflict"
                    and hit_count > 0
                    and last_match_ts is not None
                    and last_connected_ts is not None
                    and last_connected_ts > last_match_ts
                ):
                    excerpt = None
                    hit_count = 0
            else:
                search_text = effective_text if code == "hook_api_mismatch" else text
                if code == "http_401" and last_gateway_start_idx > 0:
                    search_text = "\n".join(raw_lines[last_gateway_start_idx:])
                matches = list(pattern.finditer(search_text))
                if matches:
                    hit_count = len(matches)
                    match = matches[0]
                    excerpt = search_text[max(0, match.start() - 160) : match.end() + 160].strip()
            if hit_count <= 0 or excerpt is None:
                continue
            key = (code, raw_path)
            if key in seen:
                continue
            seen.add(key)
            findings.append({"code": code, "path": raw_path, "excerpt": excerpt, "hit_count": hit_count})
    return findings


def fence_orphan_summary(min_age_seconds: int = 3600) -> dict:
    """Detect orphan Hermes fence bash wrappers older than `min_age_seconds`.

    Returns {"count": int, "orphans": [{"pid": int, "age_seconds": int, "cmd": str}, ...]}.
    Never raises — on any error, returns {"count": 0, "orphans": [], "error": "..."}.
    """
    info: dict = {"count": 0, "orphans": []}
    try:
        p = subprocess.run(
            ["pgrep", "-af", "bash.*__HERMES_FENCE__"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        info["error"] = f"pgrep failed: {exc}"
        return info
    if p.returncode not in (0, 1):
        info["error"] = f"pgrep rc={p.returncode}: {p.stderr[:120]}"
        return info
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        # Age via ps etime (portable). Fall back on failure.
        age_seconds = None
        try:
            pr = subprocess.run(
                ["ps", "-p", str(pid), "-o", "etimes="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if pr.returncode == 0:
                val = (pr.stdout or "").strip()
                if val:
                    age_seconds = int(val)
        except Exception:
            pass
        if age_seconds is None or age_seconds < min_age_seconds:
            continue
        info["orphans"].append({"pid": pid, "age_seconds": age_seconds, "cmd": cmd[:200]})
    info["count"] = len(info["orphans"])
    return info


def _load_cron_jobs(home: Path) -> list[dict]:
    jobs_path = home / "cron" / "jobs.json"
    if not jobs_path.exists():
        return []
    try:
        payload = json.loads(jobs_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    if isinstance(payload, dict):
        jobs = payload.get("jobs")
        return jobs if isinstance(jobs, list) else []
    return payload if isinstance(payload, list) else []


def _has_dream_job(home: Path) -> tuple[bool | None, str | None]:
    fallback_job_id: str | None = None
    for job in _load_cron_jobs(home):
        if not isinstance(job, dict):
            continue
        ident = str(job.get("id") or "").strip().lower()
        name = str(job.get("name") or "").strip().lower()
        prompt = str(job.get("prompt") or "").strip().lower()
        shell_command = str(job.get("shell_command") or "").strip().lower()
        enabled = bool(job.get("enabled", True))
        is_dream_sweep = (
            "daily dream sweep" in name
            or "daily dream sweep" in prompt
            or "daily-dream-sweep" in ident
            or "session extraction plus consolidation" in prompt
            or "/dream.py" in prompt
            or "/dream.py" in shell_command
            or "/dream_consolidate.py" in prompt
            or "/dream_consolidate.py" in shell_command
            or "consolidated" in prompt
            or "consolidated" in shell_command
        )
        if not is_dream_sweep:
            continue
        job_id = str(job.get("id") or job.get("name") or "dream")
        if enabled:
            return True, job_id
        fallback_job_id = fallback_job_id or job_id
    if fallback_job_id is not None:
        return False, fallback_job_id
    return None, None


def _has_launchd_dream_job(home: Path) -> tuple[bool, str | None]:
    """Check the runtime owner's launchd domains for known dream services."""
    uid = home.stat().st_uid
    labels = (
        "com.hermes.nightly-dream-cycle",
        "com.hermes.cron.daily-dream-sweep",
        "ai.hermes.nightly-dream-cycle",
    )
    for domain in ("gui", "user"):
        for label in labels:
            pr = subprocess.run(
                ["launchctl", "print", f"{domain}/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=6,
            )
            if pr.returncode == 0:
                return True, label
    return False, None


def dream_agent_summary(home: Path) -> dict:
    """Detect broken dream consolidation.

    Checks:
      - Hermes cron/jobs.json for an enabled dream job (preferred), with launchctl/systemd as legacy fallback.
      - ~/.hermes/state/consolidated.txt mtime > 36h.
      - tail of ~/.hermes/logs/dream.log for ModuleNotFoundError, Traceback, or
        3+ consecutive zero-output completion lines.

    Returns a dict with keys:
      - scheduled (bool | None) — None if detection failed
      - scheduler_source (str | None) — hermes_cron, launchctl, systemd, or probe_failed
      - dream_job_id (str | None)
      - consolidated_path, consolidated_exists, consolidated_age_seconds
      - log_path, log_exists, log_signals (list[str]), log_zero_streak (int)
      - broken (bool), reasons (list[str])
    """
    info: dict = {
        "scheduled": None,
        "scheduler_source": None,
        "dream_job_id": None,
        "consolidated_path": str(home / "state" / "consolidated.txt"),
        "consolidated_exists": False,
        "consolidated_age_seconds": None,
        "log_path": str(home / "logs" / "dream.log"),
        "log_exists": False,
        "log_signals": [],
        "log_zero_streak": 0,
        "broken": False,
        "reasons": [],
        "policy": "required",
    }

    cron_enabled, dream_job_id = _has_dream_job(home)
    if cron_enabled is not None:
        info["scheduled"] = cron_enabled
        info["scheduler_source"] = "hermes_cron"
        info["dream_job_id"] = dream_job_id
        if cron_enabled is False and dream_job_id is not None:
            info["policy"] = "intentionally_disabled"
            return info
    else:
        system = platform.system().lower()
        try:
            if system == "darwin":
                info["scheduled"], info["dream_job_id"] = _has_launchd_dream_job(home)
                info["scheduler_source"] = "launchctl"
            else:
                cron_cmd = ["crontab", "-l"]
                if os.geteuid() == 0:
                    owner = pwd.getpwuid(home.stat().st_uid).pw_name
                    cron_cmd = ["crontab", "-u", owner, "-l"]
                cron_pr = subprocess.run(cron_cmd, capture_output=True, text=True, timeout=6)
                if cron_pr.returncode == 0 and "dream" in (cron_pr.stdout or "").lower():
                    info["scheduled"] = True
                    info["scheduler_source"] = "os_cron"
                else:
                    pr = subprocess.run(
                        ["systemctl", "--user", "list-timers", "--all", "--no-legend", "--no-pager"],
                        capture_output=True,
                        text=True,
                        timeout=6,
                    )
                    info["scheduled"] = "dream" in (pr.stdout or "").lower()
                    info["scheduler_source"] = "systemd"
        except Exception as exc:
            info["scheduled"] = None
            info["scheduler_source"] = "probe_failed"
            info["reasons"].append(f"scheduler_probe_failed:{exc}")

    if info["scheduled"] is False:
        info["reasons"].append("no_dream_scheduler_entry")

    consolidated = home / "state" / "consolidated.txt"
    if consolidated.exists():
        info["consolidated_exists"] = True
        try:
            age = (datetime.now(UTC) - datetime.fromtimestamp(consolidated.stat().st_mtime, UTC)).total_seconds()
            info["consolidated_age_seconds"] = round(age, 1)
            if age > 36 * 3600:
                info["reasons"].append(f"consolidated_stale:{int(age)}s")
        except Exception as exc:
            info["reasons"].append(f"consolidated_stat_failed:{exc}")
    else:
        info["reasons"].append("consolidated_missing")

    log_path = home / "logs" / "dream.log"
    if log_path.exists():
        info["log_exists"] = True
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]
        except Exception as exc:
            info["reasons"].append(f"dream_log_read_failed:{exc}")
            lines = []
        signals: list[str] = []
        # Only evaluate the most recent dream cycle. Older traceback text can
        # remain inside the 400-line tail after a later successful run and
        # should not keep the operator surface degraded.
        latest_start = None
        for idx, line in enumerate(lines):
            if "[dream] Dream cycle starting" in line:
                latest_start = idx
        recent_lines = lines[latest_start:] if latest_start is not None else lines
        tail_text = "\n".join(recent_lines)
        if "ModuleNotFoundError" in tail_text:
            signals.append("module_not_found")
        if "Traceback (most recent call last)" in tail_text:
            signals.append("traceback")
        streak = 0
        zero_re = re.compile(
            r"(?:wrote\s+0\s+memory\s*\+\s*0\s+user|dream cycle complete:\s*\+0\s+memory,\s*\+0\s+user)",
            re.IGNORECASE,
        )
        for line in reversed(lines):
            if zero_re.search(line):
                streak += 1
            elif line.strip():
                break
        info["log_zero_streak"] = streak
        if streak >= 3:
            signals.append(f"zero_output_streak:{streak}")
        info["log_signals"] = signals
        for signal in signals:
            info["reasons"].append(f"log:{signal}")

    info["broken"] = bool(info["reasons"]) and info["scheduled"] is not None
    if info["scheduled"] is False or "consolidated_stale" in ",".join(info["reasons"]) or info["log_signals"]:
        info["broken"] = True
    return info


def runtime_root_summary() -> dict:
    """Detect multiple live hermes-agent roots on this host (dual-root drift).

    Looks for any directory matching ~/hermes-agent*, ~/.hermes/hermes-agent*,
    ~/repos/hermes-runtime/hermes-agent, or ~/live-hermes/hermes-agent* that
    contains gateway/run.py. For each root, counts live processes running from
    its venv/bin/python via ps scan.

    Returns {"roots": [{"path": str, "has_run_py": bool, "live_pids": [int,...]}],
             "dual_root": bool, "active_roots": [str, ...]}.
    """
    import glob as _glob

    info: dict = {"roots": [], "dual_root": False, "active_roots": []}
    home = str(Path.home())
    candidate_globs = [
        f"{home}/hermes-agent",
        f"{home}/hermes-agent-*",
        f"{home}/.hermes/hermes-agent",
        f"{home}/.hermes/hermes-agent-*",
        f"{home}/repos/hermes-runtime/hermes-agent",
        f"{home}/repos/hermes-runtime*/hermes-agent",
        f"{home}/live-hermes/hermes-agent",
        f"{home}/live-hermes/hermes-agent-*",
    ]
    seen: set[str] = set()
    root_dirs: list[str] = []
    for pat in candidate_globs:
        for match in _glob.glob(pat):
            real = str(Path(match).resolve())
            if real in seen:
                continue
            seen.add(real)
            if Path(real).is_dir():
                root_dirs.append(real)

    try:
        ps = subprocess.run(["ps", "-e", "-o", "pid=,command="], capture_output=True, text=True, timeout=6)
        ps_lines = (ps.stdout or "").splitlines() if ps.returncode == 0 else []
    except Exception:
        ps_lines = []

    for root in sorted(root_dirs):
        run_py = Path(root) / "gateway" / "run.py"
        venv_python = f"{root}/venv/bin/python"
        live_pids: list[int] = []
        for line in ps_lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            cmd = parts[1]
            if venv_python in cmd or f"{root}/gateway/run.py" in cmd:
                live_pids.append(pid)
        info["roots"].append(
            {
                "path": root,
                "has_run_py": run_py.exists(),
                "live_pids": live_pids,
            }
        )
        if run_py.exists() and live_pids:
            info["active_roots"].append(root)

    info["dual_root"] = len(info["active_roots"]) > 1
    return info


def classify(report: dict) -> tuple[str, list[dict]]:
    reasons: list[dict] = []
    gateway = report["gateway"]
    transcript = report["transcript"]
    auth = report["auth"]
    cron = report["cron"]
    selfheal = report.get("selfheal") or {}
    fence_orphans = report.get("fence_orphans") or {}
    dream = report.get("dream") or {}
    runtime_roots = report.get("runtime_roots") or {}
    remediation = report.get("remediation") or {}
    gateway_service = report.get("gateway_service") or {}
    owner_dm_flow = report.get("owner_dm_flow") or {}

    if gateway["gateway_state"] != "running":
        reasons.append(
            {
                "code": "gateway_not_running",
                "severity": "unhealthy",
                "detail": f"gateway_state={gateway['gateway_state']}",
            }
        )
    if gateway["telegram_state"] not in {None, "connected"}:
        reasons.append(
            {
                "code": "telegram_not_connected",
                "severity": "unhealthy",
                "detail": f"telegram_state={gateway['telegram_state']}",
            }
        )
    if (
        gateway["updated_age_seconds"] is not None
        and gateway["updated_age_seconds"] > 900
        and gateway["gateway_state"] != "running"
    ):
        reasons.append(
            {
                "code": "gateway_state_stale",
                "severity": "degraded",
                "detail": f"gateway_state age={gateway['updated_age_seconds']}s",
            }
        )
    if gateway_service.get("service_definition_stale"):
        reasons.append(
            {
                "code": "gateway_service_definition_stale",
                "severity": "degraded",
                "detail": gateway_service.get("detail") or "gateway service definition is stale",
            }
        )
    if gateway_service.get("service_not_loaded") and gateway.get("gateway_state") == "running":
        reasons.append(
            {
                "code": "gateway_service_not_loaded",
                "severity": "degraded",
                "detail": gateway_service.get("detail")
                or "gateway service is not loaded while gateway process is running",
            }
        )
    if gateway_service.get("manual_process_only"):
        reasons.append(
            {
                "code": "gateway_manual_process_only",
                "severity": "degraded",
                "detail": gateway_service.get("detail") or "gateway process is running outside the managed service",
            }
        )
    owner_dm_lag = owner_dm_flow.get("lag_seconds")
    if owner_dm_flow.get("pending_owner_dm") and gateway.get("telegram_state") == "connected":
        reasons.append(
            {
                "code": "connected_not_processing",
                "severity": "unhealthy",
                "detail": f"owner_dm lag_seconds={owner_dm_lag if owner_dm_lag is not None else 'na'} latest_inbound_at={owner_dm_flow.get('latest_inbound_at') or 'unknown'} session_updated_at={owner_dm_flow.get('session_updated_at') or 'unknown'}",
            }
        )
    if (
        auth["recognized_users"] == 0
        and int(auth.get("provider_count") or 0) == 0
        and int(auth.get("credential_pool_entries") or 0) == 0
    ):
        reasons.append({"code": "auth_continuity_thin", "severity": "degraded", "detail": "recognized_users=0"})
    if not auth.get("telegram_home_channel"):
        reasons.append(
            {"code": "telegram_home_channel_missing", "severity": "degraded", "detail": "TELEGRAM_HOME_CHANNEL missing"}
        )
    if remediation.get("attempted") and remediation.get("handoff_ok") is False:
        handoff_age = remediation.get("updated_age_seconds")
        if handoff_age is None or handoff_age <= 21600:
            reasons.append(
                {
                    "code": "handoff_failed",
                    "severity": "degraded",
                    "detail": remediation.get("error") or "recent operator handoff failed",
                }
            )
    if selfheal["exists"] and selfheal["healthy"] is False:
        reasons.append(
            {"code": "selfheal_unhealthy", "severity": "degraded", "detail": "client self-heal reported unhealthy"}
        )
    if gateway.get("latency_ms") is not None:
        try:
            latency_ms = float(gateway["latency_ms"])
        except Exception:
            latency_ms = None
        if latency_ms is not None and latency_ms >= 2500:
            reasons.append({"code": "gateway_slow", "severity": "degraded", "detail": f"latency_ms={latency_ms}"})
    if cron.get("stale_jobs"):
        reasons.append(
            {"code": "stale_job_execution", "severity": "degraded", "detail": f"stale_jobs={len(cron['stale_jobs'])}"}
        )
    if cron.get("slow_jobs"):
        reasons.append(
            {"code": "slow_job_execution", "severity": "degraded", "detail": f"slow_jobs={len(cron['slow_jobs'])}"}
        )
    if transcript.get("friction_index", 0) >= 4:
        reasons.append(
            {
                "code": "client_friction_elevated",
                "severity": "degraded",
                "detail": f"friction_index={transcript['friction_index']}",
            }
        )
    if transcript.get("anger_index", 0) >= 2:
        reasons.append(
            {
                "code": "client_anger_elevated",
                "severity": "degraded",
                "detail": f"anger_index={transcript['anger_index']}",
            }
        )
    if transcript.get("duplicate_assistant_streak_max", 0) >= 3:
        reasons.append(
            {
                "code": "assistant_repeat_loop",
                "severity": "degraded",
                "detail": f"duplicate_assistant_streak_max={transcript['duplicate_assistant_streak_max']}",
            }
        )
    if transcript.get("continuity_meta_reply_count", 0) >= 1:
        reasons.append(
            {
                "code": "continuity_meta_reply",
                "severity": "degraded",
                "detail": f"continuity_meta_reply_count={transcript['continuity_meta_reply_count']}",
            }
        )
    for finding in report.get("log_findings") or []:
        code = finding.get("code")
        if code == "http_401":
            reasons.append(
                {
                    "code": "log_signature_401",
                    "severity": "degraded",
                    "detail": f"401 detected in {finding.get('path')}",
                }
            )
        elif code == "telegram_bad_request":
            reasons.append(
                {
                    "code": "telegram_bad_request",
                    "severity": "degraded",
                    "detail": f"Bad Request detected in {finding.get('path')}",
                }
            )
        elif code == "telegram_polling_conflict":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "telegram_polling_conflict",
                    "severity": "degraded",
                    "detail": f"Telegram polling conflict detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "home_channel_prompt":
            reasons.append(
                {
                    "code": "home_channel_prompt_detected",
                    "severity": "degraded",
                    "detail": f"Home-channel prompt detected in {finding.get('path')}",
                }
            )
        elif code == "permission_denied":
            reasons.append(
                {
                    "code": "permission_denied",
                    "severity": "degraded",
                    "detail": f"Permission/auth failure detected in {finding.get('path')}",
                }
            )
        elif code == "provider_timeout":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "provider_timeout_detected",
                    "severity": "degraded",
                    "detail": f"Provider timeout detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "provider_request_invalid":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "provider_request_invalid",
                    "severity": "degraded",
                    "detail": f"Invalid provider request detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "channel_prompt_missing":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "channel_prompt_missing",
                    "severity": "degraded",
                    "detail": f"channel_prompt event-shape mismatch detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "session_resume_event_shape_mismatch":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "session_resume_event_shape_mismatch",
                    "severity": "degraded",
                    "detail": f"session resume event-shape mismatch detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "hook_api_mismatch":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "hook_api_mismatch",
                    "severity": "degraded",
                    "detail": f"hook API mismatch detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "clarify_error":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "clarify_loop_risk",
                    "severity": "degraded",
                    "detail": f"Clarify tool errors detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "context_compaction":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "context_compaction_loop",
                    "severity": "degraded",
                    "detail": f"Context compaction loop detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "session_reset_noise":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "session_reset_noise",
                    "severity": "degraded",
                    "detail": f"Session-reset noise detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "selfheal_restart_loop":
            hit_count = int(finding.get("hit_count") or 1)
            reasons.append(
                {
                    "code": "selfheal_restart_loop",
                    "severity": "degraded",
                    "detail": f"Self-heal restart loop signals detected in {finding.get('path')} (hits={hit_count})",
                }
            )
        elif code == "limitation_language":
            reasons.append(
                {
                    "code": "limitation_language_detected",
                    "severity": "degraded",
                    "detail": f"Limitation language detected in {finding.get('path')}",
                }
            )
    timeout_hits = sum(
        int(finding.get("hit_count") or 1)
        for finding in (report.get("log_findings") or [])
        if finding.get("code") == "provider_timeout"
    )
    invalid_request_hits = sum(
        int(finding.get("hit_count") or 1)
        for finding in (report.get("log_findings") or [])
        if finding.get("code") == "provider_request_invalid"
    )
    clarify_hits = sum(
        int(finding.get("hit_count") or 1)
        for finding in (report.get("log_findings") or [])
        if finding.get("code") == "clarify_error"
    )
    compaction_hits = sum(
        int(finding.get("hit_count") or 1)
        for finding in (report.get("log_findings") or [])
        if finding.get("code") == "context_compaction"
    )
    polling_conflict_hits = sum(
        int(finding.get("hit_count") or 1)
        for finding in (report.get("log_findings") or [])
        if finding.get("code") == "telegram_polling_conflict"
    )
    session_reset_hits = sum(
        int(finding.get("hit_count") or 1)
        for finding in (report.get("log_findings") or [])
        if finding.get("code") == "session_reset_noise"
    )
    selfheal_loop_hits = sum(
        int(finding.get("hit_count") or 1)
        for finding in (report.get("log_findings") or [])
        if finding.get("code") == "selfheal_restart_loop"
    )
    continuity_meta_hits = int(transcript.get("continuity_meta_reply_count", 0) or 0)
    if timeout_hits >= 2:
        reasons.append(
            {"code": "provider_degraded", "severity": "degraded", "detail": f"provider_timeout_hits={timeout_hits}"}
        )
    if timeout_hits >= 4:
        reasons.append(
            {"code": "retry_storm", "severity": "degraded", "detail": f"provider_timeout_hits={timeout_hits}"}
        )
    if timeout_hits >= 2 and transcript.get("friction_index", 0) >= 2:
        reasons.append(
            {
                "code": "client_friction_under_latency",
                "severity": "degraded",
                "detail": f"timeout_hits={timeout_hits}, friction_index={transcript.get('friction_index', 0)}",
            }
        )
    if invalid_request_hits >= 1:
        reasons.append(
            {
                "code": "model_selection_empty",
                "severity": "degraded",
                "detail": f"invalid_request_hits={invalid_request_hits}",
            }
        )
    if clarify_hits >= 2:
        reasons.append(
            {"code": "clarify_loop_risk", "severity": "degraded", "detail": f"clarify_error_hits={clarify_hits}"}
        )
    if compaction_hits >= 3:
        reasons.append(
            {
                "code": "context_compaction_loop",
                "severity": "degraded",
                "detail": f"context_compaction_hits={compaction_hits}",
            }
        )
    if polling_conflict_hits >= 1:
        reasons.append(
            {
                "code": "duplicate_poller_risk",
                "severity": "degraded",
                "detail": f"polling_conflict_hits={polling_conflict_hits}",
            }
        )
    if session_reset_hits >= 2:
        reasons.append(
            {
                "code": "session_reset_chatter",
                "severity": "degraded",
                "detail": f"session_reset_hits={session_reset_hits}",
            }
        )
    if selfheal_loop_hits >= 2:
        reasons.append(
            {"code": "restart_loop_risk", "severity": "degraded", "detail": f"selfheal_loop_hits={selfheal_loop_hits}"}
        )
    if polling_conflict_hits >= 1 and transcript.get("friction_index", 0) >= 2:
        reasons.append(
            {
                "code": "client_friction_under_polling_conflict",
                "severity": "degraded",
                "detail": f"polling_conflict_hits={polling_conflict_hits}, friction_index={transcript.get('friction_index', 0)}",
            }
        )
    if clarify_hits >= 2 and compaction_hits >= 2:
        reasons.append(
            {
                "code": "client_experience_spinout",
                "severity": "degraded",
                "detail": f"clarify_hits={clarify_hits}, compaction_hits={compaction_hits}",
            }
        )
    if continuity_meta_hits >= 1:
        reasons.append(
            {
                "code": "client_experience_spinout",
                "severity": "degraded",
                "detail": f"continuity_meta_reply_count={continuity_meta_hits}",
            }
        )

    if fence_orphans.get("count", 0) > 0:
        orphans = fence_orphans.get("orphans") or []
        pid_csv = ",".join(str(o.get("pid")) for o in orphans[:6])
        max_age = max((int(o.get("age_seconds") or 0) for o in orphans), default=0)
        reasons.append(
            {
                "code": "fence_orphan_bash",
                "severity": "unhealthy" if len(orphans) >= 3 else "degraded",
                "detail": f"orphan_count={len(orphans)}, max_age_s={max_age}, pids={pid_csv}",
            }
        )

    if dream.get("broken"):
        detail_parts = []
        if dream.get("scheduled") is False:
            detail_parts.append("scheduler_missing")
        if dream.get("consolidated_age_seconds") is not None and dream.get("consolidated_age_seconds") > 36 * 3600:
            detail_parts.append(f"consolidated_age_s={int(dream['consolidated_age_seconds'])}")
        if dream.get("log_signals"):
            detail_parts.append("log=" + ",".join(dream["log_signals"]))
        reasons.append(
            {
                "code": "dream_consolidation_broken",
                "severity": "degraded",
                "detail": "; ".join(detail_parts) or "dream signals present",
            }
        )

    if runtime_roots.get("dual_root"):
        active = runtime_roots.get("active_roots") or []
        reasons.append(
            {
                "code": "runtime_dual_root",
                "severity": "unhealthy",
                "detail": f"active_roots={len(active)}: " + " | ".join(active[:4]),
            }
        )

    overall = "healthy"
    if any(r["severity"] == "unhealthy" for r in reasons):
        overall = "unhealthy"
    elif reasons:
        overall = "degraded"
    return overall, reasons


def build_incidents(report: dict, registry: dict) -> dict:
    incident_map = (registry or {}).get("incidents") or {}
    log_map: dict[str, list[dict]] = {}
    for finding in report.get("log_findings") or []:
        code = str(finding.get("code") or "").strip()
        if code:
            log_map.setdefault(code, []).append(finding)
    grouped: dict[str, list[dict]] = {}
    for reason in report.get("reasons") or []:
        code = str(reason.get("code") or "").strip()
        if not code:
            continue
        grouped.setdefault(code, []).append(reason)

    incidents = []
    for code, rows in sorted(grouped.items()):
        reg = incident_map.get(code) or {}
        severities = [str(row.get("severity") or "degraded") for row in rows]
        severity = "unhealthy" if "unhealthy" in severities else severities[0]
        matching_findings = log_map.get(code) or []
        trace_signature = ""
        trace_hash = ""
        log_evidence = []
        if matching_findings:
            excerpts: list[str] = []
            for finding in matching_findings[:5]:
                excerpt = str((finding or {}).get("excerpt") or "").strip()
                if excerpt:
                    excerpts.append(excerpt)
                log_evidence.append(
                    {
                        "path": str((finding or {}).get("path") or ""),
                        "excerpt": excerpt[:400],
                        "hit_count": int((finding or {}).get("hit_count") or 1),
                        "line_count": int((finding or {}).get("line_count") or 0),
                    }
                )
            if excerpts:
                trace_signature = excerpts[0][:240]
                trace_hash = hashlib.sha256("\n".join(excerpts).encode("utf-8")).hexdigest()[:16]
        incidents.append(
            {
                "incident_type": code,
                "incident_family": reg.get("incident_family") or "uncategorized",
                "severity": severity,
                "source_host": report["identity"]["hostname"],
                "source_agent": report["identity"]["agent_name"],
                "trigger_source": "operator_status",
                "observed_at": report["generated_at"],
                "repeat_count": len(rows),
                "runbook_ref": reg.get("runbook_ref") or "",
                "playbook_family": reg.get("playbook_family") or "",
                "approval_tier": reg.get("approval_tier") or "read_only",
                "trace_signature": trace_signature,
                "trace_signature_hash": trace_hash,
                "details": [str(row.get("detail") or "") for row in rows],
                "log_evidence": log_evidence,
                "evidence_refs": [
                    report["evidence"]["gateway_state_path"],
                    report["evidence"]["auth_path"],
                    report["evidence"]["transcript_db_path"],
                    report["evidence"]["jobs_path"],
                ],
            }
        )

    return {
        "schema_version": 1,
        "generated_at": report["generated_at"],
        "hostname": report["identity"]["hostname"],
        "agent_name": report["identity"]["agent_name"],
        "overall_status": report.get("overall_status") or "unknown",
        "registry_path": registry.get("_path"),
        "incident_count": len(incidents),
        "incidents": incidents,
    }


def build_runtime_trace_incidents(report: dict) -> dict:
    incidents = []
    for incident in (report.get("incidents") or {}).get("incidents") or []:
        if not incident.get("trace_signature") and not incident.get("log_evidence"):
            continue
        incidents.append(
            {
                "incident_type": incident.get("incident_type") or "",
                "runbook_ref": incident.get("runbook_ref") or "",
                "incident_family": incident.get("incident_family") or "uncategorized",
                "approval_tier": incident.get("approval_tier") or "read_only",
                "severity": incident.get("severity") or "degraded",
                "observed_at": incident.get("observed_at") or report.get("generated_at"),
                "repeat_count": int(incident.get("repeat_count") or 0),
                "trace_signature": incident.get("trace_signature") or "",
                "trace_signature_hash": incident.get("trace_signature_hash") or "",
                "log_evidence": incident.get("log_evidence") or [],
                "evidence_refs": incident.get("evidence_refs") or [],
                "details": incident.get("details") or [],
            }
        )
    return {
        "schema_version": 1,
        "generated_at": report.get("generated_at"),
        "hostname": report["identity"]["hostname"],
        "agent_name": report["identity"]["agent_name"],
        "incident_count": len(incidents),
        "incidents": incidents,
    }


def build_report(home: Path) -> dict:
    lane_base = resolve_lane_base(home)
    config = read_yaml(home / "config.yaml")
    soul = soul_identity(home)
    client_identity = (
        str(config.get("client_identity") or "").strip() or soul.get("client_identity") or slugify(home.name)
    )
    client_label = soul.get("client_label") or ""
    agent_name = (
        str(config.get("agent_name") or config.get("assistant_name") or "").strip()
        or soul.get("agent_name")
        or home.name
    )
    paths = log_paths(home, lane_base)
    report = {
        "schema_version": 1,
        "generated_at": iso_now(),
        "identity": {
            "hostname": socket.gethostname(),
            "platform": platform.system().lower(),
            "hermes_home": str(home),
            "lane_base": str(lane_base),
            "client_identity": client_identity,
            "client_label": client_label,
            "agent_name": agent_name,
        },
        "gateway": gateway_summary(lane_base),
        "gateway_service": gateway_service_runtime_summary(home),
        "owner_dm_flow": owner_dm_flow_summary(home, lane_base),
        "transcript": transcript_summary(home),
        "auth": auth_summary(
            lane_base if (lane_base / "auth.json").exists() else home,
            fallback_home=home,
        ),
        "cron": cron_summary(home, lane_base),
        "selfheal": selfheal_summary(home),
        "windows_runtime": windows_runtime_summary(home),
        "operator_alerts": operator_alerts_summary(home),
        "memory": memory_summary(home),
        "fence_orphans": fence_orphan_summary(),
        "dream": dream_agent_summary(home),
        "runtime_roots": runtime_root_summary(),
        "remediation": remediation_summary(home),
        "evidence": {
            "gateway_state_path": str(lane_base / "gateway_state.json"),
            "auth_path": str((lane_base / "auth.json") if (lane_base / "auth.json").exists() else (home / "auth.json")),
            "transcript_db_path": str(home / "data" / "telegram-transcript.db"),
            "jobs_path": str(
                (lane_base / "cron" / "jobs.json")
                if (lane_base / "cron" / "jobs.json").exists()
                else (home / "cron" / "jobs.json")
            ),
            "log_paths": paths,
        },
    }
    report["log_findings"] = scan_log_signatures(paths)
    overall, reasons = classify(report)
    report["overall_status"] = overall
    report["reasons"] = reasons
    registry = load_runbook_registry(home)
    report["runbook_registry_path"] = registry.get("_path")
    report["incidents"] = build_incidents(report, registry)
    report["runtime_trace_incidents"] = build_runtime_trace_incidents(report)
    report["friction_points"] = [reason["code"] for reason in reasons]
    report["needs_operator"] = (
        any(r["severity"] == "unhealthy" for r in reasons) or not report["operator_alerts"]["configured"]
    )
    report["needs_privileged_access"] = any(r["code"] in {"gateway_not_running"} for r in reasons)
    report["can_self_repair"] = overall != "unhealthy"
    report["operator_summary"] = build_operator_summary(report)
    report["client_summary"] = build_client_summary(report)
    report["recommended_agent_action"] = build_recommended_agent_action(report)
    report["recommended_doc_action"] = build_recommended_doc_action(report)
    report["recommended_deacon_note"] = build_recommended_deacon_note(report)
    report["handoff_recommended"] = report["overall_status"] != "healthy"
    return report


def build_operator_summary(report: dict) -> str:
    gateway = report["gateway"]
    transcript = report["transcript"]
    auth = report["auth"]
    reasons = report["reasons"]
    reason_text = "; ".join(r["code"] for r in reasons) if reasons else "no active blockers"
    return (
        f"{report['overall_status'].upper()}: gateway={gateway['gateway_state'] or 'missing'}, "
        f"telegram={gateway['telegram_state'] or 'missing'}, transcript_rows={transcript['rows']}, "
        f"recognized_users={auth['recognized_users']}, friction_index={transcript.get('friction_index', 0)}, "
        f"anger_index={transcript.get('anger_index', 0)}, duplicate_streak={transcript.get('duplicate_assistant_streak_max', 0)}, blockers={reason_text}"
    )


def build_client_summary(report: dict) -> str:
    if report["overall_status"] == "healthy":
        return "Everything looks healthy from the local runtime checks right now."
    if report["overall_status"] == "degraded":
        return "The agent is up, but there are local warning signs that may need operator review."
    return "The local runtime found a blocker that likely needs operator intervention."


def build_recommended_agent_action(report: dict) -> str:
    if report["overall_status"] == "healthy":
        return "No operator remediation is needed right now."
    if report["needs_operator"] or report["needs_privileged_access"]:
        return "Run operator-remediation.py so Doc receives the current failure points, evidence paths, and relevant log excerpts."
    return "Monitor the friction points, refresh operator-status after any local fix, and escalate with operator-remediation.py if the warnings persist."


def build_recommended_doc_action(report: dict) -> str:
    if report["overall_status"] == "healthy":
        return "No action required."
    reasons = ", ".join(report["friction_points"]) or "runtime drift"
    return (
        "Inspect the attached remediation report, address the active runtime issues, "
        f"tag repeatable patterns for infrastructure follow-up, and verify the status surface returns healthy. Active reasons: {reasons}."
    )


def build_recommended_deacon_note(report: dict) -> str:
    if report["overall_status"] == "healthy":
        return "No the operator note required."
    return (
        "Send the operator a short operator note covering what happened, what was fixed, "
        "what remains open, and any infrastructure hardening needed to prevent recurrence."
    )


def write_outputs(home: Path, report: dict) -> tuple[Path, Path, Path]:
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    json_path = state_dir / "operator-status.json"
    brief_path = state_dir / "operator-status-brief.md"
    incidents_path = state_dir / "operator-incidents.json"
    runtime_trace_path = state_dir / "runtime-trace-incidents.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    incidents_path.write_text(json.dumps(report.get("incidents") or {}, indent=2) + "\n", encoding="utf-8")
    runtime_trace_path.write_text(
        json.dumps(report.get("runtime_trace_incidents") or {}, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Operator Summary",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Status: {report['overall_status']}",
        f"- Friction index: {report['transcript'].get('friction_index', 0)}",
        f"- Anger index: {report['transcript'].get('anger_index', 0)}",
        f"- Duplicate assistant streak: {report['transcript'].get('duplicate_assistant_streak_max', 0)}",
        f"- Operator summary: {report['operator_summary']}",
        f"- Client summary: {report['client_summary']}",
        f"- Agent action: {report['recommended_agent_action']}",
        f"- Doc action: {report['recommended_doc_action']}",
        f"- the operator note: {report['recommended_deacon_note']}",
        "",
        "## Reasons",
    ]
    if report["reasons"]:
        for reason in report["reasons"]:
            lines.append(f"- {reason['code']}: {reason['detail']}")
    else:
        lines.append("- none")
    lines += [
        "",
        "## Frustration Signals",
    ]
    if report["transcript"].get("frustration_hits"):
        for hit in report["transcript"]["frustration_hits"]:
            lines.append(
                f"- {hit['timestamp']} {hit.get('sender_name') or 'unknown'} [{','.join(hit.get('signals') or [])}]: {hit['text']}"
            )
    else:
        lines.append("- none")
    lines += [
        "",
        "## Repeat-Loop Signals",
    ]
    if report["transcript"].get("duplicate_assistant_hits"):
        for hit in report["transcript"]["duplicate_assistant_hits"]:
            lines.append(
                f"- {hit['timestamp']} {hit.get('sender_name') or 'assistant'} [streak={hit.get('streak', 0)}]: {hit['text']}"
            )
    else:
        lines.append("- none")
    lines += [
        "",
        "## Evidence",
        f"- Gateway state: {report['evidence']['gateway_state_path']}",
        f"- Auth: {report['evidence']['auth_path']}",
        f"- Transcript DB: {report['evidence']['transcript_db_path']}",
        f"- Cron jobs: {report['evidence']['jobs_path']}",
    ]
    for log_path in report["evidence"].get("log_paths", []):
        lines.append(f"- Log: {log_path}")
    lines += [
        "",
    ]
    brief_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, brief_path, incidents_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a local operator-facing Hermes status snapshot.")
    parser.add_argument("--hermes-home", default="", help="Override Hermes home path.")
    parser.add_argument(
        "--write", action="store_true", help="Write operator-status.json and operator-status-brief.md under state/."
    )
    parser.add_argument("--format", choices=["json", "brief"], default="json", help="Output format to stdout.")
    args = parser.parse_args()

    home = resolve_home(args.hermes_home)
    report = build_report(home)
    json_path = None
    brief_path = None
    incidents_path = None
    runtime_trace_path = None
    if args.write:
        json_path, brief_path, incidents_path = write_outputs(home, report)
        runtime_trace_path = json_path.parent / "runtime-trace-incidents.json"
        report["state_paths"] = {
            "json": str(json_path),
            "brief": str(brief_path),
            "incidents": str(incidents_path),
            "runtime_trace_incidents": str(runtime_trace_path),
        }

    if args.format == "brief":
        if brief_path and brief_path.exists():
            sys.stdout.write(brief_path.read_text(encoding="utf-8"))
        else:
            sys.stdout.write(report["operator_summary"] + "\n")
    else:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
