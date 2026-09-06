"""Content-free, per-provider-attempt LLM receipt journal.

Every physical provider call writes a ``started`` event before network I/O and
exactly one terminal event after success, error, cancellation, or stream close.
The journal deliberately excludes prompts, responses, tool arguments, and raw
credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import inspect
import json
import logging
import os
import socket
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_home

SCHEMA_VERSION = "botdoctor.llm-attempt.v1"
TERMINAL_OUTCOMES = frozenset({"success", "error", "cancelled"})
_WRITE_LOCK = threading.Lock()
_DEFERRED_TERMINALS: list[dict[str, Any]] = []
_MAX_DEFERRED_TERMINALS = 256
_LOGGER = logging.getLogger(__name__)


@dataclass
class _AuxRequestState:
    logical_request_id: str
    task: str
    requested_provider: str
    requested_model: str
    session_id: str = ""
    turn_id: str = ""
    platform: str = ""
    provenance_kind: str = ""
    provenance_ref: str = ""
    sequence: int = 0
    previous_route: tuple[str, str] | None = None
    previous_error: str | None = None


_AUX_REQUEST: contextvars.ContextVar[Optional[_AuxRequestState]] = contextvars.ContextVar(
    "llm_attempt_aux_request", default=None
)
_PENDING_MAIN: contextvars.ContextVar[dict[str, "Attempt"]] = contextvars.ContextVar(
    "llm_attempt_pending_main", default={}
)


def _journal_path() -> Path:
    override = os.getenv("HERMES_LLM_ATTEMPT_LEDGER")
    if override:
        return Path(override)
    return get_hermes_home() / "state" / "llm-attempt-receipts.jsonl"


def _runtime_id() -> str:
    return os.getenv("HERMES_AGENT_ID") or os.getenv("HERMES_PROFILE") or socket.gethostname() or "unknown"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_value(value.model_dump())
        except Exception:
            return None
    if hasattr(value, "__dict__"):
        try:
            return {str(k): _json_value(v) for k, v in vars(value).items() if not str(k).startswith("_")}
        except Exception:
            return None
    return None


def _write_event(path: Path, event: dict[str, Any]) -> None:
    payload = (json.dumps(_json_value(event), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Serialize framing checks and the append across processes using the
        # journal itself. Closing the descriptor releases either OS lock.
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        if os.lseek(fd, 0, os.SEEK_END):
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                raise OSError("receipt journal has an incomplete final record")
        if os.write(fd, payload) != len(payload):
            raise OSError("receipt journal write was incomplete")
    finally:
        os.close(fd)


def _append(event: dict[str, Any]) -> None:
    """Persist one event, flushing any terminal events deferred after I/O failure.

    Started events stay fail-closed: if the journal cannot record intent before
    network I/O, the provider call must not begin. Terminal events are different:
    the provider has already answered, so a local receipt failure must never turn
    a successful paid response into another provider attempt.
    """
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        while _DEFERRED_TERMINALS:
            _write_event(path, _DEFERRED_TERMINALS[0])
            _DEFERRED_TERMINALS.pop(0)
        _write_event(path, event)


def _append_terminal(event: dict[str, Any]) -> None:
    """Persist a post-provider event without invalidating the provider result."""
    try:
        _append(event)
    except OSError as exc:
        with _WRITE_LOCK:
            if len(_DEFERRED_TERMINALS) >= _MAX_DEFERRED_TERMINALS:
                _DEFERRED_TERMINALS.pop(0)
            _DEFERRED_TERMINALS.append(event)
        _LOGGER.error(
            "LLM terminal receipt deferred after local journal I/O failure; "
            "provider response remains valid: %s",
            exc,
        )


def _key_fingerprint(api_key: Any) -> tuple[str, str]:
    if not isinstance(api_key, str) or not api_key:
        return "id_unavailable", "unavailable"
    return hashlib.sha256(api_key.encode("utf-8", "replace")).hexdigest()[:16], "sha256_16"


def _obj_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _headers(value: Any) -> dict[str, Any]:
    for source in (
        value,
        _obj_get(value, "response"),
        _obj_get(value, "_response"),
    ):
        raw = _obj_get(source, "headers")
        if raw is None:
            continue
        try:
            return {str(k).lower(): v for k, v in dict(raw).items()}
        except Exception:
            continue
    return {}


def _provider_request_id(value: Any) -> tuple[str, str]:
    for name in ("request_id", "_request_id", "id"):
        candidate = _obj_get(value, name)
        if candidate:
            return str(candidate), name
    headers = _headers(value)
    for name in (
        "x-request-id",
        "x-openrouter-request-id",
        "request-id",
        "cf-ray",
    ):
        if headers.get(name):
            return str(headers[name]), f"header:{name}"
    return "id_unavailable", "unavailable"


def _openrouter_generation_id(value: Any) -> tuple[str, str]:
    """Return an explicit OpenRouter generation ID, never a generic request ID."""
    headers = _headers(value)
    candidate = headers.get("x-generation-id")
    if candidate:
        return str(candidate), "header:x-generation-id"
    candidate = _obj_get(value, "id")
    if isinstance(candidate, str) and candidate.startswith("gen-"):
        return candidate, "id:gen"
    return "id_unavailable", "unavailable"


def _is_openrouter(provider: str, base_url: str) -> bool:
    return str(provider or "").lower() == "openrouter" or _base_url_host(base_url) == "openrouter.ai"


def _provenance(
    *,
    surface: str,
    task: str,
    session_id: str,
    turn_id: str,
    explicit_kind: str = "",
    explicit_ref: str = "",
) -> tuple[str, str]:
    kind = str(explicit_kind or "").strip()
    ref = str(explicit_ref or "").strip()
    if kind and ref:
        return kind, ref
    if str(session_id).startswith("cron_"):
        return "cron_run", str(session_id)
    if turn_id:
        return "chat_turn", str(turn_id)
    if session_id:
        return "session", str(session_id)
    if surface == "auxiliary" and task and task != "unclassified":
        return "tool", str(task)
    return "unlinked", "id_unavailable"


class UnaccountableOpenRouterCall(RuntimeError):
    """Raised before network dispatch when paid work has no durable owner."""


def _status_code(error: BaseException) -> Optional[int]:
    for source in (error, _obj_get(error, "response")):
        raw = _obj_get(source, "status_code")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            continue
    return None


def classify_error(error: BaseException) -> str:
    status = _status_code(error)
    detail = f"{type(error).__name__} {error}".lower()
    if status in {401, 403} or any(x in detail for x in ("unauthorized", "forbidden", "invalid api key")):
        return "auth"
    if status == 402 or any(
        x in detail
        for x in (
            "payment required",
            "insufficient credit",
            "insufficient balance",
            "quota exhausted",
        )
    ):
        return "billing"
    if status == 429 or "rate limit" in detail or "too many requests" in detail:
        return "rate_limit"
    if any(x in detail for x in ("timeout", "timed out", "deadline exceeded")):
        return "timeout"
    if any(x in detail for x in ("connection", "dns", "network", "socket", "transport")):
        return "connection"
    if any(
        x in detail
        for x in (
            "invalid response",
            "invalid api response",
            "malformed",
            "missing choices",
        )
    ):
        return "invalid_response"
    if any(x in detail for x in ("cancelled", "canceled", "interrupt")):
        return "cancelled"
    if status is not None:
        return f"http_{status}"
    return "provider_error"


def _openrouter_generation_usage(api_key: Any, generation_id: str) -> Optional[dict[str, Any]]:
    """Fetch content-free usage metadata when the SDK returns zeroed usage."""
    if not isinstance(api_key, str) or not api_key:
        return None
    if not generation_id or generation_id == "id_unavailable":
        return None
    try:
        import urllib.parse
        import urllib.request

        url = "https://openrouter.ai/api/v1/generation?" + urllib.parse.urlencode({"id": generation_id})
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "hermes-llm-attempt-receipts/1.0",
            },
        )
        for delay in (0.0, 0.2, 0.5):
            if delay:
                time.sleep(delay)
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    except Exception:
        return None
    return None


def _openrouter_usage_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    def number(name: str) -> int:
        value = metadata.get(name)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    input_tokens = number("tokens_prompt")
    output_tokens = number("tokens_completion")
    cache_read_tokens = number("native_tokens_cached")
    reasoning_tokens = number("native_tokens_reasoning")
    cost = metadata.get("total_cost")
    if not isinstance(cost, (int, float, Decimal)) or isinstance(cost, bool):
        cost = metadata.get("usage")
    actual_cost = float(cost) if isinstance(cost, (int, float, Decimal)) and not isinstance(cost, bool) else None
    return {
        "input_tokens": max(0, input_tokens - cache_read_tokens),
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": 0,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": input_tokens + output_tokens,
        "usage_status": "provider_generation_metadata",
        "cost_usd": actual_cost,
        "cost_status": "actual" if actual_cost is not None else "unknown",
        "cost_source": ("openrouter_generation" if actual_cost is not None else "none"),
    }


def _usage_payload(
    response: Any,
    *,
    provider: str,
    model: str,
    base_url: str,
    api_mode: str,
    api_key: Any,
    openrouter_generation_id: str,
) -> dict[str, Any]:
    raw_usage = _obj_get(response, "usage")
    route_host = _base_url_host(base_url)
    if route_host == "openrouter.ai" and openrouter_generation_id != "id_unavailable":
        token_hint = 0
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            try:
                token_hint += int(_obj_get(raw_usage, name, 0) or 0)
            except (TypeError, ValueError):
                continue
        if raw_usage is None or token_hint == 0:
            metadata = _openrouter_generation_usage(api_key, openrouter_generation_id)
            if metadata is not None:
                return _openrouter_usage_payload(metadata)
    if raw_usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_status": "unavailable",
            "cost_usd": None,
            "cost_status": "unknown",
            "cost_source": "none",
        }

    try:
        from agent.usage_pricing import estimate_usage_cost, normalize_usage

        usage = normalize_usage(
            raw_usage,
            provider=provider,
            api_mode=api_mode,
        )
        result = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
            "usage_status": "provider_reported",
        }
    except Exception:
        result = {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_status": "unavailable",
        }
        usage = None

    actual = None
    raw = _json_value(raw_usage)
    if isinstance(raw, dict):
        for key in ("cost", "cost_usd", "total_cost", "provider_cost"):
            value = raw.get(key)
            if isinstance(value, (int, float, Decimal)):
                actual = float(value)
                break
    if actual is None:
        for key in ("cost", "cost_usd", "total_cost", "provider_cost"):
            value = _obj_get(response, key)
            if isinstance(value, (int, float, Decimal)):
                actual = float(value)
                break
    if actual is not None:
        result.update(
            cost_usd=actual,
            cost_status="actual",
            cost_source="provider_response",
        )
        return result

    if usage is not None:
        try:
            cost = estimate_usage_cost(
                model or str(_obj_get(response, "model") or ""),
                usage,
                provider=provider,
                base_url=base_url,
            )
            result.update(
                cost_usd=(float(cost.amount_usd) if cost.amount_usd is not None else None),
                cost_status=cost.status,
                cost_source=cost.source,
            )
            return result
        except Exception:
            pass
    result.update(cost_usd=None, cost_status="unknown", cost_source="none")
    return result


def _runtime_performance_turn_id() -> str:
    try:
        from agent.runtime_performance_events import current_turn_id

        return str(current_turn_id() or "")
    except Exception:
        return ""


@dataclass
class Attempt:
    surface: str
    task: str
    provider: str
    model: str
    base_url: str
    api_mode: str
    logical_request_id: str
    session_id: str = ""
    turn_id: str = ""
    platform: str = ""
    provenance_kind: str = ""
    provenance_ref: str = ""
    attempt_kind: str = "initial"
    fallback_cause: Optional[str] = None
    payload_breakdown: Optional[dict[str, Any]] = None
    runtime_performance_turn_id: str = field(
        default_factory=_runtime_performance_turn_id
    )
    api_key: Any = None
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)
    _terminal: bool = False
    _last_response: Any = None

    def start(self) -> None:
        fingerprint, algorithm = _key_fingerprint(self.api_key)
        provenance_kind, provenance_ref = _provenance(
            surface=self.surface,
            task=self.task,
            session_id=self.session_id,
            turn_id=self.turn_id,
            explicit_kind=self.provenance_kind,
            explicit_ref=self.provenance_ref,
        )
        if _is_openrouter(self.provider, self.base_url) and provenance_kind == "unlinked":
            raise UnaccountableOpenRouterCall(
                "OpenRouter request refused before dispatch: accountable provenance required"
            )
        _append(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "started",
                "attempt_id": self.attempt_id,
                "logical_request_id": self.logical_request_id,
                "runtime_id": _runtime_id(),
                "surface": self.surface,
                "task": self.task or "unclassified",
                "provider": self.provider or "unknown",
                "model": self.model or "unknown",
                "base_url_host": _base_url_host(self.base_url),
                "api_mode": self.api_mode or "unknown",
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "platform": self.platform,
                "provenance_kind": provenance_kind,
                "provenance_ref": provenance_ref,
                "attempt_kind": self.attempt_kind,
                "fallback_cause": self.fallback_cause,
                "key_fingerprint": fingerprint,
                "key_fingerprint_method": algorithm,
                "started_at": self.started_at,
            }
        )
        if self.surface == "main" and isinstance(self.payload_breakdown, dict):
            try:
                from agent.runtime_performance_events import (
                    record_model_request_started,
                )

                record_model_request_started(
                    turn_id=self.runtime_performance_turn_id,
                    api_request_id=self.attempt_id,
                    logical_request_id=self.logical_request_id,
                    provider=self.provider,
                    model=self.model,
                    payload_breakdown=self.payload_breakdown,
                    observed_at=self.started_at,
                )
            except Exception:
                pass

    def finish(
        self,
        outcome: str,
        *,
        response: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        if self._terminal:
            return
        self._terminal = True
        response = response if response is not None else self._last_response
        ended_at = time.time()
        request_id, request_id_source = _provider_request_id(response or error)
        generation_id, generation_id_source = _openrouter_generation_id(response or error)
        provenance_kind, provenance_ref = _provenance(
            surface=self.surface,
            task=self.task,
            session_id=self.session_id,
            turn_id=self.turn_id,
            explicit_kind=self.provenance_kind,
            explicit_ref=self.provenance_ref,
        )
        fingerprint, fingerprint_algorithm = _key_fingerprint(self.api_key)
        resolved_model = str(_obj_get(response, "model") or self.model or "unknown")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "event": "terminal",
            "attempt_id": self.attempt_id,
            "logical_request_id": self.logical_request_id,
            "runtime_id": _runtime_id(),
            "surface": self.surface,
            "task": self.task or "unclassified",
            "provider": self.provider or "unknown",
            "model": resolved_model,
            "base_url_host": _base_url_host(self.base_url),
            "api_mode": self.api_mode or "unknown",
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "platform": self.platform,
            "provenance_kind": provenance_kind,
            "provenance_ref": provenance_ref,
            "attempt_kind": self.attempt_kind,
            "fallback_cause": self.fallback_cause,
            "outcome": outcome,
            "error_class": classify_error(error) if error is not None else None,
            "status_code": _status_code(error) if error is not None else None,
            "provider_request_id": request_id,
            "provider_request_id_source": request_id_source,
            "openrouter_generation_id": generation_id,
            "openrouter_generation_id_source": generation_id_source,
            "key_fingerprint": fingerprint,
            "key_fingerprint_method": fingerprint_algorithm,
            "started_at": self.started_at,
            "completed_at": ended_at,
            "duration_ms": round((ended_at - self.started_at) * 1000, 3),
        }
        usage_payload = _usage_payload(
            response,
            provider=self.provider,
            model=resolved_model,
            base_url=self.base_url,
            api_mode=self.api_mode,
            api_key=self.api_key,
            openrouter_generation_id=generation_id,
        )
        payload.update(usage_payload)
        if isinstance(self.payload_breakdown, dict):
            payload["payload_breakdown"] = self.payload_breakdown
        _append_terminal(payload)
        if (
            self.surface == "main"
            and outcome == "success"
            and isinstance(self.payload_breakdown, dict)
        ):
            try:
                from agent.runtime_performance_events import (
                    record_model_request_complete,
                )

                record_model_request_complete(
                    turn_id=self.runtime_performance_turn_id,
                    api_request_id=self.attempt_id,
                    logical_request_id=self.logical_request_id,
                    provider=self.provider,
                    model=resolved_model,
                    payload_breakdown=self.payload_breakdown,
                    observed_at=ended_at,
                    input_tokens=usage_payload.get("input_tokens"),
                    cache_read_tokens=usage_payload.get("cache_read_tokens"),
                    cache_write_tokens=usage_payload.get("cache_write_tokens"),
                )
            except Exception:
                pass


def _base_url_host(base_url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(str(base_url or "")).hostname or ""
    except Exception:
        return ""


@contextlib.contextmanager
def auxiliary_request(
    *,
    task: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    session_id: str = "",
    turn_id: str = "",
    platform: str = "",
    provenance_kind: str = "",
    provenance_ref: str = "",
):
    state = _AuxRequestState(
        logical_request_id=f"aux:{uuid.uuid4().hex}",
        task=str(task or "unclassified"),
        requested_provider=str(provider or "auto"),
        requested_model=str(model or ""),
        session_id=session_id,
        turn_id=turn_id,
        platform=platform,
        provenance_kind=provenance_kind,
        provenance_ref=provenance_ref,
    )
    token = _AUX_REQUEST.set(state)
    try:
        yield state
    finally:
        _AUX_REQUEST.reset(token)


def _route_metadata(provider: str, model: str) -> tuple[str, Optional[str]]:
    state = _AUX_REQUEST.get()
    if state is None:
        return "initial", None
    route = (provider or "unknown", model or "unknown")
    state.sequence += 1
    if state.previous_route is None:
        kind = "initial"
        cause = None
    elif state.previous_route == route:
        kind = "retry"
        cause = None
    else:
        kind = "fallback"
        cause = state.previous_error or "unclassified_provider_switch"
    state.previous_route = route
    return kind, cause


def _new_aux_attempt(
    *,
    client: Any,
    provider: str,
    model: str,
    base_url: str,
    api_mode: str,
) -> Optional[Attempt]:
    state = _AUX_REQUEST.get()
    if state is None:
        return None
    kind, cause = _route_metadata(provider, model)
    attempt = Attempt(
        surface="auxiliary",
        task=state.task,
        provider=provider,
        model=model or state.requested_model,
        base_url=base_url,
        api_mode=api_mode,
        logical_request_id=state.logical_request_id,
        session_id=state.session_id,
        turn_id=state.turn_id,
        platform=state.platform,
        provenance_kind=state.provenance_kind,
        provenance_ref=state.provenance_ref,
        attempt_kind=kind,
        fallback_cause=cause,
        api_key=getattr(client, "api_key", None),
    )
    attempt.start()
    return attempt


def _note_aux_error(error: BaseException) -> None:
    state = _AUX_REQUEST.get()
    if state is not None:
        state.previous_error = classify_error(error)


def _is_stream(value: Any) -> bool:
    return (
        hasattr(value, "__next__")
        or hasattr(value, "__anext__")
        or (
            hasattr(value, "__iter__")
            and not isinstance(value, (str, bytes, dict, list, tuple))
            and _obj_get(value, "choices") is None
        )
    )


def _finish_abandoned_attempt(
    attempt: Attempt,
    close: Optional[Callable[[], Any]] = None,
) -> None:
    if attempt._terminal:
        return
    try:
        if callable(close):
            close()
    except BaseException:
        pass
    finally:
        try:
            attempt.finish("cancelled")
        except BaseException:
            # Finalizers must not surface during garbage collection or shutdown.
            pass


@dataclass
class _AsyncCleanupState:
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event
    attempt: Attempt
    close: Optional[Callable[[], Any]]
    sync_close: Optional[Callable[[], Any]]
    done: bool = False
    task: Optional[asyncio.Task] = None


async def _close_async_stream(close: Optional[Callable[[], Any]]) -> None:
    try:
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
    except BaseException:
        pass


def _close_async_stream_after_loop(
    close: Optional[Callable[[], Any]],
    sync_close: Optional[Callable[[], Any]],
) -> None:
    try:
        if callable(sync_close):
            sync_close()
    except BaseException:
        pass
    result = None
    try:
        if callable(close):
            result = close()
        if not inspect.isawaitable(result):
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None:
            running_loop.create_task(result)
        else:
            asyncio.run(result)
    except BaseException:
        if inspect.iscoroutine(result):
            result.close()


async def _monitor_async_stream(state: _AsyncCleanupState) -> None:
    try:
        await state.event.wait()
    except asyncio.CancelledError:
        # asyncio.run() waits for cancelled tasks to finish. Treat loop shutdown
        # as cancellation of any stream that did not already terminalize.
        pass
    if state.done:
        return
    try:
        await _close_async_stream(state.close)
    finally:
        if not state.attempt._terminal:
            await asyncio.to_thread(state.attempt.finish, "cancelled")


def _abandon_async_stream(state: _AsyncCleanupState) -> None:
    if state.done:
        return
    try:
        if state.loop.is_closed():
            _close_async_stream_after_loop(state.close, state.sync_close)
            _finish_abandoned_attempt(state.attempt)
            return
        if not state.loop.is_running():
            state.loop.run_until_complete(_close_async_stream(state.close))
            _finish_abandoned_attempt(state.attempt)
            state.done = True
            state.event.set()
            if state.task is not None and not state.task.done():
                state.loop.run_until_complete(state.task)
            return
        # The receipt is synchronous so collection during live-loop shutdown
        # cannot strand a start event. The resident monitor owns async close.
        _finish_abandoned_attempt(state.attempt)
        state.loop.call_soon_threadsafe(state.event.set)
    except BaseException:
        _finish_abandoned_attempt(state.attempt)


class _ObservedStream:
    def __init__(self, stream: Any, attempt: Attempt):
        self._stream = stream
        self._iterator = iter(stream)
        self._attempt = attempt
        close = getattr(stream, "close", None)
        if not callable(close):
            close = getattr(self._iterator, "close", None)
        self._abandonment = weakref.finalize(
            self,
            _finish_abandoned_attempt,
            attempt,
            close,
        )

    def __iter__(self):
        def consume():
            try:
                while True:
                    try:
                        yield self.__next__()
                    except StopIteration:
                        return
            finally:
                if not self._attempt._terminal:
                    self.close()

        return consume()

    def __next__(self):
        try:
            item = next(self._iterator)
            if (
                _obj_get(item, "usage") is not None
                or _obj_get(item, "id") is not None
                or self._attempt._last_response is None
            ):
                self._attempt._last_response = item
            return item
        except StopIteration:
            self._attempt.finish("success")
            raise
        except BaseException as exc:
            _note_aux_error(exc)
            self._attempt.finish("error", error=exc)
            raise

    def close(self):
        close = getattr(self._stream, "close", None)
        if not callable(close):
            close = getattr(self._iterator, "close", None)
        try:
            if callable(close):
                return close()
            return None
        finally:
            if not self._attempt._terminal:
                self._attempt.finish("cancelled")

    def __enter__(self):
        enter = getattr(self._stream, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            _note_aux_error(exc)
            self._attempt.finish("error", error=exc)
        self.close()
        exit_fn = getattr(self._stream, "__exit__", None)
        return exit_fn(exc_type, exc, tb) if callable(exit_fn) else False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _ObservedAsyncStream:
    def __init__(self, stream: Any, attempt: Attempt):
        self._stream = stream
        self._iterator = stream.__aiter__()
        self._attempt = attempt
        self._loop = asyncio.get_running_loop()
        close = getattr(stream, "aclose", None)
        if not callable(close):
            close = getattr(self._iterator, "aclose", None)
        sync_close = getattr(stream, "close", None)
        if not callable(sync_close):
            sync_close = getattr(self._iterator, "close", None)
        self._cleanup = _AsyncCleanupState(
            loop=self._loop,
            event=asyncio.Event(),
            attempt=attempt,
            close=close,
            sync_close=sync_close,
        )
        self._cleanup.task = self._loop.create_task(
            _monitor_async_stream(self._cleanup)
        )
        self._abandonment = weakref.finalize(
            self,
            _abandon_async_stream,
            self._cleanup,
        )

    def __aiter__(self):
        async def consume():
            try:
                while True:
                    try:
                        yield await self.__anext__()
                    except StopAsyncIteration:
                        return
            finally:
                if not self._attempt._terminal:
                    await self.aclose()

        return consume()

    async def __anext__(self):
        try:
            item = await self._iterator.__anext__()
            if (
                _obj_get(item, "usage") is not None
                or _obj_get(item, "id") is not None
                or self._attempt._last_response is None
            ):
                self._attempt._last_response = item
            return item
        except StopAsyncIteration:
            await asyncio.to_thread(self._attempt.finish, "success")
            self._cleanup.done = True
            self._cleanup.event.set()
            raise
        except BaseException as exc:
            _note_aux_error(exc)
            await asyncio.to_thread(self._attempt.finish, "error", error=exc)
            self._cleanup.done = True
            self._cleanup.event.set()
            raise

    async def aclose(self):
        close = getattr(self._stream, "aclose", None)
        if not callable(close):
            close = getattr(self._iterator, "aclose", None)
        try:
            if callable(close):
                return await close()
            return None
        finally:
            if not self._attempt._terminal:
                await asyncio.to_thread(self._attempt.finish, "cancelled")
            self._cleanup.done = True
            self._cleanup.event.set()

    async def __aenter__(self):
        enter = getattr(self._stream, "__aenter__", None)
        try:
            if callable(enter):
                await enter()
        except BaseException as enter_error:
            _note_aux_error(enter_error)
            await asyncio.to_thread(
                self._attempt.finish,
                "error",
                error=enter_error,
            )
            self._cleanup.done = True
            self._cleanup.event.set()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        exit_fn = getattr(self._stream, "__aexit__", None)
        if not callable(exit_fn):
            if exc is not None:
                _note_aux_error(exc)
                await asyncio.to_thread(
                    self._attempt.finish,
                    "error",
                    error=exc,
                )
            await self.aclose()
            return False
        try:
            result = await exit_fn(exc_type, exc, tb)
        except BaseException as exit_error:
            _note_aux_error(exit_error)
            await asyncio.to_thread(
                self._attempt.finish,
                "error",
                error=exit_error,
            )
            raise
        else:
            if exc is not None:
                _note_aux_error(exc)
                await asyncio.to_thread(
                    self._attempt.finish,
                    "error",
                    error=exc,
                )
            elif not self._attempt._terminal:
                await asyncio.to_thread(self._attempt.finish, "cancelled")
            return result
        finally:
            self._cleanup.done = True
            self._cleanup.event.set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _instrument_completions(
    client: Any,
    *,
    provider: str,
    model: str,
    task: Optional[str],
    api_mode: str,
) -> Any:
    try:
        completions = client.chat.completions
        original = completions.create
    except Exception:
        return client
    if getattr(completions, "_hermes_llm_receipts_v1", False):
        return client

    base_url = str(getattr(client, "base_url", "") or "")
    if inspect.iscoroutinefunction(original):

        async def observed_create(*args, **kwargs):
            attempt = _new_aux_attempt(
                client=client,
                provider=provider,
                model=str(kwargs.get("model") or model or ""),
                base_url=base_url,
                api_mode=api_mode,
            )
            if attempt is None:
                return await original(*args, **kwargs)
            try:
                response = await original(*args, **kwargs)
            except BaseException as exc:
                _note_aux_error(exc)
                await asyncio.to_thread(attempt.finish, "error", error=exc)
                raise
            if _is_stream(response):
                return _ObservedAsyncStream(response, attempt)
            await asyncio.to_thread(attempt.finish, "success", response=response)
            return response
    else:

        def observed_create(*args, **kwargs):
            attempt = _new_aux_attempt(
                client=client,
                provider=provider,
                model=str(kwargs.get("model") or model or ""),
                base_url=base_url,
                api_mode=api_mode,
            )
            if attempt is None:
                return original(*args, **kwargs)
            try:
                response = original(*args, **kwargs)
            except BaseException as exc:
                _note_aux_error(exc)
                attempt.finish("error", error=exc)
                raise
            if _is_stream(response):
                return _ObservedStream(response, attempt)
            attempt.finish("success", response=response)
            return response

    try:
        completions.create = observed_create
        completions._hermes_llm_receipts_v1 = True
    except Exception:
        return client
    return client


def instrument_auxiliary_client(
    client: Any,
    *,
    provider: Optional[str],
    model: Optional[str],
    task: Optional[str] = None,
    api_mode: Optional[str] = None,
) -> Any:
    if client is None:
        return None
    return _instrument_completions(
        client,
        provider=str(provider or "unknown"),
        model=str(model or ""),
        task=task,
        api_mode=str(api_mode or "chat_completions"),
    )


def execute_main_attempt(
    call: Callable[[], Any],
    *,
    task: str,
    provider: str,
    model: str,
    base_url: str,
    api_mode: str,
    logical_request_id: str,
    session_id: str,
    turn_id: str,
    platform: str,
    api_key: Any,
    retry_count: int,
    is_fallback: bool,
    fallback_cause: Optional[str],
    payload_breakdown: Optional[dict[str, Any]] = None,
    defer_success: bool = False,
) -> Any:
    attempt = Attempt(
        surface="main",
        task=task or "conversation",
        provider=provider,
        model=model,
        base_url=base_url,
        api_mode=api_mode,
        logical_request_id=logical_request_id,
        session_id=session_id,
        turn_id=turn_id,
        platform=platform,
        attempt_kind=("fallback" if is_fallback else ("retry" if retry_count else "initial")),
        fallback_cause=fallback_cause if is_fallback else None,
        payload_breakdown=payload_breakdown,
        api_key=api_key,
    )
    attempt.start()
    if defer_success:
        pending = dict(_PENDING_MAIN.get())
        pending[logical_request_id] = attempt
        _PENDING_MAIN.set(pending)
    try:
        response = call()
    except BaseException as exc:
        if defer_success:
            pending = dict(_PENDING_MAIN.get())
            pending.pop(logical_request_id, None)
            _PENDING_MAIN.set(pending)
        attempt.finish("error", error=exc)
        raise
    if defer_success:
        return response
    attempt.finish("success", response=response)
    return response


def finish_main_attempt(
    logical_request_id: str,
    outcome: str,
    *,
    response: Any = None,
    error: Optional[BaseException] = None,
) -> bool:
    pending = dict(_PENDING_MAIN.get())
    attempt = pending.pop(logical_request_id, None)
    _PENDING_MAIN.set(pending)
    if attempt is None:
        return False
    attempt.finish(outcome, response=response, error=error)
    return True


def record_main_first_byte(logical_request_id: str) -> bool:
    attempt = _PENDING_MAIN.get().get(logical_request_id)
    if attempt is None:
        return False
    try:
        from agent.runtime_performance_events import record_model_first_byte

        return record_model_first_byte(
            turn_id=attempt.runtime_performance_turn_id,
            api_request_id=attempt.attempt_id,
            logical_request_id=attempt.logical_request_id,
            provider=attempt.provider,
            model=attempt.model,
        )
    except Exception:
        return False


def reconcile_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    attempts: dict[str, dict[str, list[dict[str, Any]]]] = {}
    malformed = []
    for index, event in enumerate(events, start=1):
        if event.get("schema_version") != SCHEMA_VERSION:
            malformed.append({"line": index, "reason": "schema_version"})
            continue
        attempt_id = str(event.get("attempt_id") or "")
        kind = str(event.get("event") or "")
        if not attempt_id or kind not in {"started", "terminal"}:
            malformed.append({"line": index, "reason": "identity_or_event"})
            continue
        attempts.setdefault(attempt_id, {"started": [], "terminal": []})[kind].append(event)

    violations = list(malformed)
    for attempt_id, grouped in attempts.items():
        if len(grouped["started"]) != 1:
            violations.append(
                {
                    "attempt_id": attempt_id,
                    "reason": "started_count",
                    "count": len(grouped["started"]),
                }
            )
        if len(grouped["terminal"]) != 1:
            violations.append(
                {
                    "attempt_id": attempt_id,
                    "reason": "terminal_count",
                    "count": len(grouped["terminal"]),
                }
            )
            continue
        terminal = grouped["terminal"][0]
        for field_name in (
            "surface",
            "task",
            "provider",
            "model",
            "outcome",
            "provider_request_id",
            "provider_request_id_source",
            "openrouter_generation_id",
            "openrouter_generation_id_source",
            "provenance_kind",
            "provenance_ref",
            "cost_status",
            "key_fingerprint",
            "key_fingerprint_method",
        ):
            if terminal.get(field_name) in (None, ""):
                violations.append(
                    {
                        "attempt_id": attempt_id,
                        "reason": "missing_terminal_field",
                        "field": field_name,
                    }
                )
        if terminal.get("outcome") not in TERMINAL_OUTCOMES:
            violations.append(
                {
                    "attempt_id": attempt_id,
                    "reason": "invalid_outcome",
                    "value": terminal.get("outcome"),
                }
            )
        if terminal.get("cost_status") not in {
            "actual",
            "estimated",
            "included",
            "unknown",
        }:
            violations.append(
                {
                    "attempt_id": attempt_id,
                    "reason": "invalid_cost_status",
                    "value": terminal.get("cost_status"),
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not violations else "fail",
        "attempts": len(attempts),
        "terminal_attempts": sum(1 for grouped in attempts.values() if len(grouped["terminal"]) == 1),
        "violations": violations,
    }
