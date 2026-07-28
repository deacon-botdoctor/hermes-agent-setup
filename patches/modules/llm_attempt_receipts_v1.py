#!/usr/bin/env python3
"""Install content-free terminal receipts around main and auxiliary LLM attempts."""

from __future__ import annotations

import shutil
from pathlib import Path

MARKER = "HERMES_LLM_ATTEMPT_RECEIPTS_v1"
PAYLOAD = Path(__file__).resolve().parent.parent / "payloads/llm-attempt-receipts-v1/agent/llm_attempt_receipts.py"


class PatchError(RuntimeError):
    pass


def _replace_once(content: str, old: str, new: str, label: str) -> str:
    if content.count(old) != 1:
        raise PatchError(f"required unique anchor missing: {label}")
    return content.replace(old, new, 1)


def _patch_main_loop(content: str) -> str:
    if MARKER in content:
        return content
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
                        return agent._interruptible_api_call(next_api_kwargs)

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
    return _replace_once(
        content,
        validation_anchor,
        validation_new,
        "main validated response terminal",
    )


def _patch_fallback_cause(content: str) -> str:
    if "_active_fallback_cause" in content:
        return content
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
    return _replace_once(content, old, new, "fallback activation cause")


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
    auxiliary = target / "agent/auxiliary_client.py"
    for path in (main_loop, fallback, auxiliary, PAYLOAD):
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
        (auxiliary, _patch_auxiliary),
    )
    for path, transform in updates:
        before = path.read_text(encoding="utf-8")
        after = transform(before)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed = True
    return changed
