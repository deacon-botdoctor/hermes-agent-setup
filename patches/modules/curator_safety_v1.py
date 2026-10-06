"""Carry the reviewed curator safety backport on the pinned upstream source."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

_PAYLOAD = Path(__file__).resolve().parents[1] / "payloads/curator-safety-v1"
_MARKER = Path(".golden-runtime-carriers/curator-safety-v1.json")
_IDEMPOTENCY = "HERMES_CURATOR_SAFETY_v1"


def _identity(path: Path) -> dict:
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": path.stat().st_mode & 0o777}


def patch_curator_safety_v1(root: Path) -> bool:
    """Apply exact preimages once; reject drift before writing any source file."""
    root = Path(root).resolve()
    manifest = json.loads((_PAYLOAD / "manifest.json").read_text())
    patch = _PAYLOAD / "native.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise RuntimeError("curator safety payload integrity mismatch")
    for args, expected in ((["--absolute-git-dir"], str(root / ".git")),
                           (["HEAD"], manifest["upstream_commit"])):
        result = subprocess.run(["git", "rev-parse", *args], cwd=root,
                                capture_output=True, text=True, check=True)
        if result.stdout.strip() != expected:
            raise RuntimeError("curator safety requires its pinned standalone upstream tree")
    paths = {}
    for relative in manifest["files"]:
        path = root / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.resolve() != path or not path.is_file()):
            raise RuntimeError(f"curator safety source is missing or unsafe: {relative}")
        paths[relative] = path
    identities = {relative: _identity(path) for relative, path in paths.items()}
    marker = root / _MARKER
    receipt = {"idempotency": _IDEMPOTENCY, "patch_sha256": manifest["patch_sha256"]}
    if marker.exists() and json.loads(marker.read_text()) != receipt:
        raise RuntimeError("curator safety carrier receipt drift")
    if all(identities[name] == record["after"] for name, record in manifest["files"].items()):
        if marker.exists():
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        return True
    if marker.exists() or any(identities[name] != record["before"] for name, record in manifest["files"].items()):
        raise RuntimeError("curator safety source drift or partial installation")
    subprocess.run(["git", "apply", "--no-index", "--whitespace=nowarn", "--check", str(patch)], cwd=root,
                   capture_output=True, check=True)
    originals = {name: path.read_bytes() for name, path in paths.items()}
    try:
        subprocess.run(["git", "apply", "--no-index", "--whitespace=nowarn", str(patch)], cwd=root,
                       capture_output=True, check=True)
        for name, path in paths.items():
            if _identity(path) != manifest["files"][name]["after"]:
                raise RuntimeError(f"curator safety postimage mismatch: {name}")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    except Exception:
        for name, path in paths.items():
            path.write_bytes(originals[name])
            os.chmod(path, manifest["files"][name]["before"]["mode"])
        marker.unlink(missing_ok=True)
        raise
    return True
