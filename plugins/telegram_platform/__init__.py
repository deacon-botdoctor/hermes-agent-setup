"""telegram_platform — disabled placeholder for a future chat-adapter override.

This package intentionally does not register anything yet. The bundled Telegram adapter remains
active until this adapter subclasses or wraps it; media timeout, liveness, PDF/document ingest,
and reply-media hardening are not active from this package.
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
    """Defer registration until this adapter wraps the bundled Telegram adapter."""
    import logging

    logging.getLogger("telegram_platform").warning(
        "telegram_platform: HardenedTelegramAdapter is a placeholder; leaving bundled telegram "
        "adapter registered."
    )
