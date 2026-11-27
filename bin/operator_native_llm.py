"""Tenant-local native inference for maintenance scripts; no separate credential plane."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys


def call_native(system: str, user: str, max_tokens: int, home: Path) -> str | None:
    try:
        home = home.resolve()
        if not home.is_relative_to(Path.home().resolve()):
            raise ValueError('selected tenant home is outside the current principal')
        binding = json.loads((home / 'state/runtime-binding.json').read_text())
        root = Path(binding['runtime_root']).resolve(strict=True)
        python = Path(binding['runtime_python'])
        bound_home = Path(binding.get('profile_home') or binding['hermes_home']).resolve()
        legacy_profile = home.parent.name == 'profiles' and home.parent.parent == bound_home
        if (binding.get('status') != 'active' or (bound_home != home and not legacy_profile)
                or not root.is_relative_to(home) or not python.is_relative_to(root) or not python.is_file()):
            raise ValueError('active tenant runtime binding is invalid')
        code = ("import json,sys; from agent.auxiliary_client import call_llm; "
                "p=json.load(sys.stdin); r=call_llm(task='compression',messages=p['messages'],"
                "max_tokens=p['max_tokens'],timeout=180); "
                "print(json.dumps({'content':r.choices[0].message.content,'model':getattr(r,'model',None)}))")
        payload = {'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                   'max_tokens': max_tokens}
        environment = dict(os.environ, HERMES_HOME=str(home), HERMES_CONFIG=str(home / 'config.yaml'))
        for selector in ('HERMES_PROFILE', 'HERMES_ACTIVE_PROFILE', 'PYTHONPATH', 'PYTHONHOME'):
            environment.pop(selector, None)
        result = subprocess.run([str(python), '-c', code], cwd=root,
            env=environment, input=json.dumps(payload),
            stdout=subprocess.PIPE, text=True, timeout=210)
        if result.returncode:
            raise RuntimeError('tenant native inference failed')
        response = json.loads(result.stdout.strip().splitlines()[-1])
        model = response.get('model')
        model = model if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,160}', model) else 'unknown'
        print(json.dumps({'inference_route':'tenant-native-auxiliary-sdk','task':'compression',
                          'response_model':model,'runtime_root':str(root)}), file=sys.stderr)
        value = response['content']
        if not isinstance(value, str) or not value.strip():
            raise ValueError('native inference returned no text')
        return value.strip()
    except (OSError, ValueError, KeyError, IndexError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'ERROR: tenant native inference failed ({type(exc).__name__}); no artifact update', file=sys.stderr)
        return None
