"""LLM-request middleware — history hygiene before the provider call.

`llm_request` runs on the request about to go to the model provider. These clean up the message
history so it stays cheap and valid:

- elide_stale_tool_results: when the agent read large files or ran big tools, those tool_result
  blocks bloat the context every subsequent turn. Replace stale oversized ones (keeping the most
  recent few) with a short summary stub. This is the "tool-call mute" on the context side — it
  keeps huge tool output from dominating the window.
- drop_orphan_tool_outputs: drop function/tool outputs that have no matching call (which some
  providers reject).

Both return the modified request plus a count of what they changed, for telemetry.
"""
from __future__ import annotations

from typing import Any

KEEP_LAST = 3            # keep this many recent tool messages verbatim
ELIDE_OVER_CHARS = 4000  # elide tool results larger than this


def _is_tool_message(m: Any) -> bool:
    return isinstance(m, dict) and m.get("role") in ("tool", "function")


def elide_stale_tool_results(request: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Replace stale oversized tool messages with a summary stub; keep the last KEEP_LAST."""
    msgs = request.get("messages")
    if not isinstance(msgs, list):
        return request, 0
    tool_idxs = [i for i, m in enumerate(msgs) if _is_tool_message(m)]
    stale = set(tool_idxs[:-KEEP_LAST]) if len(tool_idxs) > KEEP_LAST else set()
    elided = 0
    for i in stale:
        content = msgs[i].get("content")
        if isinstance(content, str) and len(content) > ELIDE_OVER_CHARS:
            msgs[i] = {**msgs[i], "content": f"[elided tool result — {len(content)} chars]"}
            elided += 1
    return request, elided


def drop_orphan_tool_outputs(request: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Drop tool/function outputs with no matching tool_call_id earlier in the history."""
    msgs = request.get("messages")
    if not isinstance(msgs, list):
        return request, 0
    seen_calls: set[str] = set()
    for m in msgs:
        if isinstance(m, dict):
            for tc in m.get("tool_calls") or []:
                cid = tc.get("id") if isinstance(tc, dict) else None
                if cid:
                    seen_calls.add(cid)
    kept, dropped = [], 0
    for m in msgs:
        if _is_tool_message(m):
            cid = m.get("tool_call_id")
            if cid and cid not in seen_calls:
                dropped += 1
                continue
        kept.append(m)
    request["messages"] = kept
    return request, dropped


def llm_request_middleware(request: dict[str, Any] | None = None, **_) -> dict[str, Any] | None:
    """The middleware the runtime calls. Run history-hygiene passes in order."""
    if not request:
        return request
    request, _elided = elide_stale_tool_results(request)
    request, _dropped = drop_orphan_tool_outputs(request)
    return request
