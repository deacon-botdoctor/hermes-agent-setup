#!/usr/bin/env python3
# ruff: noqa: E501, E701, E702
"""Return read-only Common Evidence from this runtime's bound local GBrain source.

The local config is deliberately required and supplies the fixed client/source
binding. Query text never selects a source, consumer, or permission envelope.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

MAX_RESULTS, MAX_CONTENT_CHARS, MAX_TOTAL_CONTENT_CHARS = 10, 16_000, 60_000


class QueryError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config(home: Path) -> dict[str, str]:
    path = home / "config" / "client-local-evidence.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryError("not_enabled") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("enabled") is not True:
        raise QueryError("not_enabled")
    fields = ("consumer", "client_id", "source_id")
    if any(not isinstance(value.get(field), str) or not value[field].strip() for field in fields):
        raise QueryError("invalid_config")
    return {field: value[field].strip() for field in fields}


def gbrain_bin(home: Path) -> Path:
    base = home / "bin" / "gbrain"
    return base.with_suffix(".cmd") if os.name == "nt" else base


def source_rows(query: str, home: Path, source_id: str) -> list[dict[str, Any]]:
    binary = gbrain_bin(home)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise QueryError("source_unavailable")
    try:
        proc = subprocess.run(
            [str(binary), "call", "search", json.dumps({"query": query, "limit": MAX_RESULTS}, separators=(",", ":"))],
            text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QueryError("source_unavailable") from exc
    if proc.returncode != 0:
        raise QueryError("source_unavailable")
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QueryError("source_malformed") from exc
    if not isinstance(rows, list) or len(rows) > MAX_RESULTS:
        raise QueryError("source_malformed")
    if any(not isinstance(row, dict) or row.get("source_id") != source_id for row in rows):
        raise QueryError("source_not_local")
    return rows


def normalize(rows: list[dict[str, Any]], config: Mapping[str, str], evaluated_at: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        slug, title, content, chunk_id = row.get("slug"), row.get("title"), row.get("chunk_text"), row.get("chunk_id")
        if not isinstance(slug, str) or not isinstance(title, str) or not isinstance(content, str) or not isinstance(chunk_id, int):
            raise QueryError("source_malformed")
        total += len(content)
        if len(content) > MAX_CONTENT_CHARS or total > MAX_TOTAL_CONTENT_CHARS:
            raise QueryError("source_limit_exceeded")
        page_uri = f"gbrain://{config['client_id']}/{config['source_id']}/" + quote(slug, safe="/-._~:")
        uri = f"{page_uri}#chunk/{chunk_id}"
        session = slug.startswith(("session/", "topic/"))
        identity = json.dumps({"source_type": "gbrain", "source_uri": uri}, sort_keys=True, separators=(",", ":"))
        items.append({
            "schema_version": 1, "evidence_id": "ev1_" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            "source_type": "gbrain", "source_uri": uri,
            "authority": "operational_evidence" if session else "canonical",
            "scope": {"kind": "client", "id": config["client_id"]}, "title": title, "content": content,
            "created_at": row.get("effective_date") if isinstance(row.get("effective_date"), str) else None,
            "updated_at": None, "last_verified_at": evaluated_at,
            "freshness_class": "historical" if session else "durable",
            "parent_context": {"relation": "contained_by", "source_uri": page_uri, "title": title},
            "citations": [{"source_uri": uri, "title": title, "locator": f"chunk {chunk_id}"}],
            "permissions": {"visibility": "private", "allow": [f"agent:{config['consumer']}", f"scope:client/{config['client_id']}"], "deny": []},
        })
    return items


def build_pack(query: str, home: Path) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise QueryError("invalid_query")
    config = load_config(home)
    items = normalize(source_rows(query, home, config["source_id"]), config, utc_now())
    return {"schema": "common-evidence-pack/v1", "policy_id": "client-local-evidence-v1", "consumer": config["consumer"], "query": query, "evaluated_at": utc_now(), "handling": {"writeback": False, "conflicts": "surface_all; canonical controls within its authority domain"}, "receipt": {"evidence_count": len(items), "source_counts": dict(Counter(item["source_type"] for item in items)), "scope_counts": dict(Counter(item["scope"]["kind"] for item in items))}, "evidence": items}


def write_receipt(pack: Mapping[str, Any], home: Path) -> None:
    value = {"schema": "common-evidence-query-receipt/v1", "receipt_id": "ceq1_" + uuid.uuid4().hex, "generated_at": utc_now(), "consumer": pack["consumer"], "policy_id": pack["policy_id"], "pack_schema": pack["schema"], "receipt": pack["receipt"]}
    directory = home / "state" / "common-evidence-client-local" / "receipts"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as handle:
        json.dump(value, handle, sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno()); temporary = Path(handle.name)
    os.replace(temporary, directory / f"{value['generated_at'].replace(':', '')}-{value['receipt_id']}.json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--query", required=True); parser.add_argument("--record", action="store_true")
    args = parser.parse_args(argv); home = Path(os.environ.get("HERMES_HOME", Path(__file__).resolve().parents[1]))
    try:
        pack = build_pack(args.query, home)
        if args.record: write_receipt(pack, home)
    except QueryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr); return 2
    print(json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
