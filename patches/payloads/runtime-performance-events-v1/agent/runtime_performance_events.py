"""Content-free, candidate-bound runtime performance events.

The gateway and physical LLM-attempt carrier call this module in-process.  It
records counts, timestamps, and immutable runtime identity only; prompt,
message, response, tool argument, and tool-result bodies are never serialized.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

SCHEMA_VERSION = "botdoctor.runtime-performance-event.v1"
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")
_EVENT_NAMES = frozenset(
    {
        "inbound_received",
        "typing_indicator_started",
        "model_request_started",
        "first_model_byte",
        "model_request_complete",
        "first_visible_response_chunk",
        "response_complete",
        "response_sent",
    }
)
_PAYLOAD_KEYS = frozenset(
    {
        "baseline_system",
        "tool_schemas",
        "context_injections",
        "estimated_request",
        "skills",
        "mcp_schemas",
        "tool_results",
        "selector_controls",
    }
)
_WRITE_LOCK = threading.Lock()


@dataclass
class _TurnTrace:
    turn_id: str
    platform: str
    emitted: set[str] = field(default_factory=set)
    first_model_bytes: set[str] = field(default_factory=set)
    lock: Any = field(default_factory=threading.Lock)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    deferred: bool = False
    discarded: bool = False
    delivery_state: int = 0  # 0: outside response owner, 1: active, 2: finished
    delivery_failed: bool = False
    delivery_acks: int = 0
    delivery_event: dict[str, Any] | None = None


_TURN: contextvars.ContextVar[_TurnTrace | None] = contextvars.ContextVar(
    "runtime_performance_turn", default=None
)


def _journal_path() -> Path:
    override = os.getenv("HERMES_RUNTIME_PERFORMANCE_LEDGER")
    if override:
        return Path(override)
    return get_hermes_home() / "state" / "observability" / "fleet-runtime-events.jsonl"


def _open_private_journal(path: Path) -> int:
    home = Path(get_hermes_home()).resolve()
    parent = path.parent.resolve()
    try:
        parent.relative_to(home)
    except ValueError as exc:
        raise ValueError("performance journal must remain inside HERMES_HOME") from exc
    if path.is_symlink():
        raise ValueError("performance journal cannot be a symlink")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("performance journal must be a regular file")
    os.fchmod(descriptor, 0o600)
    return descriptor


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _process_started_at() -> float:
    import psutil

    return float(psutil.Process(os.getpid()).create_time())


def _load_runtime_identity() -> dict[str, Any]:
    home = Path(get_hermes_home()).resolve()
    binding_path = home / "state" / "runtime-binding.json"
    if (
        binding_path.is_symlink()
        or not binding_path.is_file()
        or binding_path.resolve() != binding_path
    ):
        raise ValueError("active runtime binding is unavailable")
    binding_raw = binding_path.read_bytes()
    binding = json.loads(binding_raw)
    if not isinstance(binding, dict):
        raise ValueError("active runtime binding is invalid")
    release = binding.get("release")
    identity = binding.get("identity")
    process = binding.get("process")
    service = binding.get("service")
    runtime_root = Path(str(binding.get("runtime_root") or ""))
    runtime_python = Path(str(binding.get("runtime_python") or ""))
    expected_runtime_python = runtime_root / (
        "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"
    )
    if (
        binding.get("schema_version") != 1
        or binding.get("kind") != "botdoctor_runtime_binding"
        or binding.get("status") != "active"
        or binding.get("hermes_home") != str(home)
        or not runtime_root.is_absolute()
        or not runtime_root.is_dir()
        or runtime_root.resolve() != runtime_root
        or not runtime_python.is_absolute()
        or not runtime_python.is_file()
        or runtime_python != expected_runtime_python
        or not isinstance(release, dict)
        or not isinstance(identity, dict)
        or not isinstance(process, dict)
        or not isinstance(service, dict)
    ):
        raise ValueError("active runtime binding is incomplete")
    try:
        Path(__file__).resolve().relative_to(runtime_root.resolve())
        runtime_python.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError("performance producer is outside the active runtime") from exc
    try:
        if not runtime_python.samefile(Path(sys.executable)):
            raise ValueError("active runtime interpreter does not match the process")
    except OSError as exc:
        raise ValueError("active runtime interpreter is unavailable") from exc

    base_sha = str(release.get("base_sha") or "")
    target_sha = str(release.get("target_sha") or "")
    fingerprint = str(binding.get("runtime_fingerprint_digest") or "")
    if (
        not _SHA40.fullmatch(base_sha)
        or not _SHA40.fullmatch(target_sha)
        or binding.get("target_sha") != target_sha
        or not _SHA64.fullmatch(fingerprint)
    ):
        raise ValueError("active runtime release identity is invalid")

    role = str(identity.get("role") or "")
    agent_id = str(identity.get("agent_id") or "")
    host = str(identity.get("host") or "")
    if (
        set(identity) != {"agent_id", "host", "role"}
        or role not in {"enoch", "ordinary_client"}
        or not agent_id
        or not host
    ):
        raise ValueError("active runtime principal identity is invalid")

    pid = process.get("pid")
    started_at = process.get("started_at")
    generation = str(process.get("generation") or "")
    if (
        set(process) != {"generation", "pid", "started_at"}
        or type(pid) is not int
        or pid != os.getpid()
        or not isinstance(started_at, (int, float))
        or isinstance(started_at, bool)
        or abs(float(started_at) - _process_started_at()) > 2.0
        or not _SHA64.fullmatch(generation)
    ):
        raise ValueError("active runtime process generation is invalid")

    launchers = service.get("launchers")
    if not isinstance(launchers, list) or not launchers:
        raise ValueError("active runtime launcher binding is invalid")
    launcher_rows: list[tuple[str, str]] = []
    for row in launchers:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ValueError("active runtime launcher binding is invalid")
        path = Path(str(row.get("path") or ""))
        expected = str(row.get("sha256") or "")
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.resolve() != path
            or not _SHA64.fullmatch(expected)
            or _sha256(path) != expected
        ):
            raise ValueError("active runtime launcher binding drifted")
        launcher_rows.append((str(path), expected))
    expected_generation = hashlib.sha256(
        json.dumps(
            {
                "pid": pid,
                "started_at": float(started_at),
                "launchers": launcher_rows,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if generation != expected_generation:
        raise ValueError("active runtime process generation does not match its launcher")

    return {
        "agent_id": agent_id,
        "base_sha": base_sha,
        "host": host,
        "process_generation": generation,
        "role": role,
        "runtime_binding_sha256": hashlib.sha256(binding_raw).hexdigest(),
        "runtime_fingerprint": fingerprint,
        "runtime_root": str(runtime_root),
        "target_sha": target_sha,
    }


def _append(event: dict[str, Any]) -> bool:
    try:
        identity = _load_runtime_identity()
        payload = {
            "schema_version": SCHEMA_VERSION,
            **identity,
            **event,
        }
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        with _WRITE_LOCK:
            descriptor = _open_private_journal(path)
            try:
                os.write(descriptor, encoded)
            finally:
                os.close(descriptor)
        return True
    except Exception:
        # Telemetry must never turn a successful client response into a retry.
        # The promotion collector fails closed on missing coverage instead.
        return False


def begin_gateway_turn(platform: str, *, defer_publication: bool = False) -> str:
    trace = _TurnTrace(
        turn_id=uuid.uuid4().hex,
        platform=str(platform or "unknown"),
        deferred=defer_publication,
    )
    _TURN.set(trace)
    record_turn_event("inbound_received")
    return trace.turn_id


def reconcile_gateway_events(existing: Any, incoming: Any) -> bool:
    """Reconcile only events the native queue has actually decided to merge.

    Telegram admission is provisional until model dispatch. Keep the trace
    owned by the surviving typing task: asyncio tasks retain their original
    ContextVar even when an event's attributes are reassigned. Never relabel
    published evidence or change the upstream queue/content decision.
    """
    left = getattr(existing, "_hermes_runtime_performance_trace", None)
    right = getattr(incoming, "_hermes_runtime_performance_trace", None)
    left_task = getattr(existing, "_typing_receipt_task", None)
    right_task = getattr(incoming, "_typing_receipt_task", None)

    def live(task: Any) -> bool:
        return task is not None and not task.done() and not task.cancelling()

    if live(left_task) and isinstance(left, _TurnTrace):
        winner, retained = left, left_task
    elif live(right_task) and isinstance(right, _TurnTrace):
        winner, retained = right, right_task
    else:
        winner = left if isinstance(left, _TurnTrace) else right
        retained = None
    if not isinstance(winner, _TurnTrace):
        return False
    loser = right if winner is left else left
    if isinstance(loser, _TurnTrace) and loser is not winner:
        # The queue path runs synchronously on the gateway loop. Ordered locks
        # also protect the explicit carrier at the executor boundary.
        first, second = sorted((winner, loser), key=id)
        with first.lock, second.lock:
            if (
                not winner.deferred or not loser.deferred
                or winner.discarded or loser.discarded
                or winner.platform != loser.platform
            ):
                winner.discarded = loser.discarded = True
                winner.pending.clear()
                loser.pending.clear()
            else:
                for name, row in loser.pending.items():
                    prior = winner.pending.get(name)
                    if prior is None or row["monotonic_ns"] < prior["monotonic_ns"]:
                        winner.pending[name] = {**row, "turn_id": winner.turn_id}
                loser.pending.clear()
                loser.discarded = True
    for task in (left_task, right_task):
        if task is not retained and live(task):
            task.cancel()
    for event in (existing, incoming):
        event._hermes_runtime_performance_trace = winner
        event._hermes_runtime_performance_turn_id = winner.turn_id
        event._hermes_runtime_performance_platform = winner.platform
        if retained is not None:
            event._typing_receipt_task = retained
        elif hasattr(event, "_typing_receipt_task"):
            del event._typing_receipt_task
    return not winner.discarded


def retire_gateway_event(event: Any, *, discard: bool = False) -> None:
    """Release this consumed, dropped, cleared, or completed event's typing owner."""
    task = getattr(event, "_typing_receipt_task", None)
    if task is not None and not task.done():
        task.cancel()
    trace = getattr(event, "_hermes_runtime_performance_trace", None)
    if isinstance(trace, _TurnTrace):
        with trace.lock:
            if discard or trace.deferred:
                trace.discarded = True
                trace.pending.clear()


def _publish_admission(trace: _TurnTrace) -> bool:
    """Publish measured admission timestamps only at actual model dispatch."""
    with trace.lock:
        if trace.discarded:
            return False
        if not trace.deferred:
            return True
        for name, row in sorted(
            trace.pending.items(), key=lambda item: item[1]["monotonic_ns"]
        ):
            if name not in trace.emitted:
                if not _append(row):
                    trace.discarded = True
                    trace.pending.clear()
                    return False
                trace.emitted.add(name)
        trace.pending.clear()
        trace.deferred = False
        return True


def current_gateway_trace() -> Any:
    """Return the in-process carrier for one exact gateway turn.

    The carrier is intentionally not serializable and contains no client
    content.  It may be attached to one inbound event so sibling asyncio tasks
    can adopt the same de-duplication lock without a process-global turn map.
    """
    return _TURN.get()


def adopt_gateway_trace(trace: Any) -> bool:
    """Adopt an exact in-process trace in another asyncio task context."""
    if not isinstance(trace, _TurnTrace):
        _TURN.set(None)
        return False
    if not re.fullmatch(r"[0-9a-f]{32}", trace.turn_id) or not trace.platform:
        _TURN.set(None)
        return False
    _TURN.set(trace)
    return True


def adopt_gateway_turn(turn_id: str, platform: str) -> bool:
    """Recreate an exact content-free trace after an executor handoff."""
    normalized_turn_id = str(turn_id or "")
    normalized_platform = str(platform or "")
    if not re.fullmatch(r"[0-9a-f]{32}", normalized_turn_id) or not normalized_platform:
        _TURN.set(None)
        return False
    _TURN.set(
        _TurnTrace(
            turn_id=normalized_turn_id,
            platform=normalized_platform,
        )
    )
    return True


def current_turn_id() -> str:
    trace = _TURN.get()
    return trace.turn_id if trace is not None else ""


def _begin_response_delivery() -> _TurnTrace | None:
    trace = _TURN.get()
    if trace is not None:
        with trace.lock:
            trace.delivery_state = 1
            trace.delivery_failed = False
            trace.delivery_acks = 0
            trace.delivery_event = None
    return trace


def _response_delivery_acks(*, trace: _TurnTrace | None = None) -> int:
    trace = trace if trace is not None else _TURN.get()
    if trace is None:
        return 0
    with trace.lock:
        return trace.delivery_acks


def _record_response_delivery(result: Any, *, trace: _TurnTrace | None = None) -> None:
    """Observe a required component without changing native partial-success policy."""
    trace = trace if trace is not None else _TURN.get()
    if trace is None:
        return
    success = bool(getattr(result, "success", False))
    with trace.lock:
        if trace.delivery_state != 1:
            return
        trace.delivery_failed |= not success
        trace.delivery_acks += int(success)
    if success:
        token = _TURN.set(trace)
        try:
            record_turn_event("first_visible_response_chunk", timing_semantics="platform_delivery_ack_exact")
            record_turn_event("response_sent", timing_semantics="platform_delivery_ack_exact")
        finally:
            _TURN.reset(token)


def _finish_response_delivery(*, trace: _TurnTrace | None = None, aborted: bool = False) -> bool:
    trace = trace if trace is not None else _TURN.get()
    if trace is None:
        return False
    with trace.lock:
        if trace.delivery_state != 1:
            return False
        trace.delivery_state = 2
        event, trace.delivery_event = trace.delivery_event, None
        if aborted or trace.delivery_failed or trace.discarded or trace.deferred or event is None:
            return False
        event = dict(event, observed_at=time.time(), monotonic_ns=time.monotonic_ns(),
                     timing_semantics="complete_platform_delivery_ack_upper_bound")
        if "response_sent" not in trace.emitted and _append(event):
            trace.emitted.add("response_sent")
            return True
    return False


def record_turn_event(
    event_name: str,
    *,
    observed_at: float | None = None,
    monotonic_ns: int | None = None,
    timing_semantics: str = "exact",
) -> bool:
    trace = _TURN.get()
    if trace is None or event_name not in _EVENT_NAMES:
        return False
    event = {
        "event_name": event_name,
        "monotonic_ns": int(monotonic_ns if monotonic_ns is not None else time.monotonic_ns()),
        "observed_at": float(observed_at if observed_at is not None else time.time()),
        "platform": trace.platform,
        "timing_semantics": timing_semantics,
        "turn_id": trace.turn_id,
    }
    with trace.lock:
        if trace.discarded or event_name in trace.emitted or event_name in trace.pending:
            return False
        if event_name == "response_sent" and trace.delivery_state:
            if trace.delivery_state == 1:
                trace.delivery_event = event
            return False
        if trace.deferred:
            # No response/model outcome is invented for a queued fragment.
            if event_name not in {"inbound_received", "typing_indicator_started"}:
                return False
            trace.pending[event_name] = event
            return True
        if _append(event):
            trace.emitted.add(event_name)
            return True
    return False


def record_model_request_started(
    *,
    turn_id: str,
    api_request_id: str,
    provider: str,
    model: str,
    logical_request_id: str,
    payload_breakdown: dict[str, Any],
    observed_at: float,
) -> bool:
    trace = _TURN.get()
    if (
        trace is None
        or trace.turn_id != turn_id
        or not api_request_id
        or not logical_request_id
        or not valid_payload_breakdown(payload_breakdown)
    ):
        return False
    if not _publish_admission(trace):
        return False
    return _append(
        {
            "api_request_id": api_request_id,
            "event_name": "model_request_started",
            "logical_request_id": logical_request_id,
            "model": str(model or "unknown"),
            "observed_at": float(observed_at),
            "platform": trace.platform,
            "provider": str(provider or "unknown"),
            "timing_semantics": "llm_attempt_started_exact",
            "turn_id": trace.turn_id,
        }
    )


def record_model_first_byte(
    *,
    turn_id: str,
    api_request_id: str,
    logical_request_id: str,
    provider: str,
    model: str,
    observed_at: float | None = None,
    timing_semantics: str = "provider_stream_first_delta_exact",
) -> bool:
    trace = _TURN.get()
    if (
        trace is None
        or trace.discarded or trace.deferred
        or trace.turn_id != turn_id
        or not api_request_id
        or not logical_request_id
    ):
        return False
    with trace.lock:
        if api_request_id in trace.first_model_bytes:
            return False
        written = _append(
            {
                "api_request_id": api_request_id,
                "event_name": "first_model_byte",
                "logical_request_id": logical_request_id,
                "model": str(model or "unknown"),
                "observed_at": float(
                    observed_at if observed_at is not None else time.time()
                ),
                "platform": trace.platform,
                "provider": str(provider or "unknown"),
                "timing_semantics": timing_semantics,
                "turn_id": trace.turn_id,
            }
        )
        if written:
            trace.first_model_bytes.add(api_request_id)
        return written


def record_model_request_complete(
    *,
    turn_id: str,
    api_request_id: str,
    provider: str,
    model: str,
    logical_request_id: str,
    payload_breakdown: dict[str, Any],
    observed_at: float,
    input_tokens: Any,
    cache_read_tokens: Any,
    cache_write_tokens: Any,
) -> bool:
    trace = _TURN.get()
    token_values = (input_tokens, cache_read_tokens, cache_write_tokens)
    if (
        trace is None
        or trace.discarded or trace.deferred
        or trace.turn_id != turn_id
        or not api_request_id
        or not logical_request_id
        or not valid_payload_breakdown(payload_breakdown)
        or any(type(value) is not int or value < 0 for value in token_values)
    ):
        return False
    common = {
        "api_request_id": api_request_id,
        "logical_request_id": logical_request_id,
        "model": str(model or "unknown"),
        "observed_at": float(observed_at),
        "platform": trace.platform,
        "provider": str(provider or "unknown"),
        "turn_id": trace.turn_id,
    }
    with trace.lock:
        first_exists = api_request_id in trace.first_model_bytes
    first = first_exists or record_model_first_byte(
        turn_id=turn_id,
        api_request_id=api_request_id,
        logical_request_id=logical_request_id,
        provider=provider,
        model=model,
        observed_at=observed_at,
        timing_semantics="llm_attempt_terminal_upper_bound",
    )
    complete = _append(
        {
            **common,
            "cache_read_tokens": cache_read_tokens,
            "cache_reused": cache_read_tokens > 0,
            "cache_write_tokens": cache_write_tokens,
            "event_name": "model_request_complete",
            "payload_breakdown": payload_breakdown,
            "provider_input_tokens": input_tokens,
            "terminal_outcome": "success",
            "timing_semantics": "llm_attempt_terminal_exact",
        }
    )
    return first and complete


def _bytes(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _tokens(byte_count: int) -> int:
    return (max(0, int(byte_count)) + 3) // 4


def _content(message: Any) -> Any:
    return message.get("content", "") if isinstance(message, dict) else ""


def _selector_controls() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        config = {}
    config = config if isinstance(config, dict) else {}
    skills = config.get("skills") if isinstance(config.get("skills"), dict) else {}
    policy = config.get("mcp_policy") if isinstance(config.get("mcp_policy"), dict) else {}
    servers = config.get("mcp_servers") if isinstance(config.get("mcp_servers"), dict) else {}
    hot = {str(item) for item in (policy.get("hot_path") or [])}
    on_demand = {str(item) for item in (policy.get("on_demand") or [])}
    return {
        "mcp_unclassified_count": len({str(item) for item in servers} - hot - on_demand),
        "skills_index_allowlist_configured": isinstance(skills.get("index_allowlist"), list),
    }


def build_payload_breakdown(
    messages: Any,
    tools: Any,
    *,
    toolset_for_tool: Any = None,
) -> dict[str, Any]:
    safe_messages = messages if isinstance(messages, list) else []
    safe_tools = tools if isinstance(tools, list) else []
    system_bytes = tool_result_bytes = 0
    tool_sizes: list[int] = []
    tool_hashes: list[str] = []
    skill_names: list[str] = []
    skills_index_bytes = 0
    for index, message in enumerate(safe_messages):
        if not isinstance(message, dict):
            continue
        content = _content(message)
        size = _bytes(content)
        role = message.get("role")
        if role == "system":
            system_bytes += size
            if isinstance(content, str):
                start = content.find("<available_skills>")
                end = content.find("</available_skills>", start + 1)
                if start >= 0 and end >= start:
                    skills_index_bytes += _bytes(
                        content[start : end + len("</available_skills>")]
                    )
        elif role == "tool":
            tool_result_bytes += size
            tool_sizes.append(size)
            tool_hashes.append(
                hashlib.sha256(
                    json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")
                ).hexdigest()
            )
        if isinstance(content, str):
            marker = '[IMPORTANT: The user has invoked the "'
            offset = 0
            while True:
                found = content.find(marker, offset)
                if found < 0:
                    break
                name_start = found + len(marker)
                name_end = content.find('" skill', name_start)
                if name_end < 0:
                    break
                skill_names.append(content[name_start:name_end])
                offset = name_end + 1

    schema_bytes = 0
    mcp_bytes = 0
    mcp_count = 0
    for tool in safe_tools:
        size = _bytes(tool)
        schema_bytes += size
        function = tool.get("function") if isinstance(tool, dict) else {}
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        try:
            toolset = toolset_for_tool(name) if callable(toolset_for_tool) and name else None
        except Exception:
            toolset = None
        if isinstance(toolset, str) and toolset.startswith("mcp-"):
            mcp_bytes += size
            mcp_count += 1
    request_bytes = _bytes(safe_messages) + schema_bytes
    payload = {
        "baseline_system": {"bytes": system_bytes},
        "context_injections": {},
        "estimated_request": {"estimated_tokens": _tokens(request_bytes)},
        "mcp_schemas": {"bytes": mcp_bytes, "count": mcp_count},
        "selector_controls": _selector_controls(),
        "skills": {
            "duplicate_injections": len(skill_names) - len(set(skill_names)),
            "index_bytes": skills_index_bytes,
            "selected_count": len(skill_names),
            "selected_names": skill_names,
        },
        "tool_results": {
            "bytes": tool_result_bytes,
            "duplicate_count": len(tool_hashes) - len(set(tool_hashes)),
            "max_bytes": max(tool_sizes, default=0),
        },
        "tool_schemas": {"bytes": schema_bytes},
    }
    if not valid_payload_breakdown(payload):
        raise ValueError("runtime payload breakdown is invalid")
    return payload


def valid_payload_breakdown(payload: Any) -> bool:
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        return False
    expected = {
        "baseline_system": {"bytes"},
        "tool_schemas": {"bytes"},
        "estimated_request": {"estimated_tokens"},
        "skills": {"index_bytes", "duplicate_injections", "selected_count", "selected_names"},
        "mcp_schemas": {"bytes", "count"},
        "tool_results": {"bytes", "max_bytes", "duplicate_count"},
        "selector_controls": {"skills_index_allowlist_configured", "mcp_unclassified_count"},
    }
    for name, keys in expected.items():
        value = payload.get(name)
        if not isinstance(value, dict) or set(value) != keys:
            return False
    numeric = (
        ("baseline_system", "bytes"),
        ("tool_schemas", "bytes"),
        ("estimated_request", "estimated_tokens"),
        ("skills", "index_bytes"),
        ("skills", "duplicate_injections"),
        ("skills", "selected_count"),
        ("mcp_schemas", "bytes"),
        ("mcp_schemas", "count"),
        ("tool_results", "bytes"),
        ("tool_results", "max_bytes"),
        ("tool_results", "duplicate_count"),
        ("selector_controls", "mcp_unclassified_count"),
    )
    if any(type(payload[name][field]) is not int or payload[name][field] < 0 for name, field in numeric):
        return False
    if type(payload["selector_controls"]["skills_index_allowlist_configured"]) is not bool:
        return False
    names = payload["skills"]["selected_names"]
    if not isinstance(names, list) or any(not isinstance(name, str) or len(name) > 128 for name in names):
        return False
    injections = payload["context_injections"]
    return isinstance(injections, dict) and not injections
