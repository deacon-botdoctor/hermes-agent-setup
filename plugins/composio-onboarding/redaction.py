"""Install Composio project-key masking into the pinned Hermes redactor."""

from __future__ import annotations

import re

COMPOSIO_PATTERN = r"ak_[A-Za-z0-9_-]{10,}"


def install_composio_key_redaction() -> bool:
    """Extend the already-loaded runtime redactor without another Golden patch."""
    from agent import redact

    patterns = getattr(redact, "_PREFIX_PATTERNS", None)
    if not isinstance(patterns, list):
        raise RuntimeError("Hermes redactor prefix registry is unavailable")
    if COMPOSIO_PATTERN in patterns:
        return False

    patterns.append(COMPOSIO_PATTERN)
    redact._PREFIX_RE = re.compile(
        r"(?<![A-Za-z0-9_-])(" + "|".join(patterns) + r")(?![A-Za-z0-9_-])"
    )
    substrings = list(getattr(redact, "_PREFIX_SUBSTRINGS", ()))
    if "ak_" not in substrings:
        substrings.append("ak_")
    redact._PREFIX_SUBSTRINGS = tuple(substrings)
    return True
