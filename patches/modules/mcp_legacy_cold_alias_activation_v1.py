#!/usr/bin/env python3
"""Activate a uniquely scoped cold MCP before dispatching its stale alias."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_MCP_LEGACY_COLD_ALIAS_ACTIVATION_v1"
DIRECT_WRAPPER_MARKER = "HERMES_DIRECT_TOOL_WRAPPER_COMPAT_v1"

MODEL_TOOLS_FUNCTION = "def handle_function_call(\n"
MODEL_TOOLS_FUNCTION_PATCHED = f"""_{MARKER} = True


def _scoped_bridge_dispatch_names(tool_defs):
    # {DIRECT_WRAPPER_MARKER}: tool_call may safely unwrap any exact tool name
    # already granted to this session. This keeps provider wrapper mistakes
    # inside the same scope as a normal top-level call.
    return frozenset(
        name
        for definition in tool_defs
        for name in [str((definition.get("function") or {{}}).get("name") or "")]
        if name
    )


def _resolve_tool_search_call_with_cold_activation(
    function_args,
    *,
    current_defs,
    enabled_toolsets,
    disabled_toolsets,
    dispatch_context,
):
    from tools import tool_search as _ts_mod

    _scoped_names = (
        _ts_mod.scoped_deferrable_names(current_defs)
        | _scoped_bridge_dispatch_names(current_defs)
    )
    _underlying, _underlying_args, _err = _ts_mod.resolve_underlying_call(
        function_args or {{}}, scoped_names=_scoped_names
    )
    _activation_attempted = False
    _requested_name = str((function_args or {{}}).get("name") or "").strip()
    _control_names = {{
        str((_definition.get("function") or {{}}).get("name") or "")
        for _definition in current_defs
    }}
    if (
        _err
        and _requested_name.startswith("mcp_")
        and not _requested_name.startswith("mcp__")
        and {{"mcp_server_status", "restart_mcp_server"}} <= _control_names
    ):
        _call_context = dict(dispatch_context or {{}})
        _call_context.update(
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            skip_pre_tool_call_hook=False,
            skip_tool_request_middleware=False,
            tool_request_middleware_trace=[],
        )
        if enabled_toolsets is None:
            try:
                _status_raw = handle_function_call(
                    function_name="mcp_server_status",
                    function_args={{}},
                    **_call_context,
                )
                _status = json.loads(_status_raw)
            except Exception:
                _status = {{}}
            _candidate_toolsets = [
                f"mcp-{{_row.get('server_name')}}"
                for _row in (_status.get("servers") or [])
                if isinstance(_row, dict) and _row.get("server_name")
            ] if isinstance(_status, dict) and _status.get("ok") is True else []
            _candidate_toolsets = set(_candidate_toolsets) - set(disabled_toolsets or [])
        else:
            _candidate_toolsets = set(enabled_toolsets) - set(disabled_toolsets or [])

        _matches = []
        for _toolset in _candidate_toolsets:
            if not str(_toolset).startswith("mcp-"):
                continue
            _server_name = str(_toolset)[4:]
            _server_component = re.sub(r"[^A-Za-z0-9_]", "_", _server_name)
            _legacy_prefix = f"mcp_{{_server_component}}_"
            if _requested_name.startswith(_legacy_prefix) and _requested_name[len(_legacy_prefix):]:
                _matches.append(_server_name)

        _matches = sorted(set(_matches))
        if len(_matches) == 1:
            _activation_attempted = True
            try:
                _activation_raw = handle_function_call(
                    function_name="restart_mcp_server",
                    function_args={{"server_name": _matches[0]}},
                    _mcp_ensure_active=True,
                    **_call_context,
                )
                _activation = json.loads(_activation_raw)
            except Exception:
                _activation = {{}}
            if isinstance(_activation, dict) and _activation.get("ok") is True:
                try:
                    current_defs = get_tool_definitions(
                        enabled_toolsets=enabled_toolsets,
                        disabled_toolsets=disabled_toolsets,
                        quiet_mode=True,
                        skip_tool_search_assembly=True,
                    ) or []
                except Exception:
                    current_defs = []
                _scoped_names = (
                    _ts_mod.scoped_deferrable_names(current_defs)
                    | _scoped_bridge_dispatch_names(current_defs)
                )
                _underlying, _underlying_args, _err = _ts_mod.resolve_underlying_call(
                    function_args or {{}}, scoped_names=_scoped_names
                )
            else:
                _status = _activation.get("status") if isinstance(_activation, dict) else None
                _err = "MCP activation failed" + (f" ({{_status}})" if _status else "")

    return _underlying, _underlying_args, _err, _scoped_names, _activation_attempted


def handle_function_call(
"""

MODEL_TOOLS_CALL = """            _scoped_deferrable = _ts_mod.scoped_deferrable_names(current_defs)
            underlying_name, underlying_args, err = _ts_mod.resolve_underlying_call(
                function_args or {}, scoped_names=_scoped_deferrable
            )
            if err or not underlying_name:
"""
MODEL_TOOLS_CALL_PATCHED = """            underlying_name, underlying_args, err, _scoped_deferrable, _ = (
                _resolve_tool_search_call_with_cold_activation(
                    function_args or {},
                    current_defs=current_defs,
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    dispatch_context={
                        "task_id": task_id,
                        "tool_call_id": tool_call_id,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "api_request_id": api_request_id,
                        "user_task": user_task,
                        "enabled_tools": enabled_tools,
                    },
                )
            )
            if err or not underlying_name:
"""

MODEL_TOOLS_STALE_SCOPE = """            _scoped_deferrable = _ts_mod.scoped_deferrable_names(current_defs)
            if underlying_name not in _scoped_deferrable:
"""
MODEL_TOOLS_REFRESHED_SCOPE = """            if underlying_name not in _scoped_deferrable:
"""

MODEL_TOOLS_CONTEXT = """    disabled_toolsets: Optional[List[str]] = None,
) -> str:
"""
MODEL_TOOLS_CONTEXT_PATCHED = """    disabled_toolsets: Optional[List[str]] = None,
    _mcp_ensure_active: bool = False,
) -> str:
"""

MODEL_TOOLS_DISPATCH = """                    return registry.dispatch(
                        function_name, next_args,
                        task_id=task_id,
                        session_id=session_id,
                        user_task=user_task,
                    )
"""
MODEL_TOOLS_DISPATCH_PATCHED = """                    return registry.dispatch(
                        function_name, next_args,
                        task_id=task_id,
                        session_id=session_id,
                        user_task=user_task,
                        **(
                            {"_mcp_ensure_active": True}
                            if function_name == "restart_mcp_server" and _mcp_ensure_active is True
                            else {}
                        ),
                    )
"""

EXECUTOR_CALL = """                _scoped_names = _tool_search_scoped_names(agent)
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(
                    function_args, scoped_names=_scoped_names
                )
"""
EXECUTOR_SCOPE_OLD = """        names = _ts.scoped_deferrable_names(scoped_defs)
"""
EXECUTOR_SCOPE_NEW = f"""        # {DIRECT_WRAPPER_MARKER}: include exact direct tools in the
        # session scope so provider-emitted tool_call wrappers execute under
        # the same hooks, approvals, and checkpoints as top-level calls.
        names = frozenset(
            name
            for definition in scoped_defs
            for name in [str((definition.get("function") or {{}}).get("name") or "")]
            if name
        )
"""
EXECUTOR_FUNCTION = "def execute_tool_calls_concurrent("
EXECUTOR_FUNCTION_PATCHED = """def _resolve_legacy_cold_alias_for_agent(
    agent, function_args, effective_task_id, tool_call_id
):
    import model_tools as _model_tools

    enabled_toolsets = getattr(agent, "enabled_toolsets", None)
    disabled_toolsets = getattr(agent, "disabled_toolsets", None)
    current_defs = _model_tools.get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=True,
        skip_tool_search_assembly=True,
    ) or []
    return _model_tools._resolve_tool_search_call_with_cold_activation(
        function_args,
        current_defs=current_defs,
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        dispatch_context={
            "task_id": effective_task_id,
            "tool_call_id": tool_call_id,
            "session_id": getattr(agent, "session_id", "") or "",
            "turn_id": getattr(agent, "_current_turn_id", "") or "",
            "api_request_id": getattr(agent, "_current_api_request_id", "") or "",
            "enabled_tools": list(agent.valid_tool_names) if agent.valid_tool_names else None,
        },
    )


def execute_tool_calls_concurrent("""
EXECUTOR_COLD_FALLBACK = """                _activation_attempted = False
                _requested_name = str((function_args or {}).get("name") or "").strip()
                if (
                    _err
                    and _requested_name.startswith("mcp_")
                ):
                    (
                        _underlying,
                        _underlying_args,
                        _err,
                        _scoped_names,
                        _activation_attempted,
                    ) = _resolve_legacy_cold_alias_for_agent(
                        agent,
                        function_args,
                        effective_task_id,
                        getattr(tool_call, "id", "") or "",
                    )
"""
EXECUTOR_CONCURRENT_CALL_PATCHED = EXECUTOR_CALL + EXECUTOR_COLD_FALLBACK + """
                if _activation_attempted and (_err or not _underlying):
                    _ts_scope_block = json.dumps(
                        {"error": _err or "MCP activation failed"}, ensure_ascii=False
                    )
"""
EXECUTOR_SEQUENTIAL_CALL_PATCHED = EXECUTOR_CALL + EXECUTOR_COLD_FALLBACK + """
                if _activation_attempted and (_err or not _underlying):
                    _ts_scope_block = _err or "MCP activation failed"
"""


def patch_model_tools_text(source: str) -> str:
    if MARKER in source:
        stale_scope_count = source.count(MODEL_TOOLS_STALE_SCOPE)
        if stale_scope_count > 1:
            raise RuntimeError("model_tools stale scope anchors drift")
        if stale_scope_count == 1:
            return source.replace(MODEL_TOOLS_STALE_SCOPE, MODEL_TOOLS_REFRESHED_SCOPE, 1)
        return source
    if source.count(MODEL_TOOLS_FUNCTION) != 1:
        raise RuntimeError("model_tools function anchor drift")
    if source.count(MODEL_TOOLS_CALL) != 1:
        raise RuntimeError("model_tools bridge anchor drift")
    if source.count(MODEL_TOOLS_CONTEXT) != 1:
        raise RuntimeError("model_tools context anchor drift")
    if source.count(MODEL_TOOLS_DISPATCH) != 1:
        raise RuntimeError("model_tools dispatch anchor drift")
    if source.count(MODEL_TOOLS_STALE_SCOPE) != 1:
        raise RuntimeError("model_tools stale scope anchor drift")
    source = source.replace(MODEL_TOOLS_FUNCTION, MODEL_TOOLS_FUNCTION_PATCHED, 1)
    source = source.replace(MODEL_TOOLS_CALL, MODEL_TOOLS_CALL_PATCHED, 1)
    source = source.replace(MODEL_TOOLS_CONTEXT, MODEL_TOOLS_CONTEXT_PATCHED, 1)
    source = source.replace(MODEL_TOOLS_DISPATCH, MODEL_TOOLS_DISPATCH_PATCHED, 1)
    return source.replace(MODEL_TOOLS_STALE_SCOPE, MODEL_TOOLS_REFRESHED_SCOPE, 1)


def patch_tool_executor_text(source: str) -> str:
    helper_name = "_resolve_legacy_cold_alias_for_agent"
    if source.count(helper_name) != 3:
        if source.count(EXECUTOR_FUNCTION) != 1:
            raise RuntimeError("tool_executor function anchor drift")
        if source.count(EXECUTOR_CALL) != 2:
            raise RuntimeError("tool_executor bridge anchors drift")
        source = source.replace(EXECUTOR_FUNCTION, EXECUTOR_FUNCTION_PATCHED, 1)
        before, between, after = source.split(EXECUTOR_CALL)
        source = (
            before
            + EXECUTOR_CONCURRENT_CALL_PATCHED
            + between
            + EXECUTOR_SEQUENTIAL_CALL_PATCHED
            + after
        )
    if source.count(EXECUTOR_SCOPE_NEW) == 1:
        return source
    if source.count(EXECUTOR_SCOPE_OLD) != 1:
        raise RuntimeError("tool_executor scoped names anchor drift")
    return source.replace(EXECUTOR_SCOPE_OLD, EXECUTOR_SCOPE_NEW, 1)


def patch_mcp_legacy_cold_alias_activation_v1(hermes_dir: Path) -> bool:
    transforms = {
        "model_tools.py": patch_model_tools_text,
        "agent/tool_executor.py": patch_tool_executor_text,
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
