#!/usr/bin/env python3
# ruff: noqa: E501
"""Make voice replies a single-delivery surface with text fallback.

Idempotent via marker: HERMES_VOICE_MEMO_SINGLE_DELIVERY_v1

The patch owns three related seams:

* auto-TTS voice-input turns do not stream their final body before synthesis;
* a successful voice send suppresses the ordinary body while a failed send
  falls through to text;
* the TTS tool schema tells the model not to repeat spoken prose beside the
  generated MEDIA directive.
"""

from __future__ import annotations

import argparse
import ast
import sys
import textwrap
from pathlib import Path

MARKER = "HERMES_VOICE_MEMO_SINGLE_DELIVERY_v1"

LONG_TTS_TEST_OLD = '''        assert adapter.sent == [
            {
                "chat_id": "-1001",
                "content": long_reply,
                "reply_to": None,
                "metadata": {"thread_id": "17585", "notify": True},
            }
        ]
'''
LONG_TTS_TEST_NEW = '''        # HERMES_VOICE_MEMO_SINGLE_DELIVERY_v1 — a confirmed voice send is
        # the one client-visible delivery even when the source prose is long.
        assert adapter.sent == []
'''


def _write_if_changed(path: Path, content: str) -> bool:
    original = path.read_text(encoding="utf-8")
    if original == content:
        return False
    backup = path.with_suffix(path.suffix + ".bak-voice-memo-single-delivery-v1")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(content, encoding="utf-8")
    return True


def patch_base_source(content: str) -> str | None:
    """Patch the adapter auto-TTS delivery boundary."""
    if MARKER in content:
        return content

    comment = "# Play TTS audio before text (voice-first experience)"
    comment_index = content.find(comment)
    if comment_index < 0:
        return None
    start = content.rfind("\n", 0, comment_index) + 1
    delivery_index = content.find(
        "delivery_adapter = self._final_delivery_adapter(event.source)", start
    )
    end = content.rfind("\n", start, delivery_index) + 1
    if start < 0 or delivery_index < 0 or end <= start:
        return None

    comment_line = content[start : content.find("\n", start) + 1]
    indent = comment_line[: len(comment_line) - len(comment_line.lstrip())]
    replacement = textwrap.indent(
        textwrap.dedent(
            '''\
            # HERMES_VOICE_MEMO_SINGLE_DELIVERY_v1
            # A voice reply is one delivery surface, not audio plus a
            # caption plus a second body message.  Text remains the
            # fail-closed fallback until the platform confirms the voice
            # send succeeded.
            _tts_voice_delivered = False
            if _tts_path and Path(_tts_path).exists():
                try:
                    tts_result = await self.play_tts(
                        chat_id=event.source.chat_id,
                        audio_path=_tts_path,
                        caption=None,
                        metadata=_final_thread_metadata,
                    )
                    _record_delivery(tts_result)
                    _tts_voice_delivered = bool(
                        getattr(tts_result, "success", False)
                    )
                except Exception as tts_send_err:
                    logger.warning(
                        "[%s] Auto-TTS voice delivery failed; falling back to text: %s",
                        self.name,
                        tts_send_err,
                    )
                finally:
                    try:
                        os.remove(_tts_path)
                    except OSError:
                        pass

            # Send the body only when synthesis or voice delivery did not
            # complete.  This preserves a usable reply on every TTS error.
            if text_content and not _tts_voice_delivered:
            '''
        ),
        indent,
    )
    patched = content[:start] + replacement + content[end:]
    postcondition_old = "delivery_attempted or _tts_caption_delivered"
    postcondition_new = "delivery_attempted or _tts_voice_delivered"
    if patched.count(postcondition_old) != 1:
        return None
    patched = patched.replace(postcondition_old, postcondition_new, 1)
    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    return patched


def patch_run_source(content: str) -> str | None:
    """Keep auto-TTS voice turns off the irreversible text-streaming path.

    Golden's durable runtime carrier extracts the in-process stream setup into
    ``TurnRunner.run_sync`` and already propagates ``message_type`` through the
    profile wrapper and queued-follow-up path.  This patch deliberately binds
    to that composed seam: it avoids another per-turn parameter and keeps
    concurrent turns isolated through the existing open ``TurnContext``.
    """
    if MARKER in content:
        return content

    patched = content

    # The remote-proxy path owns its stream consumer directly.
    proxy_signature_anchor = '''        event_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forward the message to a remote Hermes API server instead of
'''
    proxy_signature_replacement = '''        event_message_id: Optional[str] = None,
        suppress_streaming: bool = False,
    ) -> Dict[str, Any]:
        """Forward the message to a remote Hermes API server instead of
'''
    if proxy_signature_anchor not in patched:
        return None
    patched = patched.replace(proxy_signature_anchor, proxy_signature_replacement, 1)

    proxy_streaming_anchor = '''        _streaming_enabled = (
            _scfg.enabled and _scfg.transport != "off"
            if _plat_streaming is None
            else bool(_plat_streaming)
        )
'''
    proxy_streaming_replacement = proxy_streaming_anchor + '''        if suppress_streaming:
            _streaming_enabled = False
'''
    proxy_start = patched.find("    async def _run_agent_via_proxy(\n")
    proxy_end = patched.find("\n    async def _run_agent(\n", proxy_start)
    if proxy_start < 0 or proxy_end < 0:
        return None
    proxy_method = patched[proxy_start:proxy_end]
    if proxy_method.count(proxy_streaming_anchor) != 1:
        return None
    proxy_method = proxy_method.replace(
        proxy_streaming_anchor, proxy_streaming_replacement, 1
    )
    patched = patched[:proxy_start] + proxy_method + patched[proxy_end:]

    # The local/profile path uses the message type already carried by Golden's
    # composed runtime and publishes a concurrency-safe bit on TurnContext.
    inner_start = patched.find("    async def _run_agent_inner(\n")
    inner_queue = patched.find("        import queue\n", inner_start)
    if inner_start < 0 or inner_queue < 0:
        return None
    inner_prefix = patched[inner_start:inner_queue]
    proxy_call_anchor = "                event_message_id=event_message_id,\n"
    if inner_prefix.count(proxy_call_anchor) != 1:
        return None
    inner_prefix = inner_prefix.replace(
        proxy_call_anchor,
        proxy_call_anchor
        + "                suppress_streaming=self._voice_input_uses_single_delivery(\n"
        + "                    source, message_type\n"
        + "                ),\n",
        1,
    )
    patched = patched[:inner_start] + inner_prefix + patched[inner_queue:]

    turn_context_anchor = '''            persist_user_timestamp=persist_user_timestamp,
        )
        # Periodic Telegram summaries prefer real model commentary from this
'''
    turn_context_replacement = '''            persist_user_timestamp=persist_user_timestamp,
        )
        turn_ctx.suppress_streaming = self._voice_input_uses_single_delivery(
            source, message_type
        )
        # Periodic Telegram summaries prefer real model commentary from this
'''
    if turn_context_anchor not in patched:
        return None
    patched = patched.replace(turn_context_anchor, turn_context_replacement, 1)

    turn_streaming_anchor = '''        _streaming_enabled = (
            _scfg.enabled and _scfg.transport != "off"
            if _plat_streaming is None
            else bool(_plat_streaming)
        )
        _want_stream_deltas = _streaming_enabled
        _want_interim_messages = ctx.interim_assistant_messages_enabled
'''
    turn_streaming_replacement = '''        _streaming_enabled = (
            _scfg.enabled and _scfg.transport != "off"
            if _plat_streaming is None
            else bool(_plat_streaming)
        )
        if getattr(ctx, "suppress_streaming", False):
            _streaming_enabled = False
        _want_stream_deltas = _streaming_enabled
        _want_interim_messages = (
            ctx.interim_assistant_messages_enabled
            and not getattr(ctx, "suppress_streaming", False)
        )
'''
    if turn_streaming_anchor not in patched:
        return None
    patched = patched.replace(turn_streaming_anchor, turn_streaming_replacement, 1)

    # The adapter owns the effective decision: global voice.auto_tts plus the
    # per-chat /voice enabled/disabled sets.  This preserves profile identity
    # and means voice-only behavior follows the same state as synthesis.
    helper_anchor = "    def _should_send_voice_reply(\n"
    helper = '''    def _voice_input_uses_single_delivery(
        self,
        source: SessionSource,
        message_type: Any,
    ) -> bool:
        """Return whether this inbound voice turn should resolve to voice only.

        HERMES_VOICE_MEMO_SINGLE_DELIVERY_v1: the adapter owns the effective
        auto-TTS decision because it combines the global ``voice.auto_tts``
        default with per-chat ``/voice`` overrides.  Suppressing streaming for
        exactly those voice-input turns lets the adapter synthesize first and
        retain ordinary text as the fallback if synthesis or delivery fails.
        """
        normalized_type = getattr(message_type, "value", message_type)
        if normalized_type != MessageType.VOICE.value:
            return False
        adapter = self._adapter_for_source(source)
        decision = getattr(adapter, "_should_auto_tts_for_chat", None)
        if not callable(decision):
            return False
        try:
            return bool(decision(source.chat_id))
        except Exception:
            logger.debug("Voice single-delivery decision failed", exc_info=True)
            return False

'''
    if helper_anchor not in patched:
        return None
    patched = patched.replace(helper_anchor, helper + helper_anchor, 1)

    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    return patched


def patch_tts_source(content: str) -> str | None:
    """Add the no-duplicate voice-memo contract to the model tool schema."""
    if MARKER in content:
        return content
    anchor = (
        '    "description": "Convert text to speech audio. Returns a MEDIA: path that the platform delivers as native audio. '
        'Compatible providers render as a voice bubble on Telegram; otherwise audio is sent as a regular attachment. '
        'In CLI mode, saves to ~/voice-memos/. Voice and provider are user-configured (built-in providers like edge/openai '
        'or custom command providers under tts.providers.<name>), not model-selected.",\n'
    )
    if anchor not in content:
        return None
    replacement = (
        '    # HERMES_VOICE_MEMO_SINGLE_DELIVERY_v1\n'
        '    "description": "Convert text to speech audio. Returns a MEDIA: path that the platform delivers as native audio. '
        'Compatible providers render as a voice bubble on Telegram; otherwise audio is sent as a regular attachment. '
        'For a messaging-platform voice memo, use the returned MEDIA directive as the delivery and do not repeat the spoken '
        'prose in the assistant reply; include at most one short context line when necessary. In CLI mode, saves to '
        '~/voice-memos/. Voice and provider are user-configured (built-in providers like edge/openai or custom command providers '
        'under tts.providers.<name>), not model-selected.",\n'
    )
    patched = content.replace(anchor, replacement, 1)
    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    return patched


def patch_base_topic_test_source(content: str) -> str | None:
    """Align native caption expectations with Golden's voice-only contract."""
    if MARKER in content:
        return content
    if content.count(LONG_TTS_TEST_OLD) != 1:
        return None
    patched = content.replace(LONG_TTS_TEST_OLD, LONG_TTS_TEST_NEW, 1)
    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    return patched


def _patch_file(path: Path, patcher, label: str) -> bool:
    if not path.exists():
        print(f"[voice_memo_single_delivery_v1] {label} not found: {path}")
        raise FileNotFoundError(path)
    original = path.read_text(encoding="utf-8")
    patched = patcher(original)
    if patched is None:
        raise RuntimeError(f"{label} anchor missing or produced invalid Python")
    changed = _write_if_changed(path, patched)
    print(
        f"[voice_memo_single_delivery_v1] {'PATCHED' if changed else 'already patched'} {path}"
    )
    return changed


def patch_voice_memo_single_delivery_v1(hermes_dir: Path) -> bool:
    changed = False
    changed |= _patch_file(
        hermes_dir / "gateway" / "platforms" / "base.py",
        patch_base_source,
        "base adapter",
    )
    changed |= _patch_file(
        hermes_dir / "gateway" / "run.py",
        patch_run_source,
        "gateway runner",
    )
    changed |= _patch_file(
        hermes_dir / "tools" / "tts_tool.py",
        patch_tts_source,
        "TTS tool",
    )
    changed |= _patch_file(
        hermes_dir / "tests" / "gateway" / "test_base_topic_sessions.py",
        patch_base_topic_test_source,
        "base topic tests",
    )
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        patch_voice_memo_single_delivery_v1(args.hermes_dir)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[voice_memo_single_delivery_v1] ABORT: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
