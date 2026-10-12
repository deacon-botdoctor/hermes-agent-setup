#!/usr/bin/env python3
"""Bind persistent local terminal processes to Host Steward ownership."""

from __future__ import annotations

from pathlib import Path


MARKER = "HERMES_HOST_STEWARD_RUNTIME_OWNERSHIP_v1"
TARGET = Path("tools/process_registry.py")


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"[{MARKER}] expected one {label} anchor, found {count}")
    return source.replace(old, new, 1)


def _replace_one_of(source: str, alternatives: tuple[str, ...], replacement: str, label: str) -> str:
    """Replace one exact supported upstream shape, refusing ambiguous drift."""
    matches = [(anchor, source.count(anchor)) for anchor in alternatives]
    matched = [anchor for anchor, count in matches if count == 1]
    if len(matched) != 1 or any(count > 1 for _, count in matches):
        found = sum(count for _, count in matches)
        raise RuntimeError(f"[{MARKER}] expected one {label} anchor, found {found}")
    return source.replace(matched[0], replacement, 1)



_REGISTRATION_HELPER = 'def _register_with_host_steward(session: "ProcessSession") -> str:\n    supervised = _is_supervised_gateway_process()\n    session.host_steward_scope = "managed" if supervised else "user"\n    if not supervised:\n        # Interactive CLI work is user-owned rather than fleet-agent-owned.\n        return ""\n    home = get_hermes_home()\n    steward = home / "bin" / "hermes-host-steward.py"\n    if steward.is_symlink() or not steward.is_file():\n        raise _HostStewardRegistrationError(\n            "Host Steward is unavailable; refusing an unowned background process"\n        )\n    task_id = (\n        getattr(session, "task_id", "")\n        or getattr(session, "session_key", "")\n        or session.id\n    )\n    try:\n        result = subprocess.run(\n            [\n                sys.executable,\n                str(steward),\n                "--hermes-home",\n                str(home),\n                "register-process",\n                "--task-id",\n                task_id,\n                "--pid",\n                str(session.pid),\n                "--ttl",\n                str(_HOST_STEWARD_TTL_SECONDS),\n                *(["--systemd-unit", session.systemd_unit] if session.systemd_unit else []),\n            ],\n            capture_output=True,\n            text=True,\n            check=False,\n            timeout=10,\n        )\n        payload = json.loads(result.stdout) if result.stdout else {}\n    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:\n        raise _HostStewardRegistrationError(\n            "Host Steward registration failed; refusing an unowned background process"\n        ) from exc\n    lease_id = payload.get("lease_id") if isinstance(payload, dict) else None\n    if (\n        result.returncode != 0 or not isinstance(payload, dict)\n        or payload.get("schema") != "hermes-host-steward/v1"\n        or payload.get("kind") != "process"\n        or payload.get("task_id") != task_id\n        or not isinstance(lease_id, str)\n        or len(lease_id) != 32\n        or any(character not in "0123456789abcdef" for character in lease_id)\n    ):\n        raise _HostStewardRegistrationError(\n            "Host Steward rejected background-process ownership"\n        )\n    session.host_steward_renewed_at = time.time()\n    return lease_id\n\n'

def patch_host_steward_runtime_ownership_v1(hermes_dir: Path) -> bool:
    target = hermes_dir / TARGET
    if not target.is_file():
        raise RuntimeError(f"[{MARKER}] runtime target missing: {target}")
    source = target.read_text(encoding="utf-8")
    split_checkpoint_layout = (
        "from tools.process_registry_checkpoint import ProcessCheckpointMixin" in source
        and "_CHECKPOINT_FIELDS = (" in source
    )
    if not split_checkpoint_layout:
        raise RuntimeError("Host Steward requires the native split process registry")
    if MARKER in source:
        current = _REGISTRATION_HELPER
        previous = current.replace("        result.returncode != 0 or not isinstance(payload, dict)\n", "        not isinstance(payload, dict)\n", 1)
        if source.count(current) == 1:
            return False
        if source.count(previous) != 1:
            raise RuntimeError("Host Steward installed registration helper drift")
        target.write_text(source.replace(previous, current, 1), encoding="utf-8")
        return True

    source = _replace_once(
        source,
        "import subprocess\nimport tempfile\nimport threading\n",
        "import subprocess\nimport sys\nimport tempfile\nimport threading\n",
        "sys import",
    )
    source = _replace_once(
        source,
        'CHECKPOINT_PATH = get_hermes_home() / "processes.json"\n',
        '''CHECKPOINT_PATH = get_hermes_home() / "processes.json"

# HERMES_HOST_STEWARD_RUNTIME_OWNERSHIP_v1: every persistent local terminal
# process must acquire durable host ownership before it is exposed as running.
_HOST_STEWARD_TTL_SECONDS = 24 * 3600  # matches ProcessRegistry's active-age cap


class _HostStewardRegistrationError(RuntimeError):
    pass


''' + _REGISTRATION_HELPER + '''
def _renew_with_host_steward(session: "ProcessSession") -> None:
    if not session.host_steward_lease_id:
        return
    now = time.time()
    if now - session.host_steward_renewed_at < _HOST_STEWARD_TTL_SECONDS / 2:
        return
    home = get_hermes_home()
    steward = home / "bin" / "hermes-host-steward.py"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(steward),
                "--hermes-home",
                str(home),
                "renew-lease",
                "--lease-id",
                session.host_steward_lease_id,
                "--ttl",
                str(_HOST_STEWARD_TTL_SECONDS),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        payload = json.loads(result.stdout) if result.stdout else {}
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        raise _HostStewardRegistrationError("Host Steward lease renewal failed") from exc
    if result.returncode != 0 or int(payload.get("renewed") or 0) != 1:
        raise _HostStewardRegistrationError("Host Steward rejected lease renewal")
    session.host_steward_renewed_at = now


def _release_with_host_steward(session: "ProcessSession") -> None:
    lease_id = session.host_steward_lease_id
    if not lease_id:
        return
    home = get_hermes_home()
    steward = home / "bin" / "hermes-host-steward.py"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(steward),
                "--hermes-home",
                str(home),
                "release-lease",
                "--lease-id",
                lease_id,
                "--apply",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        payload = json.loads(result.stdout) if result.stdout else {}
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return
    if result.returncode == 0 and payload.get("outcome") in {
        "released",
        "already_gone",
        "preserved",
    }:
        session.host_steward_lease_id = ""
''',
        "checkpoint constant",
    )
    source = _replace_one_of(
        source,
        (
            '    systemd_unit: str = ""                      # transient scope unit name when spawned under systemd-run (#70716)\n',
            '    systemd_unit: str = ""                      # transient scope unit name when spawned under systemd-run\n',
        ),
        '    systemd_unit: str = ""                      # transient scope unit name when spawned under systemd-run (#70716)\n'
        '    host_steward_lease_id: str = ""             # durable ownership for local background work\n'
        '    host_steward_scope: str = "legacy"           # managed, user, or pre-contract legacy\n'
        '    host_steward_renewed_at: float = 0.0          # renewal throttle timestamp\n',
        "session lease field",
    )
    source = _replace_once(
        source,
        '    "command", "pid", "pid_scope", "host_start_time", "systemd_unit", "wsl_chain", "cwd",\n',
        '    "command", "pid", "pid_scope", "host_start_time", "systemd_unit", "host_steward_lease_id", "host_steward_scope", "wsl_chain", "cwd",\n',
        "split checkpoint lease fields",
    )
    source = _replace_once(
        source,
        '''        session.host_start_time = self._safe_host_start_time(session.pid)
        session._pty = pty_proc
        self._track_started(session, self._pty_reader_loop, f"proc-pty-reader-{session.id}")
''',
        '''        session.host_start_time = self._safe_host_start_time(session.pid)
        session._pty = pty_proc
        try:
            session.host_steward_lease_id = _register_with_host_steward(session)
        except _HostStewardRegistrationError:
            if session.systemd_unit:
                with suppress(Exception):
                    _stop_systemd_unit(session.systemd_unit)
            with suppress(Exception):
                self._terminate_host_pid(session.pid, session.host_start_time)
            with suppress(Exception):
                pty_proc.terminate(force=True)
            raise
        self._track_started(session, self._pty_reader_loop, f"proc-pty-reader-{session.id}")
''',
        "split PTY registration",
    )
    source = _replace_once(
        source,
        '''        session.host_start_time = self._safe_host_start_time(session.pid)
        try:
            self._track_started(session, self._reader_loop, f"proc-reader-{session.id}")
''',
        '''        session.host_start_time = self._safe_host_start_time(session.pid)
        try:
            session.host_steward_lease_id = _register_with_host_steward(session)
            self._track_started(session, self._reader_loop, f"proc-reader-{session.id}")
''',
        "split pipe registration",
    )
    source = _replace_once(
        source,
        '''        self._reconcile_local_exit(session)  # orphaned-pipe reader guard
        with session._lock:
''',
        '''        self._reconcile_local_exit(session)  # orphaned-pipe reader guard
        if not session.exited:
            try:
                _renew_with_host_steward(session)
            except _HostStewardRegistrationError as exc:
                logger.warning("Host Steward lease renewal deferred: %s", exc)
        with session._lock:
''',
        "split poll lease renewal",
    )
    source = _replace_once(
        source,
        '''        session._completion_event.set()
        return was_running

    @staticmethod
    def _exit_fields(session: ProcessSession) -> dict:
''',
        '''        session._completion_event.set()
        if was_running:
            _release_with_host_steward(session)
        return was_running

    @staticmethod
    def _exit_fields(session: ProcessSession) -> dict:
''',
        "split process completion release",
    )
    target.write_text(source, encoding="utf-8")
    return True
