#!/usr/bin/env python3
"""Run Hermes from an explicit profile's verified active runtime binding.

Invoke with a stable host Python, not a release-candidate interpreter. This
selects a runtime; callers still own maintenance admission and job draining.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

BOOTSTRAP = """import os, runpy, sys
root, cwd = sys.argv[1:3]
sys.path.insert(0, root)
os.chdir(cwd)
sys.argv = ['hermes', *sys.argv[3:]]
runpy.run_module('hermes_cli.main', run_name='__main__')
"""


def binding_validator():
    here = Path(__file__).resolve()
    # The source bundle and the profile install carry the same existing check.
    path = here.with_name('agent-runtime-coherence.py')
    if not path.is_file():
        path = here.parents[2] / 'checks/agent-runtime-coherence.py'
    spec = importlib.util.spec_from_file_location('runtime_coherence', path)
    if spec is None or spec.loader is None:
        raise ValueError('runtime coherence validator is missing')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_runtime_binding


def absolute_path(raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise ValueError(f'{label} must be an absolute path')
    path = Path(raw)
    if '..' in path.parts:
        raise ValueError(f'{label} must be lexical')
    return path


def command(home: Path, arguments: list[str]) -> tuple[list[str], dict[str, str]]:
    home = absolute_path(str(home), 'hermes home')
    if home.is_symlink() or home.resolve(strict=True) != home:
        raise ValueError('hermes home must not contain a symlink')
    binding_path = home / 'state/runtime-binding.json'
    if binding_path.is_symlink() or binding_path.resolve(strict=True) != binding_path:
        raise ValueError('runtime binding must not contain a symlink')
    before = binding_path.read_bytes()
    binding = json.loads(before)
    if not isinstance(binding, dict):
        raise ValueError('runtime binding must be an object')
    root = absolute_path(binding.get('runtime_root'), 'runtime root')
    python = absolute_path(binding.get('runtime_python'), 'runtime Python')
    if not root.is_dir() or root.resolve() != root:
        raise ValueError('runtime root is missing or symlinked')
    expected_python = root / ('venv/Scripts/python.exe' if os.name == 'nt' else 'venv/bin/python')
    if python != expected_python or not python.is_file():
        raise ValueError('runtime Python is not the bound candidate interpreter')
    proof = binding_validator()(binding_path=binding_path, runtime_root=root,
                                runtime_python=python, hermes_home=home)
    if not proof['ok']:
        raise ValueError(proof['reason'])
    # Never launch during an update. An abandoned marker needs owner recovery;
    # elapsed time is not authority to admit a new independent worker.
    if os.path.lexists(home / '.drain_request.json'):
        raise ValueError('maintenance drain is present; retry after its owner clears it')
    argv = [str(python), '-I']
    verified = root / '.operator-control/verified-runtime-launch.py'
    operator_receipt = root / '.operator-control/runtime-fingerprint.json'
    launchers = binding['service']['launchers']
    operator_rows = [row for row in launchers if row.get('path') == str(verified)]
    if verified.exists() or operator_receipt.exists() or operator_rows:
        if len(operator_rows) != 1 or not verified.is_file() or not operator_receipt.is_file():
            raise ValueError('verified runtime launcher or fingerprint is incomplete')
        if hashlib.sha256(verified.read_bytes()).hexdigest() != operator_rows[0]['sha256']:
            raise ValueError('verified runtime launcher changed')
        argv += [str(verified), str(operator_receipt), str(root), '-I']
    argv += ['-c', BOOTSTRAP, str(root), os.getcwd(), *arguments]
    env = os.environ.copy()
    env['HERMES_HOME'] = str(home)
    env['VIRTUAL_ENV'] = str(root / 'venv')
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env['PATH'] = str(python.parent) + os.pathsep + env.get('PATH', '')
    if binding_path.read_bytes() != before or os.path.lexists(home / '.drain_request.json'):
        raise ValueError('runtime binding or maintenance admission changed; retry')
    return argv, env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-home', required=True, type=Path)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    if not arguments:
        parser.error('a Hermes command is required after --')
    try:
        argv, env = command(args.hermes_home, arguments)
        os.execve(argv[0], argv, env)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'bound Hermes launch refused: {exc}', file=sys.stderr)
        return 78
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
