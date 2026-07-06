from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - keeps tests runnable without mcp installed.
    FastMCP = None  # type: ignore[assignment]


MEMORY_DB = Path(
    os.environ.get(
        "ANAMNESIS_DB",
        Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "state" / "anamnesis.db",
    )
)

mcp = FastMCP("anamnesis") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _connect() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MEMORY_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'memory',
            source TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
        USING fts5(content, kind, source, content='memories', content_rowid='id')
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, kind, source)
            VALUES (new.id, new.content, new.kind, coalesce(new.source, ''));
        END
        """
    )
    return conn


@_tool
def memory_record(content: str, kind: str = "memory", source: str | None = None) -> dict[str, Any]:
    if not content.strip():
        return {"ok": False, "error": "empty_content"}
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO memories(content, kind, source, created_at) VALUES (?, ?, ?, ?)",
            (content.strip(), kind.strip() or "memory", source, time.time()),
        )
        memory_id = int(cur.lastrowid)
    return {"ok": True, "id": memory_id}


@_tool
def memory_search(query: str, limit: int = 8) -> dict[str, Any]:
    if not query.strip():
        return {"ok": False, "error": "empty_query", "memories": []}
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.content, m.kind, m.source, m.created_at
            FROM memories_fts f
            JOIN memories m ON m.id = f.rowid
            WHERE memories_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, max(1, int(limit))),
        ).fetchall()
    return {
        "ok": True,
        "query": query,
        "memories": [
            {"id": row[0], "content": row[1], "kind": row[2], "source": row[3], "created_at": row[4]}
            for row in rows
        ],
    }


@_tool
def memory_status() -> dict[str, Any]:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    return {"ok": True, "db": str(MEMORY_DB), "memories_total": total}


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the anamnesis MCP server.")
    mcp.run()


if __name__ == "__main__":
    main()
