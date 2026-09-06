#!/usr/bin/env python3
"""Install the fail-closed computer-use authentication handoff."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

PAYLOAD_DIR = (
    Path(__file__).resolve().parents[1]
    / "payloads"
    / "human-auth-handoff-computer-use-v0"
)
MANIFEST_PATH = PAYLOAD_DIR / "manifest.json"
MARKER_RELATIVE = Path(
    ".golden-runtime-carriers/human-auth-handoff-computer-use-v0.json"
)
IDEMPOTENCY = "HERMES_HUMAN_AUTH_HANDOFF_COMPUTER_USE_v0"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob_oid(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
        usedforsecurity=False,
    ).hexdigest()


def _load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    payload = PAYLOAD_DIR / str(manifest.get("patch") or "")
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported computer-use auth carrier manifest")
    if not payload.is_file() or _sha256(payload) != manifest.get("patch_sha256"):
        raise RuntimeError("computer-use auth carrier payload mismatch")
    if manifest.get("target") != "tools/computer_use/tool.py":
        raise RuntimeError("computer-use auth carrier target mismatch")
    fragments = manifest.get("required_fragments")
    if not isinstance(fragments, list) or not fragments:
        raise RuntimeError("computer-use auth carrier fragments are invalid")
    return manifest


def _marker_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotency": IDEMPOTENCY,
        "patch_sha256": manifest["patch_sha256"],
        "postimage_git_blob": manifest["postimage_git_blob"],
    }


def _write_marker(root: Path, manifest: dict[str, Any]) -> None:
    marker = root / MARKER_RELATIVE
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(_marker_payload(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def _fragments_present(target: Path, manifest: dict[str, Any]) -> bool:
    if not target.is_file():
        return False
    source = target.read_text(encoding="utf-8")
    return all(fragment in source for fragment in manifest["required_fragments"])


def _apply_payload(root: Path, payload: Path, *, check: bool) -> None:
    command = ["git", "apply", "--no-index", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    command.append(str(payload))
    child_env = os.environ.copy()
    # Runtime candidates commonly live below a Git-managed HERMES_HOME. Stop
    # repository discovery at the candidate's parent so `git apply` cannot
    # silently target that enclosing checkout instead of `root`.
    child_env["GIT_CEILING_DIRECTORIES"] = str(root.parent.resolve())
    result = subprocess.run(
        command,
        cwd=root,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "git apply failed").strip()
        raise RuntimeError(f"computer-use auth carrier rejected: {detail}")


def patch_human_auth_handoff_computer_use_v0(root: Path) -> bool | str:
    """Apply the reviewed Lane A patch or fail closed on source drift."""
    root = Path(root).resolve()
    manifest = _load_manifest()
    target = root / manifest["target"]
    marker = root / MARKER_RELATIVE

    if marker.exists():
        installed = json.loads(marker.read_text(encoding="utf-8"))
        known_prior_receipts = (
            {
                "idempotency": IDEMPOTENCY,
                "patch_sha256": "01e5d09407e1ffc6321ea04b60b84413263b03c6ace85382dc4e871d65add5b4",
                "postimage_git_blob": "dc4c7f975ca855ffdd402ec6d80e048fd385c8d5",
            },
        )
        if installed in known_prior_receipts:
            actual = _git_blob_oid(target) if target.is_file() else None
            if actual == installed["postimage_git_blob"]:
                source = target.read_text(encoding="utf-8")
                replacements = (
                    ('    return not any(token in identity for token in known_native)\n', '    return identity.strip() not in known_native\n'),
                    ('    proc = subprocess.run(\n        [\n            sys.executable,\n            str(script),\n            "handoff",\n            "--site",\n            site,\n            "--reason",\n            "computer_use reached a website login wall",\n            "--lane",\n            "computer_use",\n        ],\n        text=True,\n        capture_output=True,\n        check=False,\n        env=child_env,\n    )\n', '    # Match the shared owner\'s existing deadline horizon, including setup.\n    try:\n        timeout = float(os.environ.get("HERMES_AUTH_HANDOFF_TIMEOUT_S", "600"))\n    except (TypeError, ValueError):\n        timeout = 600.0\n    if not math.isfinite(timeout) or timeout < 1:\n        timeout = 600.0\n    try:\n        proc = subprocess.run(\n            [\n                sys.executable,\n                str(script),\n                "handoff",\n                "--site",\n                site,\n                "--reason",\n                "computer_use reached a website login wall",\n                "--lane",\n                "computer_use",\n            ],\n            text=True,\n            capture_output=True,\n            check=False,\n            env=child_env,\n            timeout=timeout + 30.0,\n        )\n    except subprocess.TimeoutExpired:\n        return "timeout"\n'),
                    ('    identity = f"{cap.app or \'\'} {cap.window_title or \'\'}".lower()\n    if not identity.strip() and backend is not None:', '    identity = str(cap.app or "").lower()\n    if not identity.strip() and backend is not None:'),
                    ('    # A nonempty tree without web/document roots is positive native-UI\n    # evidence, including for third-party apps absent from the name list.\n    if cap.elements:\n        return False\n', ""),
                    ("if payment_evidence or (modal_surface and has_payment_action) or (",
                     "if payment_evidence or has_payment_action or ("),
                    ('r"confirm purchase$|complete purchase$)"',
                     'r"confirm purchase$|complete purchase$|confirm order$|submit order$|order now$)"'),
                    ("def _element_fingerprint(element: UIElement)",
                     "def _element_fingerprint(element: UIElement, backend: ComputerUseBackend)"),
                    ("return (element.role, element.label, bounds, element.app)",
                     'return (element.role, element.label, bounds, element.app,\n'
                     '            getattr(element, "pid", None), getattr(element, "window_id", None),\n'
                     '            _sensitive_surface_key(backend))'),
                    ("_element_fingerprint(element)", "_element_fingerprint(element, backend)"),
                    ('element.index: _element_fingerprint(element, backend)\n            for element in self._safe_elements',
                     'element.index: _element_fingerprint(element, self)\n            for element in self._safe_elements'),
                )
                for before, after in replacements:
                    source = source.replace(before, after)
                updated = source.encode("utf-8")
                updated_blob = hashlib.sha1(
                    b"blob " + str(len(updated)).encode("ascii") + b"\0" + updated,
                    usedforsecurity=False,
                ).hexdigest()
                if updated_blob != manifest["postimage_git_blob"]:
                    raise RuntimeError("computer-use safety upgrade postimage mismatch")
                target.write_bytes(updated)
            elif actual != manifest["postimage_git_blob"]:
                raise RuntimeError("computer-use safety upgrade source drift")
            # Recover a crash after the exact source write but before the receipt.
            _write_marker(root, manifest)
            return True
        if installed != _marker_payload(manifest):
            raise RuntimeError("computer-use auth carrier marker provenance mismatch")
        if not target.is_file() or _git_blob_oid(target) != manifest["postimage_git_blob"]:
            raise RuntimeError("computer-use auth carrier drifted after installation")
        if not _fragments_present(target, manifest):
            raise RuntimeError("computer-use auth carrier drifted after installation")
        return False

    if target.is_file() and _git_blob_oid(target) == manifest["postimage_git_blob"]:
        if not _fragments_present(target, manifest):
            raise RuntimeError("computer-use auth carrier postimage is incomplete")
        _write_marker(root, manifest)
        return True

    if not target.is_file() or _git_blob_oid(target) != manifest["preimage_git_blob"]:
        raise RuntimeError("computer-use auth carrier preimage mismatch")

    payload = PAYLOAD_DIR / manifest["patch"]
    _apply_payload(root, payload, check=True)
    _apply_payload(root, payload, check=False)
    if _git_blob_oid(target) != manifest["postimage_git_blob"]:
        raise RuntimeError("computer-use auth carrier postimage verification failed")
    if not _fragments_present(target, manifest):
        raise RuntimeError("computer-use auth carrier fragments are incomplete")
    _write_marker(root, manifest)
    return True

# d363 keeps the native decomposed dispatcher and installs only safety residuals.
D363_MARKER = "HERMES_HUMAN_AUTH_HANDOFF_COMPUTER_USE_v0_d363"

D363_TEST_FIXTURE = """

@pytest.fixture(autouse=True)
def _isolated_computer_input_gate(tmp_path, monkeypatch):
    # A test must never join or freeze the logged-in desktop's shared input gate.
    monkeypatch.setenv("HERMES_COMPUTER_USE_GATE_FILE", str(tmp_path / "computer-input.lock"))
"""


def _patch_d363_test_isolation(root: Path) -> bool:
    updates = json.loads((PAYLOAD_DIR / "d363_test_updates.json").read_text())
    outputs = {}
    for relative, replacements in updates.items():
        path = root / relative
        if not path.exists():
            continue
        source = path.read_text()
        for before, after in replacements:
            if after in source:
                continue
            if source.count(before) != 1:
                raise RuntimeError(f"native computer-use test owner drift: {relative}")
            source = source.replace(before, after, 1)
        outputs[path] = source
    native_path = root / "tests/tools/test_computer_use_input_target_guard.py"
    if native_path in outputs and "def test_native_pending_freezes_threads_and_other_process(" not in outputs[native_path]:
        outputs[native_path] = outputs[native_path].rstrip() + "\n\n" + (PAYLOAD_DIR / "d363_native_tests.py").read_text()
    path = root / "tests/tools/conftest.py"
    if path.exists():
        source = path.read_text()
        outputs[path] = source if "def _isolated_computer_input_gate(" in source else source.rstrip() + D363_TEST_FIXTURE
    for path, source in outputs.items():
        compile(source, str(path), "exec")
    changed = False
    for path, source in outputs.items():
        if path.read_text() != source:
            path.write_text(source)
            changed = True
    return changed


def _patch_d363_computer_use(root: Path) -> bool:
    target = root / "tools/computer_use/tool.py"
    source = target.read_text(encoding="utf-8")
    anchors = [
        ("def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:",
         "def _dispatch_native(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:"),
        ("return _capture_response(backend.capture(mode=mode,", "return _auth_capture_response(backend, backend.capture(mode='som' if mode == 'vision' else mode,"),
        ("resp, payload = _capture_response(cap), _action_payload(res)",
         "resp, payload = _auth_capture_response(backend, cap), _action_payload(res)"),
    ]
    anchors.append((
        '    return json.dumps({**json.loads(resp), **payload})  # text capture: merge the action payload in\n',
        '    capture_payload = json.loads(resp)\n'
        '    if capture_payload.get("status") == "auth_required" or capture_payload.get("error") or capture_payload.get("handoff_result"):\n'
        '        return resp  # Preserve the safety verdict before merging successful action fields.\n'
        '    return json.dumps({**capture_payload, **payload})  # safe text capture\n',
    ))
    payload = (PAYLOAD_DIR / "d363_safety.py").read_text(encoding="utf-8")
    if D363_MARKER in source:
        if (not source.endswith("\n" + payload) or source.count(payload) != 1
                or any(source.count(new) != 1 for old, new in anchors)):
            raise RuntimeError("d363 computer-use safety postimage drift")
        compile(source, str(target), "exec")
        return _patch_d363_test_isolation(root)
    for old, new in anchors:
        if source.count(old) != 1:
            raise RuntimeError("d363 computer-use safety anchor drift")
        source = source.replace(old, new, 1)
    source += "\n" + payload
    compile(source, str(target), "exec")
    target.write_text(source, encoding="utf-8")
    _patch_d363_test_isolation(root)
    return True

_old_patch = patch_human_auth_handoff_computer_use_v0
def patch_human_auth_handoff_computer_use_v0(root: Path) -> bool | str:
    target = Path(root) / "tools/computer_use/tool.py"
    source = target.read_text(encoding="utf-8") if target.is_file() else ""
    if "_ActionSpec = namedtuple(" in source or D363_MARKER in source:
        return _patch_d363_computer_use(Path(root))
    return _old_patch(root)
