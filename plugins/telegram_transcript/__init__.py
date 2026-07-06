from __future__ import annotations

try:
    from plugins._floor import register_noop
except Exception:
    from .._floor import register_noop


def register(ctx):
    return register_noop(ctx, "Telegram Transcript", "wire Telegram transcript read/search tools here")
