"""Bind store-failure admission and gateway-owned health to existing seams."""
from __future__ import annotations

import ast
from pathlib import Path

MARKER = "HERMES_STATE_TRANSACTION_HEALTH_v1"
PAYLOAD = Path(__file__).resolve().parents[1] / "payloads/state-transaction-health/gateway/state_transaction_health.py"


def admission_nodes(source: str):
    return [node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"_existing_startup_gate_decision", "_durable_admit_event"}]


def patch_admission(source: str) -> str:
    nodes = admission_nodes(source)
    if len(nodes) != 1:
        raise RuntimeError("expected exactly one durable admission decision seam")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    if MARKER in "".join(lines[node.lineno - 1:node.end_lineno]):
        return source
    start, end = node.body[0].lineno - 1, node.end_lineno
    if node.name == "_durable_admit_event":
        # Keep the native key binding outside recovery; the following lookup is
        # the first store operation, before the durable hook claim.
        if ast.unparse(node.body[0]) != "key = self._session_key_for_source(event.source)":
            raise RuntimeError("durable admission session key seam changed")
        start = node.body[0].end_lineno
        body = "".join("    " + line if line.strip() else line for line in lines[start:end])
        lines[start:end] = [f'''        # {MARKER}
        from gateway import state_transaction_health as _state_health
        try:
{body}        except Exception as error:
            if not _state_health.is_store_failure(error):
                raise
            queue_id, state = await _state_health.retain_failed_admission(
                self, event, key, error,
            )
            if state == "unauthorized":
                return None
            if not queue_id or state not in {{"queued", "claimed", "ambiguous", "completed", "handled"}}:
                raise DurableAdmissionError("Durable store-failure admission failed") from error
            return None
''']
        return "".join(lines)
    body = "".join("    " + line if line.strip() else line for line in lines[start:end])
    replacement = f'''        # {MARKER}
        from gateway import state_transaction_health as _state_health
        try:
{body}        except Exception as error:
            if not _state_health.is_store_failure(error):
                raise
            queue_id, state = await _state_health.retain_failed_admission(
                self, event, session_key, error,
            )
            if state == "unauthorized":
                return _StartupGateDecision("", True)
            saved = state in {{"queued", "claimed", "ambiguous", "completed", "handled"}}
            return _StartupGateDecision(
                response=("Your message was saved. I will resume it after service recovery."
                          if saved else "I could not save this message. Please resend it after service recovery."),
                durable_accepted=saved,
                queue_id=queue_id,
                receipt_state=state,
            )
'''
    lines[start:end] = [replacement]
    return "".join(lines)


def patch_api(source: str) -> str:
    if MARKER in source:
        return source
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_health_detailed")
    lines = source.splitlines(keepends=True)
    body = "".join(lines[node.lineno - 1:node.end_lineno])
    anchor = "        runtime = read_runtime_status() or {}\n"
    if body.count(anchor) != 1:
        raise RuntimeError("gateway detailed health seam changed")
    body = body.replace(anchor, f'''        # {MARKER}
        from gateway import state_transaction_health as _state_health
        from gateway.run import _gateway_runner_ref
        state_health = await asyncio.to_thread(
            _state_health.probe, _gateway_runner_ref(), diagnostics=request.query.get("diagnostics") == "1",
        )
''' + anchor)
    anchor = "        return web.json_response({\n"
    if body.count(anchor) != 1:
        raise RuntimeError("gateway detailed health response seam changed")
    body = body.replace(anchor, '''        if state_health["status"] != "pass":
            readiness["status"] = "degraded"
''' + anchor + '            "state_transaction": state_health,\n')
    lines[node.lineno - 1:node.end_lineno] = [body]
    return "".join(lines)


def patch_state_transaction_health_v1(root: Path) -> bool:
    root = Path(root)
    owners = [p for p in (root / "gateway/run.py", root / "gateway/run_durable_drain.py")
              if p.is_file() for _ in admission_nodes(p.read_text())]
    if len(owners) != 1:
        raise RuntimeError("durable admission owner is missing or ambiguous")
    api = root / "gateway/platforms/api_server.py"
    payload = root / "gateway/state_transaction_health.py"
    updates = {owners[0]: patch_admission(owners[0].read_text()),
               api: patch_api(api.read_text()), payload: PAYLOAD.read_text()}
    run = root / "gateway/run.py"
    run_text = updates.get(run, run.read_text())
    anchor = "    runner = GatewayRunner(config)\n"
    block = ("    from gateway.state_transaction_health import claim_owner_if_required\n"
             "    claim_owner_if_required(_hermes_home)\n" + anchor)
    if block not in run_text:
        if run_text.count(anchor) != 1:
            raise RuntimeError("gateway pre-store ownership seam changed")
        updates[run] = run_text.replace(anchor, block)
    state = root / "hermes_state.py"
    state_text = state.read_text()
    anchor = "        self.db_path = db_path or _default_db_path()\n"
    block = (anchor + "        from gateway.state_transaction_health import require_owner\n"
             "        require_owner(self.db_path)\n")
    if block not in state_text:
        if state_text.count(anchor) != 1:
            raise RuntimeError("SessionDB pre-open ownership seam changed")
        updates[state] = state_text.replace(anchor, block)
    originals = {p: p.read_text() if p.exists() else None for p in updates}
    for path, text in updates.items():
        compile(text, str(path), "exec")
    changed = [p for p in updates if updates[p] != originals[p]]
    try:
        for path in changed:
            path.write_text(updates[path])
    except Exception:
        for path in changed:
            if originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(originals[path])
        raise
    return bool(changed)
