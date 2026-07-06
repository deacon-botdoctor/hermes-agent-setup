#!/usr/bin/env python3
"""progress-compose — human-voiced progress updates for long-running work.

When the agent is on a multi-minute task, a client shouldn't stare at silence, and they shouldn't
get a robotic "iteration 14, tool_call succeeded" status either. This composes a short, natural
update in the voice of a competent operator reporting to their boss: what's been done, what's
happening now, how long it's been — with every trace of the machinery (tools, APIs, logs, job
ids, internal names) stripped out.

Pair it with a periodic trigger (a ~10-minute checkpoint on long tasks) that feeds it the current
goal + a summary of recent activity. It calls a cheap model, falls back to a templated update if
the model is unavailable, and never invents progress that isn't in the activity it was given.

Usage:
  progress-compose.py --goal "reconcile the March invoices" --elapsed 10 \
      --activity-json '{"did":["pulled the ledger"],"now":"matching line items"}'
  progress-compose.py --goal "..." --elapsed 10 --fallback     # no LLM, templated

Set OPENROUTER_API_KEY (env or ~/.hermes/.env). Model via PROGRESS_MODEL (default a cheap flash).
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
MODEL = os.environ.get("PROGRESS_MODEL", "google/gemini-2.5-flash-lite")


def load_env(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    for env_file in (HERMES_HOME / ".env.secrets", HERMES_HOME / ".env"):
        if env_file.exists():
            for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if raw.startswith(name + "="):
                    return raw.split("=", 1)[1].strip().strip("\"'")
    return ""


PROMPT = """Write a natural {elapsed}-minute progress update from an employee/operator to their boss.

Rules:
- Sound like a competent human operator, not a status bot.
- Mention "{elapsed} minutes in" naturally.
- Be specific to the task.
- Include what has been done so far and what is happening now.
- Do not mention tools, APIs, iteration counts, logs, job IDs, implementation details, or internal system names.
- Do not invent findings, root causes, fixes, client details, or completion claims.
- Only state concrete progress that is explicit in the task/activity. If evidence is thin, say you're still working through it.
- Keep it to 2-4 short lines.

Task/request:
{goal}

Recent internal activity summary, for your eyes only:
{activity}

Recent background output, if any, for your eyes only:
{recent}
"""


def compose(args) -> str:
    key = load_env("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY unavailable")
    prompt = PROMPT.format(
        elapsed=args.elapsed, goal=args.goal,
        activity=args.activity_json or "{}", recent=(args.recent or "").strip(),
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You write concise, natural operational updates for a business owner. No robotic templates."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 180,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "progress-compose",
        },
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = str(data["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise RuntimeError("empty progress text")
    return text[:1200]


def fallback(args) -> str:
    goal = " ".join((args.goal or "this").split())
    if len(goal) > 120:
        goal = goal[:117] + "..."
    return (
        f"{args.elapsed} minutes in — I'm still on {goal}.\n"
        "So far, I've got the work moving and I'm sorting through the parts that matter.\n"
        "Right now, I'm continuing the next step and checking it before I hand anything back."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Compose a natural, human-voiced progress update.")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--elapsed", required=True)
    ap.add_argument("--activity-json", default="{}")
    ap.add_argument("--recent", default="")
    ap.add_argument("--timeout", type=int, default=18)
    ap.add_argument("--fallback", action="store_true")
    args = ap.parse_args()
    try:
        text = fallback(args) if args.fallback else compose(args)
    except Exception:
        text = fallback(args)  # degrade gracefully; a client still gets a human update
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
