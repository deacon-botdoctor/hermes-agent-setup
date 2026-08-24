"""Durable Hermes task tracking with evidence-backed terminal states.

The tool schemas in :mod:`.tools` own the exact contracts. In brief, agents can
open and update durable work, close it only after acceptance reconciliation and
required delivery proof, record an evidence-backed blocker, and list tasks.
"""

from .tools import register

__all__ = ["register"]
