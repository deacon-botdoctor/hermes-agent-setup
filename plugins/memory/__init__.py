"""memory — the agent's long-term recall plugin.

Two providers, same interface:
- `SqliteMemoryProvider` — local, zero-dependency (SQLite-FTS). Runnable + tested. Good default.
- `GBrainMemoryProvider` — backs recall with a running GBrain library, so what the agent learns
  in chat feeds the same brain that holds your canonical knowledge.

Pick one at register time and wire it to your runtime's memory-provider registration. Memory is
a first-class extension point, so this is a plugin, never a patch.
"""
from __future__ import annotations

from .sqlite_provider import SqliteMemoryProvider
from .gbrain_provider import GBrainMemoryProvider

__all__ = ["SqliteMemoryProvider", "GBrainMemoryProvider"]


def register(ctx) -> None:
    # provider = SqliteMemoryProvider(char_limit=2200)          # local default
    # provider = GBrainMemoryProvider(char_limit=2200)          # GBrain-backed
    # ctx.register_memory_provider(provider)
    raise NotImplementedError("choose a provider and wire it to register_memory_provider")
