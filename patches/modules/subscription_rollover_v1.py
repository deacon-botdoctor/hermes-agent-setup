"""Port long-429 recovery to the pinned native turn and ACP boundaries."""
from pathlib import Path

MARKER = "HERMES_SUBSCRIPTION_ROLLOVER_v1"

_EDITS = {
    "agent/fallback_cooldown.py": (
        ('def switch_deferred_by_reset(', '''# HERMES_SUBSCRIPTION_ROLLOVER_v1
SUBSCRIPTION_ROLLOVER_SECONDS = 120


def is_long_subscription_rate_limit(agent, reason, status_code, reset_at) -> bool:
    """Only resolved native subscription routes use immediate rollover."""
    provider = (getattr(agent, "provider", "") or "").strip().lower()
    if provider not in {"anthropic", "openai-codex", "xai-oauth"} or reason != FailoverReason.rate_limit or status_code != 429:
        return False
    delay = _provider_reset_delay(reset_at)
    if delay is None or delay <= SUBSCRIPTION_ROLLOVER_SECONDS:
        return False
    base_url = str(getattr(agent, "base_url", "") or "").strip()
    from utils import base_url_hostname
    host = base_url_hostname(base_url) if base_url else ""
    pool = getattr(agent, "_credential_pool", None)
    pool_matches = pool is not None and (getattr(pool, "provider", "") or "").strip().lower() == provider
    if provider == "anthropic":
        if base_url and host != "api.anthropic.com":
            return False
        from agent.anthropic_credentials import _is_oauth_token
        api_key = getattr(agent, "api_key", "")
        if pool_matches:
            entry_id = getattr(agent, "_credential_pool_entry_id", None)
            try:
                entries = list(pool.entries())
                # A bound ID is authoritative; otherwise the live token must
                # identify exactly one row before that row's fields are used.
                matches = [e for e in entries if entry_id and e.id == entry_id]
                if not matches and api_key:
                    matches = [e for e in entries if getattr(e, "runtime_api_key", None) == api_key]
            except Exception as exc:
                # Unknown pool state: keep ordinary backoff, never rollover.
                logger.warning("subscription rollover: credential pool inspection failed: %s", exc)
                return False
            if len(matches) > 1:
                return False
            if len(matches) == 1:
                active = matches[0]
                source = str(getattr(active, "source", "") or "")
                auth_type = str(getattr(active, "auth_type", "") or "").lower()
                if source == "env:ANTHROPIC_API_KEY" or auth_type == "api_key":
                    return False
                return auth_type == "oauth" or _is_oauth_token(str(getattr(active, "runtime_api_key", "") or ""))
        # A readable pool with no row for this token (a mid-turn rotation not
        # yet resynced) or no pool: only the live token's own OAuth format
        # can make the route eligible. An API key never qualifies.
        return isinstance(api_key, str) and _is_oauth_token(api_key)
    if provider == "xai-oauth":
        if not base_url:
            return True
        from hermes_cli.auth_xai import _xai_url_problem
        return _xai_url_problem(base_url)[0] is None
    if not base_url or host == "chatgpt.com":
        return True
    if pool_matches:
        from agent.credential_pool import credential_pool_entry_serves_endpoint
        model = getattr(agent, "model", None)
        try:
            return any(
                credential_pool_entry_serves_endpoint(entry, base_url, provider=provider, model=model)
                for entry in pool.entries()
            )
        except Exception:
            return False
    return False


def switch_deferred_by_reset('''),
    ),
    "agent/agent_runtime_helpers.py": (
        ('''    error_context: Optional[dict[str, Any]] = None, billing_unverified: bool = False,
) -> tuple[bool, bool]:''', '''    error_context: Optional[dict[str, Any]] = None, billing_unverified: bool = False,
    long_limit_attempts: Optional[set[tuple[str, str]]] = None,
) -> tuple[bool, bool]:'''),
        ('''    def _rotate_and_swap(default_status: int, label: str) -> bool:''',
         '''    def _rotate_and_swap(default_status: int, label: str, *, try_siblings: bool = False) -> bool:'''),
        ('''        next_entry = pool.mark_exhausted_and_rotate(**kwargs)
        if next_entry is None:
            return False
        if not credential_pool_entry_serves_endpoint(next_entry, getattr(agent, "base_url", None)):''', '''        next_entry = pool.mark_exhausted_and_rotate(**kwargs)
        base_url = getattr(agent, "base_url", None)
        if try_siblings:
            first_id = getattr(next_entry, "id", None)

            def allowed(entry):
                return (entry is not None and entry.runtime_api_key != api_key_hint
                        and entry.id != credential_id
                        and (long_limit_attempts is None or (current_provider, entry.id) not in long_limit_attempts)
                        and credential_pool_entry_serves_endpoint(entry, base_url, provider=current_provider, model=model))

            def swap(entry):
                return allowed(entry) and _swap_credential_on_bound_route(agent, entry)

            if not swap(next_entry):
                # None may mean that the pool refused its first pick, not that
                # it has no eligible rows. Scan only when we can exclude the
                # request's failed identity, and never retry prior long-429 rows.
                if not (credential_id or api_key_hint):
                    return False
                for sibling in pool.entries():
                    if sibling.id == first_id or not allowed(sibling):
                        continue
                    eligible = pool.reclaim(sibling.id, model=model)
                    if swap(eligible):
                        next_entry = eligible
                        break
                else:
                    return False
        elif next_entry is None:
            return False
        elif not credential_pool_entry_serves_endpoint(next_entry, base_url):'''),
        ('''        swapped = agent._swap_credential(next_entry) is not False
        benched = next''', '''        swapped = try_siblings or agent._swap_credential(next_entry) is not False
        benched = next'''),
        ('''    if effective_reason == FailoverReason.rate_limit:
        return _recover_rate_limit(''', '''    if effective_reason == FailoverReason.rate_limit:
        from agent.fallback_cooldown import is_long_subscription_rate_limit
        if is_long_subscription_rate_limit(agent, effective_reason, status_code, (error_context or {}).get("reset_at")):
            if credential_id and long_limit_attempts is not None:
                long_limit_attempts.add((current_provider, credential_id))
            swapped = _rotate_and_swap(429, "long rate limit", try_siblings=True)
            return swapped, not swapped
        return _recover_rate_limit('''),
        ('''def restore_primary_runtime(agent) -> bool:''', '''def restore_primary_runtime(agent) -> bool:
    from hermes_cli.auth import selected_primary_auth
    with selected_primary_auth(agent):
        restored = _restore_primary_runtime_under_policy(agent)
    if restored:
        agent._subscription_rollover_route = None
    return restored


def _restore_primary_runtime_under_policy(agent) -> bool:'''),
        ('''    if pool is not None and pool_provider and not matches_primary(pool):
        agent._credential_pool = None''', '''    pooled_rollover = getattr(agent, "_subscription_rollover_route", None) is not None
    if (pool is None and pooled_rollover) or (pool is not None and pool_provider and not matches_primary(pool)):
        agent._credential_pool = None'''),
        ('''        agent._swap_credential(entry)
        logger.info(
            "Restore re-selected pool entry''', '''        if pooled_rollover:
            if not _swap_credential_on_bound_route(agent, entry):
                return
        else:
            agent._swap_credential(entry)
        logger.info(
            "Restore re-selected pool entry'''),
        ('''def _revert_credential_rotation(agent) -> None:''', '''def _swap_credential_on_bound_route(agent, entry) -> bool:
    base_url = getattr(agent, "base_url", None)
    provider = getattr(agent, "provider", None)
    model = getattr(agent, "model", None)
    if not credential_pool_entry_serves_endpoint(entry, base_url, provider=provider, model=model):
        return False
    if not credential_pool_entry_serves_endpoint(entry, base_url):
        # A raw URL can differ from the effective route (e.g. a Codex relay).
        return agent._swap_credential(entry, preserve_route=True) is not False
    return agent._swap_credential(entry) is not False


def _revert_credential_rotation(agent) -> None:'''),
        ('''    if agent._swap_credential(entry) is not False:
        logger.info(
            "Credential %s (%s) available again''', '''    if _swap_credential_on_bound_route(agent, entry):
        logger.info(
            "Credential %s (%s) available again'''),
    ),
    "agent/credential_pool.py": (
        ('''def credential_pool_entry_serves_endpoint(entry: Any, base_url: Any) -> bool:''',
         '''def credential_pool_entry_serves_endpoint(entry: Any, base_url: Any, *,
                                          provider: Optional[str] = None, model: Optional[str] = None) -> bool:'''),
        ('''    entry_url = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None)
    if not isinstance(entry_url, str) or not entry_url:''', '''    entry_url = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None)
    if provider in {"anthropic", "openai-codex", "xai-oauth"}:
        # Use the native binding policy, including the profile's proxy override.
        from hermes_cli.runtime_provider import _resolve_runtime_from_pool_entry
        entry_url = _resolve_runtime_from_pool_entry(provider=provider, entry=entry,
            requested_provider=provider, target_model=model)["base_url"]
    if not isinstance(entry_url, str) or not entry_url:'''),
        ('''    def has_credentials(self) -> bool:''', '''    def restrict_route_selection(self, admit: Callable[[PooledCredential], bool]) -> None:
        """Narrow this runtime's pool view without changing its persistence baseline."""
        with self._lock:
            self._route_admission = admit

    def admits_route_entry(self, entry: PooledCredential) -> bool:
        admit = getattr(self, "_route_admission", None)
        return admit is None or admit(entry)

    def has_credentials(self) -> bool:'''),
        ('''        self, *, clear_expired: bool = False, refresh: bool = False, model: Optional[str] = None,
    ) -> tuple[list[PooledCredential], list[PooledCredential]]:''', '''        self, *, clear_expired: bool = False, refresh: bool = False, model: Optional[str] = None,
        credential_id: Optional[str] = None,
    ) -> tuple[list[PooledCredential], list[PooledCredential]]:'''),
        ('''        for entry in self._entries:
            # Borrowed credentials persist as metadata-only references''', '''        for entry in self._entries:
            if (credential_id is not None and entry.id != credential_id) or not self.admits_route_entry(entry):
                continue
            # Borrowed credentials persist as metadata-only references'''),
        ('''            available.append(entry)
        if entries_to_prune:''', '''            if self.admits_route_entry(entry):
                available.append(entry)
        if entries_to_prune:'''),
        ('''        if cleared_any:
            self._persist(removed_ids=entries_to_prune)
        return available, pending_refresh''', '''        if cleared_any:
            self._persist(removed_ids=entries_to_prune)
        # A refresh or cooldown flush can replace an earlier row and advance
        # the persistence baseline. Never hand the caller the old generation.
        by_id = {entry.id: entry for entry in self._entries}
        live_available, live_pending = [], []
        for observed in (*available, *pending_refresh):
            live = by_id.get(observed.id)
            if live is None or not self._row_available_after_merge(live, model):
                continue
            if refresh and self._entry_needs_refresh(live):
                if self.provider in _TOKENS_SINGLETON_PROVIDERS:
                    live_pending.append(live)
            else:
                live_available.append(live)
        return live_available, live_pending

    def _row_available_after_merge(self, entry: PooledCredential, model: Optional[str]) -> bool:
        if entry.last_status == STATUS_DEAD or not entry.runtime_api_key or not self.admits_route_entry(entry):
            return False
        if entry.last_status == STATUS_EXHAUSTED:
            until = _exhausted_until(entry, sole_credential=self._is_sole_credential())
            if until is not None and until > time.time():
                return False
        return model_cooldown_until(entry, model) is None'''),
        ('''        if pending_refresh:
            self._refresh_pending_entries(pending_refresh)
            # Re-select now that the refreshed entries are back in the pool.
            if entry is None:
                entry, _ = self._select_under_lock(model=model)
        if entry is not None:
            self._unmatched_rotation_streak = 0
        return entry''', '''        if pending_refresh:
            self._refresh_pending_entries(pending_refresh)
        with self._lock:
            if entry is not None:
                entry = self._find(lambda live: live.id == entry.id)
                if entry is not None and (not self._row_available_after_merge(entry, model)
                                          or self._entry_needs_refresh(entry)):
                    entry = None
            if entry is None and pending_refresh:
                entry, _ = self._select_unlocked(model=model)
            if entry is not None:
                self._unmatched_rotation_streak = 0
            return entry'''),
        ('''        if self._strategy == STRATEGY_RANDOM:
            entry = random.choice(available)
        elif self._strategy == STRATEGY_LEAST_USED and len(available) > 1:
            entry = min(available, key=lambda e: e.request_count)
        else:
            entry = available[0]
        # Count the selection under every strategy. The counter is ``least_used``'s
        # baseline and reaches auth.json on the next persist (exhaustion, rotation,
        # refresh); it used to move only while ``least_used`` was active.
        if count:
            entry = self._adopt(entry, persist=False, request_count=entry.request_count + 1)
        if self._strategy == STRATEGY_ROUND_ROBIN and len(available) > 1:
            rotated = [candidate for candidate in self._entries if candidate.id != entry.id]
            rotated.append(replace(entry, priority=len(self._entries) - 1))
            self._entries = [replace(candidate, priority=idx) for idx, candidate in enumerate(rotated)]
            self._persist()
            entry = self._find(lambda candidate: candidate.id == entry.id) or entry
        self._current_id = entry.id
        return entry, pending_refresh''', '''        candidate_ids = {entry.id for entry in available}
        pending_ids = {entry.id for entry in pending_refresh}
        selected = None
        while candidate_ids:
            # Work from current rows after each merge, not the previous scan.
            available = []
            for live in self._entries:
                if live.id not in candidate_ids or not self._row_available_after_merge(live, model):
                    continue
                if refresh and self._entry_needs_refresh(live):
                    if self.provider in _TOKENS_SINGLETON_PROVIDERS:
                        pending_ids.add(live.id)
                    continue
                available.append(live)
            if not available:
                break
            if self._strategy == STRATEGY_RANDOM:
                entry = random.choice(available)
            elif self._strategy == STRATEGY_LEAST_USED and len(available) > 1:
                entry = min(available, key=lambda e: e.request_count)
            else:
                entry = available[0]
            chosen_id = entry.id
            candidate_ids.remove(chosen_id)
            if self._strategy == STRATEGY_ROUND_ROBIN and len(available) > 1:
                rotated = [candidate for candidate in self._entries if candidate.id != chosen_id]
                rotated.append(entry)
                self._entries = [replace(candidate, priority=idx) for idx, candidate in enumerate(rotated)]
                self._persist()
                entry = self._find(lambda live: live.id == chosen_id)
                if entry is None or not self._row_available_after_merge(entry, model):
                    continue
                if refresh and self._entry_needs_refresh(entry):
                    if self.provider in _TOKENS_SINGLETON_PROVIDERS:
                        pending_ids.add(chosen_id)
                    continue
            if count:
                entry = self._adopt(entry, persist=False, request_count=entry.request_count + 1)
            selected = entry
            break
        self._current_id = selected.id if selected is not None else None
        pending_refresh = [live for live in self._entries if live.id in pending_ids
                           and self._row_available_after_merge(live, model) and self._entry_needs_refresh(live)]
        return selected, pending_refresh'''),
        ('''        with self._lock:
            available, pending = self._available_entries(clear_expired=True, refresh=True, model=model)
        if any(e.id == credential_id for e in pending):
            self._refresh_pending_entries([e for e in pending if e.id == credential_id])
            with self._lock:
                available, _pending = self._available_entries(clear_expired=True, refresh=True, model=model)
        return next((e for e in available if e.id == credential_id), None)''', '''        if not credential_id:
            return None
        options = {"clear_expired": True, "refresh": True, "model": model, "credential_id": credential_id}
        with self._lock:
            available, pending = self._available_entries(**options)
        if pending:
            self._refresh_pending_entries(pending)
            with self._lock:
                available, _pending = self._available_entries(**options)
        return available[0] if available else None'''),
        ('''def load_pool(provider: str) -> CredentialPool:
    provider = (provider or "").strip().lower()
    if provider in SINGLE_USE_REFRESH_POOL_PROVIDERS:''', '''def load_pool(provider: str) -> CredentialPool:
    provider = (provider or "").strip().lower()
    if auth_mod.profile_local_auth_active():
        # A temporary selection boundary must not seed, normalize, or prune the
        # persisted pool. Leave external rows out of both the pool and its deletion
        # baseline; ordinary auth-store merges preserve rows not explicitly removed.
        rows = [row for row in read_credential_pool(provider) if isinstance(row, dict)
                and not is_borrowed_credential_source(row.get("source"), provider)]
        pool = CredentialPool(provider, [PooledCredential.from_dict(provider, row) for row in rows])
        pool._persisted_token_pairs = auth_mod._token_pairs_by_id(rows)
        return pool
    if provider in SINGLE_USE_REFRESH_POOL_PROVIDERS:'''),
        ('''    def next_available_at(self, *, model: Optional[str] = None) -> Optional[float]:''',
         '''    def next_available_at(self, *, model: Optional[str] = None, base_url=None,
                          credential_id: Optional[str] = None) -> Optional[float]:'''),
        ('''            if available:
                return None
            # Mirror _available_entries: a sole credential's transient throttle''', '''            def serves(entry):
                return (not credential_id or entry.id == credential_id) and credential_pool_entry_serves_endpoint(
                    entry, base_url, provider=self.provider, model=model)

            if any(serves(entry) for entry in available):
                return None
            # Mirror _available_entries: a sole credential's transient throttle'''),
        ('''            candidates = [
                until
                for until in (
                    _exhausted_until(entry, sole_credential=sole_credential)
                    for entry in self._entries
                    if entry.last_status == STATUS_EXHAUSTED
                )
                if until is not None
            ]
            candidates.extend(
                until
                for entry in self._entries
                if entry.last_status != STATUS_DEAD
                for until in (model_cooldown_until(entry, model),)
                if until is not None
            )
            return min(candidates) if candidates else None''', '''            now = time.time()
            candidates = []
            for entry in self._entries:
                if entry.last_status == STATUS_DEAD or not entry.runtime_api_key or not serves(entry):
                    continue
                account_until = _exhausted_until(entry, sole_credential=sole_credential) or 0
                model_until = model_cooldown_until(entry, model) or 0
                until = max(account_until, model_until)
                if until > now:
                    candidates.append(until)
            return min(candidates, default=None)'''),
    ),
    "agent/turn_recovery.py": (
        ('''    recovered_with_pool, _retry.has_retried_429 = agent._recover_with_credential_pool(''', '''    from agent.fallback_cooldown import is_long_subscription_rate_limit
    rollover = is_long_subscription_rate_limit(agent, classified.reason, status_code, (error_context or {}).get("reset_at"))
    rollover_kwargs = {"long_limit_attempts": _retry.long_limit_attempts} if rollover else {}
    recovered_with_pool, _retry.has_retried_429 = agent._recover_with_credential_pool('''),
        ('''        billing_unverified=classified.billing_unverified,
    )
    if recovered_with_pool:''', '''        billing_unverified=classified.billing_unverified, **rollover_kwargs,
    )
    if recovered_with_pool:'''),
        ('''    _should_fallback = (
        (is_rate_limited and _wrapped_output_cap_budget is None)''', '''    from agent.fallback_cooldown import is_long_subscription_rate_limit
    reset_at = error_context.get("reset_at") if isinstance(error_context, dict) else None
    long_limit = is_long_subscription_rate_limit(agent, classified.reason, status_code, reset_at)
    _should_fallback = (
        (is_rate_limited and _wrapped_output_cap_budget is None)'''),
        ('''    ) and not (getattr(classified, "error_context", None) or {}).get("usage_window")  # never switch models on a plan limit''', '''    ) and (long_limit or not (getattr(classified, "error_context", None) or {}).get("usage_window"))'''),
        ('''    if _should_fallback and agent._fallback_index < len(agent._fallback_chain):''', '''    pending = agent._fallback_index < len(agent._fallback_chain)
    deferred = long_limit and getattr(agent, "_deferred_subscription_fallbacks", None)
    if _should_fallback and (pending or deferred):'''),
        ('''            False if _is_upstream else _ra()._pool_may_recover_from_rate_limit(agent._credential_pool)''', '''            False if _is_upstream or long_limit else _ra()._pool_may_recover_from_rate_limit(agent._credential_pool)'''),
        ('''        if not pool_may_recover:
            agent._buffer_diagnostic_status(_eager_fallback_status(classified, _is_upstream, _is_transport_failure))
            reset_at = error_context.get("reset_at") if isinstance(error_context, dict) else None
            if agent._try_activate_fallback(reason=classified.reason, reset_at=reset_at):''', '''        if not pool_may_recover:
            if not long_limit:
                agent._buffer_diagnostic_status(_eager_fallback_status(classified, _is_upstream, _is_transport_failure))
            else:
                # The failed row might not be the first subscription to reset.
                pool = agent._credential_pool
                if pool is not None and pool.provider == agent.provider:
                    selected = getattr(pool, "selected_credential", "auto")
                    pool_reset = pool.next_available_at(model=agent.model, base_url=agent.base_url,
                                                        credential_id=None if selected == "auto" else selected)
                    if pool_reset is not None:
                        reset_at = pool_reset
            if agent._try_activate_fallback(reason=classified.reason, reset_at=reset_at,
                                            **({"subscription_rollover": True} if long_limit else {})):'''),
    ),
    "agent/turn_api_error.py": (
        ('''    if retry_count >= max_retries:
        # Before fallback''', '''    from agent.fallback_cooldown import is_long_subscription_rate_limit
    long_limit = not is_context_length_error and is_long_subscription_rate_limit(
        agent, classified.reason, status_code, error_context.get("reset_at") if isinstance(error_context, dict) else None,
    )
    if long_limit:
        # Rotation and the eligible chain were already tried. Never park a live
        # turn behind a known subscription reset, even if no route is allowed.
        _delivered = settle_delivered_partial(agent, messages, current_turn_user_idx)
        return _verdict("return", max_retries_exhausted_result(
            agent, api_error, classified, attempts=attempts_made, is_rate_limited=True,
            error_msg=error_msg, api_kwargs=api_kwargs, api_messages=api_messages,
            messages=messages, conversation_history=conversation_history,
            api_call_count=api_call_count, approx_tokens=approx_tokens, provider=_provider,
            base_url=_base, model=_model, delivered=_delivered,
        ))
    if retry_count >= max_retries:
        # Before fallback'''),
    ),
    "agent/turn_retry_state.py": (
        ('''from dataclasses import dataclass''', '''from dataclasses import dataclass, field'''),
        ('''    has_retried_429: bool = False''', '''    has_retried_429: bool = False
    long_limit_attempts: set[tuple[str, str]] = field(default_factory=set)'''),
    ),
    "agent/chat_completion_helpers.py": (
        ('''def try_activate_fallback(agent, reason: FailoverReason | None = None, reset_at=None) -> bool:''',
         '''def try_activate_fallback(agent, reason: FailoverReason | None = None, reset_at=None,
                          *, subscription_rollover: bool = False) -> bool:'''),
        ('''    if switch_deferred_by_reset(agent, reason, reset_at):''', '''    long_limit = subscription_rollover
    remaining = agent._fallback_chain[agent._fallback_index:]
    if agent._fallback_index == 0:
        agent._deferred_subscription_fallbacks = []
    deferred = getattr(agent, "_deferred_subscription_fallbacks", None) or []
    agent._deferred_subscription_fallbacks = deferred
    if remaining and not long_limit and all(entry.get("subscription_exhaustion_only") for entry in remaining):
        return False
    if not long_limit and switch_deferred_by_reset(agent, reason, reset_at):'''),
        ('''        if agent._fallback_index >= len(agent._fallback_chain):
            return _fallback_chain_exhausted(agent, reason)
        fb = agent._fallback_chain[agent._fallback_index]
        agent._fallback_index += 1
        fb_key''', '''        if long_limit and deferred:
            index = deferred.pop(0)
        elif agent._fallback_index < len(agent._fallback_chain):
            index = agent._fallback_index
            agent._fallback_index += 1
        else:
            return _fallback_chain_exhausted(agent, reason)
        fb = agent._fallback_chain[index]
        if fb.get("subscription_exhaustion_only") and not long_limit:
            deferred.append(index)
            continue
        fb_key'''),
        ('''        if _should_skip_fallback_candidate(agent, fb, fb_key, fb_provider, fb_model, unavailable):
            continue

        try:
            from agent.route_binding import bind_route_entry
            bound = bind_route_entry(agent, fb, fb_provider, fb_model)''', '''        try:
            from contextlib import nullcontext
            from hermes_cli.auth import profile_local_auth
            from agent.route_binding import bind_route_entry

            with profile_local_auth() if long_limit else nullcontext():
                if long_limit:
                    # Match the actual client dispatch, not the configured alias.
                    from agent.auxiliary_client import _normalize_aux_provider
                    from hermes_cli.model_normalize import normalize_model_for_provider
                    from hermes_cli.runtime_provider import _raise_if_provider_disabled
                    alias_target = fb_provider.removeprefix("custom:").strip()
                    if not fb_provider or not fb_model or alias_target in {"auto", "main", "moa"}:
                        continue
                    _raise_if_provider_disabled(fb_provider)
                    effective = _normalize_aux_provider(fb_provider)
                    if effective in {"auto", "main", "moa", _normalize_aux_provider(agent.provider)}:
                        continue
                    if effective != fb_provider:
                        _raise_if_provider_disabled(effective)
                    fb_provider = effective
                    fb_model = normalize_model_for_provider(fb_model, fb_provider)

                if _should_skip_fallback_candidate(agent, fb, fb_key, fb_provider, fb_model,
                                                   set() if long_limit else unavailable):
                    continue
                bound = (bind_route_entry(agent, fb, fb_provider, fb_model, subscription_rollover=True)
                         if long_limit else bind_route_entry(agent, fb, fb_provider, fb_model))'''),
        ('''            if bound is None:
                logger.warning("Fallback to %s failed: provider not configured", fb_provider)
                unavailable.add(fb_key)''', '''            if bound is None:
                logger.warning("Fallback to %s failed: no usable route", fb_provider)
                if not long_limit:
                    unavailable.add(fb_key)'''),
        ('''            _buffer_fallback_notice(agent, notice)
            # ``_fallback_activated``''', '''            if long_limit:
                agent._subscription_rollover_route = (agent.provider, agent.model)
                from agent.credential_pool import _parse_absolute_timestamp
                try:
                    reset = _parse_absolute_timestamp(reset_at)
                    until = " until " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(reset)) if reset else ""
                except (OverflowError, OSError, ValueError):
                    until = ""
                source = {"anthropic": "Claude", "openai-codex": "Codex", "xai-oauth": "Grok"}.get(old_provider, old_provider)
                target = "Sol 6.2" if (fb_provider, fb_model) == ("openai-codex", "gpt-6.2-sol") else fb_model
                agent._emit_status(f"Subscription rollover: {source} is rate limited{until}; continuing on {target}.")
            else:
                agent._subscription_rollover_route = None
                _buffer_fallback_notice(agent, notice)
            # ``_fallback_activated``'''),
    ),
    "agent/agent_init.py": (
        ('''    for _fb in _fallback_entries(fallback_model):
''', '''    for _fb in _fallback_entries(fallback_model):
        if _fb.get("subscription_exhaustion_only"):
            continue
'''),
    ),
    "agent/route_binding.py": (
        ('''def bind_route_entry(agent: Any, entry: dict[str, Any], provider: str, model: str) -> Optional[tuple[str, str]]:''',
         '''def bind_route_entry(agent: Any, entry: dict[str, Any], provider: str, model: str,
                     *, subscription_rollover: bool = False) -> Optional[tuple[str, str]]:'''),
        ('''    api_key_hint = resolve_entry_api_key(entry)
    api_mode_explicit''', '''    api_key_hint = resolve_entry_api_key(entry)
    if subscription_rollover and (entry.get("key_env") or entry.get("api_key_env")) and not api_key_hint:
        return None  # A required credential reference must not fall through to another account.
    api_mode_explicit'''),
        ('''    if base_url_hint and base_url_host_matches(base_url_hint, "ollama.com") and not api_key_hint:''',
         '''    if not subscription_rollover and base_url_hint and base_url_host_matches(base_url_hint, "ollama.com") and not api_key_hint:'''),
        ('''    client, _resolved_model = resolve_provider_client(
        provider, model=model, raw_codex=True, explicit_base_url=base_url_hint, explicit_api_key=api_key_hint,
        api_mode=api_mode)''', '''    selected_pool = None
    if subscription_rollover and provider in {"anthropic", "openai-codex", "xai-oauth"}:
        from agent.credential_pool import load_pool
        pool = load_pool(provider)
        if pool.has_credentials():
            from hermes_cli.route_identity import normalize_route_base_url
            from hermes_cli.runtime_provider import _resolve_runtime_from_pool_entry
            def resolve_row(row):
                return _resolve_runtime_from_pool_entry(provider=provider, entry=row,
                    requested_provider=provider, pool=pool, target_model=model)

            def matches_route(runtime):
                key, url = runtime["api_key"], runtime["base_url"]
                if not (key and url):
                    return False
                if provider == "xai-oauth":
                    from hermes_cli.auth_xai import _xai_url_problem
                    if _xai_url_problem(url)[0] is not None:
                        return False
                return bool((not api_key_hint or api_key_hint == key)
                    and (not base_url_hint or normalize_route_base_url(base_url_hint) == normalize_route_base_url(url)))

            expected_mode = "anthropic_messages" if provider == "anthropic" else "codex_responses"
            if api_mode_explicit and api_mode != expected_mode:
                return None
            if provider == "xai-oauth" or api_key_hint or base_url_hint:
                pool.restrict_route_selection(lambda row: matches_route(resolve_row(row)))
            selected = pool.select(model=model)
            if selected is None:
                return None  # A bench or route mismatch cannot fall through to a singleton.
            runtime = resolve_row(selected)
            if not matches_route(runtime) or runtime["api_mode"] != expected_mode:
                return None  # Revalidate the actual row after refresh or persistence merge.
            key, url = runtime["api_key"], runtime["base_url"]
            api_mode = runtime["api_mode"]
            if api_mode == "anthropic_messages":
                from types import SimpleNamespace
                # The lifecycle swap builds the native Anthropic client exactly once.
                client = SimpleNamespace(api_key=key, base_url=url)
            elif provider == "xai-oauth":
                from agent.auxiliary_client import _create_openai_client
                from tools.xai_http import hermes_xai_default_headers
                client = _create_openai_client(api_key=key, base_url=url,
                                               default_headers=hermes_xai_default_headers())
            else:
                from agent.auxiliary_client import _codex_cloudflare_headers, _create_openai_client
                client = _create_openai_client(api_key=key, base_url=url,
                                               default_headers=_codex_cloudflare_headers(key, base_url=url))
            selected_pool = pool
    if selected_pool is None:
        # The configured name carries routing semantics (e.g. a local-server
        # alias prohibits borrowing an OpenAI API key). Policy checked the
        # canonical identity, but the wire resolver must see the original.
        requested = str(entry.get("provider") or provider) if subscription_rollover else provider
        if subscription_rollover and provider == "xai-oauth":
            if api_mode_explicit and api_mode != "codex_responses":
                return None
            from agent.auxiliary_client import _create_openai_client, _resolve_xai_oauth_for_aux
            from tools.xai_http import hermes_xai_default_headers
            resolved = _resolve_xai_oauth_for_aux()
            if resolved is None:
                client = None
            else:
                from hermes_cli.auth_xai import _xai_url_problem
                key, url = resolved
                if _xai_url_problem(url)[0] is not None:
                    return None
                api_mode = "codex_responses"
                client = _create_openai_client(api_key=key, base_url=url,
                                               default_headers=hermes_xai_default_headers())
        else:
            if subscription_rollover and provider == "custom":
                from agent.auxiliary_client import _LOCAL_SERVER_ALIASES
                if not base_url_hint or (not api_key_hint and requested.strip().lower() not in _LOCAL_SERVER_ALIASES):
                    return None  # No ambient auth or autodiscovery for a bare custom route.
            client, _resolved_model = resolve_provider_client(
                requested, model=model, raw_codex=True, explicit_base_url=base_url_hint, explicit_api_key=api_key_hint,
                api_mode=api_mode)
        if subscription_rollover and client is not None and provider in {"anthropic", "openai-codex", "xai-oauth"}:
            from hermes_cli.route_identity import normalize_route_base_url
            if (base_url_hint and normalize_route_base_url(base_url_hint) != normalize_route_base_url(client.base_url)
                    or api_key_hint and api_key_hint != client.api_key):
                return None  # The raw singleton resolver need not honor explicit hints.'''),
        ('''    _rebind_fallback_credential_pool(agent, provider, model)''', '''    if selected_pool is None:
        _rebind_fallback_credential_pool(agent, provider, model)
    else:
        agent._credential_pool, agent._credential_pool_entry_id = selected_pool, selected.id'''),
    ),
    "agent/client_lifecycle.py": (
        ('''    def _swap_credential(self, entry) -> bool:''', '''    def _swap_credential(self, entry, *, preserve_route: bool = False) -> bool:'''),
        ('''        runtime_base = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None) or self.base_url
        from hermes_cli.providers import is_actual_route''', '''        runtime_base = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None) or self.base_url
        rollover = (getattr(self, "_fallback_activated", False)
                    and getattr(self, "_subscription_rollover_route", None) == (self.provider, self.model))
        if rollover:
            admit = getattr(getattr(self, "_credential_pool", None), "admits_route_entry", None)
            if callable(admit) and not admit(entry):
                return False
        if preserve_route or rollover:
            from agent.credential_pool import credential_pool_entry_serves_endpoint
            if not credential_pool_entry_serves_endpoint(entry, self.base_url, provider=self.provider, model=self.model):
                return False
            runtime_base = self.base_url
        from hermes_cli.providers import is_actual_route'''),
        ('''        try:
            from agent.anthropic_credentials import resolve_anthropic_token
            new_token = resolve_anthropic_token(model=self.model)''', '''        try:
            if (getattr(self, "_fallback_activated", False)
                    and getattr(self, "_subscription_rollover_route", None) == (self.provider, self.model)):
                # Refresh the row that supplied this exact runtime, not the
                # environment or another subscription on the same provider.
                pool = getattr(self, "_credential_pool", None)
                entry_id = getattr(self, "_credential_pool_entry_id", None)
                if pool is None or pool.provider != "anthropic" or not entry_id:
                    return False
                entry = pool.reclaim(entry_id, model=self.model)
                if entry is None:
                    return False
                from hermes_cli.route_identity import normalize_route_base_url
                from hermes_cli.runtime_provider import _resolve_runtime_from_pool_entry
                runtime = _resolve_runtime_from_pool_entry(provider=self.provider, entry=entry,
                    requested_provider=self.provider, pool=pool, target_model=self.model)
                if normalize_route_base_url(runtime["base_url"]) != normalize_route_base_url(anthropic_base_url):
                    return False
                new_token = runtime["api_key"]
            else:
                from agent.anthropic_credentials import resolve_anthropic_token
                new_token = resolve_anthropic_token(model=self.model)'''),
        ('''        # No silent account swap: a non-singleton credential (manual pool entry, explicit api_key=) must not be''',
         '''        if (getattr(self, "_fallback_activated", False)
                and getattr(self, "_subscription_rollover_route", None) == (self.provider, self.model)
                and getattr(self, "_credential_pool_entry_id", None)):
            return False  # Reactive recovery already handles the actual pool row.
        # No silent account swap: a non-singleton credential (manual pool entry, explicit api_key=) must not be'''),
    ),
    "hermes_cli/cli_agent_setup_mixin.py": (
        ('''        for _fb in _fb_chain:
            _fb_provider''', '''        for _fb in _fb_chain:
            if _fb.get("subscription_exhaustion_only"):
                continue
            _fb_provider'''),
    ),
    "hermes_cli/runtime_provider.py": (
        ('''        for entry in get_fallback_chain(config):
''', '''        for entry in get_fallback_chain(config):
            if entry.get("subscription_exhaustion_only"):
                continue
'''),
    ),
    "hermes_cli/auth.py": (
        ('''def _global_auth_file_path() -> Optional[Path]:''', '''from contextvars import ContextVar as _AuthContextVar

_profile_local_auth = _AuthContextVar("profile_local_auth", default=())


def profile_local_auth_active() -> bool:
    return any(required() if callable(required) else required for _, required in _profile_local_auth.get())


@contextmanager
def profile_local_auth(required=True, *, owner=None):
    """Resolve only this profile's auth store for a subscription rollover."""
    scope = _profile_local_auth.set((*_profile_local_auth.get(), (owner, required)))
    try:
        yield
    finally:
        _profile_local_auth.reset(scope)


@contextmanager
def selected_primary_auth(owner):
    """Suspend only the restoring agent's rollover guard, not any enclosing auth policy."""
    scope = _profile_local_auth.set(tuple(item for item in _profile_local_auth.get() if item[0] is not owner))
    try:
        yield
    finally:
        _profile_local_auth.reset(scope)


def _global_auth_file_path() -> Optional[Path]:'''),
        ('''    Read-only fallback path, so no pytest seat belt here (it lives on ``_auth_file_path()``)."""
    try:''', '''    Read-only fallback path, so no pytest seat belt here (it lives on ``_auth_file_path()``)."""
    if profile_local_auth_active():
        return None
    try:'''),
    ),
    "agent/credential_sources.py": (
        ('''    global _notice_logged
    try:
        from hermes_cli.config import load_config_readonly''', '''    from hermes_cli.auth import profile_local_auth_active

    global _notice_logged
    if profile_local_auth_active():
        return False
    try:
        from hermes_cli.config import load_config_readonly'''),
    ),
    "agent/turn_facade.py": (
        ('''            with bind_subagent_parent(self), scoped_runtime_main({}), track_in_interrupt_scope(self):''', '''            from hermes_cli.auth import profile_local_auth

            def rollover_active():
                return (getattr(self, "_fallback_activated", False)
                        and getattr(self, "_subscription_rollover_route", None)
                        == (getattr(self, "provider", None), getattr(self, "model", None)))

            with (bind_subagent_parent(self), scoped_runtime_main({}), track_in_interrupt_scope(self),
                  profile_local_auth(rollover_active, owner=self)):
'''.rstrip()),
    ),
    "agent/auxiliary_client.py": (
        ('''        chain = get_fallback_chain(load_config_readonly())''', '''        chain = [entry for entry in get_fallback_chain(load_config_readonly())
                 if not entry.get("subscription_exhaustion_only")]'''),
    ),
    "acp_adapter/session.py": (
        ('''    def _persist(self, state: SessionState) -> None:''', '''    @staticmethod
    def selected_route(state: SessionState, *, include_auth: bool = False) -> dict:
        """ACP restores/advertises the chosen route, not the engine that answered a turn."""
        agent = state.agent
        primary = getattr(agent, "_primary_runtime", None) if getattr(agent, "_provider_fallback_active", False) else None
        keys = ("provider", "base_url", "api_mode", "api_key") if include_auth else ("provider", "base_url", "api_mode")
        return {key: primary.get(key) if isinstance(primary, dict) else getattr(agent, key, None) for key in keys}

    def _persist(self, state: SessionState) -> None:'''),
        ('''        for key in ("provider", "base_url", "api_mode"):
            value = getattr(state.agent, key, None)''', '''        for key, value in self.selected_route(state).items():'''),
        ('''        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider''', '''        from hermes_cli.config import load_config
        from agent.agent_init_fallback import _init_fallback_chain
        from hermes_cli.fallback_config import get_fallback_chain
        from hermes_cli.runtime_provider import resolve_runtime_provider'''),
        ('''        # ACP stdio: stdout is protocol-only JSON-RPC; agent chatter goes to stderr.''', '''        # Attach only after the selected route initializes: the constructor's
        # legacy startup fallback would overwrite the identity being restored.
        if requested_provider and resolve_error is not None:
            raise resolve_error  # Do not keep a default-provider agent for a failed explicit route.
        _init_fallback_chain(agent, get_fallback_chain(config) or None)
        # ACP stdio: stdout is protocol-only JSON-RPC; agent chatter goes to stderr.'''),
    ),
    "acp_adapter/server.py": (
        ('''        current_provider, current_model = getattr(state.agent, "provider", None), str(state.model or "")''', '''        selected = SessionManager.selected_route(state, include_auth=True)
        current_provider, current_model = selected["provider"], str(state.model or "")'''),
        ('''            current_base_url=str(getattr(state.agent, "base_url", "") or ""),
            current_api_key=str(getattr(state.agent, "api_key", "") or ""),''', '''            current_base_url=str(selected["base_url"] or ""),
            current_api_key=str(selected["api_key"] or ""),'''),
        ('''                "base_url": getattr(state.agent, "base_url", None), "api_mode": getattr(state.agent, "api_mode", None)''', '''                "base_url": selected["base_url"], "api_mode": selected["api_mode"]'''),
        ('''        provider = getattr(state.agent, "provider", None) or detect_provider() or "openrouter"
        try:
            picker = build_model_state(model, provider, str(getattr(state.agent, "base_url", "") or ""))''', '''        selected = SessionManager.selected_route(state)
        provider = selected["provider"] or detect_provider() or "openrouter"
        try:
            picker = build_model_state(model, provider, str(selected["base_url"] or ""))'''),
        ('''        agent.tool_progress_callback = cbs.tool_progress_cb
        # Thought panes''', '''        agent.tool_progress_callback = cbs.tool_progress_cb

        def subscription_status(kind, text):
            if conn is None or kind != "lifecycle" or not text.startswith("Subscription rollover: "):
                return
            update = acp.update_agent_message_text(text + "\\n")
            if state.message_ids is not None:
                state.message_ids.close()
                update.message_id = state.message_ids.current()
                state.message_ids.close()
            _send_update(conn, session_id, loop, update)

        agent.status_callback = subscription_status if conn else None
        # Thought panes'''),
    ),
    "acp_adapter/gateway_server.py": (
        ('''        if await self._gap_guard(kind, sid, aid):
            return''', '''        if kind == "status.update" and payload.get("kind") == "lifecycle":
            text = _text(payload)
            if self._conn and text.startswith("Subscription rollover: "):
                self._end_message(sid)
                await self._conn.session_update(session_id=editor_id, update=self._chunk(sid, text + "\\n"))
                self._end_message(sid)
            return
        if await self._gap_guard(kind, sid, aid):
            return'''),
    ),
}


def patch_subscription_rollover_v1(root: Path) -> bool:
    """Validate every exact anchor and compile all postimages before writing."""
    updates = {}
    for name, edits in _EDITS.items():
        path = root / name
        original = path.read_text(encoding="utf-8")
        source = original
        for old, new in edits:
            if source.count(new) == 1:
                continue
            if new in source or source.count(old) != 1:
                raise RuntimeError(f"{name}: subscription rollover anchor missing or ambiguous: {old.splitlines()[0]}")
            source = source.replace(old, new, 1)
        compile(source, str(path), "exec")
        if source != original:
            updates[path] = source
    for path, source in updates.items():
        path.write_text(source, encoding="utf-8")
    return True
