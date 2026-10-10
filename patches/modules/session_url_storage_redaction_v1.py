"""Redact credential-bearing URLs in persisted copies, without changing live tool data."""
from pathlib import Path

MARKER = "HERMES_SESSION_URL_STORAGE_REDACTION_v1"

HELPER = '''
# HERMES_SESSION_URL_STORAGE_REDACTION_v1
def _redact_persisted_urls(value):
    """Return a storage copy. URL access grants never enter transcript storage."""
    if isinstance(value, str):
        from agent.redact import _redact_strict_url_credentials
        return _redact_strict_url_credentials(value)
    if isinstance(value, dict):
        return {key: _redact_persisted_urls(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_persisted_urls(item) for item in value]
    return value

'''


def patch_session_url_storage_redaction_v1(root: Path) -> bool:
    """Preflight every target before writing; refuse partial or mismatched application."""
    edits = {}
    for relative in ("agent/redact.py", "hermes_state_messages.py", "hermes_state.py"):
        edits[relative] = (root / relative).read_text()
    originals = dict(edits)
    if MARKER in edits["hermes_state_messages.py"] and HELPER not in edits["hermes_state_messages.py"]:
        raise RuntimeError("storage redaction marker has mismatched payload")

    def replace(relative, old, new):
        source = edits[relative]
        if source.count(new) == 1:
            return
        if new in source:
            raise RuntimeError(f"{relative}: storage redaction payload duplicated")
        if source.count(old) != 1:
            raise RuntimeError(f"{relative}: storage redaction anchor missing or ambiguous")
        edits[relative] = source.replace(old, new, 1)

    replace("agent/redact.py", '    "code", "signature", "x-amz-signature",\n',
            '    "code", "signature", "x-amz-signature",\n'
            '    "x-amz-security-token", "x-amz-credential",  # ' + MARKER + '\n')
    replace("agent/redact.py", 'def _redact_strict_url_credentials(text: str) -> str:',
            '_STRICT_SENSITIVE_QUERY_PARAMS = frozenset(map(_canonical_url_param_name, _SENSITIVE_QUERY_PARAMS))\n\n\n'
            'def _redact_strict_url_credentials(text: str) -> str:')
    replace("agent/redact.py", 'if _canonical_url_param_name(m.group(2)) in _SENSITIVE_QUERY_PARAMS else m.group(0), text)',
            'if _canonical_url_param_name(m.group(2)) in _STRICT_SENSITIVE_QUERY_PARAMS else m.group(0), text)')
    replace("hermes_state_messages.py", 'logger = logging.getLogger("hermes_state")',
            HELPER + 'logger = logging.getLogger("hermes_state")')
    replace("hermes_state_messages.py", '        if isinstance(content, str):\n',
            '        content = _redact_persisted_urls(content)\n        if isinstance(content, str):\n')
    replace("hermes_state_messages.py", '        if not display_metadata:\n',
            '        display_metadata = _redact_persisted_urls(display_metadata)\n        if not display_metadata:\n')
    replace("hermes_state_messages.py", '        return None if not value else (value if isinstance(value, str) else json.dumps(value))\n',
            '        value = _redact_persisted_urls(value)\n'
            '        return None if not value else (value if isinstance(value, str) else json.dumps(value))\n')
    replace("hermes_state_messages.py", '        _str_or_none = lambda v:',
            '        msg = dict(msg)\n'
            '        for key in ("reasoning", "reasoning_content", "api_content"):\n'
            '            msg[key] = _redact_persisted_urls(msg.get(key))\n'
            '        tool_calls = _redact_persisted_urls(tool_calls)\n'
            '        _str_or_none = lambda v:')
    replace("hermes_state_messages.py", '                json.dumps(tool_calls) if tool_calls else None)\n',
            '                json.dumps(_redact_persisted_urls(tool_calls)) if tool_calls else None)\n')
    replace("hermes_state_messages.py", '(_scrub_surrogates(api_content), session_id, self._encode_content(content)))',
            '(_scrub_surrogates(_redact_persisted_urls(api_content)), session_id, self._encode_content(content)))')
    replace("hermes_state_messages.py", '(_scrub_surrogates(api_content), row_id, session_id, self._encode_content(content)))',
            '(_scrub_surrogates(_redact_persisted_urls(api_content)), row_id, session_id, self._encode_content(content)))')
    replace("hermes_state_messages.py", 'it differed from ``content``, stored as sent except lone surrogates.',
            'it differed from ``content``, stored with lone surrogates and URL credentials redacted.')
    replace("hermes_state.py", '    sessions_dir = get_hermes_home() / "sessions"\n',
            '    from hermes_state_messages import _redact_persisted_urls\n'
            '    messages = _redact_persisted_urls(messages)\n'
            '    sessions_dir = get_hermes_home() / "sessions"\n')
    replace("hermes_state.py", '    with path.open("a", encoding="utf-8") as handle:\n',
            '    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)\n'
            '    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:\n'
            '        os.fchmod(handle.fileno(), 0o600)\n')
    for relative, source in edits.items():
        compile(source, relative, "exec")
    for relative, source in edits.items():
        if source != originals[relative]:
            (root / relative).write_text(source)
    return True
