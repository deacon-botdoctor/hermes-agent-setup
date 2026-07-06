"""gbrain_provider — memory backed by a running GBrain instead of a local DB.

Same memory-provider shape as sqlite_provider, but writes and recalls through a GBrain library
(the canonical-knowledge engine — see ../../gbrain/README.md). Use this when you want the agent's
recall to feed and draw from the same brain that holds your canonical knowledge, so a fact the
agent learns in chat becomes queryable everywhere.

GBrain is a separate product (github.com/garrytan/gbrain) that you install and run; this provider
is just the glue that points the agent's memory hooks at it via its CLI/MCP.
"""
from __future__ import annotations

import json
import subprocess


class GBrainMemoryProvider:
    """Wire the agent's memory hooks to a GBrain library through its CLI."""

    def __init__(self, gbrain_bin: str = "gbrain", namespace: str = "agent-memory", char_limit: int = 2200):
        self.gbrain = gbrain_bin       # the compiled gbrain launcher on PATH
        self.namespace = namespace     # a page namespace for agent-written memories
        self.char_limit = char_limit

    def post_setup(self) -> None:
        # Verify the brain is reachable; fail soft so memory never blocks the agent.
        try:
            subprocess.run([self.gbrain, "--version"], capture_output=True, timeout=10, check=True)
        except Exception:
            pass  # provider degrades to no-op rather than breaking the turn

    def sync_turn(self, session_id: str, messages: list) -> int:
        """Write durable facts from the turn into the brain as a namespaced page."""
        facts = [
            m["content"].strip()
            for m in messages or []
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
            and not m["content"].strip().endswith("?")
        ]
        if not facts:
            return 0
        body = "\n".join(f"- {f[: self.char_limit]}" for f in facts)
        try:
            subprocess.run(
                [self.gbrain, "write", f"{self.namespace}/{session_id}", "--append", "--stdin"],
                input=body.encode(), capture_output=True, timeout=30, check=False,
            )
        except Exception:
            return 0
        return len(facts)

    def prefetch(self, session_id: str, query: str, k: int = 5) -> list[str]:
        """Query the brain (hybrid search) for memories relevant to this turn."""
        try:
            out = subprocess.run(
                [self.gbrain, "query", query, "--json", "--limit", str(k)],
                capture_output=True, timeout=30, check=False,
            ).stdout
            hits = json.loads(out or "[]")
            texts = [h.get("text") or h.get("snippet") or "" for h in hits]
            out_list, budget = [], self.char_limit
            for t in texts:
                if t and len(t) <= budget:
                    out_list.append(t)
                    budget -= len(t)
            return out_list
        except Exception:
            return []

    def shutdown(self) -> None:
        pass
