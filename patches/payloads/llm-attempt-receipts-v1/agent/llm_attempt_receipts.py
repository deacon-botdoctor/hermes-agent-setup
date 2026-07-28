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


def _write_ledger_event(event: dict[str, Any]) -> None:
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(_json_value(event), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)


def _record_ledger_failure(error: Exception) -> None:
    _LOGGER.error(
        "LLM attempt receipt ledger write failed: %s",
        type(error).__name__,
        exc_info=True,
    )


def _append(event: dict[str, Any]) -> None:
    try:
        _write_ledger_event(event)
    except Exception as exc:
        _record_ledger_failure(exc)


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


def _usage_payload(
    response: Any,
    *,
    provider: str,
    model: str,
    base_url: str,
    api_mode: str,
) -> dict[str, Any]:
    raw_usage = _obj_get(response, "usage")
    actual = _actual_cost(response, raw_usage)
    if raw_usage is None or (
        _is_openrouter(provider, base_url) and _has_unreported_usage(raw_usage)
    ):
        return _unavailable_usage_payload(actual)

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
        result = _unavailable_usage_payload()
        usage = None

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


def _is_openrouter(provider: str, base_url: str) -> bool:
    return str(provider or "").lower() == "openrouter" or _base_url_host(
        base_url
    ) == "openrouter.ai"


def _has_unreported_usage(raw_usage: Any) -> bool:
    values = []
    for name in (
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    ):
        value = _obj_get(raw_usage, name)
        if value is None:
            continue
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            return True
    return not values or not any(values)


def _actual_cost(response: Any, raw_usage: Any) -> float | None:
    for source in (raw_usage, response):
        for key in ("cost", "cost_usd", "total_cost", "provider_cost"):
            value = _obj_get(source, key)
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                return float(value)
    return None


def _unavailable_usage_payload(actual_cost: float | None = None) -> dict[str, Any]:
    payload = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "usage_status": "unavailable",
        "cost_usd": actual_cost,
        "cost_status": "actual" if actual_cost is not None else "unknown",
        "cost_source": "provider_response" if actual_cost is not None else "none",
    }
    return payload


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
    attempt_kind: str = "initial"
    fallback_cause: Optional[str] = None
    api_key: Any = None
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)
    _terminal: bool = False
    _last_response: Any = None

    def start(self) -> None:
        fingerprint, algorithm = _key_fingerprint(self.api_key)
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
                "attempt_kind": self.attempt_kind,
                "fallback_cause": self.fallback_cause,
                "key_fingerprint": fingerprint,
                "key_fingerprint_method": algorithm,
                "started_at": self.started_at,
            }
        )

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
            "attempt_kind": self.attempt_kind,
            "fallback_cause": self.fallback_cause,
            "outcome": outcome,
            "error_class": classify_error(error) if error is not None else None,
            "status_code": _status_code(error) if error is not None else None,
            "provider_request_id": request_id,
            "provider_request_id_source": request_id_source,
            "key_fingerprint": fingerprint,
            "key_fingerprint_method": fingerprint_algorithm,
            "started_at": self.started_at,
            "completed_at": ended_at,
            "duration_ms": round((ended_at - self.started_at) * 1000, 3),
        }
        payload.update(
            _usage_payload(
                response,
                provider=self.provider,
                model=resolved_model,
                base_url=self.base_url,
                api_mode=self.api_mode,
            )
        )
        _append(payload)


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
):
    state = _AuxRequestState(
        logical_request_id=f"aux:{uuid.uuid4().hex}",
        task=str(task or "unclassified"),
        requested_provider=str(provider or "auto"),
        requested_model=str(model or ""),
        session_id=session_id,
        turn_id=turn_id,
        platform=platform,
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


def _finish_abandoned_attempt(attempt: Attempt) -> None:
    if attempt._terminal:
        return
    try:
        attempt.finish("cancelled")
    except BaseException:
        # Finalizers must not surface during garbage collection or shutdown.
        pass


class _ObservedStream:
    def __init__(self, stream: Any, attempt: Attempt):
        self._stream = stream
        self._iterator = iter(stream)
        self._attempt = attempt
        self._abandonment = weakref.finalize(
            self,
            _finish_abandoned_attempt,
            attempt,
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
        self._abandonment = weakref.finalize(
            self,
            _finish_abandoned_attempt,
            attempt,
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
            raise
        except BaseException as exc:
            _note_aux_error(exc)
            await asyncio.to_thread(self._attempt.finish, "error", error=exc)
            raise

    async def aclose(self):
        close = getattr(self._stream, "aclose", None)
        try:
            if callable(close):
                return await close()
            return None
        finally:
            if not self._attempt._terminal:
                await asyncio.to_thread(self._attempt.finish, "cancelled")

    async def __aenter__(self):
        enter = getattr(self._stream, "__aenter__", None)
        if callable(enter):
            await enter()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None:
            _note_aux_error(exc)
            await asyncio.to_thread(self._attempt.finish, "error", error=exc)
        await self.aclose()
        exit_fn = getattr(self._stream, "__aexit__", None)
        return await exit_fn(exc_type, exc, tb) if callable(exit_fn) else False

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
        api_key=api_key,
    )
    attempt.start()
    try:
        response = call()
    except BaseException as exc:
        attempt.finish("error", error=exc)
        raise
    if defer_success:
        pending = dict(_PENDING_MAIN.get())
        pending[logical_request_id] = attempt
        _PENDING_MAIN.set(pending)
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
