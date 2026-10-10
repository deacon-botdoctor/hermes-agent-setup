"""Refuse agent-created short-interval "progress update" cron jobs.

Root cause (Enoch 2026-10-07 16:04 CT): asked for progress while a repair ran, the model
created cron job 7ef0d1a9922d ("4score repair progress", every 5m, repeat 24, inherited
model). Each run was a fresh inference turn that knew nothing about the in-flight work,
posted "awaiting authorization" 10 times, and as an unclassified inference job it failed the
fleet cron model gate and stopped the BOT-37 release (native039).

Change: ``cronjob(action="create")`` from the agent rejects an agent-mode job whose schedule
recurs at <=15 minutes AND whose name/prompt reads as a progress/status update. The error
points the model at the live progress card. Script-only (no_agent) watchdogs, longer
intervals, and unrelated short jobs are unaffected. Operator override:
HERMES_CRON_ALLOW_PROGRESS_JOBS=1.
"""
from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_CRON_PROGRESS_JOB_GUARD_v1"

ANCHOR = '''    if error:
        return tool_error(error, success=False)

    context_from = a["context_from"]
'''
NEW = '''    if error:
        return tool_error(error, success=False)
    progress_error = _progress_cron_guard(a, _no_agent)  # HERMES_CRON_PROGRESS_JOB_GUARD_v1
    if progress_error:
        return tool_error(progress_error, success=False)

    context_from = a["context_from"]
'''
HELPER_ANCHOR = "def _action_create(a: Dict[str, Any]) -> str:\n"
HELPER = '''# [HERMES_CRON_PROGRESS_JOB_GUARD_v1]
_PROGRESS_CRON_MAX_MINUTES = 15
_PROGRESS_CRON_RE = None


def _progress_cron_guard(a: Dict[str, Any], no_agent: bool) -> Optional[str]:
    """Reject a <=15-minute agent-mode job that only exists to report progress on in-flight work."""
    import os as _os
    import re as _re
    global _PROGRESS_CRON_RE
    if no_agent or _os.environ.get("HERMES_CRON_ALLOW_PROGRESS_JOBS", "").strip().lower() in ("1", "true", "yes"):
        return None
    try:
        from cron.jobs import parse_schedule
        parsed = parse_schedule(str(a.get("schedule") or ""))
    except Exception:
        return None
    minutes = None
    if parsed.get("kind") == "interval":
        minutes = parsed.get("minutes")
    elif parsed.get("kind") == "cron":
        expr = str(parsed.get("expr") or "").split()
        if expr and _re.fullmatch(r"(\\*|\\d+(-\\d+)?)/(\\d+)|\\*", expr[0]) and len(expr) == 5 and expr[1] == "*":
            step = _re.search(r"/(\\d+)$", expr[0])
            minutes = int(step.group(1)) if step else 1
    if minutes is None or float(minutes) > _PROGRESS_CRON_MAX_MINUTES:
        return None
    if _PROGRESS_CRON_RE is None:
        _PROGRESS_CRON_RE = _re.compile(
            r"\\b(progress|status)\\s*(update|report|ping|check[- ]?in|card|note)s?\\b"
            r"|\\bupdate\\s+(me|deacon|the\\s+user|the\\s+chat|them)\\b"
            r"|\\b(report|post|send)\\s+(progress|status)\\b"
            r"|\\bkeep\\s+(me|deacon|the\\s+user)\\s+(posted|updated)\\b",
            _re.IGNORECASE)
    text = f"{a.get('name') or ''}\\n{a.get('prompt') or ''}"
    if not _PROGRESS_CRON_RE.search(text):
        return None
    return ("Refused: scheduled jobs are not a progress channel. Progress on in-flight work is shown by the "
            "live progress card on the current turn and by background-helper status. Reply in the conversation "
            "when there is real news instead of creating a recurring <=15-minute progress job. If the user "
            "explicitly asks for a standing recurring report, use a longer interval or a script-only job.")


'''


def patch_cron_progress_job_guard_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "tools/cronjob_tools.py"
    source = target.read_text()
    if MARKER in source:
        return False
    for old, new, label in ((ANCHOR, NEW, "create error gate"), (HELPER_ANCHOR, HELPER + HELPER_ANCHOR, "helper")):
        if source.count(old) != 1:
            raise RuntimeError(f"{MARKER}: anchor drift ({label})")
        source = source.replace(old, new, 1)
    compile(source, str(target), "exec")
    target.write_text(source)
    return True
