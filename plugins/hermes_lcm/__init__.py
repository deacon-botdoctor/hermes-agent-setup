from __future__ import annotations

try:
    from plugins._floor import register_noop
except Exception:
    from .._floor import register_noop


def register(ctx):
    if hasattr(ctx, "register_context_engine"):
        return register_noop(ctx, "hermes-lcm", "register the LCM context engine in this seam")
    return register_noop(ctx, "hermes-lcm", "runtime has no context-engine registration API")
