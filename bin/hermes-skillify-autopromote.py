#!/usr/bin/env python3
"""Compatibility guard: automatic draft promotion is disabled.

Skill activation is a separate, independently reviewed command and receipt.
This historical executable remains in place because existing nightly wiring
may invoke it; every inert or unknown-provenance draft is left untouched.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
HERMES = Path(os.environ.get("HERMES_HOME", str(HOME / ".hermes"))).expanduser()
DRAFTS = HERMES / "skills/drafts"
LOG = HERMES / "logs/skillify-autopromote.log"


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{now()} [autopromote] {message}\n")


def main() -> int:
    summary = {
        "schema": "hermes-skillify-autopromote-guard/v1",
        "promoted": [],
        "gated": [],
        "skipped": [],
        "automatic_promotion": False,
        "host": socket.gethostname(),
    }
    if not DRAFTS.is_dir():
        print(json.dumps(summary, sort_keys=True))
        return 0

    for draft in sorted(DRAFTS.iterdir()):
        if not draft.is_dir() or not (draft / "SKILL.md").is_file():
            continue
        summary["gated"].append(
            {
                "name": draft.name,
                "scope": "unknown",
                "reasons": ["automatic promotion disabled; use the separate explicit activation gate"],
            }
        )
        log(f"GATED {draft.name}: automatic promotion disabled")

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
