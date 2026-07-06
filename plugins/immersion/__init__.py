"""immersion — the reply-rules plugin.

Registers the outbound output transforms, the request-history middleware, and the /mode command.
This is where message-quality lives as a durable plugin (survives upstream bumps) instead of as
fragile source patches. See plugin.yaml for the seams it uses.

Wire register(ctx) to your runtime's plugin registration API — the method names below are the
common shape; match them to your version.
"""
from __future__ import annotations

from . import hooks, middleware, mode_command


def register(ctx) -> None:
    """Best-effort registration against the common plugin-API shape.

    Never raises: a missing API method logs a warning and skips, so installing this plugin can't
    crash plugin discovery. If nothing wired, the warning tells you to wire it by hand.
    """
    import logging

    log = logging.getLogger("immersion")
    wired = False
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("transform_llm_output", hooks.transform_llm_output)
        wired = True
    if hasattr(ctx, "register_middleware"):
        ctx.register_middleware("llm_request", middleware.llm_request_middleware)
        wired = True
    try:
        if mode_command.register(ctx):
            wired = True
    except Exception:  # /mode is optional; never let it break loading
        pass
    if not wired:
        log.warning(
            "immersion: no known registration API on ctx; wire transform_llm_output + "
            "llm_request middleware manually (see plugin.yaml)."
        )
