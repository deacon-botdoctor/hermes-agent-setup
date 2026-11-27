#!/usr/bin/env python3
"""Run required memory maintenance through the selected native runtime."""
from __future__ import annotations
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pathlib
import subprocess
import sys


def backup_sources(h: Path) -> Path:
    base=h/'state/dream-preimages'
    if any(p.is_symlink() for p in (base,*base.parents)):raise ValueError('recovery route is redirected')
    sources=[]
    for source in (h/'memories/MEMORY.md',h/'memories/USER.md'):
        if source.is_symlink():raise ValueError('maintenance source is redirected')
        before=source.read_bytes() if source.exists() else None
        if before is not None and len(before)>1048576:raise ValueError('memory preimage exceeds the bounded recovery size')
        sources.append((source,before))
    identity=json.dumps([(str(p),hashlib.sha256(b).hexdigest() if b is not None else None) for p,b in sources]).encode()
    root=base/hashlib.sha256(identity).hexdigest()
    needed=sum(len(b) for p,b in sources if b and not (root/p.name).exists())+(4096 if not (root/'receipt.json').exists() else 0)
    used=sum(p.stat().st_size for p in base.rglob('*') if p.is_file() and not p.is_symlink()) if base.exists() else 0
    if used+needed>16*1024*1024:
        raise RuntimeError('dream recovery allocation reached 16 MiB; archive verified preimages through Host Steward before continuing')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    receipt={'stage':'before-consolidation','preimages':[]}
    for source,before in sources:
        item={'source':str(source),'state':'absent' if before is None else 'empty' if not before else 'nonempty','sha256':hashlib.sha256(before).hexdigest() if before is not None else None}
        if before:
            target=root/source.name
            if not target.exists():
                subprocess.run([sys.executable,str(pathlib.Path(os.environ['HERMES_DREAM_SOURCE_BIN'])/'hermes-disk-retention.py'),'--hermes-home',str(h),
                    '--copy-recovery-source',str(source),'--recovery-text-sha256',item['sha256'],'--register-artifact',str(target),'--artifact-owner','dream-cycle',
                    '--artifact-kind','recovery','--artifact-ttl','604800','--artifact-budget','1048576'],check=True,stdout=sys.stderr,stderr=subprocess.STDOUT)
            registration=h/'state/disk-retention-artifacts'/(hashlib.sha256(str(target).encode()).hexdigest()+'.json')
            registered=json.loads(registration.read_text())
            if (target.is_symlink() or target.read_bytes()!=before or registered.get('path')!=str(target)
                    or registered.get('owner')!='dream-cycle' or registered.get('kind')!='recovery'):
                raise RuntimeError('exact registered memory recovery preimage is missing or changed')
            item['backup']=str(target)
        receipt['preimages'].append(item)
    record=root/'receipt.json';record.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'preimages_verified':len(receipt['preimages']),'stage':'before-consolidation','receipt':str(record)}),file=sys.stderr)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory-only', action='store_true')
    args = parser.parse_args()
    h = Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes'))).resolve()
    binding = json.loads((h/'state/runtime-binding.json').read_text())
    root = Path(binding['runtime_root']).resolve(strict=True)
    python = Path(binding['runtime_python'])
    bound_home = Path(binding.get('profile_home') or binding['hermes_home']).resolve()
    legacy_profile = h.parent.name == 'profiles' and h.parent.parent == bound_home
    source = Path(__file__).resolve().parent
    if (not h.is_relative_to(Path.home().resolve()) or binding.get('status') != 'active'
            or (bound_home != h and not legacy_profile) or not root.is_relative_to(h)
            or not python.is_absolute() or not python.parent.resolve(strict=True).is_relative_to(root)
            or not python.is_file() or not source.is_relative_to(h)):
        raise ValueError('selected native runtime binding or source is invalid')
    env = dict(os.environ, HERMES_HOME=str(h), HERMES_CONFIG=str(h/'config.yaml'),
               HERMES_DREAM_SOURCE_BIN=str(source), PYTHONUTF8='1')
    for key in ('HERMES_PROFILE', 'HERMES_ACTIVE_PROFILE', 'PYTHONPATH', 'PYTHONHOME', 'HERMES_DREAM_RECOVERY_RECEIPT'):
        env.pop(key, None)
    if Path(sys.executable) != python:
        return subprocess.run([str(python), str(Path(__file__).resolve()), *sys.argv[1:]], env=env).returncode
    for key in ('HERMES_PROFILE', 'HERMES_ACTIVE_PROFILE', 'PYTHONPATH', 'PYTHONHOME', 'HERMES_DREAM_RECOVERY_RECEIPT'):
        os.environ.pop(key, None)
    os.environ.update({key: env[key] for key in ('HERMES_HOME', 'HERMES_CONFIG', 'HERMES_DREAM_SOURCE_BIN')})
    log_path = h/'logs/nightly-dream-cycle.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as log:
        def mark(text):
            stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            log.write(f'[{stamp}] {text}\n')
            log.flush()
        def step(name, path):
            if not path.is_file():
                raise ValueError(f'required script missing: {name}')
            mark(f'--- {name} START ---')
            result = subprocess.run([str(python), str(path)], env=env, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                mark(f'--- {name} FAILED rc={result.returncode} ---')
                raise RuntimeError(f'required step failed: {name}')
            mark(f'--- {name} OK ---')
        try:
            mark('=== nightly-dream-cycle START ===')
            step('dream', source/'dream.py')
            with contextlib.redirect_stderr(log):
                env['HERMES_DREAM_RECOVERY_RECEIPT'] = str(backup_sources(h))
            step('dream_consolidate', source/'dream_consolidate.py')
            doctrine = h/'bin/doctrine-sync.py'
            if not args.memory_only and doctrine.is_file():
                step('doctrine-sync', doctrine)
            mark('=== nightly-dream-cycle END \u2014 verified success ===')
            return 0
        except Exception as exc:
            mark(f'FAILED nightly-dream-cycle: {type(exc).__name__}')
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
