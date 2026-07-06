"""memory — a long-term recall plugin (skeleton).

Registers a memory provider that persists durable facts and prefetches relevant ones into
context. Memory is a first-class extension point, so this is a plugin, never a patch.

Drop this package where your runtime discovers user plugins (a plugins directory, or install it
as a package exposing the plugin entry point). The runtime calls register(ctx) at startup.

Fill in the provider body against your runtime's memory-provider interface — typically hooks on
the turn cycle: one to write memories after a turn, one to prefetch before it, and setup/teardown.
"""
from __future__ import annotations


class RecallProvider:
    """Persist and retrieve durable memories across sessions.

    Implement these against your runtime's memory-provider base class. Names shown are the
    common shape; match them to your version.
    """

    def post_setup(self, ctx) -> None:
        """Open your store (sqlite, vector db, files). Called once at startup."""
        raise NotImplementedError

    def sync_turn(self, session_id: str, messages: list) -> None:
        """After a turn: extract and persist anything worth remembering."""
        raise NotImplementedError

    def prefetch(self, session_id: str, query: str) -> list:
        """Before a turn: return relevant memories to fold into context."""
        raise NotImplementedError

    def shutdown(self) -> None:
        """Flush and close the store."""
        raise NotImplementedError


def register(ctx) -> None:
    """Plugin entry point. The runtime calls this at startup."""
    # ctx.register_memory_provider(RecallProvider())   # match your runtime's API
    raise NotImplementedError("wire RecallProvider to your runtime's memory-provider registration")
