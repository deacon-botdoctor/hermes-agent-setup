# ruff: noqa: E501
"""Rotate Anthropic CredentialPool seats on HTTP 403 OAuth/auth errors before provider fallback."""
from pathlib import Path

MARKER = "HERMES_ANTHROPIC_403_POOL_ROTATION_v1"

_OLD_TRANSIENT_403_CODES = '''_403_TRANSIENT_CODES = frozenset({"upstream_unavailable"})
'''

_NEW_TRANSIENT_403_CODES = '''_403_TRANSIENT_CODES = frozenset({"upstream_unavailable"})

# HERMES_ANTHROPIC_403_POOL_ROTATION_v1
_ANTHROPIC_403_EXCLUDED_CODES = frozenset({
    "overloaded_error",
    "upstream_unavailable",
    "data_policy_denied",
    "content_policy_blocked",
    "provider_policy_blocked",
    "upstream_blocked",
})
_ANTHROPIC_CREDENTIAL_403_EXACT_SIGNALS = (
    "oauth authentication is currently not allowed for this organization",
    "oauth authentication is not allowed",
    "oauth is not allowed",
    "oauth is disabled",
    "oauth token is invalid",
    "oauth token has expired",
    "oauth token is revoked",
    "oauth token has been revoked",
    "invalid x-api-key",
    "invalid api key",
    "invalid_api_key",
    "invalid bearer token",
    "api key is disabled",
    "api key has been disabled",
    "api key is revoked",
    "api key has been revoked",
    "your api key does not have permission",
    "api key does not have permission",
    "token is revoked",
    "token has been revoked",
    "token revoked",
    "token expired",
    "account is disabled",
    "account has been disabled",
    "organization has been disabled",
    "organization is disabled",
)


def _is_anthropic_credential_403_signal(code: str, text: str) -> bool:
    """True only when a 403 carries a trusted structured authentication_error code
    or explicit Anthropic credential/OAuth/account auth-denial wording."""
    norm_code = (code or "").strip().lower()
    norm_text = (text or "").lower()
    if (
        norm_code in _ANTHROPIC_403_EXCLUDED_CODES
        or "overloaded_error" in norm_text
        or "data_policy_denied" in norm_text
    ):
        return False
    if (
        any(p in norm_text for p in _CONTENT_POLICY_BLOCKED_PATTERNS)
        or any(p in norm_text for p in _ACCOUNT_POLICY_BLOCK_PATTERNS)
        or any(p in norm_text for p in _PROVIDER_POLICY_BLOCKED_PATTERNS)
        or any(p in norm_text for p in _UPSTREAM_BLOCKED_PATTERNS)
    ):
        return False
    if norm_code == "authentication_error":
        return True
    return any(p in norm_text for p in _ANTHROPIC_CREDENTIAL_403_EXACT_SIGNALS)
'''

_OLD_STATUS_403 = '''    if any(p in c.msg for p in _UPSTREAM_BLOCKED_PATTERNS):
        return _V_UPSTREAM_BLOCKED
    return _V_AUTH_FALLBACK
'''

_NEW_STATUS_403 = '''    if any(p in c.msg for p in _UPSTREAM_BLOCKED_PATTERNS):
        return _V_UPSTREAM_BLOCKED
    # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
    if c.provider_slug == "anthropic" and _is_anthropic_credential_403_signal(c.code, c.msg):
        return _V_AUTH_ROTATE
    return _V_AUTH_FALLBACK
'''

_OLD_RECOVER_AUTH = '''def recover_auth_failure(agent, pool, *, status_code, has_retried_429, error_context, api_key_hint,
                         credential_id, rotate_and_swap):
    from agent.agent_runtime_helpers import _is_entitlement_403
    log = _ra().logger
    if _is_entitlement_403(agent, status_code, error_context):
        log.info(
            "Credential %s — entitlement-shaped 403 from %s; "
            "skipping pool refresh (account lacks subscription, "
            "not a transient auth failure).", status_code if status_code is not None else "auth",
            agent.provider or "provider",
        )
        return False, has_retried_429
'''

_NEW_RECOVER_AUTH = '''# HERMES_ANTHROPIC_403_POOL_ROTATION_v1
def _seat_dedup_key(entry_id: Any, secret_fallback: Any) -> str:
    """Stable per-turn dedup key that never exposes raw secret material."""
    if isinstance(entry_id, str) and entry_id:
        return f"id:{entry_id}"
    if isinstance(secret_fallback, str) and secret_fallback:
        import hashlib
        digest = hashlib.sha256(secret_fallback.encode("utf-8")).hexdigest()[:16]
        return f"sha256:{digest}"
    return "__unknown__"


def _safe_seat_label(entry_id: Any) -> str:
    """Log-safe pool seat identifier; never returns raw tokens or key hints."""
    return entry_id if isinstance(entry_id, str) and entry_id else "<unlabeled-seat>"


def _is_anthropic_403_pool_rotatable(
    agent: Any,
    status_code: Optional[int],
    error_context: Any,
    *,
    should_rotate_credential: Optional[bool] = None,
) -> bool:
    """Honor the single classifier decision when supplied; fall back to the shared
    Anthropic credential-403 predicate only for direct callers that omit it."""
    if status_code != 403:
        return False
    if (getattr(agent, "provider", "") or "").strip().lower() != "anthropic":
        return False
    if should_rotate_credential is not None:
        return bool(should_rotate_credential)
    if not isinstance(error_context, dict):
        return False
    from agent.error_classifier import _is_anthropic_credential_403_signal
    code = str(error_context.get("code") or error_context.get("reason") or "").strip().lower()
    haystack = " ".join(
        str(error_context.get(k) or "").lower()
        for k in ("message", "reason", "code", "error")
    )
    return _is_anthropic_credential_403_signal(code, haystack)


def _anthropic_rollover_active(agent: Any) -> bool:
    return bool(
        getattr(agent, "_fallback_activated", False)
        and getattr(agent, "_subscription_rollover_route", None)
        == (getattr(agent, "provider", None), getattr(agent, "model", None))
    )


def _sync_cached_pool_from_disk(
    pool: Any,
    fresh_entries: list[Any],
    fresh_token_pairs: Optional[dict[str, Any]] = None,
    *,
    keep_entry: Any = None,
) -> None:
    """Mirror authoritative disk rows into a cached pool in memory without saving to disk."""
    from contextlib import nullcontext, suppress
    pool_lock = getattr(pool, "_lock", None)
    with suppress(Exception), (pool_lock if pool_lock is not None else nullcontext()):
        raw_entries = getattr(pool, "_entries", None)
        if not isinstance(raw_entries, list):
            return
        synced = list(fresh_entries)
        synced_ids = {getattr(e, "id", None) for e in synced if getattr(e, "id", None)}
        if keep_entry is not None:
            keep_id = getattr(keep_entry, "id", None)
            if not keep_id or keep_id not in synced_ids:
                synced.append(keep_entry)
                if keep_id:
                    synced_ids.add(keep_id)
        pool._entries = synced
        token_pairs = getattr(pool, "_persisted_token_pairs", None)
        if isinstance(token_pairs, dict):
            token_pairs.clear()
            if isinstance(fresh_token_pairs, dict):
                token_pairs.update(fresh_token_pairs)
        cur_id = getattr(pool, "_current_id", None)
        if cur_id and cur_id not in synced_ids:
            pool._current_id = None


def _bench_and_snapshot_disk_anthropic_pool(
    agent: Any,
    *,
    safe_failed_id: Optional[str],
    failed_key: str,
    found_refresh: str,
    found_last_refresh: Any,
    error_context: Any,
) -> tuple[bool, bool, list[Any], dict[str, Any], list[Any], bool]:
    """Atomically reload, durably bench the matching failed seat on disk (only if still
    present with the same credential value/generation), and snapshot surviving rows &
    available candidates under ONE _auth_ctx + _auth_store_lock() critical section."""
    from contextlib import ExitStack, nullcontext, suppress
    import agent.credential_pool as _cp_mod
    import hermes_cli.auth as _auth_mod
    is_rollover = _anthropic_rollover_active(agent)
    profile_local = getattr(_auth_mod, "profile_local_auth", None)
    auth_ctx = profile_local() if (is_rollover and callable(profile_local)) else nullcontext()
    guarded_global_fn = getattr(_cp_mod, "_guarded_global_root", None)

    def _same_store(left: Any, right: Any) -> bool:
        same_fn = getattr(_auth_mod, "_is_same_auth_store", None) or getattr(_auth_mod, "_same_path", None)
        if callable(same_fn):
            try:
                return bool(same_fn(left, right))
            except Exception:
                pass
        try:
            return left.resolve(strict=False) == right.resolve(strict=False)
        except Exception:
            return left == right

    def _resolve_global_path(gaf_fn: Any, active_path: Any) -> Any:
        if is_rollover or not callable(gaf_fn):
            return None
        cand = gaf_fn()
        if cand is not None and callable(guarded_global_fn):
            cand = guarded_global_fn(cand)
        if cand is not None and active_path is not None and _same_store(active_path, cand):
            return None
        return cand

    with auth_ctx:
        try:
            af_fn = getattr(_auth_mod, "_auth_file_path", None)
            gaf_fn = getattr(_auth_mod, "_global_auth_file_path", None)
            af = af_fn() if callable(af_fn) else None
            gaf = _resolve_global_path(gaf_fn, af)
            af_exists = bool(af is not None and af.exists())
            gaf_exists = bool(gaf is not None and gaf.exists())
        except Exception:
            return False, False, [], {}, [], False
        if not (af_exists or gaf_exists):
            return False, False, [], {}, [], False
        auth_lock_fn = getattr(_auth_mod, "_auth_store_lock", None)
        try:
            with ExitStack() as lock_stack:
                if callable(auth_lock_fn):
                    # Follow Hermes's canonical auth-store lock hierarchy
                    # (hermes_cli.auth._provider_state_transaction & auth_oauth_grants):
                    # active/profile store OUTER -> global root store INNER.
                    # _auth_store_lock is re-entrant per thread per resolved path, so nested
                    # persist/write-through calls inside this transaction never drop the flock.
                    entered_primary = False
                    if af is not None:
                        lock_stack.enter_context(auth_lock_fn())
                        entered_primary = True
                    af = af_fn() if callable(af_fn) else af
                    gaf = _resolve_global_path(gaf_fn, af)
                    if gaf is not None:
                        try:
                            lock_stack.enter_context(auth_lock_fn(target_path=gaf))
                        except TypeError:
                            if not entered_primary:
                                lock_stack.enter_context(auth_lock_fn())
                else:
                    af = af_fn() if callable(af_fn) else af
                    gaf = _resolve_global_path(gaf_fn, af)
                if not ((af is not None and af.exists()) or (gaf is not None and gaf.exists())):
                    # Store was deleted concurrently before lock acquisition; do not fall
                    # back to legacy cached-pool persist (which would recreate/resurrect it).
                    return True, True, [], {}, [], True
                if gaf is not None and hasattr(_auth_mod, "_global_auth_store_cache"):
                    _auth_mod._global_auth_store_cache = None
                fresh_pool = _cp_mod.load_pool("anthropic")
                if safe_failed_id:
                    fresh_failed = next(
                        (e for e in fresh_pool.entries() if getattr(e, "id", None) == safe_failed_id),
                        None,
                    )
                elif failed_key:
                    fresh_failed = _entry_for(fresh_pool, None, failed_key)
                else:
                    fresh_failed = None
                failed_deleted_on_disk = fresh_failed is None
                if fresh_failed is not None:
                    fresh_key = str(
                        getattr(fresh_failed, "runtime_api_key", None)
                        or getattr(fresh_failed, "access_token", None)
                        or ""
                    ).strip()
                    fresh_refresh = str(getattr(fresh_failed, "refresh_token", None) or "").strip()
                    fresh_last_refresh = getattr(fresh_failed, "last_refresh", None)
                    same_generation = (
                        (not failed_key or (bool(fresh_key) and fresh_key == failed_key))
                        and (not found_refresh or not fresh_refresh or fresh_refresh == found_refresh)
                        and (
                            not found_last_refresh
                            or not fresh_last_refresh
                            or fresh_last_refresh == found_last_refresh
                        )
                    )
                    fresh_mark = getattr(fresh_pool, "_mark_exhausted", None)
                    if same_generation and callable(fresh_mark):
                        fresh_mark(
                            fresh_failed,
                            403,
                            error_context,
                            persist=True,
                            failure_reason="auth",
                        )
                        if failed_key:
                            same_key_siblings = [
                                s for s in fresh_pool.entries()
                                if getattr(s, "id", None) != getattr(fresh_failed, "id", None)
                                and str(
                                    getattr(s, "runtime_api_key", None)
                                    or getattr(s, "access_token", None)
                                    or ""
                                ).strip() == failed_key
                            ]
                            for sib in same_key_siblings:
                                fresh_mark(sib, 403, error_context, persist=False, failure_reason="auth")
                            if same_key_siblings and callable(getattr(fresh_pool, "_persist", None)):
                                fresh_pool._persist()
                avail_entries = []
                avail_fn = getattr(fresh_pool, "_available_entries", None)
                if callable(avail_fn):
                    with suppress(Exception):
                        avail_entries, _ = avail_fn(
                            clear_expired=False, refresh=False, model=getattr(agent, "model", None),
                        )
                fresh_entries = list(fresh_pool.entries())
                fresh_token_pairs = dict(getattr(fresh_pool, "_persisted_token_pairs", None) or {})
                if not callable(avail_fn):
                    avail_entries = list(fresh_entries)
                return True, True, fresh_entries, fresh_token_pairs, list(avail_entries), failed_deleted_on_disk
        except Exception:
            return True, False, [], {}, [], False


def _rotate_after_anthropic_403(
    agent: Any,
    pool: Any,
    *,
    has_retried_429: bool,
    error_context: Any = None,
    api_key_hint: Optional[str],
    credential_id: Optional[str],
    rotate_and_swap: Any,
    log: Any,
) -> tuple[bool, bool]:
    """Bench the 403-failing seat and rotate to the next healthy seat, strictly bounded
    to <= pool size attempts per turn so all-seats-403 falls back cleanly."""
    provider = (getattr(agent, "provider", "") or "").strip().lower() or "anthropic"
    entries_fn = getattr(pool, "entries", None)
    if not callable(entries_fn):
        return False, has_retried_429
    pool_entries = list(entries_fn())
    if not pool_entries:
        return False, has_retried_429
    pool_size = len(pool_entries)
    found = _entry_for(pool, credential_id, api_key_hint)
    found_id = getattr(found, "id", None) if found is not None else None
    safe_failed_id = (
        credential_id if isinstance(credential_id, str) and credential_id
        else (found_id if isinstance(found_id, str) and found_id else None)
    )
    failed_identity = _seat_dedup_key(safe_failed_id, api_key_hint)
    failed_label = _safe_seat_label(safe_failed_id)
    if getattr(agent, "_auth_pool_refresh_counts", None) is None:
        agent._auth_pool_refresh_counts = {}
    tracker = agent._auth_pool_refresh_counts.setdefault(
        (provider, "__entitlement_403__"), {"seen": set(), "rotations": 0},
    )
    if failed_identity in tracker["seen"]:
        log.warning(
            "Credential 403 — pool entry %s already failed with 403 this turn; "
            "stopping rotation and allowing fallback to activate.",
            failed_label,
        )
        return False, has_retried_429
    if tracker["rotations"] >= pool_size:
        log.warning(
            "Credential 403 — exhausted %s pool rotation attempts for %s; "
            "stopping rotation and allowing fallback to activate.",
            tracker["rotations"], provider,
        )
        return False, has_retried_429
    tracker["seen"].add(failed_identity)
    live_base_url = getattr(agent, "_anthropic_base_url", None) or getattr(agent, "base_url", None) or ""
    if provider == "anthropic":
        from contextlib import nullcontext, suppress
        from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
        _failed_key = str(
            api_key_hint
            or (
                (getattr(found, "runtime_api_key", None) or getattr(found, "access_token", None))
                if found is not None else ""
            )
            or getattr(agent, "_anthropic_api_key", None)
            or getattr(agent, "api_key", None)
            or ""
        ).strip()
        _found_refresh = str(getattr(found, "refresh_token", None) or "").strip() if found is not None else ""
        _found_last_refresh = getattr(found, "last_refresh", None) if found is not None else None
        (
            _store_exists,
            _loaded_ok,
            _fresh_entries,
            _fresh_token_pairs,
            _avail_entries,
            _failed_deleted_on_disk,
        ) = _bench_and_snapshot_disk_anthropic_pool(
            agent,
            safe_failed_id=safe_failed_id,
            failed_key=_failed_key,
            found_refresh=_found_refresh,
            found_last_refresh=_found_last_refresh,
            error_context=error_context,
        )
        if _store_exists:
            if not _loaded_ok:
                log.warning(
                    "Credential 403 — unable to reload authoritative Anthropic auth store; "
                    "stopping rotation without writing stale cached pool.",
                )
                return False, has_retried_429
            _is_rollover = _anthropic_rollover_active(agent)
            _admit_fn = getattr(pool, "admits_route_entry", None)
            _swap_fn = getattr(agent, "_swap_credential", None)
            _selected_cand = None
            for _cand in _avail_entries:
                _cid = getattr(_cand, "id", None)
                _ckey = getattr(_cand, "runtime_api_key", None)
                if _is_rollover and getattr(_cand, "source", None) == "claude_code":
                    continue
                if callable(_admit_fn) and not _admit_fn(_cand):
                    continue
                if _seat_dedup_key(_cid, _ckey) in tracker["seen"]:
                    continue
                if not _anthropic_pool_entry_matches_base_url(_cand, live_base_url):
                    continue
                if callable(_swap_fn) and _swap_fn(_cand) is not False:
                    _selected_cand = _cand
                    break
            if _selected_cand is not None:
                _sync_cached_pool_from_disk(pool, _fresh_entries, _fresh_token_pairs)
                with suppress(Exception):
                    pool._current_id = getattr(_selected_cand, "id", None)
                tracker["rotations"] += 1
                log.info(
                    "Credential 403 (entitlement 403) — rotated to pool entry %s",
                    _safe_seat_label(getattr(_selected_cand, "id", None)),
                )
                return True, False
            _keep_benched = None
            if _failed_deleted_on_disk:
                _mark_exhausted = getattr(pool, "_mark_exhausted", None)
                if callable(_mark_exhausted):
                    _pool_lock = getattr(pool, "_lock", None)
                    with suppress(Exception), (_pool_lock if _pool_lock is not None else nullcontext()):
                        _live_failed = _entry_for(pool, credential_id, api_key_hint)
                        if _live_failed is not None:
                            _keep_benched = _mark_exhausted(
                                _live_failed,
                                403,
                                error_context,
                                persist=False,
                                failure_reason="auth",
                            )
            _sync_cached_pool_from_disk(
                pool, _fresh_entries, _fresh_token_pairs, keep_entry=_keep_benched,
            )
            log.warning(
                "Credential 403 — no compatible healthy Anthropic pool seat remaining; "
                "stopping rotation and allowing fallback to activate.",
            )
            return False, has_retried_429
        # Pure in-memory pool fallback (no on-disk auth store exists).
        has_compatible_candidate = any(
            _seat_dedup_key(getattr(e, "id", None), getattr(e, "runtime_api_key", None)) not in tracker["seen"]
            and _anthropic_pool_entry_matches_base_url(e, live_base_url)
            for e in pool_entries
        )
        if not has_compatible_candidate:
            mark_exhausted = getattr(pool, "_mark_exhausted", None)
            if callable(mark_exhausted):
                pool_lock = getattr(pool, "_lock", None)
                with suppress(Exception), (pool_lock if pool_lock is not None else nullcontext()):
                    live_failed = _entry_for(pool, credential_id, api_key_hint)
                    if live_failed is not None:
                        mark_exhausted(
                            live_failed,
                            403,
                            error_context,
                            persist=False,
                            failure_reason="auth",
                        )
            log.warning(
                "Credential 403 — no Anthropic pool seat matches live endpoint; "
                "stopping rotation and allowing fallback to activate.",
            )
            return False, has_retried_429
    rotated = bool(rotate_and_swap(403, "entitlement 403"))
    if not rotated:
        return False, has_retried_429
    tracker["rotations"] += 1
    current_fn = getattr(pool, "current", None)
    current_entry = current_fn() if callable(current_fn) else None
    if provider == "anthropic" and current_entry is not None:
        from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
        if not _anthropic_pool_entry_matches_base_url(current_entry, live_base_url):
            log.warning(
                "Credential 403 — pool rotation yielded Anthropic seat %s incompatible with live endpoint; "
                "refusing rotation and allowing fallback to activate.",
                _safe_seat_label(getattr(current_entry, "id", None)),
            )
            return False, has_retried_429
    raw_current_id = getattr(current_entry, "id", None) if current_entry is not None else None
    raw_current_key = getattr(current_entry, "runtime_api_key", None) if current_entry is not None else None
    safe_current_id = raw_current_id if isinstance(raw_current_id, str) and raw_current_id else None
    has_current_identity = bool(safe_current_id or (isinstance(raw_current_key, str) and raw_current_key))
    current_identity = _seat_dedup_key(safe_current_id, raw_current_key) if has_current_identity else None
    if (current_identity is not None and current_identity in tracker["seen"]) or tracker["rotations"] >= pool_size:
        log.warning(
            "Credential 403 — pool rotation for %s exhausted or cycled back to %s; "
            "stopping rotation and allowing fallback to activate.",
            provider, _safe_seat_label(safe_current_id),
        )
        return False, has_retried_429
    return True, False


def recover_auth_failure(agent, pool, *, status_code, has_retried_429, error_context, api_key_hint,
                         credential_id, rotate_and_swap, should_rotate_credential=None):
    from agent.agent_runtime_helpers import _is_entitlement_403
    log = _ra().logger
    if _is_entitlement_403(agent, status_code, error_context):
        if _is_anthropic_403_pool_rotatable(
            agent, status_code, error_context,
            should_rotate_credential=should_rotate_credential,
        ):
            log.info(
                "Credential %s — entitlement-shaped 403 from %s; "
                "skipping pool refresh and rotating to next healthy pool seat.",
                status_code if status_code is not None else "auth",
                agent.provider or "provider",
            )
            return _rotate_after_anthropic_403(
                agent, pool, has_retried_429=has_retried_429,
                error_context=error_context,
                api_key_hint=api_key_hint, credential_id=credential_id,
                rotate_and_swap=rotate_and_swap, log=log,
            )
        log.info(
            "Credential %s — entitlement-shaped 403 from %s; "
            "skipping pool refresh (account lacks subscription, "
            "not a transient auth failure).", status_code if status_code is not None else "auth",
            agent.provider or "provider",
        )
        return False, has_retried_429
    if (
        status_code == 403
        and should_rotate_credential is False
        and (getattr(agent, "provider", "") or "").strip().lower() == "anthropic"
    ):
        return False, has_retried_429
'''

_OLD_TOKEN_IS_BLOCKED = '''    def token_is_blocked(self, token: str, *, model: Optional[str] = None) -> bool:
        """Whether a pool cooldown blocks *token* for *model*.

        Closes the paths that hand out a native Anthropic token without
        selecting it from the pool (env / borrowed credentials). Tokens the
        pool does not know fail open: no row can attribute a cooldown to them.
        """
        with self._lock:
            return any(
                entry.runtime_api_key == token and model_cooldown_until(entry, model) is not None
                for entry in self._entries
            )
'''

_NEW_TOKEN_IS_BLOCKED = '''    def token_is_blocked(self, token: str, *, model: Optional[str] = None) -> bool:
        """Whether a pool cooldown or seat-wide exhaustion blocks *token* for *model*.

        Closes the paths that hand out a native Anthropic token without
        selecting it from the pool (env / borrowed credentials). Tokens the
        pool does not know fail open: no row can attribute a cooldown to them.
        """
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, _exhausted_until
        now = time.time()
        with self._lock:
            sole = self._is_sole_credential()
            for entry in self._entries:
                if entry.runtime_api_key != token:
                    continue
                if model_cooldown_until(entry, model) is not None:
                    return True
                if entry.last_status == STATUS_DEAD:
                    return True
                if entry.last_status == STATUS_EXHAUSTED:
                    until = _exhausted_until(entry, sole_credential=sole)
                    if until is not None and now < until:
                        return True
            return False
'''

_OLD_TURN_RECOVERY_CALL = '''        classified_reason=classified.reason, error_context=_pool_error_context(agent, classified, error_context),
'''

_NEW_TURN_RECOVERY_CALL = '''        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        classified_reason=classified.reason, should_rotate_credential=getattr(classified, "should_rotate_credential", None),
        error_context=_pool_error_context(agent, classified, error_context),
'''

_OLD_RECOVER_POOL_SIG = '''    classified_reason: Optional[FailoverReason] = None,
    error_context: Optional[dict[str, Any]] = None, billing_unverified: bool = False,
'''

_NEW_RECOVER_POOL_SIG = '''    classified_reason: Optional[FailoverReason] = None,
    should_rotate_credential: Optional[bool] = None,
    error_context: Optional[dict[str, Any]] = None, billing_unverified: bool = False,
'''

_OLD_RECOVER_POOL_ROTATE_LOG = '''        _ra().logger.info(
            "Credential %s (%s) — rotated to pool entry %s",
            rotate_status, label, getattr(next_entry, "id", "?"),
        )
'''

_NEW_RECOVER_POOL_ROTATE_LOG = '''        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        if current_provider == "anthropic":
            from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
            _live_base = getattr(agent, "_anthropic_base_url", None) or getattr(agent, "base_url", None) or ""
            if not _anthropic_pool_entry_matches_base_url(next_entry, _live_base):
                _ra().logger.info(
                    "Credential %s (%s) — Anthropic pool entry %s does not match live endpoint; not swapping",
                    rotate_status, label, getattr(next_entry, "id", "?"),
                )
                return False
        _ra().logger.info(
            "Credential %s (%s) — rotated to pool entry %s",
            rotate_status, label, getattr(next_entry, "id", "?"),
        )
'''

_OLD_RECOVER_POOL_AUTH = '''    if effective_reason == FailoverReason.auth:
        return _recover_auth_failure(
            agent, pool, status_code=status_code, has_retried_429=has_retried_429,
            error_context=error_context, api_key_hint=api_key_hint, credential_id=credential_id,
            rotate_and_swap=_rotate_and_swap,
        )
'''

_NEW_RECOVER_POOL_AUTH = '''    if effective_reason == FailoverReason.auth:
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        return _recover_auth_failure(
            agent, pool, status_code=status_code, has_retried_429=has_retried_429,
            error_context=error_context, api_key_hint=api_key_hint, credential_id=credential_id,
            rotate_and_swap=_rotate_and_swap, should_rotate_credential=should_rotate_credential,
        )
'''

_OLD_RESOLVE_POOL_TOKEN = '''def _resolve_anthropic_pool_token(*, skip_borrowed: bool = False) -> Optional[str]:
    """First available Anthropic OAuth token from credential_pool, read-only: enumerates with ``clear_expired=False,
    refresh=False`` (never ``select()``) so diagnostic call sites (account_usage, ``hermes models``) never mutate
    auth.json or hit the network; refresh-on-expiry belongs to the API call path's pool recovery."""
    try:
        from agent.credential_pool import AUTH_TYPE_OAUTH, load_pool
        entries, _pending = load_pool("anthropic")._available_entries(clear_expired=False, refresh=False)
    except Exception:
        logger.debug("Failed to read Anthropic credential_pool", exc_info=True)
        return None
    for entry in entries:
        if skip_borrowed and entry.source == "claude_code":
            continue
'''

_NEW_RESOLVE_POOL_TOKEN = '''# HERMES_ANTHROPIC_403_POOL_ROTATION_v1
def _is_official_anthropic_endpoint(base_url: Any) -> bool:
    """True when *base_url* is empty (native default) or an official Anthropic/Claude host."""
    text = str(base_url or "").strip()
    if not text:
        return True
    from utils import base_url_host_matches
    return any(base_url_host_matches(text, host) for host in ("anthropic.com", "claude.com"))


def _anthropic_pool_entry_matches_base_url(entry: Any, base_url: Any) -> bool:
    """Endpoint-compatibility guard: native-only pool seats (empty or official Anthropic base_url)
    must never be adopted for a third-party proxy endpoint, and proxy-scoped seats must only serve
    their own normalized route."""
    target = str(base_url or "").strip()
    entry_url = str(getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None) or "").strip()
    if _is_official_anthropic_endpoint(target):
        return _is_official_anthropic_endpoint(entry_url)
    if not entry_url or _is_official_anthropic_endpoint(entry_url):
        return False
    from hermes_cli.route_identity import normalize_route_base_url
    return normalize_route_base_url(entry_url) == normalize_route_base_url(target)


def _resolve_anthropic_pool_token(
    *, skip_borrowed: bool = False, model: Optional[str] = None, base_url: Optional[str] = None
) -> Optional[str]:
    """First available Anthropic OAuth token from credential_pool, read-only: enumerates with ``clear_expired=False,
    refresh=False`` (never ``select()``) so diagnostic call sites (account_usage, ``hermes models``) never mutate
    auth.json or hit the network; refresh-on-expiry belongs to the API call path's pool recovery."""
    try:
        from agent.credential_pool import AUTH_TYPE_OAUTH, load_pool
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        pool_kwargs: dict[str, Any] = {"clear_expired": False, "refresh": False}
        if model:
            pool_kwargs["model"] = model
        entries, _pending = load_pool("anthropic")._available_entries(**pool_kwargs)
    except Exception:
        logger.debug("Failed to read Anthropic credential_pool", exc_info=True)
        return None
    for entry in entries:
        if skip_borrowed and entry.source == "claude_code":
            continue
        if not _anthropic_pool_entry_matches_base_url(entry, base_url):
            continue
'''

_OLD_RESOLVE_ANTHROPIC_TOKEN = '''def resolve_anthropic_token(*, model: Optional[str] = None) -> Optional[str]:
    """Resolve an Anthropic token from all sources in priority order (see module docstring).

    With *model*, a token the credential pool has benched for that model resolves to ``None``
    instead of being handed straight back to the caller that just saw it rate-limited."""
    _read_creds = functools.cache(read_claude_code_credentials)  # read the file at most once per resolve
    token = _first_env("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return _available_anthropic_token(
            _prefer_refreshable_claude_code_token(token, _read_creds()) or token, model,
        )
    api_key = _first_env("ANTHROPIC_API_KEY")  # an explicit API key must not be shadowed by discovered OAuth creds
    if api_key:
        return _available_anthropic_token(api_key, model)
    # The pool's claude_code row mirrors the same externally owned refresh grant.
    return _available_anthropic_token(
        _resolve_anthropic_pool_token(skip_borrowed=True) or _resolve_claude_code_token_from_credentials(_read_creds()),
        model,
    )
'''

_NEW_RESOLVE_ANTHROPIC_TOKEN = '''def resolve_anthropic_token(
    *, model: Optional[str] = None, base_url: Optional[str] = None, allow_pool_fallback: bool = True
) -> Optional[str]:
    """Resolve an Anthropic token from all sources in priority order (see module docstring).

    With *model*, a token the credential pool has benched for that model or seat-wide is skipped
    in favor of any remaining healthy pooled OAuth seat on official Anthropic endpoints instead of
    being handed straight back. Non-official proxy endpoints never fall back to native-only pool seats."""
    _read_creds = functools.cache(read_claude_code_credentials)  # read the file at most once per resolve
    token = _first_env("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        effective_base_url = (base_url if base_url is not None else _first_env("ANTHROPIC_BASE_URL")) or ""
        official_endpoint = _is_official_anthropic_endpoint(effective_base_url)
        preferred = (
            _prefer_refreshable_claude_code_token(token, _read_creds()) or token
            if official_endpoint else token
        )
        resolved = _available_anthropic_token(preferred, model)
        if resolved:
            return resolved
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1: env OAuth token is benched in the pool;
        # fall back to a healthy pooled OAuth seat ONLY when allowed and targeting an official
        # Anthropic endpoint (never leak a native-only pool token to a third-party proxy).
        if not allow_pool_fallback or not official_endpoint:
            return None
        return _available_anthropic_token(
            _resolve_anthropic_pool_token(skip_borrowed=True, model=model, base_url=effective_base_url),
            model,
        )
    api_key = _first_env("ANTHROPIC_API_KEY")  # an explicit API key must not be shadowed by discovered OAuth creds
    if api_key:
        return _available_anthropic_token(api_key, model)
    # The pool's claude_code row mirrors the same externally owned refresh grant.
    effective_base_url = (base_url if base_url is not None else _first_env("ANTHROPIC_BASE_URL")) or ""
    pool_token = (
        _resolve_anthropic_pool_token(skip_borrowed=True, model=model, base_url=effective_base_url)
        if allow_pool_fallback else None
    )
    fallback_token = (
        pool_token
        or (
            _resolve_claude_code_token_from_credentials(_read_creds())
            if _is_official_anthropic_endpoint(effective_base_url) else None
        )
    )
    return _available_anthropic_token(fallback_token, model)
'''

_OLD_CLIENT_LIFECYCLE_DEF = '''    def _try_refresh_anthropic_client_credentials(self) -> bool:
'''

_NEW_CLIENT_LIFECYCLE_DEF = '''    # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
    def _commit_refreshed_anthropic_token(self, new_token: str, *, entry_id: Optional[str] = None) -> bool:
        new_token = new_token.strip() if isinstance(new_token, str) else ""
        if not new_token or new_token == self._anthropic_api_key:
            return False
        with suppress(Exception):
            self._anthropic_client.close()
        try:
            base_url = getattr(self, "_anthropic_base_url", None)
            self._anthropic_client = self._build_direct_anthropic_client(new_token, base_url)
        except Exception as exc:
            logger.warning("Failed to rebuild Anthropic client after credential refresh: %s", exc)
            return False
        old_token = self._anthropic_api_key
        self._anthropic_api_key, self._is_anthropic_oauth = new_token, self._anthropic_oauth_flag(new_token)
        if self.api_key == old_token:
            self.api_key = new_token
            if entry_id:
                self._credential_pool_entry_id = entry_id
            elif getattr(self, "_credential_pool", None) is not None:
                with suppress(Exception):
                    _matched_id = self._credential_pool.entry_id_for_api_key(new_token)
                    if not _matched_id and getattr(self._credential_pool, "provider", None) == "anthropic":
                        from agent.credential_pool import load_pool
                        _matched_id = load_pool("anthropic").entry_id_for_api_key(new_token)
                    if _matched_id:
                        self._credential_pool_entry_id = _matched_id
        elif entry_id:
            self._credential_pool_entry_id = entry_id
        cc = getattr(self, "context_compressor", None)
        if cc is not None and getattr(cc, "api_key", None) == old_token:
            cc.api_key = new_token
        rt = getattr(self, "_primary_runtime", None) or {}
        if rt.get("anthropic_api_key") == old_token:
            rt["is_anthropic_oauth"] = self._is_anthropic_oauth
        for k in ("api_key", "anthropic_api_key", "compressor_api_key"):
            if rt.get(k) == old_token:
                rt[k] = new_token
        from agent.auxiliary_key_rotation import rotate_runtime_main_api_key
        rotate_runtime_main_api_key(old_token, new_token)
        return True

    def _try_refresh_anthropic_client_credentials(self) -> bool:
'''

_OLD_CLIENT_LIFECYCLE_GUARD = '''        anthropic_base_url = getattr(self, "_anthropic_base_url", "") or ""
        if (
            self.api_mode != "anthropic_messages" or not hasattr(self, "_anthropic_api_key")
            or self.provider != "anthropic" or base_url_host_matches(anthropic_base_url, "azure.com")
        ):
            return False
        # Off the official hosts (a /anthropic proxy the resolver accepts, or a URL-bearing alias,
        # #28660) rotate only a credential the endpoint already holds: swapping a custom key for
        # ANTHROPIC_API_KEY / the OAuth token would leak it (#17829). Hostname match, not substring,
        # so ``proxy.example/anthropic.com`` stays foreign.
        official_host = not anthropic_base_url or any(
            base_url_host_matches(anthropic_base_url, host) for host in ("anthropic.com", "claude.com"))
        current_key = str(self._anthropic_api_key or "")
        if not official_host and not (current_key.startswith("sk-ant-") or getattr(self, "_is_anthropic_oauth", False)):
            return False
'''

_NEW_CLIENT_LIFECYCLE_GUARD = '''        anthropic_base_url = getattr(self, "_anthropic_base_url", "") or ""
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1: clear any prior rollover dispatch block before
        # early returns, and run pinned active-rollover profile-local validation BEFORE every
        # host/token-shape early return (including azure.com and opaque proxy keys) while
        # preserving exact non-rollover azure.com and opaque proxy behavior.
        self._anthropic_rollover_dispatch_blocked = None
        if (
            self.api_mode != "anthropic_messages" or not hasattr(self, "_anthropic_api_key")
            or self.provider != "anthropic"
        ):
            return False
        _is_rollover = (
            getattr(self, "_fallback_activated", False)
            and getattr(self, "_subscription_rollover_route", None) == (self.provider, self.model)
        )
        _is_azure_host = base_url_host_matches(anthropic_base_url, "azure.com")
        if _is_azure_host and not _is_rollover:
            return False
        # Off the official hosts (a /anthropic proxy the resolver accepts, or a URL-bearing alias,
        # #28660) rotate only a credential the endpoint already holds: swapping a custom key for
        # ANTHROPIC_API_KEY / the OAuth token would leak it (#17829). Hostname match, not substring,
        # so ``proxy.example/anthropic.com`` stays foreign.
        official_host = not anthropic_base_url or any(
            base_url_host_matches(anthropic_base_url, host) for host in ("anthropic.com", "claude.com"))
        current_key = str(self._anthropic_api_key or "")
        _bound_pool = getattr(self, "_credential_pool", None)
        _bound_entry_id = getattr(self, "_credential_pool_entry_id", None)
        _skip_for_proxy_token_shape = (
            not official_host
            and not (current_key.startswith("sk-ant-") or getattr(self, "_is_anthropic_oauth", False))
        )
        if _skip_for_proxy_token_shape and not _is_rollover:
            return False
        if (
            _bound_pool is not None
            and getattr(_bound_pool, "provider", None) == "anthropic"
            and _bound_entry_id
        ):
            from contextlib import nullcontext
            import hermes_cli.auth as _auth_mod
            from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
            _entries_fn = getattr(_bound_pool, "entries", None)
            _cached_entry = (
                next((e for e in _entries_fn() if getattr(e, "id", None) == _bound_entry_id), None)
                if callable(_entries_fn) else None
            )
            _cached_is_claude_code = (
                _cached_entry is not None and getattr(_cached_entry, "source", None) == "claude_code"
            )
            if not _cached_is_claude_code:
                _profile_local_auth = getattr(_auth_mod, "profile_local_auth", None)
                _make_auth_ctx = (
                    (lambda: _profile_local_auth())
                    if (_is_rollover and callable(_profile_local_auth))
                    else nullcontext
                )
                _admit_fn = getattr(_bound_pool, "admits_route_entry", None) if _is_rollover else None
                _fresh_pool = None
                _fresh_entries = []
                _auth_entry = None
                _disk_pool_authoritative = False
                with suppress(Exception):
                    from agent.credential_pool import load_pool
                    with _make_auth_ctx():
                        _rollover_auth_store_present = True
                        if _is_rollover:
                            _auth_file_fn = getattr(_auth_mod, "_auth_file_path", None)
                            if callable(_auth_file_fn):
                                _af = _auth_file_fn()
                                _rollover_auth_store_present = bool(_af is not None and _af.exists())
                        if _rollover_auth_store_present:
                            _fresh_pool = load_pool("anthropic")
                            _fresh_entries = list(_fresh_pool.entries())
                            _auth_entry = next(
                                (e for e in _fresh_entries if getattr(e, "id", None) == _bound_entry_id),
                                None,
                            )
                            _disk_pool_authoritative = bool(_fresh_entries)
                            if not _disk_pool_authoritative:
                                _auth_file_fn = getattr(_auth_mod, "_auth_file_path", None)
                                if callable(_auth_file_fn):
                                    _af = _auth_file_fn()
                                    if _af is not None and _af.exists():
                                        _disk_pool_authoritative = True
                            if not _is_rollover and _auth_entry is None and not _disk_pool_authoritative:
                                _auth_entry = _cached_entry
                                _fresh_pool = _bound_pool
                if not _is_rollover and _auth_entry is None and _fresh_pool is None:
                    _auth_entry = _cached_entry
                    _fresh_pool = _bound_pool
                _model_name = getattr(self, "model", None)
                _fresh_blocked_fn = getattr(_fresh_pool, "token_is_blocked", None)
                _bound_blocked_fn = getattr(_bound_pool, "token_is_blocked", None)
                _auth_admitted = not callable(_admit_fn) or (_auth_entry is not None and bool(_admit_fn(_auth_entry)))
                if (
                    _auth_entry is not None
                    and getattr(_auth_entry, "source", None) != "claude_code"
                    and _auth_admitted
                    and _anthropic_pool_entry_matches_base_url(_auth_entry, anthropic_base_url)
                ):
                    _auth_token = (
                        getattr(_auth_entry, "runtime_api_key", None)
                        or getattr(_auth_entry, "access_token", "")
                        or ""
                    ).strip()
                    _auth_blocked = bool(
                        _auth_token and callable(_fresh_blocked_fn)
                        and _fresh_blocked_fn(_auth_token, model=_model_name)
                    )
                    if (
                        not _auth_blocked
                        and _auth_token
                        and _auth_token == current_key
                        and _fresh_pool is not _bound_pool
                        and callable(_bound_blocked_fn)
                    ):
                        _auth_blocked = bool(_bound_blocked_fn(_auth_token, model=_model_name))
                    if _auth_token and not _auth_blocked:
                        if _auth_token == current_key:
                            return False
                        with suppress(Exception):
                            if _cached_entry is not None and callable(getattr(_bound_pool, "_replace_entry", None)):
                                _bound_pool._replace_entry(_cached_entry, _auth_entry)
                                if (
                                    _fresh_pool is not None
                                    and isinstance(getattr(_bound_pool, "_persisted_token_pairs", None), dict)
                                    and isinstance(getattr(_fresh_pool, "_persisted_token_pairs", None), dict)
                                    and _bound_entry_id in _fresh_pool._persisted_token_pairs
                                ):
                                    _bound_pool._persisted_token_pairs[_bound_entry_id] = (
                                        _fresh_pool._persisted_token_pairs[_bound_entry_id]
                                    )
                        if self._commit_refreshed_anthropic_token(_auth_token, entry_id=_bound_entry_id):
                            return True
                        if _is_rollover:
                            self._anthropic_rollover_dispatch_blocked = (
                                f"Anthropic rollover pool seat {_bound_entry_id} failed to adopt updated credential."
                            )
                        return False
                if _is_rollover:
                    _replacement = None
                    _replacement_token = ""
                    if _fresh_pool is not None:
                        _avail_entries = []
                        _avail_fn = getattr(_fresh_pool, "_available_entries", None)
                        if callable(_avail_fn):
                            with suppress(Exception), _make_auth_ctx():
                                _avail_entries, _ = _avail_fn(clear_expired=False, refresh=False, model=_model_name)
                        else:
                            _avail_entries = list(_fresh_entries)
                        for _cand in _avail_entries:
                            if getattr(_cand, "source", None) == "claude_code":
                                continue
                            if callable(_admit_fn) and not _admit_fn(_cand):
                                continue
                            if not _anthropic_pool_entry_matches_base_url(_cand, anthropic_base_url):
                                continue
                            _cand_token = (
                                getattr(_cand, "runtime_api_key", None)
                                or getattr(_cand, "access_token", "")
                                or ""
                            ).strip()
                            if not _cand_token or _cand_token == current_key:
                                continue
                            if callable(_fresh_blocked_fn) and _fresh_blocked_fn(_cand_token, model=_model_name):
                                continue
                            _replacement = _cand
                            _replacement_token = _cand_token
                            break
                    if _replacement is not None and _replacement_token:
                        _rep_id = getattr(_replacement, "id", None)
                        with suppress(Exception):
                            if (
                                _cached_entry is not None
                                and _auth_entry is not None
                                and callable(getattr(_bound_pool, "_replace_entry", None))
                            ):
                                _bound_pool._replace_entry(_cached_entry, _auth_entry)
                            _existing_rep = (
                                next((e for e in _entries_fn() if getattr(e, "id", None) == _rep_id), None)
                                if callable(_entries_fn) and _rep_id else None
                            )
                            if _existing_rep is not None and callable(getattr(_bound_pool, "_replace_entry", None)):
                                _bound_pool._replace_entry(_existing_rep, _replacement)
                                if (
                                    _fresh_pool is not None
                                    and isinstance(getattr(_bound_pool, "_persisted_token_pairs", None), dict)
                                    and isinstance(getattr(_fresh_pool, "_persisted_token_pairs", None), dict)
                                    and _rep_id in _fresh_pool._persisted_token_pairs
                                ):
                                    _bound_pool._persisted_token_pairs[_rep_id] = (
                                        _fresh_pool._persisted_token_pairs[_rep_id]
                                    )
                            elif _fresh_pool is not None and _fresh_pool is not _bound_pool:
                                _route_admit = getattr(_bound_pool, "_route_admission", None)
                                if callable(_route_admit) and hasattr(_fresh_pool, "restrict_route_selection"):
                                    _fresh_pool.restrict_route_selection(_route_admit)
                                self._credential_pool = _fresh_pool
                        if self._commit_refreshed_anthropic_token(_replacement_token, entry_id=_rep_id):
                            return True
                    self._anthropic_rollover_dispatch_blocked = (
                        f"Anthropic rollover pool seat {_bound_entry_id} is blocked or unavailable on disk "
                        "and no eligible healthy replacement seat is available."
                    )
                    return False
        if _is_azure_host or _skip_for_proxy_token_shape:
            return False
        if not official_host and not _is_rollover:
            try:
                from agent.anthropic_credentials import (
                    _anthropic_pool_entry_matches_base_url,
                    resolve_anthropic_token,
                )
                _proxy_token = resolve_anthropic_token(
                    model=self.model, base_url=anthropic_base_url, allow_pool_fallback=False,
                )
            except Exception as exc:
                logger.debug("Anthropic credential refresh failed: %s", exc)
                return False
            _proxy_token = _proxy_token.strip() if isinstance(_proxy_token, str) else ""
            if not _proxy_token or _proxy_token == self._anthropic_api_key:
                return False
            with suppress(Exception):
                from agent.credential_pool import load_pool
                _check_pools = [p for p in (getattr(self, "_credential_pool", None), load_pool("anthropic")) if p is not None]
                for _cp in _check_pools:
                    _cp_entries = getattr(_cp, "entries", None)
                    if not callable(_cp_entries):
                        continue
                    _matching = [
                        e for e in _cp_entries()
                        if (getattr(e, "runtime_api_key", None) or getattr(e, "access_token", "") or "").strip() == _proxy_token
                    ]
                    if _matching and not any(_anthropic_pool_entry_matches_base_url(e, anthropic_base_url) for e in _matching):
                        return False
            return self._commit_refreshed_anthropic_token(_proxy_token)
'''

_OLD_CLIENT_LIFECYCLE_SWAP_GUARD = '''        # Refuse BEFORE any state changes below: a refused swap must leave the agent exactly as it was.
        from hermes_cli.anon_auth import route_can_serve_model
'''

_NEW_CLIENT_LIFECYCLE_SWAP_GUARD = '''        # Refuse BEFORE any state changes below: a refused swap must leave the agent exactly as it was.
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        if (getattr(self, "provider", "") or "").strip().lower() == "anthropic":
            from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
            _live_base = getattr(self, "_anthropic_base_url", None) or getattr(self, "base_url", None) or ""
            if not _anthropic_pool_entry_matches_base_url(entry, _live_base):
                logger.info(
                    "Credential %s skipped: Anthropic pool seat does not match live endpoint",
                    getattr(entry, "id", "?"),
                )
                return False
        from hermes_cli.anon_auth import route_can_serve_model
'''

_OLD_CLIENT_LIFECYCLE_CREATE_REQ = '''        if self.api_mode == "anthropic_messages":
            self._try_refresh_anthropic_client_credentials()
        key = self._request_anthropic_client_key()
'''

_NEW_CLIENT_LIFECYCLE_CREATE_REQ = '''        if self.api_mode == "anthropic_messages":
            self._try_refresh_anthropic_client_credentials()
            # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
            if getattr(self, "_anthropic_rollover_dispatch_blocked", None):
                from hermes_cli.auth import AuthError
                raise AuthError(
                    self._anthropic_rollover_dispatch_blocked,
                    provider="anthropic",
                    code="rollover_seat_blocked",
                )
        key = self._request_anthropic_client_key()
'''

_OLD_CLIENT_LIFECYCLE_MSG_CREATE = '''        if client is None and self.api_mode == "anthropic_messages":
            self._try_refresh_anthropic_client_credentials()
        # Strips Responses-only kwargs that leak in under an api_mode-flip race.
'''

_NEW_CLIENT_LIFECYCLE_MSG_CREATE = '''        if client is None and self.api_mode == "anthropic_messages":
            self._try_refresh_anthropic_client_credentials()
            # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
            if getattr(self, "_anthropic_rollover_dispatch_blocked", None):
                from hermes_cli.auth import AuthError
                raise AuthError(
                    self._anthropic_rollover_dispatch_blocked,
                    provider="anthropic",
                    code="rollover_seat_blocked",
                )
        # Strips Responses-only kwargs that leak in under an api_mode-flip race.
'''

_OLD_RP_TOKEN_OR_RAISE = '''def _anthropic_token_or_raise(*, model: str | None = None) -> str:
    from agent.anthropic_credentials import resolve_anthropic_token
    token = resolve_anthropic_token(model=model)
    if not token:
        # A key the pool benched for *this* model is not a missing credential; telling the
        # user to re-authenticate would send them chasing a cooldown that lifts on its own.
        if model and resolve_anthropic_token():
            raise AuthError(f"Anthropic credentials are rate-limited for {model}; "
                            "other Claude models remain available (see `hermes auth list`).")
        raise AuthError(_NO_ANTHROPIC_CREDENTIALS_MSG)
    return token
'''

_NEW_RP_TOKEN_OR_RAISE = '''def _anthropic_token_or_raise(*, model: str | None = None, base_url: str | None = None) -> str:
    # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
    from agent.anthropic_credentials import _is_official_anthropic_endpoint, resolve_anthropic_token
    _non_official = bool(base_url and not _is_official_anthropic_endpoint(base_url))
    token = resolve_anthropic_token(model=model, base_url=base_url) if _non_official else resolve_anthropic_token(model=model)
    if not token:
        # A key the pool benched for *this* model is not a missing credential; telling the
        # user to re-authenticate would send them chasing a cooldown that lifts on its own.
        _unscoped = resolve_anthropic_token(base_url=base_url) if _non_official else resolve_anthropic_token()
        if model and _unscoped:
            raise AuthError(f"Anthropic credentials are rate-limited for {model}; "
                            "other Claude models remain available (see `hermes auth list`).")
        raise AuthError(_NO_ANTHROPIC_CREDENTIALS_MSG)
    return token
'''

_OLD_RP_POOL_ENTRY_URL = '''    if provider == "anthropic":
        return "anthropic_messages", _anthropic_cfg_base_url(model_cfg) or base_url or _ANTHROPIC_DEFAULT_BASE_URL
'''

_NEW_RP_POOL_ENTRY_URL = '''    if provider == "anthropic":
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        cfg_base = _anthropic_cfg_base_url(model_cfg)
        if cfg_base:
            from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
            if _anthropic_pool_entry_matches_base_url(entry, cfg_base):
                return "anthropic_messages", cfg_base
        return "anthropic_messages", base_url or _ANTHROPIC_DEFAULT_BASE_URL
'''

_OLD_RP_RESOLVE_POOL = '''    if not (pool and pool.has_credentials()):
        return None
    entry = pool.select(model=target_model or None)
    if entry is None:
        return None
'''

_NEW_RP_RESOLVE_POOL = '''    if not (pool and pool.has_credentials()):
        return None
    # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
    _anthropic_target_base = (
        (explicit_base_url or _anthropic_cfg_base_url(model_cfg) or "").strip()
        if provider == "anthropic" else ""
    )
    if _anthropic_target_base:
        from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
        if hasattr(pool, "restrict_route_selection"):
            pool.restrict_route_selection(
                lambda row: _anthropic_pool_entry_matches_base_url(row, _anthropic_target_base)
            )
    entry = pool.select(model=target_model or None)
    if entry is None:
        return None
    if _anthropic_target_base:
        from agent.anthropic_credentials import _anthropic_pool_entry_matches_base_url
        if not _anthropic_pool_entry_matches_base_url(entry, _anthropic_target_base):
            return None
'''

_OLD_RP_EXPLICIT_ANTHROPIC = '''def _explicit_anthropic(requested_provider, model_cfg, api_key, base_url, target_model):
    base_url = base_url or _anthropic_cfg_base_url(model_cfg) or _ANTHROPIC_DEFAULT_BASE_URL
    api_key = api_key or _anthropic_token_or_raise(model=target_model)
'''

_NEW_RP_EXPLICIT_ANTHROPIC = '''def _explicit_anthropic(requested_provider, model_cfg, api_key, base_url, target_model):
    base_url = base_url or _anthropic_cfg_base_url(model_cfg) or _ANTHROPIC_DEFAULT_BASE_URL
    # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
    api_key = api_key or _anthropic_token_or_raise(model=target_model, base_url=base_url)
'''

_OLD_RP_ENV_ANTHROPIC = '''    else:
        token = _anthropic_token_or_raise(model=target_model)
    return _runtime("anthropic", "anthropic_messages", base_url, token, source="env", requested_provider=requested_provider)
'''

_NEW_RP_ENV_ANTHROPIC = '''    else:
        # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
        token = _anthropic_token_or_raise(model=target_model, base_url=base_url)
    return _runtime("anthropic", "anthropic_messages", base_url, token, source="env", requested_provider=requested_provider)
'''

_OLD_CLIENT_LIFECYCLE_SYNC = '''        # Claude Code revokes the old token on refresh: every holder of it (agent.api_key, the compressor's
        # main_runtime, the fallback-restore snapshot) must move too, or compression 401s for the session's life.
        if self.api_key == old_token:
            self.api_key = new_token
'''

_NEW_CLIENT_LIFECYCLE_SYNC = '''        # Claude Code revokes the old token on refresh: every holder of it (agent.api_key, the compressor's
        # main_runtime, the fallback-restore snapshot) must move too, or compression 401s for the session's life.
        if self.api_key == old_token:
            self.api_key = new_token
            # HERMES_ANTHROPIC_403_POOL_ROTATION_v1
            if getattr(self, "_credential_pool", None) is not None:
                with suppress(Exception):
                    _matched_id = self._credential_pool.entry_id_for_api_key(new_token)
                    if not _matched_id and getattr(self._credential_pool, "provider", None) == "anthropic":
                        from agent.credential_pool import load_pool
                        _matched_id = load_pool("anthropic").entry_id_for_api_key(new_token)
                    if _matched_id:
                        self._credential_pool_entry_id = _matched_id
'''

_FILES = (
    (
        "agent/error_classifier.py",
        (
            (_OLD_TRANSIENT_403_CODES, _NEW_TRANSIENT_403_CODES),
            (_OLD_STATUS_403, _NEW_STATUS_403),
        ),
    ),
    ("agent/turn_recovery.py", ((_OLD_TURN_RECOVERY_CALL, _NEW_TURN_RECOVERY_CALL),)),
    (
        "agent/agent_runtime_helpers.py",
        (
            (_OLD_RECOVER_POOL_SIG, _NEW_RECOVER_POOL_SIG),
            (_OLD_RECOVER_POOL_ROTATE_LOG, _NEW_RECOVER_POOL_ROTATE_LOG),
            (_OLD_RECOVER_POOL_AUTH, _NEW_RECOVER_POOL_AUTH),
        ),
    ),
    ("agent/credential_auth_recovery.py", ((_OLD_RECOVER_AUTH, _NEW_RECOVER_AUTH),)),
    ("agent/credential_pool_model_cooldowns.py", ((_OLD_TOKEN_IS_BLOCKED, _NEW_TOKEN_IS_BLOCKED),)),
    (
        "agent/anthropic_credentials.py",
        (
            (_OLD_RESOLVE_POOL_TOKEN, _NEW_RESOLVE_POOL_TOKEN),
            (_OLD_RESOLVE_ANTHROPIC_TOKEN, _NEW_RESOLVE_ANTHROPIC_TOKEN),
        ),
    ),
    (
        "agent/client_lifecycle.py",
        (
            (_OLD_CLIENT_LIFECYCLE_CREATE_REQ, _NEW_CLIENT_LIFECYCLE_CREATE_REQ),
            (_OLD_CLIENT_LIFECYCLE_DEF, _NEW_CLIENT_LIFECYCLE_DEF),
            (_OLD_CLIENT_LIFECYCLE_GUARD, _NEW_CLIENT_LIFECYCLE_GUARD),
            (_OLD_CLIENT_LIFECYCLE_SYNC, _NEW_CLIENT_LIFECYCLE_SYNC),
            (_OLD_CLIENT_LIFECYCLE_SWAP_GUARD, _NEW_CLIENT_LIFECYCLE_SWAP_GUARD),
            (_OLD_CLIENT_LIFECYCLE_MSG_CREATE, _NEW_CLIENT_LIFECYCLE_MSG_CREATE),
        ),
    ),
    (
        "hermes_cli/runtime_provider.py",
        (
            (_OLD_RP_TOKEN_OR_RAISE, _NEW_RP_TOKEN_OR_RAISE),
            (_OLD_RP_POOL_ENTRY_URL, _NEW_RP_POOL_ENTRY_URL),
            (_OLD_RP_RESOLVE_POOL, _NEW_RP_RESOLVE_POOL),
            (_OLD_RP_EXPLICIT_ANTHROPIC, _NEW_RP_EXPLICIT_ANTHROPIC),
            (_OLD_RP_ENV_ANTHROPIC, _NEW_RP_ENV_ANTHROPIC),
        ),
    ),
)


def patch_anthropic_403_pool_rotation_v1(root: Path) -> bool:
    """Preflight every exact pre/postimage and syntax before changing any file."""
    planned: list[tuple[Path, str, str]] = []
    for rel, replacements in _FILES:
        path = root / rel
        original = path.read_text(encoding="utf-8")
        source = original
        for old, new in replacements:
            if source.count(new) == 1:
                continue
            if new in source or source.count(old) != 1:
                raise RuntimeError(f"{rel}: Anthropic 403 pool rotation anchor missing or ambiguous")
            source = source.replace(old, new, 1)
        compile(source, str(path), "exec")
        planned.append((path, original, source))
    changed = False
    for path, original, source in planned:
        if source != original:
            path.write_text(source, encoding="utf-8")
            changed = True
    return changed
