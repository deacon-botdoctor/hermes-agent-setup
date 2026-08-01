#!/usr/bin/env python3
"""Keep automatic context lifecycle mechanics off human chat transcripts.

Local, API, and webhook diagnostics remain intact. Manual ``/compress``
feedback also remains visible; only automatic compaction/reset lifecycle output
is suppressed from human chat surfaces.
"""
from __future__ import annotations

import argparse
import ast
import shutil
import time
from pathlib import Path

COMPLETION_MARKER = "HERMES_SUPPRESS_COMPACTION_COMPLETION_STATUS_v1"
CHAT_MARKER = "HERMES_SILENT_AUTO_CONTEXT_LIFECYCLE_v1"

HELPER_OLD = '''def _emit_compaction_done(agent: Any) -> None:
    """Emit the structured terminal edge for a started compaction."""
    status_callback = getattr(agent, "status_callback", None)
    if not status_callback:
        return
    try:
        status_callback("compacted", COMPACTION_DONE_STATUS)
    except Exception:
        logger.debug("status_callback error in compaction completion", exc_info=True)
'''

HELPER_NEW = '''def _emit_compaction_done(agent: Any) -> None:
    """Keep compaction completion internal; never create a transcript event."""
    # HERMES_SUPPRESS_COMPACTION_COMPLETION_STATUS_v1
    return
'''

GATEWAY_NOISY_ANCHOR = '    r"|session\\s+compressed\\s+\\d+\\s+times"\n'
GATEWAY_NOISY_REPLACEMENT = GATEWAY_NOISY_ANCHOR + '''    # HERMES_SILENT_AUTO_CONTEXT_LIFECYCLE_v1
    # Automatic blocked-overflow diagnostics stay available on raw local/API/
    # webhook surfaces; human chat surfaces recover silently through the
    # configured context lifecycle instead of delegating /new or /compress.
    r"|context\\s+is\\s+over\\s+the\\s+compression\\s+threshold.*"
    r"compression\\s+is\\s+currently\\s+blocked"
'''

TEST_NOISY_LIST_END_ANCHOR = '''    (
        "⚠ Skipping concurrent compression — another path is already "
        "compressing this session. Will retry after it finishes."
    ),
]

# Messages that must NEVER be swallowed by the compression-noise filter:
'''
TEST_NOISY_LIST_END_REPLACEMENT = '''    (
        "⚠ Skipping concurrent compression — another path is already "
        "compressing this session. Will retry after it finishes."
    ),
    # HERMES_SILENT_AUTO_CONTEXT_LIFECYCLE_v1 — automatic blocked-overflow
    # diagnostics remain raw locally but are lifecycle noise on human chat.
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="cooldown:30"
    ),
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="ineffective"
    ),
]

# Messages that must NEVER be swallowed by the compression-noise filter:
'''
TEST_VISIBLE_BLOCK = '''    # Blocked-overflow warning (#62625/#62708): the context is over the
    # compression threshold but compression is blocked (summary-LLM cooldown
    # or the anti-thrash breaker). FAILURE-CLASS — must reach chat users so
    # they can /new or /compress before the session dies at the hard token
    # limit. Formatted from the SAME template the emit site uses, so a
    # rewording that drifts into the noise regex fails here.
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="cooldown:30"
    ),
    CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
        tokens=85_000, threshold=72_000, reason="ineffective"
    ),
'''


def _target(hermes_dir: Path, *parts: str) -> Path:
    direct = hermes_dir.joinpath(*parts)
    if direct.is_file():
        return direct
    nested = hermes_dir.joinpath("hermes-agent", *parts)
    if nested.is_file():
        return nested
    raise FileNotFoundError(direct)


def patch_completion_text(source: str) -> str:
    if COMPLETION_MARKER in source:
        return source
    if source.count(HELPER_OLD) != 1:
        raise RuntimeError("compaction completion helper anchor missing or ambiguous")
    patched = source.replace(HELPER_OLD, HELPER_NEW, 1)
    if 'status_callback("compacted", COMPACTION_DONE_STATUS)' in patched:
        raise RuntimeError("compaction completion status remained after patch")
    return patched


def patch_gateway_text(source: str) -> str:
    if CHAT_MARKER in source:
        return source
    if source.count(GATEWAY_NOISY_ANCHOR) != 1:
        raise RuntimeError("gateway context-lifecycle noise anchor missing or ambiguous")
    return source.replace(
        GATEWAY_NOISY_ANCHOR,
        GATEWAY_NOISY_REPLACEMENT,
        1,
    )


def patch_gateway_test_text(source: str) -> str:
    if CHAT_MARKER in source:
        return source
    if source.count(TEST_NOISY_LIST_END_ANCHOR) != 1:
        raise RuntimeError("gateway noise-test list anchor missing or ambiguous")
    if source.count(TEST_VISIBLE_BLOCK) != 1:
        raise RuntimeError("gateway visible-warning test anchor missing or ambiguous")
    source = source.replace(
        TEST_NOISY_LIST_END_ANCHOR,
        TEST_NOISY_LIST_END_REPLACEMENT,
        1,
    )
    return source.replace(TEST_VISIBLE_BLOCK, "", 1)


def patch_silent_context_lifecycle_v1(
    hermes_dir: Path,
    *,
    dry_run: bool = False,
) -> bool:
    hermes_dir = Path(hermes_dir)
    targets = {
        _target(hermes_dir, "agent", "conversation_compression.py"): patch_completion_text,
        _target(hermes_dir, "gateway", "run.py"): patch_gateway_text,
        _target(
            hermes_dir,
            "tests",
            "gateway",
            "test_telegram_noise_filter.py",
        ): patch_gateway_test_text,
    }
    originals = {path: path.read_text(encoding="utf-8") for path in targets}
    patched = {path: fn(originals[path]) for path, fn in targets.items()}
    changed = [path for path in targets if patched[path] != originals[path]]
    if not changed:
        return False

    for source in patched.values():
        ast.parse(source)
    if dry_run:
        return True

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in changed:
        backup = path.with_suffix(
            path.suffix + f".bak-{stamp}-pre-silent-context-lifecycle-v1"
        )
        shutil.copy2(path, backup)
    for path in changed:
        path.write_text(patched[path], encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        changed = patch_silent_context_lifecycle_v1(
            Path(args.hermes_dir),
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    print("DRY_RUN OK" if args.dry_run else ("OK: patched" if changed else "OK: already patched"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
