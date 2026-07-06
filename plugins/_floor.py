from __future__ import annotations

from typing import Any


def register_noop(ctx: Any, name: str, description: str) -> dict[str, str]:
    logger = getattr(ctx, "logger", None)
    message = f"{name}: floor placeholder loaded; {description}"
    if logger and hasattr(logger, "info"):
        logger.info(message)
    return {"plugin": name, "status": "placeholder", "description": description}
