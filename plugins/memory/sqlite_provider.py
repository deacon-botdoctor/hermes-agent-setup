"""sqlite_provider — a real, working local memory provider.

Persists durable memories to a local SQLite database with full-text search, and prefetches the
most relevant ones before a turn. No external service, no API key. This is the agent's OWN
recall — distinct from the canonical GBrain knowledge layer (see gbrain_provider.py and
../../gbrain/README.md for that).

Runnable and tested: `python -m plugins.memory.sqlite_provider --demo` exercises write + search.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path


class SqliteMemoryProvider:
    """Local SQLite-FTS memory. Implement against your runtime's memory-provider interface."""

    def __init__(self, db_path: str | Path = "~/.hermes/memory/recall.db", char_limit: int = 2200):
        self.db_path = Path(db_path).expanduser()
        self.char_limit = char_limit
        self._db: sqlite3.Connection | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def post_setup(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: a gateway calls prefetch/sync_turn from different threads.
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories(
              id INTEGER PRIMARY KEY, session_id TEXT, kind TEXT,
              text TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
              USING fts5(text, content='memories', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
              INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
            END;
            """
        )
        self._db.commit()

    def shutdown(self) -> None:
        if self._db:
            self._db.commit()
            self._db.close()
            self._db = None

    # ── the two turn-cycle hooks ─────────────────────────────────────────────
    def sync_turn(self, session_id: str, messages: list) -> int:
        """After a turn: persist durable facts worth remembering. Returns count written.

        The extraction here is deliberately simple (user statements of fact / preference). Swap in
        an LLM extraction pass if you want richer memories.
        """
        assert self._db, "call post_setup first"
        written = 0
        for m in messages or []:
            if not (isinstance(m, dict) and m.get("role") == "user"):
                continue
            content = m.get("content")
            if isinstance(content, str) and _looks_durable(content):
                self._db.execute(
                    "INSERT INTO memories(session_id, kind, text) VALUES(?,?,?)",
                    (session_id, "fact", content.strip()[: self.char_limit]),
                )
                written += 1
        self._db.commit()
        return written

    def prefetch(self, session_id: str, query: str, k: int = 5) -> list[str]:
        """Before a turn: return up to k relevant memories, total under char_limit."""
        assert self._db, "call post_setup first"
        terms = _fts_query(query)
        if not terms:
            return []
        rows = self._db.execute(
            "SELECT m.text FROM memories_fts f JOIN memories m ON m.id=f.rowid "
            "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
            (terms, k),
        ).fetchall()
        out, budget = [], self.char_limit
        for (text,) in rows:
            if len(text) <= budget:
                out.append(text)
                budget -= len(text)
        return out


def _looks_durable(text: str) -> bool:
    """Cheap heuristic: statements of fact/preference are worth remembering; questions aren't."""
    t = text.strip().lower()
    if t.endswith("?") or len(t) < 12:
        return False
    return bool(re.search(r"\b(i|my|we|our|always|never|prefer|remember|call me|i'm|i am)\b", t))


def _fts_query(query: str) -> str:
    words = re.findall(r"[a-z0-9]{3,}", (query or "").lower())
    return " OR ".join(words[:8])


def _demo() -> None:
    import tempfile

    db = Path(tempfile.mkdtemp()) / "recall.db"
    p = SqliteMemoryProvider(db_path=db)
    p.post_setup()
    n = p.sync_turn("s1", [
        {"role": "user", "content": "I prefer terse answers and I always deploy on Fridays."},
        {"role": "user", "content": "what's the weather?"},  # a question — not stored
    ])
    print(f"stored {n} memories (expected 1)")
    hits = p.prefetch("s1", "how should I answer and when do we deploy")
    print("recall:", hits)
    assert n == 1 and hits, "demo failed"
    print("OK")
    p.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.demo:
        _demo()
