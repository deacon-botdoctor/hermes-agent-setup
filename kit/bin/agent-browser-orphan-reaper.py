#!/usr/bin/env python3
"""Reap stale agent-browser trees without touching normal user browsers.

Only a controller or Chrome-for-Testing root that is already reparented to
init (ppid 1) and older than the age floor is eligible. A live task keeps its
controller parented to the owning process, so it is never selected.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
LOG = HERMES / "logs" / "agent-browser-orphan-reaper.log"
MIN_AGE_SECONDS = 600
NODE_MARK = "node_modules/agent-browser/bin/agent-browser"
CHROME_MARK = "Chrome for Testing"


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


def eligible_roots(processes: list[dict]) -> list[dict]:
    return [
        process
        for process in processes
        if process["ppid"] == 1
        and process["etimes"] >= MIN_AGE_SECONDS
        and (NODE_MARK in process["cmd"] or CHROME_MARK in process["cmd"])
    ]


def main() -> int:
    processes = snapshot()
    by_ppid: dict[int, list[dict]] = {}
    for process in processes:
        by_ppid.setdefault(process["ppid"], []).append(process)

    roots = eligible_roots(processes)
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
