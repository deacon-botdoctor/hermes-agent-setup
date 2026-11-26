"""One bounded pinned native carrier; optional external mode stays dormant.

All before-images and payloads are preflighted and compiled before the first
write. Any partial/mixed generation refuses. Golden's disposable assembly and
pre-apply snapshot own crash rollback; this function never edits a live profile.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import stat
import subprocess

UPSTREAM = "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"
MARKER = "HERMES_NATIVE_EXTERNAL_SCREEN_v1"


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _safe(root, relative):
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts or str(path) != relative:
        raise RuntimeError("invalid native external payload path")
    target = root / path
    for part in (target, *target.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise RuntimeError("native external carrier refuses symlink target/ancestor")
    return target


def patch_native_external_screen_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir).resolve(strict=True)
    actual = subprocess.check_output(["git", "--no-replace-objects", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual != UPSTREAM:
        raise RuntimeError("native external Screen requires exact pinned upstream; no dirty install source")
    payload = Path(__file__).parent.parent / "payloads/native-external-screen-v1"
    manifest = json.loads((payload / "inventory.json").read_text())
    if manifest.get("upstream") != UPSTREAM or manifest.get("schema") != 1:
        raise RuntimeError("native external carrier inventory pin/schema mismatch")
    edits_bytes = (payload / "edits.json").read_bytes()
    if _hash(edits_bytes) != manifest["edits_sha256"]:
        raise RuntimeError("native external exact-seam payload digest mismatch")
    edits = json.loads(edits_bytes)
    states, prepared = [], []
    for relative, entry in manifest["files"].items():
        target = _safe(root, relative)
        if target.exists() and not stat.S_ISREG(target.lstat().st_mode):
            raise RuntimeError("native external carrier target is not a regular file")
        current = target.read_bytes() if target.exists() else None
        digest = _hash(current) if current is not None else None
        mode = stat.S_IMODE(target.stat().st_mode) if current is not None else None
        if digest == entry["after"] and mode == entry["mode"]:
            states.append("after")
            continue
        if digest != entry["before"] or (current is not None and mode != entry["mode"]):
            raise RuntimeError(f"native external before-image drift or conflicting generation: {relative}")
        states.append("before")
        if entry["before"] is None:
            source = _safe(payload / "new", relative)
            updated = source.read_bytes()
        else:
            lines = current.decode("utf-8").splitlines(keepends=True)
            # Reverse pinned line-range edits preserve earlier offsets. Before
            # segments must match as well as the full native file digest.
            for edit in reversed(edits[relative]):
                if lines[edit["start"]:edit["end"]] != edit["before"]:
                    raise RuntimeError(f"native external exact seam drift: {relative}")
                lines[edit["start"]:edit["end"]] = edit["after"]
            updated = "".join(lines).encode()
        if _hash(updated) != entry["after"]:
            raise RuntimeError(f"native external payload digest mismatch: {relative}")
        if relative.endswith(".py"):
            compile(updated, str(target), "exec")
        elif relative.endswith(".json"):
            json.loads(updated)
        prepared.append((target, updated, entry["mode"]))
    if states and all(state == "after" for state in states):
        return False
    if not states or any(state == "after" for state in states):
        raise RuntimeError("partial native external Screen application; restore assembly snapshot")
    # No write occurs until EVERY target's before/payload/compile check passed.
    for target, data, mode in prepared:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        target.chmod(mode)
    return True
