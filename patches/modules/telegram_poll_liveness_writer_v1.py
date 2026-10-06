"""Export Telegram polling liveness for out-of-process health probes.

[HERMES_TELEGRAM_POLL_LIVENESS_WRITER_v1]

Hermes' native polling heartbeat detects and repairs polling wedges, but it
does not export the positive heartbeat consumed by
``telegram-polling-health-probe.py``. This patch adds only that writer to the
native heartbeat loop. It does not add another loop, watchdog, or recovery
path.
"""
from __future__ import annotations

import ast
import shutil
import time
from pathlib import Path

MARKER = "HERMES_TELEGRAM_POLL_LIVENESS_WRITER_v1"

_DIRECT_FILE_HELPER_METHOD = '''    def _hermes_write_poll_liveness_stamp(self, *, require_updater: bool = False) -> None:
        """Export a positive polling heartbeat for out-of-process watchdogs.

        [HERMES_TELEGRAM_POLL_LIVENESS_WRITER_v1]
        Best-effort by contract: a state-write failure must never break native
        Telegram polling or its recovery loop.
        """
        try:
            import json as _json
            import os as _os
            from datetime import datetime as _dt, timezone as _tz
            from pathlib import Path as _Path

            # A running updater/getMe does not prove a new polling generation ready.
            if getattr(self, "_send_path_degraded", False):
                return
            updater = getattr(self._app, "updater", None) if self._app else None
            updater_running = bool(getattr(updater, "running", False))
            if require_updater and not updater_running:
                return
            now = _dt.now(_tz.utc).isoformat().replace("+00:00", "Z")
            home = _Path(_os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes")))
            payload = {
                "platform": "telegram",
                "adapter": getattr(self, "name", "telegram"),
                "source": "heartbeat",
                "last_poll_probe_at": now,
                "updater_running": updater_running,
                "bot_api_ok": True,
            }
            sidecar = home / "state" / "telegram-polling-health.json"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
            tmp.write_text(_json.dumps(payload, sort_keys=True), encoding="utf-8")
            tmp.replace(sidecar)
        except Exception as exc:
            try:
                logger.debug("[%s] poll-liveness sidecar write failed: %s", self.name, exc)
            except Exception:
                pass
            return

        try:
            from gateway.status import (
                _get_runtime_status_path,
                _read_json_file,
                _write_json_file,
            )

            state_path = _get_runtime_status_path()
            state = _read_json_file(state_path)
            if isinstance(state, dict):
                platforms = state.setdefault("platforms", {})
                telegram_payload = platforms.setdefault("telegram", {})
                telegram_payload["last_successful_poll_at"] = now
                _write_json_file(state_path, state)
        except Exception as exc:
            try:
                logger.debug("[%s] poll-liveness gateway_state stamp failed: %s", self.name, exc)
            except Exception:
                pass

'''

_PREVIOUS_HELPER_METHOD = _DIRECT_FILE_HELPER_METHOD.replace('            # A running updater/getMe does not prove a new polling generation ready.\n            if getattr(self, "_send_path_degraded", False):\n                return\n', "", 1)
_DIRECT_WRITE_START = _DIRECT_FILE_HELPER_METHOD.index("            from gateway.status import (")
_DIRECT_WRITE_END = _DIRECT_FILE_HELPER_METHOD.index("        except Exception as exc:", _DIRECT_WRITE_START)
HELPER_METHOD = (
    _DIRECT_FILE_HELPER_METHOD[:_DIRECT_WRITE_START]
    + '            from gateway.status import publish_runtime_status\n\n'
    + '            publish_runtime_status(platform="telegram", last_successful_poll_at=now)\n'
    + _DIRECT_FILE_HELPER_METHOD[_DIRECT_WRITE_END:]
)


_HEARTBEAT_ONLY_HELPER_METHOD = HELPER_METHOD
HELPER_METHOD = HELPER_METHOD.replace(
    '                "bot_api_ok": True,\n',
    '                "bot_api_ok": True,\n'
    '                "ingress": {\n'
    '                    "received": getattr(self, "_updates_received_total", 0),\n'
    '                    "dispatched": getattr(self, "_updates_dispatched_total", 0),\n'
    '                    "generation": getattr(self, "_polling_generation", 0),\n'
    '                    "stalled": (not getattr(self, "_webhook_mode", False)\n'
    '                        and getattr(self, "_ingress_stalled_heartbeats", 0) >= _INGRESS_DISPATCH_STALL_HEARTBEATS\n'
    '                        and getattr(self, "_updates_received_total", 0) > getattr(self, "_updates_dispatched_total", 0)),\n'
    '                },\n',
).replace(
    'publish_runtime_status(platform="telegram", last_successful_poll_at=now)',
    'publish_runtime_status(platform="telegram", last_successful_poll_at=now, telegram_ingress=payload["ingress"])',
)

_STATUS_PARAMETER = "    multiplex_standalone_reason: Any = _UNSET,\n"
_STATUS_PARAMETER_PATCHED = _STATUS_PARAMETER + "    last_successful_poll_at: Any = _UNSET,\n"
_STATUS_FIELD = '                ("listener_base", listener_base, None),\n'
_STATUS_FIELD_PATCHED = _STATUS_FIELD + '                ("last_successful_poll_at", last_successful_poll_at, None),\n'


def _patched_status_source(source: str) -> str:
    # Use the native snapshot and queued writer so later status updates retain
    # the heartbeat. Direct file writes are overwritten by the native cache.
    for old, new, label in (
        (_STATUS_PARAMETER, _STATUS_PARAMETER_PATCHED, "status parameter"),
        (_STATUS_FIELD, _STATUS_FIELD_PATCHED, "status field"),
        ("    last_successful_poll_at: Any = _UNSET,\n",
         "    last_successful_poll_at: Any = _UNSET,\n    telegram_ingress: Any = _UNSET,\n", "ingress parameter"),
        ('                ("last_successful_poll_at", last_successful_poll_at, None),\n',
         '                ("last_successful_poll_at", last_successful_poll_at, None),\n                ("ingress", telegram_ingress, None),\n', "ingress field"),
    ):
        if new not in source:
            source = _replace_once(source, old, new, label)
        elif source.count(new) != 1:
            raise RuntimeError(f"[telegram_poll_liveness_writer] {label} drifted")
    ast.parse(source)
    return source

LOOP_DEF_ANCHOR = "    async def _polling_heartbeat_loop(self) -> None:"
PROBE_TIMEOUT_ANCHOR = (
    "        PROBE_TIMEOUT = 15        # seconds before declaring the path dead\n"
)
INITIAL_STAMP = (
    PROBE_TIMEOUT_ANCHOR
    + "\n"
    + "        # [HERMES_TELEGRAM_POLL_LIVENESS_WRITER_v1] export a fresh\n"
    + "        # heartbeat immediately after polling starts.\n"
    + "        self._hermes_write_poll_liveness_stamp(require_updater=True)\n"
)
PROBE_SUCCESS_ANCHOR = (
    "                await asyncio.wait_for(bot.get_me(), PROBE_TIMEOUT)\n"
)
PROBE_SUCCESS_STAMP = (
    PROBE_SUCCESS_ANCHOR
    + "                # [HERMES_TELEGRAM_POLL_LIVENESS_WRITER_v1] export only\n"
    + "                # after the native Bot API probe succeeds.\n"
    + "                self._hermes_write_poll_liveness_stamp(require_updater=True)\n"
)
_PREVIOUS_PROBE_SUCCESS_STAMP = PROBE_SUCCESS_STAMP.replace(
    "(require_updater=True)", "()", 1
)


def _replace_once(src: str, old: str, new: str, label: str) -> str:
    count = src.count(old)
    if count != 1:
        raise RuntimeError(
            f"[telegram_poll_liveness_writer] {label} anchor found "
            f"{count} times (expected 1)"
        )
    return src.replace(old, new, 1)


def patch_telegram_poll_liveness_writer_v1(hermes_dir: Path) -> bool:
    target = hermes_dir / "plugins" / "platforms" / "telegram" / "adapter.py"
    if not target.exists():
        raise RuntimeError(
            "[telegram_poll_liveness_writer] native Telegram adapter missing: "
            f"{target}"
        )

    status_path = hermes_dir / "gateway" / "status.py"
    status_original = status_path.read_text(encoding="utf-8")
    status_source = _patched_status_source(status_original)
    src = target.read_text(encoding="utf-8")
    original = src
    if "_INGRESS_DISPATCH_STALL_HEARTBEATS =" not in src:
        raise RuntimeError("[telegram_poll_liveness_writer] native ingress threshold missing")
    if MARKER in src:
        if _PREVIOUS_PROBE_SUCCESS_STAMP in src:
            src = _replace_once(src, _PREVIOUS_PROBE_SUCCESS_STAMP, PROBE_SUCCESS_STAMP, "installed probe upgrade")
        if INITIAL_STAMP not in src or PROBE_SUCCESS_STAMP not in src:
            raise RuntimeError("[telegram_poll_liveness_writer] installed hooks drifted")
        if HELPER_METHOD not in src:
            old = (_HEARTBEAT_ONLY_HELPER_METHOD if _HEARTBEAT_ONLY_HELPER_METHOD in src
                   else _DIRECT_FILE_HELPER_METHOD if _DIRECT_FILE_HELPER_METHOD in src
                   else _PREVIOUS_HELPER_METHOD)
            src = _replace_once(src, old, HELPER_METHOD, "installed helper upgrade")
    else:
        src = _replace_once(src, LOOP_DEF_ANCHOR, HELPER_METHOD + LOOP_DEF_ANCHOR, "helper method")
        src = _replace_once(src, PROBE_TIMEOUT_ANCHOR, INITIAL_STAMP, "initial stamp")
        src = _replace_once(src, PROBE_SUCCESS_ANCHOR, PROBE_SUCCESS_STAMP, "successful probe stamp")
    ast.parse(src)
    changed = False
    # Validate both files before changing either one.
    for path, before, after in ((status_path, status_original, status_source), (target, original, src)):
        if before == after:
            continue
        backup = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}-poll-liveness-writer")
        shutil.copy2(path, backup)
        path.write_text(after, encoding="utf-8")
        changed = True
    return changed
