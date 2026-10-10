"""Lease the native Linux browser before launch; retain Hermes session ownership."""
from pathlib import Path

MARKER = "HERMES_HOST_STEWARD_NATIVE_BROWSER_v1"
SESSION = Path("tools/browser_tool_session.py")
LIFECYCLE = Path("tools/browser_tool_lifecycle.py")

HELPERS = r'''
# HERMES_HOST_STEWARD_NATIVE_BROWSER_v1

def _steward_browser_call(operation, *args, env=None):
    from hermes_cli.config import get_hermes_home
    import sys
    home = get_hermes_home()
    steward = home / "bin" / "hermes-host-steward.py"
    if steward.is_symlink() or not steward.is_file():
        raise RuntimeError("Host Steward is unavailable for native browser ownership")
    result = subprocess.run(
        [sys.executable, str(steward), "--hermes-home", str(home), operation, *args],
        capture_output=True, text=True, timeout=15, env=env,
    )
    payload = json.loads(result.stdout or "{}")
    released = (isinstance(payload, dict) and operation == "release-lease"
                and payload.get("outcome") in {"released", "already_gone"})
    if not isinstance(payload, dict) or (result.returncode and not released):
        raise RuntimeError("Host Steward rejected native browser ownership")
    return payload


def _steward_release_local_browser(session_info):
    lease_id = session_info.get("host_steward_lease_id")
    if not lease_id:
        return
    result = _steward_browser_call("release-lease", "--lease-id", lease_id, "--apply")
    # Keep failed ownership evidence for the scheduled reconciler. Never delete
    # profile state or claim cleanup success while the exact lease remains live.
    if result.get("outcome") not in {"released", "already_gone"}:
        raise RuntimeError("Host Steward browser cleanup requires reconciliation")
    session_info.pop("host_steward_lease_id", None)
    profile = session_info.pop("host_steward_profile", None)
    if profile:
        shutil.rmtree(profile, ignore_errors=True)


def _steward_renew_local_browser(session_info, timeout):
    lease_id = session_info.get("host_steward_lease_id")
    if lease_id:
        _steward_browser_call("renew-lease", "--lease-id", lease_id, "--ttl", str(max(300, int(timeout) + 300)))


def _steward_start_local_browser(task_id, info):
    import sys
    import tempfile
    import re
    import time
    from pathlib import Path
    if not sys.platform.startswith("linux"):
        return info
    env = _agent_browser_command_env(_prepare_session_socket_dir(info["session_name"]))
    _apply_chromium_sandbox_args(env)
    configured = env.get("AGENT_BROWSER_EXECUTABLE_PATH")
    candidates = ([Path(configured)] if configured else sorted(
        (Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"),
        reverse=True,
    ))
    executable = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if (executable is None or "/snap/" in str(executable.resolve())
            or executable.open("rb").read(4) != b"\x7fELF"):
        raise RuntimeError("Native browser ownership requires an installed non-Snap Chromium executable")
    # The existing process lease owns the full foreground session. Snap's
    # launcher can move Chromium into another scope, so it is not eligible.
    flags = env.get("AGENT_BROWSER_ARGS", env.get("AGENT_BROWSER_CHROME_FLAGS", ""))
    extra = [flag.strip() for flag in flags.split(",") if flag.strip()]
    if any(flag.startswith(("--user-data-dir", "--remote-debugging", "--profile-directory")) for flag in extra):
        raise RuntimeError("Native browser flags conflict with task ownership")
    profile = tempfile.mkdtemp(prefix="agent-browser-chrome-")
    args = [str(executable), "--headless=new", "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking", *extra]
    payload = _steward_browser_call(
        "launch-process", "--task-id", "browser:" + info["session_name"],
        "--ttl", "300", "--", *args, env=env,
    )
    lease_id = payload.get("lease_id")
    if not isinstance(lease_id, str) or not re.fullmatch(r"[0-9a-f]{32}", lease_id):
        raise RuntimeError("Host Steward did not return a browser lease")
    info["host_steward_lease_id"] = lease_id
    info["host_steward_profile"] = profile
    try:
        deadline = time.monotonic() + 10
        active_port = Path(profile) / "DevToolsActivePort"
        while time.monotonic() < deadline:
            if active_port.is_file():
                port = int(active_port.read_text().splitlines()[0])
                if 0 < port < 65536:
                    info["cdp_url"] = f"http://127.0.0.1:{port}"
                    return info
            time.sleep(0.05)
        raise RuntimeError("Owned Chromium did not become ready")
    except BaseException:
        _steward_release_local_browser(info)
        raise

'''


def patch_host_steward_native_browser_v1(hermes_dir: Path, *, dry_run: bool = False) -> bool:
    paths = {name: hermes_dir / name for name in (SESSION, LIFECYCLE)}
    sources = {name: path.read_text() for name, path in paths.items()}
    if all(MARKER in text for text in sources.values()):
        return False
    if any(MARKER in text for text in sources.values()):
        raise RuntimeError("partial native browser ownership patch")
    changes = {
        SESSION: [
            ('    info = _session_record("h", None, {"local": True})\n',
             '    info = _session_record("h", None, {"local": True})\n    info = _steward_start_local_browser(task_id, info)\n'),
            ('    proc = _popen_agent_browser(cmd_parts, browser_env, task_socket_dir, command, stdin_payload)\n',
             '    _steward_renew_local_browser(session_info, timeout)\n    proc = _popen_agent_browser(cmd_parts, browser_env, task_socket_dir, command, stdin_payload)\n'),
        ],
        LIFECYCLE: [
            ('    _forget_session_tracking(task_id, session=True)\n',
             '    # HERMES_HOST_STEWARD_NATIVE_BROWSER_v1: retain tracking until release succeeds.\n    _session._steward_release_local_browser(session_info)\n    _forget_session_tracking(task_id, session=True)\n'),
        ],
    }
    for name, replacements in changes.items():
        for old, new in replacements:
            if sources[name].count(old) != 1:
                raise RuntimeError(f"native browser ownership source drift: {name}")
            sources[name] = sources[name].replace(old, new, 1)
    sources[SESSION] += HELPERS
    for name, source in sources.items():
        compile(source, str(name), "exec")
    if not dry_run:
        for name, source in sources.items():
            paths[name].write_text(source)
    return True
