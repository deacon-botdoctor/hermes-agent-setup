#!/usr/bin/env python3
"""Hermes host hygiene loop (audit + bounded apply via existing tools).

Design: kit/docs/host-hygiene-loop.md

Default: dry-run audit of host pressure + orphan candidates.
--apply: runs already-deployed specialized safe tools only:
  - hermes-host-steward.py (exact task-owned leases only)
  - agent-browser-orphan-reaper.py
  - disposable hermes tmp cleanup (age-gated)
Never deletes user data / auth / configs. Never touches gateway.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TCC_MSG_ID_RE = re.compile(r"\bmsgID=([^,\s]+)")
_TCC_SERVICE_RE = re.compile(r"\bservice=kTCCService([^,\s]+)")
_TCC_RESULT_RE = re.compile(r"\bauthValue=(\d+),\s*authReason=(\d+)")
_TCC_PROCESS_RE = re.compile(
    r"\b(responsible|accessing)=\{<?TCCDProcess:\s*(.*?)"
    r"(?=>?\},\s*(?:responsible|accessing|requesting)=|>?\},\s*\},|$)"
)
_TCC_FIELD_RE = re.compile(r"\b(identifier|pid|auid|euid|binary_path)=([^,}>]+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@contextlib.contextmanager
def single_instance_lease(path: Path):
    """Yield whether this process owns the non-blocking worker lease."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _compact_tcc_process(raw: str) -> dict[str, Any]:
    fields = {key: value.strip() for key, value in _TCC_FIELD_RE.findall(raw)}
    result: dict[str, Any] = {}
    if fields.get("identifier"):
        identifier = fields["identifier"]
        result["identifier"] = (
            "[path-redacted]"
            if "/" in identifier or "\\" in identifier or identifier.casefold().startswith("file:")
            else identifier
        )
    try:
        result["pid"] = int(fields["pid"])
    except (KeyError, ValueError):
        pass
    if fields.get("binary_path"):
        result["binary"] = Path(fields["binary_path"]).name
    uids: set[int] = set()
    for key in ("auid", "euid"):
        try:
            uids.add(int(fields[key]))
        except (KeyError, ValueError):
            pass
    result["_uids"] = sorted(uids)
    return result


def parse_tcc_access_events(
    raw: str, *, self_uid: int, limit: int = 50
) -> list[dict[str, Any]]:
    """Pair non-preflight TCC requests with redacted process attribution."""
    contexts: dict[str, dict[str, Any]] = {}
    attributions: dict[str, dict[str, dict[str, Any]]] = {}
    results: dict[str, tuple[int, int]] = {}
    for line in raw.splitlines():
        message_id_match = _TCC_MSG_ID_RE.search(line)
        if not message_id_match:
            continue
        message_id = message_id_match.group(1)
        if "AUTHREQ_CTX:" in line:
            service_match = _TCC_SERVICE_RE.search(line)
            if not service_match or "preflight=no" not in line:
                continue
            service = service_match.group(1)
            timestamp = line[:23].replace(" ", "T", 1) + "Z"
            contexts[message_id] = {
                "timestamp": timestamp,
                "message_id": message_id,
                "service": service,
            }
        elif "AUTHREQ_ATTRIBUTION:" in line:
            processes: dict[str, dict[str, Any]] = {}
            for role, process_raw in _TCC_PROCESS_RE.findall(line):
                compact = _compact_tcc_process(process_raw)
                if compact:
                    processes[role] = compact
            attributions[message_id] = processes
        elif "AUTHREQ_RESULT:" in line:
            result_match = _TCC_RESULT_RE.search(line)
            if result_match:
                results[message_id] = (int(result_match.group(1)), int(result_match.group(2)))

    events: list[dict[str, Any]] = []
    for message_id, event in contexts.items():
        attributed = attributions.get(message_id, {})
        if not any(self_uid in process.get("_uids", []) for process in attributed.values()):
            continue
        if message_id in results:
            event["auth_value"], event["auth_reason"] = results[message_id]
        for role in ("responsible", "accessing"):
            if role in attributed:
                event[role] = {
                    key: value
                    for key, value in attributed[role].items()
                    if key != "_uids"
                }
        events.append(event)
    if limit <= 0:
        return []
    return sorted(events, key=lambda event: str(event["timestamp"]))[-limit:]


def audit_macos_tcc_access(
    lookback_minutes: int,
    *,
    platform_name: str | None = None,
    self_uid: int | None = None,
    runner=subprocess.run,
) -> dict[str, Any]:
    platform_name = platform_name or sys.platform
    if platform_name != "darwin":
        return {"supported": False, "events": []}
    predicate = (
        'subsystem == "com.apple.TCC" AND '
        '(eventMessage CONTAINS "AUTHREQ_CTX" OR '
        'eventMessage CONTAINS "AUTHREQ_ATTRIBUTION" OR '
        'eventMessage CONTAINS "AUTHREQ_RESULT")'
    )
    try:
        result = runner(
            [
                "/usr/bin/log",
                "show",
                "--last",
                f"{max(1, lookback_minutes)}m",
                "--style",
                "compact",
                "--timezone",
                "UTC",
                "--predicate",
                predicate,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except Exception as exc:
        return {"supported": True, "available": False, "events": [], "error": str(exc)[:200]}
    if result.returncode != 0:
        error = (result.stderr or f"log exited {result.returncode}").strip()[:200]
        return {"supported": True, "available": False, "events": [], "error": error}
    return {
        "supported": True,
        "available": True,
        "lookback_minutes": max(1, lookback_minutes),
        "events": parse_tcc_access_events(
            result.stdout or "", self_uid=os.getuid() if self_uid is None else self_uid
        ),
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def parse_etime(raw: str) -> int:
    days = 0
    if "-" in raw:
        d, raw = raw.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            days = 0
    bits = raw.split(":")
    try:
        nums = [int(b) for b in bits]
    except ValueError:
        return 0
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0, 0, nums[0]
    else:
        return 0
    return days * 86400 + h * 3600 + m * 60 + s


@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    etimes: int
    rss_kb: int
    pcpu: float
    tty: str
    cmd: str


def load_procs() -> list[Proc]:
    r = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,uid=,etime=,rss=,pcpu=,tty=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[Proc] = []
    if r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            out.append(
                Proc(
                    int(parts[0]),
                    int(parts[1]),
                    int(parts[2]),
                    parse_etime(parts[3]),
                    int(parts[4]),
                    float(parts[5]),
                    parts[6],
                    parts[7],
                )
            )
        except ValueError:
            continue
    return out


def host_snapshot() -> dict[str, Any]:
    snap: dict[str, Any] = {
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown"
    }
    try:
        a, b, c = os.getloadavg()
        snap["load"] = {"1": a, "5": b, "15": c}
    except OSError:
        snap["load"] = None
    snap["cpu_count"] = os.cpu_count() or 1
    try:
        u = shutil.disk_usage(str(Path.home()))
        snap["disk"] = {
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "used_pct": round(100.0 * u.used / max(u.total, 1), 1),
        }
    except Exception as e:
        snap["disk"] = {"error": str(e)}
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False)
        page = 4096
        stats: dict[str, int] = {}
        for line in r.stdout.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            digits = "".join(ch for ch in v if ch.isdigit())
            if digits:
                stats[k.strip()] = int(digits) * page
        if stats:
            free = stats.get("Pages free", 0) + stats.get("Pages speculative", 0)
            snap["memory_bytes"] = {
                "free_ish": free,
                "active": stats.get("Pages active", 0),
                "inactive": stats.get("Pages inactive", 0),
                "wired": stats.get("Pages wired down", 0),
                "compressed": stats.get("Pages occupied by compressor", 0),
            }
    except Exception:
        pass
    return snap


AGENT_BROWSER_MARK = "node_modules/agent-browser/bin/agent-browser"
CHROME_FOR_TESTING_MARK = "Chrome for Testing"
PLAYWRIGHT_MARKS = ("ms-playwright", "chrome-headless-shell", "playwright_chromiumdev_profile")


def find_orphan_candidates(procs: list[Proc], min_age: int, self_uid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in procs:
        if p.uid != self_uid or p.ppid != 1 or p.etimes < min_age:
            continue
        klass = None
        if AGENT_BROWSER_MARK in p.cmd:
            klass = "agent_browser_orphan"
        elif CHROME_FOR_TESTING_MARK in p.cmd:
            klass = "chrome_for_testing_orphan"
        elif "chrome_crashpad_handler" in p.cmd and any(m in p.cmd for m in PLAYWRIGHT_MARKS):
            klass = "playwright_crashpad_orphan"
        if klass:
            out.append(
                {
                    "pid": p.pid,
                    "ppid": p.ppid,
                    "etimes": p.etimes,
                    "rss_kb": p.rss_kb,
                    "pcpu": p.pcpu,
                    "class": klass,
                    "cmd": p.cmd[:300],
                }
            )
    return out


def recommendations_from(procs: list[Proc], snap: dict[str, Any], self_uid: int) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    cpu_count = int(snap.get("cpu_count") or 1)
    load = snap.get("load") or {}
    if isinstance(load, dict) and float(load.get("1") or 0) >= 2.0 * cpu_count:
        recs.append(
            {
                "kind": "high_load",
                "load1": load.get("1"),
                "cpu_count": cpu_count,
                "recommendation": "host load elevated; review top processes",
            }
        )
    disk = snap.get("disk") or {}
    if isinstance(disk, dict) and float(disk.get("used_pct") or 0) >= 90:
        recs.append(
            {
                "kind": "disk_critical",
                "used_pct": disk.get("used_pct"),
                "free": disk.get("free"),
                "recommendation": "disk high — manual cleanup / RAR pass",
            }
        )
    for p in sorted(procs, key=lambda x: x.pcpu, reverse=True)[:8]:
        if p.uid != self_uid or p.pcpu < 50 or p.etimes < 120:
            continue
        # skip known agent helpers (reported as orphan candidates instead)
        if AGENT_BROWSER_MARK in p.cmd or CHROME_FOR_TESTING_MARK in p.cmd:
            continue
        if "hermes gateway" in p.cmd or "browser-lane" in p.cmd or "browser_lane" in p.cmd:
            continue
        recs.append(
            {
                "kind": "high_cpu",
                "pid": p.pid,
                "pcpu": p.pcpu,
                "etimes": p.etimes,
                "cmd": p.cmd[:200],
                "recommendation": "investigate; not auto-reaped",
            }
        )
    return recs


def iter_purge_targets(hermes_home: Path, cache_min_age: int) -> list[Path]:
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    roots = [hermes_home / "tmp", hermes_home / "cache" / "tmp"]
    targets: list[Path] = []
    now = time.time()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                try:
                    st = child.stat()
                except OSError:
                    continue
                if now - st.st_mtime >= cache_min_age:
                    targets.append(child)
        except OSError:
            continue
    try:
        for child in tmp.iterdir():
            name = child.name
            if not (name.startswith("hermes-") or name.startswith("hermes_")):
                continue
            if name.endswith(".sock"):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            if now - st.st_mtime >= cache_min_age:
                targets.append(child)
    except OSError:
        pass
    return targets


def path_size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def purge_targets(
    targets: list[Path], max_purge_bytes: int, apply: bool
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    used = 0
    for t in targets:
        resolved = str(t.resolve()) if t.exists() else str(t)
        refuse = (
            "/Documents" in resolved
            or resolved.endswith("config.yaml")
            or resolved.endswith("auth.json")
            or "/.ssh" in resolved
            or "Keychains" in resolved
        )
        if refuse:
            results.append({"path": str(t), "action": "skip_refuse", "bytes": 0})
            continue
        size = path_size(t)
        if used + size > max_purge_bytes:
            results.append({"path": str(t), "action": "skip_cap", "bytes": size})
            continue
        if not apply:
            results.append({"path": str(t), "action": "would_purge", "bytes": size})
            used += size
            continue
        try:
            if t.is_dir() and not t.is_symlink():
                shutil.rmtree(t, ignore_errors=False)
            else:
                t.unlink(missing_ok=True)
            results.append({"path": str(t), "action": "purged", "bytes": size})
            used += size
        except Exception as e:
            results.append(
                {"path": str(t), "action": "error", "bytes": size, "error": str(e)}
            )
    return results


def run_specialized_apply(hermes_home: Path) -> dict[str, Any]:
    """Apply path uses existing specialized tools only."""
    notes: dict[str, Any] = {}
    reaper = hermes_home / "bin" / "agent-browser-orphan-reaper.py"
    steward = hermes_home / "bin" / "hermes-host-steward.py"
    if steward.is_file():
        notes["agent_browser_orphan_reaper"] = {
            "skipped": "host_steward_owns_resource_mutation"
        }
    elif reaper.is_file():
        try:
            r = subprocess.run(
                [sys.executable, str(reaper)],
                capture_output=True,
                text=True,
                timeout=90,
            )
            notes["agent_browser_orphan_reaper"] = {
                "rc": r.returncode,
                "out": (r.stdout or "")[:300],
                "err": (r.stderr or "")[:300],
            }
        except Exception as e:
            notes["agent_browser_orphan_reaper"] = {"error": str(e)}
    else:
        notes["agent_browser_orphan_reaper"] = {"skipped": "missing"}
    # optional cpu hog watchdog is alert-only; do not invoke here
    return notes


def run_host_steward(hermes_home: Path, apply: bool) -> dict[str, Any]:
    """Reconcile exact ownership leases and return a content-free summary."""
    steward = hermes_home / "bin" / "hermes-host-steward.py"
    if not steward.is_file():
        return {"status": "missing"}
    argv = [sys.executable, str(steward), "--hermes-home", str(hermes_home), "reconcile"]
    if apply:
        argv.append("--apply")
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return {"status": "error", "error": "invalid_json", "rc": result.returncode}
    if not isinstance(payload, dict):
        return {"status": "error", "error": "invalid_shape", "rc": result.returncode}
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    outcomes: dict[str, int] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    failed = outcomes.get("failed", 0) > 0
    return {
        "status": "pass" if result.returncode == 0 and payload.get("status") == "pass" and not failed else "error",
        "mode": payload.get("mode"),
        "counts": payload.get("counts") if isinstance(payload.get("counts"), dict) else {},
        "census": payload.get("census") if isinstance(payload.get("census"), dict) else {},
        "outcomes": outcomes,
        "invalid_leases_seen": int(payload.get("invalid_leases_seen") or 0),
    }


def append_log(hermes_home: Path, line: str) -> None:
    log = hermes_home / "logs" / "host-hygiene.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def run_cycle(
    *,
    hermes_home: Path,
    apply: bool,
    min_age: int,
    max_purge: int,
    cache_min_age: int,
    tcc_lookback_minutes: int,
    json_output: bool,
) -> int:
    started = time.time()
    self_uid = os.getuid()
    snap = host_snapshot()
    procs = load_procs()
    orphans = find_orphan_candidates(procs, min_age, self_uid)
    recs = recommendations_from(procs, snap, self_uid)
    tcc_access = audit_macos_tcc_access(tcc_lookback_minutes)
    host_steward = run_host_steward(hermes_home, apply)

    apply_notes = run_specialized_apply(hermes_home) if apply else None
    purge_results = purge_targets(
        iter_purge_targets(hermes_home, cache_min_age), max_purge, apply
    )
    purged_bytes = sum(
        int(x.get("bytes") or 0)
        for x in purge_results
        if x.get("action") in {"purged", "would_purge"}
    )

    status = "ok"
    if recs or orphans:
        status = "audit" if not apply else "actioned"
    if any(x.get("action") == "error" for x in purge_results):
        status = "error"
    if host_steward.get("status") == "error":
        status = "error"

    receipt = {
        "schema_version": 2,
        "checked_at": utc_now(),
        "host": snap.get("host"),
        "hermes_home": str(hermes_home),
        "mode": "apply" if apply else "dry_run",
        "status": status,
        "duration_ms": int((time.time() - started) * 1000),
        "snapshot": snap,
        "orphan_candidates": orphans,
        "orphan_rss_kb_est": sum(int(o.get("rss_kb") or 0) for o in orphans),
        "recommendations": recs,
        "macos_tcc_access_attribution": tcc_access,
        "purges": purge_results,
        "purge_bytes": purged_bytes,
        "apply_notes": apply_notes,
        "host_steward": host_steward,
        "safety": {
            "apply_uses_specialized_tools_only": True,
            "never_delete_user_data": True,
            "self_uid_inventory_only": True,
            "single_instance": True,
            "tcc_paths_redacted": True,
            "interactive_apps_audit_only": True,
            "browser_process_mutation_lease_only": (
                not apply
                or (apply_notes.get("agent_browser_orphan_reaper") or {}).get(
                    "skipped"
                )
                == "host_steward_owns_resource_mutation"
            ),
        },
        "design": "kit/docs/host-hygiene-loop.md",
    }

    state_dir = hermes_home / "state"
    atomic_write_json(state_dir / "host-hygiene-latest.json", receipt)

    tcc_access_count = len(tcc_access.get("events") or [])
    summary = (
        f"{receipt['checked_at']} status={status} mode={receipt['mode']} "
        f"orphans={len(orphans)} orphan_rss_kb~={receipt['orphan_rss_kb_est']} "
        f"purge_bytes={purged_bytes} recs={len(recs)} tcc_access={tcc_access_count}"
    )
    append_log(hermes_home, summary)
    if json_output:
        print(json.dumps(receipt, indent=2))
    else:
        print(summary)
        if orphans:
            print(f"  orphan_candidates: {len(orphans)}")
        if recs:
            print(f"  recommendations: {len(recs)}")
        if tcc_access_count:
            print(f"  macos_tcc_access_events: {tcc_access_count}")
    return 0 if status != "error" else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes host hygiene loop")
    ap.add_argument("--apply", action="store_true", help="run safe specialized apply tools + tmp purge")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"),
    )
    args = ap.parse_args()

    hermes_home = Path(args.hermes_home).expanduser()
    apply = bool(args.apply or env_truthy("HERMES_HOST_HYGIENE_APPLY"))
    min_age = env_int("HERMES_HOST_HYGIENE_MIN_AGE", 600)
    max_purge = env_int("HERMES_HOST_HYGIENE_MAX_PURGE_BYTES", 2 * 1024 * 1024 * 1024)
    cache_min_age = env_int("HERMES_HOST_HYGIENE_CACHE_MIN_AGE", 86400)
    tcc_lookback_minutes = env_int("HERMES_HOST_HYGIENE_TCC_LOOKBACK_MINUTES", 30)
    lock_path = hermes_home / "state" / "locks" / "host-hygiene.lock"
    with single_instance_lease(lock_path) as acquired:
        if not acquired:
            skipped = {
                "schema_version": 1,
                "checked_at": utc_now(),
                "status": "skipped_already_running",
                "lock": str(lock_path),
            }
            print(json.dumps(skipped, indent=2) if args.json else "host hygiene already running; skipped")
            return 0
        return run_cycle(
            hermes_home=hermes_home,
            apply=apply,
            min_age=min_age,
            max_purge=max_purge,
            cache_min_age=cache_min_age,
            tcc_lookback_minutes=tcc_lookback_minutes,
            json_output=args.json,
        )


if __name__ == "__main__":
    raise SystemExit(main())
