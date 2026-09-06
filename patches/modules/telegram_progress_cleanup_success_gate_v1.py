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


NATIVE_TEST_BEFORE = """    await _fire_post_delivery_cb(cb)
    for _ in range(20):
"""
NATIVE_TEST_AFTER = """    # Golden requires primary-response acceptance before cleanup can resolve.
    from gateway import telegram_transaction_ledger as ledger
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ledger.begin(SimpleNamespace(source=source, platform_update_id=1, message_id="inbound"), "test")
    ledger.model_finished()
    ledger.accepted("primary-final")
    ledger.finish()
    await _fire_post_delivery_cb(cb)
    ledger.finalize_progress_cleanup()
    for _ in range(20):
"""


def patch_telegram_progress_cleanup_success_gate_v1(hermes_dir: Path) -> bool:
    split = Path(hermes_dir) / "gateway/run_turn.py"
    run_py = split if split.is_file() else Path(hermes_dir) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    test_changed = False
    test_path = Path(hermes_dir) / "tests/gateway/test_run_cleanup_progress.py"
    if split.is_file() and test_path.is_file():
        test_source = test_path.read_text(encoding="utf-8")
        if NATIVE_TEST_AFTER not in test_source and NATIVE_TEST_BEFORE in test_source:
            test_path.write_text(test_source.replace(NATIVE_TEST_BEFORE, NATIVE_TEST_AFTER, 1), encoding="utf-8")
            test_changed = True
    if MARKER in original:
        return test_changed
    anchor, replacement = ANCHOR, REPLACEMENT
    if split.is_file():
        anchor = "".join(line[4:] if line.startswith("    ") else line for line in anchor.splitlines(keepends=True))
        replacement = "".join(line[4:] if line.startswith("    ") else line for line in replacement.splitlines(keepends=True))
    if original.count(anchor) != 1:
        raise RuntimeError("Telegram progress cleanup callback anchor drift")
    patched = original.replace(anchor, replacement, 1)
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
