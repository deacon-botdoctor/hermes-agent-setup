from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'maintenance/bin/run-bound-hermes.py'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def profile(tmp_path):
    home = tmp_path / 'profile'
    (home / 'state').mkdir(parents=True)
    root = tmp_path / 'candidate'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / 'venv')], check=True)
    (root / 'hermes_cli').mkdir()
    (root / 'hermes_cli/__init__.py').write_text('')
    (root / 'hermes_cli/main.py').write_text(
        'import json, os, sys\nprint(json.dumps({"root":__file__,"args":sys.argv[1:],'
        '"home":os.environ["HERMES_HOME"],"cwd":os.getcwd(),'
        '"pythonpath":os.environ.get("PYTHONPATH")}))\n')
    definition = tmp_path / 'gateway.service'
    definition.write_text('fixture service')
    binding = {'schema_version':1,'kind':'botdoctor_runtime_binding','status':'active',
               'hermes_home':str(home),'runtime_root':str(root),
               'runtime_python':str(root / 'venv/bin/python'),
               'service':{'kind':'systemd-user','owner':'fixture',
                          'definition_path':str(definition),'definition_sha256':sha(definition),
                          'launchers':[]}}
    (home / 'state/runtime-binding.json').write_text(json.dumps(binding))
    return home, root, binding


def run(home, *arguments):
    return subprocess.run([sys.executable, str(SCRIPT), '--hermes-home', str(home), '--', *arguments],
                          text=True, capture_output=True,
                          env={**os.environ, 'PYTHONPATH':'/retired/candidate'})


def test_binding_switch_and_rollback_preserve_arguments_and_cwd(profile):
    home, root, binding = profile
    first = run(home, 'chat', '--query-file', 'file with spaces', ';literal')
    assert first.returncode == 0, first.stderr
    data = json.loads(first.stdout)
    assert data['root'] == str(root / 'hermes_cli/main.py')
    assert data['args'] == ['chat', '--query-file', 'file with spaces', ';literal']
    assert data['home'] == str(home) and data['cwd'] == os.getcwd()
    assert data['pythonpath'] is None
    second = root.with_name('candidate-next')
    root.rename(second)
    updated = {**binding,'runtime_root':str(second),'runtime_python':str(second/'venv/bin/python')}
    receipt = home / 'state/runtime-binding.json'
    receipt.write_text(json.dumps(updated))
    result = run(home, '--version')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['root'] == str(second / 'hermes_cli/main.py')
    second.rename(root)
    receipt.write_text(json.dumps(binding))
    result = run(home, '--version')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['root'] == str(root / 'hermes_cli/main.py')


@pytest.mark.parametrize('fault', ['missing', 'invalid', 'inactive', 'foreign_home', 'definition',
                                    'python', 'drain', 'symlink', 'partial_verified'])
def test_invalid_binding_never_executes(profile, fault):
    home, root, binding = profile
    receipt = home / 'state/runtime-binding.json'
    if fault == 'missing': receipt.unlink()
    elif fault == 'invalid': receipt.write_text('[]')
    elif fault == 'inactive':
        binding['status'] = 'pending'; receipt.write_text(json.dumps(binding))
    elif fault == 'foreign_home':
        binding['hermes_home'] = '/another/profile'; receipt.write_text(json.dumps(binding))
    elif fault == 'definition': Path(binding['service']['definition_path']).write_text('changed')
    elif fault == 'python':
        binding['runtime_python'] = sys.executable; receipt.write_text(json.dumps(binding))
    elif fault == 'drain': (home / '.drain_request.json').write_text('{}')
    elif fault == 'symlink':
        saved = receipt.with_suffix('.saved'); receipt.rename(saved); receipt.symlink_to(saved)
    else:
        (root / '.operator-control').mkdir()
        (root / '.operator-control/verified-runtime-launch.py').write_text('raise SystemExit(0)')
    result = run(home, '--version')
    assert result.returncode == 78
    assert not result.stdout
    assert 'bound Hermes launch refused' in result.stderr


def test_verified_launcher_is_preserved_and_failure_propagates(profile):
    home, root, binding = profile
    directory = root / '.operator-control'
    directory.mkdir()
    launcher = directory / 'verified-runtime-launch.py'
    # A rejecting existing verifier must never be skipped by the command bridge.
    launcher.write_text('import sys\nprint("fingerprint rejected", file=sys.stderr)\nraise SystemExit(78)\n')
    (directory / 'runtime-fingerprint.json').write_text('{}')
    binding['service']['launchers'] = [{'path':str(launcher),'sha256':sha(launcher)}]
    (home / 'state/runtime-binding.json').write_text(json.dumps(binding))
    result = run(home, '--version')
    assert result.returncode == 78 and result.stderr.strip() == 'fingerprint rejected'
    assert not result.stdout
    launcher.write_text('raise SystemExit(0)')
    result = run(home, '--version')
    assert result.returncode == 78 and 'binding_launcher_drift' in result.stderr


def test_verified_launcher_success_and_installed_layout(profile, tmp_path):
    import shutil
    home, root, binding = profile
    directory = root / '.operator-control'
    directory.mkdir()
    launcher = directory / 'verified-runtime-launch.py'
    launcher.write_text('import os, sys\nos.chdir(sys.argv[2])\nos.execv(sys.executable,[sys.executable,*sys.argv[3:]])\n')
    (directory / 'runtime-fingerprint.json').write_text('{}')
    binding['service']['launchers'] = [{'path':str(launcher),'sha256':sha(launcher)}]
    (home / 'state/runtime-binding.json').write_text(json.dumps(binding))
    installed = home / 'bin'
    installed.mkdir()
    shutil.copy2(SCRIPT, installed / SCRIPT.name)
    shutil.copy2(ROOT / 'checks/agent-runtime-coherence.py', installed / 'agent-runtime-coherence.py')
    result = subprocess.run([sys.executable, str(installed / SCRIPT.name), '--hermes-home',str(home),
                             '--','chat','--in',str(tmp_path)], text=True,capture_output=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['root'] == str(root / 'hermes_cli/main.py')
    assert data['cwd'] == os.getcwd()
    assert data['args'] == ['chat','--in',str(tmp_path)]


def test_binding_change_during_validation_refuses_launch(profile, monkeypatch):
    home, root, binding = profile
    spec = importlib.util.spec_from_file_location('bound_command', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    validate = module.binding_validator()
    def changed(**kwargs):
        result = validate(**kwargs)
        binding['status'] = 'pending'
        (home / 'state/runtime-binding.json').write_text(json.dumps(binding))
        return result
    monkeypatch.setattr(module, 'binding_validator', lambda:changed)
    with pytest.raises(ValueError, match='admission changed'):
        module.command(home, ['--version'])
