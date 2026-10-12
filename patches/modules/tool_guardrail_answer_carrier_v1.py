#!/usr/bin/env python3
"""Install bounded answer recovery on the pinned split Hermes turn pipeline."""
from __future__ import annotations

import json
from pathlib import Path

CURRENT_IDEMPOTENCY = "HERMES_TOOL_GUARDRAIL_ANSWER_CURRENT_v1"
CURRENT_MARKER_RELATIVE = Path(".golden-runtime-carriers/tool-guardrail-answer-current-v1.json")


def _replace_current_once(source: str, before: str, after: str, label: str) -> tuple[str, bool]:
    """Replace one reviewed current-source seam, accepting its exact postimage on retry."""
    if source.count(after) == 1 and before not in source:
        return source, False
    if source.count(before) != 1 or after in source:
        raise RuntimeError(f"current tool-guardrail {label} anchor drift")
    return source.replace(before, after, 1), True


def _current_tool_guardrail_mismatches(root: Path) -> list[str]:
    required = {
        "agent/tool_guardrails.py": (
            CURRENT_IDEMPOTENCY, "EMPTY_WEB_SEARCH_HALT_AFTER = 8",
            "repeated_empty_web_search_halt", "self._empty_web_search_streak",
        ),
        "agent/tool_dispatch_helpers.py": (CURRENT_IDEMPOTENCY, "_PARALLEL_SAFE_TOOLS"),
        "agent/turn_request_assembly.py": (CURRENT_IDEMPOTENCY, "_tool_guardrail_recovery_pending"),
        "agent/turn_context.py": (CURRENT_IDEMPOTENCY, "_tool_guardrail_recovery_sent"),
        "agent/turn_tool_round.py": (
            CURRENT_IDEMPOTENCY, "guardrail_recovery_answer", "_tool_guardrail_recovery_pending",
        ),
    }
    return [
        relative for relative, fragments in required.items()
        if not (root / relative).is_file()
        or any(fragment not in (root / relative).read_text(encoding="utf-8") for fragment in fragments)
    ]


def _is_current_split_tool_guardrail(root: Path) -> bool:
    return all(
        (root / relative).is_file()
        for relative in (
            "agent/tool_guardrails.py", "agent/tool_dispatch_helpers.py",
            "agent/turn_request_assembly.py", "agent/turn_context.py", "agent/turn_tool_round.py",
        )
    )


def _patch_current_halt_test(root: Path) -> bool:
    path = root / "tests/agent/test_tool_call_guardrail_runtime.py"
    if not path.is_file():
        return False
    source = path.read_text()
    old = '    assert executed == [("web_search", allowed_args, "c-allow")]'
    if old not in source:
        return False
    # Golden halts the whole batch after a repeated failure. Trigger the halt
    # before submitting workers so this safety proof does not depend on scheduling.
    source = source.replace('    starts = []\n    progress_events = []',
        '    assert agent._tool_guardrails.before_call("web_search", blocked_args).action == "block"\n    starts = []\n    progress_events = []', 1)
    source = source.replace(old, '    assert executed == []', 1)
    source = source.replace('    assert json.loads(messages[1]["content"]) == {"ok": "allowed"}',
        '    assert "repeated_exact_failure_block" in messages[1]["content"]', 1)
    source = source.replace('    assert starts == [("c-allow", "web_search", allowed_args)]', '    assert starts == []', 1)
    source = source.replace('    assert started_events == [("tool.started", "web_search", allowed_args, {})]\n    assert len(completed_events) == 1\n    assert completed_events[0][1] == "web_search"',
        '    assert started_events == []\n    assert completed_events == []', 1)
    compile(source, str(path), "exec")
    path.write_text(source)
    return True


def _patch_current_split_tool_guardrail(root: Path) -> bool:
    test_changed = _patch_current_halt_test(root)
    """Re-ground the bounded answer-only recovery on Hermes' split turn pipeline."""
    marker = root / CURRENT_MARKER_RELATIVE
    if marker.exists():
        try:
            installed = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("current tool-guardrail marker is unreadable") from exc
        if installed != {"idempotency": CURRENT_IDEMPOTENCY}:
            raise RuntimeError("current tool-guardrail marker provenance mismatch")
        mismatches = _current_tool_guardrail_mismatches(root)
        if mismatches:
            raise RuntimeError("current tool-guardrail carrier drifted: " + ", ".join(mismatches))
        return test_changed

    guardrails = root / "agent/tool_guardrails.py"
    dispatch = root / "agent/tool_dispatch_helpers.py"
    assembly = root / "agent/turn_request_assembly.py"
    context = root / "agent/turn_context.py"
    tool_round = root / "agent/turn_tool_round.py"
    sources = {path: path.read_text(encoding="utf-8") for path in (guardrails, dispatch, assembly, context, tool_round)}

    def replace(path: Path, before: str, after: str, label: str) -> None:
        sources[path], _ = _replace_current_once(sources[path], before, after, label)

    replace(
        guardrails,
        "_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50\n",
        "_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50\n\n"
        "EMPTY_WEB_SEARCH_HALT_AFTER = 8  # " + CURRENT_IDEMPOTENCY + "\n",
        "empty-search threshold",
    )
    replace(
        guardrails,
        '''    return (True, " [error]") if '"error"' in lower or '"failed"' in lower or result.startswith("Error") else (False, "")


# Guardrail verdict text''',
        '''    return (True, " [error]") if '"error"' in lower or '"failed"' in lower or result.startswith("Error") else (False, "")


def _web_search_result_is_empty(result: str | None) -> bool:
    if not isinstance(result, str) or not result.strip():
        return True
    parsed = safe_json_loads(result)
    if isinstance(parsed, dict):
        for key in ("results", "items", "data"):
            if key in parsed:
                value = parsed[key]
                return isinstance(value, (list, tuple, dict)) and not value
    return result.strip().lower() in {"no results", "no search results", "[]", "{}"}


# Guardrail verdict text''',
        "empty-search classifier",
    )
    replace(
        guardrails,
        '    "loop_subagent_cap": (\n        "Blocked delegate_task: this turn has already spawned {count} subagents (limit {cap}). "\n        "This looks like a runaway delegation loop. Finish the work with the results you have and answer the user."\n    ),\n}',
        '    "loop_subagent_cap": (\n        "Blocked delegate_task: this turn has already spawned {count} subagents (limit {cap}). "\n        "This looks like a runaway delegation loop. Finish the work with the results you have and answer the user."\n    ),\n    "repeated_empty_web_search_halt": (\n        "Stopped web_search: {count} consecutive searches returned no results. "\n        "Use the evidence already gathered and answer the user."\n    ),\n}',
        "empty-search message",
    )
    replace(
        guardrails,
        "        self._turn_web_search_count = 0\n        self._turn_subagent_count = 0\n",
        "        self._turn_web_search_count = 0\n        self._turn_subagent_count = 0\n"
        "        self._empty_web_search_streak = 0  # " + CURRENT_IDEMPOTENCY + "\n",
        "empty-search reset",
    )
    replace(
        guardrails,
        "        allow = ToolGuardrailDecision(tool_name=tool_name, signature=signature)\n\n        # Loop caps",
        "        allow = ToolGuardrailDecision(tool_name=tool_name, signature=signature)\n"
        "        if self._halt_decision is not None:\n"
        "            return self._halt_decision  # " + CURRENT_IDEMPOTENCY + "\n\n        # Loop caps",
        "halt persistence",
    )
    replace(
        guardrails,
        "        warnings = self.config.warnings_enabled\n\n        if failed:\n",
        "        warnings = self.config.warnings_enabled\n\n"
        "        if tool_name == \"web_search\":\n"
        "            if not failed and _web_search_result_is_empty(result):\n"
        "                self._empty_web_search_streak += 1\n"
        "                empty_count = self._empty_web_search_streak\n"
        "                if empty_count >= EMPTY_WEB_SEARCH_HALT_AFTER:\n"
        "                    return self._decide(\"halt\", \"repeated_empty_web_search_halt\", tool_name, empty_count, signature)\n"
        "            else:\n"
        "                self._empty_web_search_streak = 0\n\n"
        "        if failed:\n",
        "empty-search observation",
    )
    replace(
        dispatch,
        '    "web_extract",\n    "web_search",\n})',
        '    "web_extract",\n    # ' + CURRENT_IDEMPOTENCY + ': web searches form sequential barriers.\n})',
        "web-search sequential barrier",
    )
    replace(
        assembly,
        "    tools_for_api = agent.tools\n",
        "    tools_for_api = ([] if getattr(agent, \"_tool_guardrail_recovery_pending\", False) else agent.tools)\n"
        "    # " + CURRENT_IDEMPOTENCY + ": exactly one recovery request cannot dispatch tools.\n",
        "answer-only request",
    )
    # Native removed the per-turn vision flag; preserve the pinned postimage.
    if '    ("_tool_guardrail_halt_decision", None), ("_vision_supported", True),\n' in sources[context]:
        replace(
            context,
            '    ("_tool_guardrail_halt_decision", None), ("_vision_supported", True),\n',
            '    ("_tool_guardrail_halt_decision", None), ("_tool_guardrail_recovery_pending", False),\n'
            '    ("_tool_guardrail_recovery_sent", False), ("_vision_supported", True),  # ' + CURRENT_IDEMPOTENCY + '\n',
            "per-turn recovery reset",
        )
    else:
        replace(
            context,
            '    ("_tool_guardrail_halt_decision", None),\n',
            '    ("_tool_guardrail_halt_decision", None), ("_tool_guardrail_recovery_pending", False),\n'
            '    ("_tool_guardrail_recovery_sent", False),  # ' + CURRENT_IDEMPOTENCY + '\n',
            "per-turn recovery reset",
        )
    before = r'''    if agent._tool_guardrail_halt_decision is not None:
        decision = agent._tool_guardrail_halt_decision
        _turn_exit_reason = "guardrail_halt"
        final_response = agent._toolguard_controlled_halt_response(decision)
        agent._emit_diagnostic_status(f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}")
        append_message(messages, {"role": "assistant", "content": final_response})
        # Emit the halt so it isn't mistaken for a crash; the stream callback is still
        # alive, so SSE/TUI clients see the explanation.
        if final_response:
            agent._safe_print(f"\n{final_response}\n")
            if agent.stream_delta_callback:
                with suppress(Exception):
                    agent.stream_delta_callback(final_response)
                    agent.stream_delta_callback(None)
        return _verdict("break")
'''
    after = r'''    if agent._tool_guardrail_halt_decision is not None:
        decision = agent._tool_guardrail_halt_decision
        if not getattr(agent, "_tool_guardrail_recovery_sent", False):
            # HERMES_TOOL_GUARDRAIL_ANSWER_CURRENT_v1 / HERMES_TOOL_GUARDRAIL_ANSWER_CARRIER_v1:
            # make one no-tool answer request
            # while preserving the durable evidence already returned by the tools.
            agent._tool_guardrail_recovery_sent = True
            agent._tool_guardrail_recovery_pending = True
            agent._tool_guardrail_halt_decision = None
            agent._tool_guardrails._halt_decision = None
            agent._emit_diagnostic_status(
                f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}; "
                "requesting a final answer without more tools"
            )
            _turn_exit_reason = "guardrail_recovery_answer"
            return _verdict("continue")
        _turn_exit_reason = "guardrail_halt"
        final_response = agent._toolguard_controlled_halt_response(decision)
        agent._emit_diagnostic_status(f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}")
        append_message(messages, {"role": "assistant", "content": final_response})
        if final_response:
            agent._safe_print(f"\n{final_response}\n")
            if agent.stream_delta_callback:
                with suppress(Exception):
                    agent.stream_delta_callback(final_response)
                    agent.stream_delta_callback(None)
        return _verdict("break")
'''
    replace(tool_round, before, after, "bounded answer recovery")

    for path, source in sources.items():
        compile(source, str(path), "exec")
        path.write_text(source, encoding="utf-8")
    mismatches = _current_tool_guardrail_mismatches(root)
    if mismatches:
        raise RuntimeError("current tool-guardrail postimage verification failed: " + ", ".join(mismatches))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"idempotency": CURRENT_IDEMPOTENCY}, sort_keys=True) + "\n", encoding="utf-8")
    return True


def patch_tool_guardrail_answer_carrier_v1(root: Path) -> bool:
    """Apply only the supported split runtime; retired carrier revisions fail closed."""
    root = Path(root).resolve()
    if not _is_current_split_tool_guardrail(root):
        raise RuntimeError("current tool-guardrail requires all split runtime targets")
    return _patch_current_split_tool_guardrail(root)
