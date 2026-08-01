"""Retry transient inbound Telegram media downloads before surfacing failure.

[HERMES_TELEGRAM_MEDIA_DOWNLOAD_RETRY_v1]

Telegram file URLs are fetched in two stages: ``get_file()`` obtains a fresh
file descriptor/URL and ``download_as_bytearray()`` reads the CDN payload. A
transient timeout in either stage is safe to retry because both operations are
reads. This patch centralizes that retry without replaying message handling,
cache writes, media-group queueing, or agent delivery.
"""
from __future__ import annotations

import ast
import shutil
import time
from pathlib import Path

MARKER = "HERMES_TELEGRAM_MEDIA_DOWNLOAD_RETRY_v1"

HELPER = '''    async def _download_telegram_media_with_retry(
        self,
        source: Any,
        *,
        kind: str,
    ) -> tuple[Any, bytes]:
        """Return a fresh Telegram file object and bytes after bounded retries.

        [HERMES_TELEGRAM_MEDIA_DOWNLOAD_RETRY_v1]
        Only transient network failures retry. Each attempt refreshes the
        Telegram file descriptor/URL; cache, validation, and format errors stay
        outside this helper and therefore fail once.
        """
        attempts = 3
        base_delay = 0.25
        for attempt in range(1, attempts + 1):
            try:
                file_obj = await source.get_file()
                payload = await file_obj.download_as_bytearray()
                return file_obj, bytes(payload)
            except Exception as exc:
                retryable = self._looks_like_network_error(exc) or (
                    exc.__class__.__name__.lower() == "retryafter"
                )
                if not retryable or attempt >= attempts:
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "[Telegram] Retrying %s download after transient %s "
                    "(attempt %s/%s, delay %.2fs)",
                    kind,
                    exc.__class__.__name__,
                    attempt + 1,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable Telegram media retry state")

'''

HELPER_ANCHOR = "    async def _surface_media_cache_failure(\n"

CALLSITE_REPLACEMENTS = (
    (
        "            file_obj = await source.get_file()\n"
        "            data = bytes(await file_obj.download_as_bytearray())\n",
        "            file_obj, data = await self._download_telegram_media_with_retry(\n"
        "                source, kind=kind\n"
        "            )\n",
        2,
        "observed/replied media",
    ),
    (
        "                file_obj = await photo.get_file()\n"
        "                # Download the image bytes directly into memory\n"
        "                image_bytes = await file_obj.download_as_bytearray()\n",
        "                file_obj, image_bytes = await self._download_telegram_media_with_retry(\n"
        "                    photo, kind=\"photo\"\n"
        "                )\n",
        1,
        "photo",
    ),
    (
        "                file_obj = await msg.voice.get_file()\n"
        "                audio_bytes = await file_obj.download_as_bytearray()\n",
        "                file_obj, audio_bytes = await self._download_telegram_media_with_retry(\n"
        "                    msg.voice, kind=\"voice message\"\n"
        "                )\n",
        1,
        "voice",
    ),
    (
        "                file_obj = await msg.audio.get_file()\n"
        "                audio_bytes = await file_obj.download_as_bytearray()\n",
        "                file_obj, audio_bytes = await self._download_telegram_media_with_retry(\n"
        "                    msg.audio, kind=\"audio file\"\n"
        "                )\n",
        1,
        "audio",
    ),
    (
        "                file_obj = await msg.video.get_file()\n"
        "                video_bytes = await file_obj.download_as_bytearray()\n",
        "                file_obj, video_bytes = await self._download_telegram_media_with_retry(\n"
        "                    msg.video, kind=\"video file\"\n"
        "                )\n",
        1,
        "video",
    ),
    (
        "                    file_obj = await doc.get_file()\n"
        "                    image_bytes = await file_obj.download_as_bytearray()\n",
        "                    file_obj, image_bytes = await self._download_telegram_media_with_retry(\n"
        "                        doc, kind=\"image document\"\n"
        "                    )\n",
        1,
        "image document",
    ),
    (
        "                    file_obj = await doc.get_file()\n"
        "                    video_bytes = await file_obj.download_as_bytearray()\n",
        "                    file_obj, video_bytes = await self._download_telegram_media_with_retry(\n"
        "                        doc, kind=\"video document\"\n"
        "                    )\n",
        1,
        "video document",
    ),
    (
        "                file_obj = await doc.get_file()\n"
        "                doc_bytes = await file_obj.download_as_bytearray()\n",
        "                file_obj, doc_bytes = await self._download_telegram_media_with_retry(\n"
        "                    doc, kind=\"document\"\n"
        "                )\n",
        1,
        "document",
    ),
    (
        "            file_obj = await sticker.get_file()\n"
        "            image_bytes = await file_obj.download_as_bytearray()\n",
        "            file_obj, image_bytes = await self._download_telegram_media_with_retry(\n"
        "                sticker, kind=\"sticker\"\n"
        "            )\n",
        1,
        "sticker",
    ),
)

ENV_FLOAT_ANCHOR = '''            def _env_float(name: str, default: float) -> float:
                try:
                    return float(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    return default

'''

CONFIG_FLOAT_HELPER = ENV_FLOAT_ANCHOR + '''            def _extra_float(name: str, default: float) -> float:
                try:
                    return float((self.config.extra or {}).get(name, default))
                except (TypeError, ValueError):
                    return default

'''

READ_TIMEOUT_ANCHOR = (
    '                "read_timeout": _env_float('
    '"HERMES_TELEGRAM_HTTP_READ_TIMEOUT", 20.0),\n'
)
READ_TIMEOUT_REPLACEMENT = (
    '                "read_timeout": _env_float(\n'
    '                    "HERMES_TELEGRAM_HTTP_READ_TIMEOUT",\n'
    '                    _extra_float("http_read_timeout", 60.0),\n'
    '                ),\n'
)


def _replace_exact(src: str, old: str, new: str, count: int, label: str) -> str:
    found = src.count(old)
    if found != count:
        raise RuntimeError(
            f"[telegram_media_download_retry] {label} anchor found "
            f"{found} times (expected {count})"
        )
    return src.replace(old, new)


def patch_telegram_media_download_retry_v1(hermes_dir: Path) -> bool:
    target = hermes_dir / "plugins" / "platforms" / "telegram" / "adapter.py"
    if not target.exists():
        raise RuntimeError(
            "[telegram_media_download_retry] native Telegram adapter missing: "
            f"{target}"
        )

    src = target.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"[telegram_media_download_retry] already patched ({target.name})")
        return False

    src = _replace_exact(src, HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1, "helper")
    for old, new, count, label in CALLSITE_REPLACEMENTS:
        src = _replace_exact(src, old, new, count, label)
    src = _replace_exact(
        src,
        ENV_FLOAT_ANCHOR,
        CONFIG_FLOAT_HELPER,
        1,
        "config float helper",
    )
    src = _replace_exact(
        src,
        READ_TIMEOUT_ANCHOR,
        READ_TIMEOUT_REPLACEMENT,
        1,
        "read timeout",
    )

    ast.parse(src)
    backup = target.with_suffix(
        target.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}-media-download-retry"
    )
    shutil.copy2(target, backup)
    target.write_text(src, encoding="utf-8")
    print(f"[telegram_media_download_retry] PATCHED {target} (backup {backup.name})")
    return True
