#!/usr/bin/env python3
"""One-shot, read-only verifier for a Telegram receipt-to-reply transaction."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--update-id")
    group.add_argument("--transaction-id")
    parser.add_argument("--max-age-seconds", type=float, default=900)
    args = parser.parse_args()
    if not args.ledger.is_file():
        print(json.dumps({"ok": False, "status": "Unknown", "error": "ledger missing"}))
        return 2
    try:
        uri = f"file:{args.ledger.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            tx = args.transaction_id
            if tx is None:
                receipt = db.execute(
                    "SELECT transaction_id FROM events WHERE inbound_update_id=? AND event_type='received'",
                    (args.update_id,),
                ).fetchone()
                tx = receipt["transaction_id"] if receipt else None
            rows = (
                []
                if tx is None
                else [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM events WHERE transaction_id=? ORDER BY occurred_at,event_id",
                        (tx,),
                    )
                ]
            )
    except sqlite3.Error as error:
        print(json.dumps({"ok": False, "status": "Unknown", "error": str(error)}))
        return 2
    kinds = {row["event_type"] for row in rows}
    accepted = next((row for row in rows if row["event_type"] == "telegram_accepted"), None)
    status = (
        "Failed"
        if "failed" in kinds
        else "Replied"
        if accepted and accepted.get("outbound_message_id")
        else "Pending"
        if rows
        else "Unknown"
    )
    age = time.time() - min((row["occurred_at"] for row in rows), default=time.time())
    required = {"received", "run_started", "run_finished", "telegram_accepted"}
    ok = status == "Replied" and required <= kinds and age <= args.max_age_seconds
    print(
        json.dumps(
            {
                "ok": ok,
                "status": status,
                "transaction_id": rows[0]["transaction_id"] if rows else None,
                "outbound_message_id": accepted.get("outbound_message_id") if accepted else None,
                "age_seconds": round(age, 3),
                "event_types": sorted(kinds),
            },
            sort_keys=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
