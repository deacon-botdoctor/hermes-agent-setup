#!/usr/bin/env python3
"""Allow a changed local artifact to be re-sent from the same path."""
from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Optional

MARKER = "HERMES_MEDIA_SAME_PATH_RESEND_v1"
DELIVERY_MARKER = "HERMES_MEDIA_SAME_PATH_DELIVERY_DEDUP_v1"
BACKUP_SUFFIX = ".bak-pre-media-same-path-resend-v1"
BASE_BACKUP_SUFFIX = ".bak-pre-media-same-path-delivery-dedup-v1"

COLLECTOR_ANCHOR = "def _collect_history_media_paths(agent_history: List[Dict[str, Any]]) -> set:\n"

HELPER = '''def _history_media_path_is_unchanged(path: str, message: Dict[str, Any]) -> bool:
    """Return True when ``path`` still identifies the version in ``message``.

    # HERMES_MEDIA_SAME_PATH_RESEND_v1
    Historical transcripts store only the local path.  The persisted message
    timestamp is therefore the native version boundary: a later file mtime
    means the artifact was replaced after that response and must be eligible
    for delivery again.  Missing files/timestamps retain conservative legacy
    path deduplication.
    """
    delivered_at = _coerce_gateway_timestamp(message.get("timestamp"))
    if delivered_at is None:
        return True
    try:
        current_mtime = Path(os.path.expanduser(path)).stat().st_mtime
    except (OSError, TypeError, ValueError):
        return True
    return current_mtime <= delivered_at


'''

TEXT_MEDIA_COLLECTOR_OLD = '''    def _add_text_media_paths(content: str) -> None:
        for match in _TOOL_MEDIA_RE.finditer(content):
            path = match.group(1).strip().rstrip('",}')
            if path:
                paths.add(path)
        # The regex alone misses quoted and spaced paths that the delivery
        # pipeline's extract_media grammar accepts — collect through the same
        # extractor so the dedup set sees every path that could actually have
        # been delivered.
        media_files, _ = BasePlatformAdapter.extract_media(content)
        paths.update(path for path, _is_voice in media_files)
'''

TEXT_MEDIA_COLLECTOR_NEW = '''    def _add_text_media_paths(content: str, msg: Dict[str, Any]) -> None:
        for match in _TOOL_MEDIA_RE.finditer(content):
            path = match.group(1).strip().rstrip('",}')
            if path and _history_media_path_is_unchanged(path, msg):
                paths.add(path)
        # Reuse the native delivery grammar for quoted/spaced paths while
        # retaining the same timestamp/mtime version boundary.
        media_files, _ = BasePlatformAdapter.extract_media(content)
        paths.update(
            path
            for path, _is_voice in media_files
            if _history_media_path_is_unchanged(path, msg)
        )
'''

ASSISTANT_MEDIA_CALL_OLD = "                _add_text_media_paths(content)\n"
ASSISTANT_MEDIA_CALL_NEW = "                _add_text_media_paths(content, msg)\n"
TOOL_MEDIA_CALL_OLD = "            _add_text_media_paths(content)\n"
TOOL_MEDIA_CALL_NEW = "            _add_text_media_paths(content, msg)\n"

JSON_ADD_OLD = '''                    if isinstance(jp, str) and jp:
                        paths.add(jp)
                        break
'''

JSON_ADD_NEW = '''                    if (
                        isinstance(jp, str)
                        and jp
                        and _history_media_path_is_unchanged(jp, msg)
                    ):
                        paths.add(jp)
                        break
'''

DOC_OLD = '''    Used to dedup auto-appended and model-emitted MEDIA tags so the same file
    is not re-sent on later turns. Covers three delivery shapes:
'''

DOC_NEW = '''    Used to dedup auto-appended and model-emitted MEDIA tags so the same file
    version is not re-sent on later turns. A file replaced after the historical
    message timestamp remains eligible even when its path is unchanged. Covers
    three delivery shapes:
'''

STREAMING_DELIVERY_OLD = '''            media_files, cleaned = adapter.extract_media(response)
            media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
            # Do NOT deduplicate explicit MEDIA tags against prior turns here
            # (#73771). This rescan is already EXPLICIT-ONLY (see docstring):
            # a MEDIA: directive in the final streamed reply is the model
            # deliberately attaching a file — including a user-requested
            # resend. Stale auto-appended tags are deduped upstream in
            # _collect_auto_append_media_tags with history_media_paths.
            # Mirrors the same filter removal on the non-streaming path in
            # gateway/platforms/base.py.
'''

STREAMING_DELIVERY_NEW = '''            media_files, cleaned = adapter.extract_media(response)
            media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
            # HERMES_MEDIA_SAME_PATH_DELIVERY_DEDUP_v1
            _history_media_paths = adapter._history_media_paths_for_session(
                self._session_key_for_source(event.source)
            )
            if _history_media_paths:
                _history_media_resolved = {
                    os.path.realpath(os.path.expanduser(str(path)))
                    for path in _history_media_paths
                }
                media_files = [
                    (path, is_voice)
                    for path, is_voice in media_files
                    if os.path.realpath(os.path.expanduser(str(path)))
                    not in _history_media_resolved
                ]
            # Golden retains version-aware deduplication for explicit tags:
            # changed content at the same path is absent from this history set,
            # while an unchanged version remains suppressed.
'''

BASE_DELIVERY_OLD = '''                # Do NOT deduplicate MEDIA tags against prior turns here.
                # The auto-append path in GatewayRunner._run_agent_inner already
                # deduplicates auto-appended tags via _collect_auto_append_media_tags
                # with history_media_paths, so this filter would only catch explicit
                # MEDIA tags the model deliberately included in its response — which
                # must be preserved (user asked to resend an image, the model echoed
                # a path intentionally, etc.).  Bare-file-path dedup still applies
                # to local_files below via the same _history_media_paths set.
                _history_media_paths = self._history_media_paths_for_session(session_key)

                # Extract image URLs and send them as native platform attachments
'''

BASE_DELIVERY_NEW = '''                # Golden retains version-aware deduplication for explicit tags:
                # changed content at the same path is absent from this history set,
                # while an unchanged version remains suppressed.
                _history_media_paths = self._history_media_paths_for_session(session_key)
                # HERMES_MEDIA_SAME_PATH_DELIVERY_DEDUP_v1
                if _history_media_paths:
                    _history_media_resolved = {
                        os.path.realpath(os.path.expanduser(str(path)))
                        for path in _history_media_paths
                    }
                    media_files = [
                        (path, is_voice)
                        for path, is_voice in media_files
                        if os.path.realpath(os.path.expanduser(str(path)))
                        not in _history_media_resolved
                    ]

                # Extract image URLs and send them as native platform attachments
'''


def _replace_exact(source: str, old: str, new: str, *, count: int, label: str) -> str:
    actual = source.count(old)
    if actual != count:
        raise RuntimeError(f"[media_same_path_resend] {label} anchor found {actual} times (expected {count})")
    return source.replace(old, new, count)


def patch_source(source: str) -> Optional[str]:
    changed = False
    if MARKER not in source:
        source = _replace_exact(
            source,
            COLLECTOR_ANCHOR,
            HELPER + COLLECTOR_ANCHOR,
            count=1,
            label="collector",
        )
        source = _replace_exact(
            source,
            TEXT_MEDIA_COLLECTOR_OLD,
            TEXT_MEDIA_COLLECTOR_NEW,
            count=1,
            label="native text media collector",
        )
        source = _replace_exact(
            source,
            ASSISTANT_MEDIA_CALL_OLD,
            ASSISTANT_MEDIA_CALL_NEW,
            count=1,
            label="assistant text media collector call",
        )
        source = _replace_exact(
            source,
            TOOL_MEDIA_CALL_OLD,
            TOOL_MEDIA_CALL_NEW,
            count=1,
            label="tool text media collector call",
        )
        source = _replace_exact(
            source,
            JSON_ADD_OLD,
            JSON_ADD_NEW,
            count=1,
            label="image JSON path",
        )
        source = _replace_exact(
            source,
            DOC_OLD,
            DOC_NEW,
            count=1,
            label="collector documentation",
        )
        changed = True
    if DELIVERY_MARKER not in source:
        source = _replace_exact(
            source,
            STREAMING_DELIVERY_OLD,
            STREAMING_DELIVERY_NEW,
            count=1,
            label="streaming explicit media delivery",
        )
        changed = True
    ast.parse(source)
    return source if changed else None


def patch_base_source(source: str) -> Optional[str]:
    if DELIVERY_MARKER in source:
        return None
    source = _replace_exact(
        source,
        BASE_DELIVERY_OLD,
        BASE_DELIVERY_NEW,
        count=1,
        label="non-streaming explicit media delivery",
    )
    ast.parse(source)
    return source


def _write_with_backup(target: Path, source: str, patched: str, suffix: str) -> None:
    backup = Path(str(target) + suffix)
    if backup.exists():
        if backup.read_text(encoding="utf-8") != source:
            raise RuntimeError(
                f"[media_same_path_resend] rollback backup does not match source: {target}"
            )
    else:
        shutil.copy2(target, backup)
    target.write_text(patched, encoding="utf-8")


def patch_media_same_path_resend_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    run_target = root / "gateway" / "run.py"
    base_target = root / "gateway" / "platforms" / "base.py"
    for target in (run_target, base_target):
        if not target.exists():
            raise RuntimeError(f"[media_same_path_resend] runtime target missing: {target}")

    run_source = run_target.read_text(encoding="utf-8")
    base_source = base_target.read_text(encoding="utf-8")
    run_patched = patch_source(run_source)
    base_patched = patch_base_source(base_source)
    if run_patched is None and base_patched is None:
        return False

    # Both sources are parsed and all anchors validated before either write.
    if run_patched is not None:
        _write_with_backup(run_target, run_source, run_patched, BACKUP_SUFFIX)
    if base_patched is not None:
        _write_with_backup(
            base_target,
            base_source,
            base_patched,
            BASE_BACKUP_SUFFIX,
        )
    return True
