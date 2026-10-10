"""Use Hermes' native turn and tool seams for a lean, bounded agent loop."""


from __future__ import annotations


from pathlib import Path


MARKER = "HERMES_LEAN_AGENT_LOOP_v1"


TEST_MARKER = "HERMES_LEAN_AGENT_LOOP_TEST_v1"


HISTORY_TOOL_ANCHOR = """    if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages):
        return False
"""


HISTORY_TOOL_REPLACEMENT = f"""    # [{MARKER}] Action evidence is scoped to this user turn. A tool
    # result from yesterday must not disable the guard for today's request.
    _current_user_index = -1
    for _index in range(len(messages) - 1, -1, -1):
        _message = messages[_index]
        if isinstance(_message, dict) and _message.get("role") == "user":
            _current_user_index = _index
            break
    _current_turn = messages[_current_user_index + 1 :]
    if any(
        isinstance(msg, dict) and msg.get("role") == "tool"
        for msg in _current_turn
    ):
        return False
"""


ACK_ANCHOR = """    has_future_ack = bool(
        re.search(r"\\b(i['’]ll|i will|let me|i can do that|i can help with that)\\b", assistant_text)
    )
    if not has_future_ack:
        return False
"""


ACK_REPLACEMENT = """    has_future_ack = bool(
        re.search(r"\\b(i['’]ll|i will|let me|i can do that|i can help with that)\\b", assistant_text)
    )
    _progress_verbs = (
        "checking|tracing|investigating|reviewing|testing|fixing|searching|"
        "reading|opening|running|debugging|scanning|analyzing|exploring"
    )
    has_progress_ack = bool(
        re.search(
            rf"\\b(i['’]m|i am)\\s+(?:currently\\s+)?(?:{_progress_verbs})\\b",
            assistant_text,
        )
        or re.search(
            rf"^\\s*(?:{_progress_verbs})\\b.{{0,200}}\\b(?:right\\s+)?now\\b",
            assistant_text,
        )
    )
    if not (has_future_ack or has_progress_ack):
        return False
"""


INTENT_TEST_SOURCE = f"""

# [{TEST_MARKER}]
def test_action_evidence_is_scoped_to_current_turn():
    agent = _agent(True, "chat_completions")
    user = "Please trace why the bot missed this."
    history = [
        {{"role": "user", "content": "old request"}},
        {{"role": "tool", "content": "old result"}},
        {{"role": "user", "content": user}},
    ]
    assert looks_like_codex_intermediate_ack(
        agent, user, "I'm tracing the ingress path now.", history,
        require_workspace=False,
    )
    assert not looks_like_codex_intermediate_ack(
        agent, user, "I'm tracing the ingress path now.",
        history + [{{"role": "tool", "content": "started"}}],
        require_workspace=False,
    )


def test_progress_explanation_is_not_an_action_stub():
    agent = _agent(True, "chat_completions")
    user = "Explain tracing."
    assert not looks_like_codex_intermediate_ack(
        agent, user, "Tracing is a debugging technique.",
        [{{"role": "user", "content": user}}], require_workspace=False,
    )
"""


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if new in source:
        return source
    if source.count(old) != 1:
        raise RuntimeError(f"lean-agent-loop {label} anchor drift")
    return source.replace(old, new, 1)


def _patch_native_lean(root: Path) -> bool:
    targets = {"helper": root / "agent/agent_runtime_helpers.py",
               "final": root / "agent/turn_final_response.py",
               "test": root / "tests/agent/test_intent_ack_continuation.py"}
    original = {key: path.read_text() for key, path in targets.items()}
    patched = dict(original)
    # A refusal is a complete response, not a promise to perform the action.
    ack_old = r"\b(i['’]ll|i will|let me|i can do that|i can help with that)\b"
    ack_new = ack_old + r"(?!\s+(?:not|never)\b)"
    if ack_new not in patched["helper"]:
        if patched["helper"].count(ack_old) != 1:
            raise RuntimeError("lean-agent-loop negated acknowledgement anchor drift")
        patched["helper"] = patched["helper"].replace(ack_old, ack_new, 1)
    patched["helper"] = _replace_once(patched["helper"], HISTORY_TOOL_ANCHOR,
                                       HISTORY_TOOL_REPLACEMENT, "native current-turn evidence")
    native_ack = '    if not _ACK_FUTURE_RE.search(assistant_text):\n        return False\n'
    replacement = ACK_REPLACEMENT.replace(ACK_ANCHOR.split("    if not has_future_ack:", 1)[0],
                                         "    has_future_ack = bool(_ACK_FUTURE_RE.search(assistant_text))\n")
    patched["helper"] = _replace_once(patched["helper"], native_ack, replacement, "native acknowledgement")
    patched["helper"] = _replace_once(patched["helper"], '"analyz", "review",',
                                       '"analyz", "trac", "investigat", "review",', "native action markers")
    old = "        and codex_ack_continuations < 2\n"
    new = f"        and codex_ack_continuations < 1  # {MARKER}\n"
    if new not in patched["final"]:
        if patched["final"].count(old) != 3:
            raise RuntimeError("native lean continuation bounds drift")
        patched["final"] = patched["final"].replace(old, new).replace('"(%d/2)"', '"(%d/1)"')
    if TEST_MARKER not in patched["test"]:
        patched["test"] = patched["test"].rstrip() + "\n" + INTENT_TEST_SOURCE
    # Native sequential dispatch already runs noninteractive tools in a daemon
    # worker with interrupt/deadline polling; preserve its side-effect barriers.
    for key, content in patched.items():
        compile(content, str(targets[key]), "exec")
    changed = False
    for key, path in targets.items():
        if patched[key] != original[key]:
            path.write_text(patched[key])
            changed = True
    return changed


def patch_lean_agent_loop_v1(hermes_dir: Path) -> bool:
    return _patch_native_lean(Path(hermes_dir))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    print("patched" if patch_lean_agent_loop_v1(args.hermes_dir) else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
