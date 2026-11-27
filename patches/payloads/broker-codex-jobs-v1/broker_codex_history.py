"""Recover a complete saved plaintext conversation without executing prior tools."""
from collections import Counter
import hashlib
import json
import os
import stat
from pathlib import Path
import time
import uuid


class ConversationUnavailable(RuntimeError):
    pass


def _message_key(item):
    return hashlib.sha256(json.dumps({'role': item.get('role'), 'content': item.get('content')},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def restore_conversation(body, codex_home, thread_id, deadline):
    """Replace an observed opaque checkpoint with its saved dialogue and tool references."""
    items = body.get('input')
    if (not isinstance(items, list) or body.get('previous_response_id')
            or any(isinstance(item, dict) and item.get('type') == 'item_reference' for item in items)):
        raise ConversationUnavailable('The saved context format cannot be restored safely')
    checkpoints = [(index, item) for index, item in enumerate(items)
                   if isinstance(item, dict) and item.get('type') == 'compaction']
    if not checkpoints or not isinstance(checkpoints[-1][1].get('encrypted_content'), str):
        raise ConversationUnavailable('A saved conversation checkpoint is required')
    index, checkpoint = checkpoints[-1]
    home = Path(codex_home).resolve(strict=True)
    identifier = str(uuid.UUID(thread_id))
    matches = [path for folder in ('sessions', 'archived_sessions')
               for path in (home / folder).rglob('*-' + identifier + '.jsonl')]
    if len(matches) != 1:
        raise ConversationUnavailable('The saved conversation is missing or ambiguous')
    path = matches[0]
    path.resolve(strict=True).relative_to(home)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != home and parent.is_relative_to(home)):
        raise ConversationUnavailable('The saved conversation cannot be a symlink')
    messages, pending, completed = [], {}, []
    recovered = None
    session_matches = False
    original_turn_started = False
    saw_original_user = False
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        identity = os.fstat(stream.fileno())
        if not stat.S_ISREG(identity.st_mode):
            raise ConversationUnavailable('The saved conversation is not a regular file')
        if os.name != 'nt' and identity.st_uid != os.getuid():
            raise ConversationUnavailable('The saved conversation belongs to another owner')
        for line_number, line in enumerate(stream, 1):
            if time.monotonic() >= deadline:
                raise TimeoutError('Conversation recovery deadline expired')
            record = json.loads(line)
            payload = record.get('payload') or {}
            kind = record.get('type')
            if kind == 'session_meta':
                if payload.get('id') != identifier or payload.get('forked_from_id'):
                    raise ConversationUnavailable('The saved conversation identity is not complete')
                session_matches = True
            elif kind == 'event_msg' and payload.get('type') == 'task_started':
                original_turn_started = True
            elif kind == 'event_msg' and payload.get('type') in {'thread_rolled_back', 'turn_rolled_back'}:
                raise ConversationUnavailable('A rolled-back conversation requires native reconstruction')
            elif kind == 'response_item' and recovered is None:
                item_type = payload.get('type')
                if item_type == 'message':
                    if payload.get('role') not in {'user', 'assistant', 'developer', 'system'}:
                        raise ConversationUnavailable('The saved message role is unsupported')
                    if (not isinstance(payload.get('content'), list) or any(
                            not isinstance(part, dict) or part.get('type') not in {'input_text', 'output_text'}
                            or not isinstance(part.get('text'), str) for part in payload['content'])):
                        raise ConversationUnavailable('The saved conversation is not fully plaintext')
                    messages.append(payload)
                    saw_original_user = saw_original_user or payload.get("role") == "user"
                elif item_type in {'function_call', 'custom_tool_call'}:
                    call_id = payload.get('call_id')
                    if not isinstance(call_id, str) or not call_id or call_id in pending:
                        raise ConversationUnavailable('The saved tool identity is ambiguous')
                    pending[call_id] = {'call_id': call_id, 'tool': payload.get('name'), 'call_line': line_number}
                elif item_type in {'function_call_output', 'custom_tool_call_output'}:
                    call = pending.pop(payload.get('call_id'), None)
                    if call is None:
                        raise ConversationUnavailable('The saved tool result has no matching call')
                    completed.append({**call, 'output_line': line_number})
                elif item_type != 'reasoning':
                    raise ConversationUnavailable('The saved context contains unsupported items')
            elif kind == 'compacted' and recovered is None:
                if not original_turn_started or not saw_original_user:
                    raise ConversationUnavailable('The retained file begins from a compacted history base')
                replacement = payload.get('replacement_history') or []
                if any(item.get('type') == 'compaction' and item.get('encrypted_content') == checkpoint['encrypted_content']
                       for item in replacement if isinstance(item, dict)):
                    if pending or not session_matches:
                        raise ConversationUnavailable('The checkpoint contains incomplete tool work')
                    historical = Counter(_message_key(item) for item in messages)
                    anchors = Counter(_message_key(item) for item in replacement
                                      if item.get('type') == 'message' and item.get('role') in {'user', 'assistant'})
                    if any(historical[key] < count for key, count in anchors.items()):
                        raise ConversationUnavailable('The checkpoint contains unretained conversation messages')
                    prefix = []
                    for item in items[:index]:
                        if item.get('type') == 'message':
                            key = _message_key(item)
                            if historical[key]:
                                historical[key] -= 1
                            else:
                                prefix.append(item)
                        elif item.get('type') == 'additional_tools' and item.get('role') == 'developer' and isinstance(item.get('tools'), list):
                            prefix.append(item)
                        elif item.get('type') not in {'reasoning', 'compaction'}:
                            raise ConversationUnavailable('The checkpoint prefix cannot be reconstructed')
                    ledger = {'history_file': str(path), 'completed_tools': completed}
                    note = ('Internal recovery context: the saved user and assistant conversation precedes this note. '
                            'The indexed tool calls already completed. Do not execute them again merely to rebuild history. '
                            'Their full inputs and outputs remain at the indexed JSONL lines; read them if needed for the current task. '
                            'Keep this bookkeeping out of user-facing answers.\n' + json.dumps(ledger, separators=(',', ':')))
                    recovered = [*messages, {'type': 'message', 'role': 'developer',
                                            'content': [{'type': 'input_text', 'text': note}]}, *prefix, *items[index + 1:]]
        current = path.stat(follow_symlinks=False)
        if (not session_matches or recovered is None or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
                or path.is_symlink()):
            raise ConversationUnavailable('The exact saved checkpoint is unavailable')
    return {key: value for key, value in {**body, 'input': recovered}.items() if key != 'previous_response_id'}
