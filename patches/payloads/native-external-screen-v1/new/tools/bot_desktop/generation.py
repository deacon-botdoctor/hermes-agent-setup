"""Read-only consumer of Operator-owned same-home generation records.

Operator Control owns prepare/commit, archive creation and profile replacement.
This module only verifies retained data for agent admission and inspection; it
never writes generation/config/authority state or infers Active permission.
"""
from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import NoReturn

from . import external

ROOT = Path("/etc/hermes-screen-generations")
VIEWS = ("external-authority.json", "lease.json", "participants.json",
         "baseline.json", "recovery.json", "grant.json")
MAX_VIEW = 262144
MAX_JOURNAL = 32768
PHASES = ("excluding", "prepared", "archived", "configuring", "replacing", "committed")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fail(reason) -> NoReturn:
    raise external.ExternalScreenError("generation excluded: " + reason)


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _object(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail("invalid " + name)
    return value


def decode_identity(data):
    """Structural identity only; no live permission is inferred from these bytes."""
    _object(data, {f.name for f in fields(external.Identity)}, "identity")
    for key in ("home", "endpoint", "control_endpoint"):
        external._literal(data[key])
    for key in ("generation", "authority"):
        external.short_string(data[key], key)
    if data["mode"] != external.EXTERNAL or not _digest(data["native_incarnation"]):
        _fail("identity mode/incarnation")
    for key in set(data) - {"home", "mode", "endpoint", "control_endpoint", "generation", "authority", "native_incarnation"}:
        if type(data[key]) is not int or data[key] < 0:
            _fail("identity device/inode")
    return external.Identity(**data)


def _stable(home, identity):
    if str(home) != identity.home:
        _fail("profile mismatch")
    state = home / "bot-desktop"
    external.private_directory(state)
    info = state.lstat()
    if (info.st_dev, info.st_ino) != (identity.state_device, identity.state_inode):
        _fail("state directory changed")
    for name, expected in (("activity", (identity.activity_device, identity.activity_inode)),
                           ("lease", (identity.lease_device, identity.lease_inode)),
                           ("participants", (identity.participants_device, identity.participants_inode))):
        with external.PrivateLock(state / (name + ".lock")) as lock:
            if lock.identity != expected:
                _fail("stable " + name + " lock changed")


def _raw(path, maximum=MAX_VIEW, *, archive=False):
    external.private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if archive:
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1):
                _fail("archive custody")
        else:
            external._regular(before)
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        mark = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode, s.st_uid,
                          s.st_nlink, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > maximum or mark(before) != mark(after) or mark(after) != mark(path.lstat()):
            _fail("data changed/over bound")
        return raw
    finally:
        os.close(fd)


def _json(raw):
    from .retirement import _unique
    return json.loads(raw, object_pairs_hook=_unique)


def _authority(identity):
    return {"schema": 2, "home": identity.home, "endpoint": identity.endpoint,
            "generation": identity.generation, "authority": identity.authority,
            "activity": [identity.activity_device, identity.activity_inode],
            "control_endpoint": identity.control_endpoint, "native_incarnation": identity.native_incarnation,
            "lease": [identity.lease_device, identity.lease_inode],
            "participants": [identity.participants_device, identity.participants_inode],
            "state": [identity.state_device, identity.state_inode]}


def _authorization(home):
    from . import origin
    if os.geteuid() == 0:
        _fail("SDK must run unprivileged")
    # No CLI, config or environment selector. Reuse origin's root ancestry,
    # O_NOFOLLOW, 0644, nlink, size and stable-read discipline.
    data, digest = origin._read(ROOT / (_sha(str(home).encode()) + ".json"))
    _object(data, ("schema", "issuer", "home", "uid", "nonce", "phase", "old_identity",
                   "old_scope", "old_views", "new_identity", "extinction", "evidence"), "root authorization")
    if (type(data["schema"]) is not int or data["schema"] != 1 or data["issuer"] != "Operator"
            or data["home"] != str(home) or type(data["uid"]) is not int
            or data["uid"] != os.geteuid() or not _digest(data["nonce"])
            or data["phase"] not in ("prepare", "commit")):
        _fail("root authorization scope/version")
    old = decode_identity(data["old_identity"])
    _stable(home, old)
    _object(data["old_views"], VIEWS, "root old view inventory")
    if any(v is not None and not _digest(v) for v in data["old_views"].values()):
        _fail("root old view digest")
    scope = _object(data["old_scope"], ("boot_id", "wayland_generation", "origin_sha256"), "old scope")
    for key in ("boot_id", "wayland_generation"):
        external.short_string(scope[key], key)
    if not _digest(scope["origin_sha256"]):
        _fail("old origin digest")
    evidence = _object(data["evidence"], ("prepare_sha256", "creation_sha256", "native_sha256",
                                         "ingress_sha256", "producers_sha256"), "evidence")
    if not _digest(evidence["prepare_sha256"]):
        _fail("prepare evidence missing")
    if data["phase"] == "prepare":
        if data["new_identity"] is not None or data["extinction"] is not None or any(
                evidence[k] is not None for k in evidence if k != "prepare_sha256"):
            _fail("mixed prepare/commit")
    else:
        new = decode_identity(data["new_identity"])
        _stable(home, new)
        same = ("home", "mode", "activity_device", "activity_inode", "lease_device", "lease_inode",
                "participants_device", "participants_inode", "state_device", "state_inode")
        if any(getattr(old, k) != getattr(new, k) for k in same):
            _fail("stable custody changed")
        if (new.authority != data["nonce"] or new.generation == old.generation
                or new.native_incarnation == old.native_incarnation
                or Path(new.endpoint).parent == Path(old.endpoint).parent
                or Path(new.endpoint).parent != Path(new.control_endpoint).parent
                or new.endpoint == new.control_endpoint or not all(_digest(v) for v in evidence.values())):
            _fail("distinct new generation/retained evidence required")
        ended = _object(data["extinction"], ("kind", "boot_id", "old_compositor", "evidence_sha256"), "extinction")
        process = _object(ended["old_compositor"], ("pid", "start_ticks"), "old compositor")
        if any(type(v) is not int or v < 1 for v in process.values()) or not _digest(ended["evidence_sha256"]):
            _fail("old compositor evidence missing")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if ended["boot_id"] != boot or ended["kind"] not in ("different-boot", "compositor-ended"):
            _fail("current extinction boot")
        if (ended["kind"] == "different-boot") != (scope["boot_id"] != boot):
            _fail("extinction scope mismatch")
    return data, digest


def _journal(home):
    path = home / "bot-desktop/generation.json"
    try:
        data = _json(_raw(path, MAX_JOURNAL))
    except FileNotFoundError:
        return None
    _object(data, ("schema", "nonce", "phase", "prepare", "old_identity", "before", "excluded",
                   "archive", "new_identity", "config_before", "config_after", "previous"), "journal")
    if (type(data["schema"]) is not int or data["schema"] != 1 or not _digest(data["nonce"])
            or data["phase"] not in PHASES or not _digest(data["prepare"])
            or not _digest(data["config_before"])):
        _fail("journal version/phase")
    decode_identity(data["old_identity"])
    _object(data["before"], VIEWS, "view inventory")
    if any(v is not None and not _digest(v) for v in data["before"].values()):
        _fail("journal view digest")
    for key in ("archive", "config_after", "previous"):
        if data[key] is not None and not _digest(data[key]):
            _fail("journal digest")
    if data["new_identity"] is not None:
        decode_identity(data["new_identity"])
    return data


def require_current(home):
    """Called by resolve BEFORE identity admission, including passive readers."""
    journal = _journal(Path(home))
    if journal is None:
        try:
            (ROOT / (_sha(str(home).encode()) + ".json")).lstat()
        except FileNotFoundError:
            return  # Unchanged legacy path, before any root lifecycle record.
        _authorization(Path(home))
        _fail("root generation transaction lacks its SDK journal")
    if journal is not None:
        if journal["phase"] != "committed" or journal["new_identity"] is None:
            _fail("generation transaction incomplete; retry exact root transaction")
        identity = decode_identity(journal["new_identity"])
        authorization, _ = _authorization(Path(home))
        if (authorization["phase"] != "commit" or authorization["nonce"] != journal["nonce"]
                or authorization["new_identity"] != identity.as_dict()
                or authorization["old_views"] != journal["before"]):
            _fail("root authorization no longer commits this generation")
        if _json(_raw(Path(home) / "bot-desktop/external-authority.json")) != _authority(identity):
            _fail("committed authority changed")
        _verify_archive(Path(home), journal)


def _inventory(home):
    result = {}
    for name in VIEWS:
        try:
            result[name] = _sha(_raw(home / "bot-desktop" / name))
        except FileNotFoundError:
            result[name] = None
    if any(result[k] is None for k in ("external-authority.json", "lease.json", "participants.json")):
        _fail("old canonical data incomplete")
    return result


def require_admission(identity):
    """Keep new-generation RFB links out until the canonical first grant exists.

    Reuse baseline/grant validators, not a caller-set ready flag. Once the
    genuine initial grant exists, normal passive watching while excluded stays
    supported. Legacy generations without this journal keep their old rules.
    """
    if _journal(Path(identity.home)) is None:
        return
    from . import baseline, lease_external, origin, recovery
    require_current(identity.home)
    digest = origin.evidence(identity)
    initial = baseline.lineage(identity, digest)
    grant = external.read_record(Path(identity.home) / "bot-desktop/grant.json")
    lease_external.validate_grant(identity, digest, initial, grant, recovery.binding(identity, digest))


def _old_identity(home):
    """Old baseline indexes immutable root origin; it need not have a live socket."""
    from . import origin, lease_external
    baseline = _json(_raw(home / "bot-desktop/baseline.json"))
    fingerprint = baseline.get("identity")
    if not _digest(fingerprint):
        _fail("old baseline identity unavailable")
    record, digest = origin._read(origin.ROOT / (fingerprint + ".json"))
    identity = decode_identity(record.get("identity"))
    if identity.fingerprint != fingerprint or baseline.get("origin") != digest:
        _fail("old baseline/origin mismatch")
    _stable(home, identity)
    if _json(_raw(home / "bot-desktop/external-authority.json")) != _authority(identity):
        _fail("old structural authority mismatch")
    lease_external.validate(identity, _json(_raw(home / "bot-desktop/lease.json")))
    _validate_old_views(home, identity, digest)
    return identity, {"boot_id": record["boot_id"], "wayland_generation": record["wayland_generation"],
                      "origin_sha256": digest}


def _validate_old_views(home, identity, digest):
    data = {}
    for name in VIEWS:
        try:
            data[name] = _json(_raw(home / "bot-desktop" / name))
        except FileNotFoundError:
            if name in ("external-authority.json", "lease.json", "participants.json", "baseline.json"):
                raise
    decode_old_views(identity, digest, data)


def decode_old_views(identity, digest, data):
    """Pure legacy decoding; never live capability, cleanup or Active authority.

    Pending recovery retains pending bytes. Only a prior released target may
    structurally bind its historical grant, never current permission.
    """
    from . import baseline, lease_external, participants, recovery
    if data["external-authority.json"] != _authority(identity):
        _fail("old canonical authority is mixed")
    lease_external.validate(identity, data["lease.json"])
    initial = baseline.validate(identity, digest, data["baseline.json"])
    participants.validate(identity, data["participants.json"])
    log = recovery.validate(identity, digest, data["recovery.json"], initial) if "recovery.json" in data else None
    released = [event["target"] for event in log["events"] if event["phase"] == "released"] if log else []
    if "grant.json" in data:
        lease_external.validate_grant(identity, digest, initial, data["grant.json"], released[-1] if released else None)


def status(home=None):
    from hermes_constants import get_hermes_home
    home = external.canonical_home(home if home is not None else get_hermes_home())
    journal = _journal(home)
    if journal is None:
        identity, scope = _old_identity(home)
    else:
        if journal["phase"] == "committed":
            require_current(home)
            identity = decode_identity(journal["new_identity"])
        else:
            identity = decode_identity(journal["old_identity"])
        scope = _authorization(home)[0]["old_scope"]
    _stable(home, identity)
    return {"schema": 1, "home": str(home), "identity": identity.as_dict(),
            "old_scope": scope, "views": _inventory(home), "journal": journal,
            "active_owner_granted": False, "inspection_only": True}


def _verify_archive(home, journal):
    if journal["archive"] is None:
        _fail("archive not committed")
    target = home / "bot-desktop/generation-history" / journal["nonce"]
    raw = _raw(target / "manifest.json", MAX_JOURNAL, archive=True)
    if _sha(raw) != journal["archive"]:
        _fail("archive manifest drift")
    data = _json(raw)
    _object(data, ("schema", "nonce", "old_identity", "before", "excluded", "files",
                   "authorization_sha256", "previous"), "archive manifest")
    _object(data["files"], VIEWS, "archived view inventory")
    if (type(data["schema"]) is not int or data["schema"] != 1
            or data["before"] != journal["before"] or data["excluded"] != journal["excluded"]
            or data["previous"] != journal["previous"]
            or any(v is not None and not _digest(v) for v in data["files"].values())):
        _fail("archive manifest phase/digests")
    if data["nonce"] != journal["nonce"] or data["old_identity"] != journal["old_identity"]:
        _fail("archive lineage mismatch")
    for name in VIEWS:
        original = journal["before"][name]
        if original is not None and _sha(_raw(target / ("before-" + name), archive=True)) != original:
            _fail("original pre-exclusion bytes changed")
        digest = data["files"][name]
        if digest is not None and _sha(_raw(target / name, archive=True)) != digest:
            _fail("old generation bytes changed")
    if _sha(_raw(target / "authorization.json", archive=True)) != data["authorization_sha256"]:
        _fail("archived root authorization drift")
    authorized = _json(_raw(target / "authorization.json", archive=True))
    if (authorized["old_views"] != journal["before"] or authorized["nonce"] != journal["nonce"]
            or authorized["new_identity"] != journal["new_identity"]):
        _fail("archived root scope mismatch")
    if _sha(_raw(target / "lease-before-exclusion.json", archive=True)) != journal["before"]["lease.json"]:
        _fail("original lease bytes changed")
    if journal["previous"] is not None:
        if _sha(_raw(target / "previous-journal.json", MAX_JOURNAL, archive=True)) != journal["previous"]:
            _fail("previous generation chain changed")
    return data
