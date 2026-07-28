#!/usr/bin/env python3
"""Resolve stale flattened MCP names against the current scoped catalog."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MARKER = "HERMES_MCP_LEGACY_ALIAS_DISPATCH_v1"
COLD_ALIAS_MARKER = "HERMES_MCP_LEGACY_COLD_ALIAS_ACTIVATION_v1"

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

TOOL_SEARCH_CLASSIFY = """    if not is_deferrable_tool_name(name):
        return None, {}, (
"""
TOOL_SEARCH_CLASSIFY_PATCHED = """    canonical_name = _canonicalize_legacy_mcp_name(name, scoped_names)
    if canonical_name is None:
        return None, {}, f"Tool '{name}' has an ambiguous legacy MCP alias"
    name = canonical_name
    if name not in (scoped_names or ()) and not is_deferrable_tool_name(name):
        return None, {}, (
"""

MODEL_TOOLS_CALL = (
    "            underlying_name, underlying_args, err = "
    "_ts_mod.resolve_underlying_call(function_args or {})\n"
    "            if err or not underlying_name:\n"
)
MODEL_TOOLS_CALL_PATCHED = """            _scoped_deferrable = _ts_mod.scoped_deferrable_names(current_defs)
            underlying_name, underlying_args, err = _ts_mod.resolve_underlying_call(
                function_args or {}, scoped_names=_scoped_deferrable
            )
            if err or not underlying_name:
"""
MODEL_TOOLS_DUPLICATE_SCOPE = """            _scoped_deferrable = _ts_mod.scoped_deferrable_names(current_defs)
            if underlying_name not in _scoped_deferrable:
"""
MODEL_TOOLS_SCOPE_REUSE = """            if underlying_name not in _scoped_deferrable:
"""

EXECUTOR_CALL = """                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(function_args)
                if not _err and _underlying:
                    if _underlying in _tool_search_scoped_names(agent):
"""
EXECUTOR_CALL_PATCHED = """                _scoped_names = _tool_search_scoped_names(agent)
                _underlying, _underlying_args, _err = _ts.resolve_underlying_call(
                    function_args, scoped_names=_scoped_names
                )
                if not _err and _underlying:
                    if _underlying in _scoped_names:
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

        _, _, err = resolve_underlying_call(
            {{"name": "mcp_composio_google_personal_GOOGLESUPER_QUICK_ADD", "arguments": {{}}}},
            scoped_names=frozenset({{"mcp__search__search_status"}}),
        )
        assert err is not None
        assert "not a deferrable" in err

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
    source = _replace_exact(
        source,
        TOOL_SEARCH_FUNCTION,
        TOOL_SEARCH_FUNCTION_PATCHED,
        count=1,
        label="tool_search function",
    )
    return _replace_exact(
        source,
        TOOL_SEARCH_CLASSIFY,
        TOOL_SEARCH_CLASSIFY_PATCHED,
        count=1,
        label="tool_search classification",
    )


def patch_model_tools_text(source: str) -> str:
    # The later cold-alias patch replaces this bridge call with a helper that
    # already delegates through the scoped resolver installed here.  Reapplying
    # the registry must recognize that composed form instead of treating it as
    # anchor drift.
    if COLD_ALIAS_MARKER in source:
        required = (
            "_resolve_tool_search_call_with_cold_activation(",
            "scoped_names=_scoped_names",
        )
        if not all(marker in source for marker in required):
            raise RuntimeError("model_tools cold-alias composition drift")
        return source
    source = _replace_exact(
        source,
        MODEL_TOOLS_CALL,
        MODEL_TOOLS_CALL_PATCHED,
        count=1,
        label="model_tools bridge call",
    )
    return _replace_exact(
        source,
        MODEL_TOOLS_DUPLICATE_SCOPE,
        MODEL_TOOLS_SCOPE_REUSE,
        count=1,
        label="model_tools scope reuse",
    )


def patch_tool_executor_text(source: str) -> str:
    # The cold-alias patch runs later in the registry and expands the two
    # already-scoped bridge calls into its activation helper.  Its exact helper
    # footprint is the composed, idempotent form of this patch.
    cold_helper = "_resolve_legacy_cold_alias_for_agent"
    cold_helper_count = source.count(cold_helper)
    if cold_helper_count == 3:
        return source
    if cold_helper_count:
        raise RuntimeError("tool_executor cold-alias composition drift")
    return _replace_exact(
        source,
        EXECUTOR_CALL,
        EXECUTOR_CALL_PATCHED,
        count=2,
        label="tool_executor bridge calls",
    )


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


def patch_mcp_legacy_alias_bridge_v1(hermes_dir: Path) -> bool:
    """Apply hot resolution and cold activation as one atomic compatibility patch."""
    sibling = Path(__file__).with_name("mcp_legacy_cold_alias_activation_v1.py")
    spec = importlib.util.spec_from_file_location("mcp_legacy_cold_alias_activation_v1", sibling)
    if not spec or not spec.loader:
        raise RuntimeError("cold alias patch module is unavailable")
    cold = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cold)

    transforms = {
        "tools/tool_search.py": (patch_tool_search_text,),
        "model_tools.py": (patch_model_tools_text, cold.patch_model_tools_text),
        "agent/tool_executor.py": (patch_tool_executor_text, cold.patch_tool_executor_text),
        "tests/tools/test_tool_search.py": (patch_tool_search_tests_text,),
    }
    pending: list[tuple[Path, str]] = []
    for relative, steps in transforms.items():
        path = Path(hermes_dir) / relative
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
    changed = patch_mcp_legacy_alias_dispatch_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
