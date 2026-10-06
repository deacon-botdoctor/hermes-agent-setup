#!/usr/bin/env python3
"""Read the native Hermes MEMORY.md and USER.md size limits."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MEMORY_CHAR_LIMIT = 2200
DEFAULT_USER_CHAR_LIMIT = 1375


def load_memory_limits(config_path: Path | None = None) -> dict[str, int]:
    """Return configured native limits, falling back safely to upstream defaults."""
    path = config_path or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml"
    limits = {
        "memory": DEFAULT_MEMORY_CHAR_LIMIT,
        "user": DEFAULT_USER_CHAR_LIMIT,
    }
    try:
        import yaml

        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        memory = config.get("memory", {}) or {}
        configured = {
            "memory": memory.get("memory_char_limit"),
            "user": memory.get("user_char_limit"),
        }
        for target, value in configured.items():
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                limits[target] = parsed
    except Exception:
        pass
    return limits


def char_limit_for_target(target: str, config_path: Path | None = None) -> int:
    key = "user" if target == "user" else "memory"
    return load_memory_limits(config_path)[key]


def automatic_memory_review_enabled(config_path: Path | None = None) -> bool:
    """Nightly writers obey the native review switch; unknown means off."""
    path = config_path or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml"
    try:
        import yaml
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        interval = config.get("memory", {}).get("nudge_interval", 0)
        return not isinstance(interval, bool) and int(interval) > 0
    except Exception:
        return False
