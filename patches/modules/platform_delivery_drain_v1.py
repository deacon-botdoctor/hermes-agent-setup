#!/usr/bin/env python3
"""Keep tracked response-delivery tasks inside the graceful drain boundary."""

from __future__ import annotations

import shutil
from pathlib import Path

MARKER = "HERMES_PLATFORM_DELIVERY_DRAIN_v2"


_NATIVE_COUNTER_OLD = '                collections = (getattr(adapter, "_background_tasks", None),\n                               getattr(adapter, "_session_tasks", None))\n                tasks = {id(task): task for collection in collections\n                         if collection is not None\n                         for task in (collection.values() if isinstance(collection, dict) else collection)}\n                count += sum(not task.done() for task in tasks.values())\n'
_NATIVE_COUNTER_PREVIOUS = '                collections = (getattr(adapter, "_background_tasks", None),\n                               getattr(adapter, "_session_tasks", None))\n                tasks = {id(task): task for collection in collections\n                         if collection is not None\n                         for task in (collection.values() if isinstance(collection, dict) else collection)}\n                # The API orphan sweeper is maintenance, not an in-flight response.\n                sweep = getattr(adapter, "_sweep_orphaned_runs", None)\n                sweep_code = getattr(getattr(sweep, "__func__", None), "__code__", None)\n                if sweep_code is not None:\n                    for key, task in list(tasks.items()):\n                        coro = getattr(task, "get_coro", lambda: None)()\n                        frame = getattr(coro, "cr_frame", None)\n                        if (getattr(coro, "cr_code", None) is sweep_code and frame is not None\n                                and frame.f_locals.get("self") is adapter):\n                            tasks.pop(key)\n                count += sum(not task.done() for task in tasks.values())\n'
_NATIVE_COUNTER_NEW = '                collections = (getattr(adapter, "_background_tasks", None),\n                               getattr(adapter, "_session_tasks", None))\n                tasks = {id(task): task for collection in collections\n                         if collection is not None\n                         for task in (collection.values() if isinstance(collection, dict) else collection)}\n                maintenance_codes = {\n                    getattr(getattr(getattr(adapter, name, None), "__func__", None), "__code__", None)\n                    for name in ("_sweep_orphaned_runs", "_heartbeat_loop")\n                } - {None}\n                for key, task in list(tasks.items()):\n                    coro = getattr(task, "get_coro", lambda: None)()\n                    frame = getattr(coro, "cr_frame", None)\n                    if (getattr(coro, "cr_code", None) in maintenance_codes and frame is not None\n                            and frame.f_locals.get("self") is adapter):\n                        tasks.pop(key)\n                count += sum(not task.done() for task in tasks.values())\n'


def _once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"{label}: anchor drift")
    return text.replace(old, new, 1)


def _write_with_backup(run_py: Path, original: str, patched: str) -> None:
    backup = Path(str(run_py) + ".bak-pre-platform-delivery-drain-v2")
    try:
        shutil.copy2(run_py, backup)
        run_py.write_text(patched, encoding="utf-8")
    except Exception:
        run_py.write_text(original, encoding="utf-8")
        backup.unlink(missing_ok=True)
        raise


_NATIVE_REPLACEMENTS = [('    # Active-work accounting\n', '    # Active-work accounting\n    def _active_platform_delivery_count(self) -> int:\n        """Live adapter delivery tasks across all profiles; stale guards do not count."""\n        seen = set()\n        count = 0\n        maps = [getattr(self, "adapters", {})]\n        maps.extend(getattr(self, "_profile_adapters", {}).values())\n        for adapters in maps:\n            for adapter in adapters.values():\n                if id(adapter) in seen:\n                    continue\n                seen.add(id(adapter))\n                collections = (getattr(adapter, "_background_tasks", None),\n                               getattr(adapter, "_session_tasks", None))\n                tasks = {id(task): task for collection in collections\n                         if collection is not None\n                         for task in (collection.values() if isinstance(collection, dict) else collection)}\n                maintenance_codes = {\n                    getattr(getattr(getattr(adapter, name, None), "__func__", None), "__code__", None)\n                    for name in ("_sweep_orphaned_runs", "_heartbeat_loop")\n                } - {None}\n                for key, task in list(tasks.items()):\n                    coro = getattr(task, "get_coro", lambda: None)()\n                    frame = getattr(coro, "cr_frame", None)\n                    if (getattr(coro, "cr_code", None) in maintenance_codes and frame is not None\n                            and frame.f_locals.get("self") is adapter):\n                        tasks.pop(key)\n                count += sum(not task.done() for task in tasks.values())\n        return count\n\n'), ('            + self._active_deferred_agent_worker_count()\n', '            + self._active_deferred_agent_worker_count()\n            + self._active_platform_delivery_count()\n'), ('"""``(agents, cron, api, deferred)`` — the four sources the drain waits on."""', '"""``(agents, cron, api, deferred, delivery)`` work awaited before teardown."""'), ('            self._active_api_run_count(), self._active_deferred_agent_worker_count(),\n', '            self._active_api_run_count(), self._active_deferred_agent_worker_count(),\n            self._active_platform_delivery_count(),\n'), ('        _cron0, _api0, _deferred0 = last_counts[1:]', '        _cron0, _api0, _deferred0, _delivery0 = last_counts[1:]'), ('not (_cron0 or _api0 or _deferred0):', 'not (_cron0 or _api0 or _deferred0 or _delivery0):'), ('agents, cron, api, deferred = self._drain_work_counts()', 'agents, cron, api, deferred, delivery = self._drain_work_counts()'), ('((agents or api or deferred) and now < deadline)', '((agents or api or deferred or delivery) and now < deadline)')]


_PERSIST_BODY = '        _write_runtime_status_quiet(active_agents=self._active_work_count())\n'
_PERSIST_REFRESH = _PERSIST_BODY + r'''
        # Delivery outlives the model turn. Refresh when each tracked task finishes,
        # without counting stale guards or reporting idle before transport completes.
        watched = getattr(self, "_delivery_status_watchers", None)
        if watched is None:
            watched = self._delivery_status_watchers = set()
        maps = [getattr(self, "adapters", {})]
        maps.extend(getattr(self, "_profile_adapters", {}).values())
        for adapters in maps:
            for adapter in adapters.values():
                for collection in (getattr(adapter, "_background_tasks", None),
                                   getattr(adapter, "_session_tasks", None)):
                    if collection is None:
                        continue
                    tasks = collection.values() if isinstance(collection, dict) else collection
                    for task in tasks:
                        if not task.done() and task not in watched:
                            watched.add(task)
                            task.add_done_callback(self._delivery_task_finished)

    def _delivery_task_finished(self, task) -> None:
        self._delivery_status_watchers.discard(task)
        self._persist_active_agents()
'''


def _refresh_delivery_status(source: str) -> str:
    start = source.index("    def _persist_active_agents(self) -> None:\n")
    end = source.index("    def _running_agent_ids(self)", start)
    body = source[start:end]
    if _PERSIST_REFRESH in body:
        return source
    if "_delivery_status_watchers" in source or body.count(_PERSIST_BODY) != 1:
        raise RuntimeError("delivery status persistence source mismatch")
    return source[:start] + body.replace(_PERSIST_BODY, _PERSIST_REFRESH, 1) + source[end:]


def _patch_current_split_delivery(root: Path) -> bool:
    """Apply the reviewed drain accounting to the post-split shutdown owner."""
    target = Path(root) / "gateway/run_shutdown.py"
    source = target.read_text(encoding="utf-8")
    for previous in (_NATIVE_COUNTER_OLD, _NATIVE_COUNTER_PREVIOUS):
        if previous in source:
            prior_fragments = [new.replace(_NATIVE_COUNTER_NEW, previous) for _old, new in _NATIVE_REPLACEMENTS]
            if MARKER not in source and not all(fragment in source for fragment in prior_fragments):
                raise RuntimeError("split platform delivery source mismatch")
            if source.count(previous) != 1:
                raise RuntimeError("split platform delivery counter drift")
            upgraded = _once(source, previous, _NATIVE_COUNTER_NEW, "split maintenance counter")
            compile(upgraded, str(target), "exec")
            _write_with_backup(target, source, upgraded)
            return True
    if MARKER in source:
        if source.count(_NATIVE_COUNTER_NEW) != 1:
            raise RuntimeError("split platform delivery counter drift")
        return False
    if all(new in source for _old, new in _NATIVE_REPLACEMENTS):
        upgraded = source.replace(
            _NATIVE_REPLACEMENTS[0][1],
            _NATIVE_REPLACEMENTS[0][1].replace(
                "    def _active_platform_delivery_count",
                f"    # {MARKER}\n    def _active_platform_delivery_count",
                1,
            ),
            1,
        )
        compile(upgraded, str(target), "exec")
        _write_with_backup(target, source, upgraded)
        return True
    patched = source
    for index, (old, new) in enumerate(_NATIVE_REPLACEMENTS):
        if index == 0:
            new = new.replace(
                "    def _active_platform_delivery_count",
                f"    # {MARKER}\n    def _active_platform_delivery_count",
                1,
            )
        patched = _once(patched, old, new, "current split delivery owner")
    compile(patched, str(target), "exec")
    _write_with_backup(target, source, patched)
    return True


def patch_platform_delivery_drain_v1(root: Path) -> bool:
    """Patch ``GatewayRunner._drain_active_agents`` to await adapter delivery.

    ``_running_agents`` becomes empty as soon as the model response is ready,
    while ``BasePlatformAdapter._process_message_background`` still owns the
    send/acceptance step. Counting live adapter tasks closes that gap without
    letting stale session guards hold every restart to the timeout.
    """

    if not (Path(root) / "gateway/run_shutdown.py").is_file():
        raise RuntimeError("platform delivery drain requires the native split shutdown owner")
    run_py = Path(root) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    patched = _refresh_delivery_status(original)
    compile(patched, str(run_py), "exec")
    changed = _patch_current_split_delivery(root)
    if patched != original:
        _write_with_backup(run_py, original, patched)
        changed = True
    return changed


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_platform_delivery_drain_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
