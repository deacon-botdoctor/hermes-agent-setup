#!/usr/bin/env python3
"""Read and acknowledge the local papercut inbox used by agent reflection."""

from __future__ import annotations

import argparse
import errno
import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

UTC = timezone.utc
LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.01


def hermes_home(value: str | Path | None = None) -> Path:
    if value:
        return Path(value).expanduser()
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def ledger_path(home: Path) -> Path:
    return home / "state" / "papercuts" / "events.jsonl"


def state_path(home: Path) -> Path:
    return home / "state" / "papercuts" / "reflection-state.json"


def lock_path(home: Path) -> Path:
    path = state_path(home)
    return path.with_name(f".{path.name}.lock")


def _would_block(error: OSError) -> bool:
    return error.errno in (errno.EACCES, errno.EAGAIN)


def _try_lock_unix(handle: Any) -> bool:
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if _would_block(error):
            return False
        raise
    return True


def _unlock_unix(handle: Any) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _try_lock_windows(handle: Any) -> bool:
    import msvcrt

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        if _would_block(error):
            return False
        raise
    return True


def _unlock_windows(handle: Any) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _try_advisory_lock(handle: Any) -> bool:
    return _try_lock_windows(handle) if os.name == "nt" else _try_lock_unix(handle)


def _unlock_advisory_lock(handle: Any) -> None:
    if os.name == "nt":
        _unlock_windows(handle)
    else:
        _unlock_unix(handle)


@contextmanager
def acknowledgement_lock(home: Path) -> Iterator[None]:
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        while not _try_advisory_lock(handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for acknowledgement lock: {path}")
            time.sleep(min(LOCK_POLL_SECONDS, remaining))
        try:
            yield
        finally:
            _unlock_advisory_lock(handle)


def _load_state(home: Path) -> dict[str, Any]:
    try:
        payload = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "reviewed_ids": []}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "reviewed_ids": []}
    payload.setdefault("schema_version", 1)
    reviewed_ids = payload.get("reviewed_ids")
    payload["reviewed_ids"] = (
        [value for value in reviewed_ids if isinstance(value, str)] if isinstance(reviewed_ids, list) else []
    )
    return payload


def read_events(home: Path) -> list[dict[str, Any]]:
    path = ledger_path(home)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or "")
        if not event_id.startswith("pc_") or event_id in seen:
            continue
        seen.add(event_id)
        events.append(event)
    return events


def pending_events(home: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    reviewed = {str(value) for value in _load_state(home).get("reviewed_ids", [])}
    pending = [event for event in read_events(home) if str(event.get("id")) not in reviewed]
    return pending if limit is None else pending[: max(1, limit)]


def snapshot(home: Path, *, limit: int = 100) -> dict[str, Any]:
    all_pending = pending_events(home)
    events = all_pending[: max(1, limit)]
    patterns = Counter(
        (str(event.get("kind") or "other"), str(event.get("operation") or ""), str(event.get("route") or ""))
        for event in events
    )
    return {
        "pending_count": len(all_pending),
        "event_ids": [str(event.get("id")) for event in events],
        "patterns": [
            {"kind": kind, "operation": operation, "route": route, "count": count}
            for (kind, operation, route), count in patterns.most_common(12)
        ],
        "events": events,
    }


def acknowledge(home: Path, event_ids: list[str], *, report_id: str) -> dict[str, Any]:
    valid_ids = {str(event.get("id")) for event in read_events(home)}
    accepted = [event_id for event_id in event_ids if event_id in valid_ids]
    with acknowledgement_lock(home):
        state = _load_state(home)
        reviewed = list(dict.fromkeys([*state.get("reviewed_ids", []), *accepted]))
        state.update(
            {
                "schema_version": 1,
                "reviewed_ids": reviewed,
                "last_report_id": report_id,
                "last_reviewed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )
        path = state_path(home)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return {"acknowledged": len(accepted), "report_id": report_id, "state_path": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or acknowledge the local papercut reflection inbox.")
    parser.add_argument("--home", default="")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    home = hermes_home(args.home or None)
    payload = snapshot(home)
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Papercut inbox: {payload['pending_count']} pending")
        for pattern in payload["patterns"]:
            route = f" via {pattern['route']}" if pattern["route"] else ""
            print(f"- {pattern['count']}x {pattern['kind']} / {pattern['operation']}{route}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
