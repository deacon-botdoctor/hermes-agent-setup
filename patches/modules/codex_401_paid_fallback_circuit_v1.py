#!/usr/bin/env python3
"""Bounded paid-fallback circuit for rejected Codex OAuth credentials."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_CODEX_401_PAID_FALLBACK_CIRCUIT_v1"
HELPER_PATH = Path("agent/codex_401_circuit.py")

HELPER_SOURCE = r'''"""Runtime-local Codex OAuth circuit state (no token material)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # POSIX
    _msvcrt = None

_OPEN = {"open", "refreshing", "probing", "degraded"}
_CLIENT_SAFE_AUTH_MESSAGE = (
    "I’m reconnecting the primary model. Please retry this request in a moment."
)
_PROCESS_STATE_LOCK = threading.Lock()


def _state_path() -> Path:
    default = Path.home() / ".hermes" / "state" / "codex-401-circuit.json"
    return Path(os.environ.get("HERMES_CODEX_401_CIRCUIT_STATE", default))


def _event_path() -> Path:
    path = _state_path()
    return path.with_name(path.name + ".events.jsonl")


def _read() -> dict | None:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except FileNotFoundError:
        return {}
    except Exception:
        return None


def _write(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        "." + path.name + ".tmp-" + str(os.getpid()) + "-" + str(threading.get_ident())
    )
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _state_lock():
    lock_path = _state_path().with_name(_state_path().name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_STATE_LOCK:
        with lock_path.open("a+b") as lock:
            if _fcntl is not None:
                _fcntl.flock(lock.fileno(), _fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    _fcntl.flock(lock.fileno(), _fcntl.LOCK_UN)
                return
            if _msvcrt is None:
                raise RuntimeError("no supported cross-process file lock")
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            _msvcrt.locking(lock.fileno(), _msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                _msvcrt.locking(lock.fileno(), _msvcrt.LK_UNLCK, 1)


def _append_event(*, incident_fingerprint: str, outcome: str) -> None:
    path = _event_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {
                "at": time.time(),
                "incident_fingerprint": str(incident_fingerprint),
                "outcome": str(outcome),
            },
            sort_keys=True,
        )
        + "\n"
    )
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)


def open_from_auth_failure(
    *,
    provider: str,
    status_code: int,
    detail: str = "",
    failure_scope: str = "primary_model",
    turn_id: str = "",
    session_id: str = "",
    event_origin: str = "client",
) -> bool:
    provider = str(provider).lower()
    failure_scope = str(failure_scope).lower()
    event_origin = str(event_origin).lower()
    if provider != "openai-codex" or int(status_code) not in {401, 403}:
        return False
    # Optional/background tool auth failures have their own credential path.
    # They must never change the active conversation model.
    if failure_scope != "primary_model":
        return False
    try:
        with _state_lock():
            now = time.time()
            state = _read()
            if state is None:
                return False
            if state.get("state") in _OPEN:
                _append_event(
                    incident_fingerprint=state.get("incident_fingerprint") or "unknown",
                    outcome="duplicate_auth_failure",
                )
                return True
            state = {
                "state": "open",
                "opened_at": now,
                "first_401_at": now,
                "last_401_at": now,
                "provider": "openai-codex",
                "status_code": int(status_code),
                "failure_scope": "primary_model",
                "event_origin": event_origin,
                "client_turn_id": str(turn_id),
                "incident_fingerprint": hashlib.sha256(
                    "|".join((
                        provider,
                        str(status_code),
                        str(session_id),
                        str(turn_id),
                    )).encode("utf-8", "replace")
                ).hexdigest()[:16],
                "error_fingerprint": hashlib.sha256(
                    str(detail).encode("utf-8", "replace")
                ).hexdigest()[:16],
            }
            _write(state)
    except Exception:
        return False
    return True


def paid_fallback_allowed(
    *, entry: dict, turn_id: str, event_origin: str, circuit_required: bool = False
) -> bool:
    provider = str(entry.get("provider") or "").lower()
    base_url = str(entry.get("base_url") or "").lower()
    if provider != "openrouter" and "openrouter.ai" not in base_url:
        return True
    state = _read()
    if state is None:
        return False
    # Paid routing is opt-in, never a generic failover.  The sole allowance is
    # a matching client turn after a durable primary-Codex 401/403 incident.
    # ``circuit_required`` remains in the signature for compatibility with
    # existing call sites; it must not relax this invariant.
    if not state:
        return False
    if state.get("state") not in _OPEN:
        return False
    matching_client_turn = (
        str(event_origin).lower() == "client"
        and state.get("event_origin") == "client"
        and bool(turn_id)
        and str(turn_id) == str(state.get("client_turn_id") or "")
    )
    if matching_client_turn:
        # The claim file is the cross-process authority for one activation of
        # this configured fallback entry.  A two-model emergency chain may
        # therefore advance once from Grok to GLM, while retries or concurrent
        # workers targeting the same entry remain denied.
        entry_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "provider": provider,
                    "model": str(entry.get("model") or ""),
                    "base_url": base_url,
                },
                sort_keys=True,
            ).encode("utf-8", "replace")
        ).hexdigest()[:16]
        claim = _state_path().with_name(
            "."
            + _state_path().name
            + ".paid-"
            + str(state.get("incident_fingerprint") or "unknown")
            + "-"
            + entry_fingerprint
            + ".claim"
        )
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
            try:
                _append_event(
                    incident_fingerprint=state.get("incident_fingerprint") or "unknown",
                    outcome="paid_fallback_allowed",
                )
            except Exception:
                pass
            return True
    try:
        _append_event(
            incident_fingerprint=state.get("incident_fingerprint") or "unknown",
            outcome="paid_fallback_blocked",
        )
    except Exception:
        pass
    return False


def client_safe_auth_message() -> str:
    return _CLIENT_SAFE_AUTH_MESSAGE
'''


class PatchError(RuntimeError):
    pass


def _replace_once(content: str, old: str, new: str, label: str) -> str:
    # The carrier also accepts fixtures that use escaped newlines; production
    # source naturally uses physical newlines.
    if content.count(old) == 1:
        return content.replace(old, new, 1)
    decoded_old = old.replace(chr(92) + "n", "\n").replace(chr(92) + '"', '"')
    if content.count(decoded_old) == 1:
        decoded_new = new.replace(chr(92) + "n", "\n").replace(chr(92) + '"', '"')
        return content.replace(decoded_old, decoded_new, 1)
    raise PatchError(f"required unique anchor missing: {label}")


def _patch_primary_auth_path(content: str) -> str:
    if 'failure_scope="primary_model"' in content:
        return content
    auth_failover = """                if (
                    classified.is_auth
                    and not _retry.auth_failover_attempted
                    and agent._fallback_index < len(agent._fallback_chain)
                ):
                    _retry.auth_failover_attempted = True
"""
    auth_failover_new = """                # HERMES_CODEX_401_PAID_FALLBACK_CIRCUIT_v1: this is
                # the primary model request boundary. Optional/background tool
                # auth failures use their own credential path and never enter it.
                # Open the incident even when no fallback remains so the broker
                # controller can refresh and prove primary recovery.
                if (
                    classified.is_auth
                    and agent.provider == "openai-codex"
                    and status_code in {401, 403}
                    and api_request_id == getattr(agent, "_current_api_request_id", None)
                    and str(api_request_id).startswith(f"{turn_id}:api:")
                ):
                    _codex_event_origin = (
                        "internal"
                        if getattr(agent, "_parent_session_id", None)
                        or str(original_user_message or "").lstrip().startswith("[ASYNC DELEGATION")
                        else "client"
                    )
                    agent._codex_auth_event_origin = _codex_event_origin
                    agent._codex_auth_circuit_unavailable = True
                    agent._codex_auth_circuit_failure_turn_id = turn_id
                    try:
                        from agent.codex_401_circuit import open_from_auth_failure
                        agent._codex_auth_circuit_unavailable = not open_from_auth_failure(
                            provider=agent.provider,
                            status_code=status_code,
                            detail=str(api_error),
                            failure_scope="primary_model",
                            turn_id=turn_id,
                            session_id=agent.session_id or "",
                            event_origin=_codex_event_origin,
                        )
                    except Exception:
                        logger.exception("Codex auth circuit state write failed")

                # Preserve upstream's generic primary-provider auth failover.
                if (
                    classified.is_auth
                    and (
                        agent.provider != "openai-codex"
                        or (
                            status_code in {401, 403}
                            and api_request_id == getattr(agent, "_current_api_request_id", None)
                            and str(api_request_id).startswith(f"{turn_id}:api:")
                        )
                    )
                    and not _retry.auth_failover_attempted
                    and agent._fallback_index < len(agent._fallback_chain)
                ):
                    _retry.auth_failover_attempted = True
"""
    if auth_failover in content:
        return _replace_once(content, auth_failover, auth_failover_new, "primary-model auth failover")

    # Compatibility with the pre-split run_agent.py layout.
    legacy = """                    if is_client_error:
                        # Try fallback before aborting — a different provider
                        # may not have the same issue (rate limit, auth, etc.)
"""
    legacy_new = """                    if is_client_error:
                        # HERMES_CODEX_401_PAID_FALLBACK_CIRCUIT_v1
                        if self.provider == "openai-codex" and status_code in {401, 403}:
                            _codex_message = getattr(self, "_current_user_message", "") or getattr(
                                self, "_original_user_message", ""
                            )
                            _codex_event_origin = (
                                "internal"
                                if getattr(self, "_parent_session_id", None)
                                or str(_codex_message or "").lstrip().startswith("[ASYNC DELEGATION")
                                else "client"
                            )
                            self._codex_auth_event_origin = _codex_event_origin
                            self._codex_auth_circuit_unavailable = True
                            self._codex_auth_circuit_failure_turn_id = getattr(self, "_current_turn_id", "")
                            try:
                                from agent.codex_401_circuit import open_from_auth_failure
                                self._codex_auth_circuit_unavailable = not open_from_auth_failure(
                                    provider=self.provider,
                                    status_code=status_code,
                                    detail=str(error),
                                    failure_scope="primary_model",
                                    turn_id=getattr(self, "_current_turn_id", ""),
                                    session_id=getattr(self, "session_id", ""),
                                    event_origin=_codex_event_origin,
                                )
                            except Exception:
                                logging.exception("Codex auth circuit state write failed")
                            try:
                                from agent.codex_401_circuit import client_safe_auth_message
                                error = RuntimeError(client_safe_auth_message())
                            except Exception:
                                error = RuntimeError(
                                    "I’m reconnecting the primary model. "
                                    "Please retry this request in a moment."
                                )
                        # Try fallback before aborting — a different provider
                        # may not have the same issue (rate limit, auth, etc.)
"""
    return _replace_once(content, legacy, legacy_new, "legacy primary-model auth failover")


def _patch_client_safe_result(content: str) -> str:
    if "client_safe_auth_message" in content:
        return content
    anchor = """                    _nonretryable_summary = agent._summarize_api_error(api_error)
"""
    if anchor not in content:
        raise PatchError("required unique anchor missing: client-safe Codex auth result")
    replacement = """                    _nonretryable_summary = agent._summarize_api_error(api_error)
                    if classified.is_auth and _provider == "openai-codex":
                        try:
                            from agent.codex_401_circuit import client_safe_auth_message
                            _nonretryable_summary = client_safe_auth_message()
                        except Exception:
                            _nonretryable_summary = (
                                "I’m reconnecting the primary model. "
                                "Please retry this request in a moment."
                            )
"""
    return _replace_once(content, anchor, replacement, "client-safe Codex auth result")


def _guard_optional_agent_methods(content: str) -> str:
    """Keep split-loop helpers safe across a live agent-generation mismatch."""
    replacements = {
        "agent._is_copilot_url()": ('bool(getattr(agent, "_is_copilot_url", lambda: False)())'),
        "agent._emit_pending_fallback_notice()": ('getattr(agent, "_emit_pending_fallback_notice", lambda: None)()'),
        "agent._interim_assistant_visible_text(last_msg)": (
            'getattr(agent, "_interim_assistant_visible_text", lambda _message: "")(last_msg)'
        ),
        "agent._interim_assistant_visible_text(interim_msg)": (
            'getattr(agent, "_interim_assistant_visible_text", lambda _message: "")(interim_msg)'
        ),
        "agent._interim_assistant_visible_text(assistant_msg)": (
            'getattr(agent, "_interim_assistant_visible_text", lambda _message: "")(assistant_msg)'
        ),
        "agent._interim_assistant_visible_text(previous_msg)": (
            'getattr(agent, "_interim_assistant_visible_text", lambda _message: "")(previous_msg)'
        ),
    }
    for direct, guarded in replacements.items():
        content = content.replace(direct, guarded)
    return content


def _patch_selector(content: str) -> str:
    availability_old = '''    fallback_reason = getattr(reason, "value", reason)
    # The paid-fallback circuit limits 401/403 amplification. A subscription
    # rate limit is a separate provider-availability incident and must be able
    # to use the configured OpenRouter emergency chain.
    if not (str(fallback_reason or "").strip().lower() == "rate_limit" and is_openrouter_fallback):
'''
    availability_new = '''    fallback_reason = getattr(reason, "value", reason)
    fallback_reason_value = str(fallback_reason or "").strip().lower()
    availability_reasons = {
        "rate_limit",
        "upstream_rate_limit",
        "overloaded",
        "server_error",
        "timeout",
        "model_not_found",
    }
    current_turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    current_provider = str(getattr(agent, "provider", "") or "").strip().lower()
    event_origin = str(
        getattr(agent, "_codex_auth_event_origin", "internal") or "internal"
    ).strip().lower()
    client_origin = event_origin == "client" and not (
        getattr(agent, "is_subagent", False)
        or getattr(agent, "_parent_session_id", None)
    )
    if (
        is_openrouter_fallback
        and fallback_reason_value in availability_reasons
        and current_provider == "openai-codex"
        and current_turn_id
        and client_origin
    ):
        agent._codex_availability_fallback_turn_id = current_turn_id
    availability_allowed = (
        is_openrouter_fallback
        and fallback_reason_value in availability_reasons
        and current_turn_id
        and current_turn_id
        == str(getattr(agent, "_codex_availability_fallback_turn_id", "") or "")
        and client_origin
    )
    # Availability gets a bounded same-turn route through the configured
    # emergency chain. Auth failures continue through the durable circuit.
    if not availability_allowed:
'''
    if "paid_fallback_allowed" in content:
        if availability_old in content:
            return _replace_once(
                content,
                availability_old,
                availability_new,
                "availability fallback selector upgrade",
            )
        return content
    class_anchor = """        fb_provider = (fb.get("provider") or "").strip().lower()
        fb_model = (fb.get("model") or "").strip()
        if not fb_provider or not fb_model:
"""
    class_replacement = """        fb_provider = (fb.get("provider") or "").strip().lower()
        fb_model = (fb.get("model") or "").strip()
        try:
            if (
                getattr(self, "_codex_auth_circuit_unavailable", False)
                and str(getattr(self, "_current_turn_id", "")) == str(
                    getattr(self, "_codex_auth_circuit_failure_turn_id", "")
                )
                and (
                fb_provider == "openrouter" or "openrouter.ai" in str(fb.get("base_url") or "").lower()
                )
            ):
                logging.warning("Codex auth circuit unavailable: blocked paid fallback")
                return self._try_activate_fallback()
            from agent.codex_401_circuit import paid_fallback_allowed
            if not paid_fallback_allowed(
                entry=fb,
                turn_id=getattr(self, "_current_turn_id", ""),
                event_origin=getattr(self, "_codex_auth_event_origin", "internal"),
                circuit_required=(
                    str(getattr(self, "_codex_auth_event_origin", "internal")).lower() == "client"
                    and str(getattr(self, "_current_turn_id", "")) == str(
                        getattr(self, "_codex_auth_circuit_failure_turn_id", "")
                    )
                ),
            ):
                logging.warning("Codex auth circuit open: blocked paid fallback %s/%s", fb_provider, fb_model)
                return self._try_activate_fallback()
        except Exception:
            if fb_provider == "openrouter" or "openrouter.ai" in str(fb.get("base_url") or "").lower():
                logging.exception("Codex auth circuit unavailable: blocked paid fallback")
                return self._try_activate_fallback()
        if not fb_provider or not fb_model:
"""
    if class_anchor in content:
        return _replace_once(content, class_anchor, class_replacement, "fallback selector")

    module_anchor = """    fb_provider = (fb.get("provider") or "").strip().lower()
    fb_model = (fb.get("model") or "").strip()
    if not fb_provider or not fb_model:
"""
    module_replacement = """    fb_provider = (fb.get("provider") or "").strip().lower()
    fb_model = (fb.get("model") or "").strip()
    is_openrouter_fallback = (
        fb_provider == "openrouter" or "openrouter.ai" in str(fb.get("base_url") or "").lower()
    )
""" + availability_new + """        try:
            if (
                getattr(agent, "_codex_auth_circuit_unavailable", False)
                and str(getattr(agent, "_current_turn_id", "")) == str(
                    getattr(agent, "_codex_auth_circuit_failure_turn_id", "")
                )
                and is_openrouter_fallback
            ):
                logging.warning("Codex auth circuit unavailable: blocked paid fallback")
                return agent._try_activate_fallback(reason)
            from agent.codex_401_circuit import paid_fallback_allowed
            if not paid_fallback_allowed(
                entry=fb,
                turn_id=getattr(agent, "_current_turn_id", ""),
                event_origin=getattr(agent, "_codex_auth_event_origin", "internal"),
                circuit_required=(
                    str(getattr(agent, "_codex_auth_event_origin", "internal")).lower() == "client"
                    and str(getattr(agent, "_current_turn_id", "")) == str(
                        getattr(agent, "_codex_auth_circuit_failure_turn_id", "")
                    )
                ),
            ):
                logging.warning("Codex auth circuit open: blocked paid fallback %s/%s", fb_provider, fb_model)
                return agent._try_activate_fallback(reason)
        except Exception:
            if is_openrouter_fallback:
                logging.exception("Codex auth circuit unavailable: blocked paid fallback")
                return agent._try_activate_fallback(reason)
    if not fb_provider or not fb_model:
"""
    return _replace_once(content, module_anchor, module_replacement, "module fallback selector")


def _patch_native_codex401(hermes_dir: Path) -> bool:
    """Reuse reviewed policy at d363's recovery and candidate-selection owners."""
    import textwrap
    recovery_path = hermes_dir / "agent/turn_recovery.py"
    error_path = hermes_dir / "agent/turn_api_error.py"
    selector_path = hermes_dir / "agent/chat_completion_helpers.py"
    recovery = recovery_path.read_text(encoding="utf-8")
    error = error_path.read_text(encoding="utf-8")
    selector = selector_path.read_text(encoding="utf-8")
    if MARKER not in recovery:
        # Native decomposition moved the reviewed block from loop indentation
        # to a module function; the substantive policy remains identical.
        recovery = textwrap.dedent(_patch_primary_auth_path(textwrap.indent(recovery, " " * 12)))
        recovery = _replace_once(recovery,
            "    effective_task_id: Any,\n) -> ClassifiedErrorVerdict:",
            "    effective_task_id: Any, turn_id: Any = '', api_request_id: Any = '',\n"
            "    original_user_message: Any = '',\n) -> ClassifiedErrorVerdict:",
            "native request identity parameters")
        recovery = _replace_once(recovery, '    status_code = getattr(api_error, "status_code", None)\n\n    def _verdict', '    status_code = getattr(api_error, "status_code", None)\n    # Bind availability origin to this primary request, never stale agent state.\n    agent._codex_auth_event_origin = "internal"\n    if (api_request_id == getattr(agent, "_current_api_request_id", None)\n            and turn_id and str(api_request_id).startswith(f"{turn_id}:api:")):\n        agent._codex_auth_event_origin = (\n            "internal" if getattr(agent, "is_subagent", False)\n            or getattr(agent, "_parent_session_id", None)\n            or str(original_user_message or "").lstrip().startswith("[ASYNC DELEGATION")\n            else "client"\n        )\n\n    def _verdict', "native trusted request origin")
        recovery = recovery.replace('if getattr(agent, "_parent_session_id", None)', 'if getattr(agent, "is_subagent", False) or getattr(agent, "_parent_session_id", None)', 1)
        summary = "    _nonretryable_summary = agent._summarize_api_error(api_error)\n"
        guarded = _patch_client_safe_result(" " * 16 + summary)
        guarded = textwrap.dedent(guarded).replace("_provider", "provider")
        recovery = _replace_once(recovery, summary, textwrap.indent(guarded, "    "), "native safe summary")
    if "original_user_message: Any = ''" not in error:
        error = _replace_once(error,
            "    api_request_id: Any, api_start_time: Any, effective_task_id: Any, turn_id: Any,\n",
            "    api_request_id: Any, api_start_time: Any, effective_task_id: Any, turn_id: Any,\n"
            "    original_user_message: Any = '',\n", "native error phase message parameter")
        error = _replace_once(error,
            "        effective_task_id=effective_task_id,\n    )\n    status_code = _ce.status_code",
            "        effective_task_id=effective_task_id, turn_id=turn_id, api_request_id=api_request_id,\n"
            "        original_user_message=original_user_message,\n    )\n    status_code = _ce.status_code",
            "native recovery request identity caller")
    if "paid_fallback_allowed" not in selector:
        seed = ('    fb_provider = (fb.get("provider") or "").strip().lower()\n'
                '    fb_model = (fb.get("model") or "").strip()\n'
                '    if not fb_provider or not fb_model:\n')
        residual = _patch_selector(seed).split('    fb_model = (fb.get("model") or "").strip()\n', 1)[1]
        residual = residual.removesuffix('    if not fb_provider or not fb_model:\n')
        native = '    if _should_skip_fallback_candidate(agent, fb, fb_key, fb_provider, fb_model, unavailable):\n'
        selector = _replace_once(selector, native, residual + native, "native candidate admission")
    proposed = {recovery_path: recovery, error_path: error, selector_path: selector,
                hermes_dir / HELPER_PATH: HELPER_SOURCE.strip() + "\n"}
    for path, source in proposed.items():
        compile(source, str(path), "exec")
    changed = False
    for path, source in proposed.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != source:
            path.write_text(source, encoding="utf-8")
            changed = True
    return changed


def patch_codex_401_paid_fallback_circuit_v1(hermes_dir: Path) -> bool:
    if (hermes_dir / "agent/turn_recovery.py").is_file():
        return _patch_native_codex401(hermes_dir)
    selector_target = hermes_dir / "agent" / "chat_completion_helpers.py"
    client_target = hermes_dir / "run_agent.py"
    if not client_target.exists() or "if is_client_error" not in client_target.read_text(encoding="utf-8"):
        client_target = hermes_dir / "agent" / "conversation_loop.py"
    if not selector_target.exists() or not client_target.exists():
        return False

    helper = hermes_dir / HELPER_PATH
    helper.parent.mkdir(parents=True, exist_ok=True)
    desired = HELPER_SOURCE.strip() + "\n"
    changed = not helper.exists() or helper.read_text(encoding="utf-8") != desired
    if changed:
        helper.write_text(desired, encoding="utf-8")

    client_original = client_target.read_text(encoding="utf-8")
    selector_original = selector_target.read_text(encoding="utf-8")
    client = _patch_primary_auth_path(client_original)
    if client_target.name == "conversation_loop.py":
        client = _patch_client_safe_result(client)
        client = _guard_optional_agent_methods(client)
    selector = _patch_selector(selector_original)
    if client != client_original:
        client_target.write_text(client, encoding="utf-8")
        changed = True
    if selector != selector_original:
        selector_target.write_text(selector, encoding="utf-8")
        changed = True
    return changed


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_codex_401_paid_fallback_circuit_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
