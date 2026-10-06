#!/usr/bin/env python3
"""Install the exact native CLI without npm scripts or global symlink changes."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[1] / "config/agent-browser-release-v1.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure(home, contract, *, dry_run=False, archive=None):
    home = Path(os.path.abspath(home.expanduser()))
    row = json.loads(contract.read_text())
    if row.get("version") != "0.38.2" or row.get("url") != "https://registry.npmjs.org/agent-browser/-/agent-browser-0.38.2.tgz":
        raise ValueError("Unsupported agent-browser release")
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine())
    if system == "win32":
        arch = "x64"
    if system == "linux" and Path("/etc/alpine-release").exists():
        system = "linux-musl"
    name = f"agent-browser-{system}-{arch}" + (".exe" if system == "win32" else "")
    expected = row["binaries"].get(name)
    if not expected:
        raise ValueError("Unsupported native CLI platform")
    destination = home / "tools/agent-browser-managed/0.38.2" / name
    receipt = home / "state/agent-browser-install-v1.json"
    for path in (destination, receipt):
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != Path("/")):
            raise ValueError("Managed native CLI paths must not contain symlinks")
    result = {"ok": True, "version": "0.38.2", "executable": str(destination), "sha256": expected}
    if not destination.is_symlink() and destination.is_file() and digest(destination.read_bytes()) == expected:
        result["status"] = "idempotent"
    elif dry_run:
        return {**result, "status": "would_install"}
    else:
        if archive:
            data = archive.read_bytes()
        else:
            with urllib.request.urlopen(row["url"], timeout=45) as response:
                data = response.read(100 * 1024 * 1024 + 1)
        if digest(data) != row["archive_sha256"]:
            raise ValueError("Native CLI archive checksum failed")
        with tarfile.open(fileobj=io.BytesIO(data)) as package:
            member = package.getmember("package/bin/" + name)
            if not member.isfile() or member.size > 80 * 1024 * 1024:
                raise ValueError("Invalid native CLI artifact")
            binary = package.extractfile(member).read()
        if digest(binary) != expected:
            raise ValueError("Native CLI binary checksum failed")
        atomic(destination, binary, 0o700)
        result["status"] = "installed"
    version = subprocess.run([str(destination), "--version"], capture_output=True, text=True, timeout=10)
    if version.returncode or "0.38.2" not in version.stdout:
        raise ValueError("Native CLI version verification failed")
    if not dry_run:
        atomic(receipt, (json.dumps(result, indent=2) + "\n").encode())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--hermes-python")  # Same existing host-artifact execution contract.
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(ensure(args.hermes_home, args.contract, dry_run=args.dry_run, archive=args.archive)))
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": str(exc)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
