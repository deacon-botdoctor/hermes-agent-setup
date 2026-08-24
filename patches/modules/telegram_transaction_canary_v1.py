#!/usr/bin/env python3
# ruff: noqa: E501
"""Install immutable receipts across Telegram ingress, command, delivery, and slash-confirm seams."""

from __future__ import annotations

import ast
import importlib.util
import shutil
from pathlib import Path

MARKER = "HERMES_TELEGRAM_TRANSACTION_CANARY_v1"
BYPASS_MARKER = "HERMES_TELEGRAM_COMMAND_BYPASS_TRANSACTION_v1"
BYPASS_RECEIPT_MARKER = "HERMES_TELEGRAM_COMMAND_SEND_RECEIPT_COMPAT_v1"
CONFIRM_MARKER = "HERMES_TELEGRAM_SLASH_CONFIRM_ACCEPTANCE_v1"
CONFIRM_RESOLVE_MARKER = "HERMES_TELEGRAM_SLASH_CONFIRM_RESOLUTION_v1"
BACKUP_SUFFIX = ".bak-pre-telegram-transaction-canary-v1"
PAYLOAD = (
    Path(__file__).resolve().parents[1]
    / "payloads"
    / "telegram-transaction-canary"
    / "gateway"
    / "telegram_transaction_ledger.py"
)
OLD_FINISH = '            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)\n            _telegram_tx.finish(failed=not processing_ok, error=None if processing_ok else "processing or delivery failed")\n'
NEW_FINISH = '            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)\n            semantic_failure = _telegram_tx.is_error_envelope(response)\n            _telegram_tx.finish(\n                failed=not processing_ok or semantic_failure,\n                error="agent error envelope" if semantic_failure else None if processing_ok else "processing or delivery failed",\n            )\n'
NORMAL_FINISH = (
    "            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)\n"
)
NORMAL_FINISH_BLOCK = NEW_FINISH
OLD_FINISH_BLOCK = OLD_FINISH
NORMAL_FINISH_WITHOUT_TERMINAL = (
    "            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)\n"
    "            semantic_failure = _telegram_tx.is_error_envelope(response)\n"
)
NORMAL_FINISH_WITHOUT_SEMANTIC = NORMAL_FINISH_BLOCK.replace(
    "            semantic_failure = _telegram_tx.is_error_envelope(response)\n",
    "",
)
BASE_IMPORT = "import asyncio\n"
BASE_IMPORT_BLOCK = f"import asyncio\n\n# {MARKER}\nfrom gateway import telegram_transaction_ledger as _telegram_tx\n"
NORMAL_DELIVERY = '            if getattr(result, "success", False):\n                delivery_succeeded = True\n'
NORMAL_DELIVERY_BLOCK_V1 = NORMAL_DELIVERY + (
    '                _telegram_tx.accepted(getattr(result, "message_id", None))\n'
    "            else:\n"
    '                _telegram_tx.finish(failed=True, error=getattr(result, "error", "delivery failed"))\n'
)
NORMAL_DELIVERY_BLOCK = NORMAL_DELIVERY + (
    '                _telegram_tx.accepted(getattr(result, "message_id", None))\n'
    "            else:\n"
    '                _telegram_tx.delivery_failed(getattr(result, "error", "delivery failed"))\n'
)
NORMAL_CANCEL = "        except asyncio.CancelledError:\n            current_task = asyncio.current_task()\n"
NORMAL_CANCEL_BLOCK = (
    "        except asyncio.CancelledError:\n"
    '            _telegram_tx.finish(failed=True, error="cancelled")\n'
    "            current_task = asyncio.current_task()\n"
)
NORMAL_EXCEPTION = (
    "        except Exception as e:\n"
    '            await self._run_processing_hook("on_processing_complete", event, ProcessingOutcome.FAILURE)\n'
)
NORMAL_EXCEPTION_BLOCK = (
    "        except Exception as e:\n"
    "            _telegram_tx.finish(failed=True, error=e)\n"
    '            await self._run_processing_hook("on_processing_complete", event, ProcessingOutcome.FAILURE)\n'
)
NORMAL_MODEL_FINISHED = "            response, _ephemeral_ttl = self._unwrap_ephemeral(response)\n"
NORMAL_MODEL_FINISHED_BLOCK = NORMAL_MODEL_FINISHED + "            _telegram_tx.model_finished()\n"
STREAM_IMPORT = "from gateway.platforms.base import MEDIA_TAG_CLEANUP_RE\n"
STREAM_IMPORT_BLOCK = STREAM_IMPORT + "from gateway import telegram_transaction_ledger as _telegram_tx\n"
STREAM_MODEL_FINISHED = (
    "                        if item is _DONE:\n"
    "                            got_done = True\n"
)
STREAM_MODEL_FINISHED_BLOCK = (
    "                        if item is _DONE:\n"
    "                            _telegram_tx.model_finished()\n"
    "                            got_done = True\n"
)
STREAM_FINAL = (
    '        except Exception as e:\n            logger.error("Stream consumer error: %s", e)\n\n    # Strip MEDIA:'
)
STREAM_FINAL_BLOCK = (
    '        except Exception as e:\n            logger.error("Stream consumer error: %s", e)\n'
    "        finally:\n"
    '            if (getattr(self, "_final_response_sent", False)\n'
    '                    or getattr(self, "final_response_sent", False)):\n'
    '                _telegram_tx.accepted(getattr(self, "_message_id", None))\n\n'
    "    # Strip MEDIA:"
)
STREAM_FINAL_BLOCK_V1 = (
    '        except Exception as e:\n            logger.error("Stream consumer error: %s", e)\n'
    "        finally:\n"
    '            if (getattr(self, "_final_response_sent", False)\n'
    '                    or getattr(self, "final_response_sent", False)\n'
    '                    or getattr(self, "_final_content_delivered", False)\n'
    '                    or getattr(self, "final_content_delivered", False)):\n'
    '                _telegram_tx.accepted(getattr(self, "_message_id", None))\n\n'
    "    # Strip MEDIA:"
)
STREAM_NATIVE_FINALLY = "        finally:\n            # Safety net: if run() exits (normal return, cancellation, or\n"
STREAM_NATIVE_FINALLY_BLOCK = (
    "        finally:\n"
    '            if (getattr(self, "_final_response_sent", False)\n'
    '                    or getattr(self, "final_response_sent", False)):\n'
    '                _telegram_tx.accepted(getattr(self, "_message_id", None))\n'
    "            # Safety net: if run() exits (normal return, cancellation, or\n"
)
STREAM_NATIVE_FINALLY_BLOCK_V1 = (
    "        finally:\n"
    '            if (getattr(self, "_final_response_sent", False)\n'
    '                    or getattr(self, "final_response_sent", False)\n'
    '                    or getattr(self, "_final_content_delivered", False)\n'
    '                    or getattr(self, "final_content_delivered", False)):\n'
    '                _telegram_tx.accepted(getattr(self, "_message_id", None))\n'
    "            # Safety net: if run() exits (normal return, cancellation, or\n"
)
STREAM_FINAL_EXIT_MODEL_FINISHED_BLOCK = STREAM_FINAL_BLOCK.replace(
    "        finally:\n",
    "        finally:\n            _telegram_tx.model_finished()\n",
    1,
)
STREAM_NATIVE_EXIT_MODEL_FINISHED_BLOCK = STREAM_NATIVE_FINALLY_BLOCK.replace(
    "        finally:\n",
    "        finally:\n            _telegram_tx.model_finished()\n",
    1,
)
STREAM_FINAL_EXIT_MODEL_FINISHED_BLOCK_V1 = STREAM_FINAL_BLOCK_V1.replace(
    "        finally:\n",
    "        finally:\n            _telegram_tx.model_finished()\n",
    1,
)
STREAM_NATIVE_EXIT_MODEL_FINISHED_BLOCK_V1 = STREAM_NATIVE_FINALLY_BLOCK_V1.replace(
    "        finally:\n",
    "        finally:\n            _telegram_tx.model_finished()\n",
    1,
)
BYPASS_RESET_BEGIN = (
    "        logger.debug(\n"
    "            \"[%s] Command '/%s' bypassing active-session guard for %s\",\n"
    "            self.name,\n"
    "            cmd,\n"
    "            session_key,\n"
    "        )\n\n"
    "        current_guard = self._active_sessions.get(session_key)\n"
)
BYPASS_RESET_BEGIN_BLOCK = (
    "        logger.debug(\n"
    "            \"[%s] Command '/%s' bypassing active-session guard for %s\",\n"
    "            self.name,\n"
    "            cmd,\n"
    "            session_key,\n"
    "        )\n\n"
    f"        # {BYPASS_MARKER}: reset-like commands\n"
    "        _tx_error = None\n"
    "        current_guard = self._active_sessions.get(session_key)\n"
)
RESET_SETUP = (
    "        current_guard = self._active_sessions.get(session_key)\n"
    "        command_guard = asyncio.Event()\n"
    "        self._active_sessions[session_key] = command_guard\n"
    "        thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))\n\n"
    "        try:\n"
)
RESET_SETUP_BLOCK = (
    "        current_guard = self._active_sessions.get(session_key)\n"
    "        thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))\n"
    "        command_guard = asyncio.Event()\n"
    "        self._active_sessions[session_key] = command_guard\n"
    '        _telegram_tx.begin(event, getattr(self, "name", "hermes"))\n\n'
    "        try:\n"
)
BYPASS_RESET_ACCEPT = "                if _eph_ttl > 0 and _r.success and _r.message_id:\n"
BYPASS_RESET_RESPONSE = "            _text, _eph_ttl = self._unwrap_ephemeral(response)\n"
BYPASS_RESET_RESPONSE_BLOCK = BYPASS_RESET_RESPONSE + (
    "            _telegram_tx.model_finished()\n"
    '            if _telegram_tx.is_error_envelope(_text):\n                _tx_error = "agent error envelope"\n'
)
BYPASS_RESET_ACCEPT_BLOCK_V1 = (
    "                if _r.success:\n"
    '                    _telegram_tx.accepted(getattr(_r, "message_id", None))\n'
    "                else:\n"
    '                    _tx_error = getattr(_r, "error", None) or "delivery failed"\n' + BYPASS_RESET_ACCEPT
)
BYPASS_RESET_ACCEPT_BLOCK = (
    f"                # {BYPASS_RECEIPT_MARKER}\n"
    '                if _r is None or getattr(_r, "success", True):\n'
    '                    _telegram_tx.accepted(getattr(_r, "message_id", None))\n'
    "                else:\n"
    '                    _tx_error = getattr(_r, "error", None) or "delivery failed"\n'
    '                if _eph_ttl > 0 and _r is not None and getattr(_r, "success", True) and getattr(_r, "message_id", None):\n'
)
BYPASS_RESET_EXCEPT = (
    "        except Exception:\n"
    "            # On failure, restore the original guard\n"
    "            if current_guard is not None:\n"
    "                self._active_sessions[session_key] = current_guard\n"
    "            raise\n"
)
BYPASS_RESET_EXCEPT_PINNED = (
    "        except Exception:\n"
    "            # On failure, restore the original guard if one still exists so\n"
    "            # we don't leave the session in a half-reset state.\n"
    "            if self._active_sessions.get(session_key) is command_guard:\n"
    "                if session_key in self._session_tasks and current_guard is not None:\n"
    "                    self._active_sessions[session_key] = current_guard\n"
    "                else:\n"
    "                    self._release_session_guard(session_key, guard=command_guard)\n"
    "            raise\n"
)
RESET_GUARD_RECOVERY = (
    "            if self._active_sessions.get(session_key) is command_guard:\n"
    "                if session_key in self._session_tasks and current_guard is not None:\n"
    "                    self._active_sessions[session_key] = current_guard\n"
    "                else:\n"
    "                    self._release_session_guard(session_key, guard=command_guard)\n"
)
BYPASS_RESET_EXCEPT_BLOCK = (
    "        except asyncio.CancelledError:\n"
    '            _tx_error = "cancelled"\n' + RESET_GUARD_RECOVERY + "            raise\n"
    "        except Exception as _tx_exc:\n"
    "            _tx_error = _tx_exc\n"
    "            # On failure, restore the original guard\n" + RESET_GUARD_RECOVERY + "            raise\n"
)
BYPASS_RESET_FINAL = "\n        await self._drain_pending_after_session_command(session_key, command_guard)\n"
BYPASS_RESET_FINAL_BLOCK = (
    "        else:\n"
    "            try:\n"
    "                await self._drain_pending_after_session_command(session_key, command_guard)\n"
    "            except asyncio.CancelledError:\n"
    '                _tx_error = "cancelled"\n'
    "                if self._active_sessions.get(session_key) is command_guard:\n"
    "                    if session_key in self._session_tasks and current_guard is not None:\n"
    "                        self._active_sessions[session_key] = current_guard\n"
    "                    else:\n"
    "                        self._release_session_guard(session_key, guard=command_guard)\n"
    "                raise\n"
    "            except Exception as _drain_exc:\n"
    "                _tx_error = _drain_exc\n"
    "                if self._active_sessions.get(session_key) is command_guard:\n"
    "                    if session_key in self._session_tasks and current_guard is not None:\n"
    "                        self._active_sessions[session_key] = current_guard\n"
    "                    else:\n"
    "                        self._release_session_guard(session_key, guard=command_guard)\n"
    "                raise\n"
    "        finally:\n"
    "            _telegram_tx.finish(failed=_tx_error is not None, error=_tx_error)\n"
    "            _telegram_tx.clear()\n"
)
BYPASS_DIRECT_BEGIN = (
    "                try:\n"
    "                    _thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))\n"
)
BYPASS_DIRECT_BEGIN_BLOCK = (
    f"                # {BYPASS_MARKER}: direct commands\n"
    '                _telegram_tx.begin(event, getattr(self, "name", "hermes"))\n'
    "                _tx_error = None\n" + BYPASS_DIRECT_BEGIN
)
BYPASS_DIRECT_ACCEPT = "                        if _eph_ttl > 0 and _r.success and _r.message_id:\n"
BYPASS_DIRECT_RESPONSE = "                    _text, _eph_ttl = self._unwrap_ephemeral(response)\n"
BYPASS_DIRECT_RESPONSE_BLOCK = BYPASS_DIRECT_RESPONSE + (
    "                    _telegram_tx.model_finished()\n"
    "                    if _telegram_tx.is_error_envelope(_text):\n"
    '                        _tx_error = "agent error envelope"\n'
)
BYPASS_DIRECT_ACCEPT_BLOCK_V1 = (
    "                        if _r.success:\n"
    '                            _telegram_tx.accepted(getattr(_r, "message_id", None))\n'
    "                        else:\n"
    '                            _tx_error = getattr(_r, "error", None) or "delivery failed"\n' + BYPASS_DIRECT_ACCEPT
)
BYPASS_DIRECT_ACCEPT_BLOCK = (
    f"                        # {BYPASS_RECEIPT_MARKER}\n"
    '                        if _r is None or getattr(_r, "success", True):\n'
    '                            _telegram_tx.accepted(getattr(_r, "message_id", None))\n'
    "                        else:\n"
    '                            _tx_error = getattr(_r, "error", None) or "delivery failed"\n'
    '                        if _eph_ttl > 0 and _r is not None and getattr(_r, "success", True) and getattr(_r, "message_id", None):\n'
)
BYPASS_DIRECT_EXCEPT = (
    "                except Exception as e:\n"
    "                    logger.error(\"[%s] Command '/%s' dispatch failed: %s\", self.name, cmd, e, exc_info=True)\n"
    "                return\n"
)
BYPASS_DIRECT_EXCEPT_BLOCK = (
    "                except asyncio.CancelledError:\n"
    '                    _tx_error = "cancelled"\n'
    "                    raise\n"
    "                except Exception as e:\n"
    "                    _tx_error = e\n"
    "                    logger.error(\"[%s] Command '/%s' dispatch failed: %s\", self.name, cmd, e, exc_info=True)\n"
    "                finally:\n"
    "                    _telegram_tx.finish(failed=_tx_error is not None, error=_tx_error)\n"
    "                    _telegram_tx.clear()\n"
    "                return\n"
)
TELEGRAM_IMPORT = "from gateway.config import Platform, PlatformConfig\n"
TELEGRAM_IMPORT_BLOCK = (
    "from gateway.config import Platform, PlatformConfig\n"
    "from gateway import telegram_transaction_ledger as _telegram_tx\n"
)
SLASH_CONFIRM_SEND = (
    "            msg = await self._send_message_with_thread_fallback(**kwargs)\n"
    "            self._slash_confirm_state[confirm_id] = session_key\n"
)
SLASH_CONFIRM_SEND_BLOCK = (
    "            msg = await self._send_message_with_thread_fallback(**kwargs)\n"
    f"            # {CONFIRM_MARKER}\n"
    "            _telegram_tx.accepted(str(msg.message_id))\n"
    "            _telegram_tx.slash_confirm_requested(\n"
    "                msg.message_id, session_key, confirm_id,\n"
    '                getattr(self, "_session_store", None),\n'
    "            )\n"
    "            self._slash_confirm_state[confirm_id] = session_key\n"
)
SLASH_CONFIRM_SEND_V1_BLOCK = (
    "            msg = await self._send_message_with_thread_fallback(**kwargs)\n"
    f"            # {CONFIRM_MARKER}\n"
    "            _telegram_tx.accepted(str(msg.message_id))\n"
    "            self._slash_confirm_state[confirm_id] = session_key\n"
)
SLASH_CONFIRM_RESOLVE = (
    "                    result_text = await _slash_confirm_mod.resolve(\n"
    "                        session_key, confirm_id, choice,\n"
    "                    )\n"
    "                    if result_text and query.message:\n"
)
SLASH_CONFIRM_RESOLVE_FORMATTED = (
    "                    result_text = await _slash_confirm_mod.resolve(\n"
    "                        session_key,\n"
    "                        confirm_id,\n"
    "                        choice,\n"
    "                    )\n"
    "                    if result_text and query.message:\n"
)
SLASH_CONFIRM_RESOLVE_BLOCK = (
    "                    result_text = await _slash_confirm_mod.resolve(\n"
    "                        session_key, confirm_id, choice,\n"
    "                    )\n"
    f"                    # {CONFIRM_RESOLVE_MARKER}\n"
    "                    if result_text and query.message:\n"
    "                        _telegram_tx.slash_confirm_resolved(\n"
    "                            query.message.message_id, session_key, confirm_id, choice, data,\n"
    '                            session_store=getattr(self, "_session_store", None),\n'
    '                            update_id=getattr(update, "update_id", None),\n'
    "                            chat_id=query_chat_id, thread_id=query_thread_id,\n"
    '                            sender_user_id=getattr(query.from_user, "id", None),\n'
    "                        )\n"
)
NORMAL_BEGIN = (
    "    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:\n"
    '        """Background task that actually processes the message."""\n'
)
NORMAL_BEGIN_BLOCK = NORMAL_BEGIN + '        _telegram_tx.begin(event, getattr(self, "name", "hermes"))\n'
PINNED_CLEAR = "        finally:\n            # Stop typing before any deferred callback work."
LEGACY_PINNED_CLEAR_BLOCK = (
    "        finally:\n            _telegram_tx.finalize_progress_cleanup()\n            # Stop typing before any deferred callback work."
)
OVERLAY_CLEAR = "        finally:\n            # Fire any one-shot post-delivery callback registered for this\n"
LEGACY_OVERLAY_CLEAR_BLOCK = (
    "        finally:\n"
    "            _telegram_tx.finalize_progress_cleanup()\n"
    "            # Fire any one-shot post-delivery callback registered for this\n"
)
CLEANUP_FINALIZE_BLOCK = (
    "            except BaseException as _cleanup_error:\n"
    "                _telegram_tx.finish(failed=True, error=_cleanup_error)\n"
    "                _telegram_tx.abort_progress_cleanup()\n"
    "                raise\n"
    "            else:\n"
    "                _telegram_tx.finalize_progress_cleanup()\n"
)


def _once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"{label}: anchor drift")
    return text.replace(old, new, 1)


def _ensure(
    text: str,
    old: str,
    new: str,
    label: str,
    damaged: tuple[str, ...] = (),
) -> str:
    if new in text:
        return text
    for candidate in damaged:
        if candidate in text:
            return _once(text, candidate, new, label)
    return _once(text, old, new, label)


def _without_line(block: str, fragment: str) -> str:
    return "".join(line for line in block.splitlines(keepends=True) if fragment not in line)


def _patch_method(
    text: str,
    class_name: str,
    method_name: str,
    patcher,
) -> str:
    tree = ast.parse(text)
    matches = [
        child
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name
    ]
    if len(matches) != 1 or matches[0].end_lineno is None:
        raise RuntimeError(f"missing unique {class_name}.{method_name}")
    lines = text.splitlines(keepends=True)
    method = matches[0]
    start = sum(len(line) for line in lines[: method.lineno - 1])
    end = sum(len(line) for line in lines[: method.end_lineno])
    return text[:start] + patcher(text[start:end]) + text[end:]


def _patch_method_lexical(
    text: str,
    class_name: str,
    method_name: str,
    patcher,
) -> str:
    class_prefix = f"class {class_name}"
    if text.count(class_prefix) != 1:
        raise RuntimeError(f"missing unique class boundary: {class_name}")
    class_start = text.index(class_prefix)
    class_end_candidates = [
        position
        for token in ("\nclass ", "\ndef ", "\nasync def ")
        if (position := text.find(token, class_start + len(class_prefix))) >= 0
    ]
    class_end = min(class_end_candidates) + 1 if class_end_candidates else len(text)
    class_text = text[class_start:class_end]
    prefix = f"    async def {method_name}"
    if class_text.count(prefix) != 1:
        raise RuntimeError(f"missing unique {class_name}.{method_name} boundary")
    start = class_start + class_text.index(prefix)
    candidates = [
        position
        for token in ("\n    async def ", "\n    def ")
        if (position := text.find(token, start + len(prefix))) >= 0
    ]
    end = min(candidates) + 1 if candidates else len(text)
    return text[:start] + patcher(text[start:end]) + text[end:]


def _has_ledger_alias(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "gateway"
        and any(alias.name == "telegram_transaction_ledger" and alias.asname == "_telegram_tx" for alias in node.names)
        for node in tree.body
    )


def _ensure_ledger_import(text: str, kind: str) -> str:
    tree = ast.parse(text)
    if _has_ledger_alias(tree):
        return text
    if kind == "base":
        text = text.replace(f"# {MARKER}\n", "")
        tree = ast.parse(text)
        matches = [
            node
            for node in tree.body
            if isinstance(node, ast.Import) and any(alias.name == "asyncio" for alias in node.names)
        ]
        insertion = f"\n# {MARKER}\nfrom gateway import telegram_transaction_ledger as _telegram_tx\n"
    else:
        module = "gateway.config" if kind == "telegram" else "gateway.platforms.base"
        name = "Platform" if kind == "telegram" else "MEDIA_TAG_CLEANUP_RE"
        matches = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == module
            and any(alias.name == name for alias in node.names)
        ]
        insertion = "from gateway import telegram_transaction_ledger as _telegram_tx\n"
    if len(matches) != 1 or matches[0].end_lineno is None:
        raise RuntimeError(f"missing unique {kind} import anchor")
    lines = text.splitlines(keepends=True)
    offset = sum(len(line) for line in lines[: matches[0].end_lineno])
    return text[:offset] + insertion + text[offset:]


def _bypass_hooks_current(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    reset = _method_source(tree, text, "BasePlatformAdapter", "_dispatch_active_session_command")
    direct = _method_source(tree, text, "BasePlatformAdapter", "handle_message")
    return all(
        block in reset
        for block in (
            BYPASS_RESET_BEGIN_BLOCK,
            RESET_SETUP_BLOCK,
            BYPASS_RESET_RESPONSE_BLOCK,
            BYPASS_RESET_ACCEPT_BLOCK,
            BYPASS_RESET_EXCEPT_BLOCK,
            BYPASS_RESET_FINAL_BLOCK,
        )
    ) and all(
        block in direct
        for block in (
            BYPASS_DIRECT_BEGIN_BLOCK,
            BYPASS_DIRECT_RESPONSE_BLOCK,
            BYPASS_DIRECT_ACCEPT_BLOCK,
            BYPASS_DIRECT_EXCEPT_BLOCK,
        )
    )


def _with_reset_hooks(text: str) -> str:
    if BYPASS_RESET_ACCEPT_BLOCK_V1 in text:
        text = text.replace(BYPASS_RESET_ACCEPT_BLOCK_V1, BYPASS_RESET_ACCEPT_BLOCK, 1)
    text = _ensure(
        text,
        BYPASS_RESET_BEGIN,
        BYPASS_RESET_BEGIN_BLOCK,
        "reset bypass begin",
        (_without_line(BYPASS_RESET_BEGIN_BLOCK, "_telegram_tx.begin"),),
    )
    text = _ensure(
        text,
        RESET_SETUP,
        RESET_SETUP_BLOCK,
        "reset bypass setup",
        (_without_line(RESET_SETUP_BLOCK, "_telegram_tx.begin"),),
    )
    text = _ensure(
        text,
        BYPASS_RESET_RESPONSE,
        BYPASS_RESET_RESPONSE_BLOCK,
        "reset bypass response",
        (_without_line(BYPASS_RESET_RESPONSE_BLOCK, "_telegram_tx.model_finished"),),
    )
    text = _ensure(
        text,
        BYPASS_RESET_ACCEPT,
        BYPASS_RESET_ACCEPT_BLOCK,
        "reset bypass acceptance",
        (_without_line(BYPASS_RESET_ACCEPT_BLOCK, "_telegram_tx.accepted"),),
    )
    text = _ensure(
        text,
        BYPASS_RESET_EXCEPT,
        BYPASS_RESET_EXCEPT_BLOCK,
        "reset bypass failure",
        (BYPASS_RESET_EXCEPT_PINNED,),
    )
    return _ensure(
        text,
        BYPASS_RESET_FINAL,
        BYPASS_RESET_FINAL_BLOCK,
        "reset bypass finish",
        (
            _without_line(BYPASS_RESET_FINAL_BLOCK, "_telegram_tx.finish"),
            _without_line(BYPASS_RESET_FINAL_BLOCK, "_telegram_tx.clear"),
            _without_line(
                _without_line(BYPASS_RESET_FINAL_BLOCK, "_telegram_tx.finish"),
                "_telegram_tx.clear",
            ),
        ),
    )


def _with_direct_hooks(text: str) -> str:
    if BYPASS_DIRECT_ACCEPT_BLOCK_V1 in text:
        text = text.replace(BYPASS_DIRECT_ACCEPT_BLOCK_V1, BYPASS_DIRECT_ACCEPT_BLOCK, 1)
    text = _ensure(
        text,
        BYPASS_DIRECT_BEGIN,
        BYPASS_DIRECT_BEGIN_BLOCK,
        "direct bypass begin",
        (_without_line(BYPASS_DIRECT_BEGIN_BLOCK, "_telegram_tx.begin"),),
    )
    text = _ensure(
        text,
        BYPASS_DIRECT_RESPONSE,
        BYPASS_DIRECT_RESPONSE_BLOCK,
        "direct bypass response",
        (_without_line(BYPASS_DIRECT_RESPONSE_BLOCK, "_telegram_tx.model_finished"),),
    )
    text = _ensure(
        text,
        BYPASS_DIRECT_ACCEPT,
        BYPASS_DIRECT_ACCEPT_BLOCK,
        "direct bypass acceptance",
        (_without_line(BYPASS_DIRECT_ACCEPT_BLOCK, "_telegram_tx.accepted"),),
    )
    return _ensure(
        text,
        BYPASS_DIRECT_EXCEPT,
        BYPASS_DIRECT_EXCEPT_BLOCK,
        "direct bypass finish",
        (
            _without_line(BYPASS_DIRECT_EXCEPT_BLOCK, "_telegram_tx.finish"),
            _without_line(BYPASS_DIRECT_EXCEPT_BLOCK, "_telegram_tx.clear"),
            _without_line(
                _without_line(BYPASS_DIRECT_EXCEPT_BLOCK, "_telegram_tx.finish"),
                "_telegram_tx.clear",
            ),
        ),
    )


def _with_bypass_hooks(text: str) -> str:
    text = _patch_method_lexical(
        text,
        "BasePlatformAdapter",
        "_dispatch_active_session_command",
        _with_reset_hooks,
    )
    text = _patch_method_lexical(
        text,
        "BasePlatformAdapter",
        "handle_message",
        _with_direct_hooks,
    )
    text = _patch_method(
        text,
        "BasePlatformAdapter",
        "_dispatch_active_session_command",
        _with_reset_hooks,
    )
    return _patch_method(
        text,
        "BasePlatformAdapter",
        "handle_message",
        _with_direct_hooks,
    )


def _with_slash_confirm_hook(text: str) -> str:
    text = _patch_method_lexical(
        text,
        "TelegramAdapter",
        "send_slash_confirm",
        _with_slash_confirm_method_hook,
    )
    text = _patch_method_lexical(
        text,
        "TelegramAdapter",
        "_handle_callback_query",
        _with_slash_confirm_resolve_hook,
    )
    text = _ensure_ledger_import(text, "telegram")
    text = _patch_method(
        text,
        "TelegramAdapter",
        "send_slash_confirm",
        _with_slash_confirm_method_hook,
    )
    return _patch_method(
        text,
        "TelegramAdapter",
        "_handle_callback_query",
        _with_slash_confirm_resolve_hook,
    )


def _with_slash_confirm_method_hook(text: str) -> str:
    return _ensure(
        text,
        SLASH_CONFIRM_SEND,
        SLASH_CONFIRM_SEND_BLOCK,
        "slash confirm acceptance",
        (
            SLASH_CONFIRM_SEND_V1_BLOCK,
            _without_line(SLASH_CONFIRM_SEND_BLOCK, "_telegram_tx.accepted"),
        ),
    )


def _with_slash_confirm_resolve_hook(text: str) -> str:
    return _ensure(
        text,
        SLASH_CONFIRM_RESOLVE,
        SLASH_CONFIRM_RESOLVE_BLOCK,
        "slash confirm resolution",
        (SLASH_CONFIRM_RESOLVE_FORMATTED,),
    )


def _slash_confirm_hook_current(text: str) -> bool:
    tree = ast.parse(text)
    send = _method_source(tree, text, "TelegramAdapter", "send_slash_confirm")
    callback = _method_source(tree, text, "TelegramAdapter", "_handle_callback_query")
    return SLASH_CONFIRM_SEND_BLOCK in send and SLASH_CONFIRM_RESOLVE_BLOCK in callback and _has_ledger_alias(tree)


def _with_clear_hook(text: str) -> str:
    if _normal_clear_current(text):
        return text
    for legacy in (LEGACY_PINNED_CLEAR_BLOCK, LEGACY_OVERLAY_CLEAR_BLOCK):
        if legacy in text:
            text = text.replace(
                "            _telegram_tx.finalize_progress_cleanup()\n",
                "",
                1,
            )
            break
    if PINNED_CLEAR not in text and OVERLAY_CLEAR not in text:
        raise RuntimeError("clear: anchor drift")
    return _wrap_finally_cleanup(text)


def _core_finally(text: str) -> ast.Try:
    tree = ast.parse("class _Canary:\n" + text)
    class_node = tree.body[0]
    if not isinstance(class_node, ast.ClassDef) or len(class_node.body) != 1:
        raise RuntimeError("clear: method boundary drift")
    method = class_node.body[0]
    if not isinstance(method, ast.AsyncFunctionDef):
        raise RuntimeError("clear: method boundary drift")
    matches = [node for node in method.body if isinstance(node, ast.Try) and node.finalbody]
    if len(matches) != 1:
        raise RuntimeError("clear: finally boundary drift")
    return matches[0]


def _wrap_finally_cleanup(text: str) -> str:
    final_try = _core_finally(text)
    lines = text.splitlines(keepends=True)
    first_body_line = final_try.finalbody[0].lineno - 2
    finally_line = next(
        (
            index
            for index in range(first_body_line, final_try.lineno - 2, -1)
            if lines[index].strip() == "finally:"
        ),
        None,
    )
    if finally_line is None or final_try.finalbody[-1].end_lineno is None:
        raise RuntimeError("clear: finally boundary drift")
    body_end = final_try.finalbody[-1].end_lineno - 1
    body_indent = lines[finally_line][: -len(lines[finally_line].lstrip())] + "    "
    original_body = lines[finally_line + 1 : body_end]
    wrapped_body = [f"{body_indent}try:\n"]
    wrapped_body.extend(
        f"    {line}" if line.strip() else line for line in original_body
    )
    wrapped_body.extend(CLEANUP_FINALIZE_BLOCK.splitlines(keepends=True))
    return "".join(lines[: finally_line + 1] + wrapped_body + lines[body_end:])


def _unwrap_finally_cleanup(text: str) -> str:
    final_try = _core_finally(text)
    if len(final_try.finalbody) != 1 or not isinstance(final_try.finalbody[0], ast.Try):
        raise RuntimeError("historical clear rollback: anchor drift")
    cleanup_try = final_try.finalbody[0]
    if cleanup_try.end_lineno is None or not cleanup_try.body or cleanup_try.body[-1].end_lineno is None:
        raise RuntimeError("historical clear rollback: anchor drift")
    lines = text.splitlines(keepends=True)
    start = cleanup_try.lineno - 2
    body_end = cleanup_try.body[-1].end_lineno - 1
    end = cleanup_try.end_lineno - 1
    original_body = [
        line[4:] if line.startswith("    ") else line
        for line in lines[start + 1 : body_end]
    ]
    return "".join(lines[:start] + original_body + lines[end:])


def _normal_clear_current(text: str) -> bool:
    return CLEANUP_FINALIZE_BLOCK in text


def _core_hooks_current(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    process = _method_source(tree, text, "BasePlatformAdapter", "_process_message_background")
    return (
        all(
            block in process
            for block in (
                NORMAL_BEGIN_BLOCK,
                NORMAL_DELIVERY_BLOCK,
                NORMAL_FINISH_BLOCK,
                NORMAL_MODEL_FINISHED_BLOCK,
                NORMAL_CANCEL_BLOCK,
                NORMAL_EXCEPTION_BLOCK,
            )
        )
        and _normal_clear_current(process)
        and _has_ledger_alias(tree)
    )


def _with_core_hooks(text: str) -> str:
    text = _patch_method_lexical(
        text,
        "BasePlatformAdapter",
        "_process_message_background",
        _with_core_method_hooks,
    )
    text = _ensure_ledger_import(text, "base")
    return _patch_method(
        text,
        "BasePlatformAdapter",
        "_process_message_background",
        _with_core_method_hooks,
    )


def _with_core_method_hooks(text: str) -> str:
    text = _ensure(
        text,
        NORMAL_BEGIN,
        NORMAL_BEGIN_BLOCK,
        "begin",
        (_without_line(NORMAL_BEGIN_BLOCK, "_telegram_tx.begin"),),
    )
    text = _ensure(
        text,
        NORMAL_MODEL_FINISHED,
        NORMAL_MODEL_FINISHED_BLOCK,
        "model finished",
        (_without_line(NORMAL_MODEL_FINISHED_BLOCK, "_telegram_tx.model_finished"),),
    )
    text = _ensure(
        text,
        NORMAL_DELIVERY,
        NORMAL_DELIVERY_BLOCK,
        "delivery",
        (
            NORMAL_DELIVERY_BLOCK_V1,
            _without_line(NORMAL_DELIVERY_BLOCK, "_telegram_tx.accepted"),
            _without_line(NORMAL_DELIVERY_BLOCK, "_telegram_tx.finish"),
            _without_line(
                _without_line(NORMAL_DELIVERY_BLOCK, "_telegram_tx.accepted"),
                "_telegram_tx.finish",
            ),
        ),
    )
    text = _ensure(
        text,
        NORMAL_FINISH,
        NORMAL_FINISH_BLOCK,
        "finish upgrade",
        (
            OLD_FINISH_BLOCK,
            NORMAL_FINISH_WITHOUT_TERMINAL,
            NORMAL_FINISH_WITHOUT_SEMANTIC,
        ),
    )
    text = _ensure(
        text,
        NORMAL_CANCEL,
        NORMAL_CANCEL_BLOCK,
        "cancel",
        (_without_line(NORMAL_CANCEL_BLOCK, "_telegram_tx.finish"),),
    )
    text = _ensure(
        text,
        NORMAL_EXCEPTION,
        NORMAL_EXCEPTION_BLOCK,
        "exception",
        (_without_line(NORMAL_EXCEPTION_BLOCK, "_telegram_tx.finish"),),
    )
    return _with_clear_hook(text)


def _stream_hooks_current(text: str) -> bool:
    tree = ast.parse(text)
    run = _method_source(tree, text, "GatewayStreamConsumer", "run")
    stream_run_blocks = (
        STREAM_FINAL_BLOCK.split("\n\n    # Strip MEDIA:", 1)[0],
        STREAM_NATIVE_FINALLY_BLOCK,
    )
    return (
        any(block in run for block in stream_run_blocks)
        and STREAM_MODEL_FINISHED_BLOCK in run
        and _has_ledger_alias(tree)
    )


def _with_stream_hooks(text: str) -> str:
    text = _patch_method_lexical(
        text,
        "GatewayStreamConsumer",
        "run",
        _with_stream_method_hooks,
    )
    text = _ensure_ledger_import(text, "stream")
    return _patch_method(
        text,
        "GatewayStreamConsumer",
        "run",
        _with_stream_method_hooks,
    )


def _with_stream_method_hooks(text: str) -> str:
    old = STREAM_FINAL.split("\n\n    # Strip MEDIA:", 1)[0]
    new = STREAM_FINAL_BLOCK.split("\n\n    # Strip MEDIA:", 1)[0]
    for prior, current in (
        (STREAM_NATIVE_EXIT_MODEL_FINISHED_BLOCK_V1, STREAM_NATIVE_FINALLY_BLOCK),
        (STREAM_NATIVE_FINALLY_BLOCK_V1, STREAM_NATIVE_FINALLY_BLOCK),
        (
            STREAM_FINAL_EXIT_MODEL_FINISHED_BLOCK_V1.split("\n\n    # Strip MEDIA:", 1)[0],
            new,
        ),
        (STREAM_FINAL_BLOCK_V1.split("\n\n    # Strip MEDIA:", 1)[0], new),
    ):
        if prior in text:
            text = _once(
                text,
                prior,
                current,
                "stream finalization receipt upgrade",
            )
            break
    prior_native = STREAM_NATIVE_EXIT_MODEL_FINISHED_BLOCK
    damaged_native = _without_line(
        STREAM_NATIVE_FINALLY_BLOCK,
        "_telegram_tx.accepted",
    )
    damaged_prior_native = _without_line(
        prior_native,
        "_telegram_tx.accepted",
    )
    if prior_native in text:
        text = _once(
            text,
            prior_native,
            STREAM_NATIVE_FINALLY_BLOCK,
            "stream native-finally ordering upgrade",
        )
    elif damaged_prior_native in text:
        text = _once(
            text,
            damaged_prior_native,
            STREAM_NATIVE_FINALLY_BLOCK,
            "stream native-finally ordering repair",
        )
    elif damaged_native in text:
        text = _once(
            text,
            damaged_native,
            STREAM_NATIVE_FINALLY_BLOCK,
            "stream native-finally repair",
        )
    elif STREAM_NATIVE_FINALLY_BLOCK not in text:
        if STREAM_NATIVE_FINALLY in text:
            text = _once(
                text,
                STREAM_NATIVE_FINALLY,
                STREAM_NATIVE_FINALLY_BLOCK,
                "stream native-finally",
            )
        else:
            prior_legacy = STREAM_FINAL_EXIT_MODEL_FINISHED_BLOCK.split(
                "\n\n    # Strip MEDIA:", 1
            )[0]
            text = _ensure(
                text,
                old,
                new,
                "stream final",
                (
                    _without_line(new, "_telegram_tx.accepted"),
                    prior_legacy,
                    _without_line(prior_legacy, "_telegram_tx.accepted"),
                ),
            )
    return _ensure(
        text,
        STREAM_MODEL_FINISHED,
        STREAM_MODEL_FINISHED_BLOCK,
        "stream model finished",
    )


def _without_stream_method_hooks(text: str) -> str:
    if STREAM_MODEL_FINISHED_BLOCK in text:
        text = _once(
            text,
            STREAM_MODEL_FINISHED_BLOCK,
            STREAM_MODEL_FINISHED,
            "historical stream model-finished rollback",
        )
    legacy_new = STREAM_FINAL_BLOCK.split("\n\n    # Strip MEDIA:", 1)[0]
    legacy_old = STREAM_FINAL.split("\n\n    # Strip MEDIA:", 1)[0]
    native_blocks = (
        STREAM_NATIVE_EXIT_MODEL_FINISHED_BLOCK,
        STREAM_NATIVE_FINALLY_BLOCK,
        STREAM_NATIVE_EXIT_MODEL_FINISHED_BLOCK_V1,
        STREAM_NATIVE_FINALLY_BLOCK_V1,
    )
    installed_native = next((block for block in native_blocks if block in text), None)
    if installed_native is not None:
        return _once(
            text,
            installed_native,
            STREAM_NATIVE_FINALLY,
            "historical native stream final rollback",
        )
    legacy_blocks = (
        STREAM_FINAL_EXIT_MODEL_FINISHED_BLOCK.split("\n\n    # Strip MEDIA:", 1)[0],
        legacy_new,
        STREAM_FINAL_EXIT_MODEL_FINISHED_BLOCK_V1.split("\n\n    # Strip MEDIA:", 1)[0],
        STREAM_FINAL_BLOCK_V1.split("\n\n    # Strip MEDIA:", 1)[0],
    )
    installed_legacy = next((block for block in legacy_blocks if block in text), None)
    if installed_legacy is None:
        raise RuntimeError("historical stream final rollback: anchor drift")
    return _once(
        text,
        installed_legacy,
        legacy_old,
        "historical stream final rollback",
    )


def _telegram_adapters(root: Path) -> tuple[Path, ...]:
    candidates = (
        root / "plugins/platforms/telegram/adapter.py",
        root / "gateway/platforms/telegram.py",
    )
    alias_groups: list[list[Path]] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        group = next(
            (aliases for aliases in alias_groups if candidate.samefile(aliases[0])),
            None,
        )
        if group is None:
            alias_groups.append([candidate])
        else:
            group.append(candidate)
    representatives = []
    for aliases in alias_groups:
        backup_owners = [alias for alias in aliases if Path(str(alias) + BACKUP_SUFFIX).is_file()]
        if not backup_owners:
            representatives.append(aliases[0])
            continue
        installed_text = aliases[0].read_text()
        matching = []
        for alias in backup_owners:
            backup = Path(str(alias) + BACKUP_SUFFIX)
            try:
                _validate_pre_canary(backup, "telegram", backup=True)
                _validate_backup_matches_installed(
                    backup,
                    installed_text,
                    "telegram",
                )
            except (OSError, RuntimeError, SyntaxError):
                continue
            matching.append(alias)
        if not matching:
            raise RuntimeError("no alias-owned Telegram rollback backup matches installed source")
        if len({Path(str(alias) + BACKUP_SUFFIX).read_bytes() for alias in matching}) > 1:
            raise RuntimeError("ambiguous alias-owned Telegram rollback backups match installed source")
        representatives.append(matching[0])
    existing = tuple(representatives)
    if not existing:
        raise FileNotFoundError("Telegram adapter not found in canonical or legacy path")
    return existing


def _method_source(
    tree: ast.Module,
    text: str,
    class_name: str,
    method_name: str,
) -> str:
    matches = [
        child
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name
    ]
    if len(matches) != 1 or matches[0].end_lineno is None:
        raise RuntimeError(f"missing unique {class_name}.{method_name}")
    lines = text.splitlines(keepends=True)
    method = matches[0]
    return "".join(lines[method.lineno - 1 : method.end_lineno])


def _has_import(tree: ast.Module, module: str, name: str) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == module
        and any(alias.name == name for alias in node.names)
        for node in tree.body
    )


def _validate_pre_canary_text(text: str, kind: str, label: str) -> None:
    try:
        tree = ast.parse(text, filename=label)
        compile(tree, label, "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"pre-canary {kind} invalid: {label}") from exc
    if any(
        fragment in text for fragment in (MARKER, BYPASS_MARKER, CONFIRM_MARKER, CONFIRM_RESOLVE_MARKER, "_telegram_tx")
    ):
        raise RuntimeError(f"pre-canary {kind} contains canary hooks: {label}")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (
            str(node.module or "").endswith("telegram_transaction_ledger")
            or any(alias.name == "telegram_transaction_ledger" for alias in node.names)
        ):
            raise RuntimeError(f"pre-canary {kind} contains ledger import: {label}")
        if isinstance(node, ast.Import) and any(
            alias.name.endswith("telegram_transaction_ledger") for alias in node.names
        ):
            raise RuntimeError(f"pre-canary {kind} contains ledger import: {label}")
        if isinstance(node, ast.Attribute) and node.attr == "telegram_transaction_ledger":
            raise RuntimeError(f"pre-canary {kind} contains ledger reference: {label}")
    try:
        if kind == "base":
            process = _method_source(
                tree,
                text,
                "BasePlatformAdapter",
                "_process_message_background",
            )
            dispatch = _method_source(
                tree,
                text,
                "BasePlatformAdapter",
                "_dispatch_active_session_command",
            )
            if not all(
                process.count(anchor) == 1
                for anchor in (
                    NORMAL_BEGIN,
                    NORMAL_DELIVERY,
                    NORMAL_FINISH,
                    NORMAL_CANCEL,
                    NORMAL_EXCEPTION,
                )
            ) or (PINNED_CLEAR not in process and OVERLAY_CLEAR not in process):
                raise RuntimeError
            if dispatch.count(BYPASS_RESET_BEGIN) != 1:
                raise RuntimeError
        elif kind == "stream":
            run = _method_source(tree, text, "GatewayStreamConsumer", "run")
            legacy_tail = STREAM_FINAL.split("\n\n    # Strip MEDIA:", 1)[0]
            has_native_tail = run.count(STREAM_NATIVE_FINALLY) == 1
            has_legacy_tail = not has_native_tail and run.count(legacy_tail) == 1
            if (
                run.count(STREAM_MODEL_FINISHED) != 1
                or not (has_native_tail or has_legacy_tail)
                or not _has_import(
                    tree, "gateway.platforms.base", "MEDIA_TAG_CLEANUP_RE"
                )
            ):
                raise RuntimeError
        elif kind == "telegram":
            send_confirm = _method_source(
                tree,
                text,
                "TelegramAdapter",
                "send_slash_confirm",
            )
            if send_confirm.count(SLASH_CONFIRM_SEND) != 1 or not _has_import(tree, "gateway.config", "Platform"):
                raise RuntimeError
            callback = _method_source(
                tree,
                text,
                "TelegramAdapter",
                "_handle_callback_query",
            )
            if (
                sum(
                    callback.count(anchor)
                    for anchor in (
                        SLASH_CONFIRM_RESOLVE,
                        SLASH_CONFIRM_RESOLVE_FORMATTED,
                    )
                )
                != 1
            ):
                raise RuntimeError
        else:
            raise RuntimeError
    except RuntimeError as exc:
        raise RuntimeError(f"pre-canary {kind} provenance mismatch: {label}") from exc


def _validate_pre_canary(path: Path, kind: str, *, backup: bool = False) -> None:
    if not path.is_file():
        missing_label = "rollback backup" if backup else "pre-canary source"
        raise RuntimeError(f"{missing_label} missing: {path}")
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"pre-canary {kind} invalid: {path}") from exc
    _validate_pre_canary_text(text, kind, str(path))


def _without_historical_core_hooks(method: str) -> str:
    method = _once(method, NORMAL_BEGIN_BLOCK, NORMAL_BEGIN, "historical begin rollback")
    if NORMAL_MODEL_FINISHED_BLOCK in method:
        method = _once(
            method,
            NORMAL_MODEL_FINISHED_BLOCK,
            NORMAL_MODEL_FINISHED,
            "historical model-finished rollback",
        )
    method = _once(
        method,
        NORMAL_DELIVERY_BLOCK,
        NORMAL_DELIVERY,
        "historical delivery rollback",
    )
    finish_blocks = [block for block in (NORMAL_FINISH_BLOCK, OLD_FINISH_BLOCK) if block in method]
    if len(finish_blocks) != 1:
        raise RuntimeError("historical finish rollback: anchor drift")
    method = _once(
        method,
        finish_blocks[0],
        NORMAL_FINISH,
        "historical finish rollback",
    )
    method = _once(
        method,
        NORMAL_CANCEL_BLOCK,
        NORMAL_CANCEL,
        "historical cancel rollback",
    )
    method = _once(
        method,
        NORMAL_EXCEPTION_BLOCK,
        NORMAL_EXCEPTION,
        "historical exception rollback",
    )
    if not _normal_clear_current(method):
        raise RuntimeError("historical clear rollback: anchor drift")
    return _unwrap_finally_cleanup(method)


def _recover_historical_pre_canary(installed_text: str, kind: str) -> str:
    """Reverse the exact shipped v1 hooks when apply-all cleaned its siblings."""
    if kind == "base":
        if BYPASS_MARKER in installed_text:
            raise RuntimeError("cannot recover pre-canary base after bypass hook install")
        recovered = _once(
            installed_text,
            BASE_IMPORT_BLOCK,
            BASE_IMPORT,
            "historical base import rollback",
        )
        recovered = _patch_method(
            recovered,
            "BasePlatformAdapter",
            "_process_message_background",
            _without_historical_core_hooks,
        )
    elif kind == "stream":
        recovered = _once(
            installed_text,
            STREAM_IMPORT_BLOCK,
            STREAM_IMPORT,
            "historical stream import rollback",
        )
        recovered = _patch_method(
            recovered,
            "GatewayStreamConsumer",
            "run",
            _without_stream_method_hooks,
        )
    else:
        raise RuntimeError(f"historical recovery unsupported for {kind}")
    compile(recovered, f"<recovered-{kind}>", "exec")
    return recovered


def _normalized_canary_source(text: str, kind: str) -> str:
    """Return the current canary form without mutating a runtime tree."""
    if kind == "base":
        text = _patch_method_lexical(
            text,
            "BasePlatformAdapter",
            "_dispatch_active_session_command",
            _with_reset_hooks,
        )
        text = _patch_method_lexical(
            text,
            "BasePlatformAdapter",
            "handle_message",
            _with_direct_hooks,
        )
        normalized = _with_bypass_hooks(_with_core_hooks(text))
        media = _load_ordered_predecessor_module(
            "modules/media_same_path_resend_v1.py",
            "media_same_path_resend_normalizer_dependency",
        )
        return _with_media_delivery_dedup(media, normalized)
    if kind == "stream":
        normalized = _with_stream_hooks(text)
        # Repairing an old missing-call variant can consume the blank line at
        # this patch seam; it is not part of the pre-canary source revision.
        return normalized.replace("\n\n    # Strip MEDIA:", "\n    # Strip MEDIA:")
    if kind == "telegram":
        return _with_slash_confirm_hook(text)
    raise RuntimeError(f"unknown canary source kind: {kind}")


def _validate_backup_matches_installed(
    backup: Path,
    installed_text: str,
    kind: str,
) -> None:
    """Bind a compatible rollback backup to the installed source revision."""
    _validate_backup_text_matches_installed(
        backup.read_text(),
        installed_text,
        kind,
        str(backup),
    )


def _validate_backup_text_matches_installed(
    backup_text: str,
    installed_text: str,
    kind: str,
    label: str,
) -> None:
    backup_normalized = _normalized_canary_source(backup_text, kind)
    installed_normalized = _normalized_canary_source(installed_text, kind)
    if backup_normalized != installed_normalized:
        raise RuntimeError(f"rollback backup does not match installed {kind} source: {label}")


def _load_ordered_predecessor_module(relative_path: str, name: str):
    path = Path(__file__).resolve().parent.parent / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load ordered predecessor: {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _media_resend_plan(root: Path):
    module = _load_ordered_predecessor_module(
        "modules/media_same_path_resend_v1.py",
        "media_same_path_resend_transaction_dependency",
    )
    target = root / "gateway/run.py"
    if not target.is_file():
        raise RuntimeError(f"media resend gateway source missing: {target}")
    original = target.read_text()
    patched = module.patch_source(original)
    backup = Path(str(target) + module.BACKUP_SUFFIX)
    return module, target, original, patched, backup


def _with_media_delivery_dedup(_module, source: str) -> str:
    """Retain native explicit resend behavior.

    Golden's media overlay now limits version-aware deduplication to automatic
    history collection. Explicit MEDIA directives remain model-controlled.
    """
    return source


def _apply_media_resend_plan(plan) -> bool:
    _module, target, original, patched, backup = plan
    if patched is None:
        return False
    backup_created = not backup.exists()
    if not backup_created and backup.read_text() != original:
        raise RuntimeError(f"media resend rollback backup does not match source: {target}")
    if backup_created:
        shutil.copy2(target, backup)
    try:
        target.write_text(patched)
    except Exception:
        target.write_text(original)
        if backup_created:
            backup.unlink(missing_ok=True)
        raise
    return backup_created


def _rollback_media_resend_plan(plan, backup_created: bool) -> None:
    _module, target, original, patched, backup = plan
    if patched is None:
        return
    target.write_text(original)
    if backup_created:
        backup.unlink(missing_ok=True)


def _matches_ordered_base_predecessors(backup_text: str, installed_text: str) -> bool:
    """Accept only the exact root-guard/typing predecessor chain from the registry."""
    try:
        root_guard = _load_ordered_predecessor_module(
            "modules/telegram_dm_topic_recovery_root_guard_v1.py",
            "telegram_dm_topic_recovery_root_guard_predecessor",
        )
        typing_receipt = _load_ordered_predecessor_module(
            "apply-telegram-immediate-typing-receipt.py",
            "telegram_immediate_typing_receipt_predecessor",
        )
        guarded = root_guard.patch_base_source(backup_text)
        if guarded is None:
            return False
        return typing_receipt.patch_base(guarded) == installed_text
    except (ImportError, OSError, RuntimeError, SyntaxError, ValueError):
        return False


def patch_telegram_transaction_canary_v1(root: Path) -> bool:
    root = Path(root)
    media_plan = _media_resend_plan(root)
    payload_text = PAYLOAD.read_text()
    compile(payload_text, str(PAYLOAD), "exec")
    base = root / "gateway/platforms/base.py"
    telegrams = _telegram_adapters(root)
    stream = root / "gateway/stream_consumer.py"
    owned = root / "gateway/telegram_transaction_ledger.py"
    base_text = base.read_text()
    if MARKER in base_text:
        telegram_texts = {telegram: telegram.read_text() for telegram in telegrams}
        stream_text = stream.read_text()
        owned_existed = owned.is_file()
        owned_bytes = owned.read_bytes() if owned_existed else None
        payload_current = owned_bytes == PAYLOAD.read_bytes() if owned_existed else False
        backups = {path: Path(str(path) + BACKUP_SUFFIX) for path in (base, *telegrams, stream)}
        missing_core_backups = [path for path in (base, stream) if not backups[path].exists()]
        if BYPASS_MARKER in base_text and missing_core_backups:
            missing = ", ".join(str(path) for path in missing_core_backups)
            raise RuntimeError(f"cannot recover pre-canary backup after bypass hook install: {missing}")
        recovered_backup_texts = {}
        for path, installed_text, kind in (
            (base, base_text, "base"),
            (stream, stream_text, "stream"),
        ):
            backup = backups[path]
            if backup.exists():
                _validate_pre_canary(backup, kind, backup=True)
                _validate_backup_matches_installed(backup, installed_text, kind)
                continue
            recovered = _recover_historical_pre_canary(installed_text, kind)
            _validate_pre_canary_text(
                recovered,
                kind,
                f"recovered historical {kind} rollback",
            )
            _validate_backup_text_matches_installed(
                recovered,
                installed_text,
                kind,
                f"recovered historical {kind} rollback",
            )
            recovered_backup_texts[backup] = recovered
        telegram_backups_missing = {telegram: not backups[telegram].exists() for telegram in telegrams}
        for telegram in telegrams:
            if telegram_backups_missing[telegram]:
                _validate_pre_canary(telegram, "telegram")
            else:
                _validate_pre_canary(backups[telegram], "telegram", backup=True)
                _validate_backup_matches_installed(backups[telegram], telegram_texts[telegram], "telegram")
        if (
            _core_hooks_current(base_text)
            and payload_current
            and _bypass_hooks_current(base_text)
            and all(_slash_confirm_hook_current(text) for text in telegram_texts.values())
            and _stream_hooks_current(stream_text)
        ):
            media_base = _with_media_delivery_dedup(media_plan[0], base_text)
            if media_plan[3] is None and media_base == base_text:
                return False
            media_backup_created = False
            try:
                if media_base != base_text:
                    base.write_text(media_base)
                media_backup_created = _apply_media_resend_plan(media_plan)
            except Exception:
                base.write_text(base_text)
                _rollback_media_resend_plan(media_plan, media_backup_created)
                raise
            return True
        upgraded = _patch_method_lexical(
            base_text,
            "BasePlatformAdapter",
            "_process_message_background",
            _with_core_method_hooks,
        )
        upgraded = _patch_method_lexical(
            upgraded,
            "BasePlatformAdapter",
            "_dispatch_active_session_command",
            _with_reset_hooks,
        )
        upgraded = _patch_method_lexical(
            upgraded,
            "BasePlatformAdapter",
            "handle_message",
            _with_direct_hooks,
        )
        upgraded = _with_core_hooks(upgraded)
        upgraded = _with_bypass_hooks(upgraded)
        upgraded = _with_media_delivery_dedup(media_plan[0], upgraded)
        upgraded_telegrams = {telegram: _with_slash_confirm_hook(text) for telegram, text in telegram_texts.items()}
        upgraded_stream = _with_stream_hooks(stream_text)
        compile(upgraded, str(base), "exec")
        for telegram, upgraded_telegram in upgraded_telegrams.items():
            compile(upgraded_telegram, str(telegram), "exec")
        compile(upgraded_stream, str(stream), "exec")
        new_backup_paths = set(recovered_backup_texts)
        new_backup_paths.update(backups[telegram] for telegram in telegrams if telegram_backups_missing[telegram])
        media_backup_created = False
        try:
            for backup, recovered in recovered_backup_texts.items():
                backup.write_text(recovered)
            for telegram in telegrams:
                if telegram_backups_missing[telegram]:
                    shutil.copy2(telegram, backups[telegram])
            base.write_text(upgraded)
            for telegram, upgraded_telegram in upgraded_telegrams.items():
                telegram.write_text(upgraded_telegram)
            stream.write_text(upgraded_stream)
            shutil.copy2(PAYLOAD, owned)
            media_backup_created = _apply_media_resend_plan(media_plan)
        except Exception:
            base.write_text(base_text)
            for telegram, telegram_text in telegram_texts.items():
                telegram.write_text(telegram_text)
            stream.write_text(stream_text)
            if owned_existed:
                owned.write_bytes(owned_bytes)
            else:
                owned.unlink(missing_ok=True)
            for backup in new_backup_paths:
                backup.unlink(missing_ok=True)
            _rollback_media_resend_plan(media_plan, media_backup_created)
            raise
        return True
    _validate_pre_canary(base, "base")
    for telegram in telegrams:
        _validate_pre_canary(telegram, "telegram")
    _validate_pre_canary(stream, "stream")
    originals = {p: p.read_text() for p in (base, *telegrams, stream)}
    b = _with_core_hooks(originals[base])
    b = _with_bypass_hooks(b)
    b = _with_media_delivery_dedup(media_plan[0], b)
    s = _with_stream_hooks(originals[stream])
    telegram_outputs = {telegram: _with_slash_confirm_hook(originals[telegram]) for telegram in telegrams}
    compile(b, str(base), "exec")
    for telegram, output in telegram_outputs.items():
        compile(output, str(telegram), "exec")
    compile(s, str(stream), "exec")
    items = (
        (base, b, "base"),
        *((telegram, telegram_outputs[telegram], "telegram") for telegram in telegrams),
        (stream, s, "stream"),
    )
    rebound_backups: dict[Path, bytes] = {}
    for path, _, kind in items:
        backup = Path(str(path) + BACKUP_SUFFIX)
        if backup.exists():
            _validate_pre_canary(backup, kind, backup=True)
            if backup.read_bytes() != path.read_bytes():
                if kind != "base" or not _matches_ordered_base_predecessors(backup.read_text(), path.read_text()):
                    raise RuntimeError(f"rollback backup does not match source: {path}")
                rebound_backups[backup] = backup.read_bytes()
        else:
            _validate_pre_canary(path, kind)
    media_backup_created = False
    try:
        for path, _, _ in items:
            backup = Path(str(path) + BACKUP_SUFFIX)
            if backup in rebound_backups:
                backup.write_bytes(path.read_bytes())
        for p, _, _ in items:
            backup = Path(str(p) + BACKUP_SUFFIX)
            if not backup.exists():
                shutil.copy2(p, backup)
        for p, patched, _ in items:
            p.write_text(patched)
        owned.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PAYLOAD, owned)
        media_backup_created = _apply_media_resend_plan(media_plan)
    except Exception:
        for p, t in originals.items():
            p.write_text(t)
        owned.unlink(missing_ok=True)
        for backup, original_bytes in rebound_backups.items():
            backup.write_bytes(original_bytes)
        _rollback_media_resend_plan(media_plan, media_backup_created)
        raise
    return True
