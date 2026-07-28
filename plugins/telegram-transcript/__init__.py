"""
telegram-transcript — Hermes plugin
Indexes all Telegram messages (inbound + outbound) to SQLite.
Exposes telegram_history tool for reading chat history.
"""

from .tools import register


__all__ = ["register"]
