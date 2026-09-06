#!/usr/bin/env python3
"""Install Golden's deterministic durable-drain source carrier."""

from __future__ import annotations

import ast
import copy
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
MULTIPLEX_TEST_ANCHOR = """    def set_topic_recovery_fn(self, handler):
        self.topic_recovery_fn = handler

    def set_authorization_check(self, handler):
"""
MULTIPLEX_TEST_REPLACEMENT = f"""    def set_topic_recovery_fn(self, handler):
        self.topic_recovery_fn = handler

    def set_startup_gate_handler(self, handler):
        # {MULTIPLEX_TEST_MARKER}
        self.startup_gate_handler = handler

    def set_authorization_check(self, handler):
"""
PLATFORM_RECONNECT_TEST_MARKER = "HERMES_DURABLE_DRAIN_CREATE_TASK_TEST_COMPAT_v1"
PLATFORM_RECONNECT_TEST_ANCHOR = """        def fake_create_task(coro):
            coro.close()
            return MagicMock()
"""
PLATFORM_RECONNECT_TEST_REPLACEMENT = f"""        real_create_task = asyncio.create_task

        def fake_create_task(coro):
            # {PLATFORM_RECONNECT_TEST_MARKER}
            if getattr(getattr(coro, "cr_code", None), "co_name", None) == "to_thread":
                return real_create_task(coro)
            coro.close()
            return MagicMock()
"""
RAFT_ADAPTER_RELATIVE = Path("plugins/platforms/raft/adapter.py")
RAFT_WAKE_EVENT_ANCHOR = """            raw_message=payload,
            message_id=delivery_id,
            internal=True,
        )
"""
RAFT_WAKE_EVENT_REPLACEMENT = """            raw_message=payload,
            message_id=delivery_id,
            internal=True,
            durable_ingress=True,
            retry_transport_on_admission_failure=True,
        )
"""
RAFT_HANDLE_MESSAGE_ANCHOR = '''    async def handle_message(self, event: MessageEvent) -> None:
        """Accept Raft wake hints without interrupting an active Hermes turn."""
        if not self._message_handler:
            return

        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            profile=self._session_key_profile(event.source),
        )

        if session_key in self._active_sessions:
            logger.debug("[raft] Wake queued for busy session %s", session_key)
            merge_pending_message_event(self._pending_messages, session_key, event)
            return

        await super().handle_message(event)
'''
RAFT_HANDLE_MESSAGE_REPLACEMENT = '''    async def handle_message(self, event: MessageEvent) -> Optional[asyncio.Task]:
        """Accept Raft wake hints without interrupting an active Hermes turn."""
        if not self._message_handler:
            return None

        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            profile=self._session_key_profile(event.source),
        )

        if session_key in self._active_sessions:
            logger.debug("[raft] Wake queued for busy session %s", session_key)
            merge_pending_message_event(self._pending_messages, session_key, event)
            return None

        return await super().handle_message(event)
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
    if manifest.get("schema_version") != 2:
        raise RuntimeError("unsupported durable-drain carrier manifest")
    for field in (
        "base_commit",
        "source_seed_head",
        "reviewed_source_head",
        "payload_finalized_against_golden_parent",
    ):
        value = manifest.get(field)
        if not isinstance(value, str) or len(value) != 40 or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise RuntimeError(f"durable-drain carrier {field} is invalid")
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
    exact_counts = manifest.get("downstream_exact_fragment_counts", {})
    if (
        not isinstance(exact_counts, dict)
        or not set(exact_counts).issubset(mutable)
        or any(
            not isinstance(counts, dict)
            or not counts
            or any(
                not isinstance(fragment, str)
                or not fragment
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 1
                for fragment, count in counts.items()
            )
            for counts in exact_counts.values()
        )
    ):
        raise RuntimeError("durable-drain carrier fragment-count manifest is invalid")
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
    exact_counts = manifest.get("downstream_exact_fragment_counts", {})
    mismatches = []
    for relative, expected in manifest["postimage_git_blobs"].items():
        path = root / relative
        if not path.is_file():
            mismatches.append(relative)
            continue
        fragments = mutable.get(relative)
        if fragments:
            content = path.read_text(encoding="utf-8")
            if any(fragment not in content for fragment in fragments) or any(
                content.count(fragment) != expected_count
                for fragment, expected_count in exact_counts.get(relative, {}).items()
            ):
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
        "reviewed_source_head": manifest["reviewed_source_head"],
        "payload_finalized_against_golden_parent": manifest[
            "payload_finalized_against_golden_parent"
        ],
        "patch_sha256": manifest["patch_sha256"],
        "downstream_mutable_postimages": manifest.get("downstream_mutable_postimages", {}),
        "downstream_exact_fragment_counts": manifest.get(
            "downstream_exact_fragment_counts", {}
        ),
    }


def _write_marker(root: Path, manifest: dict[str, Any]) -> None:
    marker = root / MARKER_RELATIVE
    marker.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(_marker_payload(manifest), indent=2, sort_keys=True) + "\n"
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, marker)


NATIVE_BASE_COMMIT = "d3630f853239e8c41ce7201e09fbdf39bcbc5431"
NATIVE_PAYLOAD_DIR = PAYLOAD_DIR.with_name("durable-drain-inbox-d363-v1")


_SLASH_OLD = '    control_command = str(event.text or "").lstrip().startswith("/")'
_SLASH_NEW = '    control_command = event.is_command()'
_PRE_SLASH_LEGACY_PATCH = '8de297b57ef15538e676d09bc624e53daa3b2d2c573240344cb73e3520b67984'
_PRE_SLASH_LEGACY_BLOB = '1130d0d887259ffd16ff44fd7c5cebfbc693d3b5'
_PRE_SLASH_NATIVE_SHA = 'f27e83b73e9cca022ae11a1608fe4bee22cd9b3eb90ebd394ee9098880ff87d2'

_ROLLBACK_OLD = '                except _PostReplaceError:\n                    try:\n                        _replace_rows(path, claimed_rows)\n                    except Exception:\n                        return queue_id, final_state\n'
_ROLLBACK_NEW = '                except _PostReplaceError:\n                    try:\n                        _replace_rows(path, claimed_rows)\n                    except Exception:\n                        return queue_id, _CLAIMED\n'
_PRE_ROLLBACK_LEGACY_PATCH = "f2d872c62be352f587f19abe8167763726db2a8220187f19821d74dda6d8c5ff"
_PRE_ROLLBACK_LEGACY_BLOB = "23b7e540f367f76bb7393ab9404249def33562e8"

def _patch_native_durable_drain(root: Path) -> bool:
    """Install only the residual durable mailbox at the exact native phase-owner pin.

    Both clean application and repeat calls verify full source content. A carrier
    marker is never evidence of compatibility with this independent source line.
    """
    if _repo_head(root) != NATIVE_BASE_COMMIT:
        raise RuntimeError("native durable drain requires exact d363 source HEAD")
    manifest = json.loads((NATIVE_PAYLOAD_DIR / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("base_commit") != NATIVE_BASE_COMMIT:
        raise RuntimeError("native durable drain manifest provenance mismatch")
    preimages = manifest.get("preimage_sha256")
    postimages = manifest.get("postimage_sha256")
    if not isinstance(preimages, dict) or not preimages or not isinstance(postimages, dict) or set(preimages) != set(postimages):
        raise RuntimeError("native durable drain source image manifest is invalid")
    for relative in preimages:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("native durable drain source path is invalid")
    patch_name = manifest.get("patch")
    if not isinstance(patch_name, str) or Path(patch_name).name != patch_name:
        raise RuntimeError("native durable drain payload name is invalid")
    payload = NATIVE_PAYLOAD_DIR / patch_name
    if _sha256(payload) != manifest.get("patch_sha256"):
        raise RuntimeError("native durable drain payload checksum mismatch")

    def matches(images):
        return all(
            (not (root / relative).exists()) if expected is None
            else (root / relative).is_file() and _sha256(root / relative) == expected
            for relative, expected in images.items()
        )

    variants = manifest.get("postimage_sha256_variants", [])
    if not isinstance(variants, list) or any(not isinstance(v, dict) or set(v) != set(postimages) for v in variants):
        raise RuntimeError("native durable drain composed source images are invalid")
    if matches(postimages) or any(matches(variant) for variant in variants):
        return False
    for current in [postimages, *variants]:
        previous = dict(current)
        if "gateway/drain_inbox.py" not in previous:
            continue
        previous["gateway/drain_inbox.py"] = _PRE_SLASH_NATIVE_SHA
        if matches(previous):
            target = root / "gateway/drain_inbox.py"
            content = target.read_text()
            if content.count(_SLASH_OLD) != 1:
                raise RuntimeError("native slash admission preimage drift")
            updated = content.replace(_SLASH_OLD, _SLASH_NEW, 1).replace(_ROLLBACK_OLD, _ROLLBACK_NEW, 1)
            if hashlib.sha256(updated.encode()).hexdigest() != current["gateway/drain_inbox.py"]:
                raise RuntimeError("native slash admission postimage drift")
            target.write_text(updated)
            return True
    if not matches(preimages):
        raise RuntimeError("native durable drain pre/post source content mismatch")
    _apply_payload(root, payload, check=True)
    _apply_payload(root, payload, check=False)
    if not matches(postimages):
        raise RuntimeError("native durable drain postimage verification failed")
    return True


def _upgrade_whatsapp_reservation(root: Path) -> None:
    """Upgrade only the exact installed legacy dispatch owner."""
    target = root / "gateway/platforms/whatsapp_cloud.py"
    source = target.read_text()
    methods = [node for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch_payload"]
    if len(methods) != 1:
        raise RuntimeError("WhatsApp reservation dispatch owner drift")
    node = methods[0]
    body = ast.get_source_segment(source, node)
    if hashlib.sha256(body.encode()).hexdigest() != 'e12f0cdacadc72a27474f36dcef22616dba70ba947ae2b2cec8cdb15b05a9294':
        raise RuntimeError("WhatsApp reservation installed preimage drift")
    start = body.index("                    try:\n                        event = await self._build_message_event_from_cloud")
    end = body.index("\n                # Log status updates", start)
    content = body[start:end].rstrip("\n")
    reservation = '                    inflight = getattr(self, "_inflight_wamids", None)\n                    if inflight is None:\n                        inflight = self._inflight_wamids = set()\n                    if wamid and wamid in inflight:\n                        return False  # Retry: the first delivery has not finished admission.\n                    if wamid:\n                        inflight.add(wamid)\n                    try:\n'
    updated = body[:start] + reservation + "".join("    " + line + "\n" for line in content.splitlines()) + "                    finally:\n                        inflight.discard(wamid)\n" + body[end:]
    if hashlib.sha256(updated.encode()).hexdigest() != '30a2fe5c0cc2d8581b438bfaf4af703590a087395766157d9ebe86a0714c45cd':
        raise RuntimeError("WhatsApp reservation installed postimage drift")
    target.write_text(source.replace(body, updated, 1))


def _upgraded_finalize_rollback(root: Path) -> str:
    target = root / "gateway/drain_inbox.py"
    source = target.read_text()
    functions = [node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name == "finalize_pre_dispatch_event_result"]
    if len(functions) != 1:
        raise RuntimeError("durable rollback finalizer owner drift")
    body = ast.get_source_segment(source, functions[0])
    if hashlib.sha256(body.encode()).hexdigest() != '62277e108cd1c9537365c50e46105814d5059e1b171d580f28567907d066a72f':
        raise RuntimeError("durable rollback installed preimage drift")
    if body.count(_ROLLBACK_OLD) != 1:
        raise RuntimeError("durable rollback finalizer anchor drift")
    return source.replace(body, body.replace(_ROLLBACK_OLD, _ROLLBACK_NEW, 1), 1)


def _upgrade_finalize_rollback(root: Path) -> None:
    (root / "gateway/drain_inbox.py").write_text(_upgraded_finalize_rollback(root))


def patch_durable_drain_inbox_carrier_v1(root: Path) -> bool:
    """Apply the exact pin-rooted source delta before dependent Golden patches."""
    root = Path(root).resolve()
    if _repo_head(root) == NATIVE_BASE_COMMIT:
        return _patch_native_durable_drain(root)
    manifest = _load_manifest()
    marker = root / MARKER_RELATIVE
    mismatches = _postimage_mismatches(root, manifest)

    if marker.exists():
        installed = json.loads(marker.read_text(encoding="utf-8"))
        # Current shipped marker and its earlier slash-admission predecessor
        # share the same exact WhatsApp dispatch owner.
        pre_rollback = copy.deepcopy(manifest)
        pre_rollback["patch_sha256"] = _PRE_ROLLBACK_LEGACY_PATCH
        pre_rollback["postimage_git_blobs"]["gateway/drain_inbox.py"] = _PRE_ROLLBACK_LEGACY_BLOB
        pre_rollback.get("downstream_exact_fragment_counts", {}).get("gateway/drain_inbox.py", {}).pop(_ROLLBACK_NEW, None)
        if installed == _marker_payload(pre_rollback):
            if _marked_install_mismatches(root, pre_rollback):
                raise RuntimeError("durable rollback installed source drift")
            _upgrade_finalize_rollback(root)
            _write_marker(root, manifest)
            return True
        pre_reservation = copy.deepcopy(pre_rollback)
        pre_reservation["patch_sha256"] = 'a129e07f5f600f6de0db615968ffded767ae492e8db5f206d96e023213227210'
        pre_reservation["postimage_git_blobs"]["gateway/platforms/whatsapp_cloud.py"] = '1ce0a954210ee6f56e8622b6fccc4594b3fd6b6a'
        pre_reservation.get("downstream_exact_fragment_counts", {}).pop("gateway/platforms/whatsapp_cloud.py", None)
        if installed == _marker_payload(pre_reservation):
            if _marked_install_mismatches(root, pre_reservation):
                raise RuntimeError("WhatsApp reservation installed source drift")
            updated = _upgraded_finalize_rollback(root)
            _upgrade_whatsapp_reservation(root)
            (root / "gateway/drain_inbox.py").write_text(updated)
            _write_marker(root, manifest)
            return True
        previous_manifest = copy.deepcopy(pre_reservation)
        previous_manifest["patch_sha256"] = _PRE_SLASH_LEGACY_PATCH
        previous_manifest["downstream_exact_fragment_counts"] = {key: dict(value) for key, value in pre_reservation.get("downstream_exact_fragment_counts", {}).items()}
        previous_manifest["downstream_exact_fragment_counts"].get("gateway/drain_inbox.py", {}).pop(_SLASH_NEW, None)
        if not previous_manifest["downstream_exact_fragment_counts"].get("gateway/drain_inbox.py"):
            previous_manifest["downstream_exact_fragment_counts"].pop("gateway/drain_inbox.py", None)
        previous_manifest["postimage_git_blobs"] = dict(pre_reservation["postimage_git_blobs"])
        previous_manifest["postimage_git_blobs"]["gateway/drain_inbox.py"] = _PRE_SLASH_LEGACY_BLOB
        if installed == _marker_payload(previous_manifest):
            if _marked_install_mismatches(root, previous_manifest):
                raise RuntimeError("legacy slash admission source drift")
            target = root / "gateway/drain_inbox.py"
            content = target.read_text()
            if content.count(_SLASH_OLD) != 1:
                raise RuntimeError("legacy slash admission preimage drift")
            updated = content.replace(_SLASH_OLD, _SLASH_NEW, 1).replace(_ROLLBACK_OLD, _ROLLBACK_NEW, 1)
            encoded = updated.encode()
            blob = hashlib.sha1(b"blob " + str(len(encoded)).encode() + b"\0" + encoded).hexdigest()
            if blob != manifest["postimage_git_blobs"]["gateway/drain_inbox.py"]:
                raise RuntimeError("legacy slash admission postimage drift")
            _upgrade_whatsapp_reservation(root)
            target.write_text(updated)
            _write_marker(root, manifest)
            return True
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
        raise RuntimeError(f"durable-drain platform reconnect test anchor count is {count}, expected 2")
    path.write_text(
        source.replace(
            PLATFORM_RECONNECT_TEST_ANCHOR,
            PLATFORM_RECONNECT_TEST_REPLACEMENT,
        ),
        encoding="utf-8",
    )
    return True


def _patch_raft_durable_ingress(root: Path) -> bool:
    path = root / RAFT_ADAPTER_RELATIVE
    if not path.is_file():
        raise RuntimeError("durable-drain Raft adapter is missing")
    source = path.read_text(encoding="utf-8")
    wake_complete = RAFT_WAKE_EVENT_REPLACEMENT in source
    handler_complete = RAFT_HANDLE_MESSAGE_REPLACEMENT in source
    if wake_complete and handler_complete:
        return False
    if wake_complete or handler_complete:
        raise RuntimeError("durable-drain Raft carrier is partially applied")
    if source.count(RAFT_WAKE_EVENT_ANCHOR) != 1:
        raise RuntimeError("durable-drain Raft wake anchor is missing or ambiguous")
    if source.count(RAFT_HANDLE_MESSAGE_ANCHOR) != 1:
        raise RuntimeError("durable-drain Raft handler anchor is missing or ambiguous")
    path.write_text(
        source.replace(
            RAFT_WAKE_EVENT_ANCHOR,
            RAFT_WAKE_EVENT_REPLACEMENT,
            1,
        ).replace(
            RAFT_HANDLE_MESSAGE_ANCHOR,
            RAFT_HANDLE_MESSAGE_REPLACEMENT,
            1,
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
    if _repo_head(Path(root)) == NATIVE_BASE_COMMIT:
        return _patch_native_durable_drain(Path(root))
    changed = patch_durable_drain_inbox_carrier_v1(root)
    changed = _patch_raft_durable_ingress(root) or changed
    cron = _load_sibling("cron_scheduler_can_dispatch_compat_v1")
    changed = cron.patch_cron_scheduler_can_dispatch_compat_v1(root) or changed
    changed = _patch_multiplex_test_fixture(root) or changed
    changed = _patch_platform_reconnect_test_fixture(root) or changed
    return changed
