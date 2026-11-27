"""Strict read-only selection of ONE existing native Codex credential.

No CLI adoption, inherited store, pool selection, recovery, lock, or persistence.
The trusted operator supplies the owning native home and exact persisted row ID;
this reader does not grant authority to read a home or to run a model.
"""
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat
import time
import weakref

CODEX_BASE_URL = 'https://chatgpt.com/backend-api/codex'
_ISSUED = weakref.WeakSet()


@dataclass(frozen=True, repr=False, eq=False)
class ReadOnlyCodexCredential:
    owner_home: str
    selector: str
    model: str
    _access_token: str = field(repr=False)
    _expires_at: float = field(repr=False)
    _deadline: float = field(repr=False)

    def __reduce_ex__(self, protocol):
        raise TypeError('read-only Codex credentials are in-memory only')

    def require_live(self):
        if self not in _ISSUED:
            raise RuntimeError('native read-only Codex reader result required')
        if time.time() >= self._expires_at or time.monotonic() >= self._deadline:
            raise RuntimeError('read-only Codex credential expired; refresh is forbidden')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate native auth field')
        result[key] = value
    return result


def read_codex_snapshot_credentials(*, owner_home, selector, model):
    """Read an exact native singleton (selector='singleton') or pool row ID.

    Only the explicitly named store is read. An absent or unusable row is a
    refusal, not permission to look in another store or select another account.
    This intentionally does not use _load_auth_store: its corruption recovery
    writes a .corrupt copy. No token or raw store error enters diagnostics.
    """
    if (not isinstance(selector, str) or not selector or selector != selector.strip()
            or not isinstance(model, str) or not model or model != model.strip()):
        raise ValueError('exact native credential selector and model required')
    home = Path(owner_home)
    if not home.is_absolute() or '..' in home.parts or home.resolve(strict=True) != home:
        raise ValueError('canonical existing native owner home required')
    for directory in (home, *home.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise RuntimeError('native owner home is linked or writable by another user')
    fd = os.open(home / 'auth.json', os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 2_000_000):
            raise RuntimeError('native auth store must be bounded, owner-owned and 0600')
        try:
            with os.fdopen(fd, 'r', encoding='utf-8-sig', closefd=False) as stream:
                raw = stream.read(2_000_001)
        except UnicodeError:
            raise RuntimeError('invalid native auth store encoding; no recovery attempted') from None
        after = os.fstat(fd)
        if (len(raw) > 2_000_000 or (before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise RuntimeError('native auth store changed during read')
        try:
            store = json.loads(raw, object_pairs_hook=_unique)
        except (ValueError, TypeError):
            raise RuntimeError('invalid native auth store; no corruption recovery attempted') from None
    finally:
        os.close(fd)
    if not isinstance(store, dict):
        raise RuntimeError('invalid native auth store shape')
    if selector == 'singleton':
        providers = store.get('providers')
        state = providers.get('openai-codex') if isinstance(providers, dict) else None
        row = state.get('tokens') if isinstance(state, dict) else None
    else:
        pool = store.get('credential_pool')
        entries = pool.get('openai-codex') if isinstance(pool, dict) else None
        matches = [r for r in entries if isinstance(r, dict) and r.get('id') == selector] if isinstance(entries, list) else []
        row = matches[0] if len(matches) == 1 else None
        if isinstance(row, dict) and (
                row.get('auth_type') != 'oauth' or row.get('last_status') not in (None, 'ok')
                or row.get('provider', 'openai-codex') != 'openai-codex'
                or row.get('model_cooldowns') or row.get('last_error_reset_at')
                or row.get('base_url') not in (None, '', CODEX_BASE_URL, CODEX_BASE_URL + '/')):
            raise RuntimeError('selected native Codex row is unavailable or has a different route')
    token = row.get('access_token') if isinstance(row, dict) else None
    if not isinstance(token, str) or not token or token != token.strip():
        raise RuntimeError('selected native Codex access token unavailable')
    # Parse only expiry. This is not signature/issuer verification or availability
    # evidence; the real native subscription endpoint remains the authenticator.
    try:
        import base64
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        expiry = claims['exp']
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time():
            raise ValueError()
    except (ValueError, TypeError, KeyError, IndexError):
        raise RuntimeError('selected native Codex token has no usable expiry; refresh is forbidden') from None
    from agent.credential_pool_timestamps import _parse_absolute_timestamp
    for key in ('expires_at', 'expires_at_ms'):
        if row.get(key) is not None:
            persisted_expiry = _parse_absolute_timestamp(row[key])
            if (persisted_expiry is None or not math.isfinite(persisted_expiry)
                    or persisted_expiry <= time.time()):
                raise RuntimeError('selected native Codex persisted expiry is unavailable; refresh is forbidden')
            expiry = min(expiry, persisted_expiry)
    result = ReadOnlyCodexCredential(str(home), selector, model, token, float(expiry),
                                     time.monotonic() + max(0, expiry - time.time()))
    _ISSUED.add(result)
    return result
