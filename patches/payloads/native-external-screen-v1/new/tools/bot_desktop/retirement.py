"""Bounded client of WayVNC's owner-private native retirement IPC, not authority."""
from __future__ import annotations

import json
import re
import secrets
import socket
import time

from . import external

QUERY_SECONDS = 1.0
BARRIER_SECONDS = 4.0
MAX_REPLY = 65536
METHODS = frozenset({"handoff-status", "client-list", "client-retire", "retirement-status",
                     "baseline-capabilities", "baseline-begin", "baseline-status", "baseline-release"})
BOOTSTRAP_BLOCKER = (
    "managed-baseline-unproved: native retirement certifies only exact retained "
    "participants, not a complete clean managed baseline since generation origin. "
    "External Screen remains excluded; empty clients, a probe, force, historical "
    "receipts and file deletion cannot initialize active ownership."
)


class RetirementUnknown(external.ExternalScreenError):
    code = "native_retirement_unknown"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RetirementUnknown("duplicate native JSON field")
        result[key] = value
    return result


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise RetirementUnknown("native control total deadline exceeded")
    return left


def request(identity, method, params=None, *, deadline=None):
    """One native JSON request per connection; never CLI flat-output parsing/RFB."""
    if method not in METHODS:
        raise RetirementUnknown("unsupported native control method")
    if method in {"client-retire", "retirement-status"}:
        target(params)
    elif method in {"baseline-begin", "baseline-status", "baseline-release"}:
        from . import baseline
        baseline.target(params)
        if params["incarnation"] != identity.native_incarnation:
            raise RetirementUnknown("aggregate request incarnation mismatch")
    elif params is not None:
        raise RetirementUnknown("native discovery takes no parameters")
    deadline = min(deadline or time.monotonic() + QUERY_SECONDS,
                   time.monotonic() + QUERY_SECONDS)
    identity.validate()
    correlation = secrets.token_hex(8)
    payload = {"method": method, "id": correlation}
    if params is not None:
        payload["params"] = params
    raw = json.dumps(payload, separators=(",", ":")).encode("ascii")
    if len(raw) > 512:
        raise RetirementUnknown("native request exceeds producer bound")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.set_inheritable(False)
            channel.settimeout(_remaining(deadline))
            channel.connect(identity.control_endpoint)
            identity.validate()
            channel.settimeout(_remaining(deadline))
            channel.sendall(raw)
            response = bytearray()
            while True:
                channel.settimeout(_remaining(deadline))
                chunk = channel.recv(min(4096, MAX_REPLY + 1 - len(response)))
                if not chunk:
                    raise RetirementUnknown("native control EOF before complete reply")
                response.extend(chunk)
                if len(response) > MAX_REPLY:
                    raise RetirementUnknown("native control reply exceeds bound")
                try:
                    text = response.decode("utf-8")
                    reply, end = json.JSONDecoder(object_pairs_hook=_unique).raw_decode(text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue  # Total deadline is not reset by fragments.
                if text[end:].strip():
                    raise RetirementUnknown("native control trailing reply data")
                break
        identity.validate()
        if (not isinstance(reply, dict) or set(reply) != {"code", "data", "id"}
                or reply["id"] != correlation or type(reply["code"]) is not int):
            raise RetirementUnknown("invalid native IPC envelope/correlation")
        if reply["code"] != 0:
            detail = reply["data"]
            raise RetirementUnknown(f"native control refused ({reply['code']}): {str(detail)[:512]}")
        return reply["data"]
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise RetirementUnknown(f"native control uncertain: {exc}") from exc


def capability(identity, *, deadline=None):
    data = request(identity, "handoff-status", deadline=deadline)
    expected = {"protocol": 1, "available": True,
                "incarnation": identity.native_incarnation, "record_limit": 64,
                "pending_timeout_ms": 2000, "terminal_retention_ms": 60000,
                "peer_encoding": "sockaddr-un-sun-path-hex"}
    if (data != expected or not isinstance(data, dict)
            or type(data.get("protocol")) is not int or data.get("available") is not True
            or any(type(data.get(k)) is not int for k in
                   ("record_limit", "pending_timeout_ms", "terminal_retention_ms"))):
        raise RetirementUnknown("native retirement unavailable/unsupported or incarnation changed")
    return data


def clients(identity, *, deadline):
    capability(identity, deadline=deadline)
    data = request(identity, "client-list", deadline=deadline)
    capability(identity, deadline=deadline)
    if not isinstance(data, list) or len(data) > 64:
        raise RetirementUnknown("native client inventory unavailable/over bound")
    seen = set()
    for row in data:
        if not isinstance(row, dict) or not valid_id(row.get("id")) or row["id"] in seen:
            raise RetirementUnknown("invalid/duplicate native client identity")
        seen.add(row["id"])
    return data


def valid_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value) is not None
            and int(value) <= 2147483647)


def target(value):
    if (not isinstance(value, dict) or set(value) != {"id", "peer", "incarnation", "nonce"}
            or not valid_id(value["id"])
            or not isinstance(value["peer"], str)
            or re.fullmatch(r"(?:[0-9a-f]{2}){1,108}", value["peer"]) is None
            or not isinstance(value["incarnation"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["incarnation"]) is None
            or not isinstance(value["nonce"], str)
            or re.fullmatch(r"(?:[0-9a-f]{2}){16,32}", value["nonce"]) is None):
        raise RetirementUnknown("invalid exact native retirement tuple")
    return value


def receipt(value, expected):
    target(expected)
    fields = {"protocol", "id", "peer", "incarnation", "nonce", "state", "reason",
              "transport_retired", "keyboard_present", "pointer_present",
              "keyboard_destroy_issued", "pointer_destroy_issued",
              "device_destroy_requests_issued", "same_connection_sync_observed",
              "application_cleanup_certified", "active_owner_granted"}
    bools = fields - {"protocol", "id", "peer", "incarnation", "nonce", "state", "reason"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["protocol"]) is not int or value["protocol"] != 1
            or any(value[k] != v for k, v in expected.items())
            or any(type(value[k]) is not bool for k in bools)
            or value["state"] not in {"pending", "failed", "compositor_processed"}
            or value["application_cleanup_certified"] is not False
            or value["active_owner_granted"] is not False):
        raise RetirementUnknown("malformed/mismatched native retirement receipt")
    if value["state"] == "failed":
        raise RetirementUnknown(f"native retirement failed: {value['reason']}")
    if value["reason"] is not None:
        raise RetirementUnknown("native nonfailure receipt has a failure reason")
    if value["state"] == "compositor_processed":
        if (not all(value[k] for k in ("transport_retired", "device_destroy_requests_issued",
                                      "same_connection_sync_observed"))
                or value["keyboard_destroy_issued"] != value["keyboard_present"]
                or value["pointer_destroy_issued"] != value["pointer_present"]):
            raise RetirementUnknown("incomplete native post-destroy tuple")
        return True
    if value["same_connection_sync_observed"]:
        raise RetirementUnknown("pending native receipt claims a completed barrier")
    return False


def barrier(identity, expected, *, deadline):
    """Bind retained spontaneous tombstones too. Same nonce for every retry."""
    target(expected)
    capability(identity, deadline=deadline)
    data = request(identity, "client-retire", expected, deadline=deadline)
    while True:
        complete = receipt(data, expected)
        capability(identity, deadline=deadline)
        if complete:
            return data
        time.sleep(min(external.POLL_SECONDS, _remaining(deadline)))
        data = request(identity, "retirement-status", expected, deadline=deadline)
