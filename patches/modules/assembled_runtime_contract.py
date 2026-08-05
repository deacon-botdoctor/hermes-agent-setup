"""Small fail-closed checks for cross-file runtime assembly contracts."""

from __future__ import annotations

import ast
import inspect
import re
import threading
from pathlib import Path
from types import SimpleNamespace


class AssembledRuntimeContractError(RuntimeError):
    pass


_TELEGRAM_CHECKPOINT_MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2"
_TELEGRAM_CHECKPOINT_HELPERS = {
    "_telegram_checkpoint_task_label",
    "_telegram_checkpoint_preview_subject",
    "_telegram_checkpoint_tool_labels",
    "_capture_telegram_tool_checkpoint",
    "_telegram_checkpoint_activity_label",
    "_format_telegram_model_checkpoint",
    "_sanitize_telegram_checkpoint_commentary_v2",
    "_telegram_checkpoint_minutes_v2",
}
_TELEGRAM_CHECKPOINT_BANNED_COPY = {
    "Still working on:",
    "I’ll send the verified outcome when this run completes.",
    "I'll send the verified outcome when this run completes.",
}


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


def verify_telegram_checkpoint_contract(agent_dir: Path) -> None:
    """Reject assembled runtimes that weaken custom Telegram checkpoints."""
    gateway_path = agent_dir / "gateway" / "run.py"
    if not gateway_path.exists():
        return

    try:
        source = gateway_path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(source, filename=str(gateway_path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise AssembledRuntimeContractError(
            f"cannot inspect Telegram checkpoint runtime: {exc}"
        ) from exc

    if _TELEGRAM_CHECKPOINT_MARKER not in source:
        raise AssembledRuntimeContractError(
            "assembled gateway is missing the Telegram checkpoint marker"
        )
    banned = sorted(copy for copy in _TELEGRAM_CHECKPOINT_BANNED_COPY if copy in source)
    if banned:
        raise AssembledRuntimeContractError(
            "assembled gateway restores canned Telegram checkpoint copy: "
            + ", ".join(repr(copy) for copy in banned)
        )

    compact = re.sub(r"\s+", " ", source)
    required_wiring = {
        "commentary capture": "ctx.model_checkpoint_updates.append(checkpoint_text)",
        "interim-message privacy boundary": "if not _want_interim_messages: return",
        "Telegram commentary callback": (
            "_want_interim_messages or ctx.source.platform == Platform.TELEGRAM"
        ),
        "Telegram heartbeat branch": "if source.platform == Platform.TELEGRAM:",
        "completed tool lifecycle": "model_checkpoint_tool_completed",
        "current tool lifecycle": "model_checkpoint_tool_current",
        "truthful empty interval": (
            "No new observable milestone completed in this interval."
        ),
        "monotonic checkpoint origin": "_notify_start = time.monotonic()",
        "scheduled checkpoint deadline": (
            "_notify_deadline = _notify_start + (_notify_tick * _NOTIFY_INTERVAL)"
        ),
        "commentary privacy filter": (
            "_sanitize_telegram_checkpoint_commentary_v2(piece)"
        ),
        "same-message Telegram failure boundary": (
            "_heartbeat_msg_id and source.platform == Platform.TELEGRAM"
        ),
    }
    missing_wiring = [
        label for label, snippet in required_wiring.items() if snippet not in compact
    ]
    if missing_wiring:
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint wiring is incomplete: "
            + ", ".join(missing_wiring)
        )

    notifier_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_notify_long_running"
    ]
    if len(notifier_nodes) != 1:
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint notifier is missing or ambiguous"
        )
    notifier = notifier_nodes[0]
    tick_initializers = [
        node
        for node in ast.walk(notifier)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "_notify_tick"
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    ]
    tick_increments = [
        node
        for node in ast.walk(notifier)
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_notify_tick"
    ]
    if (
        len(tick_initializers) != 1
        or len(tick_increments) != 1
        or tick_initializers[0].lineno >= tick_increments[0].lineno
    ):
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint tick must initialize inside the notifier "
            "before its first increment"
        )

    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    missing_calls = sorted(
        {
            "_capture_telegram_tool_checkpoint",
            "_format_telegram_model_checkpoint",
        }
        - calls
    )
    if missing_calls:
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint helpers are not wired: "
            + ", ".join(missing_calls)
        )

    helper_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in _TELEGRAM_CHECKPOINT_HELPERS
    ]
    helper_counts = {
        name: sum(node.name == name for node in helper_nodes)
        for name in _TELEGRAM_CHECKPOINT_HELPERS
    }
    invalid_helpers = sorted(
        name for name, count in helper_counts.items() if count != 1
    )
    if invalid_helpers:
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint helper set is incomplete or ambiguous: "
            + ", ".join(invalid_helpers)
        )

    helper_module = ast.Module(body=helper_nodes, type_ignores=[])
    namespace = {"_redact_gateway_user_facing_secrets": lambda value: value}
    try:
        exec(compile(helper_module, str(gateway_path), "exec"), namespace)
        formatter = namespace["_format_telegram_model_checkpoint"]
        task_label = namespace["_telegram_checkpoint_task_label"]
        capture = namespace["_capture_telegram_tool_checkpoint"]
        sanitize = namespace["_sanitize_telegram_checkpoint_commentary_v2"]

        factual = formatter(
            10,
            [],
            task="the verification",
            completed=["Ran the focused tests"],
            current=["Reviewing the pending changes"],
        )
        expected_factual = (
            "10 minutes in on the verification — quick update:\n"
            "• Ran the focused tests\n"
            "• Now: Reviewing the pending changes"
        )
        if factual != expected_factual:
            raise AssembledRuntimeContractError(
                "Telegram checkpoint factual summary semantics changed"
            )

        empty = formatter(10, ["  ", "```"], task=None)
        expected_empty = (
            "10 minutes in — quick update:\n"
            "• No new observable milestone completed in this interval."
        )
        if empty != expected_empty or any(
            copy in empty for copy in _TELEGRAM_CHECKPOINT_BANNED_COPY
        ):
            raise AssembledRuntimeContractError(
                "Telegram checkpoint empty-interval semantics changed"
            )
        if task_label("Can you please handle this request?") != "":
            raise AssembledRuntimeContractError(
                "Telegram checkpoint task labels can echo generic request content"
            )
        if sanitize("The focused regression tests now pass.") != (
            "The focused regression tests now pass."
        ) or sanitize("I ran python3 -m pytest /private/client/token.txt"):
            raise AssembledRuntimeContractError(
                "Telegram checkpoint commentary privacy semantics changed"
            )

        context = SimpleNamespace(
            model_checkpoint_lock=threading.Lock(),
            model_checkpoint_tool_active={},
            model_checkpoint_tool_current=[],
            model_checkpoint_tool_completed=[],
            _run_still_current=lambda: True,
        )
        raw_command = "python3 -m pytest /private/Customer-John/token-abc123"
        capture(context, "tool.started", "exec_command", raw_command, {})
        if context.model_checkpoint_tool_current != ["Running the focused tests"]:
            raise AssembledRuntimeContractError(
                "Telegram checkpoint current tool lifecycle semantics changed"
            )
        capture(
            context,
            "tool.completed",
            "exec_command",
            None,
            {"result": "private output token-abc123", "is_error": False},
        )
        if context.model_checkpoint_tool_current or (
            context.model_checkpoint_tool_completed != ["Ran the focused tests"]
        ):
            raise AssembledRuntimeContractError(
                "Telegram checkpoint completed tool lifecycle semantics changed"
            )
        retained = repr(context.__dict__)
        if any(
            private_fragment in retained
            for private_fragment in ("Customer-John", "token-abc123", "/private/")
        ):
            raise AssembledRuntimeContractError(
                "Telegram checkpoint state retains raw tool input or result content"
            )
    except AssembledRuntimeContractError:
        raise
    except Exception as exc:
        raise AssembledRuntimeContractError(
            f"Telegram checkpoint semantic probe failed: {exc}"
        ) from exc


def verify_conversation_loop_agent_contract(agent_dir: Path) -> None:
    """Verify incident-backed cross-file AIAgent contracts.

    This includes the explicit ``run_agent.py`` → ``agent_init.py`` forwarder
    signature and optional concrete-agent methods used by the conversation
    loop.
    """
    verify_agent_init_forwarder_contract(agent_dir)
    verify_telegram_checkpoint_contract(agent_dir)

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
