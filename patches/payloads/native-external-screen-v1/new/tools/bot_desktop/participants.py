"""Durable bounded transport custody. Metadata never grants a lease or repairs itself."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import time

from . import external, retirement

LIMIT = 64
LEDGER_BYTES = 262144
FIELDS = {"token", "identity", "role", "viewer_hash", "phase", "peer_path", "unix_peer",
          "peer_address_length", "path_identity", "client_id", "input_epoch", "nonce", "receipt"}


@contextmanager
def locked(identity, *, seconds=external.LOCK_SECONDS):
    path = Path(identity.home) / "bot-desktop/participants.lock"
    with external.PrivateLock(path) as gate:
        if gate.identity != (identity.participants_device, identity.participants_inode):
            raise retirement.RetirementUnknown("participant lock generation changed")
        gate.take(exclusive=True, seconds=seconds)
        identity.validate()
        yield


def read(identity):
    data = external.read_record(Path(identity.home) / "bot-desktop/participants.json", maximum=LEDGER_BYTES)
    return validate(identity, data)


def validate(identity, data):
    if (set(data) != {"schema", "authority", "participants"} or type(data["schema"]) is not int
            or data["schema"] != 1 or data["authority"] != identity.authority
            or not isinstance(data["participants"], dict) or len(data["participants"]) > LIMIT):
        raise retirement.RetirementUnknown("participant inventory missing/incomplete/over bound")
    peers, ids, nonces = set(), set(), set()
    for token, row in data["participants"].items():
        if (not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None
                or not isinstance(row, dict) or set(row) != FIELDS or row["token"] != token
                or row["identity"] != identity.fingerprint or row["role"] not in {"agent", "human"}
                or row["phase"] not in {"reserved", "bound", "identified"}
                or row["peer_path"] != str(Path(identity.endpoint).parent / (".hp-" + token))
                or not isinstance(row["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", row["nonce"]) is None
                or row["nonce"] in nonces):
            raise retirement.RetirementUnknown("participant record malformed or cross-generation")
        nonces.add(row["nonce"])
        viewer = row["viewer_hash"]
        if (row["role"] == "agent" and viewer is not None or row["role"] == "human"
                and (not isinstance(viewer, str) or re.fullmatch(r"[0-9a-f]{64}", viewer) is None)):
            raise retirement.RetirementUnknown("participant role/viewer binding malformed")
        epoch = row["input_epoch"]
        if epoch is not None and (type(epoch) is not int or not 0 <= epoch < 2**63):
            raise retirement.RetirementUnknown("participant admitted epoch malformed")
        if row["phase"] == "reserved":
            if any(row[k] is not None for k in ("unix_peer", "peer_address_length", "path_identity",
                                                "client_id", "input_epoch", "receipt")):
                raise retirement.RetirementUnknown("partial reserved participant record")
            continue
        peer, length = row["unix_peer"], row["peer_address_length"]
        if (not isinstance(peer, str) or re.fullmatch(r"(?:[0-9a-f]{2}){1,108}", peer) is None
                or type(length) is not int or length != 2 + len(peer) // 2
                or not isinstance(row["path_identity"], list) or len(row["path_identity"]) != 2
                or any(type(v) is not int or v < 0 for v in row["path_identity"])
                or peer in peers):
            raise retirement.RetirementUnknown("participant kernel peer metadata malformed/ambiguous")
        peers.add(peer)
        name, _, padding = bytes.fromhex(peer).partition(b"\0")
        if name != os.fsencode(row["peer_path"]) or padding.strip(b"\0"):
            raise retirement.RetirementUnknown("recorded peer bytes do not match reserved pathname")
        if row["phase"] == "identified":
            retirement.target(tuple_for(row, identity))
            if row["client_id"] in ids:
                raise retirement.RetirementUnknown("native ID shared by distinct participants")
            ids.add(row["client_id"])
        elif row["client_id"] is not None or epoch is not None or row["receipt"] is not None:
            raise retirement.RetirementUnknown("partial unbound native-client metadata")
        if row["receipt"] is not None:
            if not retirement.receipt(row["receipt"], tuple_for(row, identity)):
                raise retirement.RetirementUnknown("durable participant receipt is not completed cleanup")
    return data


def write(identity, data):
    if len(json.dumps(data, allow_nan=False).encode()) > LEDGER_BYTES:
        raise retirement.RetirementUnknown("participant metadata exceeds durable bound")
    identity.validate()
    external.write_record(Path(identity.home) / "bot-desktop/participants.json", data)


def tuple_for(row, identity):
    return {"id": row["client_id"], "peer": row["unix_peer"],
            "incarnation": identity.native_incarnation, "nonce": row["nonce"]}


def kernel_peer(transport, path):
    """Actual Linux kernel-returned bytes/addrlen, not re-encoding getsockname's str.

    Layout follows the reviewed native peer binder. sockfs descriptor inode is
    deliberately not compared with the distinct bound pathname inode.
    """
    if sys.platform != "linux":
        raise retirement.RetirementUnknown("native local Linux socket custody unavailable")

    class SockaddrUn(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("path", ctypes.c_char * 108)]

    address = SockaddrUn()
    size = ctypes.c_uint(ctypes.sizeof(address))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getsockname.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    libc.getsockname.restype = ctypes.c_int
    if libc.getsockname(transport.fileno(), ctypes.byref(address), ctypes.byref(size)) != 0:
        raise OSError(ctypes.get_errno(), "getsockname failed")
    offset = SockaddrUn.path.offset
    if (address.family != socket.AF_UNIX or not offset < size.value <= ctypes.sizeof(address)
            or transport.getsockname() != str(path)):
        raise retirement.RetirementUnknown("unsupported/mismatched actual bound socket")
    raw = ctypes.string_at(ctypes.byref(address), size.value)[offset:]
    name, _, padding = raw.partition(b"\0")
    if name != os.fsencode(path) or not name.startswith(b"/") or padding.strip(b"\0"):
        raise retirement.RetirementUnknown("kernel pathname/length metadata mismatch")
    return raw.hex(), size.value


class Participant:
    def __init__(self, identity, *, role, viewer_id=None):
        from . import generation
        generation.require_admission(identity)
        if role not in {"agent", "human"}:
            raise retirement.RetirementUnknown("unsupported participant role")
        if role == "human":
            external.short_string(viewer_id, "viewer capability")
        elif viewer_id is not None:
            raise retirement.RetirementUnknown("agent cannot carry a human capability")
        self.identity = identity
        self.token = secrets.token_hex(16)
        self.role = role
        self.viewer_id = viewer_id
        self.client_id = None
        self.path = Path(identity.endpoint).parent / (".hp-" + self.token)
        if sys.platform != "linux" or len(os.fsencode(self.path)) >= 108:
            raise retirement.RetirementUnknown("native private peer pathname unsupported/too long")
        retirement.capability(identity)
        with locked(identity):
            data = read(identity)
            if len(data["participants"]) >= LIMIT or self.token in data["participants"]:
                raise retirement.RetirementUnknown("participant capacity exhausted; no record eviction")
            data["participants"][self.token] = {
                "token": self.token, "identity": identity.fingerprint, "role": role,
                "viewer_hash": hashlib.sha256(viewer_id.encode()).hexdigest() if viewer_id else None,
                "phase": "reserved", "peer_path": str(self.path), "unix_peer": None,
                "peer_address_length": None, "path_identity": None, "client_id": None,
                "input_epoch": None, "nonce": secrets.token_hex(16), "receipt": None}
            write(identity, data)  # Durable before bind/dial; never delete on failure.

    def connect(self):
        identity = self.identity
        retirement.capability(identity)
        transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            transport.set_inheritable(False)
            transport.settimeout(external.DIAL_SECONDS)
            transport.bind(str(self.path))  # Never unlink/reuse an existing name.
            os.chmod(self.path, 0o600)
            path_id = external.private_endpoint(str(self.path))
            peer, length = kernel_peer(transport, self.path)
            with locked(identity):
                data = read(identity)
                row = data["participants"][self.token]
                if row["phase"] != "reserved":
                    raise retirement.RetirementUnknown("participant reservation changed")
                row.update(phase="bound", unix_peer=peer, peer_address_length=length,
                           path_identity=list(path_id))
                write(identity, data)
            identity.validate()
            transport.connect(identity.endpoint)
            if external.private_endpoint(str(self.path)) != path_id or kernel_peer(transport, self.path) != (peer, length):
                raise retirement.RetirementUnknown("peer custody changed during dial")
            retirement.capability(identity)
            transport.setblocking(False)
            return transport
        except BaseException:
            transport.close()
            raise

    def identify(self):
        """Run while maintained RFB handshake proceeds; no lease/input required."""
        identity = self.identity
        deadline = time.monotonic() + retirement.BARRIER_SECONDS
        with locked(identity):
            original = dict(read(identity)["participants"][self.token])
        while True:
            rows = retirement.clients(identity, deadline=deadline)
            matches = [row for row in rows if row.get("unix_peer") == original["unix_peer"]]
            if len(matches) > 1:
                raise retirement.RetirementUnknown("native peer matches multiple current clients")
            if matches:
                row = matches[0]
                if row.get("peer_address_length") != original["peer_address_length"]:
                    raise retirement.RetirementUnknown("native kernel sockaddr length mismatch")
                break
            time.sleep(min(external.POLL_SECONDS, retirement._remaining(deadline)))
        with locked(identity):
            data = read(identity)
            current = data["participants"][self.token]
            if current != original or original["phase"] != "bound":
                raise retirement.RetirementUnknown("participant binding superseded/incomplete")
            if any(other["client_id"] == row["id"] for other in data["participants"].values()):
                raise retirement.RetirementUnknown("native client already has another custodian")
            current.update(phase="identified", client_id=row["id"])
            write(identity, data)
            self.client_id = row["id"]
        retirement.capability(identity, deadline=deadline)

    def admit(self, epoch, *, deadline=None):
        """Caller owns activity SH. Persist epoch BEFORE an eligible input write."""
        from . import lease_external
        with locked(self.identity, seconds=0):
            data = read(self.identity)
            row = data["participants"][self.token]
            if (row["phase"] != "identified" or self.client_id != row["client_id"]
                    or row["receipt"] is not None
                    or row["input_epoch"] not in (None, epoch)):
                raise retirement.RetirementUnknown("participant is not eligible for this input epoch")
            from . import origin, recovery
            if recovery.covered(self.identity, origin.evidence(self.identity), row):
                raise retirement.RetirementUnknown("aggregate-covered participant is irreversibly retired")
            state = lease_external.get(self.identity.home, deadline=deadline)
            if not lease_external.matches(state, self.identity, holder=self.role,
                                          viewer_id=self.viewer_id, epoch=epoch):
                raise retirement.RetirementUnknown("participant lease changed before input admission")
            if row["input_epoch"] is None:
                row["input_epoch"] = epoch
                write(self.identity, data)

    def retire(self, *, deadline):
        """Caller has stopped new input, and retains actual transport/SH custody.

        No lease lock or activity EX here: a closing link must never wait for
        its own SH. Exact completed cleanup is persisted while replay exists.
        """
        with locked(self.identity, seconds=min(external.LOCK_SECONDS, retirement._remaining(deadline))):
            data = read(self.identity)
            row = data["participants"][self.token]
            if row["input_epoch"] is None:
                return  # Passive links have no admitted input to retire.
            _retire(self.identity, data, row, deadline=deadline)


def _retire(identity, data, row, *, deadline):
    if row["receipt"] is None:
        result = retirement.barrier(identity, tuple_for(row, identity), deadline=deadline)
        if not retirement.receipt(result, tuple_for(row, identity)):
            raise retirement.RetirementUnknown("native retirement did not complete")
        row["receipt"] = result
        write(identity, data)


def inventory(identity, data, *, deadline, covered=()):
    """Exact live custody and non-reappearance, not absence-as-cleanup proof."""
    if any(row["phase"] != "identified" for row in data["participants"].values()):
        raise retirement.RetirementUnknown("partial participant inventory; no custody adoption")
    current = retirement.clients(identity, deadline=deadline)
    known = {row["client_id"]: row for row in data["participants"].values()}
    peers = {row["unix_peer"]: row for row in data["participants"].values()}
    for native in current:
        row = known.get(native["id"])
        if (row is None or peers.get(native.get("unix_peer")) is not row
                or type(native.get("peer_address_length")) is not int
                or native["peer_address_length"] != row["peer_address_length"]):
            raise retirement.RetirementUnknown("unknown/ambiguous native participant; no disconnect")
        if row["receipt"] is not None or row["token"] in covered:
            raise retirement.RetirementUnknown("retired participant reappeared in live inventory")
    return current


def reconcile(identity, *, deadline, covered=()):
    """Caller owns activity EX. Retire only registered old input links, never watchers.

    Completed receipts are immutable historical cleanup facts, not current
    authority. Fresh identity/capability/inventory, activity EX and the serialized
    lease/grant still gate every handoff. No record or original nonce is replaced.
    """
    with locked(identity):
        data = read(identity)
        inventory(identity, data, deadline=deadline, covered=covered)
        rows = list(data["participants"].values())
        retired = 0
        aggregate_covered = 0
        for row in rows:
            if row["input_epoch"] is None:
                continue  # Passive authenticated watcher keeps transport and frames.
            if row["token"] in covered:
                aggregate_covered += 1  # Historical receipt stays null/unknown.
            else:
                _retire(identity, data, row, deadline=deadline)
                retired += 1
        inventory(identity, data, deadline=deadline, covered=covered)
        retirement.capability(identity, deadline=deadline)
        return {"input_participants_retired": retired, "registered_participants": len(rows),
                "historical_input_participants_aggregate_covered": aggregate_covered,
                "receipt_scope": "same_connection_compositor_processing_only",
                "application_cleanup_certified": False, "active_owner_granted": False}
