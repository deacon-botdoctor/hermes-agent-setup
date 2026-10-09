"""Let a turn walk its whole configured fallback chain even when api_max_retries is small.

Root cause (Enoch failover test 2026-10-07 12:25 CT): every fallback hop arms
``restart_with_rebuilt_messages``; ``apply_retry_restarts`` ends the turn with
``rebuilt_restart_limit_exceeded`` once ``restart_count > max_retries``. With
agent.api_max_retries=1 and a 2-entry chain (grok-4.7 -> grok-4.6) the second hop is cut
before grok-4.6 is ever called, so the turn returns an empty response.

Change: the rebuilt-restart bound becomes max(max_retries, len(agent._fallback_chain)).
It stays finite (runaway protection from #106108 is kept); redirect restarts are unchanged.
"""
from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_FALLBACK_CHAIN_RESTART_BOUND_v1"

OLD = '''    if _retry.restart_with_rebuilt_messages:
        restart_count += 1
        if restart_count > max_retries:
'''
NEW = '''    if _retry.restart_with_rebuilt_messages:
        restart_count += 1
        # [HERMES_FALLBACK_CHAIN_RESTART_BOUND_v1] each fallback hop is one rebuilt restart;
        # a small api_max_retries must not cut the configured chain short.
        _rebuilt_limit = max(int(max_retries or 0), len(getattr(agent, "_fallback_chain", None) or ()))
        if restart_count > _rebuilt_limit:
'''
OLD_LOG = '''                "Rebuilt-message restart limit (%s) exceeded; ending turn instead of "
                "refunding the iteration budget indefinitely.",
                max_retries,
'''
NEW_LOG = '''                "Rebuilt-message restart limit (%s) exceeded; ending turn instead of "
                "refunding the iteration budget indefinitely.",
                _rebuilt_limit,
'''


def patch_fallback_chain_restart_bound_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "agent/turn_iteration_prep.py"
    source = target.read_text()
    if MARKER in source:
        return False
    for old, new, label in ((OLD, NEW, "rebuilt bound"), (OLD_LOG, NEW_LOG, "rebuilt log")):
        if source.count(old) != 1:
            raise RuntimeError(f"{MARKER}: anchor drift ({label})")
        source = source.replace(old, new, 1)
    compile(source, str(target), "exec")
    target.write_text(source)
    return True
