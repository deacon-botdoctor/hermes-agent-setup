#!/usr/bin/env python3
"""Sanitize one principal's native sessions and project exact notes to GBrain."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PRINCIPAL = os.environ.get("NATIVE_SESSION_PRINCIPAL", "client").strip().lower()
if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", PRINCIPAL):
    raise RuntimeError("native-session principal is invalid")
SCHEMA = os.environ.get("NATIVE_SESSION_SYNC_SCHEMA", "native-session-sync/v1")
STATE_SCHEMA = os.environ.get(
    "NATIVE_SESSION_STATE_SCHEMA", "native-session-sync-state/v1"
)
EXPORT_RECEIPT_SCHEMA = "session-export-receipt/v1"
ACTIVE_RUNTIME = Path(
    os.environ.get(
        "NATIVE_SESSION_RUNTIME",
        str(Path.home() / ".hermes"),
    )
)
SYNC_ROOT = Path(
    os.environ.get("NATIVE_SESSION_SYNC_ROOT", str(ACTIVE_RUNTIME / "state" / "native-session-sync"))
)
BRAIN_ROOT = Path(
    os.environ.get(
        "NATIVE_SESSION_EXPORT_ROOT",
        str(ACTIVE_RUNTIME / "state" / "native-session-sync" / "exported"),
    )
)
EXPORTER_HOME = SYNC_ROOT / "exporter"
EXPORTER = Path(
    os.environ.get(
        "NATIVE_SESSION_EXPORTER", str(ACTIVE_RUNTIME / "bin" / "session-redact-export.py")
    )
)
GBRAIN = Path(
    os.environ.get(
        "NATIVE_SESSION_GBRAIN",
        str(ACTIVE_RUNTIME / "bin" / ("gbrain.exe" if os.name == "nt" else "gbrain")),
    )
)
VAULT = Path(os.environ.get("NATIVE_SESSION_VAULT", str(ACTIVE_RUNTIME / "workspace" / "Brain")))
STATE_FILE = SYNC_ROOT / "projector-state.json"
RUN_LOCK = SYNC_ROOT / "projector-run.lock"
GBRAIN_LOCK = SYNC_ROOT / "gbrain-process.lock"
RECEIPT_ROOT = SYNC_ROOT / "receipts"
LUNA_CARDER = Path(
    os.environ.get(
        "NATIVE_SESSION_LUNA_CARDER",
        str(ACTIVE_RUNTIME / "bin" / "native-session-luna-carder.py"),
    )
)
LUNA_ENABLE_MARKER = Path(
    os.environ.get(
        "NATIVE_SESSION_LUNA_ENABLE_MARKER",
        str(ACTIVE_RUNTIME / "state" / "native-session-luna-carder" / "enabled.json"),
    )
)
SOURCE_KINDS = {"claude": "claude-code", "codex": "codex"}
SOURCES = tuple(
    item.strip().lower()
    for item in os.environ.get("NATIVE_SESSION_SOURCES", "codex").split(",")
    if item.strip()
)
if not SOURCES or any(item not in SOURCE_KINDS for item in SOURCES) or len(set(SOURCES)) != len(SOURCES):
    raise RuntimeError("native-session sources are invalid")
SOURCE_LABELS = {source: f"{PRINCIPAL}-{source}" for source in SOURCE_KINDS}
HOST_TAG = os.environ.get("NATIVE_SESSION_HOST_TAG", PRINCIPAL).strip() or PRINCIPAL
CLAUDE_ROOT = os.environ.get(
    "NATIVE_SESSION_CLAUDE_ROOT", str(Path.home() / ".claude" / "projects")
)
RUN_LIMIT = 100
MAX_PAGE_CHARS = 12_000
LOCK_TIMEOUT_SECONDS = 90
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{25,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
ABSOLUTE_PATH = re.compile(r"(?m)(?:[A-Za-z]:[\\/]|/Users/|/home/|\\\\)[^\r\n`\"']+")


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(
        path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"required regular file is missing or unsafe: {path}")
    return path.read_bytes()


def sanitize(value: str, limit: int = 4_000) -> str:
    value = value.replace("\x00", "")
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED:secret]", value)
    value = URL.sub("[REDACTED:url]", value)
    value = ABSOLUTE_PATH.sub("[REDACTED:path]", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value[:limit] + (" [truncated]" if len(value) > limit else "")


def run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
        cwd=cwd,
    )


@contextlib.contextmanager
def exclusive_lock(path: Path, *, blocking: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"lock path is unsafe: {path}")
    handle = path.open("a+b")
    acquired = False
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while not acquired:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if not blocking:
                        yield False
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError("lock acquisition timed out")
                    time.sleep(0.1)
        else:
            while not acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if not blocking:
                        yield False
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError("lock acquisition timed out")
                    time.sleep(0.1)
        yield True
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---\n"):
        raise RuntimeError("sanitized note frontmatter is missing")
    end = raw.find("\n---\n", 4)
    if end < 0:
        raise RuntimeError("sanitized note frontmatter is invalid")
    fields: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        if line.startswith((" ", "-")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip("\"'")
    return fields, raw[end + 5 :]


def section(body: str, name: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(name)}\s*$\n(.*?)(?=^##\s+|\Z)",
        body,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    return sanitize(match.group(1)) if match else ""


def render_note(source: str, path: Path) -> dict[str, str]:
    raw_bytes = read_regular(path)
    raw = raw_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    fields, body = split_frontmatter(raw)
    expected = {
        "type": "session",
        "export_schema": "session-export/v4",
        "identity_schema": "session-record-id/v1",
        "owner": PRINCIPAL,
        "source": SOURCE_LABELS[source],
        "agent_source": source,
    }
    if any(fields.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"sanitized {source} note metadata is invalid")
    record_id = fields.get("record_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", record_id):
        raise RuntimeError("sanitized note record ID is invalid")
    kind = SOURCE_KINDS[source]
    title = section(body, "Title") or "Session continuity record"
    primary = section(body, "Primary Request") or "Not available in source record."
    final = section(body, "Final Assistant Reply") or "Not available in source record."
    signature_hash = sha256_bytes(raw_bytes)
    signature = f"session-export/v4:{signature_hash}"
    slug = f"session/{kind}/{record_id}-{signature_hash[:16]}"
    content = (
        "---\n"
        "type: session\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"source: {PRINCIPAL}-native-session-sync\n"
        f"session_kind: {kind}\n"
        f"session_date: {fields.get('date', '')}\n"
        f"source_record_id: {record_id}\n"
        f"source_signature: {signature}\n"
        "---\n\n"
        f"# {title}\n\n## Primary request\n\n{primary}\n\n"
        f"## Final assistant update\n\n{final}\n\n"
        f"## Retrieval proof\n\n{signature_hash}\n"
    )[:MAX_PAGE_CHARS]
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise RuntimeError("projected page contains a secret-shaped value")
    if URL.search(content) or ABSOLUTE_PATH.search(content):
        raise RuntimeError("projected page contains unsafe source detail")
    return {
        "source_key": f"{kind}:{record_id}:{signature_hash}",
        "source_signature": signature,
        "proof": signature_hash,
        "slug": slug,
        "content": content,
    }


def export_environment() -> dict[str, str]:
    environment = os.environ.copy()
    values = {
        "SESSION_EXPORT_HOME": str(EXPORTER_HOME),
        "SESSION_EXPORT_ROOT": str(BRAIN_ROOT),
        "SESSION_EXPORT_HOST_TAG": HOST_TAG,
        "SESSION_EXPORT_SOURCES": ",".join(SOURCES),
        "SESSION_EXPORT_EXCLUDE_CODEX_ORIGINATORS": "codex_exec",
        "SESSION_EXPORT_MAX_AGE_DAYS": "120",
    }
    if "claude" in SOURCES:
        values["SESSION_EXPORT_CLAUDE_ROOT"] = CLAUDE_ROOT
    else:
        environment.pop("SESSION_EXPORT_CLAUDE_ROOT", None)
    environment.update(values)
    return environment


def run_exporter() -> dict[str, Any]:
    read_regular(EXPORTER)
    result = run([sys.executable, str(EXPORTER)], env=export_environment(), timeout=600)
    if result.returncode != 0:
        raise RuntimeError("native-session sanitizer failed")
    try:
        totals = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("native-session sanitizer output is invalid") from exc
    if not isinstance(totals, dict) or set(totals) != set(SOURCES):
        raise RuntimeError("native-session sanitizer source totals are invalid")
    return totals


def verify_generation() -> list[tuple[str, Path]]:
    receipt_path = BRAIN_ROOT / ".session-export-complete.json"
    receipt = json.loads(read_regular(receipt_path).decode("utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != EXPORT_RECEIPT_SCHEMA
        or receipt.get("export_schema") != "session-export/v4"
        or receipt.get("sources") != sorted(SOURCES)
        or not isinstance(receipt.get("files"), dict)
    ):
        raise RuntimeError("sanitized generation receipt is invalid")
    files = receipt["files"]
    core = {
        "schema": EXPORT_RECEIPT_SCHEMA,
        "export_schema": "session-export/v4",
        "sources": sorted(SOURCES),
        "files": files,
    }
    expected_generation = sha256_bytes(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    )
    if receipt.get("generation_id") != expected_generation:
        raise RuntimeError("sanitized generation ID is invalid")
    allowed = {*SOURCES, ".session-export-complete.json"}
    if {path.name for path in BRAIN_ROOT.iterdir()} != allowed:
        raise RuntimeError("sanitized generation contains an unexpected entry")
    notes: list[tuple[str, Path]] = []
    observed: dict[str, str] = {}
    for source in SOURCES:
        root = BRAIN_ROOT / source
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("sanitized source directory is unsafe")
        for path in sorted(root.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".md":
                raise RuntimeError("sanitized source entry is unsafe")
            relative = path.relative_to(BRAIN_ROOT).as_posix()
            observed[relative] = sha256_bytes(path.read_bytes())
            notes.append((source, path))
    if observed != files:
        raise RuntimeError("sanitized generation manifest does not match files")
    return notes


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"schema": STATE_SCHEMA, "sources": {}}
    value = json.loads(read_regular(STATE_FILE).decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != STATE_SCHEMA
        or not isinstance(value.get("sources"), dict)
    ):
        raise RuntimeError("native-session projector state is invalid")
    return value


def is_exact_not_found(result: subprocess.CompletedProcess[str], slug: str) -> bool:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return (
        result.returncode == 1
        and output.startswith("Error [page_not_found]: Page not found: ")
        and f"Page not found: {slug}" in output
    )


def get_page(slug: str) -> tuple[bool, str]:
    result = run([str(GBRAIN), "get", slug], timeout=90, cwd=VAULT)
    if result.returncode == 0:
        return True, result.stdout
    if is_exact_not_found(result, slug):
        return False, ""
    raise RuntimeError(f"GBrain read failed for {slug}")


def commit_page(slug: str, action: str) -> str:
    git = shutil.which("git")
    if (
        not git
        or VAULT.is_symlink()
        or not VAULT.is_dir()
        or (VAULT / ".git").is_symlink()
        or not (VAULT / ".git").is_dir()
    ):
        raise RuntimeError("tenant vault Git boundary is unavailable or unsafe")
    relative = f"{slug}.md"
    root = run([git, "rev-parse", "--show-toplevel"], cwd=VAULT, timeout=30)
    if root.returncode or Path(root.stdout.strip()).resolve() != VAULT.resolve():
        raise RuntimeError("tenant vault is not the exact Git worktree root")
    staged = run([git, "add", "-A", "--", relative], cwd=VAULT, timeout=30)
    changed = run([git, "diff", "--cached", "--quiet", "--", relative], cwd=VAULT, timeout=30)
    if staged.returncode or changed.returncode not in (0, 1):
        raise RuntimeError("tenant session page staging failed")
    if changed.returncode == 1:
        committed = run([git, "commit", "-m", f"GBrain session {action}"], cwd=VAULT, timeout=60)
        if committed.returncode:
            raise RuntimeError("tenant session page commit failed")
    head = run([git, "rev-parse", "HEAD"], cwd=VAULT, timeout=30)
    status = run([git, "status", "--porcelain=v1", "--", relative], cwd=VAULT, timeout=30)
    if head.returncode or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()) or status.returncode or status.stdout.strip():
        raise RuntimeError("tenant session page commit verification failed")
    return head.stdout.strip()


def put_page(slug: str, content: str) -> str:
    result = run([str(GBRAIN), "put", slug], input_text=content, timeout=180, cwd=VAULT)
    if result.returncode != 0:
        raise RuntimeError(f"GBrain write failed for {slug}")
    exists, readback = get_page(slug)
    if not exists:
        raise RuntimeError(f"GBrain readback is missing for {slug}")
    commit_page(slug, "write")
    return readback


def delete_page(slug: str) -> None:
    result = run([str(GBRAIN), "delete", slug], timeout=120, cwd=VAULT)
    if result.returncode != 0:
        raise RuntimeError(f"GBrain delete failed for {slug}")
    exists, _ = get_page(slug)
    if exists:
        raise RuntimeError(f"GBrain delete verification failed for {slug}")
    commit_page(slug, "rollback")


def write_page_backup(root: Path, slug: str, content: str) -> Path:
    path = root / "page-backups" / f"{sha256_bytes(slug.encode())}.md"
    atomic_write(path, content.encode("utf-8"))
    return path


def apply(receipt_path: Path) -> dict[str, Any]:
    if receipt_path.exists() or receipt_path.is_symlink():
        raise RuntimeError("native-session receipt path already exists or is unsafe")
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "applying",
        "started_at": iso(),
        "receipt_path": str(receipt_path),
        "pages": [],
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(receipt_path, receipt)
    try:
        with exclusive_lock(RUN_LOCK, blocking=False) as acquired:
            if not acquired:
                raise RuntimeError("native-session sync is already running")
            totals = run_exporter()
            notes = verify_generation()
            candidates = [render_note(source, path) for source, path in notes]
            candidates.sort(key=lambda item: item["source_key"])
            state = load_state()
            written = skipped = 0
            for candidate in candidates[:RUN_LIMIT]:
                source_key = candidate["source_key"]
                prior = state["sources"].get(source_key)
                with exclusive_lock(GBRAIN_LOCK, blocking=True):
                    exists, current = get_page(candidate["slug"])
                    if prior is not None:
                        if not isinstance(prior, dict) or set(prior) != {
                            "page_sha256",
                            "slug",
                            "source_signature",
                        }:
                            raise RuntimeError("native-session state entry is invalid")
                        if not exists:
                            raise RuntimeError("receipt-owned GBrain page is missing")
                        current_sha = sha256_bytes(current.encode("utf-8"))
                        if current_sha != prior["page_sha256"]:
                            raise RuntimeError("receipt-owned GBrain page advanced")
                        if prior["source_signature"] == candidate["source_signature"]:
                            skipped += 1
                            continue
                    elif exists and candidate["source_signature"] not in current:
                        raise RuntimeError("unowned GBrain session slug already exists")
                    action = "updated" if exists else "created"
                    before_sha = sha256_bytes(current.encode("utf-8")) if exists else None
                    backup = (
                        write_page_backup(receipt_path.parent, candidate["slug"], current)
                        if exists
                        else None
                    )
                    page_record = {
                        "action": action,
                        "slug": candidate["slug"],
                        "source_key": source_key,
                        "source_signature": candidate["source_signature"],
                        "before_sha256": before_sha,
                        "after_sha256": None,
                        "backup": str(backup) if backup else None,
                    }
                    receipt["pages"].append(page_record)
                    atomic_json(receipt_path, receipt)
                    readback = put_page(candidate["slug"], candidate["content"])
                    if candidate["source_signature"] not in readback:
                        raise RuntimeError("GBrain session readback signature mismatch")
                    after_sha = sha256_bytes(readback.encode("utf-8"))
                    page_record["after_sha256"] = after_sha
                    state["sources"][source_key] = {
                        "page_sha256": after_sha,
                        "slug": candidate["slug"],
                        "source_signature": candidate["source_signature"],
                    }
                    atomic_json(STATE_FILE, state)
                    atomic_json(receipt_path, receipt)
                    written += 1
            pending = max(0, len(candidates) - RUN_LIMIT)
            proof_candidate = candidates[0] if candidates else None
            if proof_candidate is not None:
                with exclusive_lock(GBRAIN_LOCK, blocking=True):
                    exists, page = get_page(proof_candidate["slug"])
                    if not exists or proof_candidate["proof"] not in page:
                        raise RuntimeError("direct native-session retrieval proof failed")
                    query = run(
                        [str(GBRAIN), "query", proof_candidate["proof"]],
                        timeout=180,
                        cwd=VAULT,
                    )
                    if query.returncode != 0 or proof_candidate["slug"] not in query.stdout:
                        raise RuntimeError("native-session query retrieval proof failed")
            receipt.update(
                {
                    "status": "verified",
                    "verified_at": iso(),
                    "sources": totals,
                    "candidate_count": len(candidates),
                    "written": written,
                    "skipped": skipped,
                    "pending_count": pending,
                    "proof_slug": proof_candidate["slug"] if proof_candidate else None,
                    "proof_value": proof_candidate["proof"] if proof_candidate else None,
                    "direct_retrieval": "pass" if proof_candidate else "not_applicable",
                    "query_retrieval": "pass" if proof_candidate else "not_applicable",
                }
            )
            atomic_json(receipt_path, receipt)
            return receipt
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {str(exc)[:800]}"
        receipt["failed_at"] = iso()
        atomic_json(receipt_path, receipt)
        raise


def validate_page_record(record: object, root: Path) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise TypeError("native-session rollback page record is invalid")
    action = record.get("action")
    slug = record.get("slug")
    if action not in {"created", "updated"} or not re.fullmatch(
        r"session/(?:claude-code|codex)/[0-9a-f]{64}-[0-9a-f]{16}",
        str(slug or ""),
    ):
        raise RuntimeError("native-session rollback ownership is invalid")
    after_sha = record.get("after_sha256")
    if after_sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(after_sha)):
        raise RuntimeError("native-session rollback post-digest is invalid")
    if not re.fullmatch(
        r"session-export/v4:[0-9a-f]{64}",
        str(record.get("source_signature") or ""),
    ):
        raise RuntimeError("native-session rollback source signature is invalid")
    if action == "created":
        if record.get("before_sha256") is not None or record.get("backup") is not None:
            raise RuntimeError("created-page rollback prestate is invalid")
    else:
        backup = Path(str(record.get("backup") or ""))
        expected = root / "page-backups" / f"{sha256_bytes(slug.encode())}.md"
        if backup != expected or not re.fullmatch(
            r"[0-9a-f]{64}", str(record.get("before_sha256") or "")
        ):
            raise RuntimeError("updated-page rollback prestate is invalid")
    return record


def rollback(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(read_regular(receipt_path).decode("utf-8"))
    if receipt.get("schema") != SCHEMA or receipt.get("receipt_path") != str(
        receipt_path
    ):
        raise RuntimeError("native-session rollback receipt is invalid")
    if receipt.get("status") == "rollback_verified":
        return receipt
    pages = receipt.get("pages")
    if not isinstance(pages, list):
        raise TypeError("native-session rollback page set is invalid")
    records = [validate_page_record(record, receipt_path.parent) for record in pages]
    with exclusive_lock(RUN_LOCK, blocking=True), exclusive_lock(
        GBRAIN_LOCK, blocking=True
    ):
        for record in reversed(records):
            exists, current = get_page(record["slug"])
            current_sha = sha256_bytes(current.encode("utf-8")) if exists else None
            if record["action"] == "created":
                if not exists:
                    continue
                if record["after_sha256"] is None:
                    if record["source_signature"] not in current:
                        raise RuntimeError("uncommitted created session page is unowned")
                elif current_sha != record["after_sha256"]:
                    raise RuntimeError("created session page advanced after rollout")
                delete_page(record["slug"])
            else:
                if current_sha == record["before_sha256"]:
                    continue
                if record["after_sha256"] is None:
                    if not exists or record["source_signature"] not in current:
                        raise RuntimeError("uncommitted updated session page is unowned")
                elif current_sha != record["after_sha256"]:
                    raise RuntimeError("updated session page advanced after rollout")
                before = read_regular(Path(record["backup"])).decode("utf-8")
                if sha256_bytes(before.encode("utf-8")) != record["before_sha256"]:
                    raise RuntimeError("updated session page backup digest mismatch")
                restored = put_page(record["slug"], before)
                if sha256_bytes(restored.encode("utf-8")) != record["before_sha256"]:
                    raise RuntimeError("updated session page restore verification failed")
    receipt["status"] = "rollback_verified"
    receipt["rolled_back_at"] = iso()
    atomic_json(receipt_path, receipt)
    return receipt


def generated_receipt_path() -> Path:
    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return RECEIPT_ROOT / f"{token}-{os.getpid()}.json"


def run_with_carding() -> dict[str, Any]:
    receipt_path = generated_receipt_path()
    value = apply(receipt_path)
    if not LUNA_ENABLE_MARKER.exists():
        value["luna_carder"] = {"status": "disabled"}
        atomic_json(receipt_path, value)
        return value
    read_regular(LUNA_CARDER)
    result = run([str(sys.executable), str(LUNA_CARDER), "--max", "8"], timeout=900)
    try:
        carding = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{PRINCIPAL} Luna carder output is invalid") from exc
    if result.returncode != 0 or carding.get("status") != "verified":
        value["luna_carder"] = carding
        value["status"] = "failed"
        value["error"] = f"{PRINCIPAL} Luna carder failed"
        atomic_json(receipt_path, value)
        raise RuntimeError(f"{PRINCIPAL} Luna carder failed")
    value["luna_carder"] = {
        key: carding.get(key)
        for key in (
            "status",
            "provider",
            "model",
            "candidate_count",
            "written",
            "pending_count",
            "daily_attempts",
            "daily_count",
            "usage",
        )
    }
    atomic_json(receipt_path, value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--receipt", type=Path, required=True)
    subparsers.add_parser("run")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "apply":
            value = apply(args.receipt)
        elif args.command == "run":
            value = run_with_carding()
        else:
            value = rollback(args.receipt)
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - operator CLI boundary
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
