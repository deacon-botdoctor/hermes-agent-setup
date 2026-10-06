"""Reap cached CUA backends before the gateway's os._exit boundary."""

from pathlib import Path

MARKER = "HERMES_CUA_GATEWAY_SHUTDOWN_v1"
TARGET = Path("gateway/run_shutdown.py")
TEST_TARGET = Path("tests/gateway/test_gateway_shutdown.py")
DAEMON_TARGET = Path("tools/computer_use/cua_backend_daemon.py")
TEST_PAYLOAD = Path(__file__).resolve().parents[1] / "payloads/cua-gateway-shutdown-v1/gateway_shutdown_test.py.inc"
TEST_NAME = "test_gateway_stop_reaps_evicted_computer_use_children_across_restarts"
ANCHOR = '        _step("cleanup_all_browsers", _cleanup_browsers)\n'
REPLACEMENT = ANCHOR + '''
        # HERMES_CUA_GATEWAY_SHUTDOWN_v1: os._exit skips the CUA atexit hook.
        # Evicted agents may still own backends in the process-global cache.
        def _cleanup_computer_use() -> None:
            from tools.computer_use.tool import _shutdown_backend_atexit
            _shutdown_backend_atexit()

        _step("computer_use cleanup", _cleanup_computer_use)
'''
DAEMON_MARKER = "HERMES_CUA_PARENT_LIFETIME_v1"
DAEMON_INIT_ANCHOR = "        self._process: Any = None\n"
DAEMON_INIT_REPLACEMENT = DAEMON_INIT_ANCHOR + '''        # HERMES_CUA_PARENT_LIFETIME_v1: Windows jobs close with this gateway.
        self._steward_lease_id: Optional[str] = None
        self._steward_task_id = f"cua:{uuid.uuid4().hex}"
'''
DAEMON_START_ANCHOR = "        self._owns_runtime = True\n"
DAEMON_START_REPLACEMENT = DAEMON_START_ANCHOR + '''        if sys.platform == "win32":
            try:
                self._steward_lease_id = self._register_windows_parent_lifetime(self._process)
            except Exception:
                process, self._process = self._process, None
                self._owns_runtime = False
                try:
                    if process is not None:
                        _wait_or_kill(process)
                finally:
                    with contextlib.suppress(Exception):
                        self._steward_command("finish", "--apply", "--task-id", self._steward_task_id)
                raise
'''
DAEMON_STOP_ANCHOR = "        if process is not None:\n            _wait_or_kill(process)\n"
DAEMON_STOP_REPLACEMENT = '''        try:
            if process is not None:
                _wait_or_kill(process)
        finally:
            self._release_windows_parent_lifetime()
'''
DAEMON_HELPER_ANCHOR = "    def start(self) -> None:\n"
DAEMON_HELPER = '''    def _steward_command(self, operation: str, *args: str) -> dict:
        from hermes_constants import get_hermes_home

        home = os.path.abspath(str(get_hermes_home()))
        if os.path.basename(os.path.dirname(home)).casefold() == "profiles":
            home = os.path.dirname(os.path.dirname(home))
        result = subprocess.run(
            [getattr(sys, "_base_executable", sys.executable), os.path.join(home, "bin", "hermes-host-steward.py"),
             "--hermes-home", home, operation, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            payload = {}
        if result.returncode or payload.get("status") in {"fail", "invalid", "error"}:
            raise RuntimeError("Windows embedded CUA stewardship failed")
        return payload

    def _register_windows_parent_lifetime(self, process: Any) -> str:
        payload = self._steward_command(
            "register-process", "--parent-lifetime", "--task-id", self._steward_task_id,
            "--pid", str(process.pid), "--ttl", "300",
        )
        lease_id = payload.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id:
            raise RuntimeError("Windows embedded CUA stewardship did not return a lease")
        return lease_id

    def _release_windows_parent_lifetime(self) -> None:
        lease_id = self._steward_lease_id
        if not lease_id:
            return
        try:
            payload = self._steward_command("release-lease", "--apply", "--lease-id", lease_id)
        except Exception:
            logger.warning("Windows embedded CUA lease release failed; reconciliation will retry")
            return
        if payload.get("outcome") in {"released", "already_gone"}:
            self._steward_lease_id = None

'''


def patch_cua_gateway_shutdown_v1(hermes_dir: Path) -> bool:
    target, test_target, daemon_target = hermes_dir / TARGET, hermes_dir / TEST_TARGET, hermes_dir / DAEMON_TARGET
    source, tests, daemon = target.read_text(), test_target.read_text(), daemon_target.read_text()
    if MARKER in source:
        if source.count(REPLACEMENT) != 1:
            raise RuntimeError("CUA gateway shutdown installed source drift")
        patched = source
    else:
        if source.count(ANCHOR) != 1:
            raise RuntimeError("CUA gateway shutdown anchor drift")
        patched = source.replace(ANCHOR, REPLACEMENT, 1)
    regression = TEST_PAYLOAD.read_text()
    if TEST_NAME in tests and regression.strip() not in tests:
        raise RuntimeError("CUA gateway shutdown regression drift")
    patched_tests = tests if TEST_NAME in tests else tests.rstrip() + "\n\n\n" + regression
    if DAEMON_MARKER in daemon:
        if daemon.count(DAEMON_MARKER) != 1 or any(
            daemon.count(fragment) != 1
            for fragment in ("import json\n", DAEMON_INIT_REPLACEMENT, DAEMON_HELPER,
                             DAEMON_START_REPLACEMENT, DAEMON_STOP_REPLACEMENT)
        ):
            raise RuntimeError("CUA parent-lifetime installed source drift")
        patched_daemon = daemon
    else:
        for anchor in (DAEMON_INIT_ANCHOR, DAEMON_START_ANCHOR, DAEMON_STOP_ANCHOR, DAEMON_HELPER_ANCHOR):
            if daemon.count(anchor) != 1:
                raise RuntimeError("CUA parent-lifetime anchor drift")
        if "import json\n" not in daemon:
            daemon = daemon.replace("import contextlib\n", "import contextlib\nimport json\n", 1)
        patched_daemon = daemon.replace(DAEMON_INIT_ANCHOR, DAEMON_INIT_REPLACEMENT, 1)
        patched_daemon = patched_daemon.replace(DAEMON_HELPER_ANCHOR, DAEMON_HELPER + DAEMON_HELPER_ANCHOR, 1)
        patched_daemon = patched_daemon.replace(DAEMON_START_ANCHOR, DAEMON_START_REPLACEMENT, 1)
        patched_daemon = patched_daemon.replace(DAEMON_STOP_ANCHOR, DAEMON_STOP_REPLACEMENT, 1)
    changed = [(target, source, patched), (test_target, tests, patched_tests), (daemon_target, daemon_target.read_text(), patched_daemon)]
    try:
        for path, before, after in changed:
            if before != after:
                path.write_text(after)
    except Exception:
        for path, before, after in changed:
            if before != after:
                path.write_text(before)
        raise
    return any(before != after for _, before, after in changed)
