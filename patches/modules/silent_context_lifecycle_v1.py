#!/usr/bin/env python3
"""Keep automatic control-plane mechanics off human chat transcripts.

Local, API, and webhook diagnostics remain intact. Manual ``/compress``
feedback also remains visible. Automatic compaction/reset, restart admission,
delivery recovery, and expired approval output is suppressed from human chat
surfaces without changing the underlying durability or approval state.
"""
from __future__ import annotations

import argparse
import ast
import shutil
import time
from pathlib import Path

COMPLETION_MARKER = "HERMES_SUPPRESS_COMPACTION_COMPLETION_STATUS_v1"
CHAT_MARKER = "HERMES_SILENT_AUTO_CONTEXT_LIFECYCLE_v1"
CONTINUITY_MARKER = "HERMES_CLIENT_CONVERSATION_CONTINUITY_v1"

HELPER_OLD = '''def _emit_compaction_done(agent: Any) -> None:
    """Emit the structured terminal edge for a started compaction."""
    status_callback = getattr(agent, "status_callback", None)
    if not status_callback:
        return
    try:
        status_callback("compacted", COMPACTION_DONE_STATUS)
    except Exception:
        logger.debug("status_callback error in compaction completion", exc_info=True)
'''

HELPER_NEW = '''def _emit_compaction_done(agent: Any) -> None:
    """Keep compaction completion internal; never create a transcript event."""
    # HERMES_SUPPRESS_COMPACTION_COMPLETION_STATUS_v1
    return
'''

GATEWAY_NOISY_ANCHOR = '    r"|session\\s+compressed\\s+\\d+\\s+times"\n'
GATEWAY_NOISY_REPLACEMENT = GATEWAY_NOISY_ANCHOR + '''    # HERMES_SILENT_AUTO_CONTEXT_LIFECYCLE_v1
    # Automatic blocked-overflow diagnostics stay available on raw local/API/
    # webhook surfaces; human chat surfaces recover silently through the
    # configured context lifecycle instead of delegating /new or /compress.
    r"|context\\s+is\\s+over\\s+the\\s+compression\\s+threshold.*"
    r"compression\\s+is\\s+currently\\s+blocked"
'''

TEST_NOISY_LIST_END_ANCHOR = '''    (
        "⚠ Skipping concurrent compression — another path is already "
        "compressing this session. Will retry after it finishes."
    ),
]

# Messages that must NEVER be swallowed by the compression-noise filter:
'''
TEST_NOISY_LIST_END_REPLACEMENT = '''    (
        "⚠ Skipping concurrent compression — another path is already "
        "compressing this session. Will retry after it finishes."
    ),
    # HERMES_SILENT_AUTO_CONTEXT_LIFECYCLE_v1 — automatic blocked-overflow
    # diagnostics remain raw locally but are lifecycle noise on human chat.
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="cooldown:30"
    ),
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="ineffective"
    ),
]

# Messages that must NEVER be swallowed by the compression-noise filter:
'''
TEST_VISIBLE_BLOCK = '''    # Blocked-overflow warning (#62625/#62708): the context is over the
    # compression threshold but compression is blocked (summary-LLM cooldown
    # or the anti-thrash breaker). FAILURE-CLASS — must reach chat users so
    # they can /new or /compress before the session dies at the hard token
    # limit. Formatted from the SAME template the emit site uses, so a
    # rewording that drifts into the noise regex fails here.
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="cooldown:30"
    ),
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="ineffective"
    ),
'''

DELIVERY_MARKER_OLD = '''# Visible prefix for redeliveries that might duplicate an already-received
# message (crash mid-send / post-rejection retry). Honest at-least-once.
RECOVERED_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\\n\\n"
)
'''
DELIVERY_MARKER_NEW = '''# HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
# Ambiguity remains recorded by ``needs_marker`` and the delivery receipt, but
# control-plane recovery vocabulary never enters the client conversation.
RECOVERED_MARKER = ""
'''

DELIVERY_TEST_OLD = '''        assert sent["content"].startswith(dl.RECOVERED_MARKER)
        assert sent["content"].endswith("the final answer")
'''
DELIVERY_TEST_NEW = '''        # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
        assert sent["content"] == "the final answer"
'''

DRAIN_NOTICE_REPLACEMENTS = {
    '''    _DRAIN_UNCERTAIN_NOTICE = (
        "⚠️ Hermes could not confirm completion of your saved instruction. "
        "It did not retry it. Please resend the instruction if you want it run again."
    )
    _DRAIN_COMPLETED_NOTICE = (
        "✅ Hermes already completed this saved instruction. "
        "It will not run it again."
    )
''': '''    # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1 — receipt state remains
    # internal; startup admission and replay never narrate it to the client.
    _DRAIN_UNCERTAIN_NOTICE = ""
    _DRAIN_COMPLETED_NOTICE = ""
''',
    '''                response=(
                    "⏳ Hermes is already processing this saved instruction. "
                    "It will not start it again."
                ),
''': '''                response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''                response=(
                    "⏳ Gateway startup recovery is still finishing. "
                    "Your message was saved and will run next."
                ),
''': '''                response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''            response=(
                "⚠️ Gateway startup recovery is still finishing and could not "
                "safely claim this message. Please resend it after startup completes."
            ),
''': '''            response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''                response=(
                    "⚠️ Hermes handled this message during startup but could not "
                    "durably confirm completion. It will not retry automatically."
                ),
''': '''                response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''            response=(
                "⚠️ Hermes handled this message during startup but could not "
                "durably confirm completion. It will not retry automatically."
            ),
''': '''            response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''            response=(
                "⚠️ Gateway startup recovery is still finishing and could not "
                "durably record this message's pre-dispatch result. Please resend "
                "it after startup completes."
            ),
''': '''            response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''                response=(
                    "⚠️ Gateway startup recovery is still finishing and control "
                    "commands cannot be queued. Please resend this command after "
                    "startup completes."
                ),
''': '''                response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''            response=(
                "⚠️ Gateway startup recovery is still finishing and could not "
                "safely reject this control command. Please resend it after "
                "startup completes."
            ),
''': '''            response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
    '''            response=(
                "⚠️ Gateway startup recovery is still finishing and could not "
                "safely save this message. Please resend it after startup completes."
            ),
''': '''            response="",  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
''',
}

STARTUP_RESPONSE_GUARD = '''        if decision is not None and decision.response:
'''

STARTUP_CLAIM_TEST_OLD = (
    '    assert any("could not safely claim" in message for message in adapter.sent)'
    "  # ty:ignore[unresolved-attribute]\n"
)
STARTUP_CLAIM_TEST_NEW = '''    # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
    assert adapter.sent == []  # ty:ignore[unresolved-attribute]
'''

DRAIN_CONSUMER_TEST_OLD = '''    if state == "claimed":
        assert adapter.sent == [runner._DRAIN_UNCERTAIN_NOTICE]  # ty:ignore[unresolved-attribute]
        assert committed_message_ids == {event.message_id}
        adapter.sent.clear()  # ty:ignore[unresolved-attribute]
        assert await runner._handle_startup_gate_message(
            event,
            session_key,
            runner._handle_message,
            False,
        )
        assert adapter.sent == []  # ty:ignore[unresolved-attribute]
    elif state == "completed":
        assert adapter.sent == [runner._DRAIN_COMPLETED_NOTICE]  # ty:ignore[unresolved-attribute]
    else:
        assert adapter.sent == []  # ty:ignore[unresolved-attribute]
'''
DRAIN_CONSUMER_TEST_NEW = '''    # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1: exercise the real
    # _StartupGateDecision consumer. Empty recovery responses are consumed;
    # no adapter call is made, while receipt cleanup still completes.
    assert adapter.sent == []  # ty:ignore[unresolved-attribute]
    if state == "claimed":
        assert committed_message_ids == {event.message_id}
        assert await runner._handle_startup_gate_message(
            event,
            session_key,
            runner._handle_message,
            False,
        )
        assert adapter.sent == []  # ty:ignore[unresolved-attribute]
'''

SLASH_SENDER_TEST_OLD = '''    @pytest.mark.asyncio
    async def test_approve_bypasses_guard(self):
        """/approve must bypass (deadlock prevention)."""
        adapter = _make_adapter()
        sk = _session_key()
        adapter._active_sessions[sk] = asyncio.Event()

        await adapter.handle_message(_make_event("/approve"))

        assert sk not in adapter._pending_messages
        assert any("handled:approve" in r for r in adapter.sent_responses)
'''
SLASH_SENDER_TEST_NEW = SLASH_SENDER_TEST_OLD + '''
    @pytest.mark.asyncio
    async def test_expired_approve_none_response_is_consumed_without_send(self):
        """The real active-session slash sender must not emit a blank message."""
        adapter = _make_adapter()
        sk = _session_key()
        adapter._active_sessions[sk] = asyncio.Event()

        async def expired_approval(_event):
            return None

        adapter._message_handler = expired_approval
        await adapter.handle_message(_make_event("/approve"))

        # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
        assert sk not in adapter._pending_messages
        assert adapter.sent_responses == []
'''

TELEGRAM_APPROVAL_OLD = '''                if count:
                    # Map choice to human-readable label
                    label_map = {
                        "once": "✅ Approved once",
                        "session": "✅ Approved for session",
                        "always": "✅ Approved permanently",
                        "deny": "❌ Denied",
                    }
                    label = label_map.get(choice, "Resolved")
                    edit_text = f"{label} by {user_display}"
                else:
                    label = "⌛ Approval expired"
                    edit_text = (
                        f"{label} — no command was waiting. "
                        f"It already timed out (and was denied) or was resolved elsewhere."
                    )

                await query.answer(text=label)

                # Edit message to show decision, remove buttons
                try:
                    await query.edit_message_text(
                        text=self.format_message(edit_text),
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=None,
                    )
                except Exception:
                    pass  # non-fatal if edit fails

                # Resume the typing indicator — paused when the approval was
                # sent (gateway/run.py).  The text /approve and /deny paths
                # call resume_typing_for_chat here too; without it, typing
                # stays paused for the rest of the turn after an inline
                # button click.
                if count and query_chat_id is not None:
                    self.resume_typing_for_chat(str(query_chat_id))
'''
TELEGRAM_APPROVAL_NEW = '''                if not count:
                    # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1 — consume the
                    # stale callback and remove controls without replacing the
                    # client-visible card with control-plane expiry text.
                    await query.answer()
                    try:
                        await query.edit_message_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                    return

                label_map = {
                    "once": "✅ Approved once",
                    "session": "✅ Approved for session",
                    "always": "✅ Approved permanently",
                    "deny": "❌ Denied",
                }
                label = label_map.get(choice, "Resolved")
                edit_text = f"{label} by {user_display}"
                await query.answer(text=label)
                try:
                    await query.edit_message_text(
                        text=self.format_message(edit_text),
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=None,
                    )
                except Exception:
                    pass
                if query_chat_id is not None:
                    self.resume_typing_for_chat(str(query_chat_id))
'''
TELEGRAM_ALREADY_RESOLVED_OLD = '''                if not session_key:
                    await query.answer(text="This approval has already been resolved.")
                    return
'''
TELEGRAM_ALREADY_RESOLVED_NEW = '''                if not session_key:
                    # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
                    await query.answer()
                    try:
                        await query.edit_message_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                    return
'''

TELEGRAM_CALLBACK_TEST_ANCHOR = '''    @pytest.mark.asyncio
    async def test_approval_callback_escapes_dynamic_user_name(self):
'''
TELEGRAM_CALLBACK_TEST_INSERT = '''    @pytest.mark.asyncio
    async def test_expired_approval_callback_acknowledges_without_notice(self):
        adapter = _make_adapter()
        adapter._approval_state[7] = "agent:main:telegram:group:12345:99"
        query = AsyncMock()
        query.data = "ea:once:7"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock(id="12345", first_name="Operator")
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update = MagicMock(callback_query=query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=0):
                await adapter._handle_callback_query(update, MagicMock())

        # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
        query.answer.assert_awaited_once_with()
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        query.edit_message_text.assert_not_awaited()

'''

WHATSAPP_APPROVAL_OLD = '''            # Send confirmation message — paralleling Telegram's UX.  A tap
            # that lands after the wait timed out (count == 0) must not claim
            # the command was approved: it was already denied fail-closed.
            try:
                if count:
                    confirm_text = (
                        "✅ Approved." if choice == "approve" else "❌ Denied."
                    )
                else:
                    confirm_text = (
                        "⌛ Approval expired — command was not run "
                        "(already timed out or resolved elsewhere)."
                    )
                await self.send(str(raw_message.get("from") or ""), confirm_text)
            except Exception:
                logger.exception("[whatsapp_cloud] approval confirm failed")
'''
WHATSAPP_APPROVAL_NEW = '''            # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1 — a stale approval is
            # already denied fail-closed; keep that operational fact internal.
            if count:
                try:
                    confirm_text = (
                        "✅ Approved." if choice == "approve" else "❌ Denied."
                    )
                    await self.send(str(raw_message.get("from") or ""), confirm_text)
                except Exception:
                    logger.exception("[whatsapp_cloud] approval confirm failed")
'''

RELAY_APPROVAL_OLD = '''                if not count:
                    label = "⌛ Approval expired — no command was waiting."
                # Acknowledge in-channel (the connector's prompt message can't
                # be edited cross-platform yet — edit support varies; a short
                # confirmation preserves the audit trail the native edit gives).
                await self.send(
                    chat_id, label, metadata=self._prompt_reply_metadata(event)
                )
                if count:
                    self.resume_typing_for_chat(chat_id)
'''
RELAY_APPROVAL_NEW = '''                # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
                if count:
                    await self.send(
                        chat_id, label, metadata=self._prompt_reply_metadata(event)
                    )
                    self.resume_typing_for_chat(chat_id)
'''

SLACK_EXPIRED_OLD = '''        decision_text = label_map.get(choice, f"Resolved by {user_name}")
        if not count:
            decision_text = (
                "⌛ Approval expired — command was not run "
                "(already timed out or resolved elsewhere)"
            )
'''
SLACK_EXPIRED_NEW = '''        decision_text = label_map.get(choice, f"Resolved by {user_name}")
        expired_silently = not count  # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
'''
SLACK_BLOCKS_OLD = '''        updated_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": original_text or "Command approval request",
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": decision_text},
                ],
            },
        ]

        try:
            await self._get_client(channel_id, team_id=team_id or None).chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=decision_text,
                blocks=sanitize_blocks(updated_blocks),
            )
'''
SLACK_BLOCKS_NEW = '''        updated_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": original_text or "Command approval request",
                },
            },
        ]
        if not expired_silently:
            updated_blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": decision_text}],
                }
            )

        try:
            await self._get_client(channel_id, team_id=team_id or None).chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=(original_text if expired_silently else decision_text),
                blocks=sanitize_blocks(updated_blocks),
            )
'''

SLACK_CALLBACK_TEST_ANCHOR = '''    @pytest.mark.asyncio
    async def test_global_allowlist_blocks_unauthorized_click(self, monkeypatch):
'''
SLACK_CALLBACK_TEST_INSERT = '''    @pytest.mark.asyncio
    async def test_expired_approval_acknowledges_without_expiry_context(self):
        adapter = _make_adapter()
        _attach_auth_runner(adapter)
        adapter._approval_resolved["1.3"] = False
        ack = AsyncMock()
        body = {
            "message": {"ts": "1.3", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "request"}},
            ]},
            "channel": {"id": "C1"},
            "user": {"name": "alice", "id": "U_ALICE"},
        }
        action = {"action_id": "hermes_approve_once", "value": "session-key"}
        client = adapter._team_clients["T1"]
        client.chat_update = AsyncMock()

        with patch("tools.approval.resolve_gateway_approval", return_value=0):
            await adapter._handle_approval_action(ack, body, action)

        # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
        ack.assert_awaited_once_with()
        update = client.chat_update.await_args.kwargs
        assert update["text"] == "request"
        assert all(block["type"] != "context" for block in update["blocks"])

'''

DISCORD_EXPIRED_OLD = '''            if not count:
                color = discord.Color.dark_grey()
                label = "⌛ Approval expired — command was not run (already timed out or resolved elsewhere)"

            # Update the embed with the decision
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                footer = f"{label} by {interaction.user.display_name}" if count else label
                embed.set_footer(text=footer)
'''
DISCORD_EXPIRED_NEW = '''            # Update the embed with the decision. A stale callback removes
            # controls without adding expiry text to the client transcript.
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                if count:
                    embed.color = color
                    embed.set_footer(
                        text=f"{label} by {interaction.user.display_name}"
                    )
                else:
                    # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
                    embed.remove_footer()
'''
DISCORD_ALREADY_RESOLVED_OLD = '''            if self.resolved:
                await interaction.response.send_message(
                    "This approval has already been resolved~", ephemeral=True
                )
                return
'''
DISCORD_ALREADY_RESOLVED_NEW = '''            if self.resolved:
                # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
                await interaction.response.defer()
                return
'''
DISCORD_APPROVAL_TIMEOUT_OLD = '''        async def on_timeout(self):
            """Handle view timeout -- disable buttons and mark as expired."""
            self.resolved = True
            for child in self.children:
                child.disabled = True
            # Visually update the Discord message so buttons appear disabled.
            msg = getattr(self, '_message', None)
            if msg:
                try:
                    embed = msg.embeds[0] if msg.embeds else None
                    if embed:
                        embed.color = discord.Color.greyple()
                        embed.set_footer(text="⏱ Prompt expired — no action taken")
                    await msg.edit(embed=embed, view=self)
                except Exception:
                    pass  # message deleted or too old to edit

    class SlashConfirmView(discord.ui.View):
'''
DISCORD_APPROVAL_TIMEOUT_NEW = '''        async def on_timeout(self):
            """Disable stale approval controls without a client-facing notice."""
            self.resolved = True
            for child in self.children:
                child.disabled = True
            msg = getattr(self, '_message', None)
            if msg:
                try:
                    embed = msg.embeds[0] if msg.embeds else None
                    if embed:
                        # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
                        embed.remove_footer()
                    await msg.edit(embed=embed, view=self)
                except Exception:
                    pass

    class SlashConfirmView(discord.ui.View):
'''

SLASH_APPROVAL_OLD = '''            if session_key in self._pending_approvals:
                self._pending_approvals.pop(session_key)
                return t("gateway.approval_expired")
'''
SLASH_APPROVAL_NEW = '''            if session_key in self._pending_approvals:
                self._pending_approvals.pop(session_key)
                # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
                return None
'''

FEISHU_EXPIRED_OLD = '''            if not count and choice != "deny":
                # The card was already updated synchronously to "Approved" by
                # the callback response, but nothing was waiting — the wait
                # already timed out (fail-closed deny) or was resolved via
                # /approve. Correct the record so the user doesn't believe
                # the command ran.
                _chat = str(state.get("chat_id", "") or chat_id or "")
                if _chat:
                    try:
                        await self.send(
                            _chat,
                            "⌛ That approval had already expired — the command "
                            "was not run (it timed out or was resolved elsewhere).",
                        )
                    except Exception:
                        logger.debug("[Feishu] expired-approval notice failed", exc_info=True)
'''
FEISHU_EXPIRED_NEW = '''            if not count:
                # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
                logger.info(
                    "Feishu approval callback was stale; client notice suppressed"
                )
'''

MATRIX_EXPIRED_OLD = '''        await self._send_invalid_reaction_feedback(
            room_id,
            target_event_id,
            "This approval prompt has expired. Run the command again if you still want to approve it.",
        )
'''
MATRIX_EXPIRED_NEW = '''        # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1
        logger.info("Matrix approval prompt expired; client notice suppressed")
'''

TEAMS_EXPIRED_OLD = '''        if not has_blocking_approval(session_key):
            return InvokeResponse(
                status=200,
                body=AdaptiveCardActionCardResponse(
                    value=AdaptiveCard()
                    .with_version("1.4")
                    .with_body([TextBlock(text="⚠️ Approval already resolved or expired.", wrap=True)])
                ),
            )
'''
TEAMS_EXPIRED_NEW = '''        if not has_blocking_approval(session_key):
            # HERMES_CLIENT_CONVERSATION_CONTINUITY_v1: acknowledge the card
            # action without replacing the client-visible card with expiry text.
            return InvokeResponse(
                status=200,
                body=AdaptiveCardActionMessageResponse(value=None),
            )
'''


def _target(hermes_dir: Path, *parts: str) -> Path:
    direct = hermes_dir.joinpath(*parts)
    if direct.is_file():
        return direct
    nested = hermes_dir.joinpath("hermes-agent", *parts)
    if nested.is_file():
        return nested
    raise FileNotFoundError(direct)


def patch_completion_text(source: str) -> str:
    if COMPLETION_MARKER in source:
        return source
    if source.count(HELPER_OLD) != 1:
        raise RuntimeError("compaction completion helper anchor missing or ambiguous")
    patched = source.replace(HELPER_OLD, HELPER_NEW, 1)
    if 'status_callback("compacted", COMPACTION_DONE_STATUS)' in patched:
        raise RuntimeError("compaction completion status remained after patch")
    return patched


def patch_gateway_text(source: str) -> str:
    if CHAT_MARKER in source:
        return source
    if source.count(GATEWAY_NOISY_ANCHOR) != 1:
        raise RuntimeError("gateway context-lifecycle noise anchor missing or ambiguous")
    return source.replace(
        GATEWAY_NOISY_ANCHOR,
        GATEWAY_NOISY_REPLACEMENT,
        1,
    )


def patch_gateway_test_text(source: str) -> str:
    if CHAT_MARKER in source:
        return source
    if source.count(TEST_NOISY_LIST_END_ANCHOR) != 1:
        raise RuntimeError("gateway noise-test list anchor missing or ambiguous")
    if source.count(TEST_VISIBLE_BLOCK) != 1:
        raise RuntimeError("gateway visible-warning test anchor missing or ambiguous")
    source = source.replace(
        TEST_NOISY_LIST_END_ANCHOR,
        TEST_NOISY_LIST_END_REPLACEMENT,
        1,
    )
    return source.replace(TEST_VISIBLE_BLOCK, "", 1)


def _replace_exact(
    source: str,
    old: str,
    new: str,
    *,
    label: str,
    count: int = 1,
) -> str:
    found = source.count(old)
    if found != count:
        raise RuntimeError(
            f"{label} anchor missing or ambiguous: expected {count}, found {found}"
        )
    return source.replace(old, new)


def patch_delivery_ledger_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        DELIVERY_MARKER_OLD,
        DELIVERY_MARKER_NEW,
        label="delivery recovery marker",
    )


def patch_delivery_ledger_test_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        DELIVERY_TEST_OLD,
        DELIVERY_TEST_NEW,
        label="delivery recovery behavior test",
    )


def patch_continuity_gateway_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = source
    for old, new in DRAIN_NOTICE_REPLACEMENTS.items():
        found = patched.count(old)
        if found < 1:
            raise RuntimeError("startup recovery notice anchor missing")
        patched = patched.replace(old, new)
    forbidden = (
        "Gateway startup recovery is still finishing",
        "Hermes is already processing this saved instruction",
        "Hermes handled this message during startup but could not",
        "Hermes could not confirm completion of your saved instruction",
        "Hermes already completed this saved instruction",
    )
    remaining = [text for text in forbidden if text in patched]
    if remaining:
        raise RuntimeError(
            "client-visible startup recovery text remained: " + ", ".join(remaining)
        )
    if patched.count(STARTUP_RESPONSE_GUARD) != 1:
        raise RuntimeError(
            "startup response truthiness guard missing or ambiguous"
        )
    return patched


def patch_restart_test_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        STARTUP_CLAIM_TEST_OLD,
        STARTUP_CLAIM_TEST_NEW,
        label="startup claim silence test",
    )
    ast.parse(patched)
    return patched


def patch_drain_consumer_test_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        DRAIN_CONSUMER_TEST_OLD,
        DRAIN_CONSUMER_TEST_NEW,
        label="startup decision consumer test",
    )
    ast.parse(patched)
    return patched


def patch_slash_sender_test_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        SLASH_SENDER_TEST_OLD,
        SLASH_SENDER_TEST_NEW,
        label="slash sender no-response test",
    )
    ast.parse(patched)
    return patched


def patch_telegram_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        TELEGRAM_ALREADY_RESOLVED_OLD,
        TELEGRAM_ALREADY_RESOLVED_NEW,
        label="Telegram resolved approval",
    )
    return _replace_exact(
        patched,
        TELEGRAM_APPROVAL_OLD,
        TELEGRAM_APPROVAL_NEW,
        label="Telegram expired approval",
    )


def patch_telegram_approval_test_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        TELEGRAM_CALLBACK_TEST_ANCHOR,
        TELEGRAM_CALLBACK_TEST_INSERT + TELEGRAM_CALLBACK_TEST_ANCHOR,
        label="Telegram callback acknowledgement test",
    )
    ast.parse(patched)
    return patched


def patch_whatsapp_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        WHATSAPP_APPROVAL_OLD,
        WHATSAPP_APPROVAL_NEW,
        label="WhatsApp expired approval",
    )


def patch_relay_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        RELAY_APPROVAL_OLD,
        RELAY_APPROVAL_NEW,
        label="relay expired approval",
    )


def patch_slack_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        SLACK_EXPIRED_OLD,
        SLACK_EXPIRED_NEW,
        label="Slack expired approval",
    )
    return _replace_exact(
        patched,
        SLACK_BLOCKS_OLD,
        SLACK_BLOCKS_NEW,
        label="Slack approval rendering",
    )


def patch_slack_approval_test_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        SLACK_CALLBACK_TEST_ANCHOR,
        SLACK_CALLBACK_TEST_INSERT + SLACK_CALLBACK_TEST_ANCHOR,
        label="Slack callback acknowledgement test",
    )
    ast.parse(patched)
    return patched


def patch_discord_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    patched = _replace_exact(
        source,
        DISCORD_ALREADY_RESOLVED_OLD,
        DISCORD_ALREADY_RESOLVED_NEW,
        label="Discord resolved approval",
    )
    patched = _replace_exact(
        patched,
        DISCORD_EXPIRED_OLD,
        DISCORD_EXPIRED_NEW,
        label="Discord expired approval",
    )
    return _replace_exact(
        patched,
        DISCORD_APPROVAL_TIMEOUT_OLD,
        DISCORD_APPROVAL_TIMEOUT_NEW,
        label="Discord approval timeout",
    )


def patch_slash_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        SLASH_APPROVAL_OLD,
        SLASH_APPROVAL_NEW,
        label="slash expired approval",
    )


def patch_feishu_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        FEISHU_EXPIRED_OLD,
        FEISHU_EXPIRED_NEW,
        label="Feishu expired approval",
    )


def patch_matrix_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        MATRIX_EXPIRED_OLD,
        MATRIX_EXPIRED_NEW,
        label="Matrix expired approval",
    )


def patch_teams_approval_text(source: str) -> str:
    if CONTINUITY_MARKER in source:
        return source
    return _replace_exact(
        source,
        TEAMS_EXPIRED_OLD,
        TEAMS_EXPIRED_NEW,
        label="Teams expired approval",
    )


def patch_silent_context_lifecycle_v1(
    hermes_dir: Path,
    *,
    dry_run: bool = False,
) -> bool:
    hermes_dir = Path(hermes_dir)
    gateway_path = _target(hermes_dir, "gateway", "run.py")
    targets = {
        _target(hermes_dir, "agent", "conversation_compression.py"): patch_completion_text,
        gateway_path: lambda source: patch_continuity_gateway_text(
            patch_gateway_text(source)
        ),
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_telegram_noise_filter.py",
        ): patch_gateway_test_text,
        _target(hermes_dir, "gateway", "delivery_ledger.py"): patch_delivery_ledger_text,
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_delivery_ledger.py",
        ): patch_delivery_ledger_test_text,
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_restart_resume_pending.py",
        ): patch_restart_test_text,
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_drain_inbox.py",
        ): patch_drain_consumer_test_text,
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_command_bypass_active_session.py",
        ): patch_slash_sender_test_text,
        _target(
            hermes_dir,
            "plugins",
            "platforms",
            "telegram",
            "adapter.py",
        ): patch_telegram_approval_text,
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_telegram_approval_buttons.py",
        ): patch_telegram_approval_test_text,
        _target(
            hermes_dir,
            "gateway",
            "platforms",
            "whatsapp_cloud.py",
        ): patch_whatsapp_approval_text,
        _target(hermes_dir, "gateway", "relay", "adapter.py"): patch_relay_approval_text,
        _target(
            hermes_dir,
            "plugins",
            "platforms",
            "slack",
            "adapter.py",
        ): patch_slack_approval_text,
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_slack_approval_buttons.py",
        ): patch_slack_approval_test_text,
        _target(
            hermes_dir,
            "plugins",
            "platforms",
            "discord",
            "adapter.py",
        ): patch_discord_approval_text,
        _target(
            hermes_dir,
            "plugins",
            "platforms",
            "feishu",
            "adapter.py",
        ): patch_feishu_approval_text,
        _target(
            hermes_dir,
            "plugins",
            "platforms",
            "matrix",
            "adapter.py",
        ): patch_matrix_approval_text,
        _target(
            hermes_dir,
            "plugins",
            "platforms",
            "teams",
            "adapter.py",
        ): patch_teams_approval_text,
        _target(hermes_dir, "gateway", "slash_commands.py"): patch_slash_approval_text,
    }
    originals = {path: path.read_text(encoding="utf-8") for path in targets}
    patched = {path: fn(originals[path]) for path, fn in targets.items()}
    changed = [path for path in targets if patched[path] != originals[path]]
    if not changed:
        return False

    for source in patched.values():
        ast.parse(source)
    if dry_run:
        return True

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in changed:
        backup = path.with_suffix(
            path.suffix + f".bak-{stamp}-pre-silent-context-lifecycle-v1"
        )
        shutil.copy2(path, backup)
    for path in changed:
        path.write_text(patched[path], encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        changed = patch_silent_context_lifecycle_v1(
            Path(args.hermes_dir),
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    print("DRY_RUN OK" if args.dry_run else ("OK: patched" if changed else "OK: already patched"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
