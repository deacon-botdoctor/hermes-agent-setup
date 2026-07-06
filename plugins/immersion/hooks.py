"""Output-transform hooks — the outbound reply rules.

`transform_llm_output` runs on the message the client is about to receive. These transforms
strip operator-internal noise and mask failure internals so a client only ever sees clean,
finished output. Each is a pure string transform: text in, cleaned text out.

Registered via the plugin's transform_llm_output hook. Keep them cheap (they run on every
outbound message) and side-effect free.
"""
from __future__ import annotations

import re

# Internal continuity / reset / system-note blocks that sometimes leak into model output.
# Adjust the markers to match whatever your runtime injects internally.
_INTERNAL_NOTE_RE = re.compile(
    r"(?ms)^\s*(\[(?:CONTINUITY|SESSION-RESET|INTERNAL|SYSTEM-NOTE)\].*?\[/(?:CONTINUITY|SESSION-RESET|INTERNAL|SYSTEM-NOTE)\])\s*",
)
# Only a message that is ENTIRELY a placeholder gets blanked — not one that merely starts with
# one of these words ("Interrupted the deploy to..." is a real answer, keep it).
_INTERRUPTED_RE = re.compile(
    r"(?i)^\s*(one moment|still working|please hold|interrupted)[.!,:… ]*$",
)

# Secret shapes to scrub from any client-bound message. Belt-and-suspenders on top of the
# write-boundary redaction: a secret should never reach the client's screen either. Strict
# shapes only, so this never touches normal prose.
_SECRET_RES = [
    re.compile(r"[0-9]{8,12}:AA[A-Za-z0-9_-]{30,}"),          # bot token
    re.compile(r"sk-or-v1-[a-f0-9]{40,}"),                    # OpenRouter
    re.compile(r"sk-ant-[a-z]+[0-9]+-[A-Za-z0-9_-]{40,}"),    # Anthropic
    re.compile(r"sk-proj-[A-Za-z0-9_-]{40,}"),                # OpenAI project
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),                # GitHub tokens
]


def redact_secrets_from_output(text: str | None) -> str | None:
    """Scrub credential shapes from a client-bound message (was the display_redact patch)."""
    if not text:
        return text
    for pat in _SECRET_RES:
        text = pat.sub("[redacted]", text)
    return text


def strip_leaked_internal_system_notes(text: str | None) -> str | None:
    """Remove internal continuity/reset system-note blocks from client output."""
    if not text:
        return text
    return _INTERNAL_NOTE_RE.sub("", text).strip() or None


def suppress_interrupted_final_response(text: str | None, *, is_client: bool = True) -> str | None:
    """Blank an interrupted-waiting notice on client lanes.

    When a turn is interrupted, the runtime can emit a placeholder ("one moment...") as the
    final response. A client should get nothing rather than a dangling placeholder — the real
    answer arrives on the next turn. Operator lanes keep it.
    """
    if not text or not is_client:
        return text
    if _INTERRUPTED_RE.match(text.strip()):
        return None
    return text


# A RAW unhandled-error surface — a real traceback, a raw error object/line, or a short provider
# failure line. NOT prose
# that happens to mention "rate limit" or "traceback": the agent answering a question about errors
# is a legitimate reply and must pass through untouched.
_ANCHORED_RAW_ERROR_RES = (
    re.compile(r"Traceback \(most recent call last\):"),                       # a real python traceback
    re.compile(r'(?is)^\s*[\[{]?\s*"?error"?\s*[:=]'),                          # message IS a raw error object
)
_FUZZY_PROVIDER_ERROR_RE = re.compile(
    r"(?im)^\s*(error|exception|openrouter|openai|anthropic)[: ].{0,80}\b(4\d\d|5\d\d|rate.?limit|quota|unauthorized|timeout)\b"
)


def generic_model_failure_final_response(text: str | None, *, is_client: bool = True) -> str | None:
    """Replace a raw provider/model failure surface with a clean generic message on client lanes.

    Only fires on an actual error surface (a traceback, raw error object, or short provider line)
    — a client should never see raw failure internals, but a normal reply that mentions errors is
    fine.
    """
    if not text or not is_client:
        return text
    is_raw_error = any(r.search(text) for r in _ANCHORED_RAW_ERROR_RES)
    is_provider_error_line = len(text) < 240 and _FUZZY_PROVIDER_ERROR_RE.search(text)
    if is_raw_error or is_provider_error_line:
        return "Sorry, I hit a snag on that one. Give me another try in a moment."
    return text


# The hook the runtime calls. Chain the transforms; order matters (strip, then interrupt, then
# failure-mask). Return the final text (or None to suppress the message entirely).
def transform_llm_output(output: str | None = None, *, is_client: bool = True, **_) -> str | None:
    text = strip_leaked_internal_system_notes(output)
    text = suppress_interrupted_final_response(text, is_client=is_client)
    text = generic_model_failure_final_response(text, is_client=is_client)
    text = redact_secrets_from_output(text)  # last: nothing secret reaches the client
    return text
