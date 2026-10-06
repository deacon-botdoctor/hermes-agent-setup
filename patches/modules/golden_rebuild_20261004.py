"""Apply the reviewed, client-safe rebuild after the registered Golden patches."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

_PAYLOAD = Path(__file__).resolve().parents[1] / "payloads/golden-rebuild-20261004"
_MARKER = Path(".golden-runtime-carriers/golden-rebuild-20261004.json")
_IDEMPOTENCY = "HERMES_GOLDEN_REBUILD_20261004"


def _identity(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"rebuild source is not a regular file: {path.name}")
    mode = path.stat().st_mode & 0o777
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": mode, "git_mode": "100755" if mode & 0o111 else "100644"}


def patch_golden_rebuild_20261004(root: Path) -> bool:
    """Check all preimages before mutation; restore them if application fails."""
    root = Path(root).resolve()
    manifest = json.loads((_PAYLOAD / "manifest.json").read_text(encoding="utf-8"))
    patch = _PAYLOAD / "native.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise RuntimeError("rebuild payload integrity mismatch")
    for args, expected in ((["--absolute-git-dir"], str(root / ".git")),
                           (["HEAD"], manifest["upstream_commit"])):
        result = subprocess.run(["git", "rev-parse", *args], cwd=root,
                                capture_output=True, text=True, check=True)
        if result.stdout.strip() != expected:
            raise RuntimeError("rebuild requires its pinned standalone upstream tree")
    paths = {}
    for relative in manifest["files"]:
        path = root / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.resolve() != path):
            raise RuntimeError(f"unsafe rebuild source: {relative}")
        paths[relative] = path
    identities = {name: _identity(path) for name, path in paths.items()}
    marker = root / _MARKER
    receipt = {"idempotency": _IDEMPOTENCY, "patch_sha256": manifest["patch_sha256"]}
    if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) != receipt:
        raise RuntimeError("rebuild carrier receipt drift")
    if all(identities[name] == record["after"] for name, record in manifest["files"].items()):
        if marker.exists():
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        return True
    if marker.exists() or any(identities[name] != record["before"]
                             for name, record in manifest["files"].items()):
        raise RuntimeError("rebuild source drift or partial installation")
    subprocess.run(["git", "apply", "--no-index", "--whitespace=nowarn", "--check", str(patch)],
                   cwd=root, capture_output=True, check=True)
    originals = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    try:
        subprocess.run(["git", "apply", "--no-index", "--whitespace=nowarn", str(patch)],
                       cwd=root, capture_output=True, check=True)
        for name, path in paths.items():
            if _identity(path) != manifest["files"][name]["after"]:
                raise RuntimeError(f"rebuild postimage mismatch: {name}")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        for name, path in paths.items():
            if originals[name] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(originals[name])
                os.chmod(path, manifest["files"][name]["before"]["mode"])
        marker.unlink(missing_ok=True)
        raise
    return True
