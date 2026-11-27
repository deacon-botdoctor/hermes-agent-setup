"""Aggregate C-input barrier and durable tuple custody, subordinate to the lease.

All journal writes require the existing lease lock. Caller holds activity EX
through native work and final publication. No error path releases a native gate.
"""
from __future__ import annotations

from pathlib import Path
import re
import secrets
import time

from . import external, retirement

DOMAIN = "wayvnc-c-input-same-incarnation"
SECONDS = 3.0


def target(value):
    if (not isinstance(value, dict) or set(value) != {"incarnation", "nonce", "fence"}
            or not isinstance(value["incarnation"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["incarnation"]) is None
            or not isinstance(value["nonce"], str)
            or re.fullmatch(r"(?:[0-9a-f]{2}){16,32}", value["nonce"]) is None
            or not _fence(value["fence"])):
        raise retirement.RetirementUnknown("invalid exact aggregate baseline tuple")
    return value


def _fence(value):
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
            and int(value) <= 2**64 - 1)


def capability(identity, *, deadline=None):
    data = retirement.request(identity, "baseline-capabilities", deadline=deadline)
    fixed = {"baseline_protocol": 1, "domain": DOMAIN, "available": True,
             "incarnation": identity.native_incarnation, "record_limit": 1,
             "pending_timeout_ms": 2000, "terminal_retention_ms": 60000,
             "application_cleanup_certified": False, "active_owner_granted": False}
    if (not isinstance(data, dict) or set(data) != set(fixed) | {"next_fence", "fence_held"}
            or any(type(data[k]) is not type(v) or data[k] != v for k, v in fixed.items())
            or type(data["fence_held"]) is not bool
            or data["next_fence"] is not None and not _fence(data["next_fence"])):
        raise retirement.RetirementUnknown("aggregate baseline capability/identity unsupported")
    return data


def receipt(value, expected, *, released=False):
    target(expected)
    fields = {"baseline_protocol", "domain", "incarnation", "nonce", "fence", "state", "stage", "reason",
              "fence_acquired", "fence_held", "released", "generation_valid",
              "same_connection_sync_observed", "compositor_processed",
              "application_cleanup_certified", "active_owner_granted"}
    bools = fields - {"baseline_protocol", "domain", "incarnation", "nonce", "fence", "state", "stage", "reason"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["baseline_protocol"]) is not int or value["baseline_protocol"] != 1
            or value["domain"] != DOMAIN or any(value[k] != v for k, v in expected.items())
            or any(type(value[k]) is not bool for k in bools)
            or value["application_cleanup_certified"] is not False or value["active_owner_granted"] is not False
            or value["state"] not in {"pending", "failed", "compositor_processed"}):
        raise retirement.RetirementUnknown("aggregate baseline receipt malformed/mismatched")
    if released:
        # The native producer's successful release keeps state="compositor_processed"
        # but clears the live compositor_processed flag together with fence_held
        # (observed on every real release receipt on a fleet host, Nov 13 2026).
        # A released receipt that still claims a live processed fence is not a release.
        if (value["released"] is not True or value["fence_held"] or value["stage"] != "released"
                or value["compositor_processed"] or value["state"] != "compositor_processed"
                or value["reason"] is not None or not all(value[k] for k in
                    ("fence_acquired", "generation_valid", "same_connection_sync_observed"))):
            # Explicit cancellation of failed work can release a gate, but is
            # never released successful lineage for an Active publication.
            raise retirement.RetirementUnknown("exact successful aggregate fence release unconfirmed")
        return False
    if value["state"] == "failed":
        raise retirement.RetirementUnknown(f"aggregate baseline failed: {str(value['reason'])[:256]}")
    if value["reason"] is not None or value["released"] or not value["generation_valid"]:
        raise retirement.RetirementUnknown("aggregate baseline lost/released/uncertain")
    if value["state"] == "compositor_processed":
        if value["stage"] != "certified" or not all(value[k] for k in
                ("fence_acquired", "fence_held", "same_connection_sync_observed", "compositor_processed")):
            raise retirement.RetirementUnknown("aggregate baseline lacks held same-connection certification")
        return True
    if (value["stage"] not in {"queued", "acquired", "observed"} or value["compositor_processed"]
            or value["fence_acquired"] != (value["stage"] != "queued")
            or value["fence_held"] != value["fence_acquired"]
            or value["same_connection_sync_observed"] != (value["stage"] == "observed")):
        raise retirement.RetirementUnknown("aggregate baseline pending-stage inconsistency")
    return False


def read(identity, origin_digest):
    path = Path(identity.home) / "bot-desktop/baseline.json"
    try:
        data = external.read_record(path)
    except FileNotFoundError:
        return None  # Absence is preparation only, never lineage or active proof.
    return validate(identity, origin_digest, data)


def validate(identity, origin_digest, data):
    if (set(data) != {"schema", "identity", "origin", "target", "phase", "certificate", "release"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or data["identity"] != identity.fingerprint or data["origin"] != origin_digest
            or data["phase"] not in {"requested", "processed", "released"}):
        raise retirement.RetirementUnknown("aggregate custody journal incomplete/cross-origin")
    target(data["target"])
    if data["target"]["incarnation"] != identity.native_incarnation:
        raise retirement.RetirementUnknown("aggregate custody belongs to another native incarnation")
    if data["phase"] == "requested":
        if data["certificate"] is not None or data["release"] is not None:
            raise retirement.RetirementUnknown("partial aggregate requested journal")
    else:
        if not receipt(data["certificate"], data["target"]):
            raise retirement.RetirementUnknown("journal lacks actual completed baseline")
        if data["phase"] == "released":
            receipt(data["release"], data["target"], released=True)
        elif data["release"] is not None:
            raise retirement.RetirementUnknown("partial aggregate release journal")
    return data


def write(identity, journal):
    identity.validate()
    external.write_record(Path(identity.home) / "bot-desktop/baseline.json", journal)


def prepare(identity, origin_digest):
    """Caller owns lease lock + activity EX; persist exact tuple BEFORE begin."""
    journal = read(identity, origin_digest)
    if journal is not None:
        return journal
    caps = capability(identity)
    if caps["fence_held"] or caps["next_fence"] is None:
        raise retirement.RetirementUnknown("aggregate gate owned/serial exhausted; no replacement")
    journal = {"schema": 1, "identity": identity.fingerprint, "origin": origin_digest,
               "target": {"incarnation": identity.native_incarnation,
                          "nonce": secrets.token_hex(32), "fence": caps["next_fence"]},
               "phase": "requested", "certificate": None, "release": None}
    write(identity, journal)
    return journal


def barrier(identity, journal, *, deadline, cancellation=None):
    """Returned proof must still be persisted/validated by the owning lease."""
    from .lease_external import _check_cancel
    _check_cancel(cancellation)
    expected = journal["target"]
    data = retirement.request(identity, "baseline-begin", expected, deadline=deadline)
    while True:
        _check_cancel(cancellation)
        complete = receipt(data, expected)
        caps = capability(identity, deadline=deadline)
        if complete:
            if not caps["fence_held"]:
                raise retirement.RetirementUnknown("aggregate gate lost after completion")
            return data
        time.sleep(min(external.POLL_SECONDS, retirement._remaining(deadline)))
        data = retirement.request(identity, "baseline-status", expected, deadline=deadline)


def release(identity, journal, *, deadline):
    """Only after final serialized lease write. Never invoked from finally."""
    data = retirement.request(identity, "baseline-release", journal["target"], deadline=deadline)
    receipt(data, journal["target"], released=True)
    caps = capability(identity, deadline=deadline)
    if caps["fence_held"]:
        raise retirement.RetirementUnknown("aggregate gate remains held after exact release")
    return data


def lineage(identity, origin_digest, *, deadline=None):
    journal = read(identity, origin_digest)
    if journal is None or journal["phase"] != "released":
        raise retirement.RetirementUnknown("initial managed aggregate lineage absent/unreleased")
    if capability(identity, deadline=deadline)["fence_held"]:
        raise retirement.RetirementUnknown("native admission gate currently excludes input")
    return journal
