@pytest.mark.asyncio
async def test_gateway_stop_reaps_evicted_computer_use_children_across_restarts(monkeypatch):
    """CUA sessions can outlive evicted agents, but cannot outlive their gateway."""
    import sys

    from tools.computer_use import tool as computer_use
    from tools.computer_use.cua_backend_daemon import _EmbeddedCuaDaemon

    children = []

    def spawn_child():
        child = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    class ProcessBackend:
        def __init__(self, permission_mode):
            self.daemon = _EmbeddedCuaDaemon(sys.executable, "unrestricted")

        def start(self):
            self.process = spawn_child()
            # Replace only the desktop device. There is no control socket; the
            # real daemon teardown must reap this owned child through Popen.
            self.daemon._process = self.process
            self.daemon._running = True

        def stop(self):
            self.daemon.stop()

    computer_use.reset_backend_for_tests()
    monkeypatch.setattr(computer_use, "_new_backend", ProcessBackend)
    try:
        unowned = spawn_child()
        for cycle in range(3):
            backend = computer_use._get_backend(f"evicted-session-{cycle}")
            runner, _adapter = make_restart_runner()
            # No agent remains to call close(): soft cache eviction keeps CUA.
            assert runner._running_agents == {}
            assert backend.process.poll() is None
            with patch("gateway.status.remove_pid_file"), patch("gateway.status.publish_runtime_status", create=True):
                await runner.stop()
                await runner.stop()

            assert backend.process.poll() is not None, "gateway shutdown left its CUA child alive"
            assert computer_use._backends == {}
            assert computer_use._backend_call_locks == {}
            assert unowned.poll() is None, "gateway shutdown killed an unowned child"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        computer_use.reset_backend_for_tests()


def test_windows_embedded_cua_registration_failure_reaps_its_popen(monkeypatch):
    """The Windows lease is a start precondition, so an unleased child cannot run."""
    import tools.computer_use.cua_backend_daemon as daemon_module

    class Child:
        pid = 42
        stderr = ()
        killed = False

        def wait(self, timeout):
            self.killed = True

        def terminate(self):
            self.killed = True

        def kill(self):
            self.killed = True

    child = Child()
    daemon = daemon_module._EmbeddedCuaDaemon("cua-driver", "unrestricted")
    monkeypatch.setattr(daemon_module.sys, "platform", "win32")
    monkeypatch.setattr(daemon_module._driver, "_resolve_mcp_invocation", lambda _cmd: ("cua-driver", []))
    monkeypatch.setattr(daemon_module, "_embedded_daemon_spawn_command", lambda *_args, **_kwargs: ["cua-driver"])
    monkeypatch.setattr(daemon_module.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(daemon, "_serve_args", lambda: [])
    monkeypatch.setattr(daemon, "_register_windows_parent_lifetime", lambda _child: (_ for _ in ()).throw(RuntimeError("no lease")))

    with pytest.raises(RuntimeError, match="no lease"):
        daemon.start()
    assert child.killed
    assert daemon._process is None


@pytest.mark.parametrize(("configured_home", "host_home"), [
    (r"C:\Hermes", r"C:\Hermes"),
    (r"C:\Hermes\profiles\client", r"C:\Hermes"),
])
def test_windows_embedded_cua_steward_uses_host_root(monkeypatch, configured_home, host_home):
    import json
    import ntpath
    import sys
    import types
    import tools.computer_use.cua_backend_daemon as daemon_module

    seen = []

    class Result:
        returncode = 0
        stdout = json.dumps({"schema": "hermes-host-steward/v1"})

    fake_constants = types.ModuleType("hermes_constants")
    fake_constants.get_hermes_home = lambda: configured_home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_constants)
    monkeypatch.setattr(daemon_module.subprocess, "run", lambda argv, **_kwargs: seen.append(argv) or Result())
    daemon = daemon_module._EmbeddedCuaDaemon("cua-driver", "unrestricted")
    monkeypatch.setattr(daemon_module, "os", types.SimpleNamespace(path=ntpath))

    daemon._steward_command("release-lease", "--apply", "--lease-id", "a" * 32)

    assert seen[0][0] == getattr(sys, "_base_executable", sys.executable)
    assert seen[0][1] == host_home + r"\bin\hermes-host-steward.py"
    assert seen[0][2:4] == ["--hermes-home", host_home]
