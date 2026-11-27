#!/usr/bin/env python3
"""Install and restore the pinned Printing Press engine inside one Hermes profile."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path

VERSION = "4.33.4+botdoctor.3"
CONTRACT = Path(__file__).resolve().parents[1] / "config/printing-press-release-v1.json"
MAX_BYTES = 150 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"Managed path contains a symlink: {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Managed destination is not a regular file: {path}")


def atomic(path, data, mode=0o600):
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def targets(home):
    name = "cli-printing-press" + (".exe" if platform.system() == "Windows" else "")
    return {
        "binary": home / "bin" / name,
        "license": home / "bin/printing-press.LICENSE",
        "receipt": home / "state/printing-press-install-v1.json",
    }


def snapshot(paths, backup):
    before = {}
    for key, path in paths.items():
        safe_path(path)
        if path.exists():
            data = path.read_bytes()
            before[key] = {"sha256": digest(data), "mode": path.stat().st_mode & 0o777}
            atomic(backup / key, data)
        else:
            before[key] = None
    return before


def restore_files(paths, backup, before):
    # Validate every saved preimage before restoring any destination.
    saved = {}
    for key, path in paths.items():
        safe_path(path)
        if before[key] is not None:
            safe_path(backup / key)
            data = (backup / key).read_bytes()
            if digest(data) != before[key]["sha256"]:
                raise ValueError(f"Rollback preimage checksum failed: {key}")
            saved[key] = data
    for key, path in paths.items():
        if before[key] is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, saved[key], before[key]["mode"])


def restore(home, backup):
    backup = Path(os.path.abspath(backup.expanduser()))
    expected_parent = home / "state/printing-press-backups"
    if backup.parent != expected_parent or not re.fullmatch(r"[0-9a-f]{32}", backup.name):
        raise ValueError("Rollback must name this profile's exact Printing Press backup")
    safe_path(backup / "rollback.json")
    record = json.loads((backup / "rollback.json").read_text())
    paths = targets(home)
    if record.get("home") != str(home) or set(record.get("before", {})) != set(paths):
        raise ValueError("Rollback profile or target set mismatch")
    for key, path in paths.items():
        safe_path(path)
        current = digest(path.read_bytes()) if path.is_file() else None
        before = record["before"][key]
        prior = before["sha256"] if before is not None else None
        if current not in {prior, record["after"][key]}:
            raise ValueError(f"Refusing to overwrite changed Printing Press file: {key}")
    restore_files(paths, backup, record["before"])
    return {"ok": True, "status": "restored", "backup": str(backup), "home": str(home)}


def platform_key():
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "amd64", "x86_64": "amd64"}.get(platform.machine().lower())
    if not system or not arch:
        raise ValueError("Unsupported Printing Press platform")
    return f"{system}_{arch}"


def verify_binary(path):
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=20)
    if result.returncode or not re.search(r"(?<![\w.+-])" + re.escape(VERSION) + r"(?![\w.+-])", result.stdout):
        raise ValueError("Printing Press engine version verification failed")


def archive_members(data, windows):
    binary = "cli-printing-press.exe" if windows else "cli-printing-press"
    result = {}
    if windows:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for key, name in (("binary", binary), ("license", "LICENSE")):
                member = archive.getinfo(name)
                if member.is_dir() or member.file_size > MAX_BYTES:
                    raise ValueError("Invalid Printing Press archive member")
                result[key] = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for key, name in (("binary", binary), ("license", "LICENSE")):
                member = archive.getmember(name)
                if not member.isfile() or member.size > MAX_BYTES:
                    raise ValueError("Invalid Printing Press archive member")
                result[key] = archive.extractfile(member).read(MAX_BYTES + 1)
    return result


def ensure(home, contract, *, dry_run=False, archive=None):
    contract_bytes = contract.read_bytes()
    release = json.loads(contract_bytes)
    if release.get("schema_version") != 1 or release.get("version") != VERSION:
        raise ValueError("Unsupported Printing Press release contract")
    key = platform_key()
    asset = release["assets"][key]
    suffix = ".zip" if key.startswith("windows_") else ".tar.gz"
    if release.get("distribution") != "staged":
        raise ValueError("Printing Press requires a staged release")
    for field in ("archive_sha256", "binary_sha256", "license_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", asset[field]):
            raise ValueError("Invalid Printing Press checksum")
    paths = targets(home)
    for path in paths.values():
        safe_path(path)
    result = {"ok": True, "version": VERSION, "platform": key,
              "executable": str(paths["binary"]), "sha256": asset["binary_sha256"],
              "contract_sha256": digest(contract_bytes)}
    matched = all(path.is_file() and digest(path.read_bytes()) == asset[key + "_sha256"]
                  for key, path in paths.items() if key != "receipt")
    matched = matched and os.access(paths["binary"], os.X_OK)
    if matched and paths["receipt"].is_file():
        existing = json.loads(paths["receipt"].read_text())
        if all(existing.get(key) == value for key, value in result.items()):
            verify_binary(paths["binary"])
            return {**existing, "status": "idempotent"}
    payload = {}
    if not matched:
        archive = archive or contract.resolve().parents[1] / "artifacts/printing-press" / VERSION / ("cli-printing-press_" + key + suffix)
        safe_path(archive)
        with archive.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES or digest(data) != asset["archive_sha256"]:
            raise ValueError("Printing Press archive checksum failed")
        payload = archive_members(data, key.startswith("windows_"))
        for name, data in payload.items():
            if digest(data) != asset[name + "_sha256"]:
                raise ValueError(f"Printing Press {name} checksum failed")
    if dry_run:
        return {**result, "status": "would_install"}
    backup = home / "state/printing-press-backups" / uuid.uuid4().hex
    before = snapshot(paths, backup)
    result.update(status="installed", backup=str(backup))
    receipt_bytes = (json.dumps(result, indent=2) + "\n").encode()
    record = {"home": str(home), "before": before, "after": {
        "binary": asset["binary_sha256"], "license": asset["license_sha256"],
        "receipt": digest(receipt_bytes),
    }}
    # Persist recovery before the first destination mutation, including interruption.
    atomic(backup / "rollback.json", (json.dumps(record, indent=2) + "\n").encode())
    try:
        for name, data in payload.items():
            atomic(paths[name], data, 0o700 if name == "binary" else 0o644)
        verify_binary(paths["binary"])
        atomic(paths["receipt"], receipt_bytes)
    except Exception:
        restore_files(paths, backup, before)
        raise
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--hermes-python")  # Existing host-artifact helper contract.
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore-backup", type=Path)
    args = parser.parse_args()
    home = Path(os.path.abspath(args.hermes_home.expanduser()))
    try:
        if args.restore_backup and (args.dry_run or args.archive):
            raise ValueError("Rollback cannot be combined with installation options")
        result = restore(home, args.restore_backup) if args.restore_backup else ensure(
            home, args.contract, dry_run=args.dry_run, archive=args.archive)
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": str(exc)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
