"""Opt-in conditional DIRECT API admission. Native FIFO remains the only executor.

Only the installed plugin loader creates a ProviderBinding. HTTP JSON contains
frozen references, never provider code, a principal override, or permission.
Remote permission is bounded-fresh, NOT atomic with its source service.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, fields
import hashlib
import inspect
import json
import math
import time
import uuid
from typing import NoReturn

from hermes_state_runtime import RuntimeStoreError, _epoch, _json

PROTOCOL = 'conditional_api_v1'
VERSION = 1
OBSERVATION_SECONDS = 5.0
READER_TIMEOUT_SECONDS = 10.0
PREFIX = 'gateway.conditional_api.v1.'
_PROCESS = uuid.uuid4().hex  # persisted observations cannot survive process restart


class EnqueueObservationCancelled(RuntimeStoreError):
    """No native write began; the HTTP owner must drop its reservation then cancel."""
    def __init__(self, cancellation):
        super().__init__('admission_cancelled')
        self.cancellation = cancellation


def _ref(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise RuntimeStoreError('invalid_params', field='conditional')
    return value


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise RuntimeStoreError('invalid_params', field='conditional.digest')
    return value


def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def frozen_input_digest(text, history, instructions=None):
    """Parent freezes exactly these fields; v1 accepts text and explicit history only."""
    if (not isinstance(text, str) or not isinstance(history, list)
            or (instructions is not None and not isinstance(instructions, str))
            or any(not isinstance(item, dict) or set(item) != {'role', 'content'}
                   or not isinstance(item['role'], str) or not isinstance(item['content'], str) for item in history)):
        raise RuntimeStoreError('invalid_params', field='conditional.input')
    return _hash({'text': text, 'history': history, 'instructions': instructions})


def _key(kind, *refs):
    return PREFIX + kind + '.' + _hash({'refs': list(refs)})


def _get(conn, key):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def _put(conn, key, value):
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, _json(value)))


def _deny(detail, reason='permission_denied') -> NoReturn:
    raise RuntimeStoreError(reason, field='conditional', detail=detail)


@dataclass(frozen=True)
class Guard:
    version: int
    provider_id: str
    provider_version: int
    effect_id: str
    grant_id: str
    grant_revision: str
    profile_id: str
    session_id: str
    scope_digest: str
    condition_digest: str
    context_digest: str
    input_digest: str
    source_revision: str
    authorization_revision: str
    control_epoch: int
    execution_policy_digest: str
    input_epoch: int
    expires_at: float

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            _deny('unknown or incomplete guard schema', 'invalid_params')
        if type(value['version']) is not int or value['version'] != VERSION:
            _deny('unsupported guard version')
        if type(value['provider_version']) is not int or value['provider_version'] != VERSION:
            _deny('unsupported provider version')
        for name in ('provider_id', 'effect_id', 'grant_id', 'grant_revision', 'profile_id',
                     'session_id', 'source_revision', 'authorization_revision'):
            _ref(value[name])
        for name in ('scope_digest', 'condition_digest', 'context_digest', 'input_digest', 'execution_policy_digest'):
            _digest(value[name])
        for name in ('control_epoch', 'input_epoch'):
            if type(value[name]) is not int or value[name] < 0:
                _deny('invalid local fence', 'invalid_params')
        if not _number(value['expires_at']):
            _deny('invalid grant expiry', 'invalid_params')
        return cls(**value)

    @property
    def digest(self):
        return _hash(asdict(self))


def payload_guard(payload):
    """Never interpret a future/unknown persisted guard as a legacy API turn."""
    keys = [k for k in payload if k.startswith('conditional_api')]
    if not keys:
        return None
    if keys != [PROTOCOL]:
        _deny('unsupported persisted conditional protocol')
    return Guard.parse(payload[PROTOCOL])


@dataclass(frozen=True)
class ObservationRequest:
    guard: Guard
    phase: str
    payload_json: str
    authority_epoch: int
    admission_id: str | None


@dataclass(frozen=True)
class Observation:
    """Trusted reader verdict. Timestamp is actual source observation time, not event time."""
    version: int
    guard_digest: str
    grant_revision: str
    source_revision: str
    authorization_revision: str
    observed_at: float
    permitted: bool


@dataclass(frozen=True)
class Attestation:
    guard_digest: str
    epoch: int
    process: str
    observed_at: float
    received_at: float
    deadline: float
    registration: object


def _manager(authority):
    from hermes_cli.plugins import get_plugin_manager
    # The caller is in owner_scope; this is the same home used by plugin discovery.
    from hermes_constants import hermes_home_key
    manager = get_plugin_manager()
    if manager.scope_key != hermes_home_key(authority.db.db_path.parent):
        _deny('provider profile does not own native store')
    return manager


def _binding(authority, provider_id):
    binding = getattr(_manager(authority), '_conditional_api_providers', {}).get(provider_id)
    if binding is None or not binding.enabled():
        _deny('trusted provider unavailable')
    return binding


def register_provider(ctx, provider):
    """Called only through PluginContext.register_conditional_api_provider()."""
    if ctx.get_config('conditional_api.enabled', False) is not True:
        _deny('provider is not explicitly enabled')
    if (type(getattr(provider, 'version', None)) is not int or provider.version != VERSION
            or not inspect.iscoroutinefunction(getattr(provider, 'observe', None))):
        _deny('provider requires version=1 and async observe(request)', 'invalid_params')
    manager = ctx._manager
    if not hasattr(manager, '_conditional_api_providers'):
        manager._conditional_api_providers = {}
    if ctx.plugin_id in manager._conditional_api_providers:
        _deny('provider identity already registered', 'admission_conflict')
    binding = ProviderBinding(ctx, provider)
    binding.registration = ctx._track_mapping_entry('conditional_api_provider', ctx.plugin_id,
                                                    manager._conditional_api_providers, binding)
    return binding


class ProviderBinding:
    """Protected Python-only acceptance handle; do not export through a model tool."""
    def __init__(self, ctx, provider):
        self._ctx, self._provider = ctx, provider
        self.provider_id = ctx.plugin_id
        self.registration = None

    def enabled(self):
        return (not self._ctx._load_abandoned
                and self._ctx.get_config('conditional_api.enabled', False) is True
                and getattr(self._provider, 'version', None) == VERSION
                and getattr(self._ctx._manager, '_conditional_api_providers', {}).get(self.provider_id) is self)

    def _owner(self, authority):
        if _binding(authority, self.provider_id) is not self:
            _deny('provider registration changed')

    def bind_snapshot_model(self, authority, *, guard, credential):
        """Bind one read-only native credential to an existing frozen guard.

        Python-only owner-loop seam; no grant, input, receipt or auth write. The
        caller owns authorization for the source home/row and chosen model.
        Never export this handle or credentials through tools/HTTP/config.
        """
        self._owner(authority)
        authority._require_admission_open()
        asyncio.get_running_loop()
        from gateway.session_authorities import active_authority
        if active_authority(authority.runner) is not authority:
            _deny('snapshot model requires the existing active native owner')
        from hermes_cli.auth_codex_snapshot import ReadOnlyCodexCredential
        from agent.conditional_snapshot_policy import _SnapshotCodexConsumer
        parsed = Guard.parse(guard)
        if type(credential) is not ReadOnlyCodexCredential or parsed.provider_id != self.provider_id:
            _deny('native read-only credential and this provider frozen guard required')
        credential.require_live()
        with authority.db._read_ctx() as conn:
            _epoch(conn, authority.epoch)
            _grant(conn, parsed)
            if _get(conn, _key('effect', self.provider_id, parsed.effect_id)) is not None:
                _deny('spent effect is observe-only; never bind or replace model credentials')
        epoch = authority.epoch
        def require_owner():
            # Pure local checks also run on the existing native request worker.
            # Do not consult the ambient profile manager from another thread.
            if (not self.enabled() or authority.epoch != epoch
                    or authority.retiring or authority.retired):
                _deny('snapshot model owner/registration changed')
        require_owner()
        if not hasattr(self, '_snapshot_models'):
            self._snapshot_models = {}
        if parsed.digest in self._snapshot_models:
            _deny('snapshot model binding is immutable; never rebind', 'admission_conflict')
        self._snapshot_models[parsed.digest] = _SnapshotCodexConsumer(
            credential, parsed.digest, parsed.expires_at, require_owner)

    async def accept_grant(self, authority, *, grant_id, grant_revision, session_id, scope_digest,
                           condition_digest, authorization_revision, expires_at, actor_id, actor_name,
                           execution_policy, reserve_session=False, session_declaration=None):
        """Trusted owner-acceptance boundary, not an arbitrary plugin API bind bypass.

        The profile provider verifies original authorization/current owner scope first.
        Optional zero-run reservation drives existing native bind steps only after all
        fields are validated. A partial reservation is inert, and never rebaselines.
        """
        self._owner(authority)
        for value in (grant_id, grant_revision, session_id, authorization_revision, actor_id, actor_name):
            _ref(value)
        for value in (scope_digest, condition_digest):
            _digest(value)
        from agent.turn_author import parse_turn_author
        if parse_turn_author({'id': actor_id, 'name': actor_name, 'is_bot': True}) != {
                'id': actor_id, 'name': actor_name, 'is_bot': True}:
            _deny('actor must be an exact native attribution value', 'invalid_params')
        if not _number(expires_at) or expires_at <= time.time():
            _deny('grant expired', 'invalid_params')
        from agent.conditional_snapshot_policy import policy_mapping, POLICY_DIGEST
        from gateway.session import _is_path_unsafe
        from gateway.session_contract import SessionRef
        from gateway.session_api import bind_api_steps, restore_api_session, run_steps_off_loop
        if (not isinstance(execution_policy, dict) or _json(execution_policy) != _json(policy_mapping())
                or type(reserve_session) is not bool
                or _is_path_unsafe(session_id)):
            _deny('exact protected snapshot-only policy and safe target required', 'invalid_params')
        if reserve_session:
            _ref(session_declaration)
        elif session_declaration is not None:
            _deny('declaration requires protected reservation', 'invalid_params')
        authority._require_admission_open()
        # logical/physical retargeting is not an accepted dedicated mapping.
        if authority.logical_owner(session_id) != session_id:
            _deny('dedicated target changed', 'admission_conflict')
        draft = dict(version=VERSION, provider_id=self.provider_id, grant_id=grant_id,
                     grant_revision=grant_revision, profile_id=authority.profile_id, session_id=session_id,
                     scope_digest=scope_digest, condition_digest=condition_digest,
                     authorization_revision=authorization_revision, expires_at=expires_at,
                     actor_id=actor_id, actor_name=actor_name, execution_policy_digest=POLICY_DIGEST,
                     session_declaration=session_declaration)
        key = _key('grant', self.provider_id, grant_id, grant_revision)

        def validate(conn):
            _epoch(conn, authority.epoch)
            authority._require_admission_open()
            self._owner(authority)
            if expires_at <= time.time():
                _deny('grant expired')
            from hermes_state_mutation_retirement import retired_in
            if retired_in(conn, session_id):
                _deny('dedicated target retired')
            row = conn.execute('SELECT source FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is not None and row['source'] != 'api_server':
                _deny('foreign or bot-room target')
            from hermes_state_local import API_BINDING_PREFIX
            if _get(conn, API_BINDING_PREFIX + session_id) is not None:
                existing_binding = _api_binding(conn, authority.profile_id, session_id)
                if reserve_session and existing_binding.get('declared_key') != session_declaration:
                    _deny('existing dedicated declaration changed', 'admission_conflict')
            if authority.physical_target(SessionRef(authority.profile_id, session_id)) != session_id:
                _deny('dedicated physical target changed', 'admission_conflict')
            saved = _get(conn, key)
            if saved is not None and (any(saved.get(k) != v for k, v in draft.items()) or saved.get('revoked') is not False):
                _deny('grant revision changed or revoked', 'admission_conflict')
            fence_key = _key('fence', session_id)
            fence = _get(conn, fence_key) or {'version': VERSION, 'control_epoch': 0, 'input_epoch': 0}
            reservation = {**draft, 'control_epoch': fence['control_epoch'], 'input_epoch': fence['input_epoch']}
            held = _get(conn, _key('reservation', session_id))
            if held is not None and held != reservation:
                _deny('reservation/grant/fences changed; explicit new mapping required', 'admission_conflict')
            if saved is not None and any(saved.get(k) != fence[k] for k in ('control_epoch', 'input_epoch')):
                _deny('grant was invalidated; never regrant after control/reset', 'stale_generation')
            policy = _get(conn, _key('policy', session_id))
            if policy is not None and _hash(policy) != POLICY_DIGEST:
                _deny('protected session policy changed', 'admission_conflict')
            return fence, reservation

        with authority.db._read_ctx() as conn:
            validate(conn)  # validate all fields/current scope BEFORE driving native bind steps

        def reserve(conn):
            fence, reservation = validate(conn)
            # Commits in the SAME native bind transaction. Without final grant it is inert.
            _put(conn, _key('fence', session_id), fence)
            _put(conn, _key('reservation', session_id), reservation)
            _put(conn, _key('policy', session_id), policy_mapping())

        def accept(conn):
            fence, reservation = validate(conn)
            binding = _api_binding(conn, authority.profile_id, session_id)
            if reserve_session and binding.get('declared_key') != session_declaration:
                _deny('dedicated session declaration changed', 'admission_conflict')
            record = {**reservation, 'binding_digest': _hash(binding), 'revoked': False}
            saved = _get(conn, key)
            if saved is not None and saved != record:
                _deny('accepted binding or grant changed', 'admission_conflict')
            reserve(conn)
            _put(conn, key, record)
            return record

        def steps():
            if reserve_session:
                yield from bind_api_steps(authority, session_id, declared_key=session_declaration,
                                          _authorize_write=reserve)
            else:
                restore_api_session(authority, session_id)
            yield lambda: authority.db._execute_write(accept)
            with authority.db._read_ctx() as conn:
                return _get(conn, key)
        # The entire reserve/restore/accept drive is tracked and shielded. Cancellation
        # cannot detach a committed bind from grant completion; process death leaves
        # only its immutable inert reservation, reconciled against exact fences on retry.
        return await run_steps_off_loop(authority, steps())

    async def freeze(self, authority, *, grant_id, grant_revision, effect_id, context_digest,
                     input_digest, source_revision):
        """Freeze references before hosted preparation. Does not authorize or submit work."""
        self._owner(authority)
        for value in (effect_id, source_revision):
            _ref(value)
        _digest(context_digest)
        _digest(input_digest)
        with authority.db._read_ctx() as conn:
            grant = _get(conn, _key('grant', self.provider_id, grant_id, grant_revision))
        if not grant or grant.get('revoked') or grant['expires_at'] <= time.time():
            _deny('accepted grant unavailable')
        names = {f.name for f in fields(Guard)}
        guard = {k: v for k, v in grant.items() if k in names}
        guard.update(provider_version=VERSION, effect_id=effect_id, context_digest=context_digest,
                     input_digest=input_digest, source_revision=source_revision)
        parsed = Guard.parse(guard)
        with authority.db._read_ctx() as conn:
            _epoch(conn, authority.epoch)
            _grant(conn, parsed)
        return asdict(parsed)

    async def revoke_grant(self, authority, *, grant_id, grant_revision):
        """Monotonic local revocation; cannot reopen a revision or erase its receipts."""
        def owner():
            # Disabling execution must not disable protected local revocation.
            if getattr(_manager(authority), '_conditional_api_providers', {}).get(self.provider_id) is not self:
                _deny('provider registration changed')
        owner()
        from gateway.session_runtime_workers import tracked_write
        def write(conn):
            _epoch(conn, authority.epoch)
            owner()
            key = _key('grant', self.provider_id, grant_id, grant_revision)
            record = _get(conn, key)
            if record is None:
                _deny('accepted grant unavailable', 'not_found')
            record['revoked'] = True
            _put(conn, key, record)
            return record
        return await tracked_write(authority, lambda: authority.db._execute_write(write), ordered=True)

    async def lookup_effect(self, authority, *, effect_id):
        """Protected lost-ACK lookup without regrant, rebind or resubmission.

        None means no local effect binding in a successful read. A spent identity
        without its retained native receipt is unknown_execution, never a retry.
        """
        if getattr(_manager(authority), '_conditional_api_providers', {}).get(self.provider_id) is not self:
            _deny('provider registration changed')
        _ref(effect_id)
        from hermes_state_runtime import get_session_admission
        with authority.db._read_ctx() as conn:
            effect = _get(conn, _key('effect', self.provider_id, effect_id))
        if effect is None:
            return None
        if effect.get('version') != VERSION or effect.get('profile_id') != authority.profile_id:
            _deny('unsupported effect identity')
        row = get_session_admission(authority.db, admission_id=effect['admission_id'])
        if row is None:
            _deny('spent effect has no retained native receipt; never resubmit', 'unknown_execution')
        if (row['principal_id'] != 'api' or row['target_session_id'] != effect['session_id']
                or row['request_id'] != effect['request_id']):
            _deny('native effect receipt identity differs', 'admission_conflict')
        return row


def _api_binding(conn, profile_id, session_id):
    from hermes_state_local import API_BINDING_PREFIX
    from hermes_state_mutation_retirement import retired_in
    binding = _get(conn, API_BINDING_PREFIX + session_id)
    row = conn.execute('SELECT source,session_key FROM sessions WHERE id=?', (session_id,)).fetchone()
    if (binding is None or row is None or row['source'] != 'api_server' or retired_in(conn, session_id)
            or binding.get('profile_id') != profile_id or binding.get('session_id') != session_id
            or binding.get('storage_source', 'api_server') != 'api_server'
            or binding.get('route') != row['session_key']):
        _deny('dedicated native API binding unavailable')
    # No messaging, bot-room or borrowed automation provenance can pass this gate.
    return binding


def snapshot_policy_readback(authority, session_id):
    """Read the actual protected policy, never infer it from global tool config."""
    from agent.conditional_snapshot_policy import policy_mapping, POLICY_DIGEST
    with authority.db._read_ctx() as conn:
        _epoch(conn, authority.epoch)
        binding = _api_binding(conn, authority.profile_id, session_id)
        if _hash(_get(conn, _key('policy', session_id))) != POLICY_DIGEST:
            _deny('protected snapshot-only session policy unavailable')
    return {'policy': policy_mapping(), 'policy_digest': POLICY_DIGEST, 'binding_digest': _hash(binding)}


def snapshot_execution(authority, ref, payload):
    from agent.conditional_snapshot_policy import SnapshotExecution, POLICY_DIGEST
    guard = validate_payload(authority, ref.session_id, payload)
    with authority.db._read_ctx() as conn:
        policy = _get(conn, _key('policy', ref.session_id))
        if guard is None:
            if policy is not None:
                _deny('protected snapshot session requires a guarded admission')
            return None
        _epoch(conn, authority.epoch)
        _grant(conn, guard)
    if guard.execution_policy_digest != POLICY_DIGEST:
        _deny('protected admitted policy fingerprint differs')
    consumer = getattr(_binding(authority, guard.provider_id), '_snapshot_models', {}).get(guard.digest)
    if consumer is None:
        _deny('native read-only snapshot model binding unavailable; no auth fallback')
    consumer.require_live()
    return SnapshotExecution(authority.profile_id, ref.session_id, authority.epoch, _json(payload),
                             model_consumer=consumer)


def _grant(conn, guard):
    record = _get(conn, _key('grant', guard.provider_id, guard.grant_id, guard.grant_revision))
    if not record or record.get('version') != VERSION or record.get('revoked') is not False:
        _deny('accepted grant missing, revoked or unsupported')
    for name in ('provider_id', 'grant_id', 'grant_revision', 'profile_id', 'session_id', 'scope_digest',
                 'condition_digest', 'authorization_revision', 'expires_at', 'control_epoch', 'input_epoch',
                 'execution_policy_digest'):
        if record.get(name) != getattr(guard, name):
            _deny('guard differs from immutable grant')
    fence = _get(conn, _key('fence', guard.session_id))
    if (not fence or fence.get('version') != VERSION
            or (fence.get('control_epoch'), fence.get('input_epoch')) != (guard.control_epoch, guard.input_epoch)):
        _deny('local human or control fence advanced', 'stale_generation')
    if record['binding_digest'] != _hash(_api_binding(conn, guard.profile_id, guard.session_id)):
        _deny('accepted API mapping changed', 'admission_conflict')
    from agent.conditional_snapshot_policy import policy_mapping, POLICY_DIGEST
    if (guard.execution_policy_digest != POLICY_DIGEST
            or _hash(_get(conn, _key('policy', guard.session_id))) != POLICY_DIGEST):
        _deny('protected session execution policy changed', 'admission_conflict')
    if time.time() >= guard.expires_at:
        _deny('grant expired')
    return record


def advance_input(conn, session_id):
    """New authoritative nonconditional input only, in its own native admission transaction."""
    key = _key('fence', session_id)
    fence = _get(conn, key)
    if fence is not None:
        if fence.get('version') != VERSION:
            _deny('unsupported local fence')
        fence['input_epoch'] += 1
        _put(conn, key, fence)


def advance_control(conn, session_id, identity):
    """Same native transaction as controls/mutations. Exact control replay is inert."""
    key = _key('fence', session_id)
    fence = _get(conn, key)
    if fence is None:
        return
    if fence.get('version') != VERSION:
        _deny('unsupported local fence')
    spent = _key('control', session_id, identity)
    if _get(conn, spent) is None:
        fence['control_epoch'] += 1
        _put(conn, key, fence)
        _put(conn, spent, {'version': VERSION})


async def fence_interrupt(authority, ref, actor, generation):
    with authority.db._read_ctx() as conn:
        if _get(conn, _key('fence', ref.session_id)) is None:
            return  # unguarded sessions retain native no-write/no-yield Stop semantics
    from gateway.session_runtime_workers import tracked_write
    def write(conn):
        _epoch(conn, authority.epoch)
        advance_control(conn, ref.session_id, _json({'kind': 'interrupt', 'actor': actor.subject,
                                                   'generation': generation}))
    await tracked_write(authority, lambda: authority.db._execute_write(write), ordered=True)


async def fence_mutation(authority, ref, actor, params):
    with authority.db._read_ctx() as conn:
        if _get(conn, _key('fence', ref.session_id)) is None:
            return
    from gateway.session_runtime_workers import tracked_write
    identity = _json({'kind': 'mutation', 'actor': actor.subject, 'request_id': params['request_id'],
                      'operation': params['operation']})
    def write(conn):
        _epoch(conn, authority.epoch)
        advance_control(conn, ref.session_id, identity)
    await tracked_write(authority, lambda: authority.db._execute_write(write), ordered=True)


def _input_hash(payload):
    data = payload['api_turn_v1']
    return frozen_input_digest(payload['text'], data['history'], data['settings']['ephemeral_system_prompt'])


def validate_payload(authority, sid, payload):
    guard = payload_guard(payload)
    if guard is None:
        return None
    if guard.profile_id != authority.profile_id or guard.session_id != sid:
        _deny('guard target mismatch', 'admission_conflict')
    if _input_hash(payload) != guard.input_digest:
        _deny('frozen input mismatch', 'admission_conflict')
    from agent.conditional_snapshot_policy import POLICY_DIGEST
    data = payload['api_turn_v1']
    if (guard.execution_policy_digest != POLICY_DIGEST or data['history'] != []
            or data['settings']['ephemeral_system_prompt'] is not None
            or data['settings'].get('room_dispatch') is not None
            or data['settings'].get('room_execution_policy') is not None):
        _deny('snapshot-only lane requires frozen input only and no room policy')
    return guard


async def observe(authority, guard, payload, *, phase, admission_id=None):
    """No SQLite/session lock is held while the trusted reader awaits its sources."""
    binding = _binding(authority, guard.provider_id)
    epoch, started_wall, started_mono = authority.epoch, time.time(), time.monotonic()
    request = ObservationRequest(guard, phase, _json(payload), epoch, admission_id)
    task = asyncio.create_task(binding._provider.observe(request))
    try:
        done, _ = await asyncio.wait({task}, timeout=READER_TIMEOUT_SECONDS)
        if not done:
            _deny('trusted source reader timed out')
        answer = task.result()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise RuntimeStoreError('permission_denied', field='conditional', detail='trusted source reader unavailable') from exc
    finally:
        if not task.done():
            # Never wait for a cancellation-resistant reader past the native timeout.
            # A late result is inert; only core publishes accepted attestations.
            task.cancel()
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    received_wall, received_mono = time.time(), time.monotonic()
    elapsed = received_mono - started_mono
    if (type(answer) is not Observation or type(answer.version) is not int or answer.version != VERSION
            or answer.permitted is not True or answer.guard_digest != guard.digest
            or answer.grant_revision != guard.grant_revision or answer.source_revision != guard.source_revision
            or answer.authorization_revision != guard.authorization_revision
            or not _number(answer.observed_at) or elapsed < 0 or elapsed > READER_TIMEOUT_SECONDS
            or abs((received_wall - started_wall) - elapsed) > 0.5
            or not 0 <= received_wall - answer.observed_at <= OBSERVATION_SECONDS):
        _deny('source or authorization observation refused, stale or inconsistent')
    if _binding(authority, guard.provider_id) is not binding or authority.epoch != epoch:
        _deny('provider or authority changed during observation', 'stale_epoch')
    deadline = received_mono + min(OBSERVATION_SECONDS - (received_wall - answer.observed_at),
                                   guard.expires_at - received_wall)
    return Attestation(guard.digest, epoch, _PROCESS, answer.observed_at, received_wall, deadline, binding)


def _check_local(authority, conn, guard, attestation, *, head=None):
    _epoch(conn, authority.epoch)
    record = _grant(conn, guard)
    binding = _binding(authority, guard.provider_id)
    now = time.time()
    if (attestation is None or attestation.registration is not binding or attestation.process != _PROCESS
            or attestation.epoch != authority.epoch or attestation.guard_digest != guard.digest
            or not 0 <= now - attestation.observed_at <= OBSERVATION_SECONDS
            or time.monotonic() > attestation.deadline):
        _deny('fresh local attestation unavailable')
    from hermes_state_runtime_workers import runtime_lineage, worker_states
    targets = sorted(runtime_lineage(conn, guard.session_id))
    pending = conn.execute("SELECT admission_id FROM session_admissions WHERE target_session_id IN "
                           "(SELECT value FROM json_each(?)) AND status IN ('queued','started','unknown')",
                           (json.dumps(targets),)).fetchall()
    if any(r[0] != head for r in pending) or worker_states(conn, guard.session_id):
        _deny('competing native input or worker', 'session_busy')
    live = authority.sessions.get(guard.session_id)
    running = getattr(authority.runner, '_is_session_running', None)
    if live is None or running is None or running(live.route):
        _deny('native API lane unavailable', 'session_busy')
    # Accepted observation is native state, but its process nonce/monotonic deadline is ephemeral.
    _put(conn, _key('observation', guard.provider_id, guard.effect_id), {
        'version': VERSION, 'guard_digest': guard.digest, 'authority_epoch': authority.epoch,
        'process': _PROCESS, 'observed_at': attestation.observed_at, 'received_at': attestation.received_at,
        'monotonic_deadline': attestation.deadline, 'admission_id': head})
    return record


def enqueue_guard(authority, sid, request_id, payload, attestation, target_guard):
    def check(conn):
        guard = validate_payload(authority, sid, payload)
        if guard is None:
            _deny('conditional descriptor missing')
        record = _check_local(authority, conn, guard, attestation)
        if payload['api_turn_v1'].get('turn_author') != {
                'id': record['actor_id'], 'name': record['actor_name'], 'is_bot': True}:
            _deny('automation attribution differs from protected grant')
        key = _key('effect', guard.provider_id, guard.effect_id)
        # A native exact replay bypasses this guard. A spent key reaching here means its
        # canonical row was lost/pruned: NEVER recreate it, even under another target/grant.
        if _get(conn, key) is not None:
            _deny('effect already spent; recover original receipt', 'admission_conflict')
        target_guard(conn)
        _put(conn, key, {'version': VERSION, 'profile_id': guard.profile_id, 'session_id': sid,
                         'request_id': request_id, 'guard_digest': guard.digest, 'payload_digest': _hash(payload)})
    return check


def bind_effect_receipt(conn, payload, admission_id):
    guard = payload_guard(payload)
    if guard is None:
        return
    key = _key('effect', guard.provider_id, guard.effect_id)
    record = _get(conn, key)
    if not record or record['guard_digest'] != guard.digest or record['payload_digest'] != _hash(payload):
        _deny('effect was not accepted by conditional transaction')
    record['admission_id'] = admission_id
    _put(conn, key, record)


def start_guard(authority, first, attestation, conn):
    if first is None:
        return
    guard = validate_payload(authority, first['target_session_id'], first['payload'])
    if guard is None:
        return
    # Claim's exact-head comparison follows this guard in the SAME transaction.
    row = conn.execute("SELECT admission_id FROM session_admissions WHERE target_session_id=? "
                       "AND status='queued' ORDER BY seq LIMIT 1", (guard.session_id,)).fetchone()
    if row is None or row[0] != first['admission_id']:
        return  # normal native False/re-read path; never refuse a successor
    _check_local(authority, conn, guard, attestation, head=first['admission_id'])
    effect = _get(conn, _key('effect', guard.provider_id, guard.effect_id))
    if not effect or effect.get('admission_id') != first['admission_id'] or effect['guard_digest'] != guard.digest:
        _deny('spent effect/receipt binding changed', 'admission_conflict')


async def preclaim(authority, ref):
    from hermes_state_runtime import list_session_admissions
    first = next((r for r in list_session_admissions(authority.db, session_id=ref.session_id)
                  if r['status'] == 'queued'), None)
    if first is None:
        return None, None
    try:
        guard = validate_payload(authority, ref.session_id, first['payload'])
        if guard is None:
            return first, None
        return first, await observe(authority, guard, first['payload'], phase='start', admission_id=first['admission_id'])
    except RuntimeStoreError as exc:
        # The exact refused row, not a later head. Native settlement releases all waiter kinds.
        await authority._refuse_head(ref, first['admission_id'], exc.reason)
        return first, None


async def prepare_enqueue(authority, kwargs):
    """Replays require no provider regrant. Payload still earns native exact digest checking."""
    descriptor = kwargs.get('conditional_api')
    if descriptor is None:
        return kwargs
    guard = Guard.parse(descriptor)
    if kwargs.get('session_id') != guard.session_id or not kwargs.get('run_owner_scope'):
        _deny('explicit API target and run ownership required', 'invalid_params')
    with authority.db._read_ctx() as conn:
        effect = _get(conn, _key('effect', guard.provider_id, guard.effect_id))
        grant = _get(conn, _key('grant', guard.provider_id, guard.grant_id, guard.grant_revision))
    if not grant or grant.get('version') != VERSION:
        _deny('protected grant missing')
    kwargs = dict(kwargs)
    kwargs['turn_author'] = {'id': grant['actor_id'], 'name': grant['actor_name'], 'is_bot': True}
    kwargs['history_from_session'] = False
    kwargs['session_history_delivery'] = ''
    # Native payload construction owns settings/defaults. No network in its generator.
    if effect is not None:
        if (effect.get('guard_digest') != guard.digest or effect.get('session_id') != guard.session_id
                or effect.get('request_id') != kwargs.get('active_run_id')):
            _deny('effect retry identity changed', 'admission_conflict')
        kwargs['_conditional_attestation'] = None
    else:
        probe = {'text': kwargs['user_message'], 'history': kwargs['conversation_history']}
        if frozen_input_digest(probe['text'], probe['history'], kwargs.get('ephemeral_system_prompt')) != guard.input_digest:
            _deny('frozen input mismatch', 'admission_conflict')
        kwargs['_conditional_attestation'] = await observe(authority, guard, probe, phase='enqueue')
    return kwargs


def request_descriptor(body, idempotency_key, gateway_session_key):
    if any(key.startswith('conditional') and key != 'conditional' for key in body):
        _deny('unsupported conditional protocol field', 'invalid_params')
    if 'conditional' not in body:
        return None
    if (not idempotency_key or gateway_session_key or set(body) - {
            'input', 'session_id', 'conversation_history', 'instructions', 'conditional'}
            or not isinstance(body.get('input'), str) or not isinstance(body.get('conversation_history'), list)):
        _deny('conditional v1 needs exact key, explicit target/history, text and no alternate dispatch', 'invalid_params')
    guard = Guard.parse(body['conditional'])
    if body.get('session_id') != guard.session_id:
        _deny('conditional request target mismatch', 'admission_conflict')
    if frozen_input_digest(body['input'], body['conversation_history'], body.get('instructions')) != guard.input_digest:
        _deny('conditional request input mismatch', 'admission_conflict')
    return asdict(guard)


def classic_api_lane_enrolled(authority, route):
    """Sticky opt-in API lane ownership, not a second execution reservation.

    Classic ingress checks this immediately before its synchronous slot acquisition.
    Grant acceptance precedes effects; any older classic slot is checked by native
    enqueue/claim. Every new API human turn still uses the existing native FIFO.
    """
    with authority.db._read_ctx() as conn:
        return any(_get(conn, _key('fence', row[0])) is not None for row in conn.execute(
            "SELECT id FROM sessions WHERE source='api_server' AND session_key=?", (route,)))


def refuse_classic_api_lane(runner, route):
    from gateway.session_authorities import active_authority
    from gateway.session_ingress import executing_admission
    authority = active_authority(runner)
    if authority is not None and not executing_admission.get() and classic_api_lane_enrolled(authority, route):
        _deny('enrolled API work session requires native FIFO')


def capabilities(adapter):
    """Core support is distinct from loader/provider readiness; neither proves live qualification."""
    from gateway.session_authorities import active_authority
    from hermes_state_mutation_retirement import RETRY_HORIZON_S
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
    authority = active_authority(getattr(adapter, 'gateway_runner', None))
    retention = min(RETRY_HORIZON_S, RunIdempotencyStore.RETENTION_SECONDS)
    core = authority is not None and retention >= 86400
    providers = []
    if core:
        providers = sorted(name for name, binding in getattr(_manager(authority), '_conditional_api_providers', {}).items()
                           if binding.enabled())
    return {'version': VERSION, 'core_supported': core, 'providers_ready': providers,
            'enqueue_guard': core, 'start_guard': core, 'durable_grant': core, 'durable_input_control_fences': core,
            'protected_zero_run_reservation': core, 'snapshot_only_policy': core,
            'receipt_protocol': 'api_runs_v1', 'principal': 'api', 'replay_retention_seconds': retention,
            'native_retired_receipt_seconds': RETRY_HORIZON_S, 'effect_identity': 'durable_no_automatic_pruning',
            'observation_max_age_seconds': OBSERVATION_SECONDS, 'reader_timeout_seconds': READER_TIMEOUT_SECONDS,
            'remote_atomicity': False, 'queued_cancel': core, 'generation_stop': core}
