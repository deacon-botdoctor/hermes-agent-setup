#!/usr/bin/env python3
"""Use Hermes' native turn and tool seams for a lean, bounded agent loop."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_LEAN_AGENT_LOOP_v1"
TEST_MARKER = "HERMES_LEAN_AGENT_LOOP_TEST_v1"

HISTORY_TOOL_ANCHOR = '''    if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages):
        return False
'''
HISTORY_TOOL_REPLACEMENT = f'''    # [{MARKER}] Action evidence is scoped to this user turn. A tool
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
'''

ACK_ANCHOR = '''    has_future_ack = bool(
        re.search(r"\\b(i['’]ll|i will|let me|i can do that|i can help with that)\\b", assistant_text)
    )
    if not has_future_ack:
        return False
'''
ACK_REPLACEMENT = '''    has_future_ack = bool(
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
'''

ACTION_ANCHOR = '''        "analyz",
        "review",
'''
ACTION_REPLACEMENT = '''        "analyz",
        "trac",
        "investigat",
        "review",
'''

RETRY_ANCHOR = "                    and codex_ack_continuations < 2\n"
RETRY_REPLACEMENT = f"                    and codex_ack_continuations < 1  # {MARKER}\n"

PLANNER_SIGNATURE_ANCHOR = (
    "def _plan_tool_batch_segments(tool_calls, *, execution_cwd: Optional[Path] = None) "
    "-> List[tuple]:\n"
)
PLANNER_SIGNATURE_REPLACEMENT = f'''def _plan_tool_batch_segments(
    tool_calls,
    *,
    execution_cwd: Optional[Path] = None,
    allow_single_parallel: bool = False,  # {MARKER}
) -> List[tuple]:
'''
PLANNER_DEMOTION_ANCHOR = '''        if kind == "parallel" and len(calls) < 2:
            kind = "sequential"
'''
PLANNER_DEMOTION_REPLACEMENT = '''        if kind == "parallel" and len(calls) < 2 and not allow_single_parallel:
            kind = "sequential"
'''

DISPATCH_ANCHOR = '''            if len(tool_calls) <= 1:
                return self._execute_tool_calls_sequential(
                    assistant_message, messages, effective_task_id, api_call_count
                )

            from agent.tool_dispatch_helpers import _plan_tool_batch_segments
            _active_env = get_active_env(effective_task_id)
            _exec_cwd = Path(_active_env.cwd) if _active_env is not None and _active_env.cwd else None
            segments = _plan_tool_batch_segments(tool_calls, execution_cwd=_exec_cwd)
'''
DISPATCH_REPLACEMENT = f'''            # [{MARKER}] A single safe call uses Hermes' existing guarded
            # worker path too. Interactive, unknown, and unsafe calls remain
            # sequential barriers with their richer inline behavior.
            from agent.tool_dispatch_helpers import _plan_tool_batch_segments
            _active_env = get_active_env(effective_task_id)
            _exec_cwd = Path(_active_env.cwd) if _active_env is not None and _active_env.cwd else None
            segments = _plan_tool_batch_segments(
                tool_calls,
                execution_cwd=_exec_cwd,
                allow_single_parallel=(len(tool_calls) == 1),
            )
'''

LEGACY_SINGLE_TEST_ANCHOR = '''    def test_single_tool_uses_sequential_path(self, agent):
        """Single tool call should use sequential path, not concurrent."""
        tc = _mock_tool_call(name="web_search", arguments='{"q":"test"}', call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []
        with patch.object(agent, "_execute_tool_calls_sequential") as mock_seq:
            with patch.object(agent, "_execute_tool_calls_concurrent") as mock_con:
                agent._execute_tool_calls(mock_msg, messages, "task-1")
                mock_seq.assert_called_once()
                mock_con.assert_not_called()
'''
LEGACY_SINGLE_TEST_REPLACEMENT = f'''    def test_single_safe_tool_uses_guarded_worker_path(self, agent):
        """A safe single call receives the same timeout guard as a safe batch."""
        tc = _mock_tool_call(name="web_search", arguments='{{"q":"test"}}', call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []
        with patch.object(agent, "_execute_tool_calls_sequential") as mock_seq:
            with patch.object(agent, "_execute_tool_calls_concurrent") as mock_con:
                agent._execute_tool_calls(mock_msg, messages, "task-1")
                mock_con.assert_called_once()  # {TEST_MARKER}
                mock_seq.assert_not_called()
'''

INTENT_TEST_SOURCE = f'''

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
'''

TOOL_TEST_SOURCE = f'''

# [{TEST_MARKER}]
def test_single_safe_tool_uses_guarded_worker_path(agent):
    msg = SimpleNamespace(
        content="",
        tool_calls=[_tc("web_search", '{{"query":"health"}}')],
    )
    with (
        patch.object(agent, "_execute_tool_calls_concurrent") as concurrent,
        patch.object(agent, "_execute_tool_calls_sequential") as sequential,
    ):
        agent._execute_tool_calls(msg, [], "task-1")
    concurrent.assert_called_once()
    sequential.assert_not_called()


def test_single_interactive_tool_keeps_sequential_path(agent):
    msg = SimpleNamespace(
        content="",
        tool_calls=[_tc("clarify", '{{"question":"Continue?"}}')],
    )
    with (
        patch.object(agent, "_execute_tool_calls_concurrent") as concurrent,
        patch.object(agent, "_execute_tool_calls_sequential") as sequential,
    ):
        agent._execute_tool_calls(msg, [], "task-1")
    sequential.assert_called_once()
    concurrent.assert_not_called()
'''


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if new in source:
        return source
    if source.count(old) != 1:
        raise RuntimeError(f"lean-agent-loop {label} anchor drift")
    return source.replace(old, new, 1)


def patch_lean_agent_loop_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    paths = {
        "helper": root / "agent" / "agent_runtime_helpers.py",
        "conversation": root / "agent" / "conversation_loop.py",
        "planner": root / "agent" / "tool_dispatch_helpers.py",
        "runner": root / "run_agent.py",
        "intent_test": root / "tests" / "agent" / "test_intent_ack_continuation.py",
        "tool_test": root / "tests" / "run_agent" / "test_tool_batch_segmentation.py",
        "runner_test": root / "tests" / "run_agent" / "test_run_agent.py",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"lean-agent-loop runtime file missing: {missing}")

    original = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    patched = dict(original)
    patched["helper"] = _replace_once(
        patched["helper"], HISTORY_TOOL_ANCHOR, HISTORY_TOOL_REPLACEMENT, "current-turn evidence"
    )
    patched["helper"] = _replace_once(
        patched["helper"], ACK_ANCHOR, ACK_REPLACEMENT, "ack classifier"
    )
    patched["helper"] = _replace_once(
        patched["helper"], ACTION_ANCHOR, ACTION_REPLACEMENT, "action markers"
    )
    patched["conversation"] = _replace_once(
        patched["conversation"], RETRY_ANCHOR, RETRY_REPLACEMENT, "retry bound"
    )
    patched["planner"] = _replace_once(
        patched["planner"], PLANNER_SIGNATURE_ANCHOR, PLANNER_SIGNATURE_REPLACEMENT, "planner signature"
    )
    patched["planner"] = _replace_once(
        patched["planner"], PLANNER_DEMOTION_ANCHOR, PLANNER_DEMOTION_REPLACEMENT, "single-call plan"
    )
    patched["runner"] = _replace_once(
        patched["runner"], DISPATCH_ANCHOR, DISPATCH_REPLACEMENT, "single-call dispatch"
    )
    patched["runner_test"] = _replace_once(
        patched["runner_test"],
        LEGACY_SINGLE_TEST_ANCHOR,
        LEGACY_SINGLE_TEST_REPLACEMENT,
        "single-call runtime test",
    )
    if TEST_MARKER not in patched["intent_test"]:
        patched["intent_test"] = patched["intent_test"].rstrip() + "\n" + INTENT_TEST_SOURCE
    if TEST_MARKER not in patched["tool_test"]:
        patched["tool_test"] = patched["tool_test"].rstrip() + "\n" + TOOL_TEST_SOURCE

    changed = False
    for name, path in paths.items():
        if patched[name] != original[name]:
            path.write_text(patched[name], encoding="utf-8")
            changed = True
    return changed


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    print("patched" if patch_lean_agent_loop_v1(args.hermes_dir) else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
