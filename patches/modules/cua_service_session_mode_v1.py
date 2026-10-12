"""Keep Windows service sessions on their configured desktop bridge."""

from pathlib import Path

MARKER = "HERMES_CUA_SERVICE_SESSION_MODE_v1"
TARGET = Path("tools/computer_use/tool.py")
ANCHOR = '''        from tools.computer_use.cua_backend import _cua_configured_permission_mode
        configured = _cua_configured_permission_mode()
'''
REPLACEMENT = '''        from tools.computer_use.cua_backend import _cua_configured_permission_mode
        configured = _cua_configured_permission_mode()
        # HERMES_CUA_SERVICE_SESSION_MODE_v1: Session 0 cannot own an interactive private driver.
        if os.name == "nt":
            import ctypes
            native_session = ctypes.c_ulong()
            if (ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(native_session))
                    and native_session.value == 0):
                return configured
'''


def patch_cua_service_session_mode_v1(hermes_dir: Path) -> bool:
    target = hermes_dir / TARGET
    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        if source.count(REPLACEMENT) != 1:
            raise RuntimeError("CUA service-session mode installed source drift")
        return False
    if source.count(ANCHOR) != 1:
        raise RuntimeError("CUA service-session mode anchor drift")
    patched = source.replace(ANCHOR, REPLACEMENT, 1)
    compile(patched, str(target), "exec")
    target.write_text(patched, encoding="utf-8")
    return True
