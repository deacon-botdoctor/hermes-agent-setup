"""Small fail-closed checks for cross-file runtime assembly contracts."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path


class AssembledRuntimeContractError(RuntimeError):
    pass


def _function_signature(function: ast.FunctionDef) -> inspect.Signature:
    positional = [*function.args.posonlyargs, *function.args.args]
    first_default = len(positional) - len(function.args.defaults)
    parameters = [
        inspect.Parameter(
            arg.arg,
            (
                inspect.Parameter.POSITIONAL_ONLY
                if index < len(function.args.posonlyargs)
                else inspect.Parameter.POSITIONAL_OR_KEYWORD
            ),
            default=(inspect.Parameter.empty if index < first_default else None),
        )
        for index, arg in enumerate(positional)
    ]
    if function.args.vararg is not None:
        parameters.append(inspect.Parameter(function.args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    parameters.extend(
        inspect.Parameter(
            arg.arg,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if default is None else None,
        )
        for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults)
    )
    if function.args.kwarg is not None:
        parameters.append(inspect.Parameter(function.args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def _native_init_keyword_names(agent_dir: Path, tree: ast.Module, call: ast.Call) -> list[str] | None:
    """Bind Hermes' exact locals-comprehension forwarder, never arbitrary **kwargs.

    The source shape is the evidence here: it forwards every declared ``__init__``
    parameter except the deprecated local-only ``tool_delay``.  That remains safe
    across the split-current source line, unlike accepting a generic ``**kwargs``
    forwarder.  The d363 pin used this same shape; current Hermes retained it.
    """
    owners = [method for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "AIAgent"
              for method in cls.body if isinstance(method, ast.FunctionDef) and method.name == "__init__"
              if call in list(ast.walk(method))]
    if len(owners) != 1:
        return None
    owner = owners[0]
    canonical = ast.parse("""
init_kwargs = {k: v for k, v in locals().items() if k not in ("self", "tool_delay")}
if tool_delay is not None:
    warnings.warn("tool_delay is deprecated and ignored; sequential tool calls "
                  "no longer sleep between executions.", DeprecationWarning, stacklevel=2)
from agent.agent_init import init_agent
init_agent(self, **init_kwargs)
""").body
    body = owner.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if [ast.dump(n) for n in body] != [ast.dump(n) for n in canonical]:
        return None
    args = owner.args
    if args.vararg or args.kwarg or args.posonlyargs or owner.decorator_list:
        return None
    names = [arg.arg for arg in [*args.args, *args.kwonlyargs]]
    if not names or names[0] != "self" or "tool_delay" not in names:
        return None
    return [name for name in names if name not in {"self", "tool_delay"}]


def verify_agent_init_forwarder_contract(agent_dir: Path) -> None:
    """Prove every explicit ``init_agent`` call binds to the shipped callee."""
    agent_path = agent_dir / "run_agent.py"
    init_path = agent_dir / "agent" / "agent_init.py"
    if not agent_path.exists() and not init_path.exists():
        return
    if not agent_path.exists() or not init_path.exists():
        raise AssembledRuntimeContractError("run_agent.py and agent/agent_init.py must be verified together")

    agent_tree = ast.parse(agent_path.read_text(encoding="utf-8", errors="strict"))
    init_tree = ast.parse(init_path.read_text(encoding="utf-8", errors="strict"))
    callees = [node for node in init_tree.body if isinstance(node, ast.FunctionDef) and node.name == "init_agent"]
    calls = [
        node
        for node in ast.walk(agent_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "init_agent"
    ]
    if len(callees) != 1:
        raise AssembledRuntimeContractError(
            f"expected exactly one top-level init_agent definition, found {len(callees)}"
        )
    if not calls:
        raise AssembledRuntimeContractError("run_agent.py has no explicit init_agent forwarder call")

    callee = callees[0]
    signature = _function_signature(callee)

    for call in calls:
        if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
            keyword.arg is None for keyword in call.keywords
        ):
            native_names = _native_init_keyword_names(agent_dir, agent_tree, call)
            if native_names is None:
                raise AssembledRuntimeContractError(
                    "init_agent forwarder uses dynamic argument expansion; the assembly contract cannot bind it statically"
                )
            keyword_names = native_names
        else:
            keyword_names = [keyword.arg for keyword in call.keywords]
        try:
            signature.bind(
                *([None] * len(call.args)),
                **{name: None for name in keyword_names},
            )
        except TypeError as exc:
            raise AssembledRuntimeContractError(f"init_agent forwarder cannot bind to shipped callee: {exc}") from exc




def verify_native_cua_existing_profile_grant_contract(agent_dir: Path) -> None:
    """Reject a reintroduced retired CUA existing-profile transport.

    Hermes d363 splits the CUA backend/session classes while cb5a8 keeps them
    monolithic; neither shape has an existing-profile transport. The release
    contract verifies either source-owned layout rather than relying on
    Golden-owned marker comments. If a legacy transport returns, this fails
    closed until its authorization contract is restored deliberately.
    """
    backend_path = agent_dir / "tools" / "computer_use" / "cua_backend.py"
    cua_dir = backend_path.parent
    backend_sources = sorted(cua_dir.glob("cua_backend*.py")) if cua_dir.exists() else []
    if not backend_path.exists():
        # Other assembled-contract fixtures intentionally omit CUA entirely.
        # A candidate runtime and a partial CUA tree must both fail closed.
        if not (agent_dir / "pyproject.toml").exists() and not backend_sources:
            return
        raise AssembledRuntimeContractError("native Cua backend source is missing")

    source_paths = [*backend_sources]
    defaults_path = agent_dir / "hermes_cli" / "config_defaults.py"
    if defaults_path.exists():
        source_paths.append(defaults_path)

    try:
        sources = {
            path: path.read_text(encoding="utf-8", errors="strict") for path in source_paths
        }
        trees = {path: ast.parse(text, filename=str(path)) for path, text in sources.items()}
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise AssembledRuntimeContractError(
            f"cannot inspect native Cua existing-profile transport: {exc}"
        ) from exc

    for expected in ("CuaDriverBackend", "_CuaDriverSession"):
        owners = [
            path
            for path in backend_sources
            if any(isinstance(node, ast.ClassDef) and node.name == expected for node in trees[path].body)
        ]
        if len(owners) != 1:
            raise AssembledRuntimeContractError(
                f"native Cua owner {expected} is missing or ambiguous across backend sources"
            )

    legacy_surface = (
        "grant_existing_profile",
        "typed_browser_",
        "HERMES_CUA_EXISTING_PROFILE",
        "_cua_grant_existing_profile",
    )
    introduced = sorted(
        marker
        for marker in legacy_surface
        if any(marker in ast.unparse(tree) for tree in trees.values())
    )
    if introduced:
        raise AssembledRuntimeContractError(
            "legacy Cua existing-profile transport remains: " + ", ".join(introduced)
        )


def verify_hybrid_retrieval_contract(agent_dir: Path) -> None:
    """Reject an incremental assembly that retains automatic tenant retrieval."""
    turn_path = agent_dir / "agent" / "turn_context.py"
    source = turn_path.read_text(encoding="utf-8") if turn_path.exists() else ""
    if (agent_dir / "agent" / "gbrain_context.py").exists() or any(
        marker in source for marker in ("HERMES_GBRAIN_CONTEXT_v1", "agent.gbrain_context")
    ):
        raise AssembledRuntimeContractError(
            "retired automatic GBrain retrieval remains; rebuild from clean pinned upstream"
        )


def verify_conversation_loop_agent_contract(agent_dir: Path) -> None:
    """Verify incident-backed cross-file AIAgent contracts.

    This includes the explicit ``run_agent.py`` → ``agent_init.py`` forwarder
    signature and optional concrete-agent methods used by the conversation
    loop.
    """
    verify_hybrid_retrieval_contract(agent_dir)
    verify_agent_init_forwarder_contract(agent_dir)
    verify_native_cua_existing_profile_grant_contract(agent_dir)

    loop_path = agent_dir / "agent" / "conversation_loop.py"
    agent_path = agent_dir / "run_agent.py"
    if not loop_path.exists() and not agent_path.exists():
        return
    if not loop_path.exists() or not agent_path.exists():
        raise AssembledRuntimeContractError("run_agent.py and agent/conversation_loop.py must be verified together")

    incident_methods = {
        "_is_copilot_url",
        "_emit_pending_fallback_notice",
        "_interim_assistant_visible_text",
    }
    loop_text = loop_path.read_text(encoding="utf-8", errors="strict")
    exercised = {name for name in incident_methods if re.search(rf"\bagent\s*\.\s*{re.escape(name)}\s*\(", loop_text)}
    if not exercised:
        return

    if exercised:
        raise AssembledRuntimeContractError(
            "conversation_loop directly calls optional AIAgent methods; use guarded "
            "call-site lookup: " + ", ".join(sorted(exercised))
        )
