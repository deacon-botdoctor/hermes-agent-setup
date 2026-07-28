#!/usr/bin/env python3
"""Keep durable-drain startup compatible with Windows and retained old rows."""

from __future__ import annotations

import shutil
from pathlib import Path


MARKER = "HERMES_DURABLE_DRAIN_COMPATIBILITY_v1"
TEST_MARKER = "HERMES_DURABLE_DRAIN_COMPATIBILITY_TEST_v1"
DIRECTORY_ANCHOR = '''def _open_trusted_directory(path: Path) -> os.stat_result:
    # Linux PrivateTmp mount namespaces can deny a read-open of their synthetic
'''
DIRECTORY_REPLACEMENT = f'''def _open_trusted_directory(path: Path) -> os.stat_result:
    if os.name == "nt":  # {MARKER}
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            raise PermissionError(f"Durable inbox directory is a symlink: {{path}}")
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if reparse_flag and (
            getattr(path_stat, "st_file_attributes", 0) & reparse_flag
        ):
            raise PermissionError(
                f"Durable inbox directory is a reparse point: {{path}}"
            )
        if not stat.S_ISDIR(path_stat.st_mode):
            raise PermissionError(
                f"Durable inbox directory is not a directory: {{path}}"
            )
        return path_stat

    # Linux PrivateTmp mount namespaces can deny a read-open of their synthetic
'''
PENDING_ANCHOR = '''        rows = _read_rows(path)
        if any(parsed is None for _, parsed in rows):
            raise ValueError("durable drain inbox contains malformed rows")
        return [
'''
PENDING_REPLACEMENT = '''        rows = _read_rows(path)
        return [
'''
ACK_TEST_ANCHOR = '''    assert acknowledge(queue_id)  # ty:ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="malformed rows"):
        pending_records()
    assert inbox_path().read_text(encoding="utf-8") == "{not-json\\n"
'''
ACK_TEST_REPLACEMENT = f'''    assert acknowledge(queue_id)  # ty:ignore[invalid-argument-type]
    assert pending_records() == []  # {TEST_MARKER}
    assert inbox_path().read_text(encoding="utf-8") == "{{not-json\\n"
'''
REPLAY_SETUP_ANCHOR = '''    persist_event(event, session_key, reason="test-drain")

    runner, adapter = make_restart_runner()
'''
REPLAY_SETUP_REPLACEMENT = '''    persist_event(event, session_key, reason="test-drain")
    with inbox_path().open("a", encoding="utf-8") as handle:
        handle.write("{not-json\\n")

    runner, adapter = make_restart_runner()
'''
REPLAY_ASSERTION_ANCHOR = '''    assert committed_message_ids == {event.message_id}
    assert pending_records() == []
'''
REPLAY_ASSERTION_REPLACEMENT = '''    assert committed_message_ids == {event.message_id}
    assert pending_records() == []
    assert inbox_path().read_text(encoding="utf-8") == "{not-json\\n"
'''
REPLAY_DRAIN_ANCHOR = '''    runner._run_startup_resume_event = AsyncMock()  # ty:ignore[invalid-assignment]

    drained = await runner._drain_persisted_drain_inbox()

    assert drained == 1
'''
REPLAY_DRAIN_REPLACEMENT = '''    runner._run_startup_resume_event = AsyncMock()  # ty:ignore[invalid-assignment]
    runner._startup_restore_in_progress = True
    runner._startup_restore_tasks = []
    runner._schedule_resume_pending_sessions = lambda: 0  # ty:ignore[invalid-assignment]

    await runner._finish_startup_restore()

    assert runner._startup_restore_in_progress is False
'''


def patch_durable_drain_compatibility_v1(root: Path) -> bool:
    """Support Windows roots and retain old rows without blocking startup."""
    root = Path(root)
    target = root / "gateway/drain_inbox.py"
    test_target = root / "tests/gateway/test_drain_inbox.py"
    original = target.read_text(encoding="utf-8")
    test_original = test_target.read_text(encoding="utf-8")
    runtime_marked = MARKER in original
    test_marked = TEST_MARKER in test_original
    if runtime_marked and test_marked:
        return False
    if runtime_marked or test_marked:
        raise RuntimeError("durable drain compatibility install is incomplete")
    if original.count(DIRECTORY_ANCHOR) != 1:
        raise RuntimeError("durable drain directory anchor drift")
    if original.count(PENDING_ANCHOR) != 1:
        raise RuntimeError("durable drain pending-row anchor drift")
    if test_original.count(ACK_TEST_ANCHOR) != 1:
        raise RuntimeError("durable drain malformed-row test anchor drift")
    if test_original.count(REPLAY_SETUP_ANCHOR) != 1:
        raise RuntimeError("durable drain replay setup test anchor drift")
    if test_original.count(REPLAY_ASSERTION_ANCHOR) != 1:
        raise RuntimeError("durable drain replay assertion test anchor drift")
    if test_original.count(REPLAY_DRAIN_ANCHOR) != 1:
        raise RuntimeError("durable drain replay startup test anchor drift")
    patched = original.replace(DIRECTORY_ANCHOR, DIRECTORY_REPLACEMENT, 1)
    patched = patched.replace(PENDING_ANCHOR, PENDING_REPLACEMENT, 1)
    test_patched = test_original.replace(ACK_TEST_ANCHOR, ACK_TEST_REPLACEMENT, 1)
    test_patched = test_patched.replace(
        REPLAY_SETUP_ANCHOR,
        REPLAY_SETUP_REPLACEMENT,
        1,
    )
    test_patched = test_patched.replace(
        REPLAY_ASSERTION_ANCHOR,
        REPLAY_ASSERTION_REPLACEMENT,
        1,
    )
    test_patched = test_patched.replace(
        REPLAY_DRAIN_ANCHOR,
        REPLAY_DRAIN_REPLACEMENT,
        1,
    )
    backup = Path(str(target) + ".bak-pre-durable-drain-compatibility-v1")
    test_backup = Path(
        str(test_target) + ".bak-pre-durable-drain-compatibility-v1"
    )
    shutil.copy2(target, backup)
    shutil.copy2(test_target, test_backup)
    try:
        target.write_text(patched, encoding="utf-8")
        test_target.write_text(test_patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, target)
        shutil.copy2(test_backup, test_target)
        backup.unlink(missing_ok=True)
        test_backup.unlink(missing_ok=True)
        raise
    return True


if __name__ == "__main__":
    import sys

    print(patch_durable_drain_compatibility_v1(Path(sys.argv[1])))
