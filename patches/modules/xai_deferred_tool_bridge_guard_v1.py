#!/usr/bin/env python3
"""Keep rejected deferred-tool bridge calls out of execution guardrails."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MARKER = "HERMES_XAI_DEFERRED_TOOL_BRIDGE_GUARD_v1"


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

    def test_out_of_scope_direct_tool_wrapper_is_rejected(self):
        from tools.tool_search import resolve_underlying_call

        name, arguments, err = resolve_underlying_call(
            {"name": "session_search", "arguments": {}},
            scoped_names=frozenset(),
        )
        assert name is None and arguments == {}
        assert err is not None and "session_search" in err

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
        assert "write_file" in excluded_err

'''

GUARDRAIL_TEST_ANCHOR = (
    "\ndef test_relay_rewrite_precedes_sequential_policy_approval_checkpoint_and_dispatch():\n"
)

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
    assert "session_search" in messages[0]["content"]

'''



NESTED_DISCOVERY_HELPER = '''def _restore_wrapped_discovery_call(name, arguments, aliases):
    """HERMES_NESTED_DISCOVERY_ALIAS_v1: recover only a declared discovery alias.

    A provider may wrap its advertised alias in tool_call. Lift a single valid
    call before dispatch so normal direct-tool scope and schema checks apply.
    Never infer aliases from spelling, unwrap batches, or execute unknown names.
    """
    if name != "tool_call" or not aliases:
        return name, arguments
    try:
        body = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(body, dict) or set(body) != {"calls"}:
            return name, arguments
        calls = body["calls"]
        if not isinstance(calls, list) or len(calls) != 1:
            return name, arguments
        call = calls[0]
        if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
            return name, arguments
        wire_name = call["name"]
        if not isinstance(wire_name, str) or aliases.get(wire_name) != "tool_search":
            return name, arguments
        nested = call["arguments"]
        nested = json.loads(nested) if isinstance(nested, str) else nested
        if not isinstance(nested, dict):
            return name, arguments
        return "tool_search", json.dumps(nested)
    except (TypeError, ValueError):
        return name, arguments


'''
NESTED_DISCOVERY_TESTS = '''import json
from types import SimpleNamespace
import pytest
from agent.transports import get_transport

@pytest.mark.parametrize('mode', ['codex_responses', 'chat_completions'])
@pytest.mark.parametrize('alias', ['hermes_tool_search', 'hermes_tool_search_2'])
def test_nested_discovery_alias_uses_request_provenance(mode, alias, monkeypatch):
    import agent.transports.codex
    import agent.transports.chat_completions
    transport = get_transport(mode)
    transport._last_wire_aliases = {alias: 'tool_search'}
    arguments = json.dumps({'calls': [{'name': alias, 'arguments': {'queries': ['gbrain get_page'], 'limit': 5}}]})
    call = SimpleNamespace(id='same-call-id', function=SimpleNamespace(name='tool_call', arguments=arguments))
    msg = SimpleNamespace(content=None, reasoning=None, tool_calls=[call])
    response = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason='tool_calls')], usage=None, output=[], status='completed')
    monkeypatch.setattr('agent.codex_responses_adapter._normalize_codex_response', lambda *a, **k: (msg, 'tool_calls'))
    normalized = transport.normalize_response(response).tool_calls[0]
    assert normalized.id == 'same-call-id'
    assert normalized.name == 'tool_search'
    assert json.loads(normalized.arguments) == {'queries': ['gbrain get_page'], 'limit': 5}

@pytest.mark.parametrize('mode', ['codex_responses', 'chat_completions'])
@pytest.mark.parametrize('aliases,body', [
    ({}, {'calls':[{'name':'hermes_tool_search','arguments':{}}]}),
    (None, {'calls':[{'name':'hermes_tool_search','arguments':{}}]}),
    ({'hermes_tool_search_2':'tool_search'}, {'calls':[{'name':'hermes_tool_search','arguments':{}}]}),
    ({'hermes_terminal':'terminal'}, {'calls':[{'name':'hermes_terminal','arguments':{}}]}),
    ({'hermes_tool_search':'tool_search'}, {'calls':[{'name':'hermes_tool_search','arguments':{}}, {'name':'other','arguments':{}}]}),
    ({'hermes_tool_search':'tool_search'}, {'calls':[{'name':'hermes_tool_search','arguments':[] }]}),
    ({'hermes_tool_search':'tool_search'}, {'calls':[{'name':'hermes_tool_search','arguments':{}}], 'unexpected':True}),
])
def test_nested_alias_never_guesses_or_widens_scope(mode, aliases, body, monkeypatch):
    import agent.transports.codex
    import agent.transports.chat_completions
    transport=get_transport(mode);transport._last_wire_aliases=aliases
    arguments=json.dumps(body)
    call=SimpleNamespace(id='preserved',function=SimpleNamespace(name='tool_call',arguments=arguments))
    msg=SimpleNamespace(content=None,reasoning=None,tool_calls=[call])
    response=SimpleNamespace(choices=[SimpleNamespace(message=msg,finish_reason='tool_calls')],usage=None,output=[],status='completed')
    monkeypatch.setattr('agent.codex_responses_adapter._normalize_codex_response',lambda *a,**k:(msg,'tool_calls'))
    result=transport.normalize_response(response).tool_calls[0]
    assert (result.id,result.name,result.arguments)==('preserved','tool_call',arguments)
'''

def patch_codex_discovery_alias(source: str) -> str:
    if "HERMES_NESTED_DISCOVERY_ALIAS_v1" in source:
        return source
    source = _replace_exact(source, "def _alias_reserved_tools(\n",
                            NESTED_DISCOVERY_HELPER + "def _alias_reserved_tools(\n",
                            count=1, label="discovery alias helper")
    old = "                tool_calls.append(ToolCall(\n"
    new = ('                arguments = tc.function.arguments if has_fn else getattr(tc, "arguments", "{}")\n'
           '                name, arguments = _restore_wrapped_discovery_call(name, arguments, alias_map)\n' + old)
    source = _replace_exact(source, old, new, count=1, label="Responses nested alias")
    return _replace_exact(source,
        '                    arguments=tc.function.arguments if has_fn else getattr(tc, "arguments", "{}"),\n',
        '                    arguments=arguments,\n', count=1, label="Responses normalized arguments")


def patch_chat_discovery_alias(source: str) -> str:
    if "HERMES_NESTED_DISCOVERY_ALIAS_v1" in source:
        return source
    old = '        arguments = getattr(tc_function, "arguments", None)\n'
    new = (old + '        # HERMES_NESTED_DISCOVERY_ALIAS_v1\n'
           '        from agent.transports.codex import _restore_wrapped_discovery_call\n'
           '        name, arguments = _restore_wrapped_discovery_call(name, arguments, alias_map)\n')
    return _replace_exact(source, old, new, count=1, label="Chat Completions nested alias")


def patch_discovery_alias_tests(source: str) -> str:
    if "def test_nested_discovery_alias_uses_request_provenance(" in source:
        return source
    return source + "\n\n" + NESTED_DISCOVERY_TESTS


def _replace_exact(source: str, old: str, new: str, *, count: int, label: str) -> str:
    if source.count(new) == count:
        return source
    if source.count(old) != count:
        raise RuntimeError(f"{label} anchor drift")
    return source.replace(old, new, count)


def patch_tool_search_text(source: str) -> str:
    if MARKER in source:
        return source
    if '"Invoke deferred tools. Takes `calls`' in source:
        source = _replace_exact(source,
            '            "Invoke deferred tools. Takes `calls`, an array of {name, arguments} "',
            f'            # {MARKER}\n'
            '            "Invoke ONLY deferred tools returned by tool_search; never bridge tools or directly listed tools. "\n'
            '            "Takes `calls`, an array of {name, arguments} "', count=1, label="batch bridge description")
        source = _replace_exact(source,
            '"arguments": {"type": "object", "description": "Arguments matching the tool schema."}',
            '"arguments": {"type": "object", "description": "Arguments matching the tool schema.", "additionalProperties": True}',
            count=1, label="batch arguments schema")
        if "return None, {}, not_deferrable_error(name)" in source:
            return source
        return _replace_exact(source, DIRECT_TOOL_ERROR_OLD, DIRECT_TOOL_ERROR_NEW,
                              count=1, label="batch direct route correction")
    raise RuntimeError("batch bridge description anchor drift")


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


def patch_d363_guardrail_tests_text(source: str) -> str:
    return _patch_guardrail_tests_text(source, D363_GUARDRAIL_TESTS)


def _load_sibling(name: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"tool bridge patch dependency unavailable: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch_mcp_legacy_alias_bridge_v1(hermes_dir: Path) -> bool:
    """Compose scoped alias resolution with provider-safe nested arguments.

    Supported Hermes versions own lazy MCP activation and shared tool execution.
    """
    legacy = _load_sibling("mcp_legacy_alias_dispatch_v1.py")
    root = Path(hermes_dir)
    transforms = {
        "tools/tool_search.py": (legacy.patch_tool_search_text, patch_tool_search_text),
        "model_tools.py": (legacy.patch_model_tools_text,),
        "agent/tool_executor.py": (legacy.patch_tool_executor_text,),
        "tests/tools/test_tool_search.py": (legacy.patch_tool_search_tests_text, patch_tool_search_tests_text),
        "tests/agent/test_tool_call_guardrail_runtime.py": (patch_d363_guardrail_tests_text,),
    }
    if (root / "tools/tool_search_validation.py").is_file():
        def validation(source):
            if "def not_deferrable_error(" in source:
                return source  # Native validation now owns corrective error guidance.
            old = '            return [], f"tool_call cannot invoke \'{name}\' (it is itself a bridge tool)"\n'
            new = ('            return [], (f"Route correction required: tool_call cannot invoke \'{name}\' because "\n'
                   '                        "it is a bridge tool. Call tool_search or tool_describe directly, or "\n'
                   '                        "call a deferred tool returned by tool_search.")\n')
            return source if new in source else _replace_exact(source, old, new, count=1, label="batch recursion correction")
        transforms["tools/tool_search_validation.py"] = (validation,)
        # Native provider schema now contains a list of calls; test its actual nested arguments.
        def batch_tests(source):
            source = patch_tool_search_tests_text(source)
            return source.replace('arguments = call_schema["parameters"]["properties"]["arguments"]',
                                  'arguments = call_schema["parameters"]["properties"]["calls"]["items"]["properties"]["arguments"]')
        transforms["tests/tools/test_tool_search.py"] = (legacy.patch_tool_search_tests_text, batch_tests)
    if (root / "agent/transports/codex.py").is_file():
        transforms.update({
            "agent/transports/codex.py": (patch_codex_discovery_alias,),
            "agent/transports/chat_completions.py": (patch_chat_discovery_alias,),
            "tests/agent/transports/test_codex_transport.py": (patch_discovery_alias_tests,),
        })
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
