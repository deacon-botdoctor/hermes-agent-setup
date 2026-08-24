#!/usr/bin/env python3
"""Bind validated external schedule runs to Hermes oneshot receipt provenance."""

from __future__ import annotations

import re
from pathlib import Path

MARKER = "HERMES_ONESHOT_CRON_PROVENANCE_v1"


class PatchError(RuntimeError):
    pass


def patch_source(content: str) -> str:
    if MARKER in content:
        return content
    pattern = re.compile(
        r'(?m)^(?P<indent>[ \t]+)agent = AIAgent\(\n'
        r'(?P<argindent>[ \t]+)api_key=runtime\.get\("api_key"\),\n'
    )
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        raise PatchError("required unique anchor missing: oneshot AIAgent construction")
    match = matches[0]
    indent = match.group("indent")
    argindent = match.group("argindent")
    replacement = (
        f"{indent}agent = AIAgent(\n"
        f"{argindent}# {MARKER}: the owning scheduler and caller validate the exact\n"
        f"{argindent}# job/run pair before setting this process-local identifier. Receipt\n"
        f"{argindent}# enforcement then classifies the paid attempt as ``cron_run``.\n"
        f"{argindent}session_id=(\n"
        f'{argindent}    os.getenv("HERMES_ONESHOT_SESSION_ID", "").strip()\n'
        f'{argindent}    if os.getenv("HERMES_ONESHOT_SESSION_ID", "").strip().startswith("cron_")\n'
        f"{argindent}    else None\n"
        f"{argindent}),\n"
        f'{argindent}api_key=runtime.get("api_key"),\n'
    )
    return content[: match.start()] + replacement + content[match.end() :]


def patch_oneshot_cron_provenance_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "hermes_cli" / "oneshot.py"
    if not target.is_file():
        raise PatchError(f"required file missing: {target}")
    before = target.read_text(encoding="utf-8")
    after = patch_source(before)
    if after == before:
        return False
    target.write_text(after, encoding="utf-8")
    return True
