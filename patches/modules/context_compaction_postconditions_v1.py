#!/usr/bin/env python3
# ruff: noqa: E501
"""Validate compaction handoffs before they become recursive prompt state."""

from __future__ import annotations

import argparse
import ast
import shutil
import time
from pathlib import Path

MARKER = "HERMES_CONTEXT_COMPACTION_POSTCONDITIONS_v1"
TARGET = Path("agent/context_compressor.py")

CONSTANT_ANCHOR = '''_SUMMARY_INPUT_MAX_CHARS = 160_000

# Placeholder used when pruning old tool results
'''
CONSTANT_REPLACEMENT = '''_SUMMARY_INPUT_MAX_CHARS = 160_000

# HERMES_CONTEXT_COMPACTION_POSTCONDITIONS_v1
# The calculated summary budget includes the stored handoff wrapper and end
# marker. Reserve room before validating the model-authored body so the final
# prompt-resident message remains inside the same budget.
_SUMMARY_STORAGE_OVERHEAD_TOKENS = 256
_SUMMARY_REPETITION_NGRAM_TOKENS = 12
_SUMMARY_REPETITION_MIN_OCCURRENCES = 8
_SUMMARY_REPETITION_MIN_COVERAGE = 0.05
# Placeholder used when pruning old tool results
'''

HELPER_ANCHOR = '''def _dedupe_append(items: list[str], value: str, *, limit: int) -> None:
    value = value.strip()
    if value and value not in items and len(items) < limit:
        items.append(value)


'''
HELPER_REPLACEMENT = HELPER_ANCHOR + '''def _summary_body_budget(token_budget: int) -> int:
    """Return the maximum body budget after stored-wrapper reservation."""
    return max(256, int(token_budget) - _SUMMARY_STORAGE_OVERHEAD_TOKENS)


def _summary_has_repetition_spike(summary: str) -> bool:
    """Detect a repeated phrase that materially dominates a handoff.

    The coverage gate avoids rejecting ordinary repeated headings, paths, or
    code fragments. It catches recursive-summary failures where one action or
    outcome is copied often enough to consume a meaningful share of the state.
    """
    tokens = re.findall(r"[\\w./:-]+|[^\\w\\s]", (summary or "").casefold())
    width = _SUMMARY_REPETITION_NGRAM_TOKENS
    if len(tokens) < width * _SUMMARY_REPETITION_MIN_OCCURRENCES:
        return False

    counts: dict[tuple[str, ...], int] = {}
    largest = 0
    for index in range(len(tokens) - width + 1):
        window = tuple(tokens[index:index + width])
        count = counts.get(window, 0) + 1
        counts[window] = count
        if count > largest:
            largest = count

    repeated_tokens = largest * width
    return (
        largest >= _SUMMARY_REPETITION_MIN_OCCURRENCES
        and repeated_tokens >= len(tokens) * _SUMMARY_REPETITION_MIN_COVERAGE
    )


def _summary_quality_failure(
    summary: str,
    token_budget: int,
) -> str | None:
    """Return a content-free rejection reason, or ``None`` when valid."""
    text = (summary or "").strip()
    if not text:
        return "empty_summary"

    estimated_tokens = estimate_tokens_rough(text)
    body_budget = _summary_body_budget(token_budget)
    if estimated_tokens > body_budget:
        return f"summary_budget_exceeded:{estimated_tokens}>{body_budget}"
    if _summary_has_repetition_spike(text):
        return "summary_repetition_spike"
    return None


def _fit_summary_to_budget(
    summary: str,
    token_budget: int,
    *,
    suffix: str = "",
) -> str:
    """Deterministically fit a fallback while retaining active head and tail.

    Model output is rejected instead of clipped. This fitter is only for the
    local deterministic fallback, whose head contains the active task and whose
    tail contains critical context and pruned-skill reload markers.
    """
    text = (summary or "").strip()
    if estimate_tokens_rough(text + suffix) <= token_budget:
        return text

    marker = "\\n\\n...[deterministic handoff reduced to enforced budget]...\\n\\n"

    def _candidate(keep_chars: int) -> str:
        available = max(0, keep_chars - len(marker))
        head_chars = int(available * 0.65)
        tail_chars = available - head_chars
        tail = text[-tail_chars:] if tail_chars else ""
        return text[:head_chars].rstrip() + marker + tail.lstrip()

    low, high = 0, len(text)
    best = marker.strip()
    while low <= high:
        mid = (low + high) // 2
        candidate = _candidate(mid)
        if estimate_tokens_rough(candidate + suffix) <= token_budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


'''

COMPLETED_ACTIONS_OLD = '''## Completed Actions
[Numbered list of concrete actions taken — include tool used, target, and outcome.
Format each as: N. ACTION target — outcome [tool: name]
Example:
1. READ config.py:45 — found `==` should be `!=` [tool: read_file]
2. PATCH config.py:45 — changed `==` to `!=` [tool: patch]
3. TEST `pytest tests/` — 3/50 failed: test_parse, test_validate, test_edge [tool: terminal]
Be specific with file paths, commands, line numbers, and results.]
'''
COMPLETED_ACTIONS_NEW = '''## Completed Actions
[At most 12 MATERIAL outcomes that remain relevant to the active task. Preserve
the resulting state, not the execution diary. Collapse repeated reads, tool
calls, retries, commands, paths, and equivalent outcomes into one entry. Omit
routine completed work that does not constrain continuation.]
'''

ITERATIVE_OLD = '''Update the summary using this exact structure. PRESERVE all existing information that is still relevant. ADD new completed actions to the numbered list (continue numbering). Move items from "In Progress" to "Completed Actions" when done. Move answered questions to "Resolved Questions". Update "Active State" to reflect current state. Remove information only if it is clearly obsolete. CRITICAL: Update "## Active Task" to reflect the user's most recent unfulfilled input — this includes any question, decision request, or discussion turn that the assistant has not yet answered. Only write "None" if the last exchange was fully resolved.
'''
ITERATIVE_NEW = '''REPLACE the previous checkpoint with a bounded current-state handoff using this exact structure. Preserve only information that still constrains the active request, unresolved blockers, relevant decisions, and minimal file/runtime state. Do NOT append a historical work log. Collapse duplicate actions, tool calls, retries, paths, commands, and outcomes. Keep at most 12 material completed outcomes and remove completed or unrelated topic branches. CRITICAL: Update "## Active Task" to reflect the user's most recent unfulfilled input — this includes any question, decision request, or discussion turn that the assistant has not yet answered. Only write "None" if the last exchange was fully resolved.
'''

GENERATED_VALIDATION_ANCHOR = '''            self._validate_summary_user_provenance(summary, has_user_turn)
            # Store for iterative updates on next compaction
            self._previous_summary = summary
'''
GENERATED_VALIDATION_REPLACEMENT = '''            self._validate_summary_user_provenance(summary, has_user_turn)
            quality_failure = _summary_quality_failure(
                summary,
                summary_budget,
            )
            stored_summary_tokens = estimate_tokens_rough(
                self._with_summary_prefix(summary)
                + "\\n\\n"
                + _SUMMARY_END_MARKER
            )
            if not quality_failure and stored_summary_tokens > summary_budget:
                quality_failure = (
                    "stored_summary_budget_exceeded:"
                    f"{stored_summary_tokens}>{summary_budget}"
                )
            if quality_failure:
                telemetry = getattr(self, "_active_compression_telemetry", None)
                if isinstance(telemetry, dict):
                    telemetry["failure_class"] = quality_failure.split(":", 1)[0]
                self._record_ineffective_compression_verdict(
                    self._ineffective_compression_count + 1,
                )
                raise RuntimeError(
                    "Context compression summary rejected by postconditions: "
                    + quality_failure
                )
            # Store for iterative updates on next compaction only after every
            # non-empty, budget, repetition, and provenance check passes.
            self._previous_summary = summary
'''

SUMMARY_SCAN_ANCHOR = '''        summary_hits = self._find_context_summaries(
            messages,
            summary_search_start,
            summary_search_end,
        )
        real_user_present = self._transcript_has_real_user_turn(messages)
'''
SUMMARY_SCAN_REPLACEMENT = '''        summary_hits = self._find_context_summaries(
            messages,
            summary_search_start,
            summary_search_end,
        )
        invalid_previous_summary = False
        real_user_present = self._transcript_has_real_user_turn(messages)
'''

REHYDRATION_ANCHOR = '''                if summary_bodies:
                    self._previous_summary = "\\n\\n".join(summary_bodies)
            # Zero-user provenance (#64650) rides on the newest handoff hit.
'''
REHYDRATION_REPLACEMENT = '''                if summary_bodies:
                    self._previous_summary = "\\n\\n".join(summary_bodies)
            if self._previous_summary:
                previous_failure = _summary_quality_failure(
                    self._previous_summary,
                    self.max_summary_tokens,
                )
                if previous_failure:
                    # A defective fossil is never recursively summarized. The
                    # summary row is still removed from the next assembled
                    # prompt; current compactable turns plus the protected tail
                    # seed a clean replacement.
                    invalid_previous_summary = True
                    self._previous_summary = None
                    telemetry["failure_class"] = "invalid_previous_summary"
                    self._record_ineffective_compression_verdict(
                        self._ineffective_compression_count + 1,
                    )
                    if not self.quiet_mode:
                        logger.warning(
                            "Discarding persisted context summary that failed "
                            "postconditions: %s",
                            previous_failure,
                        )
            # Zero-user provenance (#64650) rides on the newest handoff hit.
'''

EMPTY_WINDOW_ANCHOR = '''        if not turns_to_summarize:
            # The newest handoff summary consumed the entire compressible
'''
EMPTY_WINDOW_REPLACEMENT = '''        if not turns_to_summarize and invalid_previous_summary:
            # The only historical state was a defective handoff. Seed a clean
            # replacement from the newest non-summary tail without removing
            # those protected turns from the final prompt.
            recovery_start = max(
                compress_end,
                len(messages) - _PRESSURE_KEEP_RECENT_MESSAGES,
            )
            recovery_turns = []
            for message in messages[recovery_start:]:
                stripped = self._strip_context_summary_handoff_message(
                    _fresh_compaction_message_copy(message)
                )
                if stripped is not None:
                    recovery_turns.append(stripped)
            turns_to_summarize = recovery_turns or [{
                "role": "assistant",
                "content": (
                    "No uncompacted recent turn was available; create a clean "
                    "state-only checkpoint without historical execution logs."
                ),
            }]
            telemetry["chunk_count"] = 1

        if not turns_to_summarize:
            # The newest handoff summary consumed the entire compressible
'''

FALLBACK_ANCHOR = '''            summary = self._build_static_fallback_summary(
                turns_to_summarize,
                reason=self._last_summary_error,
            )

        tail_messages: List[Dict[str, Any]] = []
'''
FALLBACK_REPLACEMENT = '''            summary = self._build_static_fallback_summary(
                turns_to_summarize,
                reason=self._last_summary_error,
            )
            # The fallback is local and structured, so it may be reduced
            # deterministically. Reserve the final end-marker allowance here;
            # model-authored summaries are rejected rather than clipped.
            fallback_budget = self._compute_summary_budget(turns_to_summarize)
            summary = _fit_summary_to_budget(
                summary,
                fallback_budget,
                suffix="\\n\\n" + _SUMMARY_END_MARKER,
            )

        tail_messages: List[Dict[str, Any]] = []
'''

FALLBACK_FEASIBILITY_ANCHOR = '''            summary = self._build_static_fallback_summary(
                turns_to_summarize,
                # A stale error from an earlier real failure must not be
                # embedded into a deliberate feasibility skip's fallback.
                reason=None if feasibility_skip else self._last_summary_error,
            )

        tail_messages: List[Dict[str, Any]] = []
'''
FALLBACK_FEASIBILITY_REPLACEMENT = '''            summary = self._build_static_fallback_summary(
                turns_to_summarize,
                # A stale error from an earlier real failure must not be
                # embedded into a deliberate feasibility skip's fallback.
                reason=None if feasibility_skip else self._last_summary_error,
            )
            # The fallback is local and structured, so it may be reduced
            # deterministically. Reserve the final end-marker allowance here;
            # model-authored summaries are rejected rather than clipped.
            fallback_budget = self._compute_summary_budget(turns_to_summarize)
            summary = _fit_summary_to_budget(
                summary,
                fallback_budget,
                suffix="\\n\\n" + _SUMMARY_END_MARKER,
            )

        tail_messages: List[Dict[str, Any]] = []
'''

ABORT_CONDITION_ANCHOR = '''        if not summary and (
            self.abort_on_summary_failure
            or self._last_summary_auth_failure
            or self._last_summary_network_failure
        ):
'''
ABORT_CONDITION_REPLACEMENT = '''        if not summary and not invalid_previous_summary and (
            self.abort_on_summary_failure
            or self._last_summary_auth_failure
            or self._last_summary_network_failure
):
'''

ABORT_FEASIBILITY_ANCHOR = '''        if not summary and not feasibility_skip and (
            self.abort_on_summary_failure
            or self._last_summary_auth_failure
            or self._last_summary_network_failure
        ):
'''
ABORT_FEASIBILITY_REPLACEMENT = '''        if not summary and not feasibility_skip and not invalid_previous_summary and (
            self.abort_on_summary_failure
            or self._last_summary_auth_failure
            or self._last_summary_network_failure
        ):
'''


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    found = source.count(old)
    if found != 1:
        raise RuntimeError(
            f"[context_compaction_postconditions] {label} anchor found "
            f"{found} times (expected 1)"
        )
    return source.replace(old, new, 1)


def patch_context_compressor_source(source: str) -> str:
    """Return the validated compressor postimage, failing closed on drift."""
    required = (
        "def _summary_quality_failure(",
        "summary_repetition_spike",
        "invalid_previous_summary",
        'suffix="\\n\\n" + _SUMMARY_END_MARKER',
    )
    if MARKER in source:
        if not all(seam in source for seam in required):
            raise RuntimeError(
                "marked context compaction postconditions are incomplete"
            )
        return source

    patched = _replace_once(
        source, CONSTANT_ANCHOR, CONSTANT_REPLACEMENT, "constants"
    )
    patched = _replace_once(
        patched, HELPER_ANCHOR, HELPER_REPLACEMENT, "quality helpers"
    )
    patched = _replace_once(
        patched,
        COMPLETED_ACTIONS_OLD,
        COMPLETED_ACTIONS_NEW,
        "completed-actions prompt",
    )
    patched = _replace_once(
        patched, ITERATIVE_OLD, ITERATIVE_NEW, "iterative prompt"
    )
    patched = _replace_once(
        patched,
        GENERATED_VALIDATION_ANCHOR,
        GENERATED_VALIDATION_REPLACEMENT,
        "generated-summary validation",
    )
    patched = _replace_once(
        patched, SUMMARY_SCAN_ANCHOR, SUMMARY_SCAN_REPLACEMENT, "summary scan"
    )
    patched = _replace_once(
        patched, REHYDRATION_ANCHOR, REHYDRATION_REPLACEMENT, "rehydration"
    )
    patched = _replace_once(
        patched, EMPTY_WINDOW_ANCHOR, EMPTY_WINDOW_REPLACEMENT, "clean handoff"
    )
    fallback_variants = (
        (FALLBACK_ANCHOR, FALLBACK_REPLACEMENT),
        (FALLBACK_FEASIBILITY_ANCHOR, FALLBACK_FEASIBILITY_REPLACEMENT),
    )
    fallback_matches = [
        (anchor, replacement)
        for anchor, replacement in fallback_variants
        if patched.count(anchor) == 1
    ]
    if len(fallback_matches) != 1:
        raise RuntimeError(
            "[context_compaction_postconditions] fallback budget anchor found "
            f"{len(fallback_matches)} times (expected 1)"
        )
    patched = patched.replace(*fallback_matches[0], 1)
    abort_variants = (
        (ABORT_CONDITION_ANCHOR, ABORT_CONDITION_REPLACEMENT),
        (ABORT_FEASIBILITY_ANCHOR, ABORT_FEASIBILITY_REPLACEMENT),
    )
    abort_matches = [
        (anchor, replacement)
        for anchor, replacement in abort_variants
        if patched.count(anchor) == 1
    ]
    if len(abort_matches) != 1:
        raise RuntimeError(
            "[context_compaction_postconditions] invalid-fossil clean fallback "
            f"anchor found {len(abort_matches)} times (expected 1)"
        )
    patched = patched.replace(*abort_matches[0], 1)
    ast.parse(patched)
    return patched


def patch_context_compaction_postconditions_v1(hermes_dir: Path) -> bool:
    """Apply one transactional source patch to the pinned compressor."""
    target = Path(hermes_dir) / TARGET
    if not target.is_file():
        raise RuntimeError(f"context compressor target missing: {target}")

    original = target.read_text(encoding="utf-8")
    patched = patch_context_compressor_source(original)
    if patched == original:
        return False

    backup = target.with_suffix(
        target.suffix
        + f".bak-{time.strftime('%Y%m%d-%H%M%S')}-context-postconditions"
    )
    try:
        shutil.copy2(target, backup)
        target.write_text(patched, encoding="utf-8")
    except Exception:
        target.write_text(original, encoding="utf-8")
        backup.unlink(missing_ok=True)
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-dir", required=True, type=Path)
    args = parser.parse_args()
    changed = patch_context_compaction_postconditions_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
