"""External-mode branch of the existing native lease authority.

The lease owns aggregate bootstrap and subsequent exact participant retirement.
Origin evidence, local drain and native processing are separate prerequisites.
"""
from __future__ import annotations

import math
from pathlib import Path
import secrets
import time

from . import external

PHASES = {"active-agent": "agent", "active-human": "human", "quiescing": "excluded", "excluded": "excluded"}


def excluded(reason: str, identity=None):
    from .lease import Lease
    return Lease(holder="excluded", phase="excluded", since=0.0, reason=reason[:1024],
                 authority=identity.authority if identity else "invalid",
                 endpoint_generation=identity.generation if identity else "invalid",
                 endpoint_identity=f"{identity.device}:{identity.inode}" if identity else "invalid")


def read(identity):
    try:
        data = external.read_record(Path(identity.home) / "bot-desktop/lease.json")
        return validate(identity, data)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, external.ExternalScreenError) as exc:
        return excluded(f"external lease excluded: {exc}", identity)


def validate(identity, data):
    """Strict pure decoder for current state and preserved recovery preimages."""
    from .lease import Lease
    if not isinstance(data, dict):
        raise ValueError("external lease must be an object")
    if set(data) != set(Lease.__dataclass_fields__):
        raise ValueError("incomplete external lease")
    phase = data["phase"]
    if not isinstance(phase, str) or phase not in PHASES or data["holder"] != PHASES[phase]:
        raise ValueError("unsupported external phase/holder")
    if type(data["epoch"]) is not int or not 0 <= data["epoch"] <= 2**63 - 1:
        raise ValueError("invalid external epoch")
    since = data["since"]
    if type(since) not in (int, float) or not math.isfinite(since) or since < 0:
        raise ValueError("invalid external timestamp")
    if not isinstance(data["reason"], str) or len(data["reason"]) > 1024:
        raise ValueError("invalid external reason")
    if phase == "active-human":
        external.short_string(data["viewer_id"], "viewer_id")
    elif data["viewer_id"] is not None:
        raise ValueError("nonhuman viewer capability")
    if (data["authority"], data["endpoint_generation"], data["endpoint_identity"]) != (
            identity.authority, identity.generation, f"{identity.device}:{identity.inode}"):
        raise ValueError("external authority/endpoint generation mismatch")
    nonce = data["transition_nonce"]
    if phase == "quiescing":
        external.short_string(nonce, "transition nonce")
    elif nonce is not None:
        raise ValueError("unexpected transition nonce")
    return Lease(**data)


def get(home=None, *, deadline=None):
    try:
        identity = external.resolve(home)
        # Never expose an intermediate Active between serialized publication,
        # exact native release, grant commit and cancellation finalization.
        with external.PrivateLock(Path(identity.home) / "bot-desktop/lease.lock") as gate:
            gate.take(exclusive=False, seconds=0)
            identity.validate()
            state = read(identity)
            if state.phase in ("active-agent", "active-human"):
                from . import baseline, origin, retirement
                digest = origin.evidence(identity)
                journal = baseline.lineage(identity, digest, deadline=deadline)
                retirement.capability(identity, deadline=deadline)
                _committed_binding(identity, state, digest, journal)
            return state
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, external.ExternalScreenError) as exc:
        return excluded(f"external identity excluded: {exc}")


def _committed_binding(identity, state, digest, journal):
    from . import recovery, retirement
    if journal is None or journal["phase"] != "released":
        raise retirement.RetirementUnknown("committed aggregate lineage absent")
    grant = external.read_record(Path(identity.home) / "bot-desktop/grant.json")
    binding = recovery.binding(identity, digest)
    validate_grant(identity, digest, journal, grant, binding)
    if grant["lease"] != state.as_dict() or read(identity).as_dict() != state.as_dict():
        raise retirement.RetirementUnknown("active lease lacks exact committed grant binding")


def validate_grant(identity, digest, journal, grant, recovery_target=None):
    from . import retirement
    if not isinstance(grant, dict) or "lease" not in grant:
        raise retirement.RetirementUnknown("incomplete grant binding")
    state = validate(identity, grant["lease"])
    if state.phase not in ("active-agent", "active-human"):
        raise retirement.RetirementUnknown("grant does not bind an active lease")
    expected = {"schema": 1, "identity": identity.fingerprint, "origin": digest,
                "baseline": journal["target"], "lease": state.as_dict()}
    if recovery_target is not None:
        expected.update(schema=2, recovery=recovery_target)
    if grant != expected or type(grant.get("schema")) is not int:
        raise retirement.RetirementUnknown("grant history lacks exact lineage binding")


from contextlib import contextmanager


@contextmanager
def write_fence(identity, validated):
    """Local final fence only, after full off-loop validation; never an admission API.

    The bridge retains activity SH and holds this nonblocking lease SH through
    writer.write. No native IPC or await occurs in this local publication fence.
    """
    from . import baseline, origin
    with external.PrivateLock(Path(identity.home) / "bot-desktop/lease.lock") as gate:
        gate.take(exclusive=False, seconds=0)
        identity.validate()
        current = read(identity)
        if (current.phase not in ("active-agent", "active-human")
                or current.as_dict() != validated.as_dict()):
            raise external.ExternalScreenError("validated input authority changed before write")
        digest = origin.evidence(identity)
        _committed_binding(identity, current, digest, baseline.read(identity, digest))
        yield


def matches(state, identity, *, holder, viewer_id=None, epoch=None):
    return (state.phase == f"active-{holder}" and state.holder == holder
            and state.authority == identity.authority
            and state.endpoint_generation == identity.generation
            and state.endpoint_identity == f"{identity.device}:{identity.inode}"
            and (viewer_id is None or state.viewer_id == viewer_id)
            and (epoch is None or type(epoch) is int and state.epoch == epoch))


def _same_transition(current, quiescing):
    return (current.phase, current.transition_nonce, current.authority,
            current.endpoint_generation, current.endpoint_identity, current.epoch) == (
                quiescing.phase, quiescing.transition_nonce, quiescing.authority,
                quiescing.endpoint_generation, quiescing.endpoint_identity, quiescing.epoch)


def invalidate(identity, epoch, reason):
    """Loss of an owned transport excludes its epoch; never rewrites a successor."""
    from . import lease
    with external.PrivateLock(Path(identity.home) / "bot-desktop/lease.lock") as gate:
        gate.take(exclusive=True)
        identity.validate()
        current = read(identity)
        if current.epoch != epoch or current.phase not in ("active-agent", "active-human"):
            return
        if current.epoch >= 2**63 - 1:
            raise external.ExternalScreenError("external lease epoch exhausted")
        current.phase, current.holder, current.viewer_id = "excluded", "excluded", None
        current.transition_nonce = None
        current.epoch += 1
        current.reason = str(reason)[:1024]
        external.write_record(Path(identity.home) / "bot-desktop/lease.json", current.as_dict())
    lease._notify(identity.home, current)


def _require_transition(identity, quiescing):
    from . import retirement
    identity.validate()
    current = read(identity)
    if not _same_transition(current, quiescing):
        raise retirement.RetirementUnknown("external reconciliation superseded; no publication")
    return current


def _exclude_owned(identity, quiescing, published, reason):
    """Bounded failure finalization, including cancellation; never native release."""
    from . import lease
    state_dir = Path(identity.home) / "bot-desktop"
    with external.PrivateLock(state_dir / "lease.lock") as gate:
        gate.take(exclusive=True)
        identity.validate()
        current = read(identity)
        if not (_same_transition(current, quiescing)
                or published is not None and current.as_dict() == published):
            return  # A successor, not this failing operation, owns finalization.
        current.phase, current.holder, current.viewer_id = "excluded", "excluded", None
        current.transition_nonce = None
        current.epoch += 1
        current.reason = ("native_retirement_unknown: " + reason)[:1024]
        external.write_record(state_dir / "lease.json", current.as_dict())
    lease._notify(identity.home, current)


class Cancellation:
    """Fail-closed bridge signal; serialize cancellation against final commit.

    The guard covers only the local durable grant write, never native IPC/drain.
    Cancellation after a completed commit is a separate exact-epoch invalidation.
    """
    def __init__(self):
        import threading
        self.guard = threading.Lock()
        self.event = threading.Event()
        self.committed = None
        self.revoker = None
        self.failure = None

    def _revoke(self, committed):
        try:
            invalidate(*committed, "handoff caller canceled after publication")
        except BaseException as exc:
            self.failure = exc  # Retain failed ownership; never report cleanup success.
            raise

    def set(self):
        import threading
        fallback = None
        with self.guard:
            self.event.set()
            if self.committed is not None and self.revoker is None:
                committed = self.committed
                self.revoker = threading.Thread(target=self._revoke, args=(committed,),
                                                name="hermes-screen-cancel", daemon=False)
                try:
                    self.revoker.start()
                except RuntimeError:
                    fallback = committed
        if fallback is not None:
            # Interpreter shutdown/thread-start failure cannot drop the epoch.
            # The exceptional synchronous fallback is outside the small guard.
            self._revoke(fallback)

    def is_set(self):
        return self.event.is_set()


def _check_cancel(cancellation):
    if cancellation is not None:
        if type(cancellation) is not Cancellation:
            raise external.ExternalScreenError("invalid external handoff cancellation signal")
        if cancellation.is_set():
            raise external.ExternalScreenError("external handoff canceled; no grant")


def _commit_grant(identity, digest, journal, published, cancellation):
    from contextlib import nullcontext
    from . import baseline, recovery
    with cancellation.guard if cancellation is not None else nullcontext():
        _check_cancel(cancellation)
        grant = {
            "schema": 1, "identity": identity.fingerprint, "origin": digest,
            "baseline": baseline.read(identity, digest)["target"], "lease": published}
        binding = recovery.binding(identity, digest)
        if binding is not None:
            grant.update(schema=2, recovery=binding)
        external.write_record(Path(identity.home) / "bot-desktop/grant.json", grant)
        if cancellation is not None:
            cancellation.committed = (identity, published["epoch"])


def _drain(identity, quiescing, digest, cancellation, *, recover_before=None):
    """Caller owns activity EX, but no lease lock across native polling."""
    from . import baseline, origin, participants, recovery, retirement
    state_dir = Path(identity.home) / "bot-desktop"
    deadline = time.monotonic() + retirement.BARRIER_SECONDS
    retirement.capability(identity, deadline=deadline)
    if recover_before is not None:
        with external.PrivateLock(state_dir / "lease.lock") as gate:
            gate.take(exclusive=True)
            identity.validate()
            if read(identity).as_dict() != recover_before:
                raise retirement.RetirementUnknown("excluded recovery interval superseded before preparation")
            if origin.evidence(identity) != digest:
                raise retirement.RetirementUnknown("Operator origin changed before recovery")
            _check_cancel(cancellation)
            log = recovery.prepare(identity, digest, recover_before, deadline=deadline)
            # Preserve the old excluded lease and retry interval BEFORE replacing
            # it, and before any native begin. Ordinary handoff ordering is unchanged.
            external.write_record(state_dir / "lease.json", quiescing.as_dict())
        from . import lease
        lease._notify(identity.home, quiescing)
        journal = log["events"][-1]
        certificate = baseline.barrier(identity, journal, deadline=time.monotonic() + baseline.SECONDS,
                                       cancellation=cancellation)
        with external.PrivateLock(state_dir / "lease.lock") as gate:
            gate.take(exclusive=True)
            _require_transition(identity, quiescing)
            if recovery.read(identity, digest) != log:
                raise retirement.RetirementUnknown("aggregate recovery journal superseded")
            recovery.final_inventory(identity, log, deadline=time.monotonic() + retirement.BARRIER_SECONDS)
            journal.update(phase="processed", certificate=certificate)
            recovery.write(identity, digest, log)
        return {"receipt_scope": baseline.DOMAIN, "per_client_receipts_invented": False}, journal, log
    log = recovery.read(identity, digest)
    covered = recovery.tokens(log, participants.read(identity))
    result = participants.reconcile(identity, deadline=deadline, covered=covered)
    with external.PrivateLock(state_dir / "lease.lock") as gate:
        gate.take(exclusive=True)
        _require_transition(identity, quiescing)
        if origin.evidence(identity) != digest:
            raise retirement.RetirementUnknown("Operator origin changed during drain")
        _check_cancel(cancellation)
        journal = baseline.prepare(identity, digest)
    if journal["phase"] != "released":
        certificate = baseline.barrier(identity, journal, deadline=time.monotonic() + baseline.SECONDS,
                                       cancellation=cancellation)
        with external.PrivateLock(state_dir / "lease.lock") as gate:
            gate.take(exclusive=True)
            _require_transition(identity, quiescing)
            journal.update(phase="processed", certificate=certificate)
            baseline.write(identity, journal)
    return result, journal, None


def _publish(identity, quiescing, digest, journal, *, viewer_id, reason, cancellation, recovery_log=None):
    """Caller owns activity EX. Final lease write precedes exact native release.

    grant.json is written last: raw Active during release is not consumable. No
    caller cancellation/exception invokes a finally-release or substitutes proof.
    """
    from . import baseline, origin, recovery, retirement
    state_dir = Path(identity.home) / "bot-desktop"
    published = None
    try:
        with external.PrivateLock(state_dir / "lease.lock") as gate:
            gate.take(exclusive=True)
            current = _require_transition(identity, quiescing)
            if origin.evidence(identity) != digest:
                raise retirement.RetirementUnknown("Operator origin changed before publication")
            retirement.capability(identity)
            if recovery_log is not None:
                if recovery.read(identity, digest) != recovery_log:
                    raise retirement.RetirementUnknown("recovery changed before publication")
                recovery.final_inventory(identity, recovery_log,
                                         deadline=time.monotonic() + retirement.BARRIER_SECONDS)
            if journal["phase"] == "released":
                baseline.lineage(identity, digest)
            else:
                # A stored receipt alone is never current authority.
                now = retirement.request(identity, "baseline-status", journal["target"])
                if not baseline.receipt(now, journal["target"]):
                    raise retirement.RetirementUnknown("aggregate fence no longer certified")
            current.holder = "human" if viewer_id is not None else "agent"
            current.phase = "active-" + current.holder
            current.viewer_id, current.transition_nonce = viewer_id, None
            current.epoch += 1
            current.since, current.reason = time.time(), reason
            published = current.as_dict()
            _check_cancel(cancellation)
            external.write_record(state_dir / "lease.json", published)
            if journal["phase"] != "released":
                _check_cancel(cancellation)
                released = baseline.release(identity, journal, deadline=time.monotonic() + baseline.SECONDS)
                journal.update(phase="released", release=released)
                if recovery_log is None:
                    baseline.write(identity, journal)
                else:
                    recovery.write(identity, digest, recovery_log)
            # No lease writer can supersede publication while release/commit is
            # in progress. Revalidate privileged origin before the grant commit.
            if origin.evidence(identity) != digest:
                raise retirement.RetirementUnknown("Operator origin changed before grant commit")
            identity.validate()
            _commit_grant(identity, digest, journal, published, cancellation)
        return current
    except BaseException as exc:
        _exclude_owned(identity, quiescing, published, str(exc) or type(exc).__name__)
        raise


def reconcile(home=None, *, expected=None, viewer_id=None, reason="", cancellation=None,
              aggregate_recovery=False):
    """Explicit bounded recovery/bootstrap to agent, or an exact caller handoff.

    No clean flag, arbitrary receipt or origin argument. Failed/canceled work
    retains its exact durable tuple and any held native gate. Initial bootstrap
    requires authentic Operator evidence and zero READY clients, even watchers.
    """
    from . import lease, origin, retirement
    if type(aggregate_recovery) is not bool or aggregate_recovery and viewer_id is not None:
        raise external.ExternalScreenError("aggregate recovery is explicit agent-only recovery")
    if viewer_id is not None:
        external.short_string(viewer_id, "viewer_id")
    if not isinstance(reason, str) or len(reason) > 1024:
        raise external.ExternalScreenError("invalid lease reason")
    identity = external.resolve(home)
    state_dir = Path(identity.home) / "bot-desktop"
    with external.PrivateLock(state_dir / "lease.lock") as gate:
        gate.take(exclusive=True)
        identity.validate()
        before = read(identity)
        if before.epoch >= 2**63 - 2 or before.authority != identity.authority or before.epoch < 1:
            raise retirement.RetirementUnknown("lease incomplete/epoch exhausted; no recovery by replacement")
        if aggregate_recovery and before.phase != "excluded":
            raise retirement.RetirementUnknown("aggregate recovery requires an existing excluded interval")
        if expected is not None and before.as_dict() != expected:
            raise retirement.RetirementUnknown("external transition superseded before quiescing")
        # Always start a new nonce: an earlier waiter may still be alive. Its
        # finalizer must not share authority with this explicit recovery attempt.
        quiescing = lease.Lease(holder="excluded", phase="quiescing", epoch=before.epoch + 1,
                                since=time.time(), reason="Input draining; origin/native barriers pending",
                                authority=identity.authority, endpoint_generation=identity.generation,
                                endpoint_identity=f"{identity.device}:{identity.inode}",
                                transition_nonce=secrets.token_hex(32))
        if not aggregate_recovery:
            external.write_record(state_dir / "lease.json", quiescing.as_dict())
    if not aggregate_recovery:
        lease._notify(identity.home, quiescing)
    try:
        _check_cancel(cancellation)
        digest = origin.evidence(identity)
        # No lease/participant lock while waiting for maintained SH settlement.
        with identity.activity(exclusive=True):
            result, journal, recovery_log = _drain(
                identity, quiescing, digest, cancellation,
                recover_before=before.as_dict() if aggregate_recovery else None)
            current = _publish(identity, quiescing, digest, journal, viewer_id=viewer_id, reason=reason,
                               cancellation=cancellation, recovery_log=recovery_log)
    except BaseException as exc:
        _exclude_owned(identity, quiescing, None, str(exc) or type(exc).__name__)
        raise
    lease._notify(identity.home, current)
    return {**result, "lease": lease.public_view(current), "phase": current.phase,
            "code": "active-owner-published", "epoch": current.epoch,
            "reason": "Operator origin, drain and native barriers consumed by lease authority",
            "active_owner_granted": True, "application_cleanup_certified": False}


def transition(home=None, *, viewer_id=None, reason="", release=False, unless_human=False, epoch=None,
               cancellation=None):
    """Existing acquire/release entry points retain their exact capability guards."""
    from . import lease
    if not release:
        external.short_string(viewer_id, "viewer_id")
    if not isinstance(reason, str) or len(reason) > 1024:
        raise external.ExternalScreenError("invalid lease reason")
    identity = external.resolve(home)
    with external.PrivateLock(Path(identity.home) / "bot-desktop/lease.lock") as gate:
        gate.take(exclusive=True)
        identity.validate()
        before = read(identity)
        skip = epoch is not None and (type(epoch) is not int or before.epoch != epoch)
        skip = skip or release and (unless_human and before.holder != lease.AGENT
                                   or viewer_id is not None and before.viewer_id != viewer_id
                                   or before.phase == "active-agent")
        skip = skip or not release and before.phase == "active-human" and before.viewer_id == viewer_id
        expected = before.as_dict()
    if skip:
        return get(home)
    result = reconcile(home, expected=expected, viewer_id=None if release else viewer_id, reason=reason,
                       cancellation=cancellation)
    state = get(home)
    if state.epoch != result["epoch"]:
        raise external.ExternalScreenError("external handoff superseded after publication")
    if state.phase not in ("active-agent", "active-human"):
        raise external.ExternalScreenError(state.reason)
    return state
