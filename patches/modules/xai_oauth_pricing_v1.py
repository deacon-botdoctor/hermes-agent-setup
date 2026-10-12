"""Do not fetch API rate cards to estimate subscription-route usage."""
from pathlib import Path

MARKER = "HERMES_XAI_OAUTH_PRICING_v1"
ANCHOR = '''    if route.billing_mode == "subscription_included":
        return _INCLUDED_ENTRY
'''
REPLACEMENT = ANCHOR + '''    # HERMES_XAI_OAUTH_PRICING_v1: API rate cards cannot price this OAuth route.
    # Preserve unknown cost; do not block a reply on irrelevant /models probes.
    if route.provider == "xai-oauth":
        return None
'''


def patch_xai_oauth_pricing_v1(hermes_dir):
    path = Path(hermes_dir) / "agent/usage_pricing.py"
    source = path.read_text(encoding="utf-8")
    if REPLACEMENT in source:
        return False
    if MARKER in source or source.count(ANCHOR) != 1:
        raise RuntimeError("xai OAuth pricing anchor changed")
    updated = source.replace(ANCHOR, REPLACEMENT, 1)
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8")
    return True
