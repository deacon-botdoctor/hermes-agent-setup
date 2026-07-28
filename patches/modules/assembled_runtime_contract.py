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
            default=(
                inspect.Parameter.empty
                if index < first_default
                else None
            ),
        )
        for index, arg in enumerate(positional)
    ]
    if function.args.vararg is not None:
        parameters.append(
            inspect.Parameter(function.args.vararg.arg, inspect.Parameter.VAR_POSITIONAL)
        )
    parameters.extend(
        inspect.Parameter(
            arg.arg,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if default is None else None,
        )
        for arg, default in zip(
            function.args.kwonlyargs, function.args.kw_defaults
        )
    )
    if function.args.kwarg is not None:
        parameters.append(
            inspect.Parameter(function.args.kwarg.arg, inspect.Parameter.VAR_KEYWORD)
        )
    return inspect.Signature(parameters)


def verify_agent_init_forwarder_contract(agent_dir: Path) -> None:
    """Prove every explicit ``init_agent`` call binds to the shipped callee."""
    agent_path = agent_dir / "run_agent.py"
    init_path = agent_dir / "agent" / "agent_init.py"
    if not agent_path.exists() and not init_path.exists():
        return
    if not agent_path.exists() or not init_path.exists():
        raise AssembledRuntimeContractError(
            "run_agent.py and agent/agent_init.py must be verified together"
        )

    agent_tree = ast.parse(agent_path.read_text(encoding="utf-8", errors="strict"))
    init_tree = ast.parse(init_path.read_text(encoding="utf-8", errors="strict"))
    callees = [
        node
        for node in init_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "init_agent"
    ]
    calls = [
        node
        for node in ast.walk(agent_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "init_agent"
    ]
    if len(callees) != 1:
        raise AssembledRuntimeContractError(
            f"expected exactly one top-level init_agent definition, found {len(callees)}"
        )
    if not calls:
        raise AssembledRuntimeContractError(
            "run_agent.py has no explicit init_agent forwarder call"
        )

    callee = callees[0]
    signature = _function_signature(callee)

    for call in calls:
        if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
            keyword.arg is None for keyword in call.keywords
        ):
            raise AssembledRuntimeContractError(
                "init_agent forwarder uses dynamic argument expansion; "
                "the assembly contract cannot bind it statically"
            )
        try:
            signature.bind(
                *([None] * len(call.args)),
                **{keyword.arg: None for keyword in call.keywords},
            )
        except TypeError as exc:
            raise AssembledRuntimeContractError(
                f"init_agent forwarder cannot bind to shipped callee: {exc}"
            ) from exc


def verify_conversation_loop_agent_contract(agent_dir: Path) -> None:
    """Verify incident-backed cross-file AIAgent contracts.

    This includes the explicit ``run_agent.py`` → ``agent_init.py`` forwarder
    signature and optional concrete-agent methods used by the conversation
    loop.
    """
    verify_agent_init_forwarder_contract(agent_dir)

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
