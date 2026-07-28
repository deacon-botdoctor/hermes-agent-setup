#!/usr/bin/env python3
"""Keep the v0.19 gateway compatible with an older in-process scheduler."""

from __future__ import annotations

import shutil
from pathlib import Path


MARKER = "HERMES_CRON_SCHEDULER_CAN_DISPATCH_COMPAT_v1"
ANCHOR = '''    if isinstance(cron_provider, InProcessCronScheduler):
        cron_start_kwargs["can_dispatch"] = lambda: not (
            runner._draining or runner._external_drain_active
        )
'''
REPLACEMENT = f'''    if isinstance(cron_provider, InProcessCronScheduler):
        # {MARKER}: a mixed runtime can retain an older built-in provider.
        # Give the drain gate only to the concrete provider that supports it;
        # otherwise retain its native start contract instead of losing cron.
        try:
            _supports_can_dispatch = "can_dispatch" in inspect.signature(
                cron_provider.start
            ).parameters
        except (TypeError, ValueError):
            _supports_can_dispatch = False
        if _supports_can_dispatch:
            cron_start_kwargs["can_dispatch"] = lambda: not (
                runner._draining or runner._external_drain_active
            )
        else:
            logger.warning(
                "In-process cron scheduler lacks can_dispatch; using native start contract"
            )
'''


def patch_cron_scheduler_can_dispatch_compat_v1(root: Path) -> bool:
    """Guard the optional drain callback against an older provider signature."""
    run_py = Path(root) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    if MARKER in original:
        return False
    if original.count(ANCHOR) != 1:
        raise RuntimeError("cron scheduler start anchor drift")
    patched = original.replace(ANCHOR, REPLACEMENT, 1)
    backup = Path(str(run_py) + ".bak-pre-cron-scheduler-can-dispatch-compat-v1")
    shutil.copy2(run_py, backup)
    try:
        run_py.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, run_py)
        backup.unlink(missing_ok=True)
        raise
    return True
