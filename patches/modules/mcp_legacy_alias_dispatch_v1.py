#!/usr/bin/env python3
"""Resolve stale flattened MCP names against the current scoped catalog."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_MCP_LEGACY_ALIAS_DISPATCH_v1"

TOOL_SEARCH_FUNCTION = (
    "def resolve_underlying_call(args: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:\n"
)
TOOL_SEARCH_FUNCTION_PATCHED = f'''def _legacy_mcp_names(name: str) -> set[str]:
    body = name[len("mcp__"):]
    parts = body.split("__")
    return {{
        f"mcp_{{'__'.join(parts[:boundary])}}_{{'__'.join(parts[boundary:])}}"
        for boundary in range(1, len(parts))
    }}


def _canonicalize_legacy_mcp_name(
    name: str, scoped_names: Optional[frozenset[str]]
) -> Optional[str]:
    """Map one stale ``mcp_server_tool`` name to its scoped canonical name.

    # {MARKER}
    The old flattened contract is ambiguous when server or tool components
    contain underscores. Resolve only when the current session catalog yields
    exactly one scoped match; otherwise preserve unmatched input so the normal
    scope/deferrability checks reject it.
    """
    if not scoped_names or not name.startswith("mcp_") or name.startswith("mcp__"):
        return name
    matches = [
        candidate
        for candidate in scoped_names
        if candidate == name
        or (candidate.startswith("mcp__") and name in _legacy_mcp_names(candidate))
    ]
    if len(matches) == 1:
        return matches[0]
    return name if not matches else None


def resolve_underlying_call(
    args: Dict[str, Any], *, scoped_names: Optional[frozenset[str]] = None
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
'''


# d363 split owners retain the same bridge contract, but their API passes the
# configured defer set into the resolver and has one shared executor unwrap.
D363_TOOL_SEARCH_CLASSIFY = """    if not is_deferrable_tool_name(name, load_config_readonly().effective_defer_tools):
        return None, {}, (
"""
D363_TOOL_SEARCH_CLASSIFY_PATCHED = """    canonical_name = _canonicalize_legacy_mcp_name(name, scoped_names)
    if canonical_name is None:
        return None, {}, f\"Tool '{name}' has an ambiguous legacy MCP alias\"
    name = canonical_name
    if scoped_names is not None:
        if name in scoped_names:
            return name, raw_args, None
        return None, {}, (
            f\"Route correction required: '{name}' is not a deferrable tool. Do not \"
            f\"call tool_call again for '{name}'; call '{name}' directly with its \"
            \"arguments at the top level. tool_call is only for deferred tools \"
            \"returned by tool_search.\")
    if not is_deferrable_tool_name(
        name, load_config_readonly().effective_defer_tools
    ):
        return None, {}, (
"""
D363_MODEL_TOOLS_CALL = """    underlying_name, underlying_args, err = ts.resolve_underlying_call(args)
    if err or not underlying_name:
"""
D363_MODEL_TOOLS_CALL_PATCHED = """    # {MARKER}: d363 residual; native lazy MCP activation remains authoritative.
    _scoped_bridge_names = ts.scoped_deferrable_names(current_defs) | frozenset(
        str((definition.get(\"function\") or {}).get(\"name\") or \"\")
        for definition in current_defs
        if str((definition.get(\"function\") or {}).get(\"name\") or \"\")
    )
    underlying_name, underlying_args, err = ts.resolve_underlying_call(
        args, scoped_names=_scoped_bridge_names
    )
    if err or not underlying_name:
""".replace("{MARKER}", MARKER)
D363_MODEL_TOOLS_SCOPE = """    if underlying_name not in ts.scoped_deferrable_names(current_defs):
"""
D363_MODEL_TOOLS_SCOPE_PATCHED = """    if underlying_name not in _scoped_bridge_names:
"""
D363_EXECUTOR_CALL = """        underlying, underlying_args, err = _ts.resolve_underlying_call(function_args)
        if err or not underlying:
            return function_name, function_args, None
        if underlying not in _tool_search_scoped_names(agent):
"""
D363_EXECUTOR_CALL_PATCHED = """        _scoped_names = _tool_search_scoped_names(agent) | frozenset(
            getattr(agent, \"valid_tool_names\", ()) or ()
        )
        underlying, underlying_args, err = _ts.resolve_underlying_call(
            function_args, scoped_names=_scoped_names
        )
        if err or not underlying:
            return function_name, function_args, err or \"tool_call could not be resolved\"
        if underlying not in _scoped_names:
"""

TEST_ANCHOR = """# ---------------------------------------------------------------------------
# End-to-end via the real handle_function_call (smoke test).
# ---------------------------------------------------------------------------
"""
TESTS = f'''class TestLegacyMcpAliasDispatch:
    """{MARKER}: stale history may contain the pre-delimiter MCP name."""

    def test_unique_scoped_legacy_alias_resolves_to_canonical_name(self):
        from tools.tool_search import resolve_underlying_call

        canonical = "mcp__composio_google_personal__GOOGLESUPER_QUICK_ADD"
        name, args, err = resolve_underlying_call(
            {{"name": "mcp_composio_google_personal_GOOGLESUPER_QUICK_ADD", "arguments": {{"text": "hold"}}}},
            scoped_names=frozenset({{canonical}}),
        )
        assert err is None
        assert name == canonical
        assert args == {{"text": "hold"}}

    def test_ambiguous_legacy_alias_fails_closed(self):
        from tools.tool_search import resolve_underlying_call

        _, _, err = resolve_underlying_call(
            {{"name": "mcp_alpha_beta_gamma", "arguments": {{}}}},
            scoped_names=frozenset({{"mcp__alpha_beta__gamma", "mcp__alpha__beta_gamma"}}),
        )
        assert err is not None
        assert "ambiguous legacy MCP alias" in err

    def test_exact_scoped_name_collision_fails_closed(self):
        from tools.tool_search import resolve_underlying_call

        _, _, err = resolve_underlying_call(
            {{"name": "mcp_foo_bar", "arguments": {{}}}},
            scoped_names=frozenset({{"mcp_foo_bar", "mcp__foo__bar"}}),
        )
        assert err is not None
        assert "ambiguous legacy MCP alias" in err

    def test_component_double_underscore_is_preserved(self):
        from tools.tool_search import resolve_underlying_call

        canonical = "mcp__acme__foo__bar"
        name, _, err = resolve_underlying_call(
            {{"name": "mcp_acme_foo__bar", "arguments": {{}}}},
            scoped_names=frozenset({{canonical}}),
        )
        assert err is None
        assert name == canonical

    def test_out_of_scope_legacy_alias_is_not_resolved(self):
        from tools.tool_search import resolve_underlying_call

        name, args, err = resolve_underlying_call(
            {{"name": "mcp_composio_google_personal_GOOGLESUPER_QUICK_ADD", "arguments": {{}}}},
            scoped_names=frozenset({{"mcp__search__search_status"}}),
        )
        assert name is None and args == {{}}
        assert err is not None and "mcp_composio_google_personal_GOOGLESUPER_QUICK_ADD" in err

    def test_model_bridge_dispatches_unique_scoped_alias(self):
        import model_tools
        from tools.registry import registry

        canonical = "mcp__legacy_alias_fixture__read"

        def handler(args, task_id=None, **kwargs):
            return json.dumps({{"ok": True, "value": args.get("value")}})

        registry.register(
            name=canonical,
            handler=handler,
            schema=_td(canonical, "legacy alias fixture", {{"value": {{"type": "string"}}}}),
            toolset="mcp-legacy-alias-fixture",
        )
        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={{"name": "mcp_legacy_alias_fixture_read", "arguments": {{"value": "passed"}}}},
            enabled_toolsets=["mcp-legacy-alias-fixture"],
        ))
        assert result == {{"ok": True, "value": "passed"}}


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
    source = _replace_exact(
        source, TOOL_SEARCH_FUNCTION, TOOL_SEARCH_FUNCTION_PATCHED,
        count=1, label="tool_search function",
    )
    if "return None, {}, not_deferrable_error(name)" in source:
        # Native guidance distinguishes unknown names from directly listed tools.
        # Preserve that owner; only scoped deferred names need an extra denial.
        classification = D363_TOOL_SEARCH_CLASSIFY.split("        return", 1)[0]
        scoped = D363_TOOL_SEARCH_CLASSIFY_PATCHED.split("    if scoped_names is not None:", 1)[0]
        scoped += '''    if scoped_names is not None:
        if name in scoped_names:
            return name, raw_args, None
        if is_deferrable_tool_name(name, load_config_readonly().effective_defer_tools):
            return None, {}, f"Tool '{name}' is outside this session's tool scope. Use tool_search to find an available tool."
'''
        return _replace_exact(source, classification, scoped + classification,
                              count=1, label="native tool_search classification")
    return _replace_exact(
        source, D363_TOOL_SEARCH_CLASSIFY, D363_TOOL_SEARCH_CLASSIFY_PATCHED,
        count=1, label="tool_search classification",
    )

def patch_model_tools_text(source: str) -> str:
    # d363 moved bridge dispatch into _dispatch_bridge_tool. It has native lazy
    # MCP registration, so only scoped alias/direct-wrapper resolution remains.
    if MARKER in source or D363_MODEL_TOOLS_CALL_PATCHED in source:
        return source
    if D363_MODEL_TOOLS_CALL in source:
        source = _replace_exact(
            source, D363_MODEL_TOOLS_CALL, D363_MODEL_TOOLS_CALL_PATCHED,
            count=1, label="d363 model_tools bridge call",
        )
        return _replace_exact(
            source, D363_MODEL_TOOLS_SCOPE, D363_MODEL_TOOLS_SCOPE_PATCHED,
            count=1, label="d363 model_tools scope reuse",
        )
    raise RuntimeError("model_tools bridge call anchor drift")

def patch_tool_executor_text(source: str) -> str:
    if MARKER in source or D363_EXECUTOR_CALL_PATCHED in source:
        return source
    batch = "        if underlying == _ts.CONNECTOR_BATCH_SENTINEL:\n"
    if batch in source:
        old = "        underlying, underlying_args, err = _ts.resolve_underlying_call(function_args)\n        if err or not underlying:\n            return function_name, function_args, None\n"
        new = D363_EXECUTOR_CALL_PATCHED.split("        if underlying not in _scoped_names:", 1)[0]
        source = _replace_exact(source, old, new, count=1, label="batch executor scoped resolution")
        return _replace_exact(source, "        if underlying not in _tool_search_scoped_names(agent):\n",
                              "        if underlying not in _scoped_names:\n", count=1, label="batch executor scope")
    if D363_EXECUTOR_CALL in source:
        return _replace_exact(
            source, D363_EXECUTOR_CALL, D363_EXECUTOR_CALL_PATCHED,
            count=1, label="d363 tool_executor bridge call",
        )
    raise RuntimeError("tool_executor bridge calls anchor drift")

def patch_tool_search_tests_text(source: str) -> str:
    if MARKER in source:
        return source
    if source.count(TEST_ANCHOR) != 1:
        raise RuntimeError("tool_search test anchor drift")
    return source.replace(TEST_ANCHOR, TESTS + TEST_ANCHOR, 1)


def patch_mcp_legacy_alias_dispatch_v1(hermes_dir: Path) -> bool:
    transforms = {
        "tools/tool_search.py": patch_tool_search_text,
        "model_tools.py": patch_model_tools_text,
        "agent/tool_executor.py": patch_tool_executor_text,
        "tests/tools/test_tool_search.py": patch_tool_search_tests_text,
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


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_mcp_legacy_alias_dispatch_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
