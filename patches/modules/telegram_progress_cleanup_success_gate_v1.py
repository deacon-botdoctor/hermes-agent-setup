#!/usr/bin/env python3
"""Delete Telegram progress cards only after primary-response acceptance and turn success."""

from __future__ import annotations

import shutil
from pathlib import Path

MARKER = "HERMES_TELEGRAM_PROGRESS_CLEANUP_SUCCESS_GATE_v1"
BACKUP_SUFFIX = ".bak-pre-telegram-progress-cleanup-success-gate-v1"

ANCHOR = """            def _cleanup_temp_bubbles() -> None:
                async def _delete_all() -> None:
"""

REPLACEMENT = f"""            def _cleanup_temp_bubbles() -> None:
                # {MARKER}
                try:
                    from gateway import telegram_transaction_ledger as _telegram_tx
                    if not _telegram_tx.defer_progress_cleanup():
                        return
                except Exception:
                    return
                async def _delete_all() -> None:
                    if not await _telegram_tx.wait_for_progress_cleanup():
                        return
"""


def patch_telegram_progress_cleanup_success_gate_v1(hermes_dir: Path) -> bool:
    run_py = Path(hermes_dir) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    if MARKER in original:
        return False
    if original.count(ANCHOR) != 1:
        raise RuntimeError("Telegram progress cleanup callback anchor drift")
    patched = original.replace(ANCHOR, REPLACEMENT, 1)
    compile(patched, str(run_py), "exec")

    backup = Path(str(run_py) + BACKUP_SUFFIX)
    shutil.copy2(run_py, backup)
    try:
        run_py.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, run_py)
        backup.unlink(missing_ok=True)
        raise
    return True
