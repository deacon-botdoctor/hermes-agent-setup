#!/usr/bin/env python3
"""Reap stale agent-browser trees without touching normal user browsers.

Only a controller or Chrome-for-Testing root that is already reparented to
init (ppid 1) and older than the age floor is eligible. A live task keeps its
controller parented to the owning process, so it is never selected.
"""
from __future__ import annotations

import os
import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
LOG = HERMES / "logs" / "agent-browser-orphan-reaper.log"
MIN_AGE_SECONDS = 600
NODE_MARK = "node_modules/agent-browser/bin/agent-browser"
CHROME_MARK = "Chrome for Testing"
LEASES = HERMES / "state" / "host-steward" / "leases"
STEWARD = HERMES / "bin" / "hermes-host-steward.py"


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"[agent-browser-orphan-reaper] {msg}\n"
        )


def parse_etime(raw: str) -> int:
    """Parse ps etime ([[DD-]HH:]MM:SS) into seconds."""
    days = 0
    if "-" in raw:
        day_text, raw = raw.split("-", 1)
        days = int(day_text)
    bits = [int(bit) for bit in raw.split(":")]
    if len(bits) == 3:
        hours, minutes, seconds = bits
    elif len(bits) == 2:
        hours, minutes, seconds = 0, bits[0], bits[1]
    else:
        hours, minutes, seconds = 0, 0, bits[0]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def snapshot() -> list[dict]:
    result = subprocess.run(
        ["ps", "-eo", "pid,ppid,etime,rss,command"],
        capture_output=True,
        text=True,
        check=False,
    )
    processes = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            processes.append(
                {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "etimes": parse_etime(parts[2]),
                    "rss": int(parts[3]),
                    "cmd": parts[4],
                }
            )
        except ValueError:
            continue
    return processes


def descendants(pid: int, by_ppid: dict[int, list[dict]]) -> list[dict]:
    found, stack = [], [pid]
    while stack:
        for child in by_ppid.get(stack.pop(), []):
            found.append(child)
            stack.append(child["pid"])
    return found


def kill(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["kill", "-9", str(pid)], capture_output=True, check=False
        )
        return result.returncode == 0
    except Exception:
        return False


def protected_debug_ports() -> set[int]:
    ports: set[int] = set()
    if not LEASES.is_dir():
        return ports
    for path in LEASES.glob("*.json"):
        try:
            lease = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(lease, dict):
                continue
            resource = lease.get("resource")
            if not isinstance(resource, dict):
                continue
            endpoint = urllib.parse.urlparse(str(resource.get("endpoint") or ""))
            if (
                lease.get("schema") == "hermes-host-steward/v1"
                and lease.get("kind") == "browser_tab"
                and endpoint.hostname in {"127.0.0.1", "localhost"}
                and endpoint.port
            ):
                ports.add(int(endpoint.port))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return ports


def eligible_roots(processes: list[dict], protected_ports: set[int]) -> list[dict]:
    def protected(process: dict) -> bool:
        match = re.search(r"--remote-debugging-port(?:=|\s+)(\d+)", process["cmd"])
        return bool(match and int(match.group(1)) in protected_ports)

    by_ppid: dict[int, list[dict]] = {}
    for process in processes:
        by_ppid.setdefault(process["ppid"], []).append(process)
    roots = []
    for process in processes:
        if not (
            process["ppid"] == 1
            and process["etimes"] >= MIN_AGE_SECONDS
            and (NODE_MARK in process["cmd"] or CHROME_MARK in process["cmd"])
        ):
            continue
        tree = [process] + descendants(process["pid"], by_ppid)
        if not any(protected(member) for member in tree):
            roots.append(process)
    return roots


def main() -> int:
    if STEWARD.is_file():
        log("disabled: Host Steward owns all resource mutation")
        return 0
    processes = snapshot()
    by_ppid: dict[int, list[dict]] = {}
    for process in processes:
        by_ppid.setdefault(process["ppid"], []).append(process)

    roots = eligible_roots(processes, protected_debug_ports())
    if not roots:
        log("clean: no orphaned browser trees")
        return 0

    reclaimed_kb = 0
    killed_chrome = 0
    for root in roots:
        tree = [root] + descendants(root["pid"], by_ppid)
        for process in tree:
            if CHROME_MARK not in process["cmd"]:
                continue
            reclaimed_kb += process["rss"]
            if kill(process["pid"]):
                killed_chrome += 1
        kill(root["pid"])

    time.sleep(2)
    remaining = snapshot()
    node_stubs = sum(
        1
        for process in remaining
        if process["ppid"] == 1 and NODE_MARK in process["cmd"]
    )
    chrome = sum(1 for process in remaining if CHROME_MARK in process["cmd"])
    log(
        f"reaped roots={len(roots)} chrome_killed={killed_chrome} "
        f"reclaimed~{reclaimed_kb // 1024}MB | "
        f"remaining wedged_node_stubs={node_stubs} chrome={chrome}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
