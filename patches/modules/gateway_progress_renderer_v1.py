"""Expose native heartbeat presentation without transferring delivery ownership."""
from pathlib import Path

MARKER = "HERMES_GATEWAY_PROGRESS_RENDERER_v1"
ANCHOR = "            try:\n                _notify_res = None\n"
RENDER = """            if _long_running_mode != "generic":
                # HERMES_GATEWAY_PROGRESS_RENDERER_v1
                try:
                    from hermes_cli.plugins import invoke_hook
                    from gateway.run import _redact_gateway_user_facing_secrets
                    _progress_agent = agent_holder[0]
                    for _rendered in invoke_hook(
                        "render_gateway_progress", platform=source.platform.value,
                        session_id=str(getattr(_progress_agent, "session_id", "") or ""),
                        turn_id=str(getattr(_progress_agent, "_current_turn_id", "") or ""),
                        minutes=_elapsed_mins, task=_redact_gateway_user_facing_secrets(str(turn_ctx.message or ""))[:160],
                    ):
                        if isinstance(_rendered, str) and _rendered.strip():
                            _heartbeat_text = _redact_gateway_user_facing_secrets(_rendered[:1200])
                            break
                except Exception:
                    logger.debug("Progress renderer failed; retaining native heartbeat", exc_info=True)
"""


def patch_gateway_progress_renderer_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    target = root / "gateway/run_turn.py"
    hooks = root / "hermes_cli/plugins.py"
    stream = root / "agent/stream_delivery.py"
    stream_source = stream.read_text()
    source, api = target.read_text(), hooks.read_text()
    if MARKER in stream_source and MARKER in source and '"render_gateway_progress",' in api:
        return False
    if MARKER in stream_source or MARKER in source or '"render_gateway_progress",' in api:
        raise RuntimeError("Partial gateway progress renderer application")
    start = source.index("    async def _run_agent_notify_long_running(")
    end = source.index("    async def _run_agent_inner(", start)
    notifier = source[start:end]
    if notifier.count(ANCHOR) != 1:
        raise RuntimeError("Native progress renderer anchor drift")
    updated = source[:start] + notifier.replace(ANCHOR, RENDER + ANCHOR, 1) + source[end:]
    hook_anchor = "VALID_HOOKS: set[str] = {\n"
    if api.count(hook_anchor) != 1:
        raise RuntimeError("Native plugin hook declaration drift")
    updated_api = api.replace(hook_anchor, hook_anchor + '    "render_gateway_progress",\n', 1)
    live_anchor = "        self._deliver_interim(visible, already_streamed=False, record=[visible])\n"
    if stream_source.count(live_anchor) != 1:
        raise RuntimeError("Native live commentary anchor drift")
    updated_stream = stream_source.replace(live_anchor,
        "        # HERMES_GATEWAY_PROGRESS_RENDERER_v1: live commentary also reaches native observers.\n"
        "        self._enqueue_stream_hook(\"on_interim_message\", text=visible, already_streamed=False)\n" + live_anchor, 1)
    compile(updated_stream, str(stream), "exec")
    compile(updated, str(target), "exec")
    compile(updated_api, str(hooks), "exec")
    stream.write_text(updated_stream)
    target.write_text(updated)
    hooks.write_text(updated_api)
    return True
