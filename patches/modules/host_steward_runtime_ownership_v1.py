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



_NATIVE_D363_COMMIT = "d3630f853239e8c41ce7201e09fbdf39bcbc5431"
_NATIVE_D363_PAYLOAD = Path(__file__).resolve().parents[1] / "payloads" / "host-steward-runtime-ownership-d363-v1"


def _is_native_d363(hermes_dir: Path) -> bool:
    import subprocess
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=hermes_dir, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == _NATIVE_D363_COMMIT


def _patch_native_d363(hermes_dir: Path, *, dry_run: bool = False) -> bool:
    """Install only the reviewed native residual on its exact pre/post images."""
    import hashlib
    import json
    import re
    import subprocess
    if not _is_native_d363(hermes_dir):
        raise RuntimeError("native carrier requires exact d363 HEAD")
    manifest = json.loads((_NATIVE_D363_PAYLOAD / "manifest.json").read_text())
    patch = (_NATIVE_D363_PAYLOAD / "native.patch").read_bytes()
    digest = lambda value: hashlib.sha256(value).hexdigest() if value is not None else None
    if manifest["upstream_commit"] != _NATIVE_D363_COMMIT or digest(patch) != manifest["patch_sha256"]:
        raise RuntimeError("native d363 payload integrity mismatch")
    # Reviewed unified hunks are the owned source seam. Unrelated prior Golden
    # changes may compose in the same file; every hunk must still be byte exact.
    hunks = {}
    name = None
    old, new = [], []
    active = False
    def flush():
        if active:
            hunks.setdefault(name, []).append(("".join(old), "".join(new)))
    for line in patch.decode().splitlines(keepends=True):
        if line.startswith("--- "):
            flush()
            active = False
        elif line.startswith("+++ b/"):
            name = line[6:].rstrip("\n")
        elif line.startswith("@@ "):
            flush()
            old, new = [], []
            active = True
        elif active:
            if line.startswith((" ", "-")):
                old.append(line[1:])
            if line.startswith((" ", "+")):
                new.append(line[1:])
    flush()
    if set(hunks) != set(manifest["files"]):
        raise RuntimeError("native d363 payload file inventory mismatch")
    originals = {}
    states = []
    upgrades = {}
    def source_states(name, original):
        expected = manifest["files"][name]
        if expected["pre"] == [None]:
            if digest(original) in expected["post"]:
                return ["post"]
            if original is None:
                return ["pre"]
            raise RuntimeError(f"native d363 source drift: {name}")
        if original is None:
            raise RuntimeError(f"native d363 source missing: {name}")
        content = original.decode()
        result = []
        def occurrences(fragment):
            return len(re.findall(r"(?m)^" + re.escape(fragment), content))
        for before, after in hunks[name]:
            if after and occurrences(after) == 1:
                result.append("post")
            elif before and occurrences(before) == 1:
                result.append("pre")
            else:
                raise RuntimeError(f"native d363 source drift at owned hunk: {name}")
        return result
    for name in manifest["files"]:
        path = hermes_dir / name
        if path.is_symlink():
            raise RuntimeError(f"native d363 source is a symlink: {name}")
        original = path.read_bytes() if path.exists() else None
        originals[name] = original
        if name == 'tools/process_registry.py' and original is not None:
            before, after = (b'        not isinstance(payload, dict)\n', b'        result.returncode != 0 or not isinstance(payload, dict)\n')
            if original.count(before) == 1 and after not in original:
                candidate = original.replace(before, after, 1)
                if all(state == "post" for state in source_states(name, candidate)):
                    upgrades[name] = candidate
                    original = candidate
        states.extend(source_states(name, original))
    if all(state == "post" for state in states):
        if upgrades and not dry_run:
            for name, content in upgrades.items():
                (hermes_dir / name).write_bytes(content)
        return bool(upgrades)
    if not all(state == "pre" for state in states):
        raise RuntimeError("native d363 partial installation refused")
    check = subprocess.run(["git", "apply", "--check", "-"], cwd=hermes_dir, input=patch, capture_output=True)
    if check.returncode:
        raise RuntimeError("native d363 patch preflight failed: " + check.stderr.decode())
    if dry_run:
        return True
    try:
        subprocess.run(["git", "apply", "-"], cwd=hermes_dir, input=patch, capture_output=True, check=True)
        for name in manifest["files"]:
            if any(state != "post" for state in source_states(name, (hermes_dir / name).read_bytes())):
                raise RuntimeError(f"native d363 postimage mismatch: {name}")
    except Exception:
        for name, original in originals.items():
            path = hermes_dir / name
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise
    return True

_REGISTRATION_HELPER = 'def _register_with_host_steward(session: "ProcessSession") -> str:\n    supervised = _is_supervised_gateway_process()\n    session.host_steward_scope = "managed" if supervised else "user"\n    if not supervised:\n        # Interactive CLI work is user-owned rather than fleet-agent-owned.\n        return ""\n    home = get_hermes_home()\n    steward = home / "bin" / "hermes-host-steward.py"\n    if steward.is_symlink() or not steward.is_file():\n        raise _HostStewardRegistrationError(\n            "Host Steward is unavailable; refusing an unowned background process"\n        )\n    task_id = (\n        getattr(session, "task_id", "")\n        or getattr(session, "session_key", "")\n        or session.id\n    )\n    try:\n        result = subprocess.run(\n            [\n                sys.executable,\n                str(steward),\n                "--hermes-home",\n                str(home),\n                "register-process",\n                "--task-id",\n                task_id,\n                "--pid",\n                str(session.pid),\n                "--ttl",\n                str(_HOST_STEWARD_TTL_SECONDS),\n                *(["--systemd-unit", session.systemd_unit] if session.systemd_unit else []),\n            ],\n            capture_output=True,\n            text=True,\n            check=False,\n            timeout=10,\n        )\n        payload = json.loads(result.stdout) if result.stdout else {}\n    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:\n        raise _HostStewardRegistrationError(\n            "Host Steward registration failed; refusing an unowned background process"\n        ) from exc\n    lease_id = payload.get("lease_id") if isinstance(payload, dict) else None\n    if (\n        result.returncode != 0 or not isinstance(payload, dict)\n        or payload.get("schema") != "hermes-host-steward/v1"\n        or payload.get("kind") != "process"\n        or payload.get("task_id") != task_id\n        or not isinstance(lease_id, str)\n        or len(lease_id) != 32\n        or any(character not in "0123456789abcdef" for character in lease_id)\n    ):\n        raise _HostStewardRegistrationError(\n            "Host Steward rejected background-process ownership"\n        )\n    session.host_steward_renewed_at = time.time()\n    return lease_id\n\n'

def patch_host_steward_runtime_ownership_v1(hermes_dir: Path) -> bool:
    if _is_native_d363(hermes_dir):
        return _patch_native_d363(hermes_dir)
    target = hermes_dir / TARGET
    if not target.is_file():
        raise RuntimeError(f"[{MARKER}] runtime target missing: {target}")
    source = target.read_text(encoding="utf-8")
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
        "import subprocess\nimport threading\n",
        "import subprocess\nimport sys\nimport threading\n",
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
    source = _replace_once(
        source,
        '    systemd_unit: str = ""                      # transient scope unit name when spawned under systemd-run (#70716)\n',
        '    systemd_unit: str = ""                      # transient scope unit name when spawned under systemd-run (#70716)\n'
        '    host_steward_lease_id: str = ""             # durable ownership for local background work\n'
        '    host_steward_scope: str = "legacy"           # managed, user, or pre-contract legacy\n'
        '    host_steward_renewed_at: float = 0.0          # renewal throttle timestamp\n',
        "session lease field",
    )
    source = _replace_once(
        source,
        '''                session.host_start_time = self._safe_host_start_time(session.pid)
                # Store the pty handle on the session for read/write
                session._pty = pty_proc

                # PTY reader thread
''',
        '''                session.host_start_time = self._safe_host_start_time(session.pid)
                # Store the pty handle on the session for read/write
                session._pty = pty_proc
                try:
                    session.host_steward_lease_id = _register_with_host_steward(session)
                except _HostStewardRegistrationError:
                    if session.systemd_unit:
                        try:
                            _stop_systemd_unit(session.systemd_unit)
                        except Exception:
                            pass
                    try:
                        self._terminate_host_pid(
                            session.pid, session.host_start_time
                        )
                    except Exception:
                        pass
                    try:
                        pty_proc.terminate(force=True)
                    except Exception:
                        pass
                    raise

                # PTY reader thread
''',
        "PTY registration",
    )
    source = _replace_once(
        source,
        '''            except ImportError:
                logger.warning("ptyprocess not installed, falling back to pipe mode")
            except Exception as e:
''',
        '''            except ImportError:
                logger.warning("ptyprocess not installed, falling back to pipe mode")
            except _HostStewardRegistrationError:
                raise
            except Exception as e:
''',
        "PTY fail-closed exception",
    )
    source = _replace_once(
        source,
        '''        try:
            # Start output reader thread
''',
        '''        try:
            try:
                session.host_steward_lease_id = _register_with_host_steward(session)
            except _HostStewardRegistrationError:
                # Registration runs after Popen so the exact PID can be
                # fingerprinted. Reap the birth-bound tree before surfacing
                # the failure; the outer setup guard is a second safety net.
                self._terminate_host_pid(proc.pid, session.host_start_time)
                raise
            # Start output reader thread
''',
        "pipe registration",
    )
    source = _replace_once(
        source,
        '''                            "systemd_unit": s.systemd_unit,
                            "cwd": s.cwd,
''',
        '''                            "systemd_unit": s.systemd_unit,
                            "host_steward_lease_id": s.host_steward_lease_id,
                            "host_steward_scope": s.host_steward_scope,
                            "cwd": s.cwd,
''',
        "checkpoint lease field",
    )
    source = _replace_once(
        source,
        '''                systemd_unit=entry.get("systemd_unit", ""),
                cwd=entry.get("cwd"),
''',
        '''                systemd_unit=entry.get("systemd_unit", ""),
                host_steward_lease_id=entry.get("host_steward_lease_id", ""),
                host_steward_scope=entry.get("host_steward_scope", "legacy"),
                cwd=entry.get("cwd"),
''',
        "recovery lease field",
    )
    source = _replace_once(
        source,
        '''        # Reconcile against real child state before reading session.exited.
        # Guards against orphaned-pipe reader hangs (issue #17327).
        self._reconcile_local_exit(session)

        with session._lock:
''',
        '''        # Reconcile against real child state before reading session.exited.
        # Guards against orphaned-pipe reader hangs (issue #17327).
        self._reconcile_local_exit(session)
        if not session.exited:
            try:
                _renew_with_host_steward(session)
            except _HostStewardRegistrationError as exc:
                # Host Steward independently renews from the durable managed
                # inventory. A transient renewal fault must not hide process
                # output or prevent the caller from killing the job.
                logger.warning("Host Steward lease renewal deferred: %s", exc)

        with session._lock:
''',
        "poll lease renewal",
    )
    source = _replace_once(
        source,
        '''        session._completion_event.set()
        self._write_checkpoint()

        # Only enqueue completion notification on the FIRST move.  Without
''',
        '''        session._completion_event.set()
        self._write_checkpoint()
        if was_running:
            _release_with_host_steward(session)

        # Only enqueue completion notification on the FIRST move.  Without
''',
        "process completion release",
    )
    target.write_text(source, encoding="utf-8")
    return True
