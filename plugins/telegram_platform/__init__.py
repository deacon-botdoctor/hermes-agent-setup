"""telegram_platform — a thin chat-adapter override.

Registers under the same key as the bundled telegram adapter, so it replaces it while inheriting
everything you don't override. Carries the delivery hardening a client lane needs: media send
timeouts, connection-liveness heartbeats, PDF/document extraction, and media-in-replies.

A plugin, not a patch: same-key `register_platform` is a supported seam, so this survives upstream
bumps that a source edit would not. Override methods, do not fork files.
"""
from __future__ import annotations

# from your_runtime.platforms.telegram import TelegramAdapter as _Bundled

SEND_TIMEOUT = 45  # seconds — media sends get an explicit timeout + one retry


class HardenedTelegramAdapter:  # subclass the bundled adapter: class HardenedTelegramAdapter(_Bundled)
    """Override only the delivery methods that need hardening; inherit the rest."""

    async def send_media(self, chat_id, media, **kwargs):
        """Explicit timeout + single retry so a slow media upload doesn't hang the turn."""
        # try:
        #     return await super().send_media(chat_id, media, timeout=SEND_TIMEOUT, **kwargs)
        # except TimeoutError:
        #     return await super().send_media(chat_id, media, timeout=SEND_TIMEOUT, **kwargs)
        raise NotImplementedError

    def note_liveness(self) -> None:
        """Write a connection-liveness heartbeat a watchdog can read (prevents false restarts)."""
        raise NotImplementedError

    async def ingest_document(self, document) -> str:
        """Extract text from an inbound PDF/document so the agent can read it."""
        raise NotImplementedError


def register(ctx) -> None:
    """Register the hardened adapter under the bundled telegram key. Never raises."""
    import logging

    if hasattr(ctx, "register_platform"):
        ctx.register_platform("telegram", HardenedTelegramAdapter)
    else:
        logging.getLogger("telegram_platform").warning(
            "telegram_platform: no register_platform on ctx; wire HardenedTelegramAdapter manually."
        )
