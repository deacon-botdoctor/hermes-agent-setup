"""Small fail-closed checks for cross-file runtime assembly contracts."""

from __future__ import annotations

import ast
import asyncio
import copy
from contextlib import suppress
import inspect
import os
import re
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace


class AssembledRuntimeContractError(RuntimeError):
    pass


def _is_native_d363(root: Path) -> bool:
    import subprocess
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "d3630f853239e8c41ce7201e09fbdf39bcbc5431"


_TELEGRAM_CHECKPOINT_MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2"
_KANBAN_DELEGATED_PROGRESS_MARKER = "HERMES_KANBAN_DELEGATED_PROGRESS_CHECKPOINTS_v1"
_TELEGRAM_CHECKPOINT_HELPERS = {
    "_telegram_checkpoint_task_label",
    "_telegram_checkpoint_preview_subject",
    "_telegram_checkpoint_tool_labels",
    "_capture_telegram_tool_checkpoint",
    "_telegram_checkpoint_activity_label",
    "_format_telegram_model_checkpoint",
    "_sanitize_telegram_checkpoint_commentary_v2",
    "_telegram_checkpoint_commentary_bullet_v2",
    "_telegram_checkpoint_minutes_v2",
}
_RESTART_RECOVERY_MARKER = "HERMES_RESTART_INTERRUPTION_CHECKIN_v7"
_RESTART_RECOVERY_FORBIDDEN_COPY = (
    "Do you still need me to finish it?",
    "I was working on",
    "I didn't repeat it",
    "I didn’t repeat it",
    "[CONTEXT COMPACTION",
)


class _CheckpointProbeStop(BaseException):
    pass


class _CheckpointProbeLoopGuard(ast.NodeTransformer):
    def _guard(self, node: ast.stmt) -> ast.Expr:
        return ast.copy_location(
            ast.Expr(
                value=ast.Call(
                    func=ast.Name(id="_checkpoint_probe_iteration", ctx=ast.Load()),
                    args=[],
                    keywords=[],
                )
            ),
            node,
        )

    def visit_While(self, node: ast.While):
        self.generic_visit(node)
        node.body.insert(0, self._guard(node))
        return node

    def visit_For(self, node: ast.For):
        self.generic_visit(node)
        node.body.insert(0, self._guard(node))
        return node

    def visit_AsyncFor(self, node: ast.AsyncFor):
        self.generic_visit(node)
        node.body.insert(0, self._guard(node))
        return node


def _probe_telegram_checkpoint_notifier(
    path: Path,
    notifier: ast.AsyncFunctionDef,
    helper_nodes: list[ast.FunctionDef],
) -> None:
    class Clock:
        def __init__(self):
            self.now = 0.0
            self.sleeps = []

        def monotonic(self):
            return self.now

        async def sleep(self, seconds):
            delay = float(seconds)
            self.sleeps.append(delay)
            if len(self.sleeps) == 4:
                raise _CheckpointProbeStop
            self.now += delay
            if len(self.sleeps) == 2:
                context.model_checkpoint_updates.append("The focused regression tests now pass.")
            elif len(self.sleeps) == 3:
                context.model_checkpoint_updates.append("The immutable candidate is ready for canary.")

    class Adapter:
        def __init__(self, clock):
            self.clock = clock
            self.sent = []
            self.edited = []

        async def send(self, chat_id, text, metadata=None, **_kwargs):
            self.sent.append((self.clock.now, chat_id, text, metadata))
            return SimpleNamespace(success=True, message_id=42)

        async def edit_message(self, chat_id, message_id, text, **_kwargs):
            self.edited.append((self.clock.now, chat_id, message_id, text))
            return SimpleNamespace(success=True)

    clock = Clock()
    adapter = Adapter(clock)
    source = SimpleNamespace(platform="telegram", chat_id="probe-chat")
    context = SimpleNamespace(
        model_checkpoint_lock=threading.Lock(),
        model_checkpoint_updates=[],
        model_checkpoint_cursor=[0],
        model_checkpoint_task="the verification",
        model_checkpoint_tool_active={},
        model_checkpoint_tool_completed=[],
        model_checkpoint_tool_cursor=[0],
        model_checkpoint_tool_current=[],
        _run_still_current=lambda: True,
    )
    iterations = 0

    def checkpoint_probe_iteration():
        nonlocal iterations
        iterations += 1
        # The notifier contains bounded inner loops that sanitize and select
        # checkpoint milestones. Guard pathological loops without confusing
        # those finite passes with the scheduled notifier iterations.
        if iterations > 64:
            raise _CheckpointProbeStop

    probe_notifier = _CheckpointProbeLoopGuard().visit(copy.deepcopy(notifier))
    native = notifier.name == "_run_agent_notify_long_running"
    if native:
        probe_notifier.returns = None
        for arg in [*probe_notifier.args.args, *probe_notifier.args.kwonlyargs]:
            arg.annotation = None
        probe_notifier.body = [n for n in probe_notifier.body if not (
            isinstance(n, ast.ImportFrom) and n.module == "gateway.run"
            and [a.name for a in n.names] == ["_float_env", "_interim_metadata", "_non_conversational_metadata"])]
    module = ast.Module(
        body=[*copy.deepcopy(helper_nodes), probe_notifier],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "Platform": SimpleNamespace(TELEGRAM="telegram"),
        "_NOTIFY_INTERVAL": 300.0,
        "_checkpoint_probe_iteration": checkpoint_probe_iteration,
        "_cleanup_msg_ids": [],
        "_cleanup_progress": False,
        "_executor_task": None,
        "_generic_status_phrase": lambda _kind: "Working",
        "_long_running_mode": "full",
        "_interim_metadata": lambda metadata: metadata,
        "_non_conversational_metadata": lambda metadata, **_kwargs: metadata,
        "_notify_start": 0.0,
        "_progress_on_typing": False,
        "_initial_heartbeat_done": SimpleNamespace(set=lambda: None),
        "_redact_gateway_user_facing_secrets": lambda value: value,
        "_status_thread_metadata": {},
        "agent_holder": [None],
        "asyncio": SimpleNamespace(sleep=clock.sleep),
        "logger": SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        "platform_key": "telegram",
        "resolve_display_setting": lambda *_args, **_kwargs: True,
        "self": SimpleNamespace(
            _adapter_for_source=lambda _source: adapter,
            _should_emit_long_running_notification=lambda *_args: True,
        ),
        "session_key": "probe-session",
        "source": source,
        "time": SimpleNamespace(monotonic=clock.monotonic),
        "turn_ctx": context,
        "user_config": {},
    }
    try:
        exec(compile(module, str(path), "exec"), namespace)
        capture = namespace["_capture_telegram_tool_checkpoint"]
        capture(context, "tool.started", "read_file", "release-notes.md", {})
        capture(context, "tool.completed", "read_file", None, {"is_error": False})
        capture(context, "tool.started", "write_file", "runtime-config.json", {})
        capture(context, "tool.completed", "write_file", None, {"is_error": True})
        if native:
            from contextlib import suppress
            namespace["suppress"] = suppress
            namespace["_float_env"] = lambda *_a: 180
            context.source, context.session_key, context.agent_holder = source, "probe-session", [None]
            context._cleanup_progress, context._cleanup_msg_ids, context._status_thread_metadata = False, [], {}
            display = SimpleNamespace(_display_surface_mode=lambda *_a, **_kw: "full")
            asyncio.run(namespace[notifier.name](namespace["self"], display, context, [None]))
        else:
            asyncio.run(namespace[notifier.name]())
    except (_CheckpointProbeStop, asyncio.CancelledError):
        pass
    except Exception as exc:
        raise AssembledRuntimeContractError(f"Telegram checkpoint notifier probe failed: {exc}") from exc
    expected_sent = "10 minutes in on the verification\n\nThe focused regression tests now pass."
    expected_edited = "15 minutes in on the verification\n\nThe immutable candidate is ready for canary."
    if (
        clock.sleeps != [300.0, 300.0, 300.0, 300.0]
        or context.model_checkpoint_tool_current
        or context.model_checkpoint_tool_completed != ["Reviewed a documentation file"]
        or len(adapter.sent) != 1
        or adapter.sent[0][:3] != (600.0, "probe-chat", expected_sent)
        or len(adapter.edited) != 1
        or adapter.edited[0] != (900.0, "probe-chat", "42", expected_edited)
    ):
        raise AssembledRuntimeContractError(
            "Telegram checkpoint notifier cadence or message reuse changed: "
            f"sleeps={clock.sleeps!r}, sent={adapter.sent!r}, edited={adapter.edited!r}"
        )


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
    """Bind only d363's canonical locals-comprehension forwarder, never arbitrary **kwargs."""
    import subprocess

    head = subprocess.run(["git", "-C", str(agent_dir), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=False)
    if head.returncode or head.stdout.strip() != "d3630f853239e8c41ce7201e09fbdf39bcbc5431":
        return None
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


def verify_telegram_checkpoint_contract(agent_dir: Path) -> None:
    """Reject assembled runtimes that weaken custom Telegram checkpoints."""
    native = _is_native_d363(agent_dir)
    gateway_path = agent_dir / "gateway" / ("run_turn.py" if native else "run.py")
    if not gateway_path.exists():
        return

    try:
        source = gateway_path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(source, filename=str(gateway_path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise AssembledRuntimeContractError(f"cannot inspect Telegram checkpoint runtime: {exc}") from exc

    if _TELEGRAM_CHECKPOINT_MARKER not in source:
        raise AssembledRuntimeContractError("assembled gateway is missing the Telegram checkpoint marker")

    wiring_source = source
    if native:
        wiring_source += (agent_dir / "gateway/run_turn_runner.py").read_text()
    compact = re.sub(r"\s+", " ", wiring_source)
    required_wiring = {
        "commentary capture": "ctx.model_checkpoint_updates.append(checkpoint_text)",
        "interim-message privacy boundary": "if not _want_interim_messages: return",
        "Telegram commentary callback": ("_want_interim_messages or ctx.source.platform == Platform.TELEGRAM"),
        "Telegram heartbeat branch": "if source.platform == Platform.TELEGRAM:",
        "completed tool lifecycle": "model_checkpoint_tool_completed",
        "current tool lifecycle": "model_checkpoint_tool_current",
        "monotonic checkpoint origin": "_notify_start = time.monotonic()",
        "scheduled checkpoint deadline": ("_notify_deadline = _notify_start + (_notify_tick * _NOTIFY_INTERVAL)"),
        "same-message Telegram failure boundary": ("_heartbeat_msg_id and source.platform == Platform.TELEGRAM"),
    }
    if native:
        required_wiring.update({
            "interim-message privacy boundary": "if not want_interim_messages: return",
            "Telegram commentary callback": "want_interim_messages or ctx.source.platform == Platform.TELEGRAM",
            "scheduled checkpoint deadline": "_deadline = _notify_start + (_notify_tick * _notify_interval)",
            "same-message Telegram failure boundary": "source.platform == Platform.TELEGRAM and not getattr(_notify_res, \"success\", False)",
        })
    missing_wiring = [label for label, snippet in required_wiring.items() if snippet not in compact]
    if missing_wiring:
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint wiring is incomplete: " + ", ".join(missing_wiring)
        )

    notifier_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == ("_run_agent_notify_long_running" if native else "_notify_long_running")
    ]
    if len(notifier_nodes) != 1:
        raise AssembledRuntimeContractError("assembled Telegram checkpoint notifier is missing or ambiguous")
    notifier = notifier_nodes[0]
    tick_initializers = [
        node
        for node in ast.walk(notifier)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "_notify_tick"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    tick_increments = [
        node
        for node in ast.walk(notifier)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "_notify_tick"
    ]
    if (
        len(tick_initializers) != 1
        or len(tick_increments) != 1
        or tick_initializers[0].lineno >= tick_increments[0].lineno
    ):
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint tick must initialize inside the notifier before its first increment"
        )

    calls = {node.func.id for node in ast.walk(ast.parse(wiring_source)) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    missing_calls = sorted(
        {
            "_capture_telegram_tool_checkpoint",
            "_format_telegram_model_checkpoint",
        }
        - calls
    )
    if missing_calls:
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint helpers are not wired: " + ", ".join(missing_calls)
        )

    helper_nodes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in _TELEGRAM_CHECKPOINT_HELPERS
    ]
    helper_counts = {name: sum(node.name == name for node in helper_nodes) for name in _TELEGRAM_CHECKPOINT_HELPERS}
    invalid_helpers = sorted(name for name, count in helper_counts.items() if count != 1)
    if invalid_helpers:
        raise AssembledRuntimeContractError(
            "assembled Telegram checkpoint helper set is incomplete or ambiguous: " + ", ".join(invalid_helpers)
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
            ["I found the stale checkpoint fallback and I’m removing it now."],
            task="the verification",
            completed=["Ran the focused tests"],
            current=["Reviewing the pending changes"],
        )
        expected_factual = (
            "10 minutes in on the verification\n\nI found the stale checkpoint fallback and I’m removing it now."
        )
        if factual != expected_factual:
            raise AssembledRuntimeContractError("Telegram checkpoint factual summary semantics changed")

        telemetry_only = formatter(
            10,
            [],
            task="the verification",
            completed=["Reviewed a documentation file", "Updated a JSON file"],
            current=["Reviewing a JSON file"],
        )
        if telemetry_only != "":
            raise AssembledRuntimeContractError("Telegram checkpoint renders tool telemetry as agent copy")

        empty = formatter(10, ["  ", "```"], task=None)
        if empty != "":
            raise AssembledRuntimeContractError("Telegram checkpoint empty-interval semantics changed")
        if task_label("Can you please handle this request?") != "":
            raise AssembledRuntimeContractError("Telegram checkpoint task labels can echo generic request content")
        if sanitize("The focused regression tests now pass.") != ("The focused regression tests now pass.") or sanitize(
            "I ran python3 -m pytest /private/client/token.txt"
        ):
            raise AssembledRuntimeContractError("Telegram checkpoint commentary privacy semantics changed")

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
            raise AssembledRuntimeContractError("Telegram checkpoint current tool lifecycle semantics changed")
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
            raise AssembledRuntimeContractError("Telegram checkpoint completed tool lifecycle semantics changed")
        retained = repr(context.__dict__)
        if any(private_fragment in retained for private_fragment in ("Customer-John", "token-abc123", "/private/")):
            raise AssembledRuntimeContractError("Telegram checkpoint state retains raw tool input or result content")
        capture(context, "tool.started", "write_file", "runtime-config.json", {})
        capture(context, "tool.completed", "write_file", None, {"is_error": True})
        if context.model_checkpoint_tool_current or (
            context.model_checkpoint_tool_completed != ["Ran the focused tests"]
        ):
            raise AssembledRuntimeContractError("Telegram checkpoint failed tool lifecycle semantics changed")
        _probe_telegram_checkpoint_notifier(
            gateway_path,
            notifier,
            helper_nodes,
        )
    except AssembledRuntimeContractError:
        raise
    except Exception as exc:
        raise AssembledRuntimeContractError(f"Telegram checkpoint semantic probe failed: {exc}") from exc


def verify_restart_recovery_contract(agent_dir: Path) -> None:
    """Reject client-visible or replaying restart recovery in assembled Golden."""
    if _is_native_d363(agent_dir):
        path = agent_dir / "gateway/run_startup.py"
        tree = ast.parse(path.read_text())
        schedulers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_schedule_resume_pending_sessions"]
        if len(schedulers) != 1:
            raise AssembledRuntimeContractError("native restart scheduler missing or ambiguous")
        body = schedulers[0].body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if len(body) != 1 or not isinstance(body[0], ast.Return) or not isinstance(body[0].value, ast.Constant) or body[0].value.value != 0:
            raise AssembledRuntimeContractError("native restart scheduler emits, clears, or replays ambiguous work")
        return
    gateway_path = agent_dir / "gateway" / "run.py"
    if not gateway_path.exists():
        return

    try:
        source = gateway_path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(source, filename=str(gateway_path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise AssembledRuntimeContractError(f"cannot inspect restart recovery runtime: {exc}") from exc

    if _RESTART_RECOVERY_MARKER not in source:
        raise AssembledRuntimeContractError("assembled gateway is missing the silent restart recovery marker")

    leaked = [phrase for phrase in _RESTART_RECOVERY_FORBIDDEN_COPY if phrase in source]
    if leaked:
        raise AssembledRuntimeContractError(
            "restart recovery contains client-visible infrastructure copy: " + ", ".join(leaked)
        )

    helpers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_restart_interruption_checkin"
    ]
    schedulers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_schedule_resume_pending_sessions"
    ]
    if len(helpers) != 1 or len(schedulers) != 1:
        raise AssembledRuntimeContractError("restart recovery helper or scheduler is missing or ambiguous")

    helper = helpers[0]
    helper_calls = {
        node.func.attr
        for node in ast.walk(helper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    helper_assignments = {
        target.attr
        for node in ast.walk(helper)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute)
    }
    unsafe_helper_actions = sorted(
        ({"send", "clear_resume_pending", "handle_message"} & helper_calls)
        | ({"resume_pending", "resume_reason"} & helper_assignments)
    )
    if unsafe_helper_actions:
        raise AssembledRuntimeContractError(
            "restart recovery emits, clears, or replays ambiguous work: " + ", ".join(unsafe_helper_actions)
        )

    scheduler_calls = {
        node.func.attr
        for node in ast.walk(schedulers[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    if "_send_restart_interruption_checkin" not in scheduler_calls:
        raise AssembledRuntimeContractError("restart recovery scheduler is not wired to the silent evaluator")
    if "_run_startup_resume_event" in scheduler_calls:
        raise AssembledRuntimeContractError("restart recovery blindly replays an interrupted active turn")


def verify_native_session_liveness_contract(agent_dir: Path) -> None:
    """Keep Hermes' native stale-session recovery intact after composition."""
    adapter_path = agent_dir / "gateway" / "platforms" / "base.py"
    gateway_path = agent_dir / "gateway" / "run.py"
    if not adapter_path.exists() and not gateway_path.exists():
        return
    if not adapter_path.exists() or not gateway_path.exists():
        raise AssembledRuntimeContractError("gateway/platforms/base.py and gateway/run.py must be verified together")

    try:
        adapter_tree = ast.parse(
            adapter_path.read_text(encoding="utf-8", errors="strict"),
            filename=str(adapter_path),
        )
        gateway_tree = ast.parse(
            gateway_path.read_text(encoding="utf-8", errors="strict"),
            filename=str(gateway_path),
        )
        if _is_native_d363(agent_dir):
            owner = _runtime_class(gateway_tree, "GatewayRunner", gateway_path)
            for relative in ("gateway/run_agent_cache.py", "gateway/run_inbound.py"):
                part = ast.parse((agent_dir / relative).read_text())
                owner.body.extend(method for cls in part.body if isinstance(cls, ast.ClassDef)
                                  for method in cls.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)))
        _probe_adapter_stale_lock_recovery(adapter_path, adapter_tree)
        _probe_gateway_stale_state_eviction(gateway_path, gateway_tree)
    except AssembledRuntimeContractError:
        raise
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise AssembledRuntimeContractError(f"cannot inspect native stale-session recovery: {exc}") from exc


def _runtime_class(tree: ast.Module, name: str, path: Path) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    if len(matches) != 1:
        raise AssembledRuntimeContractError(f"{path} must define exactly one {name} class")
    return matches[0]


def _runtime_method(
    class_node: ast.ClassDef,
    name: str,
    path: Path,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if len(matches) != 1:
        raise AssembledRuntimeContractError(f"{path} must define exactly one {class_node.name}.{name} method")
    return matches[0]


def _compile_probe_class(
    path: Path,
    class_name: str,
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef],
    namespace: dict,
):
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            ast.ClassDef(
                name=class_name,
                bases=[],
                keywords=[],
                body=methods,
                decorator_list=[],
            ),
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[class_name]


def _probe_adapter_stale_lock_recovery(path: Path, tree: ast.Module) -> None:
    # Native lifecycle methods now retire their real event-owned telemetry
    # carrier. Resolve that dependency from the assembled tree, not the
    # operator's interpreter or a no-op substitute, then restore import state.
    root = path.parents[2]
    names = ("agent", "agent.runtime_performance_events", "hermes_constants")
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in names}
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, str(root))
        for name in names:
            sys.modules.pop(name, None)
        _probe_adapter_stale_lock_recovery_inner(path, tree)
    finally:
        sys.path[:] = previous_path
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _probe_adapter_stale_lock_recovery_inner(path: Path, tree: ast.Module) -> None:
    class_node = _runtime_class(tree, "BasePlatformAdapter", path)
    methods = [
        _runtime_method(class_node, name, path)
        for name in (
            "_session_task_is_stale",
            "_heal_stale_session_lock",
            "handle_message",
        )
    ]
    if any(isinstance(n, ast.FunctionDef) and n.name == "_event_session_key" for n in class_node.body):
        methods.extend(_runtime_method(class_node, name, path) for name in
                       ("_preflight_startup_gate", "_event_session_key", "_handle_message_while_active"))
    namespace = {
        "asyncio": asyncio,
        "logger": SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
        "Platform": SimpleNamespace(TELEGRAM="telegram"),
        "coerce_plaintext_gateway_command": lambda _event: None,
        "build_session_key": lambda _source, **_kwargs: "probe-session",
    }
    probe_class = _compile_probe_class(path, "BasePlatformAdapter", methods, namespace)
    adapter = object.__new__(probe_class)
    adapter.name = "contract-probe"
    adapter.config = SimpleNamespace(extra={})
    adapter._message_handler = object()
    adapter._topic_recovery_fn = None
    adapter._session_key_profile = lambda _source: None
    discarded = []
    started = []
    adapter._discard_text_debounce = discarded.append
    adapter._start_session_processing = lambda event, session_key: started.append((event, session_key))
    live_guard = object()
    live_pending = object()
    live_task = SimpleNamespace(done=lambda: False)
    adapter._active_sessions = {"probe-session": live_guard}
    adapter._pending_messages = {"probe-session": live_pending}
    adapter._session_tasks = {"probe-session": live_task}
    if (
        adapter._heal_stale_session_lock("probe-session")
        or adapter._active_sessions != {"probe-session": live_guard}
        or adapter._pending_messages != {"probe-session": live_pending}
        or adapter._session_tasks != {"probe-session": live_task}
        or discarded
        or started
    ):
        raise AssembledRuntimeContractError("adapter live owner was released or redispatched")

    async def busy_session_handler(_event, _session_key):
        return True

    adapter._busy_session_handler = busy_session_handler
    live_event = SimpleNamespace(
        source=SimpleNamespace(platform="other", chat_type="dm"),
        _hermes_startup_gate_checked=True,
        admission_checked=True,
        durable_replay=False,
        allow_gateway_control=True,
        metadata={},
        get_command=lambda: "probe",
    )
    commands = ModuleType("hermes_cli.commands")
    commands.is_interrupt_then_dispatch = lambda _cmd: False
    commands.should_bypass_active_session = lambda _cmd: False
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    missing_module = object()
    previous_modules = {name: sys.modules.get(name, missing_module) for name in ("hermes_cli", "hermes_cli.commands")}
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.commands"] = commands
    try:
        coroutine = adapter.handle_message(live_event)
        try:
            coroutine.send(None)
        except StopIteration:
            pass
        else:
            coroutine.close()
            raise AssembledRuntimeContractError("adapter live-owner handler probe unexpectedly suspended")
    except AssembledRuntimeContractError:
        raise
    except Exception as exc:
        raise AssembledRuntimeContractError(f"adapter live-owner handler probe failed: {exc}") from exc
    finally:
        for name, previous in previous_modules.items():
            if previous is missing_module:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    if (
        adapter._active_sessions != {"probe-session": live_guard}
        or adapter._pending_messages != {"probe-session": live_pending}
        or adapter._session_tasks != {"probe-session": live_task}
        or discarded
        or started
    ):
        raise AssembledRuntimeContractError("adapter live owner was released or redispatched")
    adapter._active_sessions = {"probe-session": object()}
    adapter._pending_messages = {"probe-session": object()}
    adapter._session_tasks = {"probe-session": SimpleNamespace(done=lambda: True)}
    event = SimpleNamespace(
        source=SimpleNamespace(platform="other", chat_type="dm"),
        _hermes_startup_gate_checked=True,
        admission_checked=True,
        durable_replay=False,
        allow_gateway_control=True,
        metadata={},
    )
    try:
        coroutine = adapter.handle_message(event)
        try:
            coroutine.send(None)
        except StopIteration:
            pass
        else:
            coroutine.close()
            raise AssembledRuntimeContractError("adapter inbound stale-lock probe unexpectedly suspended")
    except AssembledRuntimeContractError:
        raise
    except Exception as exc:
        raise AssembledRuntimeContractError(f"adapter inbound stale-lock healing probe failed: {exc}") from exc
    if (
        adapter._active_sessions
        or adapter._pending_messages
        or adapter._session_tasks
        or discarded != ["probe-session"]
        or len(started) != 1
        or started[0][1] != "probe-session"
    ):
        raise AssembledRuntimeContractError("adapter inbound stale-lock healing did not release and redispatch")


def _assigns_name(node: ast.stmt, name: str) -> bool:
    return any(
        isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Store) and candidate.id == name
        for candidate in ast.walk(node)
    )


def _calls_method(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Attribute) and candidate.func.attr == name
        for candidate in ast.walk(node)
    )


def _probe_gateway_stale_state_eviction(path: Path, tree: ast.Module) -> None:
    class_node = _runtime_class(tree, "GatewayRunner", path)
    release = _runtime_method(class_node, "_release_running_agent_state", path)
    handlers = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(candidate, ast.Constant) and candidate.value == "stale_running_agent_eviction"
            for candidate in ast.walk(node)
        )
    ]
    if len(handlers) != 1:
        raise AssembledRuntimeContractError(f"{path} must contain exactly one stale running-agent eviction path")
    handler = handlers[0]
    start = next(
        (index for index, statement in enumerate(handler.body) if _assigns_name(statement, "_raw_stale_timeout")),
        None,
    )
    if start is None:
        raise AssembledRuntimeContractError("gateway stale eviction is missing its inactivity threshold")
    probe_body = []
    for statement in handler.body[start:]:
        probe_body.append(statement)
        if _calls_method(statement, "_release_running_agent_state") or _calls_method(statement, "_hm_evict_running_agent"):
            break
    if not probe_body or not any(_calls_method(ast.Module(body=probe_body, type_ignores=[]), name)
                                 for name in ("_release_running_agent_state", "_hm_evict_running_agent")):
        raise AssembledRuntimeContractError("gateway stale eviction is not connected to running-state cleanup")
    probe = ast.parse("def _probe_stale_eviction(self, _quick_key):\n    pass\n").body[0]
    probe.body = probe_body
    namespace = {
        "_float_env": lambda _name, _default: 30.0,
        "_AGENT_PENDING_SENTINEL": object(),
        # The extracted probe intentionally runs without importing the entire
        # candidate as a package. Use an age beyond the native emergency TTL
        # so the atomic invalidation/release contract remains testable even
        # when the candidate-local activity resolver is not importable here.
        "time": SimpleNamespace(time=lambda: 10000.0),
        "suppress": suppress,
        "logger": SimpleNamespace(warning=lambda *_args, **_kwargs: None, debug=lambda *_args, **_kwargs: None),
    }
    probe_class = _compile_probe_class(
        path,
        "GatewayRunner",
        [release, probe, *[m for m in class_node.body if isinstance(m, ast.FunctionDef) and m.name == "_hm_evict_running_agent"]],
        namespace,
    )

    class Lease:
        released = False

        def release(self):
            self.released = True

    class Turn:
        def __init__(self):
            self.agent = SimpleNamespace(
                get_activity_summary=lambda: {
                    "seconds_since_activity": 31.0,
                    "last_activity_desc": "idle",
                    "api_call_count": 1,
                    "max_iterations": 10,
                }
            )
            self.started_ts = 900.0
            self.lease = Lease()
            self.cleared = False

        def clear(self):
            self.cleared = True
            self.agent = None
            self.started_ts = 0
            self.lease = None

    turn = Turn()
    lease = turn.lease
    state = SimpleNamespace(turn=turn)
    invalidations = []
    persisted = []
    runner = object.__new__(probe_class)
    runner._peek_session_state = lambda _key: state
    runner._is_session_run_current = lambda _key, _generation: True
    runner._invalidate_session_run_generation = lambda key, *, reason: invalidations.append((key, reason))
    runner._persist_active_agents = lambda: persisted.append(True)
    try:
        runner._probe_stale_eviction("probe-session")
    except Exception as exc:
        raise AssembledRuntimeContractError(f"gateway stale-state eviction probe failed: {exc}") from exc
    if (
        invalidations != [("probe-session", "stale_running_agent_eviction")]
        or not turn.cleared
        or not lease.released
        or not persisted
    ):
        raise AssembledRuntimeContractError("gateway stale-state eviction did not invalidate and release atomically")


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


def verify_kanban_delegated_progress_contract(agent_dir: Path) -> None:
    """Reject assemblies that weaken delegated Telegram progress invariants."""
    watcher_path = agent_dir / "gateway" / "kanban_watchers.py"
    db_path = agent_dir / "hermes_cli" / "kanban_db.py"
    run_path = agent_dir / "gateway" / "run.py"
    present = [path.exists() for path in (watcher_path, db_path)]
    if not any(present):
        return
    if not all(present):
        raise AssembledRuntimeContractError(
            "delegated Kanban progress DB and watcher must be assembled together"
        )
    native = _is_native_d363(agent_dir)
    watcher = watcher_path.read_text(encoding="utf-8", errors="strict")
    database = db_path.read_text(encoding="utf-8", errors="strict")
    if native:
        watcher += "\n" + (agent_dir / "gateway/kanban_watchers_notifier.py").read_text()
        database += "\n" + (agent_dir / "hermes_cli/kanban_db_notify.py").read_text()
        database += "\n" + (agent_dir / "hermes_cli/kanban_db_connect.py").read_text()
    if (
        _KANBAN_DELEGATED_PROGRESS_MARKER not in watcher
        or _KANBAN_DELEGATED_PROGRESS_MARKER not in database
    ):
        raise AssembledRuntimeContractError(
            "delegated Kanban progress marker is missing from DB or watcher"
        )
    required_watcher = {
        "five-minute cadence": "_KANBAN_DELEGATED_PROGRESS_INTERVAL_SECONDS = 300",
        "heartbeat-backed claim": "claim_due_notify_progress(",
        "same-message edit": "result = await adapter.edit_message(",
        "failed-edit no-fanout": "edit failed; retaining the existing progress message id",
        "delivery acknowledgement": "self._kanban_complete_progress,",
        "pre-delivery run revalidation": "self._kanban_progress_claim_current,",
        "delivery rewind": "self.runner._kanban_rewind_progress," if native else "self._kanban_rewind_progress,",
    }
    missing = [label for label, seam in required_watcher.items() if seam not in watcher]
    if missing:
        raise AssembledRuntimeContractError(
            "delegated Kanban progress watcher wiring is incomplete: "
            + ", ".join(missing)
        )
    required_db = {
        "independent progress cursor": "progress_last_event_id INTEGER NOT NULL DEFAULT 0",
        "durable message id": "progress_message_id TEXT",
        "active-run clock": "JOIN task_runs r ON r.id = t.current_run_id",
        "active-run heartbeat fence": "run_id = ? AND kind = 'heartbeat'",
        "pre-delivery run revalidation": "notify_progress_claim_is_current(",
        "minimum cadence floor": "interval = max(300, int(interval_seconds))",
        "running-task gate": 'task["status"] != "running"',
    }
    missing = [label for label, seam in required_db.items() if seam not in database]
    if missing:
        raise AssembledRuntimeContractError(
            "delegated Kanban progress DB wiring is incomplete: "
            + ", ".join(missing)
        )

    try:
        tree = ast.parse(watcher)
    except SyntaxError as exc:
        raise AssembledRuntimeContractError(
            f"cannot inspect delegated Kanban progress watcher: {exc}"
        ) from exc
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    notifier = functions.get("_kanban_notifier_watcher")
    dispatcher = functions.get("_kanban_dispatcher_watcher")
    progress_delivery = functions.get("_deliver_kanban_progress")
    if not notifier or not dispatcher or not progress_delivery:
        raise AssembledRuntimeContractError(
            "delegated Kanban progress requires notifier, dispatcher, and delivery methods"
        )
    notifier_text = ast.get_source_segment(watcher, notifier) or ""
    dispatcher_text = ast.get_source_segment(watcher, dispatcher) or ""
    progress_text = ast.get_source_segment(watcher, progress_delivery) or ""
    if "dispatch_in_gateway" in notifier_text:
        raise AssembledRuntimeContractError(
            "Kanban notifier is incorrectly gated by dispatcher activation"
        )
    if native:
        boot = functions.get("_kanban_dispatcher_boot")
        if boot is None or "self._kanban_dispatcher_boot()" not in dispatcher_text:
            raise AssembledRuntimeContractError("native Kanban dispatcher boot gate missing")
        dispatcher_text += ast.get_source_segment(watcher, boot) or ""
    if 'kanban_cfg.get("dispatch_in_gateway", True)' not in dispatcher_text:
        raise AssembledRuntimeContractError(
            "Kanban dispatcher activation gate is missing"
        )
    if "deliver_wake" in progress_text:
        raise AssembledRuntimeContractError(
            "routine delegated progress must not wake the coordinator model"
        )
    edit_index = progress_text.find("await adapter.edit_message(")
    else_index = progress_text.find("\n        else:", edit_index)
    if edit_index < 0 or else_index < 0:
        raise AssembledRuntimeContractError(
            "delegated progress edit/send branches are ambiguous"
        )
    if "adapter.send(" in progress_text[edit_index:else_index]:
        raise AssembledRuntimeContractError(
            "failed delegated progress edits may fan out into duplicate sends"
        )
    if re.search(r"_WAKE_KINDS\s*=\s*\([^)]*heartbeat", watcher, re.DOTALL):
        raise AssembledRuntimeContractError(
            "heartbeat progress entered the coordinator wake set"
        )

    if native:
        startup = ast.parse((agent_dir / "gateway/run_startup.py").read_text())
        lists = [n for n in ast.walk(startup) if isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "_PRE_RECONNECT_WATCHERS" for x in n.targets)]
        if len(lists) != 1:
            raise AssembledRuntimeContractError("native Kanban watcher startup list missing or ambiguous")
        names = ast.literal_eval(lists[0].value)
        if names.count("_kanban_notifier_watcher") != 1 or names.count("_kanban_dispatcher_watcher") != 1 or names.index("_kanban_notifier_watcher") > names.index("_kanban_dispatcher_watcher"):
            raise AssembledRuntimeContractError("native Kanban startup ownership changed")
        methods = [n for n in ast.walk(startup) if isinstance(n, ast.FunctionDef) and n.name == "_start_spawn_background_watchers"]
        if len(methods) != 1 or 'for method in self._PRE_RECONNECT_WATCHERS:' not in ast.unparse(methods[0]) or 'self._spawn_supervised(getattr(self, method), method[1:])' not in ast.unparse(methods[0]):
            raise AssembledRuntimeContractError("native Kanban supervised startup wiring changed")
    elif run_path.exists():
        run_text = run_path.read_text(encoding="utf-8", errors="strict")
        notifier_spawn = "self._spawn_supervised(self._kanban_notifier_watcher"
        dispatcher_spawn = "self._spawn_supervised(self._kanban_dispatcher_watcher"
        if (
            run_text.count(notifier_spawn) != 1
            or run_text.count(dispatcher_spawn) != 1
            or run_text.index(notifier_spawn) > run_text.index(dispatcher_spawn)
        ):
            raise AssembledRuntimeContractError(
                "Kanban notifier/dispatcher startup ownership changed"
            )


def verify_conversation_loop_agent_contract(agent_dir: Path) -> None:
    """Verify incident-backed cross-file AIAgent contracts.

    This includes the explicit ``run_agent.py`` → ``agent_init.py`` forwarder
    signature and optional concrete-agent methods used by the conversation
    loop.
    """
    verify_agent_init_forwarder_contract(agent_dir)
    verify_telegram_checkpoint_contract(agent_dir)
    verify_kanban_delegated_progress_contract(agent_dir)
    verify_restart_recovery_contract(agent_dir)
    verify_native_session_liveness_contract(agent_dir)
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
