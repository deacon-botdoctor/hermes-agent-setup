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
    # ctx.register_hook("transform_llm_output", hooks.transform_llm_output)
    # ctx.register_middleware("llm_request", middleware.llm_request_middleware)
    # mode_command.register(ctx)
    raise NotImplementedError(
        "wire hooks.transform_llm_output + middleware.llm_request_middleware + mode_command "
        "to your runtime's register_hook / register_middleware / register_command"
    )
