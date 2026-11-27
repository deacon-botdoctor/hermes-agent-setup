"""Broker-only recovery for direct auxiliary calls and stale pool writers."""
import math
import time
from urllib.parse import urlsplit


def _principal(entry):
    from agent.credential_pool import _codex_principal_identity
    return _codex_principal_identity(entry.access_token)


def _broker_entries():
    from agent.credential_pool import load_pool
    entries = load_pool('openai-codex').entries()
    if not entries or len(entries) > 4:
        return []
    principals = set()
    for entry in entries:
        identity = _principal(entry)
        if (entry.extra.get('broker_codex_failover') != 1
                or entry.refresh_token != 'dummy_broker_managed'
                or not identity
                or identity != (entry.extra.get('broker_account_id'), entry.extra.get('broker_subject'))
                or identity in principals):
            return []
        principals.add(identity)
    return entries


def complete(adapter, kwargs):
    from agent import auxiliary_client as auxiliary
    if str(adapter._client.base_url).rstrip('/') != 'https://chatgpt.com/backend-api/codex':
        return adapter._create_once(**kwargs)
    entries = _broker_entries()
    from agent.credential_pool import _codex_principal_identity
    principal = _codex_principal_identity(adapter._client.api_key)
    current = next((e for e in entries if _principal(e) == principal), None)
    if current is None:
        return adapter._create_once(**kwargs)
    timeout = kwargs.get('timeout')
    if not isinstance(timeout, (int, float)):
        timeout = getattr(getattr(adapter._client, 'timeout', None), 'read', None)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        timeout = auxiliary._DEFAULT_AUX_TIMEOUT
    deadline = time.monotonic() + timeout
    # Each call snapshots the shared transport; only its own replacement clients close.
    adapter = auxiliary._CodexCompletionsAdapter(adapter._client, adapter._model)
    owned = []
    try:
        if current.access_token != adapter._client.api_key:
            headers = {key: value for key, value in adapter._client.default_headers.items()
                       if key.lower() != 'authorization'}
            headers.update(auxiliary._codex_cloudflare_headers(
                current.access_token, base_url=str(adapter._client.base_url)))
            replacement = auxiliary._create_openai_client(
                api_key=current.access_token, base_url=str(adapter._client.base_url),
                default_headers=headers)
            owned.append(replacement)
            adapter._client = replacement
        attempted = set()
        request = dict(kwargs)
        for _ in range(len(entries)):
            attempted.add(_principal(current))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Broker subscription recovery deadline exhausted')
            request['timeout'] = remaining
            try:
                return adapter._create_once(**request)
            except Exception as error:
                quota = (auxiliary._is_payment_error(error) or auxiliary._is_rate_limit_error(error))
                if (not quota or auxiliary._is_overloaded_error(error)
                        or time.monotonic() >= deadline):
                    raise
                messages = request.get('messages') or []
                if any(m.get('codex_compaction_items') or any(
                        isinstance(item, dict) and item.get('type') == 'compaction'
                        for item in (m.get('codex_reasoning_items') or []))
                        for m in messages if isinstance(m, dict)):
                    raise
                failed_key = adapter._client.api_key
                if not auxiliary._recover_provider_pool('openai-codex', error, failed_api_key=failed_key):
                    raise
                if len(attempted) >= len(entries):
                    raise
                replacement, model = auxiliary.resolve_provider_client('openai-codex', model=adapter._model)
                if replacement is None:
                    raise
                try:
                    fresh = _broker_entries()
                    current = next((e for e in fresh if e.access_token == replacement.api_key), None)
                    if (model != adapter._model or current is None or _principal(current) in attempted
                            or str(replacement.base_url).rstrip('/') != str(adapter._client.base_url).rstrip('/')):
                        raise error
                except BaseException:
                    replacement.close()
                    raise
                owned.append(replacement)
                adapter._client = replacement._real_client
                # Account-bound reasoning cannot be replayed on another subscription.
                request['messages'] = [
                    {k: ([{name: value for name, value in item.items() if name != 'id'}
                           if isinstance(item, dict) else item for item in v]
                         if k == 'codex_message_items' and isinstance(v, list) else v)
                     for k, v in message.items() if k != 'codex_reasoning_items'}
                    if isinstance(message, dict) else message for message in messages
                ]
    finally:
        for client in owned:
            auxiliary._close_cached_client(client)


def merge_metadata(entry, disk_entry, provider):
    if (provider != 'openai-codex' or not isinstance(disk_entry, dict)
            or disk_entry.get('broker_codex_failover') != 1
            or disk_entry.get('refresh_token') != 'dummy_broker_managed'
            or entry.get('refresh_token') != 'dummy_broker_managed'):
        return entry
    from agent.credential_pool import _codex_principal_identity
    identity = _codex_principal_identity(entry.get('access_token', ''))
    if (not identity or identity != _codex_principal_identity(disk_entry.get('access_token', ''))
            or identity != (disk_entry.get('broker_account_id'), disk_entry.get('broker_subject'))):
        return entry
    return {**entry, **{key: disk_entry[key] for key in (
        'broker_codex_failover', 'broker_lane', 'broker_account_id', 'broker_subject') if key in disk_entry}}


def request_key(error, fallback):
    """Attribute a failed request to its actual transport, including shared-cache races."""
    request = getattr(error, 'request', None)
    if request is None:
        return fallback
    url = urlsplit(str(request.url))
    if (url.scheme != 'https' or url.hostname != 'chatgpt.com' or url.port not in (None, 443)
            or not url.path.startswith('/backend-api/codex/')):
        return fallback
    authorization = request.headers.get('Authorization', '')
    token = authorization[7:] if authorization.startswith('Bearer ') else fallback
    if getattr(error, 'status_code', None) in (402, 429):
        from agent import auxiliary_client as auxiliary
        if not auxiliary._is_overloaded_error(error):
            from agent.credential_pool import _codex_principal_identity
            principal = _codex_principal_identity(token)
            current = next((entry for entry in _broker_entries() if _principal(entry) == principal), None)
            if current is not None:
                return current.access_token
    return token
