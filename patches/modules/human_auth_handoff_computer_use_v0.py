#!/usr/bin/env python3
"""Install the fail-closed native computer-use authentication handoff."""
from __future__ import annotations

import json
from pathlib import Path

PAYLOAD_DIR = Path(__file__).resolve().parents[1] / "payloads" / "human-auth-handoff-computer-use-v0"

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
            # Upstream now supplies a new backend through its lease owner.
            # Keep that fixture seam; only add the same safe capture evidence.
            if before not in source and after not in source and before.replace('"_get_backend"', '"_new_backend"') in source:
                before = before.replace('"_get_backend"', '"_new_backend"')
                after = after.replace('"_get_backend"', '"_new_backend"')
            elif after.replace('"_get_backend"', '"_new_backend"') in source:
                after = after.replace('"_get_backend"', '"_new_backend"')
            if after in source:
                continue
            if source.count(before) != 1:
                raise RuntimeError(f"native computer-use test owner drift: {relative}")
            source = source.replace(before, after, 1)
        outputs[path] = source
    native_path = root / "tests/tools/test_computer_use_input_target_guard.py"
    if native_path in outputs and "def test_native_pending_freezes_threads_and_other_process(" not in outputs[native_path]:
        native_tests = (PAYLOAD_DIR / "d363_native_tests.py").read_text()
        if "def _backend_for_call(" in (root / "tools/computer_use/tool.py").read_text():
            native_tests = native_tests.replace("monkeypatch.setattr(native, '_get_backend', lambda **kw: backend)",
                "native.reset_backend_for_tests()\n    monkeypatch.setattr(native, '_new_backend', lambda *args, **kw: backend)")
        outputs[native_path] = outputs[native_path].rstrip() + "\n\n" + native_tests
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
    if "args: Dict[str, Any], session_id: Optional[str] = None)" in source:
        anchors[0] = tuple(text.replace("args: Dict[str, Any])", "args: Dict[str, Any], session_id: Optional[str] = None)") for text in anchors[0])
        anchors[2] = tuple(text.replace("(cap)", "(cap, session_id=session_id)").replace("(backend, cap)", "(backend, cap, session_id=session_id)") for text in anchors[2])
        payload = payload.replace("args: Dict[str, Any])", "args: Dict[str, Any], session_id: Optional[str] = None)").replace("_dispatch_native(backend, action, args)", "_dispatch_native(backend, action, args, session_id=session_id)")
        payload = payload.replace("def _auth_capture_response(backend, cap):", "def _auth_capture_response(backend, cap, session_id=None):").replace("return _capture_response(cap)", "return _capture_response(cap, session_id=session_id)")
    if "args: Dict[str, Any], fence: Callable[[], None]" in source:
        anchors[0] = (
            "def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any], fence: Callable[[], None] = lambda: None,\n              session_id: Optional[str] = None) -> Any:",
            "def _dispatch_native(backend: ComputerUseBackend, action: str, args: Dict[str, Any], fence: Callable[[], None] = lambda: None,\n              session_id: Optional[str] = None) -> Any:",
        )
        anchors[1] = ("    return _capture_response(cap, session_id=session_id)\n",
                      "    return _auth_capture_response(backend, cap, session_id=session_id)\n")
        anchors[2] = ("resp, payload = _capture_response(cap, session_id=session_id), _action_payload(res)",
                      "resp, payload = _auth_capture_response(backend, cap, session_id=session_id), _action_payload(res)")
        anchors.append(("cap = backend.capture(mode=mode, app=args.get(\"app\")",
                        "cap = backend.capture(mode='som' if mode == 'vision' else mode, app=args.get(\"app\")"))
        payload = payload.replace("args: Dict[str, Any])", "args: Dict[str, Any], fence=lambda: None, session_id=None)")
        payload = payload.replace("_dispatch_native(backend, action, args)",
                                  "_dispatch_native(backend, action, args, fence=fence, session_id=session_id)")
        # Preserve upstream lease rejection before input and before derived capture output.
        payload = payload.replace("    epoch = _read_shared_handoff_epoch()\n", "    fence()\n    epoch = _read_shared_handoff_epoch()\n", 1)
        payload = payload.replace("                    cap = _guard_capture(backend, cap)\n",
                                  "                    fence()\n                    cap = _guard_capture(backend, cap)\n", 1)
        payload = payload.replace("def _auth_capture_response(backend, cap):", "def _auth_capture_response(backend, cap, session_id=None):")
        payload = payload.replace("return _capture_response(cap)", "return _capture_response(cap, session_id=session_id)")
        payload = payload.replace("    with _native_auth_dispatch_lock:\n", (
            "    # Enumeration cannot inject input or derive a capture. Preserve independent profile reads.\n"
            "    if action in {\"list_apps\", \"list_windows\"}:\n"
            "        return _dispatch_native(backend, action, args, fence=fence, session_id=session_id)\n"
            "    with _native_auth_dispatch_lock:\n"), 1)
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

def patch_human_auth_handoff_computer_use_v0(root: Path) -> bool | str:
    target = Path(root) / "tools/computer_use/tool.py"
    source = target.read_text(encoding="utf-8") if target.is_file() else ""
    if "_ActionSpec = namedtuple(" in source or D363_MARKER in source:
        return _patch_d363_computer_use(Path(root))
    raise RuntimeError("unsupported native computer-use dispatcher")
