#!/usr/bin/env python3
"""Install Golden's deterministic durable-drain source carrier."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

PAYLOAD_DIR = Path(__file__).resolve().parents[1] / "payloads" / "durable-drain-inbox-v1"
MANIFEST_PATH = PAYLOAD_DIR / "manifest.json"
MARKER_RELATIVE = Path(".golden-runtime-carriers/durable-drain-inbox-v1.json")
IDEMPOTENCY = "HERMES_DURABLE_DRAIN_INBOX_CARRIER_v1"
MULTIPLEX_TEST_MARKER = "HERMES_DURABLE_DRAIN_MULTIPLEX_TEST_COMPAT_v1"
MULTIPLEX_TEST_ANCHOR = '''    def set_topic_recovery_fn(self, handler):
        self.topic_recovery_fn = handler

    def set_authorization_check(self, handler):
'''
MULTIPLEX_TEST_REPLACEMENT = f'''    def set_topic_recovery_fn(self, handler):
        self.topic_recovery_fn = handler

    def set_startup_gate_handler(self, handler):
        # {MULTIPLEX_TEST_MARKER}
        self.startup_gate_handler = handler

    def set_authorization_check(self, handler):
'''
PLATFORM_RECONNECT_TEST_MARKER = "HERMES_DURABLE_DRAIN_CREATE_TASK_TEST_COMPAT_v1"
PLATFORM_RECONNECT_TEST_ANCHOR = '''        def fake_create_task(coro):
            coro.close()
            return MagicMock()
'''
PLATFORM_RECONNECT_TEST_REPLACEMENT = f'''        real_create_task = asyncio.create_task

        def fake_create_task(coro):
            # {PLATFORM_RECONNECT_TEST_MARKER}
            if getattr(getattr(coro, "cr_code", None), "co_name", None) == "to_thread":
                return real_create_task(coro)
            coro.close()
            return MagicMock()
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_oid(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
        usedforsecurity=False,
    ).hexdigest()


def _load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported durable-drain carrier manifest")
    payload = PAYLOAD_DIR / str(manifest.get("patch") or "")
    if not payload.is_file():
        raise RuntimeError("durable-drain carrier payload is missing")
    if _sha256(payload) != manifest.get("patch_sha256"):
        raise RuntimeError("durable-drain carrier payload checksum mismatch")
    postimages = manifest.get("postimage_git_blobs")
    if not isinstance(postimages, dict) or not postimages:
        raise RuntimeError("durable-drain carrier postimage manifest is empty")
    mutable = manifest.get("downstream_mutable_postimages", {})
    if (
        not isinstance(mutable, dict)
        or not set(mutable).issubset(postimages)
        or any(
            not isinstance(fragments, list)
            or not fragments
            or any(not isinstance(fragment, str) or not fragment for fragment in fragments)
            for fragments in mutable.values()
        )
    ):
        raise RuntimeError("durable-drain carrier mutable postimage manifest is invalid")
    return manifest


def _postimage_mismatches(root: Path, manifest: dict[str, Any]) -> list[str]:
    mismatches = []
    for relative, expected in manifest["postimage_git_blobs"].items():
        path = root / relative
        if not path.is_file() or _git_blob_oid(path) != expected:
            mismatches.append(relative)
    return mismatches


def _marked_install_mismatches(root: Path, manifest: dict[str, Any]) -> list[str]:
    mutable = manifest.get("downstream_mutable_postimages", {})
    mismatches = []
    for relative, expected in manifest["postimage_git_blobs"].items():
        path = root / relative
        if not path.is_file():
            mismatches.append(relative)
            continue
        fragments = mutable.get(relative)
        if fragments:
            content = path.read_text(encoding="utf-8")
            if any(fragment not in content for fragment in fragments):
                mismatches.append(relative)
        elif _git_blob_oid(path) != expected:
            mismatches.append(relative)
    return mismatches


def _repo_head(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _apply_payload(root: Path, payload: Path, *, check: bool) -> None:
    command = ["git", "apply", "--no-index", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    command.append(str(payload))
    result = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git apply failed").strip()
        raise RuntimeError(f"durable-drain carrier payload rejected: {detail}")


def _marker_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotency": IDEMPOTENCY,
        "base_commit": manifest["base_commit"],
        "source_seed_head": manifest["source_seed_head"],
        "payload_finalized_in_golden_commit": manifest["payload_finalized_in_golden_commit"],
        "patch_sha256": manifest["patch_sha256"],
        "downstream_mutable_postimages": manifest.get("downstream_mutable_postimages", {}),
    }


def _write_marker(root: Path, manifest: dict[str, Any]) -> None:
    marker = root / MARKER_RELATIVE
    marker.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(_marker_payload(manifest), indent=2, sort_keys=True) + "\n"
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, marker)


def patch_durable_drain_inbox_carrier_v1(root: Path) -> bool:
    """Apply the exact pin-rooted source delta before dependent Golden patches."""
    root = Path(root).resolve()
    manifest = _load_manifest()
    marker = root / MARKER_RELATIVE
    mismatches = _postimage_mismatches(root, manifest)

    if marker.exists():
        installed = json.loads(marker.read_text(encoding="utf-8"))
        if installed != _marker_payload(manifest):
            raise RuntimeError("durable-drain carrier marker provenance mismatch")
        marked_mismatches = _marked_install_mismatches(root, manifest)
        if marked_mismatches:
            raise RuntimeError(
                "durable-drain carrier marker exists but payload drifted: " + ", ".join(marked_mismatches)
            )
        return False

    if not mismatches:
        _write_marker(root, manifest)
        return True

    head = _repo_head(root)
    if head is not None and head != manifest["base_commit"]:
        raise RuntimeError(f"durable-drain carrier requires base {manifest['base_commit']}, got {head}")

    payload = PAYLOAD_DIR / manifest["patch"]
    _apply_payload(root, payload, check=True)
    _apply_payload(root, payload, check=False)
    mismatches = _postimage_mismatches(root, manifest)
    if mismatches:
        raise RuntimeError("durable-drain carrier postimage verification failed: " + ", ".join(mismatches))
    _write_marker(root, manifest)
    return True


def _patch_multiplex_test_fixture(root: Path) -> bool:
    """Keep the upstream fake adapter aligned with the assembled base contract."""
    path = root / "tests/gateway/test_multiplex_adapter_registry.py"
    if not path.is_file():
        return False
    source = path.read_text(encoding="utf-8")
    if MULTIPLEX_TEST_MARKER in source:
        return False
    if MULTIPLEX_TEST_ANCHOR not in source:
        raise RuntimeError("durable-drain multiplex test fixture anchor missing")
    path.write_text(
        source.replace(MULTIPLEX_TEST_ANCHOR, MULTIPLEX_TEST_REPLACEMENT, 1),
        encoding="utf-8",
    )
    return True


def _patch_platform_reconnect_test_fixture(root: Path) -> bool:
    """Let durable lease acquisition run through broad create-task test mocks."""
    path = root / "tests/gateway/test_platform_reconnect.py"
    if not path.is_file():
        return False
    source = path.read_text(encoding="utf-8")
    if PLATFORM_RECONNECT_TEST_MARKER in source:
        return False
    count = source.count(PLATFORM_RECONNECT_TEST_ANCHOR)
    if count != 2:
        raise RuntimeError(
            f"durable-drain platform reconnect test anchor count is {count}, expected 2"
        )
    path.write_text(
        source.replace(
            PLATFORM_RECONNECT_TEST_ANCHOR,
            PLATFORM_RECONNECT_TEST_REPLACEMENT,
        ),
        encoding="utf-8",
    )
    return True


def _load_sibling(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"durable-drain companion unavailable: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch_durable_drain_runtime_v1(root: Path) -> bool:
    """Apply the carrier and cron dispatch seam as one lifecycle subsystem."""
    changed = patch_durable_drain_inbox_carrier_v1(root)
    cron = _load_sibling("cron_scheduler_can_dispatch_compat_v1")
    changed = cron.patch_cron_scheduler_can_dispatch_compat_v1(root) or changed
    changed = _patch_multiplex_test_fixture(root) or changed
    changed = _patch_platform_reconnect_test_fixture(root) or changed
    return changed
