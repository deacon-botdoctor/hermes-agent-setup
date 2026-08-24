#!/usr/bin/env python3
"""Add an explicit host-scoped existing-profile grant to private Cua daemons."""

from __future__ import annotations

from pathlib import Path


MARKER = "HERMES_CUA_EXISTING_PROFILE_LAUNCH_GRANT_v1"
PRIVATE_DAEMON_MARKER = "HERMES_CUA_EXISTING_PROFILE_PRIVATE_DAEMON_v1"
TARGET = Path("tools/computer_use/cua_backend.py")
TEST_TARGET = Path("tests/computer_use/test_existing_profile_launch_grant.py")

COMMAND_ANCHOR = '''        command = [
            self._command,
            "serve",
            "--embedded",
            "--socket",
            self.socket_path,
            "--no-permissions-gate",
            "--permission-mode",
            "unrestricted",
            "--dangerously-bypass-approvals",
        ]
'''

COMMAND_REPLACEMENT = f'''        existing_profile_grant = _existing_profile_launch_grant_enabled()
        if existing_profile_grant:
            # {MARKER}: CuaDriver accepts launch grants only in standard mode.
            # This remains a host-scoped, immutable launch decision; the model
            # cannot enable it through a tool call.
            env["CUA_DRIVER_PERMISSION_MODE"] = "standard"
            env.pop("CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS", None)
        command = [
            self._command,
            "serve",
            "--embedded",
            "--socket",
            self.socket_path,
            "--no-permissions-gate",
            "--permission-mode",
            "standard" if existing_profile_grant else "unrestricted",
        ]
        if existing_profile_grant:
            command.extend(["--grant", "existing-profile"])
        else:
            command.append("--dangerously-bypass-approvals")
'''

EMBEDDED_CLASS_ANCHOR = '''class _EmbeddedCuaDaemon:
'''

EMBEDDED_CLASS_REPLACEMENT = f'''def _existing_profile_launch_grant_enabled() -> bool:
    """Return the immutable host launch grant selected before agent startup."""
    return os.getenv(
        "HERMES_CUA_EXISTING_PROFILE_GRANT", ""
    ).strip().lower() in {{"1", "true", "yes", "on"}}


class _EmbeddedCuaDaemon:
'''

EMBEDDED_INIT_ANCHOR = '''        if permission_mode != "unrestricted":
            raise ValueError("embedded permission override supports unrestricted only")
'''

EMBEDDED_INIT_REPLACEMENT = f'''        if permission_mode != "unrestricted" and not (
            permission_mode == "standard"
            and _existing_profile_launch_grant_enabled()
        ):
            raise ValueError("embedded daemon requires unrestricted mode or a host launch grant")
'''

BACKEND_INIT_ANCHOR = '''        self._embedded_daemon = (
            _EmbeddedCuaDaemon(resolve_cua_driver_cmd() or "", permission_mode)
            if permission_mode == "unrestricted"
            else None
        )
'''

BACKEND_INIT_REPLACEMENT = f'''        # {PRIVATE_DAEMON_MARKER}: a standard session with the host grant
        # must use its own grant-bearing daemon. The machine-wide standard
        # daemon cannot acquire grants after it has already started.
        self._embedded_daemon = (
            _EmbeddedCuaDaemon(resolve_cua_driver_cmd() or "", permission_mode)
            if permission_mode == "unrestricted"
            or _existing_profile_launch_grant_enabled()
            else None
        )
'''

TEST_SOURCE = '''from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch


def _launch_command(monkeypatch, enabled: bool):
    from tools.computer_use import cua_backend

    if enabled:
        monkeypatch.setenv("HERMES_CUA_EXISTING_PROFILE_GRANT", "1")
    else:
        monkeypatch.delenv("HERMES_CUA_EXISTING_PROFILE_GRANT", raising=False)

    process = Mock()
    process.poll.return_value = None
    process.stderr = []
    process.wait.return_value = 0
    status = SimpleNamespace(returncode=0, stdout="running", stderr="")
    stopped = SimpleNamespace(returncode=0, stdout="", stderr="")
    daemon = cua_backend._EmbeddedCuaDaemon("cua-driver", "unrestricted")
    with patch.object(
        cua_backend,
        "_resolve_mcp_invocation",
        return_value=("/opt/cua-driver", ["mcp"]),
    ), patch.object(
        cua_backend.subprocess, "Popen", return_value=process
    ) as popen, patch.object(
        cua_backend.subprocess, "run", side_effect=[status, stopped]
    ):
        daemon.start()
        command = popen.call_args.args[0]
        child_env = popen.call_args.kwargs["env"]
        daemon.stop()
    return command, child_env


def test_existing_profile_grant_is_explicitly_opt_in(monkeypatch):
    command, _ = _launch_command(monkeypatch, enabled=False)
    assert "--grant" not in command


def test_existing_profile_grant_is_bound_at_private_daemon_launch(monkeypatch):
    command, child_env = _launch_command(monkeypatch, enabled=True)
    assert command[command.index("--grant") + 1] == "existing-profile"
    assert command[command.index("--permission-mode") + 1] == "standard"
    assert "--dangerously-bypass-approvals" not in command
    assert child_env["CUA_DRIVER_PERMISSION_MODE"] == "standard"
    assert "CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS" not in child_env


def test_standard_backend_uses_grant_bearing_private_daemon(monkeypatch):
    from tools.computer_use import cua_backend

    monkeypatch.setenv("HERMES_CUA_EXISTING_PROFILE_GRANT", "1")
    with patch.object(cua_backend, "resolve_cua_driver_cmd", return_value="cua-driver"):
        backend = cua_backend.CuaDriverBackend(permission_mode="standard")
    assert backend._embedded_daemon is not None
    assert backend._embedded_daemon.permission_mode == "standard"


def test_standard_backend_without_grant_reuses_machine_daemon(monkeypatch):
    from tools.computer_use import cua_backend

    monkeypatch.delenv("HERMES_CUA_EXISTING_PROFILE_GRANT", raising=False)
    backend = cua_backend.CuaDriverBackend(permission_mode="standard")
    assert backend._embedded_daemon is None


def test_default_launch_remains_unrestricted(monkeypatch):
    command, child_env = _launch_command(monkeypatch, enabled=False)
    assert command[command.index("--permission-mode") + 1] == "unrestricted"
    assert "--dangerously-bypass-approvals" in command
    assert child_env["CUA_DRIVER_PERMISSION_MODE"] == "unrestricted"
    assert child_env["CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS"] == "1"
'''


def patch_source(source: str) -> str:
    if PRIVATE_DAEMON_MARKER in source:
        return source
    patched = source
    if MARKER not in patched:
        if patched.count(COMMAND_ANCHOR) != 1:
            raise RuntimeError("Cua embedded-daemon launch command anchor drift")
        patched = patched.replace(COMMAND_ANCHOR, COMMAND_REPLACEMENT, 1)
    for label, anchor, replacement in (
        ("embedded class", EMBEDDED_CLASS_ANCHOR, EMBEDDED_CLASS_REPLACEMENT),
        ("embedded init", EMBEDDED_INIT_ANCHOR, EMBEDDED_INIT_REPLACEMENT),
        ("backend init", BACKEND_INIT_ANCHOR, BACKEND_INIT_REPLACEMENT),
    ):
        if patched.count(anchor) != 1:
            raise RuntimeError(f"Cua {label} anchor drift")
        patched = patched.replace(anchor, replacement, 1)
    return patched


def patch_cua_existing_profile_launch_grant_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    target = root / TARGET
    if not target.is_file():
        return False
    source = target.read_text(encoding="utf-8")
    patched = patch_source(source)
    changed = patched != source
    if changed:
        target.write_text(patched, encoding="utf-8")

    test_path = root / TEST_TARGET
    if not test_path.is_file() or test_path.read_text(encoding="utf-8") != TEST_SOURCE:
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(TEST_SOURCE, encoding="utf-8")
        changed = True
    return changed


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_cua_existing_profile_launch_grant_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
