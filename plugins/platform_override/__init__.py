"""platform_override — a thin chat-adapter override (skeleton).

Registers under the SAME key as a bundled chat platform, so it replaces the bundled adapter.
Because it subclasses the bundled one, you inherit all upstream behavior and override only the
methods you need — reliable media send timeouts, connection-liveness writes, and similar
delivery hardening. Override methods, do not fork files.

This is rung 3 (plugin), not a patch: same-key registration is a supported seam, so it survives
upstream bumps that a source patch would not.
"""
from __future__ import annotations

# from your_runtime.platforms.telegram import TelegramAdapter as _Bundled  # the bundled adapter


class HardenedAdapter:  # subclass the bundled adapter in real use: class HardenedAdapter(_Bundled)
    """Override only what needs changing; inherit the rest."""

    async def send_media(self, *args, **kwargs):
        """Example override: give media sends an explicit timeout and one retry."""
        # return await super().send_media(*args, timeout=SEND_TIMEOUT, **kwargs)
        raise NotImplementedError

    def note_liveness(self) -> None:
        """Example override: write a connection-liveness heartbeat a watchdog can read."""
        raise NotImplementedError


def register(ctx) -> None:
    """Plugin entry point. Registering under the bundled platform's key replaces it."""
    # ctx.register_platform("telegram", HardenedAdapter)   # same key as the bundled one
    raise NotImplementedError("register HardenedAdapter under your bundled platform's key")
