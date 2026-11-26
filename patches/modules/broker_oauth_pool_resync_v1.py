"""Resync broker-pushed access-token-only OAuth pool entries across mid-turn rotations."""
from pathlib import Path

MARKER = "HERMES_BROKER_OAUTH_POOL_RESYNC_v1"

_OLD_REFRESH = '''    def _refresh_entry(self, entry: PooledCredential, *, force: bool, failed_key: Optional[str] = None):
        if entry.auth_type != AUTH_TYPE_OAUTH or not entry.refresh_token:
            if force:
                self._mark_exhausted(entry, None)
            return None
'''

_NEW_REFRESH = '''    def _refresh_entry(self, entry: PooledCredential, *, force: bool, failed_key: Optional[str] = None):
        # HERMES_BROKER_OAUTH_POOL_RESYNC_v1
        if entry.auth_type == AUTH_TYPE_OAUTH and not entry.refresh_token:
            synced = entry
            if self.provider in ("anthropic", "xai-oauth") or plugin_refresh_hook(self.provider) is not None:
                with _auth_store_lock(timeout_seconds=self._single_use_refresh_lock_timeout()):
                    synced = self._sync_entry_from_pool_store(entry)
            if synced.refresh_token:
                entry = synced
            else:
                has_token = bool((synced.access_token or "").strip())
                rotated = (
                    synced.access_token != entry.access_token
                    or bool(failed_key and has_token and synced.access_token != failed_key)
                )
                if (
                    has_token
                    and rotated
                    and synced.last_status not in (STATUS_DEAD, STATUS_EXHAUSTED)
                    and (force or not self._entry_needs_refresh(synced))
                ):
                    return synced
                if force:
                    self._mark_exhausted(entry, None)
                return None
        if entry.auth_type != AUTH_TYPE_OAUTH or not entry.refresh_token:
            if force:
                self._mark_exhausted(entry, None)
            return None
'''

_OLD_RESYNC = '''        if entry.last_status not in {STATUS_EXHAUSTED, STATUS_DEAD}:
            return entry
        cleared_at = self._reset_cleared_after(entry)
        if cleared_at is not None:
            return self._adopt(entry, persist=False, **_MARK_OK, status_cleared_at=cleared_at)
        if entry.source != _RESYNC_SOURCE.get(self.provider):
            return entry
'''

_NEW_RESYNC = '''        if entry.last_status not in {STATUS_EXHAUSTED, STATUS_DEAD}:
            return entry
        cleared_at = self._reset_cleared_after(entry)
        if cleared_at is not None:
            return self._adopt(entry, persist=False, **_MARK_OK, status_cleared_at=cleared_at)
        if entry.source != _RESYNC_SOURCE.get(self.provider):
            # HERMES_BROKER_OAUTH_POOL_RESYNC_v1
            if (
                entry.auth_type == AUTH_TYPE_OAUTH
                and (
                    entry.last_status == STATUS_DEAD
                    or (entry.last_error_code in (None, 401) and entry.failure_reason in (None, "auth"))
                )
            ):
                return self._sync_entry_from_pool_store(entry)
            return entry
'''

_OLD_AUTH_IMPORT = '''    stale writer still apply to the rotated pair and go through the ordinary recency
    merge in ``_merge_disk_cooldown_state``."""
    from agent.credential_pool import STATUS_DEAD
'''

_NEW_AUTH_IMPORT = '''    stale writer still apply to the rotated pair and go through the ordinary recency
    merge in ``_merge_disk_cooldown_state``."""
    from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED
'''

_OLD_AUTH_MERGE = '''    _take_from_disk(_POOL_TOKEN_GENERATION_FIELDS)
    if not status_cleared and entry.get("last_status") == STATUS_DEAD:
        _take_from_disk((*_POOL_STATUS_FIELDS, "failure_reason"))
    return _merge_disk_cooldown_state(merged, merge_disk, provider_id)
'''

_NEW_AUTH_MERGE = '''    _take_from_disk(_POOL_TOKEN_GENERATION_FIELDS)
    # HERMES_BROKER_OAUTH_POOL_RESYNC_v1
    stale_token_auth_verdict = (
        entry.get("last_status") == STATUS_DEAD
        or (
            entry.get("last_status") == STATUS_EXHAUSTED
            and entry.get("last_error_code") in (None, 401)
            and entry.get("failure_reason") in (None, "auth")
        )
    )
    if not status_cleared and stale_token_auth_verdict:
        _take_from_disk((*_POOL_STATUS_FIELDS, "failure_reason"))
    return _merge_disk_cooldown_state(merged, merge_disk, provider_id)
'''

_FILES = (
    ("agent/credential_pool.py", ((_OLD_REFRESH, _NEW_REFRESH), (_OLD_RESYNC, _NEW_RESYNC))),
    ("hermes_cli/auth.py", ((_OLD_AUTH_IMPORT, _NEW_AUTH_IMPORT), (_OLD_AUTH_MERGE, _NEW_AUTH_MERGE))),
)


def patch_broker_oauth_pool_resync_v1(root: Path) -> bool:
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
                raise RuntimeError(f"{rel}: broker OAuth pool resync anchor missing or ambiguous")
            source = source.replace(old, new, 1)
        compile(source, str(path), "exec")
        planned.append((path, original, source))
    for path, original, source in planned:
        if source != original:
            path.write_text(source, encoding="utf-8")
    return True
