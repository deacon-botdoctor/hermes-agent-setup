"""Per-process Responses transport for managed Codex CLI workers."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import tempfile
import uuid
import hashlib
import secrets
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from agent.broker_codex_jobs import _broker_entries, _principal
from agent.credential_pool import load_pool

_ENDPOINT = 'https://chatgpt.com/backend-api/codex'
_HOP = {'host', 'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
        'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length', 'content-encoding'}
_SESSION_HEADER = 'X-Hermes-Broker-Session'


def _plaintext(body):
    """Keep messages and completed calls; omit account-bound reasoning and item IDs."""
    items = body.get('input')
    if not isinstance(items, list):
        return body, bool(body.get('previous_response_id'))
    bound = bool(body.get('previous_response_id')) or any(
        isinstance(item, dict) and item.get('type') in {'compaction', 'item_reference'}
        for item in items)
    if bound:
        return body, True
    return {**body, 'input': [
        {key: value for key, value in item.items() if key != 'id'}
        if isinstance(item, dict) else item
        for item in items if not (isinstance(item, dict) and item.get('type') == 'reasoning')
    ]}, False



def _stream_start(response):
    """Retain only the initial SSE envelope until useful output or a rejection."""
    chunks = response.iter_bytes()
    if (response.status_code != 200 or
            'text/event-stream' not in response.headers.get('Content-Type', '')):
        return b'', chunks, None
    prefix = bytearray()
    pending = b''
    for chunk in chunks:
        prefix.extend(chunk)
        pending += chunk
        while b'\n\n' in pending:
            frame, pending = pending.split(b'\n\n', 1)
            data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines()
                              if line.startswith(b'data:'))
            if not data:
                continue
            try:
                event = json.loads(data)
            except ValueError:
                return bytes(prefix), chunks, None
            kind = event.get('type') if isinstance(event, dict) else None
            if kind in {'response.created', 'response.in_progress'}:
                continue
            if kind in {'error', 'response.failed'}:
                error = event.get('error') or (event.get('response') or {}).get('error') or event
                if error.get('code') in {'usage_limit_reached', 'rate_limit_exceeded',
                                        'quota_exceeded', 'insufficient_quota'}:
                    return bytes(prefix), chunks, {'error': error}
            return bytes(prefix), chunks, None
        if len(prefix) >= 262144:
            return bytes(prefix), chunks, None
    return bytes(prefix), chunks, None

@contextmanager
def _session_record(server, thread_id, deadline):
    # Codex keeps the same thread ID on resume; store only its principal, never a token.
    directory = server.state_home
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or (os.name != 'nt' and directory.stat().st_mode & 0o077):
        raise RuntimeError('Broker conversation state must be private')
    name = str(uuid.UUID(thread_id))
    record = directory / (name + '.json')
    lock = directory / (name + '.lock')
    if record.is_symlink() or lock.is_symlink():
        raise RuntimeError('Broker conversation state cannot be a symlink')
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'r+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if not lock.stat().st_size:
                stream.write(b'0'); stream.flush()
            def acquire():
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        while True:
            try:
                acquire(); break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Broker conversation is busy')
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            data = json.loads(record.read_text()) if record.exists() else None
            if data is not None and (data.get('version') != 1 or not isinstance(data.get('principal'), list)
                                     or len(data['principal']) != 2
                                     or type(data.get('plaintext_history', False)) is not bool):
                raise RuntimeError('Invalid broker conversation ownership')
            plaintext_history = bool(data and (data.get('plaintext_history') or tuple(data['principal']) != server.initial_principal))
            def save(principal):
                nonlocal plaintext_history
                plaintext_history = (plaintext_history or principal != server.initial_principal
                                     or bool(data and principal != tuple(data['principal'])))
                descriptor, filename = tempfile.mkstemp(dir=directory)
                path = Path(filename)
                try:
                    with os.fdopen(descriptor, 'w') as temporary:
                        json.dump({'version': 1, 'principal': list(principal),
                                   'plaintext_history': plaintext_history}, temporary)
                        temporary.flush(); os.fsync(temporary.fileno())
                    os.replace(path, record)
                finally:
                    path.unlink(missing_ok=True)
            yield (tuple(data['principal']) if data else None), plaintext_history, save
        finally:
            release()


class _Transport(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, idle_timeout, broker_home, initial_api_key, state_home):
        self.broker_home = broker_home
        self.codex_home = Path(state_home)
        self.state_home = Path(state_home) / "hermes-broker" / hashlib.sha256(str(Path(broker_home).resolve()).encode()).hexdigest()[:20]
        self.token = secrets.token_urlsafe(32)
        self.idle_timeout = idle_timeout
        self.sessions_lock = threading.Lock()
        self.sessions = {}
        self.initial_principal = _principal(SimpleNamespace(access_token=initial_api_key))
        super().__init__(('127.0.0.1', 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass  # Provider errors and request URLs can contain tenant data.

    def _error(self, status, message):
        raw = json.dumps({'error': {'message': message, 'type': 'broker_transport_error'}}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._request('GET')

    def do_POST(self):
        self._request('POST')

    def _request(self, method):
        self.close_connection = True
        self.deadline = time.monotonic() + self.server.idle_timeout
        if (self.headers.get('Origin') or not hmac.compare_digest(
                self.headers.get(_SESSION_HEADER, '') or self.headers.get('Authorization', '').removeprefix('Bearer '), self.server.token)):
            return self._error(401, 'Unauthorized local broker request')
        path = urlsplit(self.path).path
        if not ((method == 'GET' and path == '/models') or (method == 'POST' and path in {
                '/responses', '/responses/compact', '/images/generations', '/images/edits', '/alpha/search'})):
            return self._error(404, 'Unsupported broker endpoint')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if method == 'GET':
                return self._forward({}, method)
            if size <= 0 or self.headers.get('Transfer-Encoding'):
                return self._error(413, 'Unsupported request size or transfer encoding')
            self.connection.settimeout(self.server.idle_timeout)
            raw = self.rfile.read(size)
            if len(raw) != size:
                return self._error(400, 'Incomplete request')
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get('model'), str):
                return self._error(400, 'A model is required')
            session = self.headers.get('thread-id') or self.headers.get('session-id')
            if not session and path.startswith('/images/'):
                session = self.headers.get('x-codex-image-turn-id')
            if not session and path == '/alpha/search':
                session = body.get('id')
            if not session:
                return self._error(400, 'A Codex conversation identifier is required')
            with self.server.sessions_lock:
                state = self.server.sessions.setdefault(session, {'lock': threading.Lock()})
            if not state['lock'].acquire(timeout=max(0, self.deadline - time.monotonic())):
                return self._error(504, 'Broker conversation wait expired')
            try:
                with _session_record(self.server, session, self.deadline) as (principal, plaintext_history, save):
                    state['principal'], state['plaintext_history'], state['save'], state['thread_id'] = principal, plaintext_history, save, session
                    self._forward(body, method, state)
            finally:
                state['lock'].release()
        except (ValueError, TypeError, json.JSONDecodeError):
            self._error(400, 'Invalid Responses request')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # Never expose access tokens, upstream URLs, prompts or tracebacks.
            if not getattr(self, '_response_started', False):
                self._error(502, 'Broker request failed; inspect the owner-local runtime')

    def _forward(self, body, method, state=None):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(self.server.broker_home)
        try:
            return self._forward_bound(body, method, state)
        finally:
            reset_hermes_home_override(token)

    def _restore_conversation(self, body, state):
        from agent.broker_codex_history import restore_conversation
        if state is None:
            raise RuntimeError('A conversation is required for history recovery')
        replay, bound = _plaintext(restore_conversation(
            body, self.server.codex_home, state['thread_id'], self.deadline))
        if bound:
            raise RuntimeError('The retained conversation still contains account-bound context')
        return replay

    def _forward_bound(self, body, method, state):
        entries = _broker_entries()
        if not entries:
            return self._error(503, 'A valid managed broker pool is required')
        pool = load_pool('openai-codex')
        replay_body, bound = _plaintext(body) if self.path.startswith('/responses') or self.path == '/alpha/search' else (body, False)
        principal = state.get('principal') if state else None
        plaintext_history = bool(state and state.get('plaintext_history'))
        current = next((entry for entry in entries if _principal(entry) == principal), None)
        restored = False
        if principal is not None and current is None:
            if bound:
                replay_body = self._restore_conversation(body, state)
                bound, restored = False, True
            current = pool.select(model=body.get('model'))
        if current is None and bound:
            # Existing encrypted history retains the CLI's original account.
            current = next((entry for entry in entries
                            if _principal(entry) == self.server.initial_principal), None)
        if current is None and bound:
            replay_body = self._restore_conversation(body, state)
            bound, restored = False, True
        if current is None and not bound:
            current = pool.select(model=body.get('model'))
        if current is None or _principal(current) not in {_principal(entry) for entry in entries}:
            return self._error(503, 'No eligible broker subscription')
        attempted = set()
        retry_same = False
        # Retries share the CLI stream-idle window; no whole-job replay occurs.
        deadline = self.deadline
        reference = principal or self.server.initial_principal
        if plaintext_history and bound:
            replay_body = self._restore_conversation(body, state)
            bound, restored = False, True
        outgoing = replay_body if not bound and (plaintext_history or _principal(current) != reference) else body
        encoded = json.dumps(outgoing).encode() if method == 'POST' else None
        with httpx.Client(follow_redirects=False) as client:
            for _ in range(len(entries) + 1):
                identity = _principal(current)
                if identity in attempted and not retry_same:
                    return self._error(503, 'Broker subscriptions exhausted')
                retry_same = False
                attempted.add(identity)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._error(504, 'Broker recovery deadline exhausted')
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in _HOP | {'authorization', 'chatgpt-account-id', 'content-type',
                                                        _SESSION_HEADER.lower()}}
                if identity != reference or restored or plaintext_history:
                    headers = {key: value for key, value in headers.items()
                               if key.lower() != 'x-codex-turn-state'}
                headers.update(Authorization='Bearer ' + current.access_token,
                               **{'ChatGPT-Account-ID': identity[0], 'Content-Type': 'application/json'})
                if state is not None:
                    state['save'](identity)
                with client.stream(method, _ENDPOINT + self.path, content=encoded,
                                   headers=headers, timeout=remaining) as response:
                    prefix, chunks, stream_quota = _stream_start(response)
                    if bound and response.status_code == 400 and not restored:
                        response.read()
                        try:
                            context = response.json()
                        except ValueError:
                            context = {}
                        error = context.get('error', {}) if isinstance(context, dict) else {}
                        if isinstance(error, dict) and error.get('code') == 'invalid_encrypted_content':
                            replay_body = self._restore_conversation(body, state)
                            bound, restored, retry_same = False, True, True
                            encoded = json.dumps(replay_body).encode()
                            continue
                        prefix, chunks = response.content, iter(())
                    if response.status_code in {402, 429} or stream_quota:
                        # A rejected request is replayed before any response or tool data
                        # reaches the CLI. Completed client-side actions stay intact.
                        if stream_quota:
                            context = stream_quota
                            error = RuntimeError(json.dumps(context))
                        else:
                            response.read()
                            try:
                                context = response.json()
                            except ValueError:
                                context = {}
                            error = RuntimeError(response.text)
                        from agent.auxiliary_client import _is_overloaded_error
                        if not _is_overloaded_error(error):
                            if bound:
                                replay_body = self._restore_conversation(body, state)
                                bound, restored = False, True
                            replacement = pool.mark_exhausted_and_rotate(
                                status_code=response.status_code if not stream_quota else 429,
                                error_context=context, api_key_hint=current.access_token,
                                model=body.get('model'))
                            if (replacement is not None and _principal(replacement) in {_principal(entry) for entry in entries}
                                    and _principal(replacement) not in attempted
                                    and len(attempted) < len(entries)):
                                current = replacement
                                encoded = json.dumps(replay_body).encode() if method == 'POST' else None
                                continue
                        if not stream_quota:
                            # HTTP errors were consumed for native classification above.
                            prefix, chunks = response.content, iter(())
                    if state is not None and response.status_code < 400 and not stream_quota:
                        state['principal'] = _principal(current)
                    self.send_response(response.status_code)
                    for key, value in response.headers.items():
                        if key.lower() not in _HOP:
                            self.send_header(key, value)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self._response_started = True
                    if prefix:
                        self.wfile.write(prefix)
                        self.wfile.flush()
                    for chunk in chunks:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    return


@contextmanager
def transport(broker_home, initial_api_key, state_home, idle_timeout=300):
    server = _Transport(idle_timeout, broker_home, initial_api_key, state_home)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', server.token
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
