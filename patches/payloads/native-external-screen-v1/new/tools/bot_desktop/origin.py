"""Read Operator's privileged generation-origin evidence; never manufacture it.

Root custody is the trust boundary, not a JSON claim from a lease caller. The
Operator must establish the origin and producer controls before issuing evidence.
This consumer cannot certify application consumption or sandbox malicious same UID.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import external, retirement

ROOT = Path("/etc/hermes-screen-origins")


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _read(path):
    # Every component must be root-owned and non-writable to an unprivileged
    # producer. NOFOLLOW on the leaf alone would not protect a user-owned parent.
    for part in [*reversed(path.parent.parents), path.parent]:
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise retirement.RetirementUnknown("Operator origin directory custody unavailable")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or stat.S_IMODE(before.st_mode) != 0o644 or before.st_nlink != 1):
            raise retirement.RetirementUnknown("origin evidence must be root-owned single-link 0644")
        raw = os.read(fd, 16385)
        after = os.fstat(fd)
        stable = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode,
                               info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (len(raw) > 16384 or stable(before) != stable(after)
                or stable(after) != stable(path.lstat())):
            raise retirement.RetirementUnknown("origin evidence changed/over bound")
        data = json.loads(raw, object_pairs_hook=retirement._unique)
        return data, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def evidence(identity):
    """Fixed supported producer interface documented in the provider README.

    No path/config/caller override. Missing or revoked evidence always excludes.
    The caller does not gain root authority by supplying a schema-shaped record.
    """
    try:
        if os.geteuid() == 0:
            raise retirement.RetirementUnknown("root runtime collapses Operator origin trust boundary")
        data, digest = _read(ROOT / (identity.fingerprint + ".json"))
        fields = {"schema", "issuer", "identity", "boot_id", "wayland_generation",
                  "generation_creation_evidence_sha256", "native_bundle_sha256",
                  "managed_ingress", "other_input_producers"}
        if (not isinstance(data, dict) or set(data) != fields
                or type(data["schema"]) is not int or data["schema"] != 1
                or data["issuer"] != "Operator" or data["identity"] != identity.as_dict()
                or data["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                or not _digest(data["generation_creation_evidence_sha256"])
                or not _digest(data["native_bundle_sha256"])):
            raise retirement.RetirementUnknown("Operator origin identity/boot/provenance mismatch")
        external.short_string(data["wayland_generation"], "genuine Wayland generation")
        ingress = data["managed_ingress"]
        if (not isinstance(ingress, dict) or set(ingress) != {"policy", "evidence_sha256"}
                or ingress["policy"] != "s2-shared-only-since-generation-origin"
                or not _digest(ingress["evidence_sha256"])):
            raise retirement.RetirementUnknown("shared-only generation-origin ingress unproved")
        producers = data["other_input_producers"]
        if not isinstance(producers, list) or not 1 <= len(producers) <= 32:
            raise retirement.RetirementUnknown("non-C input producer inventory absent/over bound")
        names = set()
        for producer in producers:
            if (not isinstance(producer, dict) or set(producer) != {"name", "disposition", "evidence_sha256"}
                    or producer["disposition"] != "disabled-since-generation-origin"
                    or not _digest(producer["evidence_sha256"])):
                raise retirement.RetirementUnknown("non-C producer origin exclusion unproved")
            name = external.short_string(producer["name"], "input producer")
            if name in names:
                raise retirement.RetirementUnknown("duplicate non-C input producer")
            names.add(name)
        if not {"Cua", "all-other-non-C-input"} <= names:
            raise retirement.RetirementUnknown("Cua and remaining non-C inputs require separate origin evidence")
        identity.validate()
        return digest
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise retirement.RetirementUnknown(f"Operator origin evidence unavailable: {exc}") from exc
