"""Make acknowledged external delivery history durable in the split Hermes tree."""
from pathlib import Path

MARKER = "HERMES_DURABLE_DELIVERY_HISTORY_v1"
PAYLOAD = Path(__file__).resolve().parents[1] / "payloads/durable-delivery-history-v1"


def patch_durable_delivery_history_v1(root: Path) -> bool:
    target = root / "hermes_state_messages.py"
    source = target.read_text()
    if MARKER not in source:
        anchor = "    def append_delegation_delivery("
        if source.count(anchor) != 1:
            raise RuntimeError("delivery history SessionDB anchor missing or ambiguous")
        source = source.replace(anchor, "    # " + MARKER + "\n" + (PAYLOAD / "state_method.py.inc").read_text() + anchor, 1)
        compile(source, str(target), "exec")
        target.write_text(source)
    elif (PAYLOAD / "state_method.py.inc").read_text() not in source:
        raise RuntimeError("delivery history marker exists with mismatched payload")
    payload = (Path(__file__).resolve().parents[2] / "kit/bin/telegram_delivery_history.py").read_text()
    compile(payload, "gateway/delivery_history.py", "exec")
    (root / "gateway/delivery_history.py").write_text(payload)
    _patch_integrations(root)
    tests = root / "tests/cron/test_scheduler.py"
    if tests.is_file():
        import ast
        before = tests.read_text()
        lines = before.splitlines(keepends=True)
        for node in ast.walk(ast.parse(before)):
            if isinstance(node, ast.FunctionDef) and node.name == "test_delivery_mirrors_clean_content_not_wrapped":
                block = "".join(lines[node.lineno - 1:node.end_lineno])
                # Golden records acknowledged Telegram delivery through its durable owner.
                block = block.replace('new=AsyncMock(return_value={"success": True})', 'new=AsyncMock(return_value={"success": True, "message_id": "fixture-receipt"})')
                block = block.replace('patch("gateway.mirror.mirror_to_session", return_value=True)', 'patch("gateway.delivery_history.record_cron_delivery", return_value=True)')
                block = block.replace('mirrored_text = mirror_mock.call_args[0][2]', 'assert mirror_mock.call_args.args[1] == "fixture-receipt"\n        mirrored_text = mirror_mock.call_args.args[0].mirror_text')
                lines[node.lineno - 1:node.end_lineno] = [block]
                break
        after = "".join(lines)
        if after != before:
            compile(after, str(tests), "exec")
            tests.write_text(after)
    return True


def _replace(root, relative, old, new):
    path = root / relative
    source = path.read_text()
    if new in source:
        return
    if source.count(old) != 1:
        raise RuntimeError(f"{relative}: delivery history anchor missing or ambiguous")
    source = source.replace(old, new, 1)
    compile(source, str(path), "exec")
    path.write_text(source)


def _patch_integrations(root):
    _replace(root, "cron/scheduler_delivery.py",
        "    job = t.job\n    origin = t.origin\n",
        "    job = t.job\n    origin = t.origin\n"
        "    # " + MARKER + "\n"
        "    if t.platform_name == 'telegram':\n"
        "        from gateway.delivery_history import record_cron_delivery\n"
        "        record_cron_delivery(t, delivered_message_id)\n"
        "        return\n")
    _replace(root, "cron/scheduler_delivery.py",
        "    # Thread seeding only happens on the live lane, so no thread_seeded gate applies here.\n",
        "    # " + MARKER + "\n"
        "    if t.platform_name == 'telegram':\n"
        "        from gateway.delivery_history import record_cron_delivery\n"
        "        if 'thread_id' in (result or {}):\n"
        "            t.thread_id = result['thread_id']\n"
        "            t.opened_thread_id = None\n"
        "        record_cron_delivery(t, (result or {}).get('message_id'))\n"
        "        return\n"
        "    # Thread seeding only happens on the live lane, so no thread_seeded gate applies here.\n")
    _replace(root, "cron/scheduler_tick.py",
        "        _sched._maybe_reap_dead_owners()\n",
        "        # " + MARKER + "\n"
        "        from gateway.delivery_history import retry_runtime_pending\n"
        "        retry_runtime_pending()\n"
        "        _sched._maybe_reap_dead_owners()\n")
    _replace(root, "gateway/run_turn.py",
        "        return source, session_entry, session_key",
        "        # " + MARKER + "\n"
        "        from gateway.delivery_history import retry_runtime_pending\n"
        "        await asyncio.to_thread(retry_runtime_pending)\n"
        "        return source, session_entry, session_key")
    _replace(root, "tools/send_message_tool.py",
        '            if mirror_text and _mirror_sent_message(platform_name, chat_id, mirror_text, thread_id):\n',
        "            # " + MARKER + "\n"
        "            if platform_name == 'telegram' and mirror_text:\n"
        "                from gateway.delivery_history import record_acknowledged_message\n"
        "                from gateway.session_context import get_session_env\n"
        "                result['history'] = record_acknowledged_message(platform=platform_name,\n"
        "                    chat_id=chat_id, thread_id=result.get('thread_id', thread_id), text=mirror_text,\n"
        "                    user_id=get_session_env('HERMES_SESSION_USER_ID', '') or None,\n"
        "                    message_id=result.get('message_id'))\n"
        "                result['mirrored'] = result['history']['status'] == 'recorded'\n"
        "            elif mirror_text and _mirror_sent_message(platform_name, chat_id, mirror_text, thread_id):\n")

    _replace(root, "tools/send_message_senders.py",
        '        return _success("telegram", chat_id, warnings, message_id=str(last_msg.message_id))\n',
        '        return _success("telegram", chat_id, warnings, message_id=str(last_msg.message_id),\n'
        '                        thread_id=getattr(last_msg, "message_thread_id", None))\n')
    _replace(root, "cron/scheduler_delivery.py",
        '        requested_thread_id = send_raw_response.get("requested_thread_id") or t.thread_id\n',
        '        requested_thread_id = send_raw_response.get("requested_thread_id") or t.thread_id\n'
        '        # History follows the acknowledged root fallback, never the requested topic.\n'
        '        t.thread_id = None\n'
        '        t.opened_thread_id = None\n')
    _replace(root, "tools/send_message_tool.py",
        '            return {"success": True, "message_id": result.message_id}\n',
        '            response = {"success": True, "message_id": result.message_id}\n'
        '            if platform.value == "telegram":\n'
        '                raw = getattr(result, "raw_response", None) or {}\n'
        '                response["thread_id"] = None if raw.get("thread_fallback") else thread_id\n'
        '            return response\n')
