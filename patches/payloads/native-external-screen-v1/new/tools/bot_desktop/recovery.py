"""Explicit zero-READY C-baseline recovery, never a per-client receipt or grant.

The original baseline is not migrated/replaced. This additive, bounded journal
preserves its complete data and each recovery's real prior lease/grant/ledger.
Every writer owns lease EX + activity EX. Native failures keep their exact tuple
and held fence; ordinary Watch/Take cannot create or resume a recovery event.
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets

from . import baseline, external, participants, retirement

LIMIT = 64
MAXIMUM = participants.LEDGER_BYTES
FIELDS = {"target", "phase", "certificate", "release", "before", "attempts"}


def preserved(before, current):
    """No loss/rebinding of original tuples or historical completed receipts."""
    for token, row in before["participants"].items():
        now = current["participants"].get(token)
        if (now is None or any(now[k] != row[k] for k in participants.FIELDS - {"receipt"})
                or row["receipt"] is not None and now["receipt"] != row["receipt"]):
            raise retirement.RetirementUnknown("recovery participant history lost/changed")


def validate(identity, digest, data, initial):
    from . import lease_external
    if (not isinstance(data, dict)
            or set(data) != {"schema", "kind", "identity", "origin", "baseline", "events"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or data["kind"] != "aggregate-c-recovery"
            or data["identity"] != identity.fingerprint or data["origin"] != digest
            or data["baseline"] != initial or initial is None or initial["phase"] != "released"
            or not isinstance(data["events"], list) or not 1 <= len(data["events"]) <= LIMIT):
        raise retirement.RetirementUnknown("aggregate recovery journal malformed/cross-lineage")
    baseline.validate(identity, digest, initial)
    baseline.validate(identity, digest, data["baseline"])
    last_fence = int(initial["target"]["fence"])
    nonces = {initial["target"]["nonce"]}
    previous = None
    for index, event in enumerate(data["events"]):
        if (not isinstance(event, dict) or set(event) != FIELDS
                or event["phase"] not in {"requested", "processed", "released"}
                or index < len(data["events"]) - 1 and event["phase"] != "released"):
            raise retirement.RetirementUnknown("partial aggregate recovery history")
        expected = baseline.target(event["target"])
        if (expected["incarnation"] != identity.native_incarnation
                or int(expected["fence"]) <= last_fence or expected["nonce"] in nonces):
            raise retirement.RetirementUnknown("aggregate recovery reused/cross-generation tuple")
        last_fence = int(expected["fence"])
        nonces.add(expected["nonce"])
        before = event["before"]
        if not isinstance(before, dict) or set(before) != {"lease", "grant", "participants"}:
            raise retirement.RetirementUnknown("recovery preimages incomplete")
        state = lease_external.validate(identity, before["lease"])
        if state.phase != "excluded" or state.epoch < 1:
            raise retirement.RetirementUnknown("recovery requires a preserved excluded interval")
        if not isinstance(event["attempts"], list) or len(event["attempts"]) > LIMIT:
            raise retirement.RetirementUnknown("recovery retry history malformed/over bound")
        last_epoch = state.epoch
        for attempt in event["attempts"]:
            retry = lease_external.validate(identity, attempt)
            if retry.phase != "excluded" or retry.epoch <= last_epoch:
                raise retirement.RetirementUnknown("recovery retry interval reused/cross-lineage")
            last_epoch = retry.epoch
        grant = before["grant"]
        lease_external.validate_grant(identity, digest, initial, grant,
                                      previous["target"] if previous else None)
        if grant["lease"]["epoch"] >= state.epoch:
            raise retirement.RetirementUnknown("recovery preimage grant is not historical")
        snapshot = participants.validate(identity, before["participants"])
        if any(row["phase"] != "identified" for row in snapshot["participants"].values()):
            raise retirement.RetirementUnknown("recovery lacks complete known participant custody")
        if previous is not None:
            preserved(previous["before"]["participants"], snapshot)
        if event["phase"] == "requested":
            if event["certificate"] is not None or event["release"] is not None:
                raise retirement.RetirementUnknown("partial requested recovery")
        else:
            if not baseline.receipt(event["certificate"], expected):
                raise retirement.RetirementUnknown("recovery has no measured completed aggregate barrier")
            if event["phase"] == "released":
                baseline.receipt(event["release"], expected, released=True)
            elif event["release"] is not None:
                raise retirement.RetirementUnknown("partial recovery release")
        previous = event
    return data


def read(identity, digest):
    try:
        data = external.read_record(Path(identity.home) / "bot-desktop/recovery.json", maximum=MAXIMUM)
    except FileNotFoundError:
        return None
    return validate(identity, digest, data, baseline.read(identity, digest))


def write(identity, digest, data):
    validate(identity, digest, data, baseline.read(identity, digest))
    # Bound actual durable encoding, not a smaller canonical approximation.
    if len(json.dumps(data, allow_nan=False).encode()) > MAXIMUM:
        raise retirement.RetirementUnknown("recovery history capacity exhausted; no eviction")
    identity.validate()
    external.write_record(Path(identity.home) / "bot-desktop/recovery.json", data)


def tokens(data, current):
    covered = set()
    if data is not None:
        for event in data["events"]:
            if event["phase"] != "released":
                raise retirement.RetirementUnknown("aggregate recovery unfinished; explicit recovery required")
            snapshot = event["before"]["participants"]
            preserved(snapshot, current)
            covered.update(token for token, row in snapshot["participants"].items()
                           if row["input_epoch"] is not None)
    return covered


def binding(identity, digest):
    data = read(identity, digest)
    if data is None:
        return None
    tokens(data, participants.read(identity))
    return data["events"][-1]["target"]


def covered(identity, digest, row):
    data = read(identity, digest)
    if data is None:
        return False
    # Also rejects incomplete recovery before an old SDK object can re-admit.
    return row["token"] in tokens(data, participants.read(identity))


def zero_ready(identity, current, *, deadline, covered=()):
    if participants.inventory(identity, current, deadline=deadline, covered=covered):
        raise retirement.RetirementUnknown("aggregate recovery requires zero READY clients, including watchers; no eviction")


def prepare(identity, digest, before, *, deadline):
    """Persist genuine preimages and fresh native tuple before baseline-begin.

    Caller owns activity EX and lease EX. Participant lock is nonblocking here:
    never wait in lease->participant order against first-admission's inverse.
    """
    initial = baseline.read(identity, digest)
    if initial is None or initial["phase"] != "released":
        raise retirement.RetirementUnknown("recovery requires original released aggregate lineage")
    data = read(identity, digest)
    with participants.locked(identity, seconds=0):
        current = participants.read(identity)
        if data is not None and data["events"][-1]["phase"] != "released":
            # Resume exactly the owned target, never adopt a held foreign fence.
            preserved(data["events"][-1]["before"]["participants"], current)
            zero_ready(identity, current, deadline=deadline)
            event = data["events"][-1]
            last = event["attempts"][-1] if event["attempts"] else event["before"]["lease"]
            if before != last:
                if len(event["attempts"]) >= LIMIT:
                    raise retirement.RetirementUnknown("recovery retry history exhausted; no eviction")
                event["attempts"].append(before)
                write(identity, digest, data)
            return data
        covered_tokens = tokens(data, current)
        baseline.lineage(identity, digest, deadline=deadline)
        zero_ready(identity, current, deadline=deadline, covered=covered_tokens)
        if not any(row["input_epoch"] is not None and row["receipt"] is None
                   and token not in covered_tokens for token, row in current["participants"].items()):
            raise retirement.RetirementUnknown("no uncovered historical input; use ordinary reconciliation")
        caps = baseline.capability(identity, deadline=deadline)
        if caps["fence_held"] or caps["next_fence"] is None:
            raise retirement.RetirementUnknown("aggregate recovery fence owned/exhausted; no adoption")
        if data is None:
            data = {"schema": 1, "kind": "aggregate-c-recovery", "identity": identity.fingerprint,
                    "origin": digest, "baseline": initial, "events": []}
        if len(data["events"]) >= LIMIT:
            raise retirement.RetirementUnknown("aggregate recovery event capacity exhausted; no eviction")
        data["events"].append({
            "target": {"incarnation": identity.native_incarnation, "nonce": secrets.token_hex(32),
                       "fence": caps["next_fence"]},
            "phase": "requested", "certificate": None, "release": None, "attempts": [],
            "before": {"lease": before, "grant": external.read_record(Path(identity.home) / "bot-desktop/grant.json"),
                       "participants": current}})
        write(identity, digest, data)
    return data


def final_inventory(identity, data, *, deadline):
    current = participants.read(identity)
    preserved(data["events"][-1]["before"]["participants"], current)
    zero_ready(identity, current, deadline=deadline)
