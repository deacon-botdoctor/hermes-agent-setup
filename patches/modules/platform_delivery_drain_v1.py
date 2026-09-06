#!/usr/bin/env python3
"""Keep tracked response-delivery tasks inside the graceful drain boundary."""

from __future__ import annotations

import shutil
import hashlib
import subprocess
from pathlib import Path

LEGACY_MARKER = "HERMES_PLATFORM_DELIVERY_DRAIN_v1"
MARKER = "HERMES_PLATFORM_DELIVERY_DRAIN_v2"


_COUNTER_OLD = '                tasks = getattr(adapter, "_background_tasks", None)\n                if tasks is None:\n                    session_tasks = getattr(adapter, "_session_tasks", None)\n                    if isinstance(session_tasks, dict):\n                        tasks = session_tasks.values()\n                    elif session_tasks is not None:\n                        tasks = session_tasks\n                if tasks is not None:\n                    active += sum(not task.done() for task in tasks)\n                else:\n                    active += len(getattr(adapter, "_active_sessions", {}))\n'
_COUNTER_NEW = '                collections = (getattr(adapter, "_background_tasks", None),\n                               getattr(adapter, "_session_tasks", None))\n                tasks = {id(task): task for collection in collections\n                         if collection is not None\n                         for task in (collection.values() if isinstance(collection, dict) else collection)}\n                if any(collection is not None for collection in collections):\n                    active += sum(not task.done() for task in tasks.values())\n                else:\n                    active += len(getattr(adapter, "_active_sessions", {}))\n'

def _once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"{label}: anchor drift")
    return text.replace(old, new, 1)


def _once_any(text: str, replacements: tuple[tuple[str, str], ...], label: str) -> str:
    for old, new in replacements:
        if old in text:
            return text.replace(old, new, 1)
    raise RuntimeError(f"{label}: anchor drift")


def _write_with_backup(run_py: Path, original: str, patched: str) -> None:
    backup = Path(str(run_py) + ".bak-pre-platform-delivery-drain-v2")
    legacy_backup = Path(str(run_py) + ".bak-pre-platform-delivery-drain-v1")
    backup_source = legacy_backup if LEGACY_MARKER in original and legacy_backup.is_file() else run_py
    try:
        shutil.copy2(backup_source, backup)
        run_py.write_text(patched, encoding="utf-8")
    except Exception:
        run_py.write_text(original, encoding="utf-8")
        backup.unlink(missing_ok=True)
        raise


def _upgrade_legacy_counter(text: str) -> str:
    old = f"""        # {LEGACY_MARKER}
        seen_adapters: set[int] = set()
        active = 0
"""
    new = f"""        # {MARKER}
        seen_adapters: set[int] = set()
        active = 0
"""
    legacy_counter = """                sessions = getattr(adapter, "_active_sessions", None)
                if sessions:
                    active += len(sessions)
"""
    task_counter = """                collections = (getattr(adapter, "_background_tasks", None),
                               getattr(adapter, "_session_tasks", None))
                tasks = {id(task): task for collection in collections
                         if collection is not None
                         for task in (collection.values() if isinstance(collection, dict) else collection)}
                if any(collection is not None for collection in collections):
                    active += sum(not task.done() for task in tasks.values())
                else:
                    active += len(getattr(adapter, "_active_sessions", {}))
"""
    patched = _once(text, old, new, "legacy marker")
    return _once_any(
        patched,
        ((legacy_counter, task_counter), (_COUNTER_OLD, task_counter), (task_counter, task_counter)),
        "legacy delivery counter",
    )


def _patch_agent_only_drain(text: str) -> str:
    patched = _once(
        text,
        "    async def _drain_active_agents(self, timeout: float) -> tuple[Dict[str, Any], bool]:\n",
        f'''    def _active_platform_delivery_count(self) -> int:
        """Return active platform message/delivery sessions across profiles."""
        # {MARKER}
        seen_adapters: set[int] = set()
        active = 0
        adapter_maps = [getattr(self, "adapters", {{}})]
        adapter_maps.extend(getattr(self, "_profile_adapters", {{}}).values())
        for adapter_map in adapter_maps:
            for adapter in adapter_map.values():
                adapter_id = id(adapter)
                if adapter_id in seen_adapters:
                    continue
                seen_adapters.add(adapter_id)
                collections = (getattr(adapter, "_background_tasks", None),
                               getattr(adapter, "_session_tasks", None))
                tasks = {{id(task): task for collection in collections
                         if collection is not None
                         for task in (collection.values() if isinstance(collection, dict) else collection)}}
                if any(collection is not None for collection in collections):
                    active += sum(not task.done() for task in tasks.values())
                else:
                    active += len(getattr(adapter, "_active_sessions", {{}}))
        return active

    async def _drain_active_agents(self, timeout: float) -> tuple[Dict[str, Any], bool]:
''',
        "delivery-count helper",
    )
    patched = _once(
        patched,
        "        last_active_count = self._running_agent_count()\n        last_status_at = 0.0\n",
        "        last_active_count = self._running_agent_count()\n"
        "        last_delivery_count = self._active_platform_delivery_count()\n"
        "        last_status_at = 0.0\n",
        "initial delivery count",
    )
    patched = _once(
        patched,
        "            nonlocal last_active_count, last_status_at\n",
        "            nonlocal last_active_count, last_delivery_count, last_status_at\n",
        "status nonlocal",
    )
    patched = _once(
        patched,
        "            active_count = self._running_agent_count()\n"
        "            if force or active_count != last_active_count or (now - last_status_at) >= 1.0:\n",
        "            active_count = self._running_agent_count()\n"
        "            delivery_count = self._active_platform_delivery_count()\n"
        "            if (\n"
        "                force\n"
        "                or active_count != last_active_count\n"
        "                or delivery_count != last_delivery_count\n"
        "                or (now - last_status_at) >= 1.0\n"
        "            ):\n",
        "status delivery count",
    )
    patched = _once(
        patched,
        "                last_active_count = active_count\n                last_status_at = now\n",
        "                last_active_count = active_count\n"
        "                last_delivery_count = delivery_count\n"
        "                last_status_at = now\n",
        "status delivery assignment",
    )
    patched = _once(
        patched,
        "        if not self._running_agents:\n",
        "        if not self._running_agents and last_delivery_count == 0:\n",
        "initial delivery gate",
    )
    patched = _once(
        patched,
        "        while self._running_agents and asyncio.get_running_loop().time() < deadline:\n",
        "        while (\n"
        "            self._running_agents or self._active_platform_delivery_count()\n"
        "        ) and asyncio.get_running_loop().time() < deadline:\n",
        "drain loop delivery gate",
    )
    return _once(
        patched,
        "        timed_out = bool(self._running_agents)\n",
        "        timed_out = bool(self._running_agents) or bool(self._active_platform_delivery_count())\n",
        "timeout delivery gate",
    )


NATIVE_BASE = "d3630f853239e8c41ce7201e09fbdf39bcbc5431"
# Exact whole-file pre/post identities: pristine native and durable-carrier composition.
_NATIVE_IMAGES = {'c280164863bc33e99c0dd24a030222b618d9533a1ef8c3f1ca1fd73c008b808f': '1685ac84071919f885263c83296fe3825cb6d455c13134da69cd3fa5f320484d', '1db9c7e2985260a4da985ffee6708e8af42e8ecc7abe6f40aff7ca19effab34c': 'ea35a793b415282f4d63f269ab2f892ec7c0c1bc85b0b9b2a524a47a2ddcf8c9'}
_NATIVE_REPLACEMENTS = [('    # Active-work accounting\n', '    # Active-work accounting\n    def _active_platform_delivery_count(self) -> int:\n        """Live adapter delivery tasks across all profiles; stale guards do not count."""\n        seen = set()\n        count = 0\n        maps = [getattr(self, "adapters", {})]\n        maps.extend(getattr(self, "_profile_adapters", {}).values())\n        for adapters in maps:\n            for adapter in adapters.values():\n                if id(adapter) in seen:\n                    continue\n                seen.add(id(adapter))\n                collections = (getattr(adapter, "_background_tasks", None),\n                               getattr(adapter, "_session_tasks", None))\n                tasks = {id(task): task for collection in collections\n                         if collection is not None\n                         for task in (collection.values() if isinstance(collection, dict) else collection)}\n                count += sum(not task.done() for task in tasks.values())\n        return count\n\n'), ('            + self._active_deferred_agent_worker_count()\n', '            + self._active_deferred_agent_worker_count()\n            + self._active_platform_delivery_count()\n'), ('"""``(agents, cron, api, deferred)`` — the four sources the drain waits on."""', '"""``(agents, cron, api, deferred, delivery)`` work awaited before teardown."""'), ('            self._active_api_run_count(), self._active_deferred_agent_worker_count(),\n', '            self._active_api_run_count(), self._active_deferred_agent_worker_count(),\n            self._active_platform_delivery_count(),\n'), ('        _cron0, _api0, _deferred0 = last_counts[1:]', '        _cron0, _api0, _deferred0, _delivery0 = last_counts[1:]'), ('not (_cron0 or _api0 or _deferred0):', 'not (_cron0 or _api0 or _deferred0 or _delivery0):'), ('agents, cron, api, deferred = self._drain_work_counts()', 'agents, cron, api, deferred, delivery = self._drain_work_counts()'), ('((agents or api or deferred) and now < deadline)', '((agents or api or deferred or delivery) and now < deadline)')]


def _patch_native_delivery(root: Path) -> bool:
    target = Path(root) / "gateway/run_shutdown.py"
    source = target.read_text()
    digest = hashlib.sha256(source.encode()).hexdigest()
    if digest in _NATIVE_IMAGES.values():
        return False
    if digest not in _NATIVE_IMAGES:
        raise RuntimeError("native platform delivery drain source mismatch")
    patched = source
    for old, new in _NATIVE_REPLACEMENTS:
        patched = _once(patched, old, new, "native delivery owner")
    if hashlib.sha256(patched.encode()).hexdigest() != _NATIVE_IMAGES[digest]:
        raise RuntimeError("native platform delivery drain postimage mismatch")
    target.write_text(patched)
    return True


def patch_platform_delivery_drain_v1(root: Path) -> bool:
    """Patch ``GatewayRunner._drain_active_agents`` to await adapter delivery.

    ``_running_agents`` becomes empty as soon as the model response is ready,
    while ``BasePlatformAdapter._process_message_background`` still owns the
    send/acceptance step. Counting live adapter tasks closes that gap without
    letting stale session guards hold every restart to the timeout.
    """

    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode == 0 and head.stdout.strip() == NATIVE_BASE:
        return _patch_native_delivery(root)
    run_py = Path(root) / "gateway/run.py"
    original = run_py.read_text(encoding="utf-8")
    if MARKER in original:
        if original.count(_COUNTER_NEW) == 1:
            return False
        if original.count(_COUNTER_OLD) != 1:
            raise RuntimeError("platform delivery task counter drift")
        _write_with_backup(run_py, original, original.replace(_COUNTER_OLD, _COUNTER_NEW, 1))
        return True
    if LEGACY_MARKER in original:
        patched = _upgrade_legacy_counter(original)
        _write_with_backup(run_py, original, patched)
        return True

    if "        last_api_count = self._active_api_run_count()\n" not in original:
        patched = _patch_agent_only_drain(original)
        _write_with_backup(run_py, original, patched)
        return True

    delivery_helper = f'''    def _active_platform_delivery_count(self) -> int:
        """Return active platform message/delivery sessions across profiles."""
        # {MARKER}
        seen_adapters: set[int] = set()
        active = 0
        adapter_maps = [getattr(self, "adapters", {{}})]
        adapter_maps.extend(
            getattr(self, "_profile_adapters", {{}}).values()
        )
        for adapter_map in adapter_maps:
            for adapter in adapter_map.values():
                adapter_id = id(adapter)
                if adapter_id in seen_adapters:
                    continue
                seen_adapters.add(adapter_id)
                collections = (getattr(adapter, "_background_tasks", None),
                               getattr(adapter, "_session_tasks", None))
                tasks = {{id(task): task for collection in collections
                         if collection is not None
                         for task in (collection.values() if isinstance(collection, dict) else collection)}}
                if any(collection is not None for collection in collections):
                    active += sum(not task.done() for task in tasks.values())
                else:
                    active += len(getattr(adapter, "_active_sessions", {{}}))
        return active

'''
    patched = _once_any(
        original,
        (
            (
                "    async def _drain_active_agents(self, timeout: float) -> tuple[Dict[str, Any], bool]:\n",
                delivery_helper
                + "    async def _drain_active_agents(self, timeout: float) -> tuple[Dict[str, Any], bool]:\n",
            ),
            (
                "    async def _drain_active_agents(\n"
                "        self, timeout: float, cron_timeout: Optional[float] = None\n"
                "    ) -> tuple[Dict[str, Any], bool]:\n",
                delivery_helper + "    async def _drain_active_agents(\n"
                "        self, timeout: float, cron_timeout: Optional[float] = None\n"
                "    ) -> tuple[Dict[str, Any], bool]:\n",
            ),
        ),
        "delivery-count helper",
    )
    patched = _once(
        patched,
        "        last_api_count = self._active_api_run_count()\n        last_status_at = 0.0\n",
        "        last_api_count = self._active_api_run_count()\n"
        "        last_delivery_count = self._active_platform_delivery_count()\n"
        "        last_status_at = 0.0\n",
        "initial delivery count",
    )
    patched = _once(
        patched,
        "            nonlocal last_active_count, last_cron_count, last_api_count, last_status_at\n",
        "            nonlocal last_active_count, last_cron_count, last_api_count, "
        "last_delivery_count, last_status_at\n",
        "status nonlocal",
    )
    patched = _once(
        patched,
        "            api_count = self._active_api_run_count()\n            if (\n",
        "            api_count = self._active_api_run_count()\n"
        "            delivery_count = self._active_platform_delivery_count()\n"
        "            if (\n",
        "status delivery count",
    )
    patched = _once(
        patched,
        "                or api_count != last_api_count\n                or (now - last_status_at) >= 1.0\n",
        "                or api_count != last_api_count\n"
        "                or delivery_count != last_delivery_count\n"
        "                or (now - last_status_at) >= 1.0\n",
        "status delivery change",
    )
    patched = _once(
        patched,
        "                last_api_count = api_count\n                last_status_at = now\n",
        "                last_api_count = api_count\n"
        "                last_delivery_count = delivery_count\n"
        "                last_status_at = now\n",
        "status delivery assignment",
    )
    patched = _once(
        patched,
        "        # API-server / desk sessions have the same structural gap (#63529).\n"
        "        if not self._running_agents and last_cron_count == 0 and last_api_count == 0:\n",
        "        # API-server / desk sessions have the same structural gap (#63529).\n"
        "        # Platform message tasks outlive _running_agents while the response is\n"
        "        # being sent and transport acceptance is recorded.  Disconnecting an\n"
        "        # adapter in that window drops a completed response during restart.\n"
        "        if (\n"
        "            not self._running_agents\n"
        "            and last_cron_count == 0\n"
        "            and last_api_count == 0\n"
        "            and last_delivery_count == 0\n"
        "        ):\n",
        "initial delivery gate",
    )
    patched = _once_any(
        patched,
        (
            (
                "                or self._active_api_run_count()\n            )\n",
                "                or self._active_api_run_count()\n"
                "                or self._active_platform_delivery_count()\n"
                "            )\n",
            ),
            (
                "            self._running_agents or self._active_cron_job_count() or self._active_api_run_count()\n",
                "            self._running_agents\n"
                "            or self._active_cron_job_count()\n"
                "            or self._active_api_run_count()\n"
                "            or self._active_platform_delivery_count()\n",
            ),
            (
                "                len(self._running_agents) or self._active_api_run_count()\n"
                "            ) and now < deadline:\n",
                "                len(self._running_agents)\n"
                "                or self._active_api_run_count()\n"
                "                or self._active_platform_delivery_count()\n"
                "            ) and now < deadline:\n",
            ),
        ),
        "drain loop delivery gate",
    )
    patched = _once_any(
        patched,
        (
            (
                "            or bool(self._active_api_run_count())\n        )\n",
                "            or bool(self._active_api_run_count())\n"
                "            or bool(self._active_platform_delivery_count())\n"
                "        )\n",
            ),
            (
                "            bool(self._running_agents) or "
                "bool(self._active_cron_job_count()) or "
                "bool(self._active_api_run_count())\n",
                "            bool(self._running_agents)\n"
                "            or bool(self._active_cron_job_count())\n"
                "            or bool(self._active_api_run_count())\n"
                "            or bool(self._active_platform_delivery_count())\n",
            ),
        ),
        "timeout delivery gate",
    )

    _write_with_backup(run_py, original, patched)
    return True


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
