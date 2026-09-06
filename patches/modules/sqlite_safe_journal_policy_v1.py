#!/usr/bin/env python3
"""Keep managed Linux SQLite writers compatible with rollback journaling."""

from __future__ import annotations

import ast
import io
import re
import shutil
import time
import tokenize
from pathlib import Path
from typing import Union

MARKER = "HERMES_SQLITE_SAFE_JOURNAL_POLICY_v1"
PATCH_NOT_APPLICABLE = "not-applicable"
FORCED_WAL = re.compile(r"PRAGMA\s+journal_mode\s*=\s*['\"]?WAL\b", re.I)
TARGETS = (
    "hermes_state.py",
    "hermes_state_wal.py",
    "agent/verification_evidence.py",
    "gateway/media_provenance.py",
    "gateway/plugin_storage.py",
    "gateway/telegram_transaction_ledger.py",
    "plugins/plugin_storage.py",
    "plugins/telegram-platform/media_provenance.py",
    "tools/async_delegation.py",
)


class PatchError(RuntimeError):
    pass


# Upstream d3630f853239e8c41ce7201e09fbdf39bcbc5431, hermes_state_dbfile.py.
# Retire this legacy backport when the baseline uses that native split owner.
LEGACY_HEADER_PROBE = r'''_HEADER_PROBE_LOCK = threading.Lock()
_HEADER_PROBE_FDS: "dict[str, tuple[int, int, int]]" = {}  # key -> (fd, dev, ino)
_RETIRED_HEADER_PROBE_FDS: "list[int]" = []  # intentionally never closed


def _pread_db_header(db_path: Path, length: int) -> "Optional[bytes]":
    """Lock-safe raw header read of a possibly-live SQLite database: POSIX preads from a cached,
    never-closed fd (rebound when the path names a new inode); Windows reads plainly, since
    advisory-lock cancellation is a POSIX-only hazard."""
    if _IS_WINDOWS:
        with contextlib.suppress(OSError), db_path.open("rb") as handle:
            return handle.read(length)
        return None
    key = str(db_path)
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    with _HEADER_PROBE_LOCK:
        cached = _HEADER_PROBE_FDS.get(key)
        if cached is not None and (cached[1], cached[2]) != (st.st_dev, st.st_ino):
            # Path re-pointed at a new file. Retire (never close) the old fd.
            _RETIRED_HEADER_PROBE_FDS.append(_HEADER_PROBE_FDS.pop(key)[0])
            cached = None
        if cached is None:
            try:
                fd = os.open(db_path, os.O_RDONLY)
            except OSError:
                return None
            try:
                fst = os.fstat(fd)
            except OSError:
                _RETIRED_HEADER_PROBE_FDS.append(fd)
                return None
            cached = _HEADER_PROBE_FDS[key] = (fd, fst.st_dev, fst.st_ino)
        with contextlib.suppress(OSError):
            return os.pread(cached[0], length, 0)
    return None


def _read_sqlite_application_id(db_path: Path) -> "Optional[int]":
    """application_id from the SQLite header, via the lock-safe :func:`_pread_db_header`."""
    end = _STATE_DB_APPLICATION_ID_OFFSET + 4
    header = _pread_db_header(db_path, end)
    if header is None or len(header) < end or header[:16] != b"SQLite format 3\x00":
        return None
    return int(struct.unpack(">I", header[_STATE_DB_APPLICATION_ID_OFFSET:end])[0])

'''


def patch_legacy_header_probe(source: str) -> str:
    """Backport the native d3630f8 lock-safe header reader; native owns its copy."""
    if "def _read_sqlite_application_id(" not in source or "def _pread_db_header(" in source:
        return source
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                and item.name == "_read_sqlite_application_id")
    lines = source.splitlines(keepends=True)
    original = "".join(lines[node.lineno - 1:node.end_lineno])
    if original.count('with db_path.open("rb") as handle:') != 1:
        raise PatchError("legacy SQLite header reader anchor drift")
    replacement = LEGACY_HEADER_PROBE.rstrip() + "\n"
    return "".join(lines[:node.lineno - 1]) + replacement + "".join(lines[node.end_lineno:])

def patch_sqlite_writer_source(source: str) -> str:
    """Make literal WAL setters read-only on Linux and retain other platforms."""
    source = patch_legacy_header_probe(source)
    if MARKER in source:
        if FORCED_WAL.search(source):
            raise PatchError("marked SQLite writer still contains a forced-WAL pragma")
        # Upgrade the canonical legacy read-only policy without changing modes.
        previous = '        if current_mode in ("delete", "memory"):\n            if require_wal:\n'
        if previous in source:
            if source.count(previous) != 1:
                raise PatchError("legacy journal preservation anchor drift")
            source = source.replace(previous, previous.replace('"memory")', '"memory", "persist", "truncate")'), 1)
        return source
    if not FORCED_WAL.search(source):
        return source

    journal_expression = (
        '("PRAGMA journal_mode" if __import__("sys").platform.startswith("linux") '
        'else "PRAGMA journal_mode=" + "WAL")'
    )
    working = source
    if "def _enable_wal(" in working and "def _apply_wal_companions(" in working:
        anchor = "    current_mode = _on_disk_journal_mode(conn)\n    if current_mode == \"wal\":\n"
        if working.count(anchor) != 1:
            raise PatchError("native WAL journal admission anchor drift")
        replacement = """    current_mode = _on_disk_journal_mode(conn)
    # HERMES_SQLITE_SAFE_JOURNAL_POLICY_v1: retain native vulnerable-SQLite
    # handling above; fixed Linux builds also need stopped ownership to change modes.
    if sys.platform.startswith("linux") and current_mode != "wal":
        if current_mode not in ("delete", "memory", "persist", "truncate"):
            raise sqlite3.OperationalError("cannot verify Linux journal mode; refusing startup transition")
        if require_wal:
            raise WalUnsupportedError(f"WAL required but Linux policy preserved {current_mode}")
        return current_mode
    if current_mode == "wal":
"""
        working = working.replace(anchor, replacement, 1)
    elif "def apply_wal_with_fallback(" in working:
        anchor = "    configured = resolve_journal_mode()\n"
        if working.count(anchor) != 1:
            raise PatchError(
                f"apply_wal_with_fallback configured-mode anchor count={working.count(anchor)}"
            )
        working = working.replace(
            anchor,
            anchor
            + "\n"
            + "    # [HERMES_SQLITE_SAFE_JOURNAL_POLICY_v1] Linux promotion must\n"
            + "    # preserve the on-disk mode until stopped maintenance owns it.\n"
            + "    if __import__(\"sys\").platform.startswith(\"linux\"):\n"
            + "        current_mode = _on_disk_journal_mode(conn)\n"
            + "        if current_mode == \"wal\":\n"
            + "            _apply_wal_size_limit(conn)\n"
            + "            _apply_macos_checkpoint_barrier(conn)\n"
            + "            _enforce_macos_synchronous_full(conn)\n"
            + "            return current_mode\n"
            + "        if current_mode in (\"delete\", \"memory\", \"persist\", \"truncate\"):\n"
            + "            if require_wal:\n"
            + "                raise WalUnsupportedError(\n"
            + "                    f\"WAL required but Linux policy preserved {current_mode}\"\n"
            + "                )\n"
            + "            return current_mode\n"
            + "        raise sqlite3.OperationalError(\n"
            + "            \"could not verify existing Linux journal mode; refusing startup transition\"\n"
            + "        )\n",
            1,
        )

    parsed = ast.parse(working)
    docstring_positions = set()
    for node in ast.walk(parsed):
        body = getattr(node, "body", None)
        if not body or not isinstance(body, list):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_positions.add((first.value.lineno, first.value.col_offset))

    # Comments and true docstrings must not trip the runtime's fail-closed
    # literal scanner. Any other spelling in executable source is unsupported
    # and fails before the caller writes a file.
    tokens = []
    for token in tokenize.generate_tokens(io.StringIO(working).readline):
        if FORCED_WAL.search(token.string):
            if token.type == tokenize.COMMENT or (
                token.type == tokenize.STRING and token.start in docstring_positions
            ):
                token = token._replace(
                    string=FORCED_WAL.sub("PRAGMA journal mode WAL", token.string)
                )
            elif token.type == tokenize.STRING and token.string in {
                '"PRAGMA journal_mode=WAL"',
                "'PRAGMA journal_mode=WAL'",
            }:
                token = token._replace(string=journal_expression)
            else:
                raise PatchError(
                    f"unsupported forced-WAL source form at {token.start[0]}:{token.start[1]}"
                )
        tokens.append(token)
    patched = tokenize.untokenize(tokens)
    patched = patched.rstrip() + f"\n\n# [{MARKER}]\n"
    ast.parse(patched)
    if FORCED_WAL.search(patched):
        raise PatchError("forced-WAL pragma remained after patch")
    return patched


def patch_sqlite_safe_journal_policy_v1(hermes_dir: Path) -> Union[bool, str]:
    marked = False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    planned = []
    for relative in TARGETS:
        path = hermes_dir / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        marked = marked or MARKER in original
        patched = patch_sqlite_writer_source(original)
        if patched == original:
            continue
        backup = path.with_suffix(
            path.suffix + f".bak-{stamp}-sqlite-safe-journal-policy-v1"
        )
        planned.append((path, patched, backup))
    if not planned:
        return False if marked else PATCH_NOT_APPLICABLE

    # Transform every target before the first backup or source write. A later
    # unfamiliar WAL form must leave the runtime completely untouched.
    for path, _patched, backup in planned:
        shutil.copy2(path, backup)

    attempted = []
    try:
        for path, patched, backup in planned:
            attempted.append((path, backup))
            path.write_text(patched, encoding="utf-8")
    except Exception as exc:
        rollback_failures = []
        for path, backup in reversed(attempted):
            try:
                shutil.copy2(backup, path)
            except Exception as rollback_exc:
                rollback_failures.append(f"{path}:{rollback_exc}")
        if rollback_failures:
            raise PatchError(
                f"SQLite writer patch failed ({exc}); rollback failed: "
                + "; ".join(rollback_failures)
            ) from exc
        raise PatchError(f"SQLite writer patch failed and was rolled back: {exc}") from exc
    return True
