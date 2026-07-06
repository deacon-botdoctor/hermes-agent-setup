"""Hot-path MCP capability catalog with usage-ranked search."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - keeps direct tests runnable without mcp installed.
    FastMCP = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = ROOT / "registry.json"
REGISTRY_PATH = Path(os.environ.get("CAPABILITY_REGISTRY", DEFAULT_REGISTRY_PATH))
USAGE_DB_PATH = Path(
    os.environ.get(
        "CAPABILITY_USAGE_DB",
        Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        / "state"
        / "capability-router-usage.db",
    )
)

mcp = FastMCP("capability-router") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _load_registry() -> dict[str, Any]:
    data = json.loads(REGISTRY_PATH.read_text())
    data.setdefault("categories", [])
    data.setdefault("capabilities", [])
    return data


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 1}


def _capability_text(capability: dict[str, Any]) -> str:
    fields: list[str] = [
        str(capability.get("id", "")),
        str(capability.get("category", "")),
        str(capability.get("label", "")),
        str(capability.get("summary", "")),
        str(capability.get("mcp_server", "")),
        str(capability.get("tool_name", "")),
    ]
    fields.extend(str(item) for item in capability.get("preferred_for", []) or [])
    return " ".join(fields)


def _usage_scores() -> dict[str, int]:
    if not USAGE_DB_PATH.exists():
        return {}
    try:
        with sqlite3.connect(USAGE_DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT capability_id,
                       SUM(CASE outcome WHEN 'success' THEN 1 WHEN 'failure' THEN -2 ELSE 0 END)
                FROM capability_usage
                GROUP BY capability_id
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(capability_id): int(score or 0) for capability_id, score in rows}


def _init_usage_db() -> None:
    USAGE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                capability_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                query TEXT,
                error_class TEXT,
                error_detail TEXT,
                duration_ms INTEGER,
                created_at REAL NOT NULL
            )
            """
        )


@_tool
def search_capabilities(query: str, max_hits: int = 8) -> dict[str, Any]:
    """Search the registry and rank matching capabilities with usage feedback."""
    registry = _load_registry()
    query_tokens = _tokens(query)
    usage = _usage_scores()
    hits: list[dict[str, Any]] = []
    for capability in registry["capabilities"]:
        text_tokens = _tokens(_capability_text(capability))
        overlap = len(query_tokens & text_tokens)
        phrase_bonus = 3 if query.lower() and query.lower() in _capability_text(capability).lower() else 0
        preferred_bonus = sum(
            2 for item in capability.get("preferred_for", []) or [] if query_tokens & _tokens(str(item))
        )
        usage_score = usage.get(str(capability.get("id")), 0)
        score = overlap + phrase_bonus + preferred_bonus + usage_score
        matched = overlap > 0 or phrase_bonus > 0 or preferred_bonus > 0
        if query_tokens and not matched:
            continue
        hit = dict(capability)
        hit["availability"] = "catalog"
        hit["can_invoke_now"] = True
        hit["score"] = score
        hit["score_breakdown"] = {
            "token_overlap": overlap,
            "phrase": phrase_bonus,
            "preferred_for": preferred_bonus,
            "usage": usage_score,
        }
        hits.append(hit)
    hits.sort(key=lambda item: (-item["score"], item.get("id", "")))
    return {"ok": True, "query": query, "hits": hits[: max(1, int(max_hits))]}


@_tool
def describe_capability(capability_id: str) -> dict[str, Any]:
    """Return one registry capability by id."""
    registry = _load_registry()
    for capability in registry["capabilities"]:
        if capability.get("id") == capability_id:
            found = dict(capability)
            found["availability"] = "catalog"
            found["can_invoke_now"] = True
            return {"ok": True, "capability": found}
    return {"ok": False, "error": "unknown_capability", "capability_id": capability_id}


@_tool
def list_categories() -> dict[str, Any]:
    """Return the registry category list."""
    registry = _load_registry()
    return {"ok": True, "categories": registry["categories"]}


@_tool
def registry_status() -> dict[str, Any]:
    """Report registry size and recorded usage totals."""
    registry = _load_registry()
    usage_rows: list[tuple[str, str]] = []
    if USAGE_DB_PATH.exists():
        try:
            with sqlite3.connect(USAGE_DB_PATH) as conn:
                usage_rows = conn.execute("SELECT capability_id, outcome FROM capability_usage").fetchall()
        except sqlite3.Error:
            usage_rows = []
    success = sum(1 for _, outcome in usage_rows if outcome == "success")
    failure = sum(1 for _, outcome in usage_rows if outcome == "failure")
    scores = _usage_scores()
    return {
        "ok": True,
        "registry_path": str(REGISTRY_PATH),
        "capabilities_total": len(registry["capabilities"]),
        "categories_total": len(registry["categories"]),
        "usage_records_total": len(usage_rows),
        "usage_records_success": success,
        "usage_records_failure": failure,
        "usage_by_capability": [
            {"capability_id": capability_id, "usage_score": score}
            for capability_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


@_tool
def record_capability_outcome(
    capability_id: str,
    outcome: str | None = None,
    ok: bool | None = None,
    query: str | None = None,
    failure_class: str | None = None,
    failure_detail: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Persist success/failure feedback for future capability ranking."""
    if outcome is None and ok is None:
        return {"ok": False, "recorded": False, "reason": "missing_outcome"}
    normalized = outcome or ("success" if ok else "failure")
    if normalized not in {"success", "failure"}:
        return {"ok": False, "recorded": False, "reason": "invalid_outcome", "outcome": normalized}
    record = {
        "capability_id": capability_id,
        "outcome": normalized,
        "query": query,
        "error_class": failure_class,
        "error_detail": failure_detail,
        "duration_ms": duration_ms,
        "created_at": time.time(),
    }
    try:
        _init_usage_db()
        with sqlite3.connect(USAGE_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO capability_usage
                    (capability_id, outcome, query, error_class, error_detail, duration_ms, created_at)
                VALUES
                    (:capability_id, :outcome, :query, :error_class, :error_detail, :duration_ms, :created_at)
                """,
                record,
            )
    except OSError as exc:
        return {"ok": False, "recorded": False, "reason": str(exc)}
    except sqlite3.Error as exc:
        return {"ok": False, "recorded": False, "reason": str(exc)}
    scores = _usage_scores()
    return {
        "ok": True,
        "recorded": True,
        "record": record,
        "usage_score": scores.get(capability_id, 0),
        "usage_failures": max(0, -scores.get(capability_id, 0) // 2),
    }


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the capability-router server.")
    mcp.run()


if __name__ == "__main__":
    main()
