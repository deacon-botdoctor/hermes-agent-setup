#!/usr/bin/env python3
"""Ensure Golden's pinned Cua Driver through the exact Hermes runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "kit" / "config" / "cua-driver-release-v1.json"
SENSITIVE_ENV_RE = re.compile(
    r"(?:^|_)(?:api_?key|auth|credential|password|secret|token)(?:_|$)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    release = data.get("release") if isinstance(data, dict) else None
    installer = data.get("installer") if isinstance(data, dict) else None
    acceptance = data.get("acceptance") if isinstance(data, dict) else None
    if (
        data.get("schema_version") != 1
        or not isinstance(release, dict)
        or not re.fullmatch(r"\d+(?:\.\d+){2,}", str(release.get("version", "")))
        or release.get("tag") != f"cua-driver-rs-v{release.get('version')}"
        or not isinstance(data.get("assets"), dict)
        or not isinstance(installer, dict)
        or installer.get("method") != "native_hermes_computer_use_install"
        or installer.get("pin_environment") != "CUA_DRIVER_RS_VERSION"
        or not isinstance(acceptance, dict)
        or acceptance.get("baseline") != "exact_version_present"
        or not re.fullmatch(
            r"\d+(?:\.\d+){2,}",
            str(acceptance.get("minimum_compatible_version", "")),
        )
    ):
        raise ValueError("invalid Cua Driver release contract")
    version = tuple(int(part) for part in str(release["version"]).split("."))
    minimum = tuple(
        int(part) for part in str(acceptance["minimum_compatible_version"]).split(".")
    )
    if version < minimum:
        raise ValueError("Cua Driver release is below the Hermes compatibility floor")
    profile = data.get('host_profile')
    if profile is not None:
        import base64, hashlib
        if (not isinstance(profile, dict) or profile.get('schema_version') != 1
                or not isinstance(profile.get('policy'), str)
                or not all(isinstance(profile.get(key), list) and profile[key] for key in
                           ['approved_binary_sha256', 'approved_route_sha256', 'compatible_versions'])):
            raise ValueError('invalid quiet host-profile contract')
        if any(not re.fullmatch(r'[0-9a-f]{64}', value) for key in
               ['approved_binary_sha256', 'approved_route_sha256'] for value in profile[key]):
            raise ValueError('invalid admitted binary/route digest')
        if hashlib.sha256(base64.b64decode(profile['theme_base64'], validate=True)).hexdigest() != profile['theme_sha256']:
            raise ValueError('quiet theme source digest mismatch')
    return data


def platform_asset_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"amd64", "x64", "x86_64"}:
        arch = "x86_64"
    else:
        raise ValueError(f"unsupported architecture: {machine or 'unknown'}")
    if system == "darwin":
        return "macos-universal"
    if system == "windows":
        return f"windows-{arch}"
    if system == "linux":
        return f"linux-{arch}"
    raise ValueError(f"unsupported platform: {system or 'unknown'}")


def child_env(home: Path, contract: dict[str, Any]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not SENSITIVE_ENV_RE.search(key)
    }
    installer = contract["installer"]
    # Host diagnostics may intentionally shadow destructive commands in
    # HERMES_HOME/bin.  The native Cua installer needs the operating-system
    # mv/tar/curl implementations, so never inherit Hermes-local PATH entries.
    home_resolved = home.expanduser().resolve()
    path_entries: list[str] = []
    for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if not entry:
            continue
        try:
            Path(entry).expanduser().resolve().relative_to(home_resolved)
        except (OSError, ValueError):
            path_entries.append(entry)
    trusted_prefix = (
        ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        if os.name != "nt"
        else [
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"),
            os.environ.get("SystemRoot", r"C:\Windows"),
        ]
    )
    env["PATH"] = os.pathsep.join(dict.fromkeys([*trusted_prefix, *path_entries]))
    env["HERMES_HOME"] = str(home)
    env[installer["pin_environment"]] = contract["release"]["version"]
    env[installer.get("telemetry_environment", "CUA_DRIVER_RS_TELEMETRY_ENABLED")] = str(
        installer.get("telemetry_default", "0")
    )
    return env


PROBE_CODE = r'''
import json
import os
import re
import subprocess
import shutil
from tools.computer_use import cua_backend

resolve = getattr(cua_backend, 'resolve_cua_driver_cmd', None)
path = resolve() if resolve else shutil.which(getattr(cua_backend, '_CUA_DRIVER_CMD', os.environ.get('HERMES_CUA_DRIVER_CMD', 'cua-driver')))
cua_driver_child_env = getattr(cua_backend, 'cua_driver_child_env', lambda: dict(os.environ))
payload = {"installed": False, "path": path, "version": None, "version_output": None}
if path:
    proc = subprocess.run(
        [path, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        env=cua_driver_child_env(),
    )
    output = (proc.stdout or proc.stderr or "").strip()
    match = re.search(r"(?:cua-driver\s+)?(\d+(?:\.\d+){2,})", output, re.IGNORECASE)
    payload.update({
        "installed": proc.returncode == 0 and bool(match),
        "version": match.group(1) if match else None,
        "version_output": output[:200],
    })
print(json.dumps(payload, sort_keys=True))
'''

PINNED_INSTALL_CODE = r'''
import json
import sys
from pm import ensure
from pm.lock import Lockfile
from pm.paths import lockfile_path
from pm.store import current_target

wanted, expected = sys.argv[1], json.loads(sys.argv[2])
lock = Lockfile(lockfile_path())
if lock.version("cua-driver") != wanted or lock.artifacts("cua-driver", current_target()) != [expected]:
    raise SystemExit("native Cua lock does not match the approved release contract")
ensure("cua-driver", explicit=True)
'''


DOCTOR_CODE = r'''
from tools.computer_use.doctor import run_doctor

raise SystemExit(run_doctor(include=[], skip=[], json_output=True))
'''


def run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )


def probe_driver(
    python: Path,
    *,
    env: dict[str, str],
) -> dict[str, Any]:
    proc = run([str(python), "-c", PROBE_CODE], env=env, timeout=20)
    if proc.returncode != 0:
        return {
            "installed": False,
            "path": None,
            "version": None,
            "probe_error": (proc.stderr or proc.stdout).strip()[-500:],
        }
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {
            "installed": False,
            "path": None,
            "version": None,
            "probe_error": "driver probe returned invalid JSON",
        }
    return payload if isinstance(payload, dict) else {"installed": False}


def parse_doctor(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def ensure_driver(
    *,
    python: Path,
    home: Path,
    contract_path: Path,
    dry_run: bool = False,
    require_ready: bool = False,
    system: str | None = None,
    machine: str | None = None,
    diagnose: bool = True,
) -> tuple[int, dict[str, Any]]:
    contract = load_contract(contract_path)
    asset_key = platform_asset_key(system, machine)
    asset = contract["assets"].get(asset_key)
    if not isinstance(asset, dict) or not re.fullmatch(
        r"[0-9a-f]{64}", str(asset.get("sha256", ""))
    ):
        raise ValueError(f"release contract has no valid asset for {asset_key}")
    if not python.is_file():
        raise ValueError(f"Hermes runtime Python is missing: {python}")

    env = child_env(home, contract)
    wanted = contract["release"]["version"]
    before = probe_driver(python, env=env)
    exact_before = before.get("installed") is True and before.get("version") == wanted
    install_attempted = False
    install_returncode: int | None = None
    install_stdout = ""
    install_stderr = ""

    if not exact_before and not dry_run:
        install_attempted = True
        proc = run(
            [
                str(python),
                "-c",
                PINNED_INSTALL_CODE,
                wanted,
                json.dumps({
                    "url": (
                        f"https://github.com/{contract['release']['repository']}/releases/download/"
                        f"{contract['release']['tag']}/{asset['name']}"
                    ),
                    "sha256": asset["sha256"],
                }),
            ],
            env=env,
            timeout=900,
        )
        install_returncode = proc.returncode
        install_stdout = (proc.stdout or "").strip()[-1_000:]
        install_stderr = (proc.stderr or "").strip()[-1_000:]

    after = before if dry_run or exact_before else probe_driver(python, env=env)
    exact_after = after.get("installed") is True and after.get("version") == wanted

    doctor_payload = None
    doctor_returncode: int | None = None
    if exact_after and diagnose:
        doctor = run(
            [
                str(python),
                "-c",
                DOCTOR_CODE,
            ],
            env=env,
            timeout=120,
        )
        doctor_returncode = doctor.returncode
        doctor_payload = parse_doctor(doctor.stdout)

    ready = (
        doctor_returncode == 0
        and isinstance(doctor_payload, dict)
        and doctor_payload.get("ok") is not False
        and doctor_payload.get("overall") not in {"degraded", "error", "failed"}
    )
    if dry_run:
        status = "idempotent" if exact_before else "would_install"
        ok = True
    elif not exact_after:
        status = "failed"
        ok = False
    elif require_ready and not ready:
        status = "blocked_not_ready"
        ok = False
    else:
        status = "idempotent" if exact_before else "installed"
        ok = True

    receipt = {
        "schema_version": 1,
        "kind": "cua_driver_install_receipt",
        "generated_at": utc_now(),
        "ok": ok,
        "status": status,
        "dry_run": dry_run,
        "require_ready": require_ready,
        "hermes_home": str(home),
        "hermes_python": str(python),
        "release": contract["release"],
        "asset": {"key": asset_key, **asset},
        "before": before,
        "after": after,
        "install_attempted": install_attempted,
        "install_returncode": install_returncode,
        "install_stdout": install_stdout,
        "install_stderr": install_stderr,
        "doctor_ready": ready,
        "doctor_returncode": doctor_returncode,
        "doctor": doctor_payload,
    }
    return (0 if ok else 1), receipt


# Host-control profile. This module is copied as the admitted launcher payload.
def host_profile_run(command, **kwargs):
    env = {key: value for key, value in (kwargs.get('env') or os.environ).items()
           if not SENSITIVE_ENV_RE.search(key)}
    env.setdefault('CUA_DRIVER_RS_TELEMETRY_ENABLED', '0')
    if platform.system() == 'Darwin':
        # Suppress only first-launch onboarding UI; macOS still enforces TCC permissions.
        env['CUA_DRIVER_RS_PERMISSIONS_GATE'] = '0'
    kwargs['env'] = env
    return subprocess.run(command, **kwargs)


def host_profile_launch_agents():
    import plistlib
    from xml.parsers.expat import ExpatError
    for path in (Path.home() / 'Library/LaunchAgents').glob('*.plist'):
        body = path.read_bytes()
        try:
            yield path, plistlib.loads(body)
        except (plistlib.InvalidFileException, ExpatError):
            if b'cua' in body.lower() or 'cua' in path.name.lower():
                raise RuntimeError('Malformed CUA LaunchAgent requires owning repair: ' + str(path))
            # An unrelated malformed job cannot own a CUA executable or binding.
            continue


def host_profile_paths():
    home = Path.home()
    base = home / '.cua-driver' / 'host-control'
    if platform.system() == 'Darwin':
        theme = home / 'Library/Application Support/Cua Driver/cursor-themes'
        socket = str(home / 'Library/Caches/cua-driver/cua-driver.sock')
    else:
        theme = Path(os.environ.get('XDG_DATA_HOME', str(home / '.local/share'))) / 'cua-driver/cursor-themes'
        socket = str(Path(os.environ.get('XDG_CACHE_HOME', str(home / '.cache'))) / 'cua-driver/cua-driver.sock')
    return base, theme, socket


def host_profile_inspect():
    import hashlib
    base, _, _ = host_profile_paths()
    try:
        data = json.loads((base / 'profile.json').read_text())
        changed = [name for name, digest in data['sha256'].items()
                   if not Path(name).is_file() or hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest]
        for name, target in data.get('links', {}).items():
            if not Path(name).is_symlink() or str(Path(name).resolve()) != target:
                changed.append(name)
        if data.get('cursor_config'):
            saved = json.loads(Path(data['cursor_config']).read_text())
            effects = saved.get('cursor', {}).get('motion', {}).get('effects', {})
            if any(effects.get(key) is not False for key in ['glow', 'trail', 'magnet', 'ripple', 'squish']):
                changed.append(data['cursor_config'])
        return data, {'ready': not changed, 'changed': changed, 'id': data['id']}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, {'ready': False, 'error': str(exc)}


def host_profile_policy_digest(path):
    import hashlib
    name = path.name.encode()
    body = path.read_bytes()
    return hashlib.sha256(b'cua-driver-policy-v1\0' + len(name).to_bytes(8, 'big') + name
                          + len(body).to_bytes(8, 'big') + body).hexdigest()


def host_profile_endpoint(route, socket, policy, env=None):
    result = host_profile_run([str(route), 'status', '--socket', socket], env=env,
                              capture_output=True, text=True, timeout=10)
    if (result.returncode or 'permission mode: standard' not in result.stdout
            or 'managed policy: configured=true, active=true, valid=true' not in result.stdout
            or 'managed policy sha256: ' + host_profile_policy_digest(Path(policy)) not in result.stdout):
        return False
    # Read an unpositioned implicit cursor; never move it or send physical input.
    state = host_profile_run([str(route), 'call', 'get_agent_cursor_state', '{}', '--socket', socket], env=env,
                             capture_output=True, text=True, timeout=10)
    try:
        cursor = json.loads(state.stdout)
        theme = cursor['theme']
        effects = cursor['motion']['effects']
        return (state.returncode == 0 and theme.get('id') == 'io.botdoctor.cursor.quiet-gray'
                and theme.get('reduced_motion') == 'on' and theme.get('fallback') is None
                and all(effects.get(key) is False for key in ['glow', 'trail', 'magnet', 'ripple', 'squish']))
    except (ValueError, KeyError, TypeError):
        return False


def host_profile_external(previous, base, config):
    import hashlib
    checker = base / 'bin/cua-host-profile'
    if not checker.is_file():
        raise RuntimeError('Existing external profile has no inspectable admission command')
    proof = host_profile_run([str(checker)], capture_output=True, text=True, timeout=20)
    policies = [Path(name) for name in previous.get('sha256', {}) if name.endswith('.rego')]
    expected = hashlib.sha256(config['policy'].encode()).hexdigest()
    policy = next((path for path in policies if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected), None)
    _, _, socket = host_profile_paths()
    route = base / 'bin/cua-driver'
    theme_matches = any(name.endswith('.cua-theme') and digest == config['theme_sha256'] for name, digest in previous.get('sha256', {}).items())
    native_matches = any(digest in config['approved_binary_sha256'] for digest in previous.get('sha256', {}).values())
    ok = (proof.returncode == 0 and policy is not None and theme_matches and native_matches and host_profile_endpoint(route, socket, policy))
    return {'ok': ok, 'status': 'idempotent' if ok else 'failed', 'external_profile_preserved': True,
            'quiet_endpoint_verified': ok, 'proof': json.loads(proof.stdout)}


def host_profile_guard(args):
    import time
    data, check = host_profile_inspect()
    if args == ['--host-profile-status']:
        print(json.dumps(check))
        return 0 if check['ready'] else 1
    if not check['ready']:
        raise RuntimeError('CUA stopped before dispatch: verified binary/profile changed; review the exact update before admission: ' + json.dumps(check))
    args = list(args) or ['serve']
    command = args[0]
    if command == 'update' and '--apply' in args:
        raise RuntimeError('CUA update --apply refused: stage and verify the update through the owning host-profile release')
    if command == 'autostart' and any(x in args for x in ['enable', 'kick']):
        raise RuntimeError('Use the admitted quiet host service; native autostart would bypass its launcher')
    if command == 'permissions' and 'grant' in args:
        raise RuntimeError('Host control never opens permission onboarding; an explicit operator grant is required')
    if '--direct' in args:
        raise RuntimeError('Direct host runtime is not admitted; use the owning quiet endpoint with no alternate runtime fallback')
    if '--dangerously-bypass-approvals' in args or '--no-permissions-gate' in args:
        raise RuntimeError('The admitted host permission mode and input access must be preserved')
    def option(name):
        values = []
        for i, value in enumerate(args):
            if value == name:
                if i + 1 == len(args):
                    raise ValueError('missing ' + name)
                values.append(args[i + 1])
            elif value.startswith(name + '='):
                values.append(value.split('=', 1)[1])
        if len(set(values)) > 1:
            raise ValueError('conflicting repeated option ' + name)
        return values[0] if values else None
    if option('--socket') not in (None, data['socket']):
        raise RuntimeError('socket differs from the admitted host endpoint; no alternate daemon or desktop fallback')
    approved_commands = {'serve', 'mcp', 'call', 'manifest', 'status', 'sessions', 'stop',
                         '--version', '-v', '--help', '-h', 'help', 'list-tools', 'describe',
                         'check-update', 'permissions'}
    if command not in approved_commands:
        raise RuntimeError('Host command is not admitted: ' + command + '; use the guarded manifest, call, or MCP route; installer and new CLI routes require exact operator review')
    if command in {'serve', 'status', 'sessions', 'stop'} and option('--socket') is None:
        args += ['--socket', data['socket']]
    env = dict(os.environ, CUA_DRIVER_MANAGED_POLICY_FILE=data['policy'],
               CUA_DRIVER_RS_INSTALL_DIR=str(Path.home() / '.cua-driver/vendor-bin'))
    env = {key: value for key, value in env.items() if not SENSITIVE_ENV_RE.search(key)}
    env.setdefault('CUA_DRIVER_RS_TELEMETRY_ENABLED', '0')
    env.update(data.get('environment', {}))
    if platform.system() == 'Darwin':
        env['CUA_DRIVER_RS_PERMISSIONS_GATE'] = '0'
    if command == 'manifest':
        result = host_profile_run([data['route'], *args], env=env, capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        value['mcp_invocation'] = {'command': data['launcher'], 'args': ['mcp']}
        print(json.dumps(value))
        return 0
    if command in {'serve', 'mcp'}:
        for key, wanted in [('--cursor-theme', 'io.botdoctor.cursor.quiet-gray'), ('--cursor-reduced-motion', 'on')]:
            if option(key) not in (None, wanted):
                raise RuntimeError(key + ' differs from the admitted quiet profile')
            if option(key) is None:
                args += [key, wanted]
        if option('--permission-mode') not in (None, data['permission_mode']):
            raise RuntimeError('permission mode differs from the existing admitted host mode')
        if command == 'serve' and option('--permission-mode') is None:
            args += ['--permission-mode', data['permission_mode']]
    if command == 'serve' and platform.system() == 'Darwin':
        for key in ['CUA_DRIVER_MANAGED_POLICY_FILE', 'CUA_DRIVER_RS_INSTALL_DIR']:
            host_profile_run(['/bin/launchctl', 'setenv', key, env[key]], check=True)
    if command in {'call', 'mcp'}:
        socket = option('--socket') or data['socket']
        def admitted():
            return host_profile_endpoint(data['route'], socket, data['policy'], env)
        # Idle hosts without a user manager start only through an explicit guarded serve.
        # Preserving raw autolaunch here would restore the input/cursor bypass.
        if not admitted() and socket == data['socket'] and data.get('user_manager_available', True):
            host_profile_run(data['start_service'], capture_output=True, text=True, timeout=20)
            for _ in range(20):
                if admitted():
                    break
                time.sleep(.2)
        if not admitted():
            raise RuntimeError('CUA endpoint unavailable or lacks admitted policy; no foreground, raw-daemon, or desktop fallback: ' + socket + '; start the admitted serve route or restore its owning quiet service')
        if option('--socket') is None:
            args += ['--socket', socket]
    os.execve(data['route'], [data['route'], *args], env)


def host_profile_link(path, target):
    # Same-directory rename preserves either the old route or the admitted link.
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.cua-route-', dir=path.parent) as directory:
        staged = Path(directory) / 'cua-driver'
        staged.symlink_to(target)
        os.replace(staged, path)


def host_profile_install(python, home, route, config, *, dry_run=False):
    import base64
    import hashlib
    import shutil
    import shlex
    import tarfile
    import time
    import plistlib
    if platform.system() not in {'Darwin', 'Linux'}:
        raise RuntimeError('This host profile uses the POSIX owner route; Windows retains its private desktop bridge')
    base, theme_dir, socket = host_profile_paths()
    if platform.system() == 'Linux':
        runtime_dir = Path('/run/user') / str(os.getuid())
        if runtime_dir.is_dir() and runtime_dir.stat().st_uid == os.getuid():
            os.environ.setdefault('XDG_RUNTIME_DIR', str(runtime_dir))
            os.environ.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + str(runtime_dir / 'bus'))
    profile_file = base / 'profile.json'
    if profile_file.exists():
        previous = json.loads(profile_file.read_text())
        if 'route' not in previous:
            return host_profile_external(previous, base, config)
        if previous.get('payload_sha256') == hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            if previous.get('contract_sha256') != hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest():
                raise RuntimeError('Requested host contract differs from the admitted contract; explicit reviewed migration required')
            if previous.get('phase') != 'complete':
                if dry_run:
                    return {'ok': True, 'status': 'would_install', 'pending_activation': True}
                for name, destination in previous.get('links', {}).items():
                    path = Path(name)
                    if not path.exists() and not path.is_symlink() and name in previous.get('recovery_absent', []):
                        host_profile_link(path, destination)
                    elif path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == previous['prior_route_sha256']:
                        host_profile_link(path, destination)
            _, current = host_profile_inspect()
            if not current['ready']:
                raise RuntimeError('Existing profile changed; automatic re-admission is forbidden: ' + json.dumps(current))
            if previous.get('phase') != 'complete':
                return host_profile_activate(python, home, previous)
            return {'ok': True, 'status': 'idempotent', 'profile': current}
        raise RuntimeError('Existing host-profile payload differs; require an exact reviewed profile migration')
    selected = Path(route).expanduser()
    original = selected
    if original.parent != Path.home() / '.local/bin':
        alias = Path.home() / '.local/bin/cua-driver'
        if alias.exists() or alias.is_symlink():
            if not alias.is_file() or alias.resolve() != selected.resolve():
                raise RuntimeError('Existing per-user alias differs from the selected native route; preserve both for owning review')
        original = alias
    alias_missing = not original.exists() and not original.is_symlink()
    resolved = selected.resolve() if alias_missing else original.resolve()
    # New native package installations need an admitted alias, never a rewritten package.
    route_bytes = resolved.read_bytes() if alias_missing else original.read_bytes()
    if alias_missing and route_bytes.startswith(b'#!'):
        raise RuntimeError('External adapter without a native host alias requires its owning route; no local fallback')
    if hashlib.sha256(route_bytes).hexdigest() not in config['approved_route_sha256']:
        raise RuntimeError('Existing route bytes are not admitted by this release: ' + str(original))
    manifest = host_profile_run([str(selected if alias_missing else original), 'manifest'], capture_output=True, text=True, timeout=15)
    if manifest.returncode:
        raise RuntimeError('Owning route cannot report its native manifest; no raw fallback')
    native_manifest = json.loads(manifest.stdout)
    raw = Path(native_manifest['binary_path']).resolve()
    if not raw.is_file():
        raise RuntimeError('Manifest native binary is unavailable: ' + str(raw))
    script_route = raw != resolved
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    if digest not in config['approved_binary_sha256']:
        raise RuntimeError('Installed native binary is not admitted by this exact release: ' + str(raw) + ' sha256=' + digest)
    before = host_profile_run([str(raw), 'status'], capture_output=True, text=True, timeout=20)
    status_text = before.stdout + before.stderr
    if 'Access is denied' in status_text or 'Permission denied' in status_text:
        raise RuntimeError('Existing desktop endpoint denied this identity; preserve its owner boundary')
    running = before.returncode == 0 and 'daemon is running' in before.stdout
    if running:
        sessions = host_profile_run([str(raw), 'sessions'], capture_output=True, text=True, timeout=20)
        if sessions.returncode != 0 or 'No live sessions' not in sessions.stdout:
            raise RuntimeError('Existing CUA sessions are active or unverified; preserve them before service replacement')
    mode_match = re.search(r'permission mode: (standard|bounded|unrestricted)', before.stdout)
    mode = mode_match.group(1) if mode_match else 'standard'
    if mode != 'standard':
        raise RuntimeError('Existing non-standard permission/grant binding requires exact host review before replacement')
    if platform.system() == 'Linux':
        manager = (host_profile_run(['systemctl', '--user', 'show-environment'], capture_output=True, text=True, timeout=15)
                   if shutil.which('systemctl') else subprocess.CompletedProcess([], 1, '', 'systemctl is unavailable'))
        if manager.returncode and running:
            raise RuntimeError('Owning systemd user manager unavailable; existing live CUA route left intact: ' + manager.stderr[-300:])
    elif platform.system() == 'Darwin':
        manager = host_profile_run(['/bin/launchctl', 'print', 'gui/' + str(os.getuid())], capture_output=True, text=True, timeout=15)
        if manager.returncode:
            raise RuntimeError('Owning Aqua launch domain unavailable; existing CUA route left intact')
    # Existing supervisors must be transferred by their owning operator route.
    # Refuse before mutation rather than race an old native autostart or idle service.
    supervisors = []
    guarded_supervisors = []
    stable_launcher = str(base / 'bin/cua-driver')
    if platform.system() == 'Darwin':
        for item, payload in host_profile_launch_agents():
            argv = payload.get('ProgramArguments', [])
            executable = payload.get('Program', argv[0] if argv else '')
            if executable == stable_launcher:
                guarded_supervisors.append(item)
            elif 'cua-driver' in Path(executable).name and '--private-desktop-session' not in argv:
                supervisors.append(str(item))
    elif platform.system() == 'Linux':
        for folder in [Path.home() / '.config/systemd/user', Path.home() / '.local/share/systemd/user', Path('/etc/systemd/user'), Path('/usr/lib/systemd/user')]:
            for item in folder.glob('*.service'):
                body = item.read_text()
                starts = [line for line in body.splitlines() if line.startswith('ExecStart=') and 'cua-driver' in line and '--private-desktop-session' not in line]
                if starts and all(stable_launcher in line for line in starts):
                    guarded_supervisors.append(item)
                elif starts:
                    supervisors.append(str(item))
    if supervisors:
        raise RuntimeError('Existing CUA lifecycle must be transferred before host cutover; no route changed: ' + json.dumps(supervisors))
    if len(guarded_supervisors) > 1:
        raise RuntimeError('Multiple guarded service scopes require their owning operator binding; no route changed')
    service_file = guarded_supervisors[0] if guarded_supervisors else None
    service_label = None
    if service_file is not None:
        if platform.system() == 'Darwin':
            settings = plistlib.loads(service_file.read_bytes())
            service_label = settings['Label']
            service_args = settings['ProgramArguments']
        else:
            service_label = service_file.name
            starts = [line.split('=', 1)[1] for line in service_file.read_text().splitlines() if line.startswith('ExecStart=')]
            service_args = shlex.split(starts[0]) if len(starts) == 1 else []
        if not re.fullmatch(r'[A-Za-z0-9._-]+', service_label) or not service_args or service_args[0] != stable_launcher or 'serve' not in service_args:
            raise RuntimeError('Existing guarded service is not an exact inspectable serve binding')
        for key, expected in [('--socket', socket), ('--permission-mode', mode), ('--cursor-theme', 'io.botdoctor.cursor.quiet-gray'), ('--cursor-reduced-motion', 'on')]:
            values = [value.split('=', 1)[1] for value in service_args if value.startswith(key + '=')]
            values += [service_args[index + 1] for index, value in enumerate(service_args[:-1]) if value == key]
            if any(value != expected for value in values):
                raise RuntimeError('Existing guarded service differs from this default host scope: ' + key)
    # Keep the host guard independent of a disposable Hermes runtime candidate.
    guard_python = Path('/usr/bin/python3')
    if not guard_python.is_file():
        raise RuntimeError('Stable OS Python is unavailable; preserve the owning route for explicit host admission')
    if platform.system() == 'Darwin' and host_profile_run(['/usr/bin/xcode-select', '-p'], capture_output=True, text=True).returncode:
        raise RuntimeError('Native Python developer tools are unavailable; never open installer onboarding')
    runtime_check = host_profile_run([str(guard_python), '-c', 'import sys; print(int(sys.version_info >= (3, 9)))'], capture_output=True, text=True, timeout=15)
    if runtime_check.returncode or runtime_check.stdout.strip() != '1':
        raise RuntimeError('The existing stable OS Python must support the admitted host guard')
    if dry_run:
        return {'ok': True, 'status': 'would_install', 'native': str(raw), 'native_sha256': digest,
                'running': running, 'route': str(original)}
    cursor_config = Path.home() / '.cua-driver/config.json'
    saved_config = json.loads(cursor_config.read_text()) if cursor_config.exists() else {}
    if not isinstance(saved_config, dict) or not isinstance(saved_config.get('cursor', {}), dict) or not isinstance(saved_config.get('cursor', {}).get('motion', {}), dict):
        raise RuntimeError('Existing native cursor configuration is not an object; preserve it for owning review')
    # Register the private recovery producer before writing any prior file data.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    recovery = home / 'state/cua-host-profile-recovery' / (stamp + '.tar.gz')
    installed_retention = home / 'bin/hermes-disk-retention.py'
    if not installed_retention.is_file() and home.parent.name == 'profiles':
        installed_retention = home.parent.parent / 'bin/hermes-disk-retention.py'
    retention = None
    # Reuse the exact Golden source producer when an older installed CLI lacks registration.
    # This changes no host retention service, schedule, or cleanup policy.
    for producer in dict.fromkeys([installed_retention, ROOT / 'bin/hermes-disk-retention.py']):
        if not producer.is_file():
            continue
        capability = host_profile_run([str(python), str(producer), '--help'], capture_output=True, text=True, timeout=20)
        if capability.returncode == 0 and '--register-artifact' in capability.stdout:
            retention = producer
            break
    if retention is None:
        raise RuntimeError('Owning recovery producer cannot register artifacts; no CUA route changed')
    recovery.parent.mkdir(parents=True, exist_ok=True)
    # Reserve only an empty private inode; register it before writing recovery data.
    fd = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    registration = host_profile_run([str(python), str(retention), '--hermes-home', str(home), '--register-artifact', str(recovery),
                                   '--artifact-owner', 'cua-host-profile', '--artifact-kind', 'recovery',
                                   '--artifact-ttl', '604800', '--artifact-budget', str(len(route_bytes) + 4 * 1024 * 1024)],
                                  capture_output=True, text=True, timeout=30)
    if registration.returncode:
        raise RuntimeError('Recovery registration refused: ' + registration.stderr[-500:])
    base.mkdir(parents=True, exist_ok=True)
    recovery.parent.mkdir(parents=True, exist_ok=True)
    paths = [original, home / '.env', Path.home() / '.profile', Path.home() / '.zshenv',
             Path.home() / '.zprofile', Path.home() / '.zshrc', Path.home() / '.bashrc', Path.home() / '.bash_profile', cursor_config]
    if platform.system() == 'Darwin':
        paths += list((Path.home() / 'Library/LaunchAgents').glob('*.plist'))
    fd = os.open(recovery, os.O_WRONLY)
    with os.fdopen(fd, 'wb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for path in paths:
            if path.exists() or path.is_symlink():
                archive.add(path, arcname=str(path).lstrip('/\\'), recursive=False)
    missing_before = [str(path) for path in paths if not path.exists() and not path.is_symlink()]
    # Prove the complete archive is readable before changing the prior route.
    with tarfile.open(recovery) as archive:
        for item in archive:
            if item.isfile():
                archive.extractfile(item).read()
    motion = saved_config.setdefault('cursor', {}).setdefault('motion', {})
    motion['effects'] = dict.fromkeys(['glow', 'trail', 'magnet', 'ripple', 'squish'], False)
    cursor_config.write_text(json.dumps(saved_config, indent=2) + '\n')
    binary_dir = base / 'bin'
    binary_dir.mkdir(exist_ok=True)
    native_route = resolved
    if (script_route or original == resolved):
        # Plain native launchers and existing wrapper scripts need stable, distinct paths.
        native_route = base / 'vendor' / 'cua-driver'
        native_route.parent.mkdir(exist_ok=True)
        shutil.copy2(resolved, native_route)
        sidecar = resolved.with_name('cua-cursor-theme')
        if sidecar.is_file():
            shutil.copy2(sidecar, native_route.parent / sidecar.name)
    if native_route != resolved:
        native_route.chmod(0o755)
    script = binary_dir / 'host-control.py'
    shutil.copy2(Path(__file__), script)
    policy = base / 'background-only.rego'
    policy.parent.mkdir(exist_ok=True)
    policy.write_text(config['policy'])
    theme_dir.mkdir(parents=True, exist_ok=True)
    theme = theme_dir / 'io.botdoctor.cursor.quiet-gray.cua-theme'
    theme.write_bytes(base64.b64decode(config['theme_base64'], validate=True))
    if hashlib.sha256(theme.read_bytes()).hexdigest() != config['theme_sha256']:
        raise RuntimeError('Theme payload digest mismatch')
    launcher = binary_dir / 'cua-driver'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(str(guard_python)) + ' ' + shlex.quote(str(script)) + ' --host-guard "$@"\n')
    launcher.chmod(0o755)
    pins = {str(raw): digest, str(native_route): hashlib.sha256(native_route.read_bytes()).hexdigest(),
            str(script): hashlib.sha256(script.read_bytes()).hexdigest(), str(policy): hashlib.sha256(policy.read_bytes()).hexdigest(),
            str(theme): config['theme_sha256'], str(launcher): hashlib.sha256(launcher.read_bytes()).hexdigest()}
    preflight = binary_dir / 'cua-host-profile'
    preflight.write_text('#!/bin/sh\nexec ' + shlex.quote(str(guard_python)) + ' ' + shlex.quote(str(script)) + ' --host-guard --host-profile-status\n')
    preflight.chmod(0o755)
    pins[str(preflight)] = hashlib.sha256(preflight.read_bytes()).hexdigest()
    pins[str(guard_python)] = hashlib.sha256(guard_python.read_bytes()).hexdigest()
    pins.update({str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in guarded_supervisors})
    # When the original file is replaced by our shim, pin its preserved native bytes instead.
    if raw == original:
        pins.pop(str(raw), None)
    links = {}
    links[str(original)] = str(launcher)
    data = {'id': 'fleet-quiet-background-20261022', 'route': str(native_route), 'native': str(raw),
            'launcher': str(launcher), 'original_route': str(original), 'socket': socket, 'policy': str(policy), 'permission_mode': mode,
            'sha256': pins, 'links': links, 'payload_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'contract_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            'recovery': str(recovery), 'recovery_producer': str(retention), 'recovery_producer_sha256': hashlib.sha256(retention.read_bytes()).hexdigest(), 'recovery_absent': missing_before, 'cursor_config': str(cursor_config), 'start_service': [], 'phase': 'installing',
            'guard_python': str(guard_python), 'was_running': running, 'script_route': script_route, 'user_manager_available': platform.system() != 'Linux' or manager.returncode == 0, 'native_sha256': digest, 'prior_route_sha256': hashlib.sha256(route_bytes).hexdigest()}
    # Preserve selected existing display binding for native Linux services.
    desktop_env = {key: os.environ[key] for key in ['DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'] if key in os.environ} if not script_route else {}
    if platform.system() == 'Linux' and running and not script_route:
        values = []
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if directory.stat().st_uid != os.getuid() or not (directory / 'exe').resolve().name.startswith('cua-driver'):
                    continue
                value = dict(item.split(b'=', 1) for item in (directory / 'environ').read_bytes().split(b'\0') if b'=' in item)
                selected = {key: value[key.encode()].decode() for key in ['DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'] if key.encode() in value}
                if selected and selected not in values:
                    values.append(selected)
            except (OSError, ValueError):
                continue
        if len(values) > 1:
            raise RuntimeError('Multiple native desktop bindings exist; cannot select one without its owning route')
        if values:
            desktop_env = values[0]
    if platform.system() == 'Linux':
        data['environment'] = {'XDG_DATA_HOME': str(theme_dir.parents[1]), 'XDG_CACHE_HOME': str(Path(socket).parents[1])}
        desktop_env.update(data['environment'])
    arguments = [str(launcher), 'serve', '--socket', socket, '--permission-mode', mode,
                 '--cursor-theme', 'io.botdoctor.cursor.quiet-gray', '--cursor-reduced-motion', 'on']
    if service_file is not None:
        data['service_reused'] = True
        data['service_file'] = str(service_file)
        data['service_label'] = service_label
        data['start_service'] = (['/bin/launchctl', 'kickstart', 'gui/' + str(os.getuid()) + '/' + service_label] if platform.system() == 'Darwin' else ['systemctl', '--user', 'start', service_label])
    elif platform.system() == 'Darwin':
        label = 'com.botdoctor.cua-quiet-host'
        plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': arguments, 'RunAtLoad': True,
            'KeepAlive': True, 'ThrottleInterval': 60, 'LimitLoadToSessionType': 'Aqua',
            'EnvironmentVariables': {'CUA_DRIVER_RS_PERMISSIONS_GATE': '0', 'CUA_DRIVER_RS_TELEMETRY_ENABLED': os.environ.get('CUA_DRIVER_RS_TELEMETRY_ENABLED', '0'), 'CUA_DRIVER_MANAGED_POLICY_FILE': str(policy),
                                     'CUA_DRIVER_RS_INSTALL_DIR': str(Path.home() / '.cua-driver/vendor-bin')},
            'StandardOutPath': str(base / 'service.log'), 'StandardErrorPath': str(base / 'service.err')}))
        pins[str(plist)] = hashlib.sha256(plist.read_bytes()).hexdigest()
        data['start_service'] = ['/bin/launchctl', 'kickstart', 'gui/' + str(os.getuid()) + '/' + label]
    elif os.name == 'nt':
        data['start_service'] = ['schtasks.exe', '/Run', '/TN', 'CuaQuietHost']
    else:
        unit = Path.home() / '.config/systemd/user/cua-quiet-host.service'
        unit.parent.mkdir(parents=True, exist_ok=True)
        desktop_env.setdefault('CUA_DRIVER_RS_TELEMETRY_ENABLED', os.environ.get('CUA_DRIVER_RS_TELEMETRY_ENABLED', '0'))
        env_lines = ''.join('Environment=' + shlex.quote(key + '=' + value) + '\n' for key, value in desktop_env.items())
        unit.write_text('[Unit]\nDescription=Quiet background-only CUA host\n[Service]\nType=simple\n' + env_lines +
                        'ExecStart=' + shlex.join(arguments) + '\nRestart=no\n[Install]\nWantedBy=default.target\n')
        pins[str(unit)] = hashlib.sha256(unit.read_bytes()).hexdigest()
        data['start_service'] = ['systemctl', '--user', 'start', 'cua-quiet-host.service']
    profile_file.write_text(json.dumps(data, indent=2) + '\n')
    for name, target in links.items():
        path = Path(name)
        host_profile_link(path, target)
    return host_profile_activate(python, home, data)


def host_profile_activate(python, home, data):
    import hashlib, plistlib, shlex, time
    base, _, _ = host_profile_paths()
    if platform.system() == 'Linux':
        os.environ.setdefault('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
        os.environ.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/' + str(os.getuid()) + '/bus')
    policy = Path(data['policy'])
    launcher = Path(data['launcher'])
    binary_dir = launcher.parent
    recovery = Path(data['recovery'])
    native_route = Path(data['route'])
    socket = data['socket']
    mode = data['permission_mode']
    profile_file = base / 'profile.json'
    running = data['was_running']
    arguments = [str(launcher), 'serve', '--socket', socket, '--permission-mode', mode,
                 '--cursor-theme', 'io.botdoctor.cursor.quiet-gray', '--cursor-reduced-motion', 'on']
    label = data.get('service_label', 'com.botdoctor.cua-quiet-host')
    plist = Path(data.get('service_file', str(Path.home() / 'Library/LaunchAgents' / (label + '.plist'))))
    # Existing native/default route now resolves the guard. Configure future host entry points.
    env_line = 'HERMES_CUA_DRIVER_CMD=' + str(launcher)
    env_file = home / '.env'
    env_text = env_file.read_text() if env_file.exists() else ''
    existing = re.search(r'^HERMES_CUA_DRIVER_CMD=(.*)$', env_text, re.M)
    local_entries = {'cua-driver', 'cua-driver.exe', data['original_route'], data['native'], data['route'], data['launcher']}
    remote_bridge = bool(existing and existing.group(1).strip().strip('\"\'') not in local_entries)
    if platform.system() == 'Darwin':
        for item, settings in host_profile_launch_agents():
            value = settings.get('EnvironmentVariables', {}).get('HERMES_CUA_DRIVER_CMD', '')
            remote_bridge = remote_bridge or bool(value and value not in local_entries)
    if not remote_bridge and os.name != 'nt':
        updated = re.sub(r'^HERMES_CUA_DRIVER_CMD=.*$', lambda match: env_line, env_text, flags=re.M) if existing else env_text.rstrip() + '\n' + env_line + '\n'
        try:
            env_file.write_text(updated)
        except PermissionError:
            if platform.system() != 'Darwin':
                raise
    path_line = '\n# CUA quiet host admission\nexport PATH="' + str(binary_dir) + ':$PATH"\nexport CUA_DRIVER_MANAGED_POLICY_FILE="' + str(policy) + '"\nexport CUA_DRIVER_RS_INSTALL_DIR="$HOME/.cua-driver/vendor-bin"\n'
    for name in ['.profile', '.zshenv', '.zprofile', '.zshrc', '.bashrc'] + (['.bash_profile'] if (Path.home() / '.bash_profile').exists() else []):
        p = Path.home() / name
        old = p.read_text() if p.exists() else ''
        if '# CUA quiet host admission' not in old:
            p.write_text(old + path_line)
    if platform.system() == 'Darwin':
        for p, payload in host_profile_launch_agents():
            if not ('hermes' in p.name.lower() and 'gateway' in p.name.lower()) or p.name == plist.name:
                continue
            environment = payload.setdefault('EnvironmentVariables', {})
            current = environment.get('HERMES_CUA_DRIVER_CMD', '')
            if not current or current in local_entries:
                environment['HERMES_CUA_DRIVER_CMD'] = str(launcher)
            environment['CUA_DRIVER_MANAGED_POLICY_FILE'] = str(policy)
            environment['CUA_DRIVER_RS_INSTALL_DIR'] = str(Path.home() / '.cua-driver/vendor-bin')
            p.write_bytes(plistlib.dumps(payload))
        host_profile_run(['/bin/launchctl', 'setenv', 'CUA_DRIVER_MANAGED_POLICY_FILE', str(policy)], check=True)
    native_status = host_profile_run([str(native_route), 'status'], capture_output=True, text=True, timeout=20)
    already_protected = host_profile_endpoint(native_route, socket, policy)
    currently_running = native_status.returncode == 0 and 'daemon is running' in native_status.stdout
    if currently_running and not already_protected:
        sessions = host_profile_run([str(native_route), 'sessions'], capture_output=True, text=True, timeout=20)
        if sessions.returncode or 'No live sessions' not in sessions.stdout:
            raise RuntimeError('CUA became active before cutover; preserve those sessions and resume later')
        stopped = host_profile_run([str(native_route), 'stop'], capture_output=True, text=True, timeout=20)
        if stopped.returncode:
            raise RuntimeError('Existing idle CUA daemon did not stop normally: ' + stopped.stderr[-300:])
        time.sleep(2)
    if platform.system() == 'Darwin':
        loaded = host_profile_run(['/bin/launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + label], capture_output=True, text=True)
        if loaded.returncode:
            host_profile_run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(plist)], check=True, capture_output=True, text=True)
        elif not already_protected:
            host_profile_run(data['start_service'], check=True, capture_output=True, text=True)
    elif data.get('user_manager_available', True):
        host_profile_run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True, text=True)
        host_profile_run(['systemctl', '--user', 'enable', data.get('service_label', 'cua-quiet-host.service')], check=True, capture_output=True, text=True)
        if running or data.get('service_reused') or data['script_route'] or os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
            host_profile_run(data['start_service'], check=True, capture_output=True, text=True)
    _, check = host_profile_inspect()
    if not check['ready']:
        raise RuntimeError('Installed profile did not pass exact file admission: ' + json.dumps(check))
    # Direct status is read-only and does not launch or capture a desktop.
    activation_expected = (running or platform.system() == 'Darwin' or
                           (data.get('user_manager_available', True) and
                            (data.get('service_reused') or data['script_route'] or
                             os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))))
    active = False
    for _ in range(20 if activation_expected else 1):
        after = host_profile_run([str(native_route), 'status'], capture_output=True, text=True, timeout=20)
        active = host_profile_endpoint(native_route, socket, policy)
        if active:
            break
        time.sleep(.25)
    if activation_expected and not active:
        raise RuntimeError('Requested host service did not activate its managed quiet profile: ' + after.stdout[-500:] + after.stderr[-500:])
    data['phase'] = 'complete'
    data['managed_policy_active'] = active
    profile_file.write_text(json.dumps(data, indent=2) + '\n')
    return {'ok': True, 'status': 'installed', 'profile': check, 'native_sha256': data['native_sha256'],
            'managed_policy_active': active, 'idle_guarded': not active and not running,
            'recovery': str(recovery), 'default_socket': socket, 'launcher': str(launcher)}



def ensure_host_driver_profile(python, home, contract_path, *, dry_run=False, require_ready=False):
    # Preserve the native version installer API; host admission is the CLI's outer boundary.
    contract = load_contract(contract_path)
    profile_contract = contract.get('host_profile')
    if not profile_contract:
        return ensure_driver(python=python, home=home, contract_path=contract_path,
                             dry_run=dry_run, require_ready=require_ready)
    if platform_asset_key().split('-')[0] not in profile_contract['enabled_platforms']:
        code, receipt = ensure_driver(python=python, home=home, contract_path=contract_path, dry_run=dry_run, require_ready=require_ready)
        receipt['host_profile'] = {'ok': False, 'status': 'pending_platform_binding', 'platform': platform.system()}
        return code, receipt
    env = child_env(home, contract)
    profile_path = Path.home() / '.cua-driver/host-control/profile.json'
    if contract.get('host_profile') and profile_path.exists():
        existing_profile = json.loads(profile_path.read_text())
        if 'route' not in existing_profile:
            proof = host_profile_external(existing_profile, profile_path.parent, contract['host_profile'])
            if not proof['ok']:
                raise RuntimeError('Existing protected profile changed; native installation/re-admission refused')
            # An admitted external/native app is already installed; never rediscover a raw PM route.
            env['HERMES_CUA_DRIVER_CMD'] = str(profile_path.parent / 'bin/cua-driver')
        else:
            if existing_profile.get('phase') != 'complete':
                host_profile_install(python, home, existing_profile['route'], contract['host_profile'], dry_run=dry_run)
            _, proof = host_profile_inspect()
            if not proof['ready']:
                raise RuntimeError('Existing admitted files changed; native installation/re-admission refused: ' + json.dumps(proof))
    before = probe_driver(python, env=env)
    if before.get('probe_error'):
        raise RuntimeError('Owning CUA route discovery failed; no native installer fallback: ' + before['probe_error'])
    if before.get('installed') and before.get('version') not in profile_contract['compatible_versions']:
        raise RuntimeError('Installed CUA version is not admitted; no automatic downgrade or repin')
    if not before.get('installed'):
        code, receipt = ensure_driver(python=python, home=home, contract_path=contract_path,
                                      dry_run=dry_run, require_ready=False, diagnose=False)
        if code or dry_run:
            return code, receipt
        after = receipt['after']
    else:
        after = before
        receipt = {'schema_version': 1, 'kind': 'cua_driver_install_receipt',
                   'generated_at': utc_now(), 'hermes_home': str(home), 'hermes_python': str(python),
                   'release': contract['release'], 'before': before, 'after': after,
                   'install_attempted': False, 'dry_run': dry_run}
    route = after.get('path')
    local_alias = Path.home() / '.local/bin/cua-driver'
    if os.name != 'nt' and local_alias.is_file():
        # The authorized cohort includes the default host endpoint even when Hermes uses an external bridge.
        # Verify that route independently below and preserve the configured external bridge during activation.
        route = str(local_alias)
    profile = host_profile_install(python, home, route, profile_contract, dry_run=dry_run)
    if dry_run:
        receipt.update({'ok': profile.get('ok') is True, 'status': profile['status'], 'host_profile': profile, 'doctor_ready': None})
        return (0 if receipt['ok'] else 1), receipt
    # Diagnostics must use the newly bound route, including an existing external bridge.
    bound = re.search(r'^HERMES_CUA_DRIVER_CMD=(.*)$', (home / '.env').read_text() if (home / '.env').exists() else '', re.M)
    env['HERMES_CUA_DRIVER_CMD'] = bound.group(1).strip().strip('\"\'') if bound else str(profile_path.parent / 'bin/cua-driver')
    doctor = run([str(python), '-c', DOCTOR_CODE], env=env, timeout=120)
    doctor_payload = parse_doctor(doctor.stdout)
    ready = (doctor.returncode == 0 and isinstance(doctor_payload, dict)
             and doctor_payload.get('ok') is not False
             and doctor_payload.get('overall') not in {'degraded', 'error', 'failed'})
    ok = profile.get('ok') is True and (not require_ready or ready)
    receipt.update({'ok': ok, 'status': profile['status'] if ok else 'blocked_not_ready',
                    'require_ready': require_ready, 'host_profile': profile,
                    'doctor_ready': ready, 'doctor_returncode': doctor.returncode, 'doctor': doctor_payload})
    return (0 if ok else 1), receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-python", required=True, type=Path)
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        code, receipt = ensure_host_driver_profile(
            python=args.hermes_python.expanduser(),
            home=args.hermes_home.expanduser(),
            contract_path=args.contract.expanduser(),
            dry_run=args.dry_run,
            require_ready=args.require_ready,
        )
    except Exception as exc:
        code = 1
        receipt = {
            "schema_version": 1,
            "kind": "cua_driver_install_receipt",
            "generated_at": utc_now(),
            "ok": False,
            "status": "failed",
            "error": str(exc),
        }

    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    if sys.argv[1:2] == ['--host-guard']:
        try:
            raise SystemExit(host_profile_guard(sys.argv[2:]))
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
            print('cua-host-profile: ' + str(error), file=sys.stderr)
            raise SystemExit(1)
    raise SystemExit(main())
