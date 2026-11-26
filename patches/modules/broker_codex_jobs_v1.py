"""Bind native direct calls and pool persistence to the broker recovery boundary."""
from pathlib import Path
import hashlib
import ast
import os

MARKER = 'HERMES_BROKER_CODEX_JOBS_v1'


def patch_broker_codex_jobs_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    # The consumer endpoint rejects zstd bodies for real retained client requests.
    # Keep the native JSON body until compression is supported on this route.
    headers = root / 'agent/codex_headers.py'
    header_source = headers.read_text()
    compression = [node for node in ast.parse(header_source).body
                   if isinstance(node, ast.FunctionDef) and node.name == 'codex_request_compression_hooks']
    if len(compression) != 1:
        raise RuntimeError('Native Codex compression boundary changed')
    node = compression[0]
    lines = header_source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [
        'def codex_request_compression_hooks(base_url: str, *, async_mode: bool = False) -> dict[str, list] | None:\n',
        '    """Use raw JSON: the consumer endpoint rejects compressed retained requests."""\n',
        '    return None\n',
    ]
    plain_headers = ''.join(lines)
    compile(plain_headers, str(headers), 'exec')
    compression_changed = plain_headers != header_source
    auxiliary = root / 'agent/auxiliary_client.py'
    auth = root / 'hermes_cli/auth.py'
    payload = root / 'agent/broker_codex_jobs.py'
    expected = (Path(__file__).parent.parent / 'payloads/broker-codex-jobs-v1/broker_codex_jobs.py').read_text()
    directory = Path(__file__).parent.parent / 'payloads/broker-codex-jobs-v1'
    cli_files = [(root / ('agent/' + name), (directory / name).read_text(), 0o644)
                 for name in ('broker_codex_cli.py', 'broker_codex_cli_bootstrap.py', 'broker_codex_history.py')]
    cli_files += [(root / 'agent/broker_cli_bin' / name, (directory / name).read_text(), mode)
                  for name, mode in (('codex', 0o755), ('codex.cmd', 0o644))]
    for path, text, mode in cli_files:
        if path.suffix != '.cmd':
            compile(text, str(path), 'exec')
        if path.exists() and path.read_text() != text:
            raise RuntimeError('Conflicting broker worker transport')
    cli_changes = [(path, text, mode) for path, text, mode in cli_files
                   if not path.exists() or (os.name != 'nt' and path.stat().st_mode & 0o777 != mode)]
    main = root / 'hermes_cli/main.py'
    main_source = main.read_text()
    if 'HERMES_BROKER_CODEX_CLI_v1' not in main_source:
        functions = [node for node in ast.parse(main_source).body
                     if isinstance(node, ast.FunctionDef) and node.name == 'main']
        if len(functions) != 1 or not functions[0].body:
            raise RuntimeError('Native CLI startup boundary changed')
        first = functions[0].body[0]
        insertion = (first.end_lineno if isinstance(first, ast.Expr)
                     and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
                     else first.lineno - 1)
        lines = main_source.splitlines(keepends=True)
        lines.insert(insertion, '    # HERMES_BROKER_CODEX_CLI_v1\n'
                     '    from agent.broker_codex_cli_bootstrap import prepare_worker_path\n'
                     '    prepare_worker_path()\n')
        main_source = ''.join(lines)
        compile(main_source, str(main), 'exec')
        cli_changes.append((main, main_source, main.stat().st_mode & 0o777))
    source, store = auxiliary.read_text(), auth.read_text()
    helper = root / 'agent/agent_runtime_helpers.py'
    runtime_source = helper.read_text()
    legacy = 'HERMES_CODEX_SUBSCRIPTION_FAILOVER_v1' in runtime_source
    if MARKER in source or MARKER in store or payload.exists():
        if (MARKER in source and MARKER in store and payload.exists()
                and (not legacy or MARKER in runtime_source)):
            previous = payload.read_text()
            if (previous != expected and hashlib.sha256(previous.encode()).hexdigest()
                    != '6b48f50a2eaf590decba22861c9f3b0af306ae3427df34000e09e2d38f6cbc3c'):
                raise RuntimeError('Conflicting broker recovery generation')
            compile(expected, str(payload), 'exec')
            changes = [(path, text) for path, text, mode in cli_changes]
            if compression_changed:
                changes.append((headers, plain_headers))
            if previous != expected:
                changes.append((payload, expected))
            for path, text in changes:
                path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                path.write_text(text)
            for path, text, mode in cli_changes:
                path.chmod(mode)
            return bool(changes)
        raise RuntimeError('Partial or conflicting broker job recovery patch')
    start = source.index('class _CodexCompletionsAdapter:')
    end = source.index('\nclass _ChatShim:', start)
    block = source[start:end]
    if block.count('    def create(self, **kwargs) -> Any:\n') != 1:
        raise RuntimeError('Native Codex auxiliary boundary changed')
    block = block.replace('    def create(self, **kwargs) -> Any:\n',
        '    def create(self, **kwargs) -> Any:\n'
        '        # HERMES_BROKER_CODEX_JOBS_v1\n'
        '        from agent.broker_codex_jobs import complete\n'
        '        return complete(self, kwargs)\n\n'
        '    def _create_once(self, **kwargs) -> Any:\n')
    source = source[:start] + block + source[end:]
    start = source.index('def _recover_provider_pool(')
    end = source.index('\ndef ', start + 1)
    block = source[start:end]
    anchor = '    normalized = _normalize_aux_provider(provider)\n'
    if block.count(anchor) != 1 or block.count('        if pool.try_refresh_current() is not None:') != 1:
        raise RuntimeError('Native auxiliary recovery boundary changed')
    block = block.replace(anchor, anchor +
        '    if normalized == "openai-codex":\n'
        '        from agent.broker_codex_jobs import request_key\n'
        '        failed_api_key = request_key(exc, failed_api_key)\n')
    block = block.replace('        if pool.try_refresh_current() is not None:',
        '        refreshed = (pool.try_refresh_matching(api_key_hint=failed_api_key)\n'
        '                     if normalized == "openai-codex" and failed_api_key else pool.try_refresh_current())\n'
        '        if refreshed is not None:')
    source = source[:start] + block + source[end:]
    start = store.index('def _merge_pool_row_generation(')
    end = store.index('\ndef _merge_disk_cooldown_state(', start)
    block = store[start:end]
    anchor = '    merge_disk = None if status_cleared else disk_entry\n'
    if block.count(anchor) != 1:
        raise RuntimeError('Native pool generation merge boundary changed')
    block = block.replace(anchor,
        '    # HERMES_BROKER_CODEX_JOBS_v1\n'
        '    from agent.broker_codex_jobs import merge_metadata\n'
        '    entry = merge_metadata(entry, disk_entry, provider_id)\n' + anchor)
    store = store[:start] + block + store[end:]
    # Existing clients still carry this retired preflight in their immutable base.
    # Remove its aggregate usage gate; the actual model request determines quota.
    if legacy:
        start = runtime_source.index('def _broker_codex_quota(entry, timeout):')
        end = runtime_source.index('\ndef _broker_codex_entries(pool):', start)
        runtime_source = (runtime_source[:start]
            + 'def _broker_codex_quota(entry, timeout):\n'
              '    # HERMES_BROKER_CODEX_JOBS_v1: model request is quota authority.\n'
              '    from agent.credential_pool import _codex_principal_identity\n'
              '    identity = _codex_principal_identity(entry.access_token)\n'
              '    return bool(identity and identity == (entry.extra.get("broker_account_id"), entry.extra.get("broker_subject")))\n\n'
            + runtime_source[end:])
        start = runtime_source.index('def _try_broker_codex_alternative(')
        end = runtime_source.index('\ndef _broker_codex_retry_timeout(', start)
        block = runtime_source[start:end]
        block = block.replace('len(entries) > 5', 'len(entries) > 4').replace('len(fresh) > 5', 'len(fresh) > 4').replace('len(tried) >= 5', 'len(tried) >= 4')
        runtime_source = runtime_source[:start] + block + runtime_source[end:]
    changes = [(auxiliary, source), (auth, store), (payload, expected),
               *[(path, text) for path, text, mode in cli_changes]]
    if compression_changed:
        changes.append((headers, plain_headers))
    if legacy:
        changes.append((helper, runtime_source))
    for path, text in changes:
        if path.suffix != '.cmd':
            compile(text, str(path), 'exec')
    for path, text in changes:
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_text(text)
    for path, text, mode in cli_changes:
        path.chmod(mode)
    return True
