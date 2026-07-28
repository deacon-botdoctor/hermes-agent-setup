"""
task-ledger — Hermes plugin
Persistent task tracking for work items from chats.

Tools exposed to agents:
  task_open   — register a new work item when user asks for something
  task_update — mark task as in-progress with status note
  task_done   — mark task done with artifact reference
  task_block  — mark task blocked with reason
  task_list   — list open/recent tasks
"""

from .tools import register

__all__ = ["register"]
