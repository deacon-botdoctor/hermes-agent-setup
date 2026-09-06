#!/usr/bin/env python3
"""Keep rejected deferred-tool bridge calls out of execution guardrails."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MARKER = "HERMES_XAI_DEFERRED_TOOL_BRIDGE_GUARD_v1"

DESCRIPTION_OLD = '''    desc_call = (
        "Invoke a deferred tool by name with the given arguments. Argument shape "
        f"matches the tool's schema (see `{TOOL_DESCRIBE_NAME}`). Policy, hooks, "
        "and approvals run exactly as for any directly-listed tool."
    )
'''
DESCRIPTION_NEW = f'''    # {MARKER}
    desc_call = (
        "Invoke ONLY a deferred tool returned by tool_search. Never use this "
        "wrapper for a tool already listed directly, and never use it to invoke "
        "tool_search, tool_describe, or itself. The nested `arguments` object "
        f"must match the deferred tool's schema (see `{{TOOL_DESCRIBE_NAME}}`). "
        "Policy, hooks, and approvals run exactly as for any directly-listed tool."
    )
'''

# d363 inlined the bridge description instead of naming ``desc_call``.
D363_DESCRIPTION_OLD = '''            "Invoke a deferred tool by name with the given arguments. Argument shape "
            f"matches the tool's schema (see `{TOOL_DESCRIBE_NAME}`). Policy, hooks, "
            "and approvals run exactly as for any directly-listed tool.",
'''
D363_DESCRIPTION_NEW = f'''            # {MARKER}
            "Invoke ONLY a deferred tool returned by tool_search. Never use this "
            "wrapper for a tool already listed directly, and never use it to invoke "
            "tool_search, tool_describe, or itself. The nested `arguments` object "
            f"must match the deferred tool's schema (see `{{TOOL_DESCRIBE_NAME}}`). "
            "Policy, hooks, and approvals run exactly as for any directly-listed tool.",
'''
D363_ARGUMENTS_SCHEMA_OLD = '''                "arguments": {
                    "type": "object",
                    "description": "Arguments for the tool, matching its schema.",
                },
'''
D363_ARGUMENTS_SCHEMA_NEW = '''                "arguments": {
                    "type": "object",
                    "description": "Arguments for the tool, matching its schema.",
                    "additionalProperties": True,
                },
'''
D363_DIRECT_TOOL_ERROR_OLD = '''        return None, {}, (
            f"'{name}' is not a deferrable tool. If it appears in the model-facing tools "
            "list already, call it directly instead of via tool_call.")
'''
D363_DIRECT_TOOL_ERROR_NEW = '''        return None, {}, (
            f"Route correction required: '{name}' is not a deferrable tool. Do not "
            f"call tool_call again for '{name}'; call '{name}' directly with its "
            "arguments at the top level. tool_call is only for deferred tools "
            "returned by tool_search.")
'''

ARGUMENTS_SCHEMA_OLD = '''                        "arguments": {
                            "type": "object",
                            "description": "Arguments for the tool, matching its schema.",
                        },
'''
ARGUMENTS_SCHEMA_NEW = '''                        "arguments": {
                            "type": "object",
                            "description": "Arguments for the tool, matching its schema.",
                            "additionalProperties": True,
                        },
'''

RECURSION_ERROR_OLD = (
    "        return None, {}, f\"tool_call cannot invoke '{name}' "
    "(it is itself a bridge tool)\"\n"
)
RECURSION_ERROR_NEW = '''        return None, {}, (
            f"Route correction required: tool_call cannot invoke '{name}' because "
            "it is a bridge tool. Call tool_search or tool_describe directly, or "
            "call a deferred tool returned by tool_search."
        )
'''

DIRECT_TOOL_ERROR_OLD = '''        return None, {}, (
            f"'{name}' is not a deferrable tool. If it appears in the model-facing tools "
            "list already, call it directly instead of via tool_call."
        )
'''
DIRECT_TOOL_ERROR_NEW = '''        return None, {}, (
            f"Route correction required: '{name}' is not a deferrable tool. Do not "
            f"call tool_call again for '{name}'; call '{name}' directly with its "
            "arguments at the top level. tool_call is only for deferred tools "
            "returned by tool_search."
        )
'''

CONCURRENT_BLOCK_OLD = '''                if _activation_attempted and (_err or not _underlying):
                    _ts_scope_block = json.dumps(
                        {"error": _err or "MCP activation failed"}, ensure_ascii=False
                    )
                if not _err and _underlying:
'''
CONCURRENT_BLOCK_NEW = f'''                if _activation_attempted and (_err or not _underlying):
                    _ts_scope_block = json.dumps(
                        {{"error": _err or "MCP activation failed"}}, ensure_ascii=False
                    )
                # {MARKER}: resolution rejects are not executions of tool_call.
                if _err and _ts_scope_block is None:
                    _ts_scope_block = _err
                if not _err and _underlying:
'''

SEQUENTIAL_BLOCK_OLD = '''                if _activation_attempted and (_err or not _underlying):
                    _ts_scope_block = _err or "MCP activation failed"
                if not _err and _underlying:
'''
SEQUENTIAL_BLOCK_NEW = f'''                if _activation_attempted and (_err or not _underlying):
                    _ts_scope_block = _err or "MCP activation failed"
                # {MARKER}: return route correction without dispatch or guardrail accounting.
                if _err and _ts_scope_block is None:
                    _ts_scope_block = _err
                if not _err and _underlying:
'''

TOOL_SEARCH_TEST_ANCHOR = "\n\nclass TestLegacyMcpAliasDispatch:\n"
TOOL_SEARCH_TESTS = '''
    def test_bridge_schema_allows_arbitrary_nested_arguments(self):
        """Provider-strict schemas must permit deferred tool argument keys."""
        from tools.tool_search import bridge_tool_schemas, TOOL_CALL_NAME

        schemas = bridge_tool_schemas(1)
        call_schema = next(
            item["function"]
            for item in schemas
            if item["function"]["name"] == TOOL_CALL_NAME
        )
        arguments = call_schema["parameters"]["properties"]["arguments"]
        assert arguments["type"] == "object"
        assert arguments["additionalProperties"] is True

    def test_direct_tool_bridge_error_gives_explicit_route_correction(self):
        from tools.tool_search import resolve_underlying_call

        _, _, err = resolve_underlying_call(
            {"name": "session_search", "arguments": {}},
            scoped_names=frozenset(),
        )
        assert err is not None
        assert "Route correction required" in err
        assert "call 'session_search' directly" in err
        assert "Do not call tool_call again" in err

    def test_scoped_direct_tool_wrapper_resolves_without_widening_scope(self):
        from tools.tool_search import resolve_underlying_call

        name, arguments, err = resolve_underlying_call(
            {"name": "terminal", "arguments": {"command": "pwd"}},
            scoped_names=frozenset({"terminal"}),
        )
        assert err is None
        assert name == "terminal"
        assert arguments == {"command": "pwd"}

        _, _, excluded_err = resolve_underlying_call(
            {"name": "write_file", "arguments": {}},
            scoped_names=frozenset({"terminal"}),
        )
        assert excluded_err is not None
        assert "Route correction required" in excluded_err

'''

GUARDRAIL_TEST_ANCHOR = (
    "\ndef test_relay_rewrite_precedes_sequential_policy_approval_checkpoint_and_dispatch():\n"
)
GUARDRAIL_TESTS = f'''
def test_scoped_direct_tool_wrappers_dispatch_as_the_real_tools():
    """{MARKER}: recover the observed Enoch/Grok wrapper failure in scope."""
    agent = _make_agent(
        "terminal",
        "write_file",
        "tool_search",
        "tool_describe",
        "tool_call",
        config=_hard_stop_config(),
    )
    requested = [
        ("terminal", {{"command": "pwd"}}),
        ("write_file", {{"path": "/tmp/enoch-canary", "content": "ok"}}),
    ]
    calls = [
        _mock_tool_call(
            "tool_call",
            json.dumps({{"name": name, "arguments": arguments}}),
            f"c-{{i}}",
        )
        for i, (name, arguments) in enumerate(requested)
    ]
    messages = []

    with (
        patch(
            "agent.tool_executor._tool_search_scoped_names",
            return_value=frozenset(name for name, _ in requested),
        ),
        patch("run_agent.handle_function_call", return_value='{{"ok": true}}') as mock_hfc,
    ):
        agent._execute_tool_calls_concurrent(
            SimpleNamespace(content="", tool_calls=calls), messages, "task-1"
        )

    # Native execution is concurrent; prove exact dispatch without assuming start order.
    assert sorted(
        (call.args[0], json.dumps(call.args[1], sort_keys=True))
        for call in mock_hfc.call_args_list
    ) == sorted((name, json.dumps(arguments, sort_keys=True)) for name, arguments in requested)
    assert agent._tool_guardrail_halt_decision is None
    assert len(messages) == len(calls)
    assert all('"ok": true' in message["content"] for message in messages)


def test_out_of_scope_direct_tool_wrapper_is_still_blocked():
    agent = _make_agent("session_search", "tool_call", config=_hard_stop_config())
    call = _mock_tool_call(
        "tool_call",
        json.dumps({{"name": "session_search"}}),
        "c-direct-misroute",
    )
    messages = []

    with (
        patch(
            "agent.tool_executor._tool_search_scoped_names",
            return_value=frozenset(),
        ),
        patch("run_agent.handle_function_call", return_value="SHOULD_NOT_RUN") as mock_hfc,
    ):
        agent._execute_tool_calls_sequential(
            SimpleNamespace(content="", tool_calls=[call]), messages, "task-1"
        )

    mock_hfc.assert_not_called()
    assert agent._tool_guardrail_halt_decision is None
    assert "call 'session_search' directly" in messages[0]["content"]

'''

D363_GUARDRAIL_TESTS = f'''
def test_scoped_direct_tool_wrappers_dispatch_as_the_real_tools():
    """{MARKER}: recover the observed Enoch/Grok wrapper failure in scope."""
    agent = _make_agent(
        "terminal",
        "write_file",
        "tool_search",
        "tool_describe",
        "tool_call",
        config=_hard_stop_config(),
    )
    requested = [
        ("terminal", {{"command": "pwd"}}),
        ("write_file", {{"path": "/tmp/enoch-canary", "content": "ok"}}),
    ]
    calls = [
        _mock_tool_call(
            "tool_call",
            json.dumps({{"name": name, "arguments": arguments}}),
            f"c-{{i}}",
        )
        for i, (name, arguments) in enumerate(requested)
    ]
    messages = []

    with (
        patch(
            "agent.tool_executor._tool_search_scoped_names",
            return_value=frozenset(name for name, _ in requested),
        ),
        patch("model_tools.handle_function_call", return_value='{{"ok": true}}') as mock_hfc,
    ):
        agent._execute_tool_calls_concurrent(
            SimpleNamespace(content="", tool_calls=calls), messages, "task-1"
        )

    # Native execution is concurrent; prove exact dispatch without assuming start order.
    assert sorted(
        (call.args[0], json.dumps(call.args[1], sort_keys=True))
        for call in mock_hfc.call_args_list
    ) == sorted((name, json.dumps(arguments, sort_keys=True)) for name, arguments in requested)
    assert agent._tool_guardrail_halt_decision is None
    assert len(messages) == len(calls)
    assert all('"ok": true' in message["content"] for message in messages)


def test_out_of_scope_direct_tool_wrapper_is_still_blocked():
    agent = _make_agent("session_search", "tool_call", config=_hard_stop_config())
    call = _mock_tool_call(
        "tool_call",
        json.dumps({{"name": "session_search"}}),
        "c-direct-misroute",
    )
    messages = []
    # The resolver combines deferred names with the agent's direct-tool grant;
    # remove session_search from that grant to exercise the out-of-scope path.
    agent.valid_tool_names = frozenset({{"tool_call"}})

    with (
        patch(
            "agent.tool_executor._tool_search_scoped_names",
            return_value=frozenset(),
        ),
        patch("model_tools.handle_function_call", return_value="SHOULD_NOT_RUN") as mock_hfc,
    ):
        agent._execute_tool_calls_sequential(
            SimpleNamespace(content="", tool_calls=[call]), messages, "task-1"
        )

    mock_hfc.assert_not_called()
    assert agent._tool_guardrail_halt_decision is None
    assert "call 'session_search' directly" in messages[0]["content"]

'''


def _replace_exact(source: str, old: str, new: str, *, count: int, label: str) -> str:
    if source.count(new) == count:
        return source
    if source.count(old) != count:
        raise RuntimeError(f"{label} anchor drift")
    return source.replace(old, new, count)


def patch_tool_search_text(source: str) -> str:
    if MARKER in source:
        return source
    is_d363 = D363_DESCRIPTION_OLD in source
    if is_d363:
        source = _replace_exact(source, D363_DESCRIPTION_OLD, D363_DESCRIPTION_NEW,
                                count=1, label="d363 bridge description")
        source = _replace_exact(source, D363_ARGUMENTS_SCHEMA_OLD, D363_ARGUMENTS_SCHEMA_NEW,
                                count=1, label="d363 bridge arguments schema")
    else:
        source = _replace_exact(source, DESCRIPTION_OLD, DESCRIPTION_NEW,
                                count=1, label="bridge description")
        source = _replace_exact(source, ARGUMENTS_SCHEMA_OLD, ARGUMENTS_SCHEMA_NEW,
                                count=1, label="bridge arguments schema")
    source = _replace_exact(source, RECURSION_ERROR_OLD, RECURSION_ERROR_NEW,
                            count=1, label="bridge recursion error")
    return _replace_exact(
        source,
        D363_DIRECT_TOOL_ERROR_OLD if is_d363 else DIRECT_TOOL_ERROR_OLD,
        D363_DIRECT_TOOL_ERROR_NEW if is_d363 else DIRECT_TOOL_ERROR_NEW,
        count=1, label="direct tool route correction",
    )

def patch_tool_executor_text(source: str) -> str:
    # d363's single unwrap is made fail-closed by the alias residual itself:
    # resolver errors become scope blocks before guardrails/execution. The old
    # generation still needs these two cold-activation-shaped blocks.
    if "scoped_names=_scoped_names" in source and (
        "return function_name, function_args, err or \"tool_call could not be resolved\"" in source
    ):
        return source
    source = _replace_exact(
        source, CONCURRENT_BLOCK_OLD, CONCURRENT_BLOCK_NEW,
        count=1, label="concurrent bridge rejection",
    )
    return _replace_exact(
        source, SEQUENTIAL_BLOCK_OLD, SEQUENTIAL_BLOCK_NEW,
        count=1, label="sequential bridge rejection",
    )

def patch_tool_search_tests_text(source: str) -> str:
    if "test_bridge_schema_allows_arbitrary_nested_arguments" in source:
        return source
    if source.count(TOOL_SEARCH_TEST_ANCHOR) != 1:
        raise RuntimeError("tool_search test anchor drift")
    return source.replace(
        TOOL_SEARCH_TEST_ANCHOR,
        "\n" + TOOL_SEARCH_TESTS + TOOL_SEARCH_TEST_ANCHOR,
        1,
    )


def _patch_guardrail_tests_text(source: str, tests: str) -> str:
    if "test_scoped_direct_tool_wrappers_dispatch_as_the_real_tools" in source:
        return source
    if source.count(GUARDRAIL_TEST_ANCHOR) != 1:
        raise RuntimeError("guardrail test anchor drift")
    return source.replace(GUARDRAIL_TEST_ANCHOR, "\n" + tests + GUARDRAIL_TEST_ANCHOR, 1)


def patch_guardrail_tests_text(source: str) -> str:
    return _patch_guardrail_tests_text(source, GUARDRAIL_TESTS)


def patch_d363_guardrail_tests_text(source: str) -> str:
    return _patch_guardrail_tests_text(source, D363_GUARDRAIL_TESTS)


def patch_xai_deferred_tool_bridge_guard_v1(hermes_dir: Path) -> bool:
    transforms = {
        "tools/tool_search.py": patch_tool_search_text,
        "agent/tool_executor.py": patch_tool_executor_text,
        "tests/tools/test_tool_search.py": patch_tool_search_tests_text,
        "tests/run_agent/test_tool_call_guardrail_runtime.py": patch_guardrail_tests_text,
    }
    pending: list[tuple[Path, str]] = []
    for relative, transform in transforms.items():
        path = Path(hermes_dir) / relative
        original = path.read_text(encoding="utf-8")
        patched = transform(original)
        if patched != original:
            pending.append((path, patched))
    for path, patched in pending:
        path.write_text(patched, encoding="utf-8")
    return bool(pending)


def _load_sibling(name: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"tool bridge patch dependency unavailable: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch_mcp_legacy_alias_bridge_v1(hermes_dir: Path) -> bool:
    """Compose d363's scoped alias residual with the xAI guard.

    d363 already provides lazy MCP schema registration and first-call connect;
    do not revive the removed cold-control plugin. Historical cb input retains
    the old composition for exact generated compatibility.
    """
    legacy = _load_sibling("mcp_legacy_alias_dispatch_v1.py")
    root = Path(hermes_dir)
    model_source = (root / "model_tools.py").read_text(encoding="utf-8")
    is_d363 = legacy.D363_MODEL_TOOLS_CALL in model_source or legacy.MARKER in model_source
    transforms = {
        "tools/tool_search.py": (legacy.patch_tool_search_text, patch_tool_search_text),
        "model_tools.py": (legacy.patch_model_tools_text,),
        "agent/tool_executor.py": (legacy.patch_tool_executor_text,),
        "tests/tools/test_tool_search.py": (legacy.patch_tool_search_tests_text, patch_tool_search_tests_text),
        "tests/run_agent/test_tool_call_guardrail_runtime.py": (
            patch_d363_guardrail_tests_text if is_d363 else patch_guardrail_tests_text,
        ),
    }
    if not is_d363:
        cold = _load_sibling("mcp_legacy_cold_alias_activation_v1.py")
        transforms["model_tools.py"] += (cold.patch_model_tools_text,)
        transforms["agent/tool_executor.py"] += (cold.patch_tool_executor_text, patch_tool_executor_text)
    pending: list[tuple[Path, str]] = []
    for relative, steps in transforms.items():
        path = root / relative
        original = path.read_text(encoding="utf-8")
        patched = original
        for transform in steps:
            patched = transform(patched)
        if patched != original:
            pending.append((path, patched))
    for path, patched in pending:
        path.write_text(patched, encoding="utf-8")
    return bool(pending)

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_mcp_legacy_alias_bridge_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
