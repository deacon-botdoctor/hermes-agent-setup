#!/usr/bin/env python3
"""Tenant-local repair state. No model, network, credentials or service commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
STATES = {"failed", "healthy", "unknown"}
RESULTS = {"completed", "failed", "deferred", "outcome_unknown"}
REASONS = {"deadline", "recurrence", "cannot_fix", "needs_approval", "insufficient_evidence", "action_outcome_unknown"}
STATUS_SCHEMA_VERSION = 2
INTERVAL_S = 300


def identifier(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError("invalid identifier")
    return value


def epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid timestamp")
    return float(value)


def private_dir(path):
    path = Path(path).absolute()
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise ValueError("symlink in state path")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("state directory must be private and owned by this user")
    return path


def atomic_json(path, value):
    import tempfile

    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".medic-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(name).unlink(missing_ok=True)


class Store:
    def __init__(self, directory, target):
        self.directory = private_dir(directory)
        self.target = identifier(target)
        path = self.directory / "medic.sqlite"
        if path.is_symlink():
            raise ValueError("symlink database")
        if not path.exists() and (self.directory / "status.json").exists():
            raise ValueError("missing prior database; restore requires reconciliation")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("database must be private")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(
                service TEXT NOT NULL,signal TEXT NOT NULL,sample TEXT NOT NULL,
                observed REAL NOT NULL,status TEXT NOT NULL,PRIMARY KEY(service,signal));
            CREATE TABLE IF NOT EXISTS episodes(
                id TEXT PRIMARY KEY,service TEXT NOT NULL,signal TEXT NOT NULL,
                opened REAL NOT NULL,recovered REAL,healthy_since REAL,passes INTEGER NOT NULL DEFAULT 0,
                last_sample TEXT,last_observed REAL NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '');
            CREATE UNIQUE INDEX IF NOT EXISTS active_episode ON episodes(service,signal) WHERE recovered IS NULL;
            CREATE TABLE IF NOT EXISTS actions(
                id TEXT PRIMARY KEY,episode TEXT NOT NULL UNIQUE,name TEXT NOT NULL,tier INTEGER NOT NULL,
                reserved REAL NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,at REAL NOT NULL,
                episode TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
        """)
        with self.db:
            for key, value in (("target", self.target), ("generation", uuid.uuid4().hex), ("schema", "1")):
                self.db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
            if self.meta("target") != self.target or self.meta("schema") != "1":
                raise ValueError("state identity or version mismatch")

    def close(self):
        self.db.close()

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
        )

    def _event(self, now, episode, kind, payload=None):
        self.db.execute(
            "INSERT INTO events(id,at,episode,kind,payload) VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, now, episode, kind, json.dumps(payload or {}, sort_keys=True)),
        )

    def observe(self, service, signal, status, sample, observed_at, *, now=None):
        service, signal, sample = map(identifier, (service, signal, sample))
        if status not in STATES:
            raise ValueError("invalid observation")
        now = epoch(time.time() if now is None else now)
        observed_at = epoch(observed_at)
        # Future, old or incomplete samples cannot certify recovery.
        if not 0 <= now - observed_at <= 600:
            status = "unknown"
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM episodes WHERE service=? AND signal=? AND recovered IS NULL", (service, signal)
            ).fetchone()
            last = self.db.execute(
                "SELECT * FROM observations WHERE service=? AND signal=?", (service, signal)
            ).fetchone()
            if last and (sample == last["sample"] or observed_at <= last["observed"]):
                previous = (
                    row
                    or self.db.execute(
                        "SELECT * FROM episodes WHERE service=? AND signal=? ORDER BY opened DESC LIMIT 1",
                        (service, signal),
                    ).fetchone()
                )
                self.db.commit()
                return dict(previous) if previous else None
            self.db.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?) ON CONFLICT(service,signal) DO UPDATE SET sample=excluded.sample,observed=excluded.observed,status=excluded.status",
                (service, signal, sample, observed_at, status),
            )
            if row is None:
                prior = self.db.execute(
                    "SELECT * FROM episodes WHERE service=? AND signal=? ORDER BY last_observed DESC LIMIT 1",
                    (service, signal),
                ).fetchone()
                if prior and (sample == prior["last_sample"] or observed_at <= prior["last_observed"]):
                    self.db.commit()
                    return dict(prior)
                if status != "failed":
                    self.db.commit()
                    return None
                eid = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO episodes(id,service,signal,opened,last_observed,status,last_sample) VALUES (?,?,?,?,?,?,?)",
                    (eid, service, signal, observed_at, observed_at, status, sample),
                )
                self._event(now, eid, "opened", {"service": service, "signal": signal})
            else:
                eid = row["id"]
                continuous = status == "healthy" and observed_at - row["last_observed"] <= 600
                since = row["healthy_since"] if continuous else None
                passes = row["passes"] + 1 if continuous else (1 if status == "healthy" else 0)
                if status == "healthy" and since is None:
                    since = observed_at
                recovered = observed_at if status == "healthy" and passes >= 2 and observed_at - since >= 300 else None
                self.db.execute(
                    "UPDATE episodes SET last_sample=?,last_observed=?,status=?,healthy_since=?,passes=?,recovered=? WHERE id=?",
                    (sample, observed_at, status, since, passes, recovered, eid),
                )
                if recovered is not None:
                    self._event(now, eid, "recovered")
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone())
        except BaseException:
            self.db.rollback()
            raise

    def reserve(self, episode, action, *, tier=0, now=None):
        identifier(episode)
        identifier(action)
        if tier not in (0, 1):
            raise ValueError("invalid repair tier")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            # Another observer can advance the episode while this writer waits.
            now = epoch(time.time() if now is None else now)
            row = self.db.execute("SELECT * FROM episodes WHERE id=? AND recovered IS NULL", (episode,)).fetchone()
            if row is None or row["status"] != "failed" or not 0 <= now - row["last_observed"] <= 600:
                self.db.commit()
                return None
            previous = self.db.execute("SELECT * FROM actions WHERE episode=?", (episode,)).fetchone()
            if previous:
                # An intent with no result may already have caused a side effect.
                self.db.commit()
                return None
            aid = hashlib.sha256(f"{episode}:{action}:1".encode()).hexdigest()
            self.db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?)", (aid, episode, action, tier, now, "pending"))
            self._event(now, episode, "action_reserved", {"action_id": aid, "action": action, "tier": tier})
            self.db.commit()
            return aid
        except BaseException:
            self.db.rollback()
            raise

    def result(self, action_id, result, *, now=None):
        if result not in RESULTS:
            raise ValueError("invalid action result")
        now = epoch(time.time() if now is None else now)
        with self.db:
            row = self.db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise ValueError("unknown action")
            if row["result"] != "pending":
                if row["result"] != result:
                    raise ValueError("conflicting action result")
                return
            self.db.execute("UPDATE actions SET result=? WHERE id=?", (result, action_id))
            self._event(
                now, row["episode"], "action_result", {"action_id": action_id, "result": result, "tier": row["tier"]}
            )
            if result in {"failed", "outcome_unknown"}:
                reason = "cannot_fix" if result == "failed" else "action_outcome_unknown"
                self.db.execute("UPDATE episodes SET reason=? WHERE id=?", (reason, row["episode"]))
                self._event(now, row["episode"], "escalated", {"reason": reason})

    def escalate(self, episode, reason, *, now=None):
        if reason not in REASONS:
            raise ValueError("invalid escalation")
        with self.db:
            row = self.db.execute("SELECT reason FROM episodes WHERE id=?", (episode,)).fetchone()
            if row is None:
                raise ValueError("unknown episode")
            if row[0] != reason:
                self.db.execute("UPDATE episodes SET reason=? WHERE id=?", (reason, episode))
                self._event(epoch(time.time() if now is None else now), episode, "escalated", {"reason": reason})

    def cycle(self, *, complete, now=None):
        now = epoch(time.time() if now is None else now)
        with self.db:
            self._meta("last_attempt", now)
            if complete:
                self._meta("last_completed", now)

    def summary(self, *, now=None):
        now = epoch(time.time() if now is None else now)
        rows = list(self.db.execute("SELECT * FROM episodes ORDER BY opened"))
        repair_rows = list(
            self.db.execute(
                """
                SELECT actions.name, actions.reserved, actions.result, episodes.service, episodes.signal
                FROM actions JOIN episodes ON episodes.id=actions.episode
                ORDER BY actions.reserved DESC, actions.id DESC LIMIT 10
                """
            )
        )
        repair_history = [
            {
                "timestamp": row["reserved"],
                "condition": row["service"] + ":" + row["signal"],
                "action": row["name"],
                "outcome": row["result"],
            }
            for row in repair_rows
        ]
        recent_repairs = list(
            self.db.execute(
                """
                SELECT episodes.service, episodes.signal
                FROM actions JOIN episodes ON episodes.id=actions.episode
                WHERE actions.reserved>? AND actions.reserved<=?
                """,
                (now - 86400, now),
            )
        )
        repairs_by_condition = {}
        for row in recent_repairs:
            key = (row["service"], row["signal"])
            repairs_by_condition[key] = repairs_by_condition.get(key, 0) + 1
        budget = self.db.execute(
            """
            SELECT actions.result, episodes.reason
            FROM actions JOIN episodes ON episodes.id=actions.episode
            WHERE episodes.recovered IS NULL
            ORDER BY actions.reserved DESC, actions.id DESC LIMIT 1
            """
        ).fetchone()
        budget_exhausted = budget is not None
        if budget is None:
            budget_reason = ""
        elif budget["result"] == "pending":
            budget_reason = "pending_repair"
        elif budget["reason"]:
            budget_reason = budget["reason"]
        else:
            budget_reason = "one_repair_per_episode"
        groups = {}
        for row in rows:
            key = (row["service"], row["signal"])
            item = groups.setdefault(
                key,
                {
                    "service": key[0],
                    "signal": key[1],
                    "episodes_24h": 0,
                    "open": False,
                    "reason": "",
                    "episode_id": row["id"],
                },
            )
            item["episode_id"] = row["id"]
            item["opened_at"] = row["opened"]
            if now - 86400 < row["opened"] <= now:
                item["episodes_24h"] += 1
            if row["recovered"] is None:
                item.update(
                    open=True, episode_id=row["id"], opened_at=row["opened"], status=row["status"], reason=row["reason"]
                )
                pending = self.db.execute(
                    "SELECT reserved FROM actions WHERE episode=? AND result='pending'", (row["id"],)
                ).fetchone()
                if pending and now - pending[0] > 300:
                    item["reason"] = "action_outcome_unknown"
                elif item["reason"] in {"", "recurrence"} and now - row["opened"] >= 600:
                    item["reason"] = "deadline"
        for item in groups.values():
            item["eligible"] = bool(item["reason"] or item["episodes_24h"] >= 3)
            if not item["reason"] and item["episodes_24h"] >= 3:
                item["reason"] = "recurrence"
            key = (item["service"], item["signal"])
            if item["reason"] not in {"", "recurrence"}:
                item["severity"] = "high"
            elif repairs_by_condition.get(key, 0):
                item["severity"] = "medium"
            elif item["open"]:
                item["severity"] = "low"
            else:
                item["severity"] = "documented"
        completed = float(self.meta("last_completed") or 0)
        return {
            "schema": "client-medic-status/v1",
            "schema_version": STATUS_SCHEMA_VERSION,
            "target": self.target,
            "generation": self.meta("generation"),
            "generated_at": now,
            "last_attempt_at": float(self.meta("last_attempt") or 0),
            "last_completed_at": completed,
            "stale": not (completed > 0 and 0 <= now - completed <= 1200),
            "conditions": list(groups.values()),
            "interval_s": INTERVAL_S,
            "repairs_24h": len(recent_repairs),
            "repair_history": repair_history,
            "budget_exhausted": budget_exhausted,
            "budget_exhausted_reason": budget_reason,
            "daily": self.daily(now),
            "event_cursor": self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0],
            "rollup": {
                "episodes": len(rows),
                "recoveries": sum(r["recovered"] is not None for r in rows),
                "actions": self.db.execute("SELECT count(*) FROM actions").fetchone()[0],
                "open": sum(r["recovered"] is None for r in rows),
            },
        }

    def daily(self, now):
        start = now - now % 86400

        def count(sql):
            return self.db.execute(sql, (start, now)).fetchone()[0]

        return {
            "start": start,
            "end": now,
            "episodes": count("SELECT count(*) FROM episodes WHERE opened>=? AND opened<=?"),
            "recoveries": count("SELECT count(*) FROM episodes WHERE recovered>=? AND recovered<=?"),
            "tier0_actions": count("SELECT count(*) FROM actions WHERE tier=0 AND reserved>=? AND reserved<=?"),
            "tier1_actions": count("SELECT count(*) FROM actions WHERE tier=1 AND reserved>=? AND reserved<=?"),
            "escalations": count(
                "SELECT count(DISTINCT episode) FROM events WHERE kind='escalated' AND at>=? AND at<=?"
            ),
        }

    def publish(self, *, now=None):
        value = self.summary(now=now)
        atomic_json(self.directory / "status.json", value)
        return value

    def events(self, after=0):
        if type(after) is not int or after < 0:
            raise ValueError("invalid cursor")
        latest = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        if after > latest:
            raise ValueError("cursor exceeds committed events; possible restore gap")
        for row in self.db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (after,)):
            yield {
                "schema": "client-medic-event/v1",
                "target": self.target,
                "generation": self.meta("generation"),
                "sequence": row["seq"],
                "event_id": row["id"],
                "at": row["at"],
                "episode_id": row["episode"],
                "kind": row["kind"],
                **json.loads(row["payload"]),
            }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", required=True, type=Path)
    p.add_argument("--target", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    observe = sub.add_parser("observe")
    for name in ("service", "signal", "status", "sample"):
        observe.add_argument("--" + name, required=True)
    observe.add_argument("--observed-at", type=float, required=True)
    observe.add_argument("--now", type=float)
    status = sub.add_parser("status")
    status.add_argument("--now", type=float)
    export = sub.add_parser("export")
    export.add_argument("--after", type=int, default=0)
    args = p.parse_args()
    store = Store(args.state_dir, args.target)
    try:
        if args.command == "observe":
            value = store.observe(args.service, args.signal, args.status, args.sample, args.observed_at, now=args.now)
        elif args.command == "export":
            for event in store.events(args.after):
                print(json.dumps(event, sort_keys=True))
            return
        else:
            value = store.summary(now=args.now)
        print(json.dumps(value, sort_keys=True))
    finally:
        store.close()


if __name__ == "__main__":
    main()
