"""/mode command — flip queue<->interrupt at runtime.

`queue`: a message that arrives mid-turn waits for the current turn to finish (in-flight work is
preserved). This is the client-safe default.
`interrupt`: a new message drops the current turn. Useful for power users who want to redirect
the agent immediately.

The command persists the choice to config so it survives a restart. If your config is
freeze-protected, unfreeze -> write -> refreeze.
"""
from __future__ import annotations

VALID = ("queue", "interrupt")


def handle_mode(raw_args: str, *, get_config, set_config) -> str:
    """Handle `/mode [queue|interrupt]`. Returns the message to send back."""
    arg = (raw_args or "").strip().lower()
    if not arg:
        current = get_config("busy_input_mode") or "queue"
        return f"Input mode is **{current}**. Use `/mode queue` or `/mode interrupt` to change it."
    if arg not in VALID:
        return f"Unknown mode {arg!r}. Choose `queue` or `interrupt`."
    set_config("busy_input_mode", arg)
    if arg == "queue":
        return "Mode set to **queue** — new messages wait for the current turn to finish."
    return "Mode set to **interrupt** — a new message will drop whatever I'm doing."


def register(ctx) -> None:
    """Register /mode if the runtime exposes a command API. No-op (returns) otherwise."""
    if not (hasattr(ctx, "register_command") and hasattr(ctx, "get_config") and hasattr(ctx, "set_config")):
        return  # command/config API not present; caller falls back to config default
    ctx.register_command(
        "mode",
        lambda raw: handle_mode(raw, get_config=ctx.get_config, set_config=ctx.set_config),
    )
