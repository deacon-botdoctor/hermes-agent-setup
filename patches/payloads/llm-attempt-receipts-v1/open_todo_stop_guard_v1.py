"""Patch support for the LLM-turn unit's current-turn todo stop guard."""

from __future__ import annotations

from pathlib import Path
import hashlib

MARKER = "HERMES_OPEN_TODO_STOP_GUARD_v1"
PREVIOUS_HELPER_SHA256 = "28ffde411c7cbd9aa2a9aa8e97377f5a13c44c9c06c6fe2b19bf56c4a163ebde"
PREVIOUS_TEST_SHA256 = "e4092ca762123df669297f1c25e5c4c9e44d1cf76ccc2e7786e5704d879e659c"

HELPER_SOURCE = '''"""Language-neutral stop guard for current-turn todo plans."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


MAX_OPEN_TODO_STOP_NUDGES = 2
SYNTHETIC_FLAG = "_open_todo_stop_synthetic"


def _tool_call_parts(tool_call: Any) -> tuple[str, str, Any]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        name = function.get("name", "") if isinstance(function, dict) else ""
        arguments = function.get("arguments") if isinstance(function, dict) else None
        call_id = tool_call.get("id", "")
    else:
        function = getattr(tool_call, "function", None)
        name = getattr(function, "name", "") if function is not None else ""
        arguments = getattr(function, "arguments", None)
        call_id = getattr(tool_call, "id", "")
    return str(name or "").strip(), str(call_id or "").strip(), arguments


def _current_real_user_index(messages: List[Dict[str, Any]]) -> Optional[int]:
    # Both stop guards use one logical user-turn boundary across their nudges.
    from agent.outcome_stop import _current_real_user_index as outcome_user_index
    return outcome_user_index(messages)


def _current_turn_written_todo_ids(messages: List[Dict[str, Any]]) -> set[str]:
    from agent.outcome_stop import _tool_result_succeeded
    user_index = _current_real_user_index(messages)
    if user_index is None:
        return set()
    calls: dict[str, set[str]] = {}
    written: set[str] = set()
    for message in messages[user_index + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                name, call_id, arguments = _tool_call_parts(tool_call)
                if name != "todo" or not call_id:
                    continue
                try:
                    args = json.loads(arguments) if isinstance(arguments, str) else arguments
                    todos = args.get("todos") if isinstance(args, dict) else None
                    todos = json.loads(todos) if isinstance(todos, str) else todos
                except (ValueError, TypeError):
                    continue
                if isinstance(todos, list):
                    calls[call_id] = {str(item.get("id", "")).strip() or "?"
                                      for item in todos if isinstance(item, dict)}
        elif message.get("role") == "tool":
            ids = calls.pop(str(message.get("tool_call_id") or "").strip(), set())
            content = message.get("content")
            if not ids or not _tool_result_succeeded(content):
                continue
            try:
                result = json.loads(content) if isinstance(content, str) else content
            except (ValueError, TypeError):
                continue
            items = result.get("todos") if isinstance(result, dict) else None
            if isinstance(items, list):
                written.update(ids.intersection(str(item.get("id", "")).strip()
                                                for item in items if isinstance(item, dict)))
    return written


def build_open_todo_stop_nudge(
    *,
    agent: Any,
    messages: List[Dict[str, Any]],
    attempts: int,
) -> Optional[str]:
    """Return a bounded continuation nudge for an unfinished current-turn plan.

    The todo tool is model-authored lifecycle state. A plan created in this
    user turn is therefore a language-neutral declaration that work remains.
    Prior-turn plans never qualify, and unpaired caller-supplied tool rows do
    not qualify.
    """
    if attempts >= MAX_OPEN_TODO_STOP_NUDGES:
        return None
    written_ids = _current_turn_written_todo_ids(messages)
    if not written_ids:
        return None

    store = getattr(agent, "_todo_store", None)
    read = getattr(store, "read", None)
    if not callable(read):
        return None
    try:
        items = read()
    except Exception:
        return None

    active = [
        item
        for item in items or []
        if isinstance(item, dict)
        and str(item.get("id", "")).strip() in written_ids
        and str(item.get("status") or "").strip().lower()
        in {"pending", "in_progress"}
    ]
    if not active:
        return None

    return (
        "[System: Current-turn todos remain active. Complete the remaining "
        "work and update their status before finalizing; cancel only work "
        "that is no longer required.]"
    )
'''


CONVERSATION_ANCHOR = '''                try:
                    from agent.verification_stop import (
'''

CONVERSATION_INSERT = f'''                # {MARKER}: a todo plan created in this user turn is
                # language-neutral evidence that the work is not complete. Keep
                # the foreground turn alive instead of delivering a plan-only
                # acknowledgement as the final answer.
                try:
                    from agent.open_todo_stop import build_open_todo_stop_nudge

                    _open_todo_nudge = build_open_todo_stop_nudge(
                        agent=agent,
                        messages=messages,
                        attempts=getattr(agent, "_open_todo_stop_nudges", 0),
                    )
                except Exception:
                    logger.debug("open-todo stop-loop check failed", exc_info=True)
                    _open_todo_nudge = None

                if _open_todo_nudge:
                    agent._open_todo_stop_nudges = (
                        getattr(agent, "_open_todo_stop_nudges", 0) + 1
                    )
                    final_msg["finish_reason"] = "open_todo_required"
                    final_msg["_open_todo_stop_synthetic"] = True
                    messages.append(final_msg)
                    messages.append({{
                        "role": "user",
                        "content": _open_todo_nudge,
                        "_open_todo_stop_synthetic": True,
                    }})
                    agent._session_messages = messages
                    logger.info(
                        "open-todo stop-loop nudge issued (attempt %d)",
                        agent._open_todo_stop_nudges,
                    )
                    agent._emit_status(
                        "↻ Todo plan is still open — continuing before final reply"
                    )
                    _pending_verification_response = final_response
                    _pending_verification_response_previewed = False
                    final_response = None
                    continue

'''


FINALIZER_OLD = '''_VERIFICATION_CONTINUATION_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
)
'''

FINALIZER_NEW = f'''_VERIFICATION_CONTINUATION_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_open_todo_stop_synthetic",  # {MARKER}
)
'''


TEST_SOURCE = '''"""Regression coverage for current-turn open-todo stop continuation."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.open_todo_stop import build_open_todo_stop_nudge
from run_agent import AIAgent
from tools.todo_tool import TodoStore


def _todo_messages(*, after_current_user=True, paired=True):
    call_id = "todo-current" if after_current_user else "todo-prior"
    pair = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "todo", "arguments": json.dumps({"todos": [{"id": "research", "content": "Research", "status": "in_progress"}]})},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id if paired else "different-call",
            "content": json.dumps({"todos": [{"id": "research", "content": "Research", "status": "in_progress"}]}),
        },
    ]
    if after_current_user:
        return [{"role": "user", "content": "research it"}, *pair]
    return [*pair, {"role": "user", "content": "new unrelated question"}]


def _helper_agent(status="in_progress"):
    store = TodoStore()
    store.write([{"id": "research", "content": "Research", "status": status}])
    return SimpleNamespace(_todo_store=store)


def test_helper_catches_current_turn_open_plan_without_language_matching():
    nudge = build_open_todo_stop_nudge(
        agent=_helper_agent(), messages=_todo_messages(), attempts=0
    )
    assert nudge is not None


@pytest.mark.parametrize(
    ("messages", "status", "attempts"),
    [
        (_todo_messages(after_current_user=False), "in_progress", 0),
        (_todo_messages(paired=False), "in_progress", 0),
        (_todo_messages(), "completed", 0),
        (_todo_messages(), "cancelled", 0),
        (_todo_messages(), "in_progress", 2),
    ],
)
def test_helper_ignores_nonqualifying_state(messages, status, attempts):
    assert build_open_todo_stop_nudge(
        agent=_helper_agent(status), messages=messages, attempts=attempts
    ) is None


def _response(content="final answer", *, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model",
        usage=None,
    )


def _todo_response(status):
    tool_call = SimpleNamespace(
        id=f"todo-{status}",
        type="function",
        function=SimpleNamespace(
            name="todo",
            arguments=json.dumps(
                {
                    "todos": [
                        {"id": "research", "content": "Research", "status": status}
                    ]
                }
            ),
        ),
    )
    return _response(content="", tool_calls=[tool_call], finish_reason="tool_calls")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="open-todo-stop-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=4,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance.valid_tool_names = ["todo"]
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def test_plan_only_stop_continues_until_todo_is_closed(agent, monkeypatch):
    answers = iter(
        [
            _todo_response("in_progress"),
            _response(
                "Fac cercetarea exactă acum și verific anul, prețul și valoarea."
            ),
            _todo_response("completed"),
            _response("Iată cercetarea completă și verdictul verificat."),
        ]
    )
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Caută totul și dă-mi verdictul.")

    assert result["api_calls"] == 4
    assert result["final_response"] == "Iată cercetarea completă și verdictul verificat."
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    assert agent._todo_store.read()[0]["status"] == "completed"
    assert not any(
        message.get("_open_todo_stop_synthetic")
        for message in result["messages"]
        if isinstance(message, dict)
    )
    agent._handle_max_iterations.assert_not_called()


def test_budget_exhaustion_preserves_candidate_and_strips_scaffolding(
    agent, monkeypatch
):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter(
        [
            _todo_response("in_progress"),
            _response("Fac cercetarea exactă acum și revin cu verdictul."),
        ]
    )
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Caută totul și dă-mi verdictul.")

    assert result["final_response"] == (
        "Fac cercetarea exactă acum și revin cu verdictul."
    )
    assert result["turn_exit_reason"] == "max_iterations_reached(2/2)"
    assert result["completed"] is False
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert not any(
        message.get("_open_todo_stop_synthetic")
        for message in result["messages"]
        if isinstance(message, dict)
    )
    agent._handle_max_iterations.assert_not_called()


@pytest.mark.parametrize('flag', ['_open_todo_stop_synthetic', '_outcome_stop_synthetic', '_verification_stop_synthetic', '_pre_verify_synthetic'])
def test_other_stop_nudges_preserve_todo_turn_and_real_user_resets_it(flag):
    messages = _todo_messages()
    messages.append({'role': 'user', 'content': 'Internal continuation', flag: True})
    assert build_open_todo_stop_nudge(agent=_helper_agent(), messages=messages, attempts=0) is not None
    messages.append({'role': 'user', 'content': 'A genuinely new request'})
    assert build_open_todo_stop_nudge(agent=_helper_agent(), messages=messages, attempts=0) is None


def test_outcome_then_todo_continuation_reaches_real_finalization(agent, monkeypatch):
    agent.platform = 'telegram'
    agent.max_iterations = 6
    answers = iter([
        _todo_response('in_progress'),
        _response('Blocked\\nI have not finished the research.'),
        _response('Here is the answer, but the plan is still open.'),
        _todo_response('completed'),
        _response('The plan is now complete.'),
    ])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    monkeypatch.setenv('HERMES_VERIFY_ON_STOP', '0')
    with patch('hermes_cli.plugins.has_hook', return_value=False), patch('hermes_cli.plugins.invoke_hook', return_value=[]):
        result = agent.run_conversation('Complete the research.')
    assert result['api_calls'] == 5
    assert result['completed'] is True and result['final_response'] == 'The plan is now complete.'
    assert agent._todo_store.read()[0]['status'] == 'completed'
    assert agent._outcome_stop_nudges == 1 and agent._open_todo_stop_nudges == 1


def test_cached_agent_gets_new_todo_nudge_budget_each_real_turn(agent, monkeypatch):
    agent.max_iterations = 6
    agent.iteration_budget.max_total = 6
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    first_answers = iter([
        _todo_response("in_progress"), _response("First candidate"),
        _response("Second candidate"), _response("Third candidate"),
    ])
    agent._interruptible_api_call = lambda _kwargs: next(first_answers)
    with patch("hermes_cli.plugins.has_hook", return_value=False), patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        first = agent.run_conversation("First real user task")
        assert first["api_calls"] == 4
        assert agent._open_todo_stop_nudges == 2
        second_answers = iter([
            _todo_response("in_progress"), _response("Premature second task answer"),
            _todo_response("completed"), _response("Completed second task"),
        ])
        agent._interruptible_api_call = lambda _kwargs: next(second_answers)
        second = agent.run_conversation("Second distinct real user task", conversation_history=first["messages"])
    assert second["api_calls"] == 4
    assert second["final_response"] == "Completed second task"
    assert second["completed"] is True
    assert agent._todo_store.read()[0]["status"] == "completed"
    assert agent._open_todo_stop_nudges == 1



def test_truncated_tool_retry_keeps_real_turn_and_strips_internal_prompt(agent, monkeypatch):
    agent.max_iterations = 6
    agent.iteration_budget.max_total = 6
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    truncated = _todo_response("in_progress")
    truncated.choices[0].message.tool_calls[0].function.arguments = '{"todos": ['
    truncated.choices[0].finish_reason = "length"
    answers = iter([
        _todo_response("in_progress"), truncated, _response("Premature answer."),
        _todo_response("completed"), _response("The real task is complete."),
    ])
    wire_calls = []
    def provider(kwargs):
        wire_calls.append(kwargs)
        return next(answers)
    agent._interruptible_api_call = provider
    with patch("hermes_cli.plugins.has_hook", return_value=False), patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation("Complete the real research task.")
    assert len(wire_calls) == 5
    assert result["api_calls"] == 4  # the incomplete-tool retry is not a completed iteration
    assert result["completed"] is True
    assert result["final_response"] == "The real task is complete."
    assert agent._todo_store.read()[0]["status"] == "completed"
    assert agent._open_todo_stop_nudges == 1
    for messages in (result["messages"], agent._session_messages):
        assert [row["content"] for row in messages if row.get("role") == "user"] == ["Complete the real research task."]
        assert not any(row.get("_tool_retry_synthetic") for row in messages)

@pytest.mark.parametrize('kind,expected', [('read',False),('merge_completed',False),('merge_active',True),('replace_active',True),('string_todos',True),('failed',False),('unpaired',False)])
def test_current_turn_write_ids_match_active_store_items(kind, expected):
    from tools.todo_tool import todo_tool
    store = TodoStore()
    store.write([{'id':'prior','content':'Unrelated prior work','status':'pending'}])
    args = {} if kind == 'read' else {'todos':[{'id':'new','content':'Current work','status':'completed' if kind == 'merge_completed' else 'pending'}], 'merge':kind != 'replace_active'}
    if kind == 'string_todos':
        args['todos'] = json.dumps(args['todos'])
    if kind == 'failed':
        args['todos'] = 'malformed'
    result = todo_tool(store=store, **args)
    messages = [{'role':'user','content':'New task'}, {'role':'assistant','tool_calls':[{'id':'call','function':{'name':'todo','arguments':json.dumps(args)}}]}, {'role':'tool','tool_call_id':'other' if kind == 'unpaired' else 'call','content':result}]
    assert bool(build_open_todo_stop_nudge(agent=SimpleNamespace(_todo_store=store),messages=messages,attempts=0)) is expected


@pytest.mark.parametrize('args', [{}, {'todos':[{'id':'new','content':'Current work done','status':'completed'}], 'merge':True}])
def test_stale_store_does_not_prolong_current_conversation(agent, monkeypatch, args):
    agent._todo_store.write([{'id':'prior','content':'Unrelated prior work','status':'pending'}])
    call = SimpleNamespace(id='current-todo',type='function',function=SimpleNamespace(name='todo',arguments=json.dumps(args)))
    responses = iter([_response('',tool_calls=[call],finish_reason='tool_calls'),_response('Current request complete.')])
    agent._interruptible_api_call = lambda _kwargs: next(responses)
    monkeypatch.setenv('HERMES_VERIFY_ON_STOP','0')
    with patch('hermes_cli.plugins.has_hook',return_value=False), patch('hermes_cli.plugins.invoke_hook',return_value=[]):
        result = agent.run_conversation('Read or update the current work only.')
    assert result['completed'] is True and result['api_calls'] == 2
    assert result['final_response'] == 'Current request complete.'
    assert agent._open_todo_stop_nudges == 0
    assert next(item for item in agent._todo_store.read() if item['id']=='prior')['status']=='pending'

'''


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def _write_exact(path: Path, content: str, *, known_previous_sha256: tuple[str, ...] = ()) -> bool:
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == content:
            return False
        if hashlib.sha256(current.encode()).hexdigest() not in known_previous_sha256:
            raise RuntimeError(f"{path}: refusing to overwrite unexpected existing file")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True



def _turn_context_reset_source(target: Path) -> tuple[Path, str]:
    path = target / "agent/turn_context.py"
    source = path.read_text(encoding="utf-8")
    if "_PER_TURN_RESET_STATE" in source:
        before = '    ("_pre_verify_nudges", 0),\n'
        after = '    ("_pre_verify_nudges", 0), ("_open_todo_stop_nudges", 0),\n'
    else:
        before = '    agent._pre_verify_nudges = 0\n'
        after = before + '    agent._open_todo_stop_nudges = 0\n'
    if after not in source:
        source = _replace_once(source, before, after, label="per-turn todo nudge reset")
    compile(source, str(path), "exec")
    return path, source


def _patch_native_stop(hermes_dir: Path) -> bool:
    import textwrap
    target = Path(hermes_dir)
    gate = target / "agent/turn_stop_gates.py"
    finalizer = target / "agent/turn_finalizer.py"
    source, final = gate.read_text(), finalizer.read_text()
    context_path, context_source = _turn_context_reset_source(target)
    outputs = {context_path: context_source}
    if MARKER not in source:
        insert = textwrap.indent(textwrap.dedent(CONVERSATION_INSERT), "    ")
        insert = insert.replace("_pending_verification_response =", "pending_verification_response =")
        insert = insert.replace("_pending_verification_response_previewed =", "pending_verification_response_previewed =")
        insert = insert.replace('        continue\n', '        return StopGateVerdict(True, None, pending_verification_response, False)\n')
        anchor = '    def _continue(nudge: str, flag: str) -> StopGateVerdict:\n'
        source = _replace_once(source, anchor, insert + anchor, label="native stop owner")
    if MARKER not in final:
        final = _replace_once(final, '_VERIFICATION_CONTINUATION_FLAGS = ("_verification_stop_synthetic", "_pre_verify_synthetic")\n',
            '_VERIFICATION_CONTINUATION_FLAGS = (\n    "_verification_stop_synthetic", "_pre_verify_synthetic",\n    "_open_todo_stop_synthetic",  # ' + MARKER + '\n)\n', label="native synthetic flag")
    native_tests = TEST_SOURCE
    for old, new in (("run_agent.get_tool_definitions", "model_tools.get_tool_definitions"),
                     ("run_agent.check_toolset_requirements", "model_tools.check_toolset_requirements"),
                     ("run_agent.OpenAI", "agent.process_bootstrap.OpenAI")):
        native_tests = native_tests.replace(old, new)
    # The fixtures mock the nonstreaming provider method explicitly.
    native_tests = native_tests.replace('    instance.compression_enabled = False', '    instance._disable_streaming = True\n    instance.compression_enabled = False')
    outputs.update({gate: source, finalizer: final,
                    target / "agent/open_todo_stop.py": HELPER_SOURCE,
                    target / "tests/run_agent/test_open_todo_stop_guard.py": native_tests})
    for path, body in outputs.items():
        compile(body, str(path), "exec")
    changed = False
    for path, body in outputs.items():
        if not path.exists() or path.read_text() != body:
            path.write_text(body)
            changed = True
    return changed


def patch_open_todo_stop_guard_v1(hermes_dir: Path) -> bool:
    """Apply the open-todo stop guard to a pinned Hermes tree."""
    hermes_dir = Path(hermes_dir)
    if (hermes_dir / "agent/turn_stop_gates.py").exists():
        return _patch_native_stop(hermes_dir)
    conversation_path = hermes_dir / "agent" / "conversation_loop.py"
    finalizer_path = hermes_dir / "agent" / "turn_finalizer.py"
    helper_path = hermes_dir / "agent" / "open_todo_stop.py"
    test_path = hermes_dir / "tests" / "run_agent" / "test_open_todo_stop_guard.py"

    context_path, context_source = _turn_context_reset_source(hermes_dir)
    conversation = conversation_path.read_text(encoding="utf-8")
    finalizer = finalizer_path.read_text(encoding="utf-8")

    changed = False
    if MARKER not in conversation:
        conversation = _replace_once(
            conversation,
            CONVERSATION_ANCHOR,
            CONVERSATION_INSERT + CONVERSATION_ANCHOR,
            label="conversation loop",
        )
        conversation_path.write_text(conversation, encoding="utf-8")
        changed = True

    if MARKER not in finalizer:
        finalizer = _replace_once(
            finalizer,
            FINALIZER_OLD,
            FINALIZER_NEW,
            label="turn finalizer",
        )
        finalizer_path.write_text(finalizer, encoding="utf-8")
        changed = True

    changed = _write_exact(helper_path, HELPER_SOURCE, known_previous_sha256=(PREVIOUS_HELPER_SHA256,)) or changed
    changed = _write_exact(test_path, TEST_SOURCE, known_previous_sha256=(PREVIOUS_TEST_SHA256,)) or changed

    compile(conversation, str(conversation_path), "exec")
    compile(finalizer, str(finalizer_path), "exec")
    compile(HELPER_SOURCE, str(helper_path), "exec")
    compile(TEST_SOURCE, str(test_path), "exec")
    if context_path.read_text(encoding="utf-8") != context_source:
        context_path.write_text(context_source, encoding="utf-8")
        changed = True
    return changed
