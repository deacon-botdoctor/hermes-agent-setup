"""Offer a configured completion reporter the native run before delivery."""
from pathlib import Path

MARKER = 'HERMES_CRON_COMPLETION_REPORTER_v1'
HELPER = '''"""Optional native scheduler completion publication."""
import json
import logging
from pathlib import Path
import subprocess


def report_completion(phase, execution_id, *, success=None, output_file=None):
    from hermes_cli.config import load_config_readonly
    try:
        config = load_config_readonly().get('cron', {})
        if not isinstance(config, dict):
            raise ValueError('Cron configuration must be an object')
        command = config.get('completion_reporter')
        if command is None:
            return None
        if (not isinstance(command, list) or not command or len(command) > 20
                or any(not isinstance(part, str) or not part or len(part) > 1024 for part in command)
                or not Path(command[0]).is_absolute()):
            raise ValueError('Completion reporter requires a bounded absolute command')
        payload = {'phase': phase, 'execution_id': execution_id}
        if phase == 'produced':
            payload.update(success=success, output_file=str(output_file) if output_file else None)
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                text=True, timeout=60)
        response = json.loads(result.stdout) if result.returncode == 0 else {}
        if not isinstance(response, dict):
            raise ValueError('Completion reporter response must be an object')
        acknowledged = response.get('retained') if phase == 'state' else response.get('accepted')
        if acknowledged is not True or response.get('execution_id') != execution_id:
            raise ValueError('Completion reporter has not accepted this execution')
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logging.getLogger(__name__).warning('Native completion publication remains pending (%s)', type(exc).__name__)
        return 'Native completion publication remains pending; inspect the reporter receipt.'
'''


def patch_cron_completion_reporter_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    scheduler = root / 'cron/scheduler.py'
    executions = root / 'cron/executions.py'
    helper = root / 'cron/completion_reporter.py'
    source, ledger = scheduler.read_text(), executions.read_text()
    anchor = '    if not d.should_deliver:\n        return\n    d.unresolved_origin = ('
    replacement = '''    # HERMES_CRON_COMPLETION_REPORTER_v1
    from cron.completion_reporter import report_completion
    publication_error = report_completion(
        'produced', execution_id, success=d.success, output_file=output_file)
    if publication_error:
        d.delivery_error = publication_error
        d.should_deliver = False
        return
    if not d.should_deliver:
        return
    d.unresolved_origin = ('''
    terminal = '    """Project durable state to monitoring without affecting ledger behavior."""\n    try:\n'
    terminal_replacement = '''    """Project durable state to monitoring without affecting ledger behavior."""
    # HERMES_CRON_COMPLETION_REPORTER_v1
    if record:
        from cron.completion_reporter import report_completion
        report_completion('state', record['id'])
    try:
'''
    signature = '    adapters, loop, verbose: bool, execution_token, deferred_agents: list,\n'
    new_signature = signature.rstrip('\n') + ' execution_id=None,\n'
    caller = '                execution_token=execution_token, deferred_agents=_deferred_agents)\n'
    new_caller = '                execution_token=execution_token, deferred_agents=_deferred_agents, execution_id=execution_id)\n'
    crash = '    job: dict, err_text: str, *, adapters, loop,\n'
    new_crash = '    job: dict, err_text: str, *, adapters, loop, execution_id=None,\n'
    crash_body = '    """Failure notice for a run that raised out of run_job. Returns (delivery_error, outcome)."""\n'
    new_crash_body = crash_body + '''    from cron.completion_reporter import report_completion
    publication_error = report_completion('produced', execution_id or job.get('execution_id'), success=False)
    if publication_error:
        return publication_error, 'failed'
'''
    crash_caller = '                job, _err_text, adapters=adapters, loop=loop)\n'
    new_crash_caller = '                job, _err_text, adapters=adapters, loop=loop, execution_id=execution_id)\n'
    if MARKER in source or MARKER in ledger or helper.exists():
        if (source.count(replacement) != 1 or ledger.count(terminal_replacement) != 1
                or source.count(new_signature) != 1 or source.count(new_caller) != 1
                or source.count(new_crash) != 1 or source.count(new_crash_body) != 1
                or source.count(new_crash_caller) != 1
                or not helper.exists() or helper.read_text() != HELPER):
            raise RuntimeError(f'{MARKER}: incomplete or changed installed reporter')
        return False
    if (source.count(anchor) != 1 or ledger.count(terminal) != 1
            or source.count(signature) != 1 or source.count(caller) != 1
            or source.count(crash) != 1 or source.count(crash_body) != 1 or source.count(crash_caller) != 1):
        raise RuntimeError(f'{MARKER}: native scheduler anchor changed')
    source, ledger = source.replace(anchor, replacement), ledger.replace(terminal, terminal_replacement)
    source = source.replace(signature, new_signature).replace(caller, new_caller)
    source = source.replace(crash, new_crash).replace(crash_body, new_crash_body).replace(crash_caller, new_crash_caller)
    for path, content in [(scheduler, source), (executions, ledger), (helper, HELPER)]:
        compile(content, str(path), 'exec')
    for path, content in [(scheduler, source), (executions, ledger), (helper, HELPER)]:
        path.write_text(content)
    return True
