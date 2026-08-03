#!/usr/bin/env python3
"""Install content-free terminal receipts around main and auxiliary LLM attempts."""

from __future__ import annotations

import runpy
import shutil
from pathlib import Path

MARKER = "HERMES_LLM_ATTEMPT_RECEIPTS_v1"
PAYLOAD = Path(__file__).resolve().parent.parent / "payloads/llm-attempt-receipts-v1/agent/llm_attempt_receipts.py"
TRUNCATED_RETRY_MARKER = "HERMES_TRUNCATED_TOOL_RETRY_CACHE_BUST_v1"
ITERATION_SUMMARY_MARKER = "HERMES_XAI_ITERATION_SUMMARY_TOOLLESS_v1"

TRUNCATED_RETRY_ANCHOR = """                                # Don't append the broken response to messages;
                                # just re-run the same API call from the current
                                # message state, giving the model another chance.
                                continue
"""

TRUNCATED_RETRY_REPLACEMENT = f"""                                # [{TRUNCATED_RETRY_MARKER}]
                                # Repeating the identical request can replay a
                                # provider-side cached broken completion forever,
                                # especially when the output cap is already at its
                                # ceiling. Append a bounded recovery instruction to
                                # both durable and wire messages so every retry has
                                # a fresh request identity and the next transcript
                                # remains byte-consistent with what the model saw.
                                _tool_retry_message = {{
                                    "role": "user",
                                    "content": (
                                        "The previous tool call was incomplete and was not "
                                        "executed. Retry the same intended action now, but "
                                        "keep the tool arguments concise and split large "
                                        "content or scripts into smaller tool calls. "
                                        f"Recovery attempt {{truncated_tool_call_retries}}/4."
                                    ),
                                }}
                                messages.append(_tool_retry_message)
                                if api_messages is not messages:
                                    api_messages.append(dict(_tool_retry_message))
                                agent._session_messages = messages
                                continue
"""

TRUNCATED_TOOL_TAIL_ANCHOR = """                            # Prior successful tool batches (or injected tool
                            # errors) can leave a tool-result tail; this path
                            # never reaches finalize_turn (#48879 class).
                            close_interrupted_tool_sequence(messages, _final_response)
"""

TRUNCATED_TOOL_TAIL_REPLACEMENT = """                            # A recovery instruction is a real user message.
                            # Close it with the safe assistant response before
                            # persisting so the next turn never starts from a
                            # dangling user tail.
                            if messages and messages[-1].get("role") == "user":
                                messages.append(
                                    {"role": "assistant", "content": _final_response}
                                )
                            # Prior successful tool batches (or injected tool
                            # errors) can leave a tool-result tail; this path
                            # never reaches finalize_turn (#48879 class).
                            close_interrupted_tool_sequence(messages, _final_response)
"""

TRUNCATED_TOOL_SAFE_MESSAGE = (
    "The action was truncated due to output length limit before it finished. "
    "I did not run the incomplete action. Please retry; I’ll continue in smaller steps."
)

TRUNCATED_TEST_CLASS_ANCHOR = "\n\nclass TestHookPayloadSanitizesSimpleNamespace:"

TRUNCATED_TEST_METHOD = f'''

    # [{TRUNCATED_RETRY_MARKER}] behavioral regression
    def test_truncated_tool_retry_changes_request_identity(self, agent):
        """A truncated tool call must not replay an identical cached request."""
        self._setup_agent(agent)
        agent.valid_tool_names.add("write_file")
        bad_tc = _mock_tool_call(
            name="write_file",
            arguments='{{"path":"report.md","content":"partial',
            call_id="c1",
        )
        good_tc = _mock_tool_call(
            name="write_file",
            arguments='{{"path":"report.md","content":"full content"}}',
            call_id="c2",
        )
        responses = iter(
            [
                _mock_response(content="", finish_reason="length", tool_calls=[bad_tc]),
                _mock_response(content="", finish_reason="stop", tool_calls=[good_tc]),
                _mock_response(content="Done!", finish_reason="stop"),
            ]
        )
        seen_messages = []

        def _create(**kwargs):
            seen_messages.append(json.loads(json.dumps(kwargs["messages"])))
            return next(responses)

        agent.client.chat.completions.create.side_effect = _create
        with (
            patch("run_agent.handle_function_call", return_value='{{"success":true}}') as mock_hfc,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("write the report")

        assert result["final_response"] == "Done!"
        assert len(seen_messages) == 3
        assert seen_messages[0] != seen_messages[1]
        assert seen_messages[1][-1]["role"] == "user"
        assert "Recovery attempt 1/4" in seen_messages[1][-1]["content"]
        assert "smaller tool calls" in seen_messages[1][-1]["content"]
        mock_hfc.assert_called_once()
'''


class PatchError(RuntimeError):
    pass


def _replace_once(content: str, old: str, new: str, label: str) -> str:
    if content.count(old) != 1:
        raise PatchError(f"required unique anchor missing: {label}")
    return content.replace(old, new, 1)


def _patch_truncated_tool_retry(content: str) -> str:
    if TRUNCATED_RETRY_MARKER in content:
        return content
    content = _replace_once(
        content,
        TRUNCATED_RETRY_ANCHOR,
        TRUNCATED_RETRY_REPLACEMENT,
        "truncated tool retry",
    )
    content = _replace_once(
        content,
        TRUNCATED_TOOL_TAIL_ANCHOR,
        TRUNCATED_TOOL_TAIL_REPLACEMENT,
        "truncated tool exhaustion tail",
    )
    content = _replace_once(
        content,
        'else "Response truncated due to output length limit"',
        f"else {TRUNCATED_TOOL_SAFE_MESSAGE!r}",
        "truncated plain-text response",
    )
    content = _replace_once(
        content,
        '"final_response": "Response truncated due to output length limit",',
        f'"final_response": {TRUNCATED_TOOL_SAFE_MESSAGE!r},',
        "truncated tool response",
    )
    content = _replace_once(
        content,
        '"final_response": "First response truncated due to output length limit",',
        f'"final_response": {TRUNCATED_TOOL_SAFE_MESSAGE!r},',
        "first truncated tool response",
    )
    content = _replace_once(
        content,
        '_final_response = "Response truncated due to output length limit"',
        f"_final_response = {TRUNCATED_TOOL_SAFE_MESSAGE!r}",
        "truncated fallback response",
    )
    return content


def _patch_truncated_tool_test(content: str) -> str:
    if f"# [{TRUNCATED_RETRY_MARKER}] behavioral regression" in content:
        return content
    return _replace_once(
        content,
        TRUNCATED_TEST_CLASS_ANCHOR,
        TRUNCATED_TEST_METHOD + TRUNCATED_TEST_CLASS_ANCHOR,
        "truncated retry regression test",
    )


def _patch_main_loop(content: str) -> str:
    if MARKER in content:
        return _patch_truncated_tool_retry(content)
    old_template = """                def _perform_api_call(next_api_kwargs):
                    if agent.api_mode == "codex_responses":
                        next_api_kwargs = agent._get_transport().preflight_kwargs(
                            next_api_kwargs,
                            allow_stream=False,
                            is_github_responses={copilot_expression},
                        )
                    if _use_streaming:
                        return agent._interruptible_streaming_api_call(
                            next_api_kwargs, on_first_delta=_stop_spinner
                        )
                    return agent._interruptible_api_call(next_api_kwargs)
"""
    old_variants = (
        old_template.format(copilot_expression="agent._is_copilot_url()"),
        old_template.format(copilot_expression=('bool(getattr(agent, "_is_copilot_url", lambda: False)())')),
        """                def _perform_api_call(next_api_kwargs):
                    if agent.api_mode == "codex_responses":
                        next_api_kwargs = agent._get_transport().preflight_kwargs(
                            next_api_kwargs,
                            allow_stream=False,
                            is_github_responses=agent._is_copilot_url(),
                        )
                    if _use_streaming:
                        return agent._interruptible_streaming_api_call(
                            next_api_kwargs, on_first_delta=_stop_spinner
                        )
                    from agent import relay_llm

                    return relay_llm.execute(
                        next_api_kwargs,
                        agent._interruptible_api_call,
                        session_id=str(agent.session_id or ""),
                        name=str(agent.provider or "provider"),
                        model_name=str(agent.model or ""),
                        metadata={
                            "api_mode": agent.api_mode,
                            "api_request_id": api_request_id,
                            "call_role": (
                                "delegated"
                                if getattr(agent, "is_subagent", False)
                                else "fallback"
                                if int(getattr(agent, "_fallback_index", 0) or 0) > 0
                                else "primary"
                            ),
                            "retry_count": retry_count,
                        },
                        defer_logical_completion=True,
                    )
""",
        """                def _perform_api_call(next_api_kwargs):
                    if agent.api_mode == "codex_responses":
                        next_api_kwargs = agent._get_transport().preflight_kwargs(
                            next_api_kwargs,
                            allow_stream=False,
                            is_github_responses=agent._is_copilot_url(),
                            sanitize_harmony_tokens=agent._is_codex_backend(),
                        )
                    if _use_streaming:
                        return agent._interruptible_streaming_api_call(
                            next_api_kwargs, on_first_delta=_stop_spinner
                        )
                    from agent import relay_llm

                    return relay_llm.execute(
                        next_api_kwargs,
                        agent._interruptible_api_call,
                        session_id=str(agent.session_id or ""),
                        name=str(agent.provider or "provider"),
                        model_name=str(agent.model or ""),
                        metadata={
                            "api_mode": agent.api_mode,
                            "api_request_id": api_request_id,
                            "call_role": (
                                "delegated"
                                if getattr(agent, "is_subagent", False)
                                else "fallback"
                                if int(getattr(agent, "_fallback_index", 0) or 0) > 0
                                else "primary"
                            ),
                            "retry_count": retry_count,
                        },
                        defer_logical_completion=True,
                    )
""",
    )
    new = """                def _perform_api_call(next_api_kwargs):
                    # HERMES_LLM_ATTEMPT_RECEIPTS_v1: this callback is invoked
                    # once per physical provider attempt, including each retry
                    # and each provider fallback selected by the outer loop.
                    if agent.api_mode == "codex_responses":
                        next_api_kwargs = agent._get_transport().preflight_kwargs(
                            next_api_kwargs,
                            allow_stream=False,
                            is_github_responses=bool(
                                getattr(
                                    agent,
                                    "_is_copilot_url",
                                    lambda: False,
                                )()
                            ),
                        )

                    def _raw_provider_call():
                        if _use_streaming:
                            return agent._interruptible_streaming_api_call(
                                next_api_kwargs, on_first_delta=_stop_spinner
                            )
                        # Preserve Hermes's native Relay seam when present.
                        # The local receipt remains the always-on content-free
                        # accounting record; Relay may be disabled at runtime.
                        try:
                            from agent import relay_llm
                        except ImportError:
                            return agent._interruptible_api_call(next_api_kwargs)
                        return relay_llm.execute(
                            next_api_kwargs,
                            agent._interruptible_api_call,
                            session_id=str(agent.session_id or ""),
                            name=str(agent.provider or "provider"),
                            model_name=str(agent.model or ""),
                            metadata={
                                "api_mode": agent.api_mode,
                                "api_request_id": api_request_id,
                                "call_role": (
                                    "delegated"
                                    if getattr(agent, "is_subagent", False)
                                    else "fallback"
                                    if int(getattr(agent, "_fallback_index", 0) or 0) > 0
                                    else "primary"
                                ),
                                "retry_count": retry_count,
                            },
                            defer_logical_completion=True,
                        )

                    from agent.llm_attempt_receipts import execute_main_attempt
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
                        defer_success=True,
                    )
"""
    matches = [old for old in old_variants if content.count(old) == 1]
    if len(matches) != 1:
        raise PatchError("required unique anchor missing: main physical provider call")
    content = content.replace(matches[0], new, 1)
    redirect_anchor = "                if _redirect_crossed_response:\n"
    redirect_new = """                if _redirect_crossed_response:
                    from agent.llm_attempt_receipts import finish_main_attempt
                    finish_main_attempt(
                        api_request_id,
                        "cancelled",
                        response=response,
                        error=RuntimeError("response discarded after redirect"),
                    )
"""
    content = _replace_once(
        content,
        redirect_anchor,
        redirect_new,
        "redirect-discarded response terminal",
    )
    validation_anchor = """                if response_invalid:
                    agent._invoke_api_request_error_hook(
"""
    validation_new = """                # HERMES_LLM_ATTEMPT_RECEIPTS_v1: a provider response is
                # terminal-success only after the same transport validation
                # used by the retry/fallback loop. Malformed HTTP-200 responses
                # are terminal errors, never successful spend.
                from agent.llm_attempt_receipts import finish_main_attempt
                if response_invalid:
                    finish_main_attempt(
                        api_request_id,
                        "error",
                        response=response,
                        error=RuntimeError(
                            ", ".join(error_details) or "Invalid API response"
                        ),
                    )
                else:
                    finish_main_attempt(
                        api_request_id,
                        "success",
                        response=response,
                    )

                if response_invalid:
                    agent._invoke_api_request_error_hook(
"""
    content = _replace_once(
        content,
        validation_anchor,
        validation_new,
        "main validated response terminal",
    )
    return _patch_truncated_tool_retry(content)


def _patch_fallback_cause(content: str) -> str:
    if "_active_fallback_cause" not in content:
        old = """        agent._fallback_activated = True

        # Rebind the credential pool to the fallback provider when the provider
"""
        new = """        agent._fallback_activated = True
        # HERMES_LLM_ATTEMPT_RECEIPTS_v1: retain the classified cause on the
        # selected route so the next physical attempt is not inferred from key
        # identity or provider spend.
        agent._active_fallback_cause = (
            getattr(reason, "value", None)
            or (str(reason) if reason is not None else "unclassified_failure")
        )

        # Rebind the credential pool to the fallback provider when the provider
"""
        content = _replace_once(content, old, new, "fallback activation cause")
    return _patch_iteration_summary(content)


def _patch_iteration_summary(content: str) -> str:
    if ITERATION_SUMMARY_MARKER in content:
        return content

    toolless_anchor = """            codex_kwargs = agent._build_api_kwargs(api_messages)
            codex_kwargs.pop("tools", None)
"""
    toolless_replacement = f"""            codex_kwargs = agent._build_api_kwargs(api_messages)
            # {ITERATION_SUMMARY_MARKER}: these fields are one
            # contract. Leaving tool_choice behind after removing tools makes
            # strict Responses providers reject the fallback summary request.
            for tool_only_key in ("tools", "tool_choice", "parallel_tool_calls"):
                codex_kwargs.pop(tool_only_key, None)
"""
    content = _replace_once(
        content,
        toolless_anchor,
        toolless_replacement,
        "initial toolless iteration summary",
    )
    retry_anchor = """                codex_kwargs = agent._build_api_kwargs(api_messages)
                codex_kwargs.pop("tools", None)
"""
    retry_replacement = """                codex_kwargs = agent._build_api_kwargs(api_messages)
                for tool_only_key in ("tools", "tool_choice", "parallel_tool_calls"):
                    codex_kwargs.pop(tool_only_key, None)
"""
    content = _replace_once(
        content,
        retry_anchor,
        retry_replacement,
        "retry toolless iteration summary",
    )

    raw_error_anchor = (
        '        final_response = f"I reached the maximum iterations '
        "({agent.max_iterations}) but couldn't summarize. Error: {str(e)}\"\n"
    )
    safe_error_replacement = """        final_response = (
            "I reached the iteration limit before I could finish. "
            "Send “continue” and I’ll pick up from the preserved conversation."
        )
"""
    return _replace_once(
        content,
        raw_error_anchor,
        safe_error_replacement,
        "iteration summary safe failure",
    )


def _patch_iteration_status(content: str) -> str:
    status_anchor = """        agent._emit_status(
            f"⚠️ Iteration budget exhausted ({api_call_count}/{agent.max_iterations}) "
            "— asking model to summarise"
        )
"""
    status_replacement = """        logger.info(
            "Iteration budget exhausted (%d/%d); requesting toolless summary",
            api_call_count,
            agent.max_iterations,
        )
"""
    if status_replacement in content:
        return content
    return _replace_once(
        content,
        status_anchor,
        status_replacement,
        "iteration summary transcript status",
    )


def _patch_iteration_tests(content: str) -> str:
    old_name = "    def test_api_failure_returns_error(self, agent):\n"
    new_name = "    def test_api_failure_returns_safe_continuation_message(self, agent):\n"
    if new_name not in content:
        content = _replace_once(
            content,
            old_name,
            new_name,
            "iteration summary safe-failure test name",
        )
    old_assertions = (
        '        assert "error" in result.lower()\n'
        '        assert "API down" in result\n'
    )
    new_assertions = (
        '        assert "continue" in result.lower()\n'
        '        assert "API down" not in result\n'
    )
    if new_assertions not in content:
        content = _replace_once(
            content,
            old_assertions,
            new_assertions,
            "iteration summary safe-failure assertions",
        )
    codex_assert_anchor = """        assert result == "Summary"
        input_items = captured["input"]
"""
    codex_assert_replacement = """        assert result == "Summary"
        assert "tools" not in captured
        assert "tool_choice" not in captured
        assert "parallel_tool_calls" not in captured
        input_items = captured["input"]
"""
    if codex_assert_replacement not in content:
        content = _replace_once(
            content,
            codex_assert_anchor,
            codex_assert_replacement,
            "iteration summary request regression",
        )
    return content


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


def _sync_wrapper() -> str:
    return r"""

# HERMES_LLM_ATTEMPT_RECEIPTS_v1: establish one logical chain around every
# physical auxiliary request, including retries, provider switches, and streams.
_call_llm_without_attempt_receipts = call_llm


def call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = None,
    extra_body: dict = None,
    reasoning_config: Optional[dict] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    api_mode: str = None,
    stream: bool = False,
    stream_options: dict = None,
) -> Any:
    from agent.aux_accounting import get_accounting_context
    from agent.llm_attempt_receipts import auxiliary_request

    accounting = get_accounting_context()
    session_id = str(accounting[1]) if accounting else ""
    with auxiliary_request(
        task=task,
        provider=provider,
        model=model,
        session_id=session_id,
    ):
        return _call_llm_without_attempt_receipts(
            task,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            main_runtime=main_runtime,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            timeout=timeout,
            extra_body=extra_body,
            reasoning_config=reasoning_config,
            extra_headers=extra_headers,
            api_mode=api_mode,
            stream=stream,
            stream_options=stream_options,
        )
"""


def _async_wrapper() -> str:
    return r"""


# HERMES_LLM_ATTEMPT_RECEIPTS_v1: async parity for auxiliary calls.
_async_call_llm_without_attempt_receipts = async_call_llm


async def async_call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = None,
    extra_body: dict = None,
    reasoning_config: Optional[dict] = None,
) -> Any:
    from agent.aux_accounting import get_accounting_context
    from agent.llm_attempt_receipts import auxiliary_request

    accounting = get_accounting_context()
    session_id = str(accounting[1]) if accounting else ""
    with auxiliary_request(
        task=task,
        provider=provider,
        model=model,
        session_id=session_id,
    ):
        return await _async_call_llm_without_attempt_receipts(
            task,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            main_runtime=main_runtime,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            timeout=timeout,
            extra_body=extra_body,
            reasoning_config=reasoning_config,
        )
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


def _patch_auxiliary(content: str) -> str:
    if MARKER not in content:
        public_anchor = "\n\n# ── Public API ──────────────────────────────────────────────────────────────\n"
        content = _replace_once(
            content,
            public_anchor,
            _resolver_wrapper() + public_anchor,
            "auxiliary resolver boundary",
        )
        extract_anchor = "\n\ndef extract_content_or_reasoning(response) -> str:\n"
        content = _replace_once(
            content,
            extract_anchor,
            _sync_wrapper() + extract_anchor,
            "sync auxiliary call boundary",
        )
        content = content.rstrip() + _async_wrapper().rstrip() + "\n"
    if "_resolve_vision_provider_client_without_attempt_receipts" not in content:
        content = content.rstrip() + _vision_resolver_wrapper().rstrip() + "\n"
    if "HERMES_LLM_ATTEMPT_RESOLVED_CLIENTS_v1" in content:
        return content
    resolved_client_anchor = "    effective_timeout = _effective_aux_timeout(task, timeout)\n"
    resolved_client_new = (
        """    # HERMES_LLM_ATTEMPT_RESOLVED_CLIENTS_v1:
    # Some vision and provider-specific routes
    # construct clients without resolve_provider_client. Instrument the final
    # concrete client at the common execution boundary as well, so those calls
    # cannot bypass physical-attempt receipts.
    from agent.llm_attempt_receipts import instrument_auxiliary_client
    client = instrument_auxiliary_client(
        client,
        provider=resolved_provider,
        model=final_model,
        task=task,
        api_mode=resolved_api_mode,
    )

"""
        + resolved_client_anchor
    )
    if content.count(resolved_client_anchor) != 2:
        raise PatchError("required exact anchors missing: resolved auxiliary clients")
    content = content.replace(resolved_client_anchor, resolved_client_new)
    return content


def patch_llm_attempt_receipts_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir)
    helper = target / "agent/llm_attempt_receipts.py"
    main_loop = target / "agent/conversation_loop.py"
    fallback = target / "agent/chat_completion_helpers.py"
    turn_finalizer = target / "agent/turn_finalizer.py"
    auxiliary = target / "agent/auxiliary_client.py"
    run_agent_test = target / "tests/run_agent/test_run_agent.py"
    for path in (
        main_loop,
        fallback,
        turn_finalizer,
        auxiliary,
        run_agent_test,
        PAYLOAD,
    ):
        if not path.exists():
            raise PatchError(f"required file missing: {path}")

    changed = False
    helper_source = PAYLOAD.read_text(encoding="utf-8")
    if not helper.exists() or helper.read_text(encoding="utf-8") != helper_source:
        helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PAYLOAD, helper)
        changed = True

    updates = (
        (main_loop, _patch_main_loop),
        (fallback, _patch_fallback_cause),
        (turn_finalizer, _patch_iteration_status),
        (auxiliary, _patch_auxiliary),
        (
            run_agent_test,
            lambda content: _patch_iteration_tests(
                _patch_truncated_tool_test(content)
            ),
        ),
    )
    for path, transform in updates:
        before = path.read_text(encoding="utf-8")
        after = transform(before)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed = True

    # This registry entry already owns the main LLM turn and finalization
    # surfaces. Keep the language-neutral current-turn todo stop guard in the
                                # same finalization owner instead of creating a competing stop-decision path.
    open_todo_module = runpy.run_path(
        str(PAYLOAD.parent.parent / "open_todo_stop_guard_v1.py")
    )
    changed = open_todo_module["patch_open_todo_stop_guard_v1"](target) or changed
    return changed
