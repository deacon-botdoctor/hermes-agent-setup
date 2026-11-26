"""Versioned conditional direct-API admission, owned by native FIFO and controls."""
from pathlib import Path
import ast

MARKER = 'HERMES_CONDITIONAL_API_ADMISSION_v1'


def _replace(source, old, new, path):
    if source.count(old) != 1:
        raise RuntimeError(f'Conditional API anchor drift in {path}: {old[:90]!r}')
    return source.replace(old, new, 1)


def patch_conditional_api_admission_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    payload = (Path(__file__).parent.parent / 'payloads/conditional-api-admission-v1/session_conditional_api.py').read_text()
    policy_payload = (Path(__file__).parent.parent / 'payloads/conditional-api-admission-v1/conditional_snapshot_policy.py').read_text()
    auth_payload = (Path(__file__).parent.parent / 'payloads/conditional-api-admission-v1/auth_codex_snapshot.py').read_text()
    targets = ('gateway/session_api_turn.py', 'gateway/session_authority.py', 'hermes_state_runtime.py',
               'hermes_state_mutations.py', 'hermes_cli/plugins.py', 'gateway/platforms/api_server_runs.py',
               'gateway/platforms/api_server.py', 'gateway/run_inbound.py', 'gateway/run_startup.py',
               'gateway/session_mutations.py', 'hermes_cli/plugin_isolation.py', 'gateway/session_api.py',
               'gateway/session_execution.py', 'agent/agent_init.py', 'agent/tool_executor.py',
               'agent/agent_runtime_helpers.py', 'agent/turn_context.py', 'agent/turn_request_assembly.py',
               'model_tools.py', 'agent/turn_api_request.py', 'agent/turn_api_call.py', 'agent/relay_llm.py',
               'gateway/run.py', 'agent/client_lifecycle.py', 'agent/conversation_loop.py',
               'agent/codex_runtime.py', 'agent/turn_api_error.py', 'agent/turn_response_check.py')
    originals = {name: (root / name).read_text() for name in targets}
    destination = root / 'gateway/session_conditional_api.py'
    policy_destination = root / 'agent/conditional_snapshot_policy.py'
    auth_destination = root / 'hermes_cli/auth_codex_snapshot.py'
    markers = [MARKER in source for source in originals.values()]
    if any(markers) or destination.exists() or policy_destination.exists() or auth_destination.exists():
        if (all(markers) and destination.exists() and destination.read_text() == payload
                and policy_destination.exists() and policy_destination.read_text() == policy_payload
                and auth_destination.exists() and auth_destination.read_text() == auth_payload):
            for name, source in originals.items():
                ast.parse(source, filename=name)
            return False
        raise RuntimeError('Partial or conflicting conditional API carrier')
    updates = dict(originals)

    def edit(name, old, new):
        updates[name] = _replace(updates[name], old, new, name)

    name = 'hermes_cli/plugins.py'
    edit(name, '    def register_approval_transport(self, name: str, present_fn: Callable) -> None:\n',
         '    def register_conditional_api_provider(self, provider):\n'
         '        """HERMES_CONDITIONAL_API_ADMISSION_v1: loader-bound, opt-in trusted provider."""\n'
         '        from gateway.session_conditional_api import register_provider\n'
         '        return register_provider(self, provider)\n\n'
         '    def register_approval_transport(self, name: str, present_fn: Callable) -> None:\n')
    name = 'hermes_cli/plugin_isolation.py'
    edit(name, 'HOST_UNSUPPORTED_CTX_METHODS: dict[str, str] = {\n',
         'HOST_UNSUPPORTED_CTX_METHODS: dict[str, str] = {\n'
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: grant handles stay in the native owner process.\n'
         '    "register_conditional_api_provider": "conditional API grants require the native authority and owner loop",\n')

    name = 'gateway/session_api_turn.py'
    edit(name, '    return run_steps(admit_api_steps(adapter, **kwargs))\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: remote readers require the async door.\n'
         '    if kwargs.get("conditional_api") is not None:\n'
         '        raise RuntimeStoreError("permission_denied", field="conditional", detail="async admission required")\n'
         '    return run_steps(admit_api_steps(adapter, **kwargs))\n')
    edit(name, '    return await run_steps_off_loop(authority, steps or admit_api_steps(adapter, **kwargs), orphaned=schedule_orphan)\n',
         '    from gateway.session_conditional_api import prepare_enqueue, EnqueueObservationCancelled\n'
         '    if steps is not None and kwargs.get("conditional_api") is not None:\n'
         '        raise RuntimeStoreError("invalid_params")\n'
         '    try:\n'
         '        kwargs = await prepare_enqueue(authority, kwargs)\n'
         '    except asyncio.CancelledError as exc:\n'
         '        if kwargs.get("conditional_api") is None:\n'
         '            raise\n'
         '        # No native write can precede the read-only observation. Let the HTTP\n'
         '        # owner forget its reservation, then propagate the original cancellation.\n'
         '        raise EnqueueObservationCancelled(exc) from exc\n'
         '    return await run_steps_off_loop(authority, steps or admit_api_steps(adapter, **kwargs), orphaned=schedule_orphan)\n')
    edit(name, '    from hermes_state_terminal import retry_terminal_admission\n    row = retry_terminal_admission',
         '    from gateway.session_conditional_api import enqueue_guard, validate_payload\n'
         '    if kwargs.get("conditional_api") is not None:\n'
         '        payload["conditional_api_v1"] = kwargs["conditional_api"]\n'
         '        validate_payload(authority, sid, payload)\n'
         '    from hermes_state_terminal import retry_terminal_admission\n    row = retry_terminal_admission')
    edit(name, "    row = yield partial(admit_session_input, authority.db, epoch=authority.epoch, principal_id='api',\n"
               "                        session_id=sid, request_id=request_id, payload=payload,\n"
               "                        _authorize_write=authority._admission_gate(partial(\n"
               "                            _admit_target, sid, request_id, payload['api_turn_v1']['settings'],\n"
               "                            client_key=bool(kwargs.get('request_id')))))\n",
         "    target_guard = partial(_admit_target, sid, request_id, payload['api_turn_v1']['settings'],\n"
         "                           client_key=bool(kwargs.get('request_id')))\n"
         "    if 'conditional_api_v1' in payload:\n"
         "        target_guard = enqueue_guard(authority, sid, request_id, payload,\n"
         "                                     kwargs.get('_conditional_attestation'), target_guard)\n"
         "    row = yield partial(admit_session_input, authority.db, epoch=authority.epoch, principal_id='api',\n"
         "                        session_id=sid, request_id=request_id, payload=payload,\n"
         "                        _authorize_write=authority._admission_gate(target_guard))\n")

    name = 'hermes_state_runtime.py'
    edit(name, "        admission_id = uuid.uuid4().hex\n",
         "        # HERMES_CONDITIONAL_API_ADMISSION_v1: exact replays returned above.\n"
         "        from gateway.session_conditional_api import payload_guard, advance_input\n"
         "        conditional = payload_guard(payload)\n"
         "        if conditional is None:\n"
         "            advance_input(conn, session_id)\n"
         "        admission_id = uuid.uuid4().hex\n")
    edit(name, "            (admission_id, request_id, principal_id, session_id, json.dumps([session_id]), encoded, digest, intent, epoch))\n"
               "        return _row(_admission(conn, admission_id))\n",
         "            (admission_id, request_id, principal_id, session_id, json.dumps([session_id]), encoded, digest, intent, epoch))\n"
         "        if conditional is not None:\n"
         "            from gateway.session_conditional_api import bind_effect_receipt\n"
         "            bind_effect_receipt(conn, payload, admission_id)\n"
         "        return _row(_admission(conn, admission_id))\n")
    edit(name, "        if row['status'] == 'queued':\n            conn.execute(_CANCEL_QUEUED, (admission_id,))\n",
         "        if row['status'] == 'queued':\n"
         "            from gateway.session_conditional_api import advance_control\n"
         "            advance_control(conn, row['target_session_id'], 'cancel:' + admission_id)\n"
         "            conn.execute(_CANCEL_QUEUED, (admission_id,))\n")
    # A guarded row may not enter the queued-steer path, which bypasses claim preflight.
    edit(name, "        if row['status'] != 'queued':\n            raise RuntimeStoreError('admission_started')\n",
         "        if row['status'] != 'queued':\n            raise RuntimeStoreError('admission_started')\n"
         "        from gateway.session_conditional_api import payload_guard\n"
         "        if payload_guard(json.loads(row['payload_json'])) is not None:\n"
         "            raise RuntimeStoreError('permission_denied', field='conditional', detail='conditional input cannot be steered')\n")
    # Adoption cannot resurrect a conditional unknown execution from an older authority epoch.
    edit(name, "        if linked is not None:\n            changed = conn.execute(\"\"\"UPDATE session_admissions SET owner_epoch=?,status='started'\n",
         "        if linked is not None:\n"
         "            from gateway.session_conditional_api import payload_guard\n"
         "            if payload_guard(json.loads(linked['payload_json'])) is not None and linked['owner_epoch'] != epoch:\n"
         "                raise RuntimeStoreError('unknown_execution')\n"
         "            changed = conn.execute(\"\"\"UPDATE session_admissions SET owner_epoch=?,status='started'\n")

    name = 'hermes_state_mutations.py'
    edit(name, 'def apply_action(db, conn, session_id, operation, payload, *, prepared=None):\n',
         'def apply_action(db, conn, session_id, operation, payload, *, prepared=None):\n'
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: successful explicit changes revoke v1 baselines.\n'
         '    if operation not in {"rename", "archive", "sidebar", "import", "branch"}:\n'
         '        from gateway.session_conditional_api import advance_control\n'
         '        advance_control(conn, session_id, "mutation:" + operation + ":" + str(\n'
         '            conn.execute("SELECT runtime_revision FROM sessions WHERE id=?", (session_id,)).fetchone()[0]))\n')

    name = 'gateway/session_authority.py'
    edit(name, "        if handle.execution_generation != generation:\n            raise RuntimeStoreError('stale_generation')\n",
         "        if handle.execution_generation != generation:\n            raise RuntimeStoreError('stale_generation')\n"
         "        # HERMES_CONDITIONAL_API_ADMISSION_v1: idle Stop also fences pending effects.\n"
         "        from gateway.session_conditional_api import fence_interrupt\n"
         "        await fence_interrupt(self, ref, actor, generation)\n"
         "        # The fence write yielded: do not deliver this Stop to a successor generation.\n"
         "        handle = self._handle(ref)\n"
         "        if handle.execution_generation != generation:\n"
         "            raise RuntimeStoreError('stale_generation')\n")
    edit(name, '    async def _claim_next(self, ref, live):\n',
         '    async def _claim_next(self, ref, live):\n'
         '        from gateway.session_conditional_api import preclaim\n'
         '        observed, attestation = await preclaim(self, ref)\n'
         '        try:\n'
         '            return await self._claim_next_observed(ref, live, observed, attestation)\n'
         '        except RuntimeStoreError as exc:\n'
         '            if observed is not None and any(k.startswith("conditional_api") for k in observed["payload"]):\n'
         '                await self._refuse_head(ref, observed["admission_id"], exc.reason)\n'
         '                return False, observed\n'
         '            raise\n\n'
         '    async def _claim_next_observed(self, ref, live, observed, attestation):\n')
    edit(name, "            first = next((row for row in pending if row['status'] == 'queued'), None)\n",
         "            first = next((row for row in pending if row['status'] == 'queued'), None)\n"
         "            if (first['admission_id'] if first else None) != (observed['admission_id'] if observed else None):\n"
         "                return False, first\n")
    edit(name, "            row = await tracked_write(self, partial(claim_session_input, self.db, epoch=self.epoch,\n",
         "            from gateway.session_conditional_api import start_guard\n"
         "            row = await tracked_write(self, partial(claim_session_input, self.db, epoch=self.epoch,\n")
    edit(name, "                                                    session_id=ref.session_id, _guard=self._admission_gate(),\n",
         "                                                    session_id=ref.session_id, _guard=self._admission_gate(partial(\n"
         "                                                        start_guard, self, first, attestation)),\n")

    name = 'gateway/run_inbound.py'
    edit(name, '        _active_session_lease, _limit_message = self._claim_active_session_slot(_quick_key, source)\n',
         '        # HERMES_CONDITIONAL_API_ADMISSION_v1: no classic writer on an enrolled API lane.\n'
         '        from gateway.session_conditional_api import refuse_classic_api_lane\n'
         '        refuse_classic_api_lane(self, _quick_key)\n'
         '        _active_session_lease, _limit_message = self._claim_active_session_slot(_quick_key, source)\n')
    name = 'gateway/run_startup.py'
    edit(name, '            # Epoch math: the marker was stamped naive-local by the previous process, possibly\n',
         '            # HERMES_CONDITIONAL_API_ADMISSION_v1: no synthetic restart of a dedicated effect lane.\n'
         '            from gateway.session_conditional_api import classic_api_lane_enrolled\n'
         '            if any(classic_api_lane_enrolled(authority, entry.session_key) for authority in all_authorities(self)):\n'
         '                continue\n'
         '            # Epoch math: the marker was stamped naive-local by the previous process, possibly\n')
    name = 'gateway/session_mutations.py'
    edit(name, "    if operation == 'branch' and 'session:create' not in actor.capabilities:\n",
         "    # HERMES_CONDITIONAL_API_ADMISSION_v1: accepted reset/revoke intent fences even a busy refusal.\n"
         "    if operation in {'reset', 'delete', 'rewind'}:\n"
         "        from hermes_state_mutations import validate_action\n"
         "        validate_action(operation, params['payload'])\n"
         "        from gateway.session_conditional_api import fence_mutation\n"
         "        await fence_mutation(authority, owner or ref, actor, params)\n"
         "    if operation == 'branch' and 'session:create' not in actor.capabilities:\n")

    name = 'gateway/platforms/api_server_runs.py'
    edit(name, '        return store_error_response(_openai_error, exc)\n    return None\n',
         '        from gateway.session_conditional_api import EnqueueObservationCancelled\n'
         '        if isinstance(exc, EnqueueObservationCancelled):\n'
         '            raise exc.cancellation\n'
         '        return store_error_response(_openai_error, exc)\n    return None\n')
    edit(name, '    _openai_error = _api_server._openai_error\n    # Long-term memory scope header',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: never run conditional JSON through legacy/bot-chat fallback.\n'
         '    _openai_error = _api_server._openai_error\n    # Long-term memory scope header')
    edit(name, '    raw_input = body.get("input")\n',
         '    from gateway.session_conditional_api import request_descriptor\n'
         '    from gateway.platforms.api_server_authority_runs import store_error_response\n'
         '    from hermes_state_runtime import RuntimeStoreError\n'
         '    try:\n'
         '        conditional = request_descriptor(body, idempotency_key, gateway_session_key)\n'
         '    except RuntimeStoreError as exc:\n'
         '        return store_error_response(_openai_error, exc)\n'
         '    raw_input = body.get("input")\n')
    # Exact HTTP replay above readiness returns original run, including unknown/terminal.
    edit(name, '    # Enforce concurrency only for a genuinely new run.\n',
         '    if conditional is not None:\n'
         '        from gateway.session_authorities import active_authority\n'
         '        if active_authority(self.gateway_runner) is None:\n'
         '            return store_error_response(_openai_error, RuntimeStoreError("permission_denied"))\n'
         '    # Enforce concurrency only for a genuinely new run.\n')
    edit(name, '    if not conversation_history and selected_session_id and not previous_response_id:\n',
         '    if conditional is None and not conversation_history and selected_session_id and not previous_response_id:\n')
    edit(name, '    session_history_delivery = not previous_response_id and not conversation_history\n',
         '    session_history_delivery = conditional is None and not previous_response_id and not conversation_history\n')
    edit(name, '    # A canonical Bot Chat that a Desktop holds live is that Desktop\'s to run:',
         '    if conditional is not None:\n'
         '        launch.agent_kwargs["conditional_api"] = conditional\n'
         '    # A canonical Bot Chat that a Desktop holds live is that Desktop\'s to run:')
    edit(name, '    admitted = await self._admit_to_live_bot_chat(session_id, user_message, turn_author) if selected_session_id else None\n',
         '    admitted = await self._admit_to_live_bot_chat(session_id, user_message, turn_author) if selected_session_id and conditional is None else None\n')

    name = 'gateway/platforms/api_server.py'
    edit(name, '        features = {\n            "chat_completions": True,',
         '        # HERMES_CONDITIONAL_API_ADMISSION_v1: separate core support from provider readiness.\n'
         '        from gateway.session_conditional_api import capabilities as conditional_api_capabilities\n'
         '        features = {\n            "conditional_api": conditional_api_capabilities(self),\n            "chat_completions": True,')

    # HERMES_CONDITIONAL_API_ADMISSION_v1: protected acceptance is the only additional
    # native bind caller. Its validator runs IN the existing binding transaction.
    name = 'gateway/session_api.py'
    edit(name, "def bind_api_steps(authority, session_id, *, hosted_dispatch=None, declared_key=None, caller_key=None,\n                   walked_from=None):",
         "def bind_api_steps(authority, session_id, *, hosted_dispatch=None, declared_key=None, caller_key=None,\n                   walked_from=None, _authorize_write=None):")
    edit(name, "        authority._require_admission_open()  # a drain that began while this waited on the writer wins\n",
         "        authority._require_admission_open()  # a drain that began while this waited on the writer wins\n"
         "        # HERMES_CONDITIONAL_API_ADMISSION_v1: protected loader owner-grant reservation only.\n"
         "        if _authorize_write is not None:\n            _authorize_write(conn)\n")
    edit(name, '    """Only the authenticated API edge may reserve an API source; never public RPC."""',
         '    """Authenticated API edge reservation; protected owner-grant acceptance reuses bind steps. Never public RPC."""')

    name = 'gateway/session_api_turn.py'
    edit(name, "    try:\n        yield\n    finally:\n        if token is not None:\n",
         "    try:\n        from agent.conditional_snapshot_policy import snapshot_scope\n"
         "        with snapshot_scope(current.get('snapshot_execution') if current else None):\n            yield\n"
         "    finally:\n        if token is not None:\n")
    edit(name, "    return {'adapter': adapter, 'settings': settings, 'history': data['history'] if data else None,\n",
         "    from gateway.session_conditional_api import snapshot_execution\n"
         "    return {'adapter': adapter, 'settings': settings, 'snapshot_execution': snapshot_execution(authority, ref, payload),\n"
         "            'history': data['history'] if data else None,\n")

    name = 'gateway/session_execution.py'
    edit(name, "        skip_context_files = self._skip_context_files(platform_key)\n",
         "        # HERMES_CONDITIONAL_API_ADMISSION_v1: never reuse a broader/mutable cached agent.\n"
         "        from agent.conditional_snapshot_policy import current_snapshot\n"
         "        if current_snapshot() is not None:\n"
         "            return self._build_fresh_agent(turn_route, platform_key, None, 1, reasoning_config, pr, True), False\n"
         "        skip_context_files = self._skip_context_files(platform_key)\n")

    name = 'agent/agent_init.py'
    edit(name, "    _install_safe_stdio()\n\n    _params = locals()\n",
         "    # HERMES_CONDITIONAL_API_ADMISSION_v1: immutable per-admission restriction, not safe_worker_policy.\n"
         "    from agent.conditional_snapshot_policy import current_snapshot\n"
         "    agent._conditional_snapshot_execution = current_snapshot()\n"
         "    if agent._conditional_snapshot_execution is not None:\n"
         "        enabled_toolsets, disabled_toolsets = [], []\n"
         "        skip_context_files = skip_memory = skip_background_review = True\n"
         "        load_soul_identity = False\n"
         "        ephemeral_system_prompt, prefill_messages, memory_manager = None, [], None\n"
         "        fallback_model, max_iterations = {}, 1\n"
         "    _install_safe_stdio()\n\n    _params = locals()\n")
    edit(name, '    _engine_name = "compressor"\n',
         '    from agent.conditional_snapshot_policy import current_snapshot\n'
         '    if current_snapshot() is not None:\n        return None\n'
         '    _engine_name = "compressor"\n')
    edit(name, '    agent._context_engine_tool_names: set = set()\n',
         '    agent._context_engine_tool_names: set = set()\n'
         '    from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '    if snapshot_for_agent(agent) is not None:\n        return  # no engine tools OR session-start callbacks\n')
    edit(name, '    _snapshot_primary_runtime(agent)\n',
         '    from agent.conditional_snapshot_policy import finish_agent_init\n'
         '    finish_agent_init(agent)\n    _snapshot_primary_runtime(agent)\n')
    edit(name, '    _resolve_api_mode(agent, api_mode, provider_name, base_url)\n',
         '    _resolve_api_mode(agent, api_mode, provider_name, base_url)\n'
         '    from agent.conditional_snapshot_policy import check_runtime\n'
         '    check_runtime(agent)  # refuse agent-owned tools before any transport/client construction\n')
    edit(name, 'def _load_tools(agent, enabled_toolsets, disabled_toolsets):\n',
         'def _load_tools(agent, enabled_toolsets, disabled_toolsets):\n'
         '    from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '    if snapshot_for_agent(agent) is not None:\n'
         '        from tools.registry import registry\n'
         '        from toolsets import tool_selection_key\n'
         '        agent._tool_snapshot_generation, agent._delegation_backend = registry._generation, None\n'
         '        agent._configured_disabled_toolsets = agent.disabled_toolsets = []\n'
         '        agent._tool_snapshot_selection = tool_selection_key([], [])\n'
         '        agent.tools, agent.valid_tool_names, agent._kanban_worker_guidance = [], set(), ""\n'
         '        return\n')

    name = 'agent/tool_executor.py'
    edit(name, '    block_message, block_error_type = scope_block, "tool_scope_block"\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: outside fail-open hooks/middleware.\n'
         '    from agent.conditional_snapshot_policy import forbid_tools\n'
         '    forbid_tools(agent)\n    block_message, block_error_type = scope_block, "tool_scope_block"\n')
    name = 'agent/agent_runtime_helpers.py'
    edit(name, '    hook_ids = tool_hook_ids(agent, effective_task_id, tool_call_id)\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: covers direct inline/concurrent invocation.\n'
         '    from agent.conditional_snapshot_policy import forbid_tools\n'
         '    forbid_tools(agent)\n    hook_ids = tool_hook_ids(agent, effective_task_id, tool_call_id)\n')
    name = 'model_tools.py'
    edit(name, '    function_args = coerce_tool_args(function_name, function_args)\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: registry/connector bridges cannot widen a snapshot.\n'
         '    from agent.conditional_snapshot_policy import forbid_tools\n'
         '    forbid_tools()\n    function_args = coerce_tool_args(function_name, function_args)\n')

    name = 'agent/turn_context.py'
    edit(name, '    if getattr(agent, "_persist_disabled", False):\n        return ""\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: no pre-LLM mutable context additions.\n'
         '    from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '    if snapshot_for_agent(agent) is not None:\n        return ""\n'
         '    if getattr(agent, "_persist_disabled", False):\n        return ""\n')
    edit(name, '    _refresh_mcp_tools_between_turns(agent)\n',
         '    from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '    if snapshot_for_agent(agent) is None:\n        _refresh_mcp_tools_between_turns(agent)\n')
    edit(name, '    refresh_warm_prompt(agent, system_message, conversation_history)\n',
         '    if snapshot_for_agent(agent) is None:\n        refresh_warm_prompt(agent, system_message, conversation_history)\n')
    edit(name, '    apply_requested_tool_rebuild(agent)\n',
         '    if snapshot_for_agent(agent) is None:\n        apply_requested_tool_rebuild(agent)\n')
    edit(name, '    upgrade = getattr(agent, "_deferred_title_upgrade", None)\n',
         '    from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '    if snapshot_for_agent(agent) is not None:\n        return\n'
         '    upgrade = getattr(agent, "_deferred_title_upgrade", None)\n')
    edit(name, '    should_review_memory = _tick_memory_nudge(agent)\n',
         '    should_review_memory = False if snapshot_for_agent(agent) is not None else _tick_memory_nudge(agent)\n')
    edit(name, '    if not plugin_block_message:\n        _maybe_title_session_at_turn_start',
         '    if not plugin_block_message and snapshot_for_agent(agent) is None:\n        _maybe_title_session_at_turn_start')
    edit(name, '    ) if plugin_block_message else run_turn_start_compaction(\n',
         '    ) if plugin_block_message or snapshot_for_agent(agent) is not None else run_turn_start_compaction(\n')
    edit(name, '    stage_skills_index_note(agent, active_system_prompt, messages)\n'
               '    plugin_user_context = _merge_gateway_notes(\n'
               '        agent, messages, current_turn_user_idx, plugin_user_context\n    )\n',
         '    if snapshot_for_agent(agent) is None:\n'
         '        stage_skills_index_note(agent, active_system_prompt, messages)\n'
         '        plugin_user_context = _merge_gateway_notes(\n'
         '            agent, messages, current_turn_user_idx, plugin_user_context\n        )\n')

    name = 'agent/turn_request_assembly.py'
    edit(name, '    api_messages, effective_system = build_api_messages(\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: exact admitted context, no plugins/engines/prefills/MoA.\n'
         '    from agent.conditional_snapshot_policy import frozen_request\n'
         '    snapshot = frozen_request(agent)\n'
         '    if snapshot is not None:\n'
         '        tokens = estimate_messages_tokens_rough(snapshot)\n'
         '        return AssembledRequest("fallthrough", snapshot, [], None, None, tokens, tokens,\n'
         '                                sum(len(m["content"]) for m in snapshot))\n'
         '    api_messages, effective_system = build_api_messages(\n')

    name = 'agent/turn_api_request.py'
    edit(name, '    agent._reset_stream_delivery_tracking()\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: no request-mutating plugin middleware.\n'
         '    from agent.conditional_snapshot_policy import frozen_request\n'
         '    snapshot = frozen_request(agent)\n'
         '    if snapshot is not None:\n'
         '        agent._reset_stream_delivery_tracking()\n'
         '        agent._last_api_first_chunk_at = None\n'
         '        kwargs = agent._build_api_kwargs(snapshot, tools_for_api=[])\n'
         '        if kwargs.get("tools") or kwargs.get("extra_body", {}).get("tools"):\n'
         '            raise RuntimeError("snapshot-only native request attempted to add tools")\n'
         '        return ApiRequestBuild("fallthrough", snapshot, None, [], kwargs, dict(kwargs), [])\n'
         '    agent._reset_stream_delivery_tracking()\n')
    edit(name, '    from agent.conversation_loop import _system_prompt_for_hooks\n',
         '    from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '    if snapshot_for_agent(agent) is not None:\n        return\n'
         '    from agent.conversation_loop import _system_prompt_for_hooks\n')

    name = 'agent/turn_api_call.py'
    edit(name, '        response = run_llm_execution_middleware(\n'
               '            api_kwargs, _perform_api_call, original_request=_original_api_kwargs,\n'
               '            task_id=effective_task_id, turn_id=turn_id, api_request_id=api_request_id,\n'
               '            session_id=agent.session_id or "", platform=agent.platform or "", model=agent.model,\n'
               '            provider=agent.provider, base_url=agent.base_url, api_mode=agent.api_mode,\n'
               '            api_call_count=api_call_count, middleware_trace=list(_llm_middleware_trace),\n'
               '        )\n',
         '        # HERMES_CONDITIONAL_API_ADMISSION_v1: execution middleware may replace provider kwargs.\n'
         '        from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '        if snapshot_for_agent(agent) is not None:\n'
         '            response = _perform_api_call(api_kwargs)\n'
         '        else:\n'
         '            response = run_llm_execution_middleware(\n'
         '                api_kwargs, _perform_api_call, original_request=_original_api_kwargs,\n'
         '                task_id=effective_task_id, turn_id=turn_id, api_request_id=api_request_id,\n'
         '                session_id=agent.session_id or "", platform=agent.platform or "", model=agent.model,\n'
         '                provider=agent.provider, base_url=agent.base_url, api_mode=agent.api_mode,\n'
         '                api_call_count=api_call_count, middleware_trace=list(_llm_middleware_trace),\n'
         '            )\n')
    name = 'agent/relay_llm.py'
    edit(name, '        if session_id is None:\n            session_id = _current_session_id()\n',
         '        # HERMES_CONDITIONAL_API_ADMISSION_v1: managed Relay can rewrite context/tools.\n'
         '        from agent.conditional_snapshot_policy import current_snapshot\n'
         '        if current_snapshot() is not None:\n            return None\n'
         '        if session_id is None:\n            session_id = _current_session_id()\n')

    # Bind only the existing protected snapshot scope, BEFORE the mutable
    # gateway default/provider/session resolution. Ordinary paths are unchanged.
    name = 'gateway/run.py'
    edit(name, '    # Capture primary provider/model from config before the try block so we\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: protected in-memory native credential, no resolver.\n'
         '    from agent.conditional_snapshot_policy import snapshot_model_consumer\n'
         '    consumer = snapshot_model_consumer()\n'
         '    if consumer is not None:\n'
         '        from hermes_cli.config import load_config_readonly, is_provider_enabled, split_model_config_default\n'
         '        config = load_config_readonly()\n'
         '        model_config = config.get("model")\n'
         '        if not isinstance(model_config, dict):\n'
         '            raise RuntimeError("protected snapshot requires an explicit native Codex model route")\n'
         '        configured_model, configured_provider = split_model_config_default(\n'
         '            model_config.get("default") or model_config.get("model"))\n'
         '        configured_provider = model_config.get("provider") or configured_provider\n'
         '        providers = config.get("providers") or {}\n'
         '        block = providers.get("openai-codex") if isinstance(providers, dict) else None\n'
         '        runtime = consumer.runtime_kwargs()\n'
         '        if (configured_provider != runtime["provider"] or configured_model != runtime["model"]\n'
         '                or str(model_config.get("openai_runtime") or "").strip().lower() == "codex_app_server"\n'
         '                or (model_config.get("base_url") or runtime["base_url"]).rstrip("/") != runtime["base_url"]\n'
         '                or (isinstance(block, dict) and not is_provider_enabled(block))):\n'
         '            raise RuntimeError("protected snapshot native configured model route is unavailable or changed")\n'
         '        return runtime\n'
         '    # Capture primary provider/model from config before the try block so we\n')
    name = 'gateway/platforms/api_server.py'
    edit(name, '        request_model = _clean_request_string(requested_model)\n',
         '        from agent.conditional_snapshot_policy import snapshot_model_consumer\n'
         '        consumer = snapshot_model_consumer()\n'
         '        if consumer is not None:\n'
         '            expected = consumer.runtime_kwargs()\n'
         '            if (model != expected.pop("model") or runtime_kwargs != expected or route is not None\n'
         '                    or requested_model is not None or requested_provider is not None\n'
         '                    or session_model not in (None, model)):\n'
         '                raise RuntimeError("protected snapshot model selection changed")\n'
         '            return model, None, None, None\n'
         '        request_model = _clean_request_string(requested_model)\n')

    name = 'agent/agent_runtime_helpers.py'
    edit(name, '    from agent.auxiliary_client import (\n        _to_openai_base_url,\n',
         '    from agent.conditional_snapshot_policy import snapshot_client_kwargs\n'
         '    restricted = snapshot_client_kwargs(agent, client_kwargs)\n'
         '    if restricted is not None:\n'
         '        from openai import OpenAI\n'
         '        from agent.codex_headers import apply_required_codex_headers\n'
         '        apply_required_codex_headers(restricted, access_token=restricted["api_key"],\n'
         '                                     base_url=restricted["base_url"])\n'
         '        return OpenAI(**restricted)\n'
         '    from agent.auxiliary_client import (\n        _to_openai_base_url,\n')
    name = 'agent/client_lifecycle.py'
    # Fail before any getter/adoption/refresh. No False result can invite another
    # recovery route after an unexpected direct refresh invocation.
    source = updates[name]
    tree = ast.parse(source)
    for method in ('_try_refresh_codex_client_credentials', '_try_refresh_env_client_credentials'):
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == method)
        first = node.body[1] if isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str) else node.body[0]
        lines = source.splitlines(keepends=True)
        offset = sum(len(line) for line in lines[:first.lineno - 1])
        indent = ' ' * first.col_offset
        source = (source[:offset] + indent + '# HERMES_CONDITIONAL_API_ADMISSION_v1: no auth resolution/adoption.\n'
                  + indent + 'from agent.conditional_snapshot_policy import forbid_snapshot_recovery\n'
                  + indent + 'forbid_snapshot_recovery(self)\n' + source[offset:])
        tree = ast.parse(source)
    updates[name] = source
    name = 'agent/conversation_loop.py'
    edit(name, '        agent._try_refresh_env_client_credentials()\n',
         '        # HERMES_CONDITIONAL_API_ADMISSION_v1: no per-turn env credential adoption.\n'
         '        from agent.conditional_snapshot_policy import snapshot_model_consumer\n'
         '        if snapshot_model_consumer(agent) is None:\n'
         '            agent._try_refresh_env_client_credentials()\n')
    name = 'agent/turn_context.py'
    edit(name, '    agent._restore_primary_runtime()\n',
         '    from agent.conditional_snapshot_policy import snapshot_model_consumer\n'
         '    if snapshot_model_consumer(agent) is None:\n'
         '        agent._restore_primary_runtime()\n')
    name = 'agent/codex_runtime.py'
    edit(name, '    max_stream_retries, model = 1, api_kwargs.get("model")\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: no zero-event or transport resend.\n'
         '    from agent.conditional_snapshot_policy import snapshot_model_consumer, prepare_snapshot_codex_wire\n'
         '    max_stream_retries = 0 if snapshot_model_consumer(agent) is not None else 1\n'
         '    model = api_kwargs.get("model")\n')
    edit(name, '        stream_kwargs["stream"] = True\n',
         '        stream_kwargs["stream"] = True\n'
         '        prepare_snapshot_codex_wire(agent, stream_kwargs)\n')
    edit(name, '        # Successful completion orders all output, even when no calls remain pending.\n',
         '        from agent.conditional_snapshot_policy import snapshot_model_consumer\n'
         '        if snapshot_model_consumer() is not None and not self.saw_response_completed:\n'
         '            raise RuntimeError("protected snapshot stream has no completed terminal frame")\n'
         '        # Successful completion orders all output, even when no calls remain pending.\n')
    name = 'agent/turn_api_error.py'
    edit(name, '    _provider_overflow_recovery_pending = False\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: preserve the original error, no recovery/refresh.\n'
         '    from agent.conditional_snapshot_policy import snapshot_for_agent\n'
         '    if snapshot_for_agent(agent) is not None:\n        raise api_error\n'
         '    _provider_overflow_recovery_pending = False\n')
    name = 'agent/turn_response_check.py'
    edit(name, '    response_invalid, error_details = validate_response_shape(agent, response)\n',
         '    # HERMES_CONDITIONAL_API_ADMISSION_v1: reject partial/empty/tool results before recovery.\n'
         '    from agent.conditional_snapshot_policy import check_snapshot_response\n'
         '    check_snapshot_response(agent, response)\n'
         '    response_invalid, error_details = validate_response_shape(agent, response)\n')

    # Validate every exact anchor and syntax tree before writing anything. A partial
    # process failure is detected and refused, never silently treated as applied.
    for name, source in updates.items():
        compile(source, name, 'exec')
    compile(payload, str(destination), 'exec')
    compile(policy_payload, str(policy_destination), 'exec')
    compile(auth_payload, str(auth_destination), 'exec')
    for name, source in updates.items():
        (root / name).write_text(source)
    destination.write_text(payload)
    policy_destination.write_text(policy_payload)
    auth_destination.write_text(auth_payload)
    return True
