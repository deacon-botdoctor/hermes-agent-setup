#!/usr/bin/env python3
"""Release rejected native clarifies before active-turn steering.

Idempotent via marker: HERMES_CLARIFY_PROSE_DEADLOCK_GUARD_v1
Targets: gateway/run.py, tools/clarify_gateway.py, and their focused tests
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

MARKER = "HERMES_CLARIFY_PROSE_DEADLOCK_GUARD_v1"

TOOLS_ANCHOR = """    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None:
            return False
    entry.response = str(response) if response is not None else ""
    entry.event.set()
    return True
"""

TOOLS_REPLACEMENT = f"""    with _lock:
        entry = _entries.get(clarify_id)
        # [{MARKER}] The first button, text, or cancellation result wins.
        # Keeping the check and mutation under one lock prevents rejected
        # prose from overwriting a native-button result that just landed.
        if entry is None or entry.event.is_set():
            return False
        entry.response = str(response) if response is not None else ""
        entry.event.set()
        return True
"""

GATEWAY_ANCHOR = """                    return ""

        # Intercept messages that are responses to a pending /reload-mcp
"""

GATEWAY_REPLACEMENT = f"""                    return ""
                # [{MARKER}] Native-choice prompts deliberately reject
                # unmatched prose. Release that clarify with the existing
                # empty cancellation sentinel before normal busy routing:
                # redirect() becomes steer() while a tool is executing, and
                # steer cannot drain until this clarify tool returns.
                _clarify_mod.resolve_gateway_clarify(
                    _pending_clarify.clarify_id,
                    "",
                )

        # Intercept messages that are responses to a pending /reload-mcp
"""

TOOLS_TEST_ANCHOR = """        result = cm.wait_for_response("id1", timeout=10.0)
        assert result == "B"

    def test_open_ended_auto_awaits_text(self):
"""

TOOLS_TEST_REPLACEMENT = '''        result = cm.wait_for_response("id1", timeout=10.0)
        assert result == "B"

    def test_first_resolution_wins(self):
        """A late cancellation must not overwrite an already-selected choice."""
        from tools import clarify_gateway as cm

        entry = cm.register("id-race", "sk-race", "Pick one", ["A", "B"])

        assert cm.resolve_gateway_clarify("id-race", "A") is True
        assert cm.resolve_gateway_clarify("id-race", "") is False
        assert entry.response == "A"

    def test_open_ended_auto_awaits_text(self):
'''

GATEWAY_TEST_ANCHOR = """    # The clarify entry must still be pending and unresolved.
    with cm._lock:
        entry = cm._entries.get("cl-native")
    assert entry is not None
    assert not entry.event.is_set()
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_prose_still_accepted_after_other_flips_text_capture():
"""

GATEWAY_TEST_REPLACEMENT = '''    # The prose is not accepted as the answer, but the clarify must be
    # released before normal busy routing so redirect-to-steer can drain.
    with cm._lock:
        entry = cm._entries.get("cl-native")
    assert entry is not None
    assert entry.event.is_set()
    assert entry.response == ""
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_thread_prose_does_not_overwrite_concurrent_button_choice():
    """A button result that wins the race remains the clarify response."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    entry = cm.register(
        "cl-button-race",
        SESSION_KEY,
        "Pick a UI variant",
        ["buttons", "dropdown"],
    )
    assert cm.resolve_gateway_clarify("cl-button-race", "buttons") is True

    with pytest.raises(_FellThroughIntercept):
        await _dispatch(runner, _event("one more unrelated thought"))

    assert entry.event.is_set()
    assert entry.response == "buttons"
    _clear_clarify_state()


@pytest.mark.asyncio
async def test_prose_still_accepted_after_other_flips_text_capture():
'''


def patch_tools_source(source: str) -> str:
    """Return clarify_gateway.py with atomic first-resolution semantics."""
    if MARKER in source:
        required = (
            "if entry is None or entry.event.is_set():",
            "entry.response = str(response) if response is not None else",
        )
        if not all(seam in source for seam in required):
            raise RuntimeError("marked clarify resolver is missing required seams")
        return source
    if source.count(TOOLS_ANCHOR) != 1:
        raise RuntimeError("clarify resolver anchor drift")
    patched = source.replace(TOOLS_ANCHOR, TOOLS_REPLACEMENT, 1)
    ast.parse(patched)
    return patched


def patch_gateway_source(source: str) -> str:
    """Return gateway/run.py with the rejected-prose deadlock broken."""
    if MARKER in source:
        required = (
            "_clarify_mod.resolve_gateway_clarify(",
            "_pending_clarify.clarify_id,",
        )
        if not all(seam in source for seam in required):
            raise RuntimeError("marked gateway clarify guard is missing required seams")
        return source
    if source.count(GATEWAY_ANCHOR) != 1:
        raise RuntimeError("gateway clarify intercept anchor drift")
    patched = source.replace(GATEWAY_ANCHOR, GATEWAY_REPLACEMENT, 1)
    ast.parse(patched)
    return patched


def patch_tools_test_source(source: str) -> str:
    """Return the clarify primitive test with first-wins coverage."""
    if "def test_first_resolution_wins(self):" in source:
        return source
    if source.count(TOOLS_TEST_ANCHOR) != 1:
        raise RuntimeError("clarify primitive test anchor drift")
    patched = source.replace(TOOLS_TEST_ANCHOR, TOOLS_TEST_REPLACEMENT, 1)
    ast.parse(patched)
    return patched


def patch_gateway_test_source(source: str) -> str:
    """Return the gateway follow-up test with release and race coverage."""
    if "test_thread_prose_does_not_overwrite_concurrent_button_choice" in source:
        required = ('assert entry.response == ""', 'assert entry.response == "buttons"')
        if not all(seam in source for seam in required):
            raise RuntimeError("marked gateway clarify test is missing required seams")
        return source
    if source.count(GATEWAY_TEST_ANCHOR) != 1:
        raise RuntimeError("gateway clarify test anchor drift")
    patched = source.replace(GATEWAY_TEST_ANCHOR, GATEWAY_TEST_REPLACEMENT, 1)
    ast.parse(patched)
    return patched


def patch_clarify_prose_deadlock_guard_v1(hermes_dir: Path) -> bool:
    """Apply both runtime seams after validating the complete postimage."""
    root = Path(hermes_dir)
    tools_path = root / "tools" / "clarify_gateway.py"
    gateway_path = root / "gateway" / "run.py"
    tools_test_path = root / "tests" / "tools" / "test_clarify_gateway.py"
    gateway_test_path = root / "tests" / "gateway" / "test_clarify_thread_followup_not_swallowed.py"
    targets = (tools_path, gateway_path, tools_test_path, gateway_test_path)
    for target in targets:
        if not target.is_file():
            raise RuntimeError(f"clarify deadlock target missing: {target}")

    originals = {target: target.read_text(encoding="utf-8") for target in targets}
    patched = {
        tools_path: patch_tools_source(originals[tools_path]),
        gateway_path: patch_gateway_source(originals[gateway_path]),
        tools_test_path: patch_tools_test_source(originals[tools_test_path]),
        gateway_test_path: patch_gateway_test_source(originals[gateway_test_path]),
    }

    changed = False
    for target in targets:
        if patched[target] != originals[target]:
            target.write_text(patched[target], encoding="utf-8")
            changed = True
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-dir", required=True, type=Path)
    args = parser.parse_args()
    changed = patch_clarify_prose_deadlock_guard_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
