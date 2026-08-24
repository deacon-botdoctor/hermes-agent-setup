"""Patch support for unresolved outcome claims and cron toolset validation."""

from __future__ import annotations

from pathlib import Path


MARKER = "HERMES_OUTCOME_STOP_GUARD_v1"
CRON_TOOLSET_MARKER = "HERMES_CRON_TOOLSET_VALIDATION_v1"
SUBAGENT_COMPLETION_MARKER = "HERMES_SUBAGENT_COMPLETION_TRUTH_v1"
CRON_MAX_ITERATION_MARKER = "HERMES_CRON_MAX_ITERATION_FAILURE_TRUTH_v1"


HELPER_SOURCE = '''"""Keep recoverable Blocked/Partial claims inside the active turn."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


MAX_OUTCOME_STOP_NUDGES = 2
SYNTHETIC_FLAG = "_outcome_stop_synthetic"
_INTERNAL_USER_FLAGS = {
    SYNTHETIC_FLAG,
    "_open_todo_stop_synthetic",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
}
_STATUS_HEADER = re.compile(
    r"^\\s*(?:#{1,6}\\s*)?(?:\\*\\*|__)?(?:status\\s*:\\s*)?"
    r"(blocked|partial)(?:\\*\\*|__)?\\s*:?[ \\t]*$",
    re.IGNORECASE,
)
_MACHINE_FAILURE_HEADER = re.compile(
    r"^\\s*[a-z][a-z0-9_]*(?:failed|error|exhausted|unavailable|refused)\\s*:",
    re.IGNORECASE,
)
_POLICY_FAILURE_PREFIXES = (
    "semantic computer-control policy blocked this call:",
)
_FAILURE_PREFIXES = (
    "error",
    "failed",
    "denied",
    "blocked",
    "cancelled",
    "timed out",
    "timeout",
)


def classify_terminal_claim(response: str) -> str:
    """Classify explicit status headers and machine-readable failure finals."""
    first = next((line for line in str(response or "").splitlines() if line.strip()), "")
    match = _STATUS_HEADER.fullmatch(first)
    if match:
        return match.group(1).lower()
    if _MACHINE_FAILURE_HEADER.match(first):
        return "blocked"
    if first.strip().casefold().startswith(_POLICY_FAILURE_PREFIXES):
        return "blocked"
    return ""


def cron_max_iteration_fallback_allowed(result: Dict[str, Any]) -> bool:
    """Allow a max-turn summary only when it is not a classified failure."""
    final_response = str(result.get("final_response") or "").strip()
    return (
        result.get("failed") is not True
        and result.get("completed") is False
        and str(result.get("turn_exit_reason") or "").startswith(
            "max_iterations_reached("
        )
        and bool(final_response)
        and not classify_terminal_claim(final_response)
    )


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
        if any(message.get(flag) for flag in _INTERNAL_USER_FLAGS):
            continue
        return index
    return None


def current_turn_key(messages: List[Dict[str, Any]]) -> str:
    index = _current_real_user_index(messages)
    return str(index) if index is not None else "none"


def _tool_result_succeeded(content: Any) -> bool:
    text = str(content or "").strip().lower()
    return bool(text) and not text.startswith(_FAILURE_PREFIXES)


def _paired_successful_tools(messages: List[Dict[str, Any]]) -> set[str]:
    user_index = _current_real_user_index(messages)
    if user_index is None:
        return set()

    calls: dict[str, str] = {}
    results: dict[str, Any] = {}
    for message in messages[user_index + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                name, call_id = _tool_call_parts(tool_call)
                if name and call_id:
                    calls[call_id] = name
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if call_id:
                results[call_id] = message.get("content")

    return {
        name
        for call_id, name in calls.items()
        if call_id in results and _tool_result_succeeded(results[call_id])
    }


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


def unknown_requested_toolsets(
    requested: Any,
    registered: Any,
    aliases: Any,
) -> list[str]:
    """Return exact requested cron toolsets that the live registry cannot resolve."""
    known = {str(name) for name in (registered or [])}
    known.update(str(name) for name in (aliases or {}))
    return sorted(
        str(name)
        for name in (requested or [])
        if str(name) not in known and str(name) != "no_mcp"
    )


def classify_subagent_result(*, completed: bool, summary: str, interrupted: bool) -> str:
    """Do not promote an incomplete child merely because it returned prose."""
    if interrupted:
        return "interrupted"
    clean_summary = str(summary or "").strip()
    if completed and clean_summary and clean_summary != "(empty)":
        return "completed"
    return "failed"


def build_outcome_stop_nudge(
    *,
    agent: Any,
    messages: List[Dict[str, Any]],
    response: str,
    platform: Any,
) -> Optional[str]:
    """Return a bounded continuation nudge for unsupported terminal claims.

    ``Blocked`` is terminal only after a successful evidence-backed
    ``task_block`` call. ``Partial`` may leave the foreground only when a
    successful ``task_update`` is paired with an active asynchronous execution
    lane. Everything else remains internal recovery work for up to two nudges.
    """
    turn_key = current_turn_key(messages)
    if getattr(agent, "_outcome_stop_turn_key", None) != turn_key:
        agent._outcome_stop_turn_key = turn_key
        agent._outcome_stop_nudges = 0
        agent._outcome_stop_exhausted_claim = ""

    claim = classify_terminal_claim(response)
    if not claim or _platform_name(platform) not in {"telegram", "cron", "subagent"}:
        agent._outcome_stop_exhausted_claim = ""
        return None

    successful_tools = _paired_successful_tools(messages)
    if claim == "blocked" and "task_block" in successful_tools:
        agent._outcome_stop_exhausted_claim = ""
        return None
    if (
        claim == "partial"
        and "task_update" in successful_tools
        and successful_tools.intersection({"delegate_task", "cronjob", "process"})
    ):
        agent._outcome_stop_exhausted_claim = ""
        return None

    attempts = int(getattr(agent, "_outcome_stop_nudges", 0) or 0)
    if attempts >= MAX_OUTCOME_STOP_NUDGES:
        agent._outcome_stop_exhausted_claim = claim
        return None

    agent._outcome_stop_nudges = attempts + 1
    agent._outcome_stop_exhausted_claim = ""
    return (
        "[System: Your proposed final response declares the parent outcome "
        f"{claim}, but this turn has no matching lifecycle evidence that may "
        "end it. Keep the response internal and continue the authorized work "
        "now. Reuse already granted authority, repair or switch recoverable "
        "local routes, and do not repeat completed side effects. If a real hard "
        "stop remains after exhausting safe routes, record it with task_block "
        "including attempted routes and the exact resume condition before "
        "sending the final response.]"
    )
'''


CONVERSATION_ANCHOR = '''                # HERMES_OPEN_TODO_STOP_GUARD_v1: a todo plan created in this user turn is
'''


CONVERSATION_INSERT = f'''                # {MARKER}: a naked Blocked/Partial status is not a terminal
                # outcome. Keep it inside the active turn until lifecycle
                # evidence exists or the bounded recovery budget is exhausted.
                try:
                    from agent.outcome_stop import build_outcome_stop_nudge

                    _outcome_stop_nudge = build_outcome_stop_nudge(
                        agent=agent,
                        messages=messages,
                        response=final_response,
                        platform=getattr(agent, "platform", None) or "",
                    )
                except Exception:
                    logger.debug("outcome stop-loop check failed", exc_info=True)
                    _outcome_stop_nudge = None

                if _outcome_stop_nudge:
                    final_msg["finish_reason"] = "outcome_evidence_required"
                    final_msg["_outcome_stop_synthetic"] = True
                    messages.append(final_msg)
                    messages.append({{
                        "role": "user",
                        "content": _outcome_stop_nudge,
                        "_outcome_stop_synthetic": True,
                    }})
                    agent._session_messages = messages
                    logger.warning(
                        "unsupported terminal outcome kept internal (attempt %d)",
                        getattr(agent, "_outcome_stop_nudges", 0),
                    )
                    agent._emit_status(
                        "↻ Outcome is still open — continuing before final reply"
                    )
                    _pending_verification_response = final_response
                    _pending_verification_response_previewed = False
                    final_response = None
                    continue

'''


FINALIZER_OLD = '''_VERIFICATION_CONTINUATION_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_open_todo_stop_synthetic",  # HERMES_OPEN_TODO_STOP_GUARD_v1
)
'''


FINALIZER_NEW = f'''_VERIFICATION_CONTINUATION_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_open_todo_stop_synthetic",  # HERMES_OPEN_TODO_STOP_GUARD_v1
    "_outcome_stop_synthetic",  # {MARKER}
)
'''


COMPLETED_OLD = '''    normal_text_response = str(_turn_exit_reason).startswith("text_response(")
    completed = (
        final_response is not None
        and not failed
        and (
            api_call_count < agent.max_iterations
            or normal_text_response
        )
    )
'''


COMPLETED_NEW = f'''    normal_text_response = str(_turn_exit_reason).startswith("text_response(")
    completed = (
        final_response is not None
        and not failed
        and (
            api_call_count < agent.max_iterations
            or normal_text_response
        )
        # {MARKER}: an unsupported terminal claim remains incomplete even
        # after the bounded continuation budget is exhausted.
        and not getattr(agent, "_outcome_stop_exhausted_claim", "")
    )
'''


CRON_AGENT_ANCHOR = '''        agent = AIAgent(
'''


CRON_AGENT_INSERT = '''        _cron_enabled_toolsets = _resolve_cron_enabled_toolsets(job, _cfg)
'''


CRON_TOOLSET_OLD = '''            enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),
'''


CRON_TOOLSET_NEW = '''            enabled_toolsets=_cron_enabled_toolsets,
'''


CRON_POST_AGENT_ANCHOR = '''        # Run the agent with an *inactivity*-based timeout: the job can run
'''


CRON_POST_AGENT_INSERT = f'''        # {CRON_TOOLSET_MARKER}: reject misspelled or stale per-job toolsets
        # before the model can improvise through a weaker route. Discovery has
        # already loaded built-ins, plugins, and configured MCP tools.
        if _cron_enabled_toolsets:
            from agent.outcome_stop import unknown_requested_toolsets
            from tools.registry import registry as _tool_registry

            _unknown_toolsets = unknown_requested_toolsets(
                _cron_enabled_toolsets,
                _tool_registry.get_registered_toolset_names(),
                _tool_registry.get_registered_toolset_aliases(),
            )
            if _unknown_toolsets:
                raise RuntimeError(
                    "Cron job requests unknown toolset(s): "
                    + ", ".join(_unknown_toolsets)
                )

'''


CRON_MAX_ITERATION_OLD = '''        max_iteration_summary = (
            result.get("failed") is not True
            and result.get("completed") is False
            and turn_exit_reason.startswith("max_iterations_reached(")
            and bool(final_response_text)
        )
'''


CRON_MAX_ITERATION_NEW = f'''        # {CRON_MAX_ITERATION_MARKER}: a max-turn fallback may be delivered only
        # when it is a real summary, never when it is a classified failure.
        from agent.outcome_stop import cron_max_iteration_fallback_allowed

        max_iteration_summary = cron_max_iteration_fallback_allowed(result)
'''


DELEGATE_OLD = '''        if interrupted:
            status = "interrupted"
        elif summary and not _empty_sentinel:
            # A summary means the subagent produced usable output.
            # exit_reason ("completed" vs "max_iterations") already
            # tells the parent *how* the task ended.
            status = "completed"
        else:
            status = "failed"
'''


DELEGATE_NEW = f'''        # {SUBAGENT_COMPLETION_MARKER}: prose is not completion evidence.
        # Preserve an incomplete child's summary for diagnosis, but report the
        # child failed unless its own turn lifecycle says it completed.
        from agent.outcome_stop import classify_subagent_result

        status = classify_subagent_result(
            completed=completed,
            summary=summary,
            interrupted=interrupted,
        )
'''


TEST_SOURCE = '''"""Regression coverage for terminal outcome continuation."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.outcome_stop import (
    build_outcome_stop_nudge,
    classify_subagent_result,
    classify_terminal_claim,
    cron_max_iteration_fallback_allowed,
    unknown_requested_toolsets,
)
from run_agent import AIAgent


def _response(content="final answer", *, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model",
        usage=None,
    )


def _paired_tool(name, call_id, content="ok"):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="outcome-stop-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=5,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="telegram",
        )
    instance.valid_tool_names = []
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


@pytest.mark.parametrize("header", ["Blocked", "# Partial", "**Blocked**", "Status: Partial"])
def test_classifies_only_explicit_status_headers(header):
    assert classify_terminal_claim(header + "\\nmore") in {"blocked", "partial"}


@pytest.mark.parametrize(
    "text",
    [
        "Blocked records: 4",
        "The task is partial",
        "Not blocked",
        "Records unavailable: 4",
    ],
)
def test_does_not_classify_incidental_words(text):
    assert classify_terminal_claim(text) == ""


def test_machine_failure_final_is_an_open_blocker():
    assert (
        classify_terminal_claim(
            "browser_reconnect_exhausted: the approved socket could not be claimed"
        )
        == "blocked"
    )


def test_semantic_policy_rejection_is_an_open_blocker():
    assert (
        classify_terminal_claim(
            "Semantic computer-control policy blocked this call: "
            "dedicated-principal binding mismatch: agent_id."
        )
        == "blocked"
    )


def test_cron_max_iteration_failure_is_not_a_success_summary():
    base = {
        "failed": False,
        "completed": False,
        "turn_exit_reason": "max_iterations_reached(90)",
    }
    assert cron_max_iteration_fallback_allowed({
        **base,
        "final_response": "Useful checkpoint summary with remaining work.",
    }) is True
    assert cron_max_iteration_fallback_allowed({
        **base,
        "final_response": "browser_reconnect_exhausted: socket unavailable",
    }) is False


def test_unknown_cron_toolset_is_rejected_before_agent_execution():
    assert unknown_requested_toolsets(
        ["computer", "file", "terminal"],
        ["computer_use", "file", "terminal"],
        {},
    ) == ["computer"]
    assert unknown_requested_toolsets(
        ["computer_use", "file", "terminal"],
        ["computer_use", "file", "terminal"],
        {},
    ) == []


def test_incomplete_subagent_summary_is_not_promoted_to_completed():
    assert classify_subagent_result(
        completed=False,
        summary="Partial work with three deliverables missing",
        interrupted=False,
    ) == "failed"
    assert classify_subagent_result(
        completed=True,
        summary="All requested work verified",
        interrupted=False,
    ) == "completed"


def test_evidenced_blocker_may_close():
    helper = SimpleNamespace()
    messages = [
        {"role": "user", "content": "do it"},
        *_paired_tool("task_block", "b1", "Task t1 blocked. Resume when: login restored"),
    ]
    assert build_outcome_stop_nudge(
        agent=helper,
        messages=messages,
        response="Blocked\\nService requires human verification.",
        platform="telegram",
    ) is None


def test_partial_requires_durable_update_and_active_async_lane():
    helper = SimpleNamespace()
    messages = [
        {"role": "user", "content": "do it"},
        *_paired_tool("task_update", "u1", "Task t1 updated"),
        *_paired_tool("delegate_task", "d1", "Worker started"),
    ]
    assert build_outcome_stop_nudge(
        agent=helper,
        messages=messages,
        response="Partial\\nWorker remains active.",
        platform="telegram",
    ) is None


def test_two_unsupported_claims_exhaust_as_incomplete():
    helper = SimpleNamespace()
    messages = [{"role": "user", "content": "do it"}]
    for expected_attempt in (1, 2):
        assert build_outcome_stop_nudge(
            agent=helper,
            messages=messages,
            response="Blocked\\nI stopped after one route.",
            platform="telegram",
        ) is not None
        assert helper._outcome_stop_nudges == expected_attempt
    assert build_outcome_stop_nudge(
        agent=helper,
        messages=messages,
        response="Blocked\\nI still refuse.",
        platform="telegram",
    ) is None
    assert helper._outcome_stop_exhausted_claim == "blocked"


def test_blocked_reply_continues_and_can_finish(agent, monkeypatch):
    answers = iter([
        _response("Blocked\\nThe first browser route failed."),
        _response("Completed with exact evidence."),
    ])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Finish the browser task.")
    assert result["api_calls"] == 2
    assert result["final_response"] == "Completed with exact evidence."
    assert result["completed"] is True
    assert not any(
        message.get("_outcome_stop_synthetic")
        for message in result["messages"]
        if isinstance(message, dict)
    )


def test_repeated_unsupported_blocker_cannot_report_completed(agent, monkeypatch):
    answers = iter([_response("Blocked\\nNo.") for _ in range(3)])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Finish the browser task.")
    assert result["api_calls"] == 3
    assert result["final_response"].startswith("Blocked")
    assert result["completed"] is False
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


def patch_outcome_stop_guard_v1(hermes_dir: Path) -> bool:
    """Apply outcome continuation and cron toolset validation."""
    hermes_dir = Path(hermes_dir)
    conversation_path = hermes_dir / "agent" / "conversation_loop.py"
    finalizer_path = hermes_dir / "agent" / "turn_finalizer.py"
    cron_path = hermes_dir / "cron" / "scheduler.py"
    delegate_path = hermes_dir / "tools" / "delegate_tool.py"
    helper_path = hermes_dir / "agent" / "outcome_stop.py"
    test_path = hermes_dir / "tests" / "run_agent" / "test_outcome_stop_guard.py"

    conversation = conversation_path.read_text(encoding="utf-8")
    finalizer = finalizer_path.read_text(encoding="utf-8")
    cron = cron_path.read_text(encoding="utf-8")
    delegate = delegate_path.read_text(encoding="utf-8")
    changed = False

    if MARKER not in conversation:
        conversation = _replace_once(
            conversation,
            CONVERSATION_ANCHOR,
            CONVERSATION_INSERT + CONVERSATION_ANCHOR,
            label="conversation outcome stop",
        )
        conversation_path.write_text(conversation, encoding="utf-8")
        changed = True

    if MARKER not in finalizer:
        finalizer = _replace_once(
            finalizer, FINALIZER_OLD, FINALIZER_NEW, label="finalizer synthetic flags"
        )
        finalizer = _replace_once(
            finalizer, COMPLETED_OLD, COMPLETED_NEW, label="finalizer completion"
        )
        finalizer_path.write_text(finalizer, encoding="utf-8")
        changed = True

    if CRON_TOOLSET_MARKER not in cron:
        cron = _replace_once(
            cron,
            CRON_AGENT_ANCHOR,
            CRON_AGENT_INSERT + CRON_AGENT_ANCHOR,
            label="cron toolset resolve",
        )
        cron = _replace_once(
            cron, CRON_TOOLSET_OLD, CRON_TOOLSET_NEW, label="cron toolset reuse"
        )
        cron = _replace_once(
            cron,
            CRON_POST_AGENT_ANCHOR,
            CRON_POST_AGENT_INSERT + CRON_POST_AGENT_ANCHOR,
            label="cron toolset validation",
        )
        cron_path.write_text(cron, encoding="utf-8")
        changed = True

    if CRON_MAX_ITERATION_MARKER not in cron:
        cron = _replace_once(
            cron,
            CRON_MAX_ITERATION_OLD,
            CRON_MAX_ITERATION_NEW,
            label="cron max-iteration failure truth",
        )
        cron_path.write_text(cron, encoding="utf-8")
        changed = True

    if SUBAGENT_COMPLETION_MARKER not in delegate:
        delegate = _replace_once(
            delegate,
            DELEGATE_OLD,
            DELEGATE_NEW,
            label="subagent completion truth",
        )
        delegate_path.write_text(delegate, encoding="utf-8")
        changed = True

    changed = _write_exact(helper_path, HELPER_SOURCE) or changed
    changed = _write_exact(test_path, TEST_SOURCE) or changed

    compile(conversation, str(conversation_path), "exec")
    compile(finalizer, str(finalizer_path), "exec")
    compile(cron, str(cron_path), "exec")
    compile(delegate, str(delegate_path), "exec")
    compile(HELPER_SOURCE, str(helper_path), "exec")
    compile(TEST_SOURCE, str(test_path), "exec")
    return changed
