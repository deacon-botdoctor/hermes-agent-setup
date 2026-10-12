"""Install content-free terminal receipts around main and auxiliary LLM attempts."""


from __future__ import annotations


import runpy


import re


import shutil


from pathlib import Path


MARKER = "HERMES_LLM_ATTEMPT_RECEIPTS_v1"


PAYLOAD = Path(__file__).resolve().parent.parent / "payloads/llm-attempt-receipts-v1/agent/llm_attempt_receipts.py"


TRUNCATED_RETRY_MARKER = "HERMES_TRUNCATED_TOOL_RETRY_CACHE_BUST_v1"


CODEX_APP_SERVER_MARKER = "HERMES_LLM_ATTEMPT_CODEX_APP_SERVER_v1"


TRUNCATED_TOOL_SAFE_MESSAGE = (
    "The action was truncated due to output length limit before it finished. "
    "I did not run the incomplete action. Please retry; I’ll continue in smaller steps."
)


class PatchError(RuntimeError):
    pass


def _replace_once(content: str, old: str, new: str, label: str) -> str:
    if content.count(old) != 1:
        raise PatchError(f"required unique anchor missing: {label}")
    return content.replace(old, new, 1)


def _patch_instruction_accounting(content: str) -> str:
    if 'instructions=next_api_kwargs.get("instructions")' in content:
        return content
    anchor = 'toolset_for_tool=_runtime_tool_registry.get_toolset_for_tool,'
    if content.count(anchor) != 1:
        raise PatchError("required unique anchor missing: request instruction accounting")
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if anchor in line:
            indent = line[:len(line) - len(line.lstrip())]
            lines.insert(index + 1, indent + 'instructions=next_api_kwargs.get("instructions"),\n')
            break
    return "".join(lines)


def _resolver_wrapper() -> str:
    return r"""

# HERMES_LLM_ATTEMPT_RECEIPTS_v1: keep the concrete provider client intact,
# but wrap its chat-completions execution seam. The wrapper is dormant outside
# an auxiliary_request context, so main-loop clients are not double-receipted.
_resolve_provider_client_without_attempt_receipts = resolve_provider_client


def resolve_provider_client(
    provider: str,
    model: str = None,
    async_mode: bool = False,
    raw_codex: bool = False,
    explicit_base_url: str = None,
    explicit_api_key: str = None,
    api_mode: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    is_vision: bool = False,
    task: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    client, resolved_model = _resolve_provider_client_without_attempt_receipts(
        provider,
        model,
        async_mode,
        raw_codex,
        explicit_base_url,
        explicit_api_key,
        api_mode,
        main_runtime,
        is_vision,
        task,
    )
    if not raw_codex:
        try:
            from agent.llm_attempt_receipts import instrument_auxiliary_client

            client = instrument_auxiliary_client(
                client,
                provider=provider,
                model=resolved_model or model,
                task=task,
                api_mode=api_mode,
            )
        except Exception:
            logger.debug(
                "Auxiliary attempt instrumentation unavailable", exc_info=True
            )
    return client, resolved_model
"""


def _vision_resolver_wrapper() -> str:
    return r"""


_resolve_vision_provider_client_without_attempt_receipts = resolve_vision_provider_client


def resolve_vision_provider_client(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    async_mode: bool = False,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[Any], Optional[str]]:
    resolved_provider, client, resolved_model = (
        _resolve_vision_provider_client_without_attempt_receipts(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            async_mode=async_mode,
            main_runtime=main_runtime,
        )
    )
    try:
        from agent.llm_attempt_receipts import instrument_auxiliary_client

        client = instrument_auxiliary_client(
            client,
            provider=resolved_provider or provider,
            model=resolved_model or model,
            task="vision",
        )
    except Exception:
        logger.debug(
            "Vision attempt instrumentation unavailable", exc_info=True
        )
    return resolved_provider, client, resolved_model
"""


def _patch_codex_app_server(content: str) -> str:
    """Account for the opaque Codex app-server turn boundary.

    ``codex_app_server`` returns before the ordinary conversation-loop provider
    seam, so without this carrier a model-backed turn can bypass the attempt
    ledger entirely. Treat one app-server turn as one accountable main attempt;
    the app-server remains responsible for its internal provider lifecycle.
    """
    if CODEX_APP_SERVER_MARKER in content:
        return content
    anchor = """    try:
        _start_codex_thread(agent)
        wire_model = _codex_wire_model(agent, getattr(agent, "_codex_session_model_provider", None))
        turn = agent._codex_session.run_turn(
            user_input=user_message,
            model=wire_model, reasoning_effort=_codex_turn_effort(agent, wire_model),
            service_tier=_codex_turn_service_tier(agent))
    except Exception as exc:
"""
    replacement = f"""    try:
        _start_codex_thread(agent)
        # {CODEX_APP_SERVER_MARKER}: this runtime bypasses the ordinary
        # conversation-loop provider seam. Bind the opaque app-server turn to
        # the same content-free accountable-attempt ledger before dispatch.
        from agent.llm_attempt_receipts import execute_main_attempt

        _codex_attempt_id = f"codex-app-server:{{effective_task_id}}"
        wire_model = _codex_wire_model(agent, getattr(agent, "_codex_session_model_provider", None))
        turn = execute_main_attempt(
            lambda: agent._codex_session.run_turn(user_input=user_message, model=wire_model,
                reasoning_effort=_codex_turn_effort(agent, wire_model),
                service_tier=_codex_turn_service_tier(agent)),
            task=effective_task_id or "conversation",
            provider=str(getattr(agent, "provider", "") or "openai-codex"),
            model=str(getattr(agent, "model", "") or ""),
            base_url=str(getattr(agent, "base_url", "") or ""),
            api_mode=str(getattr(agent, "api_mode", "") or "codex_app_server"),
            logical_request_id=_codex_attempt_id,
            session_id=str(getattr(agent, "session_id", "") or ""),
            turn_id=str(effective_task_id or ""),
            platform=str(getattr(agent, "platform", "") or ""),
            api_key=getattr(agent, "api_key", None),
            retry_count=0,
            is_fallback=bool(getattr(agent, "_fallback_activated", False)),
            fallback_cause=getattr(agent, "_active_fallback_cause", None),
        )
    except Exception as exc:
"""
    return _replace_once(
        content,
        anchor,
        replacement,
        "Codex app-server accountable attempt boundary",
    )


def _native_owner(source: str, name: str, transform) -> str:
    import ast
    nodes = [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(nodes) != 1:
        raise PatchError(f"required unique native owner missing: {name}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    before = "".join(lines[node.lineno - 1:node.end_lineno])
    after = transform(before)
    return "".join(lines[:node.lineno - 1]) + after + "".join(lines[node.end_lineno:])


def _native_receipt_api(source: str) -> str:
    if MARKER in source:
        return _patch_instruction_accounting(source)
    # Preserve native transport preflight, Relay and streaming bodies verbatim.
    def wrap(body):
        start = body.index("        if _use_streaming")
        raw = body[start:]
        import textwrap
        wrapped = "        def _raw_provider_call():\n" + textwrap.indent(raw, "    ")
        tail = r'''from agent.llm_attempt_receipts import execute_main_attempt
from agent.runtime_performance_events import build_payload_breakdown
from tools.registry import registry as _runtime_tool_registry

_runtime_payload_breakdown = build_payload_breakdown(
    next_api_kwargs.get("messages")
    or next_api_kwargs.get("input")
    or [],
    next_api_kwargs.get("tools") or [],
    toolset_for_tool=_runtime_tool_registry.get_toolset_for_tool,
    instructions=next_api_kwargs.get("instructions"),
)
return execute_main_attempt(
    _raw_provider_call,
    task=effective_task_id or "conversation",
    provider=agent.provider,
    model=agent.model,
    base_url=str(agent.base_url or ""),
    api_mode=agent.api_mode,
    logical_request_id=api_request_id,
    session_id=agent.session_id or "",
    turn_id=turn_id,
    platform=agent.platform or "",
    api_key=(
        getattr(getattr(agent, "client", None), "api_key", None)
        or getattr(agent, "_anthropic_api_key", None)
        or getattr(agent, "api_key", None)
    ),
    retry_count=retry_count,
    is_fallback=bool(getattr(agent, "_fallback_activated", False)),
    fallback_cause=getattr(agent, "_active_fallback_cause", None),
    payload_breakdown=_runtime_payload_breakdown,
    defer_success=True,
)
'''
        return body[:start] + wrapped + "\n" + textwrap.indent(tail, "        ")
    source = _native_owner(source, "_perform_api_call", wrap)
    source = _replace_once(source, "        nonlocal thinking_spinner\n",
        "        # " + MARKER + "\n        from agent.llm_attempt_receipts import record_main_first_byte\n        record_main_first_byte(api_request_id)\n        nonlocal thinking_spinner\n", "native first byte")
    return _replace_once(source, "    if _redirect_crossed_response:\n",
        '    if _redirect_crossed_response:\n        from agent.llm_attempt_receipts import finish_main_attempt\n        finish_main_attempt(api_request_id, "cancelled", response=response,\n                            error=RuntimeError("response discarded after redirect"))\n', "native redirect receipt")


def _native_receipt_validation(source: str) -> str:
    if MARKER in source:
        return source
    source = _replace_once(source, "            agent, response, finish_reason, _retry, messages=messages,\n", "            agent, response, finish_reason, _retry, messages=messages, api_messages=api_messages,\n", "native truncated wire propagation")
    anchor = "    response_invalid, error_details = validate_response_shape(agent, response)\n"
    return _replace_once(source, anchor, anchor +
        '    # ' + MARKER + '\n    from agent.llm_attempt_receipts import finish_main_attempt\n'
        '    finish_main_attempt(api_request_id, "error" if response_invalid else "success",\n'
        '                        response=response, error=(RuntimeError(", ".join(error_details) or "Invalid API response")\n'
        '                                                  if response_invalid else None))\n', "native validation receipt")


def _native_receipt_auxiliary(source: str) -> str:
    # Native async fallback conversion creates a fresh client after the resolver
    # wrapper ran. Bind the physical create seam again with the resolved route.
    old = '            fb_client, _ = _to_async_client(fb_client, fb_model or "", is_vision=(task == "vision"))\n'
    new = ('            _receipt_destination = _fallback_destination(task, fb_client, fb_model, fb_label)\n'
           + old
           + '            from agent.llm_attempt_receipts import instrument_auxiliary_client\n'
           + '            fb_client = instrument_auxiliary_client(\n'
           + '                fb_client, provider=_receipt_destination.provider,\n'
           + '                model=_receipt_destination.model or fb_model, task=task,\n'
           + '                api_mode=_receipt_destination.api_mode)\n')
    source = _native_owner(source, "_async_call_llm_impl", lambda body:
        body if new in body else _replace_once(body, old, new, "native async fallback receipt"))
    if MARKER in source:
        return source
    # Keep current public signatures and native throttling/stream ownership.
    def context(body):
        import ast, textwrap
        node = ast.parse(body).body[0]
        first = node.body[1] if isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) else node.body[0]
        lines = body.splitlines(keepends=True)
        return ("".join(lines[:first.lineno - 1]) +
                "    from agent.llm_attempt_receipts import auxiliary_request\n" +
                "    with auxiliary_request(task=task, provider=provider, model=model):\n" +
                textwrap.indent("".join(lines[first.lineno - 1:]), "    "))
    for name in ("call_llm", "async_call_llm"):
        source = _native_owner(source, name, context)
    # The resolver wrapper instruments retry/fallback routes too; final preparation
    # additionally covers direct clients which bypass that resolver.
    source = source.rstrip() + _resolver_wrapper() + _vision_resolver_wrapper() + "\n"
    anchor = "    effective_timeout = _effective_aux_timeout(task, timeout)\n"
    return _replace_once(source, anchor,
        "    from agent.llm_attempt_receipts import instrument_auxiliary_client\n"
        "    client = instrument_auxiliary_client(client, provider=effective_provider or resolved_provider,\n"
        "                                         model=final_model, task=task, api_mode=resolved_api_mode)\n" + anchor,
        "native shared auxiliary client")


def _native_receipt_fallback(source: str) -> str:
    if MARKER in source:
        return source
    fallback_anchor = re.compile(
        r"^(?P<indent>[ \t]+)agent\._fallback_activated = True$", re.MULTILINE
    )
    match = fallback_anchor.search(source)
    if match is None or fallback_anchor.search(source, match.end()) is not None:
        raise RuntimeError("native fallback cause anchor missing or ambiguous")
    indent = match.group("indent")
    replacement = (
        f"{indent}agent._fallback_activated = True\n"
        f"{indent}# {MARKER}\n"
        f"{indent}agent._active_fallback_cause = getattr(reason, 'value', None) or "
        "(str(reason) if reason is not None else 'unclassified_failure')\n"
    )
    source = source[:match.start()] + replacement + source[match.end():]
    source = _replace_once(source, '        codex_kwargs.pop("tools", None)\n',
        '        for key in ("tools", "tool_choice", "parallel_tool_calls"):\n            codex_kwargs.pop(key, None)\n', "native summary tool fields")
    # Native site_copy keeps provider error details out of the final response.
    return source


def _native_receipt_truncation(source: str) -> str:
    if TRUNCATED_RETRY_MARKER in source:
        before = '        recovery = {"role": "user", "content": (\n'
        after = '        recovery = {"role": "user", "_tool_retry_synthetic": True, "content": (\n'
        return source if after in source else _replace_once(source, before, after, "native retry synthetic flag")
    anchor = '        return st.done("continue")  # don\'t append the broken response\n'
    replacement = (
        '        # ' + TRUNCATED_RETRY_MARKER + '\n'
        '        recovery = {"role": "user", "_tool_retry_synthetic": True, "content": (\n'
        '            "The previous tool call was incomplete and was not executed. Retry the same intended action now, "\n'
        '            "but keep the tool arguments concise and split large content or scripts into smaller tool calls. "\n'
        '            f"Recovery attempt {n}/4.")}\n'
        '        append_message(st.messages, recovery)\n'
        '        if st.api_messages is not None and st.api_messages is not st.messages:\n            append_message(st.api_messages, dict(recovery))\n'
        '        agent._session_messages = st.messages\n'
        '        return st.done("continue")\n')
    source = _replace_once(source, anchor, replacement, "native truncated request identity")
    source = _replace_once(source, "    agent: Any\n    response: Any\n", "    agent: Any\n    api_messages: Any = None\n    response: Any\n", "native wire truncation state")
    source = _replace_once(source, "    compression_attempts: int,\n) -> TruncationVerdict:\n", "    compression_attempts: int, api_messages: Any = None,\n) -> TruncationVerdict:\n", "native wire truncation input")
    source = _replace_once(source, "        agent=agent, response=response, finish_reason=finish_reason,\n", "        agent=agent, api_messages=api_messages, response=response, finish_reason=finish_reason,\n", "native wire truncation propagation")
    source = _replace_once(source, '    close_interrupted_tool_sequence(st.messages, _final_response)\n',
        '    if st.messages and st.messages[-1].get("role") == "user":\n'
        '        append_message(st.messages, {"role": "assistant", "content": _final_response})\n'
        '    close_interrupted_tool_sequence(st.messages, _final_response)\n', "native truncated tail")
    return source


NATIVE_RECEIPT_TESTS = r'''

@pytest.mark.parametrize("invalid_first", [False, True])
def test_golden_native_receipts_validate_physical_attempts(agent, tmp_path, monkeypatch, invalid_first):
    from agent import llm_attempt_receipts
    TestRunConversation()._setup_agent(agent)
    ledger = tmp_path / "attempts.jsonl"
    monkeypatch.setenv("HERMES_LLM_ATTEMPT_LEDGER", str(ledger))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    responses = [_mock_response(content="Finished and verified.")]
    if invalid_first:
        responses.insert(0, SimpleNamespace(choices=[]))
    agent.client.chat.completions.create.side_effect = responses
    with patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("private prompt sentinel")
    assert result["final_response"] == "Finished and verified."
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    starts = [e for e in events if e["event"] == "started"]
    terminals = [e for e in events if e["event"] == "terminal"]
    assert len(starts) == len(terminals) == (2 if invalid_first else 1)
    assert [e["outcome"] for e in terminals] == (["error", "success"] if invalid_first else ["success"])
    assert {e["attempt_id"] for e in starts} == {e["attempt_id"] for e in terminals}
    assert "private prompt sentinel" not in ledger.read_text()
    assert "test-key-1234567890" not in ledger.read_text()


def test_golden_native_truncation_changes_wire_request(agent):
    TestRunConversation()._setup_agent(agent)
    agent.valid_tool_names.add("write_file")
    bad = _mock_tool_call(name="write_file", arguments='{"path":"report","content":"partial', call_id="bad")
    responses = iter([_mock_response(content="", finish_reason="length", tool_calls=[bad]), _mock_response(content="Finished.")])
    requests = []
    def create(**kwargs):
        requests.append(json.loads(json.dumps(kwargs["messages"])))
        return next(responses)
    agent.client.chat.completions.create.side_effect = create
    with patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"), patch("model_tools.handle_function_call") as dispatch:
        result = agent.run_conversation("write a report")
    assert result["final_response"] == "Finished."
    assert requests[0] != requests[1]
    assert "Recovery attempt 1/4" in requests[1][-1]["content"]
    dispatch.assert_not_called()
'''


def _native_main_tests(source: str) -> str:
    old = '        assert result == "Summary"\n        input_items = captured["input"]\n'
    new = '        assert result == "Summary"\n        assert "tools" not in captured\n        assert "tool_choice" not in captured\n        assert "parallel_tool_calls" not in captured\n        input_items = captured["input"]\n'
    source = source if new in source else _replace_once(source, old, new, "native toolless summary regression")
    return source if "def test_golden_native_receipts_validate_physical_attempts(" in source else source.rstrip() + NATIVE_RECEIPT_TESTS


NATIVE_AUX_RECEIPT_TESTS = r'''

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_golden_native_auxiliary_attempt_receipt(tmp_path, monkeypatch, asynchronous):
    from agent import auxiliary_client as aux
    response = SimpleNamespace(id="response-test", model="test-model", usage={"prompt_tokens": 4, "completion_tokens": 2}, choices=[SimpleNamespace(message=SimpleNamespace(content="result"))])
    create = AsyncMock(return_value=response) if asynchronous else MagicMock(return_value=response)
    client = SimpleNamespace(base_url="https://example.invalid/v1", api_key="testkey", chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setenv("HERMES_LLM_ATTEMPT_LEDGER", str(tmp_path / "attempts.jsonl"))
    monkeypatch.setattr(aux, "_resolve_task_provider_model", lambda *a, **k: ("openai", "test-model", "https://example.invalid/v1", None, "chat_completions"))
    monkeypatch.setattr(aux, "_resolve_call_client", lambda *a, **k: (client, "test-model", "openai", "openai"))
    monkeypatch.setattr(aux, "_get_task_extra_body", lambda *a, **k: {})
    monkeypatch.setattr(aux, "_provider_requires_stream", lambda *a, **k: False)
    kwargs = dict(task="title", provider="openai", model="test-model", messages=[{"role": "user", "content": "private-content-sentinel"}])
    actual = await aux.async_call_llm(**kwargs) if asynchronous else aux.call_llm(**kwargs)
    assert actual is response
    create.assert_called_once()
    raw = (tmp_path / "attempts.jsonl").read_text()
    events = [json.loads(row) for row in raw.splitlines()]
    assert [e["event"] for e in events] == ["started", "terminal"]
    assert events[-1]["outcome"] == "success"
    assert events[0]["attempt_id"] == events[1]["attempt_id"]
    assert events[0]["task"] == "title"
    assert "private-content-sentinel" not in raw
    assert "testkey" not in raw
'''


def _native_auxiliary_tests(source: str) -> str:
    old = "    def test_refresh_provider_credentials_force_refreshes_anthropic_oauth_and_evicts_cache(self, monkeypatch):\n"
    new = old.replace("monkeypatch):", "monkeypatch, tmp_path):")
    if new not in source:
        source = _replace_once(source, old, new, "native auth test isolation")
    anchor = '            patch("agent.auxiliary_client._client_cache", {cache_key: (stale_client, "claude-haiku-4-5-20251001", None)}),\n'
    if 'patch("agent.anthropic_credentials.claude_code_credentials_path", return_value=tmp_path / ".credentials.json")' not in source:
        source = _replace_once(source, anchor,
        '            patch("agent.anthropic_credentials.claude_code_credentials_path", return_value=tmp_path / ".credentials.json"),\n' + anchor,
        "native test credential lock path")

    return source if "def test_golden_native_auxiliary_attempt_receipt(" in source else source.rstrip() + NATIVE_AUX_RECEIPT_TESTS


def _patch_native_receipts(target: Path) -> bool:
    def status(source):
        old = '            agent._emit_diagnostic_status(\n                f"⚠️ Iteration budget exhausted ({api_call_count}/{agent.max_iterations}) "\n                "— asking model to summarise"\n            )\n'
        new = '            logger.info("Iteration budget exhausted (%d/%d); requesting toolless summary", api_call_count, agent.max_iterations)\n'
        return source if new in source else _replace_once(source, old, new, "native summary status")
    updates = {
        "agent/turn_api_call.py": _native_receipt_api,
        "agent/turn_response_check.py": _native_receipt_validation,
        "agent/turn_truncation.py": _native_receipt_truncation,
        "agent/chat_completion_helpers.py": _native_receipt_fallback,
        "agent/turn_finalizer.py": status,
        "agent/auxiliary_client.py": _native_receipt_auxiliary,
        "agent/codex_runtime.py": _patch_codex_app_server,
        ("tests/agent/test_run_agent.py" if (target / "tests/agent/test_run_agent.py").is_file() else "tests/run_agent/test_run_agent.py"): _native_main_tests,
        "tests/agent/test_auxiliary_client.py": _native_auxiliary_tests,
    }
    outputs = {target / name: fn((target / name).read_text()) for name, fn in updates.items()}
    outputs[target / "agent/llm_attempt_receipts.py"] = PAYLOAD.read_text()
    for path, source in outputs.items():
        compile(source, str(path), "exec")
    changed = False
    for path, source in outputs.items():
        if not path.exists() or path.read_text() != source:
            path.write_text(source)
            changed = True
    # The existing stop guards are applied by the entry point after source receipts.
    for name in ("open_todo_stop_guard_v1", "outcome_stop_guard_v1"):
        module = runpy.run_path(str(PAYLOAD.parent.parent / (name + ".py")))
        changed = module["patch_" + name](target) or changed
    return changed


def patch_llm_attempt_receipts_v1(hermes_dir: Path) -> bool:
    return _patch_native_receipts(Path(hermes_dir))
