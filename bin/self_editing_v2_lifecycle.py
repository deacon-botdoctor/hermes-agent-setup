#!/usr/bin/env python3
"""Operator-driven, isolated skill promotion rehearsal. Never installs into a live home."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

from self_editing_v2_archive import preflight_candidate, write_candidate_archive
from self_editing_v2_acceptance import run_acceptance
from self_editing_v2_promotion import run_promotion_dry_run, utc_now


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def bind(request):
    spec = importlib.util.spec_from_file_location('reflection_bridge', Path(__file__).with_name('hermes-reflect-candidates-to-drafts.py'))
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    report_path = Path(request['report'])
    report = read_json(report_path)
    index = request['proposal_index']
    if type(index) is not int or index < 0:
        raise ValueError('proposal_index must be a nonnegative integer')
    candidate = report['proposal_inputs'][index]
    preflight_candidate(candidate)
    pending = read_json(request['pending_item'])
    envelope = bridge._validate_pending_envelope(pending['envelope'], scope_id=request['scope_id'])
    if pending != bridge._pending_item(envelope):
        raise ValueError('pending binding does not match envelope')
    expected = bridge.build_envelope(report, candidate, index, report_path, scope_id=request['scope_id'])
    # File modification time is not source identity when the report has no timestamp.
    expected['original_timestamp'] = envelope['original_timestamp']
    if expected != envelope:
        raise ValueError('source proposal does not match pending envelope')
    owner = request['owner']
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', owner):
        raise ValueError('owner must be one normalized skill name')
    target = f'skills/{owner}/SKILL.md'
    if candidate.get('target_rung') != 'skill' or candidate.get('proposed_paths') != [target]:
        raise ValueError('proposal must name the exact existing skill owner')
    if candidate.get('changed_paths', [target]) != [target]:
        raise ValueError('additional changed paths are not supported')
    previous = Path(request['previous_skill']).read_bytes()
    draft = Path(request['draft_skill']).read_bytes()
    for data in (previous, draft):
        frontmatter = data.decode().split('---', 2)
        if len(frontmatter) != 3 or frontmatter[0].strip() or not re.search(r'^name:\s*[\'\"]?' + re.escape(owner) + r'[\'\"]?\s*$', frontmatter[1], re.M):
            raise ValueError('skill frontmatter must identify the exact owner')
    if digest(previous) != request['previous_sha256'] or digest(draft) != request['draft_sha256']:
        raise ValueError('operator-approved skill digest mismatch')
    if previous == draft:
        raise ValueError('draft does not change the existing skill')
    return report, candidate, pending, previous, draft


def run_isolated(request_path: Path, commands_path: Path, work_root: Path):
    request = read_json(request_path)
    commands = read_json(commands_path)
    report, candidate, pending, previous, draft = bind(request)
    watch = commands['watch_seconds']
    timeout = commands['command_timeout_seconds']
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (watch, timeout)):
        raise ValueError('watch and command timeout must be positive finite seconds')
    for stage in ('canary', 'watch', 'rollback'):
        batch = commands[stage]
        if not isinstance(batch, list) or not batch or any(not isinstance(argv, list) or not argv or any(not isinstance(s, str) or not s for s in argv) for argv in batch):
            raise ValueError(f'{stage} requires operator-owned argv lists')
    work_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='isolated-promotion-', dir=work_root.resolve()))
    home = root / 'runtime'
    workspace = root / 'workspace'
    home.mkdir()
    workspace.mkdir()
    state = {'schema': 'isolated_skill_lifecycle_v1', 'run_root': str(root), 'production_action': 'hold',
             'isolated_only': True, 'watch_seconds_requested': watch, 'history': [], 'commands': [],
             'previous_sha256': digest(previous), 'draft_sha256': digest(draft), 'owner': request['owner']}
    def record(phase):
        state['state'] = phase
        state['history'].append({'state': phase, 'at': utc_now()})
        write_json(root / 'lifecycle.json', state)
    def safe_target():
        target = home / 'skills' / request['owner'] / 'SKILL.md'
        for path in (home, home / 'skills', target.parent, target):
            if path.is_symlink():
                raise ValueError(f'isolated target became a symlink: {path}')
        return target
    def install(data):
        target = safe_target()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.skill-', dir=target.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        Path(name).replace(target)
    def execute(stage):
        for argv in commands[stage]:
            n = len(state['commands'])
            started = time.monotonic()
            env = os.environ.copy()
            env.pop('HERMES_SAFE_MODE', None)
            for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
                env.pop(key, None)
            env['GIT_CEILING_DIRECTORIES'] = str(root)
            env['HERMES_HOME'] = str(home)
            env['SELF_EDIT_RUN_ROOT'] = str(root)
            with (root / f'{n}-{stage}.stdout').open('wb') as out, (root / f'{n}-{stage}.stderr').open('wb') as err:
                child = subprocess.Popen(argv, cwd=workspace, env=env, stdout=out, stderr=err, start_new_session=True)
                try:
                    code = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    code = 124
                finally:
                    # Terminate the process group, including children that outlive the parent.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
            state['commands'].append({'stage': stage, 'argv': argv, 'exit_code': code, 'seconds': time.monotonic()-started})
            write_json(root / 'lifecycle.json', state)
            if code:
                raise RuntimeError(f'{stage} command {n} failed with exit {code}')
    installed = False
    outcome = 'error'
    record('bound')
    try:
        archive = write_candidate_archive(candidate, hermes_home=root / 'archive', report=report,
            evidence={'pending_item': pending, 'previous_sha256': digest(previous), 'draft_sha256': digest(draft)})
        state['archive_path'] = str(archive)
        (archive / 'previous.SKILL.md').write_bytes(previous)
        (archive / 'draft.SKILL.md').write_bytes(draft)
        cases = read_json(request['acceptance_cases'])
        write_json(archive / 'acceptance-cases.json', cases)
        record('archived')
        decision = run_acceptance(archive)
        if decision['decision'] != 'accepted':
            record('rejected')
            outcome = 'rejected'
        else:
            run_promotion_dry_run(archive)
            record('accepted')
            for relative, source in request.get('workspace_inputs', {}).items():
                dest = Path(relative)
                if dest.is_absolute() or '..' in dest.parts or not dest.parts:
                    raise ValueError('workspace inputs require bounded relative paths')
                dest = workspace / dest
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(Path(source).read_bytes())
            (home / 'config.yaml').write_text('skills:\n  auto_load:\n    - ' + request['owner'] + '\n')
            install(previous)
            installed = True
            install(draft)
            record('canary_running')
            execute('canary')
            if digest(safe_target().read_bytes()) != digest(draft):
                raise RuntimeError('canary changed the installed skill')
            record('isolated_promoted')
            start = time.monotonic()
            execute('watch')
            time.sleep(max(0, watch - (time.monotonic()-start)))
            execute('watch')
            state['watch_seconds_observed'] = time.monotonic()-start
            if digest(safe_target().read_bytes()) != digest(draft):
                raise RuntimeError('watch changed the installed skill')
            record('watch_passed')
            outcome = 'passed'
    except (Exception, KeyboardInterrupt) as exc:
        state['error'] = f'{type(exc).__name__}: {exc}'
        record('failed')
    finally:
        if installed:
            try:
                install(previous)
                state['rollback_sha256'] = digest(safe_target().read_bytes())
                if state['rollback_sha256'] != digest(previous):
                    raise RuntimeError('rollback bytes differ')
                execute('rollback')
                record('rolled_back')
            except (Exception, KeyboardInterrupt) as exc:
                state['rollback_error'] = f'{type(exc).__name__}: {exc}'
                outcome = 'error'
                record('rollback_failed')
        if state['state'] != 'rollback_failed':
            # Retain logs, workspace, and frozen drafts outside runtime discovery.
            if home.is_symlink():
                home.unlink()
            else:
                shutil.rmtree(home)
            state['runtime_removed'] = True
            record('retired')
        state['outcome'] = outcome
        write_json(root / 'lifecycle.json', state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--commands', required=True, type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        result = run_isolated(args.request, args.commands, args.work_root)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result['outcome'] == 'passed' else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
