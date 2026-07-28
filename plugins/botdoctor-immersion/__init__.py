"""Bot Doctor immersion plugin pack."""

from __future__ import annotations

from .middleware import llm_request_middleware


def register(ctx) -> None:
    """Register the proven exactly-once operating-floor middleware."""
    ctx.register_middleware("llm_request", llm_request_middleware)
