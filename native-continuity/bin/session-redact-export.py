#!/usr/bin/env python3
"""Create a local, sanitized export of explicitly enabled native-agent sessions.

Reads a closed set of local agent session formats, redacts secrets, and writes
one clean Markdown file per session into a tenant-runtime staging directory.
Raw transcripts, reasoning, and full tool I/O never leave this machine.

Redaction level: SEARCHABLE-SAFE
  - keep user + assistant prose
  - drop model "thinking"/"reasoning" blocks
  - scrub secret-shaped strings -> [REDACTED:<kind>]
  - retain tool names but omit tool arguments and results

Sources:
  factory : ~/.factory/sessions/**/*.jsonl   (type=message, content blocks)
  codex   : ~/.codex/sessions/**/*.jsonl      (type=response_item payloads)
  claude  : ~/.claude/projects/**/*.jsonl     (type=user|assistant messages)

Idempotent: reuses receipt-verified outputs whose versioned source signature matches.
Fail-closed: an invalid source aborts publication and preserves the live generation.
Each complete generation carries a hash manifest consumed by the local tenant
GBrain projector. Codex is the only default source; every other parser requires
an explicit adapter selection.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path, PurePath

if os.name == "nt":
    import msvcrt
else:
    import fcntl

HOME = Path.home()
FACTORY = Path(
    os.environ.get(
        "SESSION_EXPORT_HOME",
        str(HOME / ".hermes" / "state" / "native-session-sync" / "exporter"),
    )
).expanduser()
EXPORT_ROOT = Path(
    os.environ.get("SESSION_EXPORT_ROOT", str(FACTORY / "exported"))
).expanduser()
STATE_FILE = FACTORY / "state" / "session-redact-export.json"
LEGACY_INVENTORY_FILE = FACTORY / "state" / "session-redact-export-legacy-aliases.json"
LEGACY_QUARANTINE_ROOT = FACTORY / "quarantine" / "session-redact-export"
LOG = FACTORY / "logs" / "session-redact-export.log"
LOCK_FILE = FACTORY / "state" / "session-redact-export.lock"

MAX_AGE_DAYS = min(
    max(int(os.environ.get("SESSION_EXPORT_MAX_AGE_DAYS", "120")), 1), 120
)
SUMMARY_LIMIT = 4000
HOST_TAG = os.environ.get("SESSION_EXPORT_HOST_TAG", "client").strip()
if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", HOST_TAG):
    raise RuntimeError("SESSION_EXPORT_HOST_TAG is invalid")
EXPORT_SCHEMA = "session-export/v4"
STATE_SCHEMA_KEY = "__export_schema__"
STATE_CONTRACT_KEY = "__state_contract__"
STATE_CONTRACT = "session-export-state/v1"
REDACTOR_VERSION_KEY = "__redactor_version__"
REDACTOR_VERSION = "session-redactor/v5"
IDENTITY_SCHEMA_KEY = "__identity_schema__"
IDENTITY_SCHEMA = "session-record-id/v1"
OUTPUTS_KEY = "outputs"
LEGACY_INVENTORY_SCHEMA = "session-export-legacy-aliases/v1"
RECEIPT_SCHEMA = "session-export-receipt/v1"
RECEIPT_NAME = ".session-export-complete.json"

ALL_SOURCES = {
    "factory": HOME / ".factory" / "sessions",
    "codex": HOME / ".codex" / "sessions",
    "claude": HOME / ".claude" / "projects",
}
_claude_root = os.environ.get("SESSION_EXPORT_CLAUDE_ROOT", "").strip()
if _claude_root:
    if "\x00" in _claude_root or len(_claude_root) > 4096:
        raise RuntimeError("SESSION_EXPORT_CLAUDE_ROOT is invalid")
    _claude_path = Path(_claude_root).expanduser()
    if not _claude_path.is_absolute():
        raise RuntimeError("SESSION_EXPORT_CLAUDE_ROOT must be absolute")
    ALL_SOURCES["claude"] = _claude_path
_source_filter = os.environ.get("SESSION_EXPORT_SOURCES", "").strip()
if _source_filter:
    _requested_sources = tuple(part.strip() for part in _source_filter.split(","))
    if (
        not _requested_sources
        or any(not part or part not in ALL_SOURCES for part in _requested_sources)
        or len(_requested_sources) != len(set(_requested_sources))
    ):
        raise RuntimeError("SESSION_EXPORT_SOURCES is invalid")
    SOURCES = {name: ALL_SOURCES[name] for name in _requested_sources}
else:
    SOURCES = {"codex": ALL_SOURCES["codex"]}
SOURCE_LABEL = {
    "factory": f"{HOST_TAG}-droid",
    "codex": f"{HOST_TAG}-codex",
    "claude": f"{HOST_TAG}-claude",
}
_excluded_originators = os.environ.get(
    "SESSION_EXPORT_EXCLUDE_CODEX_ORIGINATORS", ""
).strip()
EXCLUDED_CODEX_ORIGINATORS = frozenset(
    part.strip() for part in _excluded_originators.split(",") if part.strip()
)
if any(
    not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)
    for value in EXCLUDED_CODEX_ORIGINATORS
):
    raise RuntimeError("SESSION_EXPORT_EXCLUDE_CODEX_ORIGINATORS is invalid")

# --- secret scrubbing -------------------------------------------------------
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)"
        ),
    ),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_-]{25,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}")),
    ("assignment", re.compile(r"(?i)\b(api[_-]?key|private[_-]?key|access[_-]?key|auth[_-]?key|secret|token|password|passwd|client[_-]?secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=:-]{16,}[\"']?")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("highentropy", re.compile(r"(?<![-/])[A-Za-z0-9+/_-]{120,}={0,2}")),
]
ABSOLUTE_PATH_PREFIX = r"(?:[A-Za-z]:[\\/]|\\\\|/)"
URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
DELIMITED_ABSOLUTE_PATHS = (
    re.compile(rf'"{ABSOLUTE_PATH_PREFIX}(?:\\.|[^"\r\n])*"(?=\s|$|[,.;:!?])'),
    re.compile(rf"'{ABSOLUTE_PATH_PREFIX}(?:\\.|[^'\r\n])*'(?=\s|$|[,.;:!?])"),
    re.compile(rf"`{ABSOLUTE_PATH_PREFIX}(?:\\.|[^`\r\n])*`(?=\s|$|[,.;:!?])"),
    re.compile(rf"\({ABSOLUTE_PATH_PREFIX}(?:\\.|[^)\r\n])*\)(?=\s|$|[,.;:!?])"),
    re.compile(rf"\[{ABSOLUTE_PATH_PREFIX}(?:\\.|[^]\r\n])*\](?=\s|$|[,.;:!?])"),
)
UNQUOTED_ABSOLUTE_PATH = re.compile(rf"{ABSOLUTE_PATH_PREFIX}[^\r\n]*")


def scrub(text: str) -> str:
    if not text:
        return text
    for kind, pat in SECRET_PATTERNS:
        text = pat.sub(f"[REDACTED:{kind}]", text)
    text = URL.sub("[REDACTED:url]", text)
    for pattern in DELIMITED_ABSOLUTE_PATHS:
        text = pattern.sub("[REDACTED:path]", text)
    text = UNQUOTED_ABSOLUTE_PATH.sub("[REDACTED:path]", text)
    return text


# --- io helpers -------------------------------------------------------------
def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("export state is invalid")
    return value


def save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, encoding="utf-8", delete=False
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def acquire_run_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+b")
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return None
    else:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
    return handle


def release_run_lock(handle) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def record_id_for(source: str, path: Path) -> str:
    return hashlib.sha256(f"{source}\0{path}".encode()).hexdigest()


def legacy_short_for(path: Path) -> str:
    short = re.sub(r"[^a-f0-9]", "", path.stem.lower())[-8:] or path.stem[-8:]
    return short


def prior_id_for(source: str, filename: str) -> str:
    return hashlib.sha256(
        f"{SOURCE_LABEL[source]}:{filename}".encode()
    ).hexdigest()[:20]


def source_signature(path: Path) -> str:
    stat = path.stat()
    return f"{REDACTOR_VERSION}:{stat.st_size}:{stat.st_mtime_ns}"


def load_legacy_inventory() -> dict:
    if not LEGACY_INVENTORY_FILE.exists():
        return {
            "schema": LEGACY_INVENTORY_SCHEMA,
            "records": {},
            "placeholders": {},
        }
    value = json.loads(LEGACY_INVENTORY_FILE.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != LEGACY_INVENTORY_SCHEMA
        or not isinstance(value.get("records"), dict)
        or not isinstance(value.get("placeholders"), dict)
    ):
        raise ValueError("legacy alias inventory is invalid")
    for record_id, aliases in value["records"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", record_id) or not valid_aliases(aliases):
            raise ValueError("legacy alias inventory is invalid")
    for record_id, placeholder in value["placeholders"].items():
        if (
            not re.fullmatch(r"[0-9a-f]{64}", record_id)
            or not isinstance(placeholder, dict)
            or set(placeholder) != {"source", "date"}
            or placeholder["source"] not in SOURCES
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", placeholder["date"])
            or record_id not in value["records"]
        ):
            raise ValueError("legacy alias inventory is invalid")
    return value


def valid_aliases(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and len(value)
        == len(
            {
                (alias.get("date"), alias.get("record_id"))
                for alias in value
                if isinstance(alias, dict)
            }
        )
        and all(
            isinstance(alias, dict)
            and set(alias) == {"date", "record_id"}
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", alias["date"])
            and re.fullmatch(r"[0-9a-f]{20}", alias["record_id"])
            for alias in value
        )
    )


def save_legacy_inventory(inventory: dict) -> None:
    save_json(LEGACY_INVENTORY_FILE, inventory)


def inventory_legacy_generation(root: Path) -> dict:
    raw_paths = {
        source: sorted(source_root.glob("**/*.jsonl"))
        for source, source_root in SOURCES.items()
    }
    records: dict[str, list[dict[str, str]]] = {}
    placeholders: dict[str, dict[str, str]] = {}
    for source in SOURCES:
        prefix = re.compile(
            rf"^(\d{{4}}-\d{{2}}-\d{{2}})-{HOST_TAG}-{source}-.+\.md$"
        )
        for legacy_path in sorted((root / source).iterdir()):
            match = prefix.fullmatch(legacy_path.name)
            if not match:
                raise ValueError("legacy export filename is invalid")
            candidates = [
                path
                for path in raw_paths[source]
                if legacy_path.name.endswith(f"-{legacy_short_for(path)}.md")
            ]
            if len(candidates) > 1:
                raise ValueError("legacy export identity is ambiguous")
            if candidates:
                record_id = record_id_for(source, candidates[0])
            else:
                record_id = hashlib.sha256(
                    f"legacy-export\0{source}\0{legacy_path.name}".encode()
                ).hexdigest()
                placeholders[record_id] = {"source": source, "date": match.group(1)}
            records.setdefault(record_id, []).append(
                {
                    "date": match.group(1),
                    "record_id": prior_id_for(source, legacy_path.name),
                }
            )
    for aliases in records.values():
        aliases.sort(key=lambda item: (item["date"], item["record_id"]))
    return {
        "schema": LEGACY_INVENTORY_SCHEMA,
        "records": records,
        "placeholders": placeholders,
    }


def manifest_relative(path: PurePath, root: PurePath) -> str:
    return path.relative_to(root).as_posix()


def generation_receipt(root: Path, export_schema: str = EXPORT_SCHEMA) -> dict:
    files = {
        manifest_relative(path, root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*/*.md"))
    }
    core = {
        "schema": RECEIPT_SCHEMA,
        "export_schema": export_schema,
        "sources": sorted(SOURCES),
        "files": files,
    }
    generation_id = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**core, "generation_id": generation_id}


def verify_generation(root: Path) -> bool:
    try:
        receipt_path = root / RECEIPT_NAME
        if not receipt_path.is_file() or receipt_path.is_symlink():
            return False
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return False
        export_schema = receipt.get("export_schema")
        schema_match = (
            re.fullmatch(r"session-export/v(\d+)", export_schema)
            if isinstance(export_schema, str)
            else None
        )
        if not schema_match or int(schema_match.group(1)) < 4:
            return False
        expected = generation_receipt(root, export_schema)
        if receipt != expected:
            return False
        allowed = {root / name for name in SOURCES}
        for path in root.iterdir():
            if path == receipt_path:
                continue
            if path not in allowed or not path.is_dir() or path.is_symlink():
                return False
        if not all(path.is_dir() for path in allowed):
            return False
        return all(
            child.suffix == ".md" and child.is_file() and not child.is_symlink()
            for directory in allowed
            for child in directory.iterdir()
        )
    except Exception:
        return False


def verify_legacy_generation(root: Path) -> bool:
    try:
        if not root.is_dir() or root.is_symlink() or (root / RECEIPT_NAME).exists():
            return False
        expected = {root / source for source in SOURCES}
        if set(root.iterdir()) != expected:
            return False
        for source in SOURCES:
            directory = root / source
            if not directory.is_dir() or directory.is_symlink():
                return False
            name_pattern = re.compile(
                rf"^\d{{4}}-\d{{2}}-\d{{2}}-{HOST_TAG}-{source}-.+\.md$"
            )
            for child in directory.iterdir():
                if (
                    not name_pattern.fullmatch(child.name)
                    or not child.is_file()
                    or child.is_symlink()
                ):
                    return False
                schema = re.search(
                    r"^export_schema:\s*(session-export/v\d+)\s*$",
                    child.read_text(encoding="utf-8", errors="ignore"),
                    re.MULTILINE,
                )
                if schema and schema.group(1) not in {
                    "session-export/v2",
                    "session-export/v3",
                }:
                    return False
        return True
    except Exception:
        return False


def generation_kind(root: Path) -> str:
    if verify_generation(root):
        return "modern"
    if verify_legacy_generation(root):
        return "legacy"
    return "invalid"


def owner_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def quarantine_legacy_generation(path: Path) -> Path:
    owner_directory(LEGACY_QUARANTINE_ROOT)
    destination = LEGACY_QUARANTINE_ROOT / (
        datetime.utcnow().strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    )
    os.replace(path, destination)
    for child in destination.rglob("*"):
        child.chmod(0o700 if child.is_dir() else 0o600)
    destination.chmod(0o700)
    return destination


def recover_orphan_generation() -> str:
    backups = sorted(
        EXPORT_ROOT.parent.glob(f".{EXPORT_ROOT.name}.previous-*"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    if EXPORT_ROOT.exists():
        live_kind = generation_kind(EXPORT_ROOT)
        if live_kind == "invalid":
            raise RuntimeError("live export generation is invalid")
        for backup in backups:
            backup_kind = generation_kind(backup)
            if backup_kind == "modern":
                shutil.rmtree(backup)
            elif backup_kind == "legacy":
                quarantine_legacy_generation(backup)
            else:
                raise RuntimeError("orphan export backup is invalid")
        return live_kind
    classified = [(backup, generation_kind(backup)) for backup in backups]
    if any(kind == "invalid" for _, kind in classified):
        raise RuntimeError("orphan export backup is invalid")
    if not classified:
        return "absent"
    modern = [backup for backup, kind in classified if kind == "modern"]
    legacy = [backup for backup, kind in classified if kind == "legacy"]
    valid = modern or legacy
    if not valid:
        if backups:
            raise RuntimeError("orphan export backups are invalid")
        return "absent"
    recovered = valid[0]
    os.replace(recovered, EXPORT_ROOT)
    recovered_kind = generation_kind(EXPORT_ROOT)
    if recovered_kind == "invalid":
        raise RuntimeError("recovered export generation is invalid")
    for backup, kind in classified:
        if backup == recovered:
            continue
        if kind == "modern":
            shutil.rmtree(backup)
        else:
            quarantine_legacy_generation(backup)
    return recovered_kind


def publish_generation(staging: Path, live_kind: str) -> None:
    backup = EXPORT_ROOT.with_name(
        f".{EXPORT_ROOT.name}.previous-{os.getpid()}-{datetime.utcnow().timestamp():.6f}"
    )
    previous_moved = False
    try:
        if EXPORT_ROOT.exists():
            os.replace(EXPORT_ROOT, backup)
            backup.chmod(0o700)
            previous_moved = True
        os.replace(staging, EXPORT_ROOT)
    except Exception:
        if previous_moved and backup.exists() and not EXPORT_ROOT.exists():
            os.replace(backup, EXPORT_ROOT)
        raise
    if backup.exists():
        try:
            if live_kind == "legacy":
                quarantine_legacy_generation(backup)
            else:
                shutil.rmtree(backup)
        except OSError as exc:
            log(f"backup cleanup deferred {backup}: {exc}")


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or "session")[:limit].strip("-") or "session"


def clip(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = scrub(text.strip())
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + " [truncated]"


def render_tool_use(name: str, raw_args: object) -> str:
    del raw_args
    return f"`→ {scrub(str(name or 'tool'))}(arguments omitted)`"


def render_tool_result(content: object, is_error: bool = False) -> str | None:
    del content
    err = " (error)" if is_error else ""
    return f"`← tool result omitted{err}`"


# --- per-source parsers -----------------------------------------------------
def _render_block_list(content: list) -> str:
    out: list[str] = []
    for b in content:
        if isinstance(b, str):
            s = scrub(b.strip())
            if s:
                out.append(s)
            continue
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt in ("text", "input_text", "output_text"):
            s = scrub((b.get("text") or "").strip())
            if s:
                out.append(s)
        elif bt in ("thinking", "reasoning"):
            continue
        elif bt == "tool_use":
            out.append(render_tool_use(b.get("name"), b.get("input")))
        elif bt == "tool_result":
            r = render_tool_result(b.get("content"), bool(b.get("is_error")))
            if r:
                out.append(r)
    return "\n\n".join(out).strip()


def parse_factory(path: Path) -> tuple[list[dict], dict]:
    meta = {"session_id": path.stem, "cwd": "", "title": ""}
    messages: list[dict] = []
    started = ended = ""
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid session JSON in {path}") from exc
        ts = e.get("timestamp")
        if isinstance(ts, str):
            started = started or ts
            ended = ts
        et = e.get("type")
        if et == "session_start":
            meta = {
                "session_id": str(e.get("id", path.stem)),
                "cwd": str(e.get("cwd", "")),
                "title": str(e.get("title", "") or e.get("sessionTitle", "")),
            }
            continue
        if et != "message":
            continue
        m = e.get("message")
        if not isinstance(m, dict) or m.get("role") not in {"user", "assistant"}:
            continue
        content = m.get("content", "")
        text = _render_block_list(content) if isinstance(content, list) else scrub(str(content).strip())
        if text:
            messages.append({"role": m["role"], "text": text})
    meta["started_at"], meta["ended_at"] = started, ended
    return messages, meta


def parse_codex(path: Path) -> tuple[list[dict], dict]:
    meta = {"session_id": path.stem, "cwd": "", "title": "", "originator": ""}
    messages: list[dict] = []
    started = ended = ""
    pending_calls: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid session JSON in {path}") from exc
        ts = e.get("timestamp")
        if isinstance(ts, str):
            started = started or ts
            ended = ts
        et = e.get("type")
        if et == "session_meta":
            p = e.get("payload", {})
            meta = {
                "session_id": str(p.get("id", path.stem)),
                "cwd": str(p.get("cwd", "")),
                "title": str(p.get("instructions", "") or "")[:80],
                "originator": str(p.get("originator", "") or ""),
            }
            continue
        if et != "response_item":
            continue
        p = e.get("payload", {})
        pt = p.get("type")
        if pt == "message":
            role = p.get("role")
            if role not in {"user", "assistant"}:  # skip developer/system
                continue
            content = p.get("content", [])
            text = _render_block_list(content) if isinstance(content, list) else scrub(str(content).strip())
            if text:
                messages.append({"role": role, "text": text})
        elif pt == "function_call":
            messages.append({"role": "assistant", "text": render_tool_use(p.get("name"), p.get("arguments"))})
        elif pt == "function_call_output":
            r = render_tool_result(p.get("output"))
            if r:
                messages.append({"role": "user", "text": r})
        elif pt == "reasoning":
            continue
    meta["started_at"], meta["ended_at"] = started, ended
    return messages, meta


def parse_claude(path: Path) -> tuple[list[dict], dict]:
    meta = {"session_id": path.stem, "cwd": "", "title": ""}
    messages: list[dict] = []
    started = ended = ""
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid session JSON in {path}") from exc
        ts = e.get("timestamp")
        if isinstance(ts, str):
            started = started or ts
            ended = ts
        if not meta.get("cwd") and e.get("cwd"):
            meta["cwd"] = str(e.get("cwd"))
        et = e.get("type")
        if et not in {"user", "assistant"}:
            continue
        m = e.get("message")
        if not isinstance(m, dict):
            continue
        role = m.get("role") or et
        if role not in {"user", "assistant"}:
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            text = _render_block_list(content)
        else:
            text = scrub(str(content).strip())
        # skip injected local-command caveats / meta noise
        if text.startswith("<local-command") or text.startswith("<command-"):
            continue
        if text:
            messages.append({"role": role, "text": text})
    meta["started_at"], meta["ended_at"] = started, ended
    return messages, meta


PARSERS = {"factory": parse_factory, "codex": parse_codex, "claude": parse_claude}


# --- note builder -----------------------------------------------------------
def build_note(
    source: str,
    path: Path,
    messages: list[dict],
    meta: dict,
    prior_records: list[dict[str, str]] | None = None,
) -> tuple[str, str, str]:
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime)
    date_str = modified.strftime("%Y-%m-%d")
    time_str = modified.strftime("%H:%M:%S")
    users = [m["text"] for m in messages if m["role"] == "user"]
    assts = [m["text"] for m in messages if m["role"] == "assistant"]
    first_user = users[0] if users else ""
    raw_title = str(
        meta.get("title") or (first_user.splitlines()[0] if first_user else "session")
    )
    title = scrub(raw_title)
    session_id = scrub(str(meta.get("session_id") or ""))
    label = SOURCE_LABEL[source]
    record_id = record_id_for(source, path)
    filename = f"{slugify(title)}-{record_id}.md"

    primary_request = clip(users[0]) if users else "Not available."
    final_reply = clip(assts[-1]) if assts else "Not available."

    note = f"""---
type: session
export_schema: {EXPORT_SCHEMA}
identity_schema: {IDENTITY_SCHEMA}
source: {label}
owner: {HOST_TAG}
agent_source: {source}
record_id: {record_id}
prior_records: {json.dumps(prior_records or [])}
date: {date_str}
time: {time_str}
session_id: {json.dumps(session_id, ensure_ascii=False)}
title: {json.dumps(title, ensure_ascii=False)}
user_turns: {len(users)}
assistant_turns: {len(assts)}
tags:
  - {source}
  - {HOST_TAG}
  - session-sync
---

# {source.title()} Session ({HOST_TAG}) {date_str} {time_str}

## Title

{scrub(title)}

## Primary Request

{primary_request}

## Final Assistant Reply

{final_reply}
"""
    signature = source_signature(path)
    return filename, note, signature


def build_placeholder_note(
    source: str, record_id: str, date_str: str, prior_records: list[dict[str, str]]
) -> tuple[str, str]:
    filename = f"retired-session-{record_id}.md"
    note = f"""---
type: session
export_schema: {EXPORT_SCHEMA}
identity_schema: {IDENTITY_SCHEMA}
source: {SOURCE_LABEL[source]}
owner: {HOST_TAG}
agent_source: {source}
record_id: {record_id}
prior_records: {json.dumps(prior_records)}
date: {date_str}
time: 00:00:00
session_id: ""
title: "Retired session continuity record"
user_turns: 0
assistant_turns: 0
tags:
  - {source}
  - {HOST_TAG}
  - session-sync
---

# Retired Session Continuity Record

## Primary Request

Not available in the retained source set.

## Final Assistant Reply

The legacy projector record was retired during the sanitized schema migration.
"""
    return filename, note


def reusable_outputs(state: dict, live_kind: str) -> dict:
    if (
        live_kind != "modern"
        or state.get(STATE_CONTRACT_KEY) != STATE_CONTRACT
        or state.get(STATE_SCHEMA_KEY) != EXPORT_SCHEMA
        or state.get(REDACTOR_VERSION_KEY) != REDACTOR_VERSION
        or state.get(IDENTITY_SCHEMA_KEY) != IDENTITY_SCHEMA
        or not isinstance(state.get(OUTPUTS_KEY), dict)
    ):
        return {}
    outputs = state[OUTPUTS_KEY]
    for key, entry in outputs.items():
        if (
            not isinstance(key, str)
            or not isinstance(entry, dict)
            or set(entry) != {"signature", "relative_path", "sha256"}
            or not isinstance(entry["signature"], str)
            or (
                entry["relative_path"] is not None
                and not isinstance(entry["relative_path"], str)
            )
            or (
                entry["sha256"] is not None
                and not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            )
            or ((entry["relative_path"] is None) != (entry["sha256"] is None))
        ):
            return {}
    return outputs


def reuse_output(
    source: str,
    key: str,
    signature: str,
    entry: object,
    receipt_files: dict[str, str],
    staging: Path,
) -> dict | None:
    if not isinstance(entry, dict) or entry.get("signature") != signature:
        return None
    relative = entry.get("relative_path")
    digest = entry.get("sha256")
    if relative is None and digest is None:
        return dict(entry)
    if not isinstance(relative, str) or not isinstance(digest, str):
        return None
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or relative_path.parts[:1] != (source,)
        or ".." in relative_path.parts
        or receipt_files.get(relative) != digest
    ):
        return None
    existing = EXPORT_ROOT / relative_path
    if (
        not existing.is_file()
        or existing.is_symlink()
        or hashlib.sha256(existing.read_bytes()).hexdigest() != digest
    ):
        return None
    destination = staging / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(existing, destination)
    return dict(entry)


def main() -> int:
    EXPORT_ROOT.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_run_lock()
    if lock is None:
        print("export already running; skipping")
        return 0
    staging: Path | None = None
    try:
        live_kind = recover_orphan_generation()
        missing = [source for source, root in SOURCES.items() if not root.is_dir()]
        if missing:
            raise RuntimeError(
                f"missing configured raw source roots: {', '.join(sorted(missing))}"
            )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{EXPORT_ROOT.name}.staging-", dir=EXPORT_ROOT.parent
            )
        )
        previous_state = load_state()
        inventory = load_legacy_inventory()
        if live_kind == "legacy":
            discovered_inventory = inventory_legacy_generation(EXPORT_ROOT)
            if inventory["records"] and inventory != discovered_inventory:
                raise RuntimeError("legacy alias inventory changed")
            inventory = discovered_inventory
            save_legacy_inventory(inventory)
        previous_outputs = reusable_outputs(previous_state, live_kind)
        receipt_files: dict[str, str] = {}
        if live_kind == "modern":
            receipt = json.loads(
                (EXPORT_ROOT / RECEIPT_NAME).read_text(encoding="utf-8")
            )
            receipt_files = receipt["files"]
        state = {
            STATE_CONTRACT_KEY: STATE_CONTRACT,
            STATE_SCHEMA_KEY: EXPORT_SCHEMA,
            REDACTOR_VERSION_KEY: REDACTOR_VERSION,
            IDENTITY_SCHEMA_KEY: IDENTITY_SCHEMA,
            OUTPUTS_KEY: {},
        }
        outputs = state[OUTPUTS_KEY]
        cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
        totals: dict[str, dict] = {}
        for source, root in SOURCES.items():
            exported = skipped = empty = suppressed = 0
            out_dir = staging / source
            out_dir.mkdir(parents=True, exist_ok=True)
            parser = PARSERS[source]
            for path in sorted(root.glob("**/*.jsonl")):
                record_id = record_id_for(source, path)
                aliases = inventory["records"].get(record_id, [])
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff and not aliases:
                    continue
                key = f"{source}:{path}"
                sig_now = source_signature(path)
                reused = reuse_output(
                    source,
                    key,
                    sig_now,
                    previous_outputs.get(key),
                    receipt_files,
                    staging,
                )
                if reused is not None:
                    outputs[key] = reused
                    skipped += 1
                    continue
                messages, meta = parser(path)
                if (
                    source == "codex"
                    and meta.get("originator") in EXCLUDED_CODEX_ORIGINATORS
                ):
                    suppressed += 1
                    outputs[key] = {
                        "signature": sig_now,
                        "relative_path": None,
                        "sha256": None,
                    }
                    continue
                if not messages:
                    if aliases:
                        filename, note = build_placeholder_note(
                            source, record_id, aliases[0]["date"], aliases
                        )
                        destination = out_dir / filename
                        destination.write_text(note, encoding="utf-8")
                        relative = manifest_relative(destination, staging)
                        outputs[key] = {
                            "signature": sig_now,
                            "relative_path": relative,
                            "sha256": hashlib.sha256(
                                destination.read_bytes()
                            ).hexdigest(),
                        }
                        exported += 1
                        continue
                    empty += 1
                    outputs[key] = {
                        "signature": sig_now,
                        "relative_path": None,
                        "sha256": None,
                    }
                    continue
                filename, note, signature = build_note(
                    source, path, messages, meta, aliases
                )
                destination = out_dir / filename
                destination.write_text(note, encoding="utf-8")
                relative = manifest_relative(destination, staging)
                outputs[key] = {
                    "signature": signature,
                    "relative_path": relative,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                }
                exported += 1
            for record_id, placeholder in inventory["placeholders"].items():
                if placeholder["source"] != source:
                    continue
                filename, note = build_placeholder_note(
                    source,
                    record_id,
                    placeholder["date"],
                    inventory["records"][record_id],
                )
                (out_dir / filename).write_text(note, encoding="utf-8")
                exported += 1
            totals[source] = {
                "exported": exported,
                "skipped": skipped,
                "empty": empty,
                "suppressed": suppressed,
            }
        receipt = generation_receipt(staging)
        (staging / RECEIPT_NAME).write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not verify_generation(staging):
            raise RuntimeError("staged export receipt verification failed")
        save_state(state)
        publish_generation(staging, live_kind)
        try:
            print(json.dumps(totals))
            log(f"done {totals}")
        except OSError:
            pass
        return 0
    except Exception as exc:
        log(f"FAILED export generation: {exc}")
        return 1
    finally:
        if staging is not None and staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                pass
        release_run_lock(lock)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-receipt":
        raise SystemExit(0 if verify_generation(Path(sys.argv[2])) else 1)
    raise SystemExit(main())
