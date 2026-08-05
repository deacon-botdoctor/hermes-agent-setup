#!/usr/bin/env python3
"""Telegram polling health probe for Hermes gateways.

Checks the transport layer that shallow gateway_state cannot prove:
- gateway_state says Telegram connected and process is alive
- process-wide CLOSE_WAIT sockets are reported as advisory telemetry
- Telegram adapter heartbeat file is fresh (written by live adapter task)
- recent logs do not show polling conflicts or reconnect storms

Exit 0 = healthy. Exit 1 = unhealthy. Output is a compact reason string.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"unreadable:{path.name}:{exc.__class__.__name__}") from exc


def _pid_alive(pid: str | int | None) -> bool:
    if not pid:
        return False
    return subprocess.run(["kill", "-0", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _close_wait_count(pid: str | int) -> int:
    if platform.system() == "Darwin":
        proc = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True)
        return sum(1 for line in proc.stdout.splitlines() if "CLOSE_WAIT" in line)
    proc = subprocess.run(["ss", "-tnp"], capture_output=True, text=True)
    needle = f"pid={pid},"
    return sum(1 for line in proc.stdout.splitlines() if "CLOSE-WAIT" in line and needle in line)


def _recent_log_hits(profile_dir: Path, window_sec: int, since_ts: float | None = None) -> list[str]:
    now = time.time()
    patterns = [
        re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)?"),
        re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),
    ]
    bad_needles = (
        "terminated by other getUpdates request",
        "telegram_polling_conflict",
        "Telegram polling retry",
        "Telegram polling could not recover",
        "Updater not running after reconnect",
        "Polling heartbeat probe failed",
    )
    hits: list[str] = []
    for rel in ("logs/gateway.log", "logs/gateway.error.log"):
        path = profile_dir / rel
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines()[-600:]:
            if not any(n in line for n in bad_needles):
                continue
            ts = None
            for pat in patterns:
                m = pat.match(line)
                if not m:
                    continue
                raw = m.group(1)
                fmt = "%Y-%m-%d %H:%M:%S" if " " in raw else "%Y-%m-%dT%H:%M:%S"
                dt = datetime.strptime(raw, fmt)
                if " " in raw:
                    dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.astimezone(timezone.utc).timestamp()
                break
            if ts is None:
                continue
            if since_ts is not None and ts <= since_ts:
                continue
            if now - ts <= window_sec:
                hits.append(line[-240:])
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile-dir", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    ap.add_argument("--max-heartbeat-age", type=int, default=int(os.environ.get("HERMES_TELEGRAM_POLL_HEARTBEAT_MAX_AGE", "120")))
    ap.add_argument("--max-state-age", type=int, default=int(os.environ.get("HERMES_TELEGRAM_STATE_MAX_AGE", "180")))
    ap.add_argument("--max-close-wait", type=int, default=int(os.environ.get("HERMES_TELEGRAM_CLOSE_WAIT_MAX", "5")))
    ap.add_argument("--log-window", type=int, default=int(os.environ.get("HERMES_TELEGRAM_POLL_LOG_WINDOW", "900")))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    profile_dir = Path(args.profile_dir).expanduser()
    state_path = profile_dir / "gateway_state.json"
    health_path = profile_dir / "state" / "telegram-polling-health.json"
    now = time.time()
    result: dict[str, Any] = {"ok": False, "profile_dir": str(profile_dir)}

    try:
        state = _load_json(state_path)
        pid = state.get("pid")
        result["pid"] = pid
        if state.get("gateway_state") != "running":
            raise RuntimeError(f"gateway_state={state.get('gateway_state')}")
        tg = (state.get("platforms") or {}).get("telegram") or {}
        if tg.get("state") != "connected":
            raise RuntimeError(f"telegram={tg.get('state')}")
        state_ts = _parse_ts(state.get("updated_at"))
        if state_ts is not None:
            result["state_age"] = int(now - state_ts)
        if not _pid_alive(pid):
            raise RuntimeError(f"pid_dead={pid}")
        cw = _close_wait_count(pid)
        result["close_wait"] = cw
        # This process owns Telegram, provider, MCP, and other HTTP sockets.
        # lsof/ss cannot attribute a generic CLOSE_WAIT row to Telegram, so a
        # process-wide count is useful leak telemetry but is not evidence that
        # Telegram polling is unhealthy.  The heartbeat, updater, Bot API, and
        # recent polling-error checks below remain the fatal signals.
        close_wait_advisory = cw > args.max_close_wait
        result["close_wait_advisory"] = close_wait_advisory

        try:
            health = _load_json(health_path)
            result["health"] = health
            hb_ts = _parse_ts(health.get("last_poll_probe_at"))
            if hb_ts is None:
                raise RuntimeError("missing_poll_heartbeat")
            if health.get("updater_running") is not True:
                raise RuntimeError(f"updater_running={health.get('updater_running')}")
            if health.get("bot_api_ok") is not True:
                raise RuntimeError(f"bot_api_ok={health.get('bot_api_ok')}")
        except RuntimeError as file_exc:
            # Fleet compatibility: HERMES_TELEGRAM_LIVENESS_v1 writes the same
            # positive polling proof into gateway_state instead of a sidecar file.
            # Only fall back when the sidecar is absent/missing its heartbeat;
            # if a sidecar explicitly says updater/bot API is bad, fail closed.
            msg = str(file_exc)
            if not (msg.startswith("unreadable:telegram-polling-health.json") or msg == "missing_poll_heartbeat"):
                raise
            hb_ts = _parse_ts(tg.get("last_successful_poll_at"))
            if hb_ts is None:
                raise file_exc
            result["health"] = {
                "source": "gateway_state.last_successful_poll_at",
                "last_poll_probe_at": tg.get("last_successful_poll_at"),
                "updater_running": True,
                "bot_api_ok": True,
            }
        hb_age = int(now - hb_ts)
        result["heartbeat_age"] = hb_age
        if hb_age > args.max_heartbeat_age:
            raise RuntimeError(f"poll_heartbeat_stale={hb_age}s")

        hits = _recent_log_hits(profile_dir, args.log_window, hb_ts)
        result["recent_polling_error_hits"] = len(hits)
        if hits:
            result["recent_polling_errors"] = hits[:3]
            raise RuntimeError(f"recent_polling_errors={len(hits)}")

        result["ok"] = True
        advisory = " advisory" if close_wait_advisory else ""
        result["reason"] = f"ok heartbeat_age={hb_age}s close_wait={cw}{advisory}"
        print(json.dumps(result, sort_keys=True) if args.json else result["reason"])
        return 0
    except Exception as exc:
        result["reason"] = str(exc)
        print(json.dumps(result, sort_keys=True) if args.json else str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
