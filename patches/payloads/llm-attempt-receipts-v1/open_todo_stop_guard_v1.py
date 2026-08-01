"""Patch support for the LLM-turn unit's current-turn todo stop guard."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_OPEN_TODO_STOP_GUARD_v1"

HELPER_SOURCE = '''"""Language-neutral stop guard for current-turn todo plans."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


MAX_OPEN_TODO_STOP_NUDGES = 2
SYNTHETIC_FLAG = "_open_todo_stop_synthetic"


def _tool_call_parts(tool_call: Any) -> tuple[str, str]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        name = function.get("name", "") if isinstance(function, dict) else ""
        call_id = tool_call.get("id", "")
    else:
        function = getattr(tool_call, "function", None)
        name = getattr(function, "name", "") if function is not None else ""
        call_id = getattr(tool_call, "id", "")
    return str(name or "").strip(), str(call_id or "").strip()


def _current_real_user_index(messages: List[Dict[str, Any]]) -> Optional[int]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if message.get(SYNTHETIC_FLAG):
            continue
        return index
    return None


def _current_turn_has_paired_todo(messages: List[Dict[str, Any]]) -> bool:
    user_index = _current_real_user_index(messages)
    if user_index is None:
        return False

    todo_call_ids: set[str] = set()
    for message in messages[user_index + 1 :]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            name, call_id = _tool_call_parts(tool_call)
            if name == "todo" and call_id:
                todo_call_ids.add(call_id)

    if not todo_call_ids:
        return False

    return any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and str(message.get("tool_call_id") or "").strip() in todo_call_ids
        for message in messages[user_index + 1 :]
    )


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
    if not _current_turn_has_paired_todo(messages):
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
        and str(item.get("status") or "").strip().lower()
        in {"pending", "in_progress"}
    ]
    if not active:
        return None

    return (
        "[System: You created a todo plan in this user turn and are trying "
        "to stop while items remain pending or in progress. Continue the "
        "work now. Do not send another acknowledgement or promise. Use the "
        "required tools, then mark every todo completed or cancelled before "
        "sending the final answer.]"
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
                    "function": {"name": "todo", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id if paired else "different-call",
            "content": "{}",
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
    assert "Do not send another acknowledgement" in nudge


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
'''


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def _write_exact(path: Path, content: str) -> bool:
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == content:
            return False
        raise RuntimeError(f"{path}: refusing to overwrite unexpected existing file")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def patch_open_todo_stop_guard_v1(hermes_dir: Path) -> bool:
    """Apply the open-todo stop guard to a pinned Hermes tree."""
    hermes_dir = Path(hermes_dir)
    conversation_path = hermes_dir / "agent" / "conversation_loop.py"
    finalizer_path = hermes_dir / "agent" / "turn_finalizer.py"
    helper_path = hermes_dir / "agent" / "open_todo_stop.py"
    test_path = hermes_dir / "tests" / "run_agent" / "test_open_todo_stop_guard.py"

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

    changed = _write_exact(helper_path, HELPER_SOURCE) or changed
    changed = _write_exact(test_path, TEST_SOURCE) or changed

    compile(conversation, str(conversation_path), "exec")
    compile(finalizer, str(finalizer_path), "exec")
    compile(HELPER_SOURCE, str(helper_path), "exec")
    compile(TEST_SOURCE, str(test_path), "exec")
    return changed
