"""Patch support for unresolved outcome claims and cron toolset validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

MARKER = "HERMES_OUTCOME_STOP_GUARD_v1"
CRON_TOOLSET_MARKER = "HERMES_CRON_TOOLSET_VALIDATION_v1"
CRON_CONFIGURED_MCP_MARKER = "HERMES_CRON_CONFIGURED_MCP_TOOLSET_VALIDATION_v2"
SUBAGENT_COMPLETION_MARKER = "HERMES_SUBAGENT_COMPLETION_TRUTH_v1"
CRON_MAX_ITERATION_MARKER = "HERMES_CRON_MAX_ITERATION_FAILURE_TRUTH_v1"
CANONICAL_HELPER_SHA256 = "90f77630aacf064f41ca6e087e5231a26464e4da9aa1a4046f072b70543116d7"
PREVIOUS_HELPER_SHA256 = "6bdb7d5f2c949e026d7fcb15babad8954d15d10458c121eae3d91ff619514e3e"
CANONICAL_TEST_SHA256 = "95b6c09dc1898f45b47684818e0f6ba55b0fd803a79943a732009d584a4f5b0c"
PREVIOUS_TEST_SHA256 = "ce83960cbfe4f4d1596ae9ce90101a751b928a86d92ec8bcf2ef1ea59d4229d9"


HELPER_SOURCE = '''"""Keep recoverable Blocked/Partial claims inside the active turn."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


MAX_OUTCOME_STOP_NUDGES = 2
SYNTHETIC_FLAG = "_outcome_stop_synthetic"
_INTERNAL_USER_FLAGS = {
    SYNTHETIC_FLAG,
    "_open_todo_stop_synthetic",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_tool_retry_synthetic",
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


_STRUCTURED_FAILURE_STATUSES = {
    "error",
    "failed",
    "denied",
    "cancelled",
    "timeout",
    "timed_out",
    "unsupported",
}
_STRUCTURED_RESULT_WRAPPERS = ("result", "content", "data", "output")
_MAX_STRUCTURED_RESULT_WRAPPER_DEPTH = 2


def _decode_structured_tool_result(content: Any) -> Any:
    """Decode a JSON object result without treating ordinary text as a schema."""
    if not isinstance(content, str):
        return content
    text = content.strip()
    if not text.startswith("{"):
        return content
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return content
    return decoded if isinstance(decoded, dict) else content


def _structured_tool_result_failed(content: Any, *, _depth: int = 0) -> bool:
    """True for an explicit failure envelope, following only bounded wrappers."""
    result = _decode_structured_tool_result(content)
    if not isinstance(result, dict):
        return False
    if (
        result.get("isError") is True
        or result.get("is_error") is True
        or result.get("success") is False
        or result.get("ok") is False
        or result.get("failed") is True
        or result.get("error") not in (None, "", False)
        or str(result.get("status") or "").strip().lower()
        in _STRUCTURED_FAILURE_STATUSES
    ):
        return True
    if _depth >= _MAX_STRUCTURED_RESULT_WRAPPER_DEPTH:
        return False
    return any(
        _structured_tool_result_failed(result[key], _depth=_depth + 1)
        for key in _STRUCTURED_RESULT_WRAPPERS
        if key in result
    )


def _tool_result_succeeded(content: Any) -> bool:
    if _structured_tool_result_failed(content):
        return False
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
    configured_mcp: Any = None,
) -> list[str]:
    """Return requested cron toolsets absent from both live and cold registries."""
    known = {str(name) for name in (registered or [])}
    known.update(str(name) for name in (aliases or {}))
    # Configured on-demand MCP servers intentionally remain cold until a tool
    # lookup activates them. They are valid toolset names even though eager MCP
    # discovery has not placed them in the live registry yet.
    configured = {str(name) for name in (configured_mcp or []) if str(name)}
    known.update(configured)
    # Hermes' MCP registration seam uses ``mcp-<server>`` toolset aliases,
    # while platform/cron configuration may carry the bare server name. A
    # configured cold server is valid in either canonical representation.
    known.update(
        name if name.startswith("mcp-") else f"mcp-{name}"
        for name in configured
    )
    known.update(name[4:] for name in configured if name.startswith("mcp-"))
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
        f"[System: This {claim} final lacks matching lifecycle evidence. Do not "
        "finalize it; continue the authorized work. A hard stop "
        "requires task_block with attempted routes and a resume condition.]"
    )
'''


CONVERSATION_ANCHOR = """                # HERMES_OPEN_TODO_STOP_GUARD_v1: a todo plan created in this user turn is
"""


CONVERSATION_INSERT = f"""                # {MARKER}: a naked Blocked/Partial status is not a terminal
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

"""


FINALIZER_OLD = """_VERIFICATION_CONTINUATION_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_open_todo_stop_synthetic",  # HERMES_OPEN_TODO_STOP_GUARD_v1
)
"""


FINALIZER_NEW = f"""_VERIFICATION_CONTINUATION_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_open_todo_stop_synthetic",  # HERMES_OPEN_TODO_STOP_GUARD_v1
    "_outcome_stop_synthetic",  # {MARKER}
)
"""


COMPLETED_OLD = """    normal_text_response = str(_turn_exit_reason).startswith("text_response(")
    completed = (
        final_response is not None
        and not failed
        and (
            api_call_count < agent.max_iterations
            or normal_text_response
        )
    )
"""


COMPLETED_NEW = f"""    normal_text_response = str(_turn_exit_reason).startswith("text_response(")
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
"""


COMPLETED_021_OLD = """    normal_text_response = str(_turn_exit_reason).startswith("text_response(")
    successful_text_response = normal_text_response or successful_guardrail_recovery
    completed = (
        final_response is not None
        and not failed
        and (
            api_call_count < agent.max_iterations
            or successful_text_response
        )
    )
"""


COMPLETED_021_NEW = f"""    normal_text_response = str(_turn_exit_reason).startswith("text_response(")
    successful_text_response = normal_text_response or successful_guardrail_recovery
    completed = (
        final_response is not None
        and not failed
        and (
            api_call_count < agent.max_iterations
            or successful_text_response
        )
        # {MARKER}: an unsupported terminal claim remains incomplete even
        # after the bounded continuation budget is exhausted.
        and not getattr(agent, "_outcome_stop_exhausted_claim", "")
    )
"""


CRON_AGENT_ANCHOR = """        agent = AIAgent(
"""


CRON_AGENT_INSERT = """        _cron_enabled_toolsets = _resolve_cron_enabled_toolsets(job, _cfg)
"""


CRON_TOOLSET_OLD = """            enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),
"""


CRON_TOOLSET_NEW = """            enabled_toolsets=_cron_enabled_toolsets,
"""


CRON_POST_AGENT_ANCHOR = """        # Run the agent with an *inactivity*-based timeout: the job can run
"""


CRON_POST_AGENT_INSERT = f"""        # {CRON_TOOLSET_MARKER}: reject misspelled or stale per-job toolsets
        # before the model can improvise through a weaker route. Discovery has
        # already loaded built-ins, plugins, and configured MCP tools.
        if _cron_enabled_toolsets:
            from agent.outcome_stop import unknown_requested_toolsets
            from hermes_cli.tools_config import enabled_mcp_server_names
            from tools.registry import registry as _tool_registry

            # {CRON_CONFIGURED_MCP_MARKER}: configured on-demand MCPs are valid
            # before cold activation registers their live toolset definitions.
            _unknown_toolsets = unknown_requested_toolsets(
                _cron_enabled_toolsets,
                _tool_registry.get_registered_toolset_names(),
                _tool_registry.get_registered_toolset_aliases(),
                enabled_mcp_server_names(_cfg),
            )
            if _unknown_toolsets:
                raise RuntimeError(
                    "Cron job requests unknown toolset(s): "
                    + ", ".join(_unknown_toolsets)
                )

"""


CRON_CONFIGURED_MCP_OLD = """            _unknown_toolsets = unknown_requested_toolsets(
                _cron_enabled_toolsets,
                _tool_registry.get_registered_toolset_names(),
                _tool_registry.get_registered_toolset_aliases(),
            )
"""


CRON_CONFIGURED_MCP_NEW = f"""            from hermes_cli.tools_config import enabled_mcp_server_names

            # {CRON_CONFIGURED_MCP_MARKER}: configured on-demand MCPs are valid
            # before cold activation registers their live toolset definitions.
            _unknown_toolsets = unknown_requested_toolsets(
                _cron_enabled_toolsets,
                _tool_registry.get_registered_toolset_names(),
                _tool_registry.get_registered_toolset_aliases(),
                enabled_mcp_server_names(_cfg),
            )
"""


CRON_MAX_ITERATION_OLD = """        max_iteration_summary = (
            result.get("failed") is not True
            and result.get("completed") is False
            and turn_exit_reason.startswith("max_iterations_reached(")
            and bool(final_response_text)
        )
"""


CRON_MAX_ITERATION_NEW = f"""        # {CRON_MAX_ITERATION_MARKER}: a max-turn fallback may be delivered only
        # when it is a real summary, never when it is a classified failure.
        from agent.outcome_stop import cron_max_iteration_fallback_allowed

        max_iteration_summary = cron_max_iteration_fallback_allowed(result)
"""


DELEGATE_OLD = """        if interrupted:
            status = "interrupted"
        elif summary and not _empty_sentinel:
            # A summary means the subagent produced usable output.
            # exit_reason ("completed" vs "max_iterations") already
            # tells the parent *how* the task ended.
            status = "completed"
        else:
            status = "failed"
"""


DELEGATE_NEW = f"""        # {SUBAGENT_COMPLETION_MARKER}: prose is not completion evidence.
        # Preserve an incomplete child's summary for diagnosis, but report the
        # child failed unless its own turn lifecycle says it completed.
        from agent.outcome_stop import classify_subagent_result

        status = classify_subagent_result(
            completed=completed,
            summary=summary,
            interrupted=interrupted,
)
"""


DELEGATE_021_OLD = """        if interrupted:
            status = "interrupted"
        elif result.get("failed") or result.get("error"):
            # A structured failure (provider rejection / terminal exception)
            # must WIN over the summary-presence heuristic below. The child's
            # conversation loop returns the error text as final_response, so an
            # error-shaped summary would otherwise be labeled "completed" here
            # despite completed=False. The heuristic is only a fallback for
            # legacy/mock results that omit the structured failure fields.
            # (Community report Aug 2026; #97655.)
            status = "failed"
        elif _schema_valid is False:
            # T1-24 follow-up: a schema was declared and the final answer —
            # after the one bounded retry — still violates it (empty `{}`
            # fallback included). A summary exists, but it is unusable under
            # the contract the caller asked for, so it must not be reported
            # as a completed delegation: the batch line would print ✓ and
            # orchestrators that read only status/icon would accept an
            # empty verdict. schema_valid/schema_errors (below) carry the
            # detail; status has to agree with them. _schema_valid stays
            # None on schema-less runs, which never take this branch.
            status = "failed"
        elif summary and not _empty_sentinel:
            # A summary means the subagent produced usable output.
            # exit_reason ("completed" vs "max_iterations") already
            # tells the parent *how* the task ended.
            status = "completed"
        else:
            status = "failed"
"""


DELEGATE_021_NEW = f"""        # {SUBAGENT_COMPLETION_MARKER}: prose is not completion evidence.
        # Preserve an incomplete child's summary for diagnosis, but report the
        # child failed unless its own turn lifecycle says it completed. The
        # schema/failure fields remain authoritative inputs to `completed`.
        from agent.outcome_stop import classify_subagent_result

        status = classify_subagent_result(
            completed=(
                bool(completed)
                and not bool(result.get("failed") or result.get("error"))
                and _schema_valid is not False
            ),
            summary=summary,
            interrupted=interrupted,
        )
"""


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


def test_configured_cold_mcp_is_valid_before_live_registration():
    assert unknown_requested_toolsets(
        ["web", "visual-identity"],
        ["web"],
        {},
        ["visual-identity"],
    ) == []
    assert unknown_requested_toolsets(
        ["web", "mcp-visual-identity"],
        ["web"],
        {},
        ["visual-identity"],
    ) == []
    assert unknown_requested_toolsets(
        ["web", "visual-identity"],
        ["web"],
        {},
        ["mcp-visual-identity"],
    ) == []
    assert unknown_requested_toolsets(
        ["web", "visual-identit-typo"],
        ["web"],
        {},
        ["visual-identity"],
    ) == ["visual-identit-typo"]


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


def test_explicit_structured_tool_failures_do_not_close_terminal_claims():
    for content in (
        {"success": False, "error": "permission denied"},
        json.dumps({"success": False, "error": "permission denied"}),
        {"result": json.dumps({"status": "failed", "error": "permission denied"})},
        {"result": {"isError": True, "content": [{"type": "text", "text": json.dumps({"success": False})}]}},
    ):
        helper = SimpleNamespace()
        messages = [
            {"role": "user", "content": "do it"},
            *_paired_tool("task_block", "b1", content),
        ]
        assert build_outcome_stop_nudge(
            agent=helper,
            messages=messages,
            response="Blocked\\nService requires human verification.",
            platform="telegram",
        ) is not None


def test_statusless_result_and_explicit_success_remain_valid_evidence():
    helper = SimpleNamespace()
    statusless = [
        {"role": "user", "content": "do it"},
        *_paired_tool("task_block", "b1", {"message": "recorded"}),
    ]
    assert build_outcome_stop_nudge(
        agent=helper,
        messages=statusless,
        response="Blocked\\nService requires human verification.",
        platform="telegram",
    ) is None

    helper = SimpleNamespace()
    partial = [
        {"role": "user", "content": "do it"},
        *_paired_tool("task_update", "u1", {"success": True}),
        *_paired_tool("delegate_task", "d1", {"result": {"success": True}}),
    ]
    assert build_outcome_stop_nudge(
        agent=helper,
        messages=partial,
        response="Partial\\nWorker remains active.",
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


def test_cached_agent_gets_outcome_budget_after_history_pruning(agent, monkeypatch):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with patch("hermes_cli.plugins.has_hook", return_value=False), patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        for user in ("First real task", "Second distinct task after pruning"):
            answers = iter([_response("Blocked\\nNo lifecycle evidence.") for _ in range(3)])
            agent._interruptible_api_call = lambda _kwargs: next(answers)
            result = agent.run_conversation(user, conversation_history=[])
            assert result["api_calls"] == 3
            assert result["completed"] is False
            assert agent._outcome_stop_turn_key == "0"
            assert agent._outcome_stop_nudges == 2
            assert agent._outcome_stop_exhausted_claim == "blocked"
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
        current_sha256 = hashlib.sha256(current.encode()).hexdigest()
        if current_sha256 not in known_previous_sha256:
            raise RuntimeError(f"{path}: refusing to overwrite unexpected existing file")
        path.write_text(content, encoding="utf-8")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True



def _retry_finalizer_flags(source: str) -> str:
    before = '    "_outcome_stop_synthetic",  # ' + MARKER + '\n'
    after = before + '    "_tool_retry_synthetic",\n'
    return source if after in source else _replace_once(source, before, after, label="retry finalizer flag")


def _guardrail_completion_sources(target: Path, outputs: dict[Path, str]) -> dict[Path, str]:
    """Preserve one-shot partial text without advertising a completed turn."""
    native = (target / "agent/turn_final_response.py").exists()
    owner = target / ("agent/turn_final_response.py" if native else "agent/conversation_loop.py")
    source = outputs.get(owner, owner.read_text())
    if '"guardrail_recovery_answer"' not in source:
        return outputs  # Standalone stop-helper fixtures have no guardrail carrier.
    indent, reason = ("            ", "finish_reason") if native else ("        ", "response_finish_reason")
    anchor = indent + '_turn_exit_reason = "guardrail_recovery_answer"\n'
    replacement = anchor + indent + f'if {reason} in {{"length", "incomplete"}}:\n' + indent + '    _turn_exit_reason = "guardrail_recovery_incomplete"\n'
    if replacement not in source:
        source = _replace_once(source, anchor, replacement, label="guardrail partial disposition")
    outputs[owner] = source
    finalizer = target / "agent/turn_finalizer.py"
    final = outputs.get(finalizer, finalizer.read_text())
    anchor = '    completed = (\n        final_response is not None\n        and not failed\n'
    old_predicate = '        and str(_turn_exit_reason) != "guardrail_recovery_incomplete"\n'
    predicate = '        and str(_turn_exit_reason) not in {"guardrail_recovery_incomplete", "guardrail_recovery_error", "guardrail_recovery_refused", "guardrail_halt"}\n'
    completed_start = final.index('    completed = (\n')
    completed_end = final.index('\n    )', completed_start)
    completion = final[completed_start:completed_end]
    if predicate.rstrip() not in completion:
        if old_predicate.rstrip() in completion:
            final = _replace_once(final, old_predicate, predicate, label="guardrail halted completion")
        else:
            final = _replace_once(final, anchor, anchor + predicate, label="guardrail halted completion")
    anchor = '        "partial": False,  # True only when stopped due to invalid tool calls\n'
    replacement = '        "partial": str(_turn_exit_reason) == "guardrail_recovery_incomplete",\n'
    if replacement not in final:
        final = _replace_once(final, anchor, replacement, label="guardrail partial result")
    outputs[finalizer] = final
    # The historical carrier explicitly tested preserving truncated text. Keep
    # that behavior, but correct its completion assertion on already-installed bases.
    test = target / "tests/run_agent/test_tool_call_guardrail_runtime.py"
    if not native and test.exists():
        tests = test.read_text()
        halt_assertion = '    assert result["turn_exit_reason"] == "guardrail_halt"\n'
        truth_assertion = halt_assertion + '    assert result["completed"] is False\n'
        if halt_assertion in tests:
            tests = tests.replace(truth_assertion, halt_assertion).replace(halt_assertion, truth_assertion)
            outputs[test] = tests
        name = 'def test_guardrail_recovery_salvages_truncated_visible_text_without_continuation'
        if name in tests:
            start = tests.index(name)
            end = tests.find('\ndef test_', start + len(name))
            end = len(tests) if end < 0 else end
            body = tests[start:end]
            if '"guardrail_recovery_incomplete"' not in body:
                body = body.replace(name + '():', '@pytest.mark.parametrize("finish_reason", ["length", "incomplete"])\n' + name + '(finish_reason):', 1)
                body = body.replace('finish_reason="length"', 'finish_reason=finish_reason', 1)
                body = body.replace('assert result["turn_exit_reason"] == "guardrail_recovery_answer"', 'assert result["turn_exit_reason"] == "guardrail_recovery_incomplete"\n    assert result["completed"] is False\n    assert result["partial"] is True', 1)
                outputs[test] = tests[:start] + body + tests[end:]
    return outputs


def _turn_context_reset_source(target: Path) -> tuple[Path, str]:
    path = target / "agent/turn_context.py"
    source = path.read_text(encoding="utf-8")
    if "_PER_TURN_RESET_STATE" in source:
        before = '    ("_pre_verify_nudges", 0), ("_open_todo_stop_nudges", 0),\n'
        after = before + '    ("_outcome_stop_turn_key", None), ("_outcome_stop_nudges", 0),\n    ("_outcome_stop_exhausted_claim", ""),\n'
    else:
        before = '    agent._open_todo_stop_nudges = 0\n'
        after = before + '    agent._outcome_stop_turn_key = None\n    agent._outcome_stop_nudges = 0\n    agent._outcome_stop_exhausted_claim = ""\n'
    if after not in source:
        source = _replace_once(source, before, after, label="per-turn outcome reset")
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
        anchor = '    # HERMES_OPEN_TODO_STOP_GUARD_v1: a todo plan created in this user turn is\n'
        source = _replace_once(source, anchor, insert + anchor, label="native stop owner")
    if MARKER not in final:
        anchor = '    "_open_todo_stop_synthetic",  # HERMES_OPEN_TODO_STOP_GUARD_v1\n'
        final = _replace_once(final, anchor, anchor + '    "_outcome_stop_synthetic",  # ' + MARKER + '\n', label="native outcome flag")
        final = _replace_once(final, '        final_response is not None\n        and not failed\n',
            '        final_response is not None\n        and not failed\n        and not getattr(agent, "_outcome_stop_exhausted_claim", "")\n', label="native truthful completion")
    final = _retry_finalizer_flags(final)
    cron_path = target / "cron/scheduler.py"
    cron = cron_path.read_text()
    if CRON_TOOLSET_MARKER not in cron:
        # Validate after construction so native plugin discovery has completed.
        cron = _replace_once(cron, '    return AIAgent(\n',
            '    _cron_enabled_toolsets = _resolve_cron_enabled_toolsets(job, _cfg)\n    agent = AIAgent(\n', label="native cron construction")
        cron = _replace_once(cron, '        enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),\n',
            '        enabled_toolsets=_cron_enabled_toolsets,\n', label="native toolset reuse")
        insert = textwrap.indent(textwrap.dedent(CRON_POST_AGENT_INSERT), "    ")
        cron = _replace_once(cron, '\n\nclass _FireAudit:', '\n' + insert + '    return agent\n\n\nclass _FireAudit:', label="native cron validation")
    if CRON_MAX_ITERATION_MARKER not in cron:
        cron = _replace_once(cron, textwrap.indent(textwrap.dedent(CRON_MAX_ITERATION_OLD), "    "),
            textwrap.indent(textwrap.dedent(CRON_MAX_ITERATION_NEW), "    "), label="native cron failure truth")
    delegate_path = target / "tools/delegate_tool_child_run.py"
    delegate = delegate_path.read_text()
    if SUBAGENT_COMPLETION_MARKER not in delegate:
        delegate = _replace_once(delegate,
            '        status = "completed" if schema.valid is not False and usable_summary else "failed"\n',
            '        # ' + SUBAGENT_COMPLETION_MARKER + '\n        status = "completed" if result.get("completed", False) and schema.valid is not False and usable_summary else "failed"\n', label="native subagent completion")
    outputs.update({cron_path: cron, delegate_path: delegate})
    native_tests = TEST_SOURCE
    for old, new in (("run_agent.get_tool_definitions", "model_tools.get_tool_definitions"),
                     ("run_agent.check_toolset_requirements", "model_tools.check_toolset_requirements"),
                     ("run_agent.OpenAI", "agent.process_bootstrap.OpenAI")):
        native_tests = native_tests.replace(old, new)
    # The fixtures mock the nonstreaming provider method explicitly.
    native_tests = native_tests.replace('    instance.compression_enabled = False', '    instance._disable_streaming = True\n    instance.compression_enabled = False')
    outputs.update({gate: source, finalizer: final,
                    target / "agent/outcome_stop.py": HELPER_SOURCE,
                    target / "tests/run_agent/test_outcome_stop_guard.py": native_tests})
    outputs = _guardrail_completion_sources(target, outputs)
    for path, body in outputs.items():
        compile(body, str(path), "exec")
    changed = False
    for path, body in outputs.items():
        if not path.exists() or path.read_text() != body:
            path.write_text(body)
            changed = True
    return changed


def patch_outcome_stop_guard_v1(hermes_dir: Path) -> bool:
    """Apply outcome continuation and cron toolset validation."""
    hermes_dir = Path(hermes_dir)
    if (hermes_dir / "agent/turn_stop_gates.py").exists():
        return _patch_native_stop(hermes_dir)
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
        finalizer = _replace_once(finalizer, FINALIZER_OLD, FINALIZER_NEW, label="finalizer synthetic flags")
        if COMPLETED_OLD in finalizer:
            finalizer = _replace_once(finalizer, COMPLETED_OLD, COMPLETED_NEW, label="finalizer completion")
        else:
            finalizer = _replace_once(
                finalizer,
                COMPLETED_021_OLD,
                COMPLETED_021_NEW,
                label="0.21 finalizer completion",
            )
        finalizer_path.write_text(finalizer, encoding="utf-8")
        changed = True

    retry_finalizer = _retry_finalizer_flags(finalizer)
    if retry_finalizer != finalizer:
        finalizer = retry_finalizer
        finalizer_path.write_text(finalizer, encoding="utf-8")
        changed = True

    if CRON_TOOLSET_MARKER not in cron:
        cron = _replace_once(
            cron,
            CRON_AGENT_ANCHOR,
            CRON_AGENT_INSERT + CRON_AGENT_ANCHOR,
            label="cron toolset resolve",
        )
        cron = _replace_once(cron, CRON_TOOLSET_OLD, CRON_TOOLSET_NEW, label="cron toolset reuse")
        cron = _replace_once(
            cron,
            CRON_POST_AGENT_ANCHOR,
            CRON_POST_AGENT_INSERT + CRON_POST_AGENT_ANCHOR,
            label="cron toolset validation",
        )
        cron_path.write_text(cron, encoding="utf-8")
        changed = True

    if CRON_CONFIGURED_MCP_MARKER not in cron:
        cron = _replace_once(
            cron,
            CRON_CONFIGURED_MCP_OLD,
            CRON_CONFIGURED_MCP_NEW,
            label="cron configured MCP toolset validation",
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
        if DELEGATE_OLD in delegate:
            delegate = _replace_once(
                delegate,
                DELEGATE_OLD,
                DELEGATE_NEW,
                label="subagent completion truth",
            )
        else:
            delegate = _replace_once(
                delegate,
                DELEGATE_021_OLD,
                DELEGATE_021_NEW,
                label="0.21 subagent completion truth",
            )
        delegate_path.write_text(delegate, encoding="utf-8")
        changed = True

    changed = (
        _write_exact(
            helper_path,
            HELPER_SOURCE,
            known_previous_sha256=(PREVIOUS_HELPER_SHA256, CANONICAL_HELPER_SHA256),
        )
        or changed
    )
    changed = (
        _write_exact(
            test_path,
            TEST_SOURCE,
            known_previous_sha256=(PREVIOUS_TEST_SHA256, CANONICAL_TEST_SHA256),
        )
        or changed
    )

    context_path, context_source = _turn_context_reset_source(hermes_dir)
    truth_outputs = _guardrail_completion_sources(hermes_dir, {context_path: context_source})
    for path, body in truth_outputs.items():
        compile(body, str(path), "exec")
    for path, body in truth_outputs.items():
        if path.read_text() != body:
            path.write_text(body)
            changed = True
    compile(conversation, str(conversation_path), "exec")
    compile(finalizer, str(finalizer_path), "exec")
    compile(cron, str(cron_path), "exec")
    compile(delegate, str(delegate_path), "exec")
    compile(HELPER_SOURCE, str(helper_path), "exec")
    compile(TEST_SOURCE, str(test_path), "exec")
    return changed
