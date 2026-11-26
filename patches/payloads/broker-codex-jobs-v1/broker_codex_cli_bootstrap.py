"""Run vendor Codex with an ephemeral, tenant-local broker transport."""
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import sys
import tomllib


def _vendor():
    """Skip managed head shims; preserve the installed vendor executable."""
    for folder in os.get_exec_path():
        root = Path(folder)
        for name in (('codex.exe', 'codex.cmd') if os.name == 'nt' else ('codex',)):
            path = root / name
            if not path.is_file():
                continue
            resolved = path.resolve()
            if name.endswith('.cmd'):
                script = root / 'node_modules/@openai/codex/bin/codex.js'
                node = shutil.which('node')
                if script.is_file() and node:
                    return [node, str(script)]
                continue
            if resolved.name == 'codex.js':
                node = shutil.which('node')
                if node:
                    return [node, str(resolved)]
            elif resolved.name in {'codex', 'codex.exe'}:
                # Managed shims carry a fixed marker, not a vendor path guess.
                with resolved.open('rb') as stream:
                    if b'HERMES_BROKER_CODEX_CLI_v1' in stream.read(2048):
                        continue
                if os.access(path, os.X_OK):
                    return [str(path)]
    raise RuntimeError('The installed vendor Codex executable is unavailable')


def _delegate(vendor, args):
    if os.name == 'nt':
        raise SystemExit(subprocess.call([*vendor, *args], env=os.environ))
    os.execvpe(vendor[0], [*vendor, *args], os.environ)


def _merge(config, layer):
    for key, value in layer.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            _merge(config[key], value)
        else:
            config[key] = value


def _command_index(args):
    valued = {'-c', '--config', '-p', '--profile', '-C', '--cd', '-m', '--model',
              '-s', '--sandbox', '-a', '--ask-for-approval', '-i', '--image', '--add-dir',
              '--enable', '--disable', '--local-provider'}
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
        elif arg in valued:
            skip = True
        elif arg == '--':
            return None
        elif not arg.startswith('-'):
            return index
    return None


def _without_provider_override(args):
    kept = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            kept.extend(args[index:])
            break
        value = (args[index + 1] if arg in {'-c', '--config'} and index + 1 < len(args)
                 else arg.split('=', 1)[1] if arg.startswith('--config=')
                 else arg[2:].removeprefix('=') if arg.startswith('-c') and len(arg) > 2 else None)
        if value is not None and value.split('=', 1)[0].strip() == 'model_provider':
            index += 2 if arg in {'-c', '--config'} else 1
            continue
        kept.append(arg)
        index += 1
    return kept


def _toml(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + _toml(child)
                              for key, child in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_toml(child) for child in value) + ']'
    return json.dumps(value)


def _configuration(args, codex_home):
    """Inspect provider overrides without rewriting config or touching logins."""
    files = [] if '--ignore-user-config' in args else [codex_home / 'config.toml']
    cwd = Path.cwd()
    for i, arg in enumerate(args):
        if arg == '--':
            break
        if arg in {'-C', '--cd'} and i + 1 < len(args):
            cwd = Path(args[i + 1]).expanduser().absolute()
        elif arg.startswith('--cd='):
            cwd = Path(arg.split('=', 1)[1]).expanduser().absolute()
    # Conservatively honor project provider selections even before trust admission.
    files.extend(p / '.codex/config.toml' for p in reversed([cwd, *cwd.parents]))
    layers = []
    for path in files:
        if path.is_file():
            layers.append(tomllib.loads(path.read_text()))
    profile = None
    overrides = []
    for i, arg in enumerate(args):
        if arg == '--':
            break
        if arg in {'--oss', '--local-provider'} or arg.startswith('--local-provider='):
            return None
        if arg in {'-p', '--profile'} and i + 1 < len(args):
            profile = args[i + 1]
        elif arg.startswith('--profile='):
            profile = arg.split('=', 1)[1]
        elif arg in {'-c', '--config'} and i + 1 < len(args):
            overrides.append(args[i + 1])
        elif arg.startswith('--config='):
            overrides.append(arg.split('=', 1)[1])
        elif arg.startswith('-c') and len(arg) > 2:
            overrides.append(arg[2:].removeprefix('='))
    config = {}
    for layer in layers:
        _merge(config, layer)
    if profile:
        profile_file = codex_home / (profile + '.config.toml')
        if profile_file.is_file():
            _merge(config, tomllib.loads(profile_file.read_text()))
        for layer in layers:
            _merge(config, layer.get('profiles', {}).get(profile, {}))
    for override in overrides:
        if '=' not in override:
            return None
        path, raw = override.split('=', 1)
        try:
            value = tomllib.loads('value=' + raw)['value']
        except tomllib.TOMLDecodeError:
            value = raw
        layer = value
        for part in reversed(path.strip().split('.')):
            if not part or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for character in part):
                return None
            layer = {part: layer}
        _merge(config, layer)
    provider = config.get('model_provider', 'openai')
    if provider != 'openai' or config.get('openai_base_url'):
        return None
    info = config.get('model_providers', {}).get('openai', {})
    if info.get('base_url') or info.get('env_key'):
        return None
    return config


def prepare_worker_path():
    """Expose the managed worker command only inside the Hermes process tree."""
    runtime = Path(__file__).resolve().parents[1]
    head = (runtime.parent.parent.parent if runtime.parent.parent.name == 'state'
            else runtime.parent.parent) if runtime.parent.name == 'runtime-candidates' else runtime.parent
    if not (head / 'state/runtime-binding.json').is_file():
        return
    folders = [str(runtime / 'agent/broker_cli_bin')]
    folders.extend(os.get_exec_path())
    os.environ['PATH'] = os.pathsep.join(dict.fromkeys(folders))


def main():
    args = sys.argv[1:]
    vendor = _vendor()
    if not os.environ.get('CODEX_HOME'):
        from dotenv import dotenv_values
        worker = Path(os.environ.get('HERMES_HOME', os.environ['HERMES_BROKER_HOME']))
        configured = (dotenv_values(worker / '.env').get('CODEX_HOME')
                      or dotenv_values(Path(os.environ['HERMES_BROKER_HOME']) / '.env').get('CODEX_HOME'))
        if configured:
            os.environ['CODEX_HOME'] = configured
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    # CODEX_API_KEY selects vendor API auth; OPENAI_API_KEY may serve other Hermes tools.
    # API/custom/local routes and account-management commands retain vendor behavior.
    administrative = {'login', 'logout', 'completion', 'mcp',
                      'debug', 'features', 'help', 'cloud'}
    command_index = _command_index(args)
    command = args[command_index] if command_index is not None else None
    control_args = args[:args.index('--')] if '--' in args else args
    if (command in administrative or '--help' in control_args or '-h' in control_args
            or '--version' in control_args or '-V' in control_args
            or '--oss' in control_args or '--local-provider' in control_args
            or any(value.startswith('--local-provider=') for value in control_args)
            or os.environ.get('OPENAI_BASE_URL') or os.environ.get('HERMES_BROKER_PROXY_TOKEN')
            or any(os.environ.get(key) for key in ('CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'))):
        _delegate(vendor, args)
    config = _configuration(args, codex_home)
    if config is None:
        _delegate(vendor, args)
    try:
        auth = json.loads((codex_home / 'auth.json').read_text())
    except (OSError, ValueError):
        _delegate(vendor, args)
    if (auth.get('OPENAI_API_KEY') or auth.get('auth_mode') not in {None, 'chatgpt'}
            or auth.get('tokens', {}).get('refresh_token') != 'dummy_broker_managed'):
        _delegate(vendor, args)
    from agent.broker_codex_cli import transport
    info = config.get('model_providers', {}).get('openai', {})
    idle = info.get('stream_idle_timeout_ms', 300000)
    if not isinstance(idle, (int, float)) or isinstance(idle, bool) or idle <= 0:
        raise RuntimeError('The Codex stream deadline is invalid')
    with transport(os.environ['HERMES_BROKER_HOME'], auth.get('tokens', {}).get('access_token', ''), codex_home, idle / 1000) as (url, token):
        provider = {
            **info, 'name': 'Hermes Broker', 'base_url': url, 'wire_api': 'responses',
            'requires_openai_auth': True, 'env_key': 'HERMES_BROKER_PROXY_TOKEN',
            'supports_websockets': False,
            'supports_standalone_web_search': info.get('supports_standalone_web_search', True),
            'capabilities': {'remote_compaction': 'v2', **info.get('capabilities', {})},
            'env_http_headers': {'X-Hermes-Broker-Session': 'HERMES_BROKER_PROXY_TOKEN'},
        }
        flags = ['-c', 'model_provider="hermes_broker"', '-c',
                 'model_providers.hermes_broker=' + _toml(provider),
                 '-c', 'shell_environment_policy.set.HERMES_BROKER_PROXY_TOKEN=""']
        args = _without_provider_override(args)
        boundary = args.index('--') if '--' in args else len(args)
        args = [*args[:boundary], *flags, *args[boundary:]]
        child = subprocess.Popen([*vendor, *args], env={**os.environ,
                                 'HERMES_BROKER_PROXY_TOKEN': token})
        previous = {}
        def forward(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, forward)
        try:
            status = child.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return status if status >= 0 else 128 - status


if __name__ == '__main__':
    raise SystemExit(main())
