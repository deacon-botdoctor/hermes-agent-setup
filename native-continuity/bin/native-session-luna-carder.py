#!/usr/bin/env python3
"""Create receipt-backed Luna memory cards from one principal's sanitized sessions."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
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
SCHEMA = os.environ.get(
    "NATIVE_SESSION_LUNA_SCHEMA", "native-session-luna-carder/v1"
)
MARKER_SCHEMA = os.environ.get(
    "NATIVE_SESSION_LUNA_MARKER_SCHEMA",
    "native-session-luna-carder-enable/v1",
)
MARKER_TARGET = os.environ.get("NATIVE_SESSION_LUNA_TARGET", PRINCIPAL)
ACTIVE_RUNTIME = Path(
    os.environ.get(
        "NATIVE_SESSION_RUNTIME",
        str(Path.home() / ".hermes"),
    )
)
ROOT = Path(
    os.environ.get(
        "NATIVE_SESSION_EXPORT_ROOT",
        str(ACTIVE_RUNTIME / "workspace" / "Brain" / "Native Sessions"),
    )
)
STATE_ROOT = Path(
    os.environ.get(
        "NATIVE_SESSION_LUNA_STATE_ROOT",
        str(ACTIVE_RUNTIME / "state" / "native-session-luna-carder"),
    )
)
STATE_FILE = STATE_ROOT / "state.json"
RECEIPT_ROOT = STATE_ROOT / "receipts"
RUN_LOCK = STATE_ROOT / "run.lock"
ENABLE_MARKER = STATE_ROOT / "enabled.json"
GBRAIN_LOCK = Path(
    os.environ.get(
        "NATIVE_SESSION_GBRAIN_LOCK",
        str(ACTIVE_RUNTIME / "state" / "native-session-sync" / "gbrain-process.lock"),
    )
)
GBRAIN = Path(
    os.environ.get(
        "NATIVE_SESSION_GBRAIN",
        str(ACTIVE_RUNTIME / "bin" / ("gbrain.exe" if os.name == "nt" else "gbrain")),
    )
)
VAULT = Path(os.environ.get("NATIVE_SESSION_VAULT", str(ACTIVE_RUNTIME / "workspace" / "Brain")))
NODE = ACTIVE_RUNTIME / "node" / "node.exe"
CODEX_JS = ACTIVE_RUNTIME / "node" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
_CODEX = os.environ.get("NATIVE_SESSION_CODEX", "").strip()
CODEX = Path(_CODEX) if _CODEX else None
SESSION_SYNC = Path(
    os.environ.get(
        "NATIVE_SESSION_SYNC", str(ACTIVE_RUNTIME / "bin" / "native-session-sync.py")
    )
)
SOURCES = tuple(
    item.strip().lower()
    for item in os.environ.get("NATIVE_SESSION_SOURCES", "codex").split(",")
    if item.strip()
)
if not SOURCES or any(item not in {"claude", "codex"} for item in SOURCES):
    raise RuntimeError("native-session sources are invalid")
MODEL = "gpt-5.6-luna"
PROVIDER = "openai-codex"
RUN_CAP = 8
MAX_NOTE_CHARS = 10_000
LOCK_TIMEOUT_SECONDS = 90
DIRECT_WRITE_RECOVERY_RECORDS: frozenset[str] = frozenset()


class SynthesisError(RuntimeError):
    def __init__(self, message: str, usage: dict[str, Any] | None = None):
        super().__init__(message)
        self.usage = usage


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + (LOCK_TIMEOUT_SECONDS if blocking else 0)
            while not acquired:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Luna carder is already running")
                    time.sleep(0.1)
        else:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
                acquired = True
            except BlockingIOError as exc:
                raise RuntimeError("Luna carder is already running") from exc
        yield
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def require_marker() -> dict[str, Any]:
    if ENABLE_MARKER.is_symlink() or not ENABLE_MARKER.is_file():
        raise RuntimeError(f"{PRINCIPAL} Luna carding is not enabled")
    value = json.loads(ENABLE_MARKER.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != MARKER_SCHEMA
        or value.get("target") != MARKER_TARGET
        or not isinstance(value.get("rollout_id"), str)
        or value.get("carder_sha256") != sha256_bytes(Path(__file__).read_bytes())
        or value.get("session_sync_sha256") != sha256_bytes(SESSION_SYNC.read_bytes())
    ):
        raise RuntimeError(f"{PRINCIPAL} Luna carding enable marker is invalid")
    return value


def load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        value: dict[str, Any] = {"schema": SCHEMA, "processed": {}, "daily": {}}
    else:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA
        or not isinstance(value.get("processed"), dict)
        or not isinstance(value.get("daily"), dict)
    ):
        raise RuntimeError(f"{PRINCIPAL} Luna carder state is invalid")
    for key, digest in value["processed"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(key)) or not re.fullmatch(
            r"[0-9a-f]{64}", str(digest)
        ):
            raise RuntimeError(f"{PRINCIPAL} Luna carder state entry is invalid")
    for receipt_path in sorted(RECEIPT_ROOT.glob("*.json")) if RECEIPT_ROOT.is_dir() else []:
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise RuntimeError(f"{PRINCIPAL} Luna carder receipt path is unsafe")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        if receipt.get("schema") != SCHEMA:
            continue
        cards = receipt.get("cards")
        if not isinstance(cards, list):
            raise RuntimeError(f"{PRINCIPAL} Luna carder receipt cards are invalid")
        for card in cards:
            if (
                not isinstance(card, dict)
                or not re.fullmatch(r"[0-9a-f]{64}", str(card.get("record_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(card.get("source_sha256", "")))
                or not isinstance(card.get("slug"), str)
            ):
                raise RuntimeError(f"{PRINCIPAL} Luna carder receipt entry is invalid")
            value["processed"][card["record_id"]] = card["source_sha256"]
    return value


def frontmatter(raw: str) -> dict[str, str]:
    if not raw.startswith("---\n"):
        return {}
    end = raw.find("\n---\n", 4)
    if end < 0:
        return {}
    fields: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        if line.startswith((" ", "-")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip("\"'")
    return fields


def section(raw: str, name: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(name)}\s*$\n(.*?)(?=^##\s+|\Z)",
        raw,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip()[:4_000] if match else ""


def candidates(state: dict[str, Any], limit: int) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for source in SOURCES:
        source_root = ROOT / source
        if source_root.is_symlink() or not source_root.is_dir():
            continue
        for path in source_root.glob("*.md"):
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8-sig", errors="strict")
            fields = frontmatter(raw)
            record_id = fields.get("record_id", "")
            if not (
                fields.get("type") == "session"
                and fields.get("export_schema") == "session-export/v4"
                and fields.get("identity_schema") == "session-record-id/v1"
                and fields.get("owner") == PRINCIPAL
                and fields.get("agent_source") == source
                and re.fullmatch(r"[0-9a-f]{64}", record_id)
            ):
                continue
            digest = sha256_bytes(raw.encode("utf-8"))
            if state["processed"].get(record_id) == digest:
                continue
            values.append(
                {
                    "key": record_id,
                    "sha256": digest,
                    "source": source,
                    "source_slug": (
                        f"session/{'claude-code' if source == 'claude' else 'codex'}/"
                        f"{record_id}-{digest[:16]}"
                    ),
                    "date": fields.get("date", ""),
                    "title": fields.get("title", "Session")[:200],
                    "primary_request": section(raw[:MAX_NOTE_CHARS], "Primary Request"),
                    "final_reply": section(raw[:MAX_NOTE_CHARS], "Final Assistant Reply"),
                }
            )
    values.sort(key=lambda item: (item["date"], item["key"]), reverse=True)
    return values[:limit]


def run(
    command: list[str],
    *,
    stdin: str | None = None,
    timeout: int = 600,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        cwd=cwd,
    )


def commit_card(slug: str) -> str:
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
    staged = run([git, "add", "--", relative], cwd=VAULT, timeout=30)
    changed = run([git, "diff", "--cached", "--quiet", "--", relative], cwd=VAULT, timeout=30)
    if staged.returncode or changed.returncode not in (0, 1):
        raise RuntimeError("tenant Luna card staging failed")
    if changed.returncode == 1:
        committed = run([git, "commit", "-m", "GBrain Luna card"], cwd=VAULT, timeout=60)
        if committed.returncode:
            raise RuntimeError("tenant Luna card commit failed")
    head = run([git, "rev-parse", "HEAD"], cwd=VAULT, timeout=30)
    status = run([git, "status", "--porcelain=v1", "--", relative], cwd=VAULT, timeout=30)
    if head.returncode or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()) or status.returncode or status.stdout.strip():
        raise RuntimeError("tenant Luna card commit verification failed")
    return head.stdout.strip()


def synthesize(items: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    launcher = [str(CODEX)] if CODEX is not None else [str(NODE), str(CODEX_JS)]
    for path in ((CODEX,) if CODEX is not None else (NODE, CODEX_JS)):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"{PRINCIPAL} native Codex runtime is unavailable for Luna carding")
    prompt_items = [
        {key: item[key] for key in ("key", "source", "date", "title", "primary_request", "final_reply")}
        for item in items
    ]
    prompt = (
        "Create one concise durable memory card per sanitized native-LLM session. "
        "Use only the supplied summaries. Do not infer secrets, paths, people, or tool payloads. "
        "Return only one JSON object with a cards array. Each card must contain exactly "
        "key, topic, outcome, and summary. outcome is shipped, blocked, explored, abandoned, or unknown.\n\n"
        + json.dumps(prompt_items, ensure_ascii=False)
    )
    with tempfile.TemporaryDirectory(prefix=f"{PRINCIPAL}-luna-carder-") as directory:
        schema_path = Path(directory) / "card-schema.json"
        schema_path.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "cards": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "topic": {"type": "string"},
                                    "outcome": {
                                        "type": "string",
                                        "enum": ["shipped", "blocked", "explored", "abandoned", "unknown"],
                                    },
                                    "summary": {"type": "string"},
                                },
                                "required": ["key", "topic", "outcome", "summary"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["cards"],
                    "additionalProperties": False,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        result = run(
            [
                *launcher, "exec", "--ignore-user-config",
                "--ignore-rules", "-m", MODEL, "-c", 'approval_policy="never"',
                "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                "--color", "never", "--json", "--output-schema", str(schema_path), "-",
            ],
            stdin=prompt,
        )
        events: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SynthesisError(f"{PRINCIPAL} native Codex returned invalid JSONL") from exc
            if not isinstance(event, dict):
                raise SynthesisError(f"{PRINCIPAL} native Codex event is invalid")
            events.append(event)
        turn_started = [event for event in events if event.get("type") == "turn.started"]
        turn_completed = [event for event in events if event.get("type") == "turn.completed"]
        failures = [event for event in events if event.get("type") in {"error", "turn.failed"}]
        messages = [
            event.get("item", {}).get("text")
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "agent_message"
        ]
        usage_event = turn_completed[0].get("usage", {}) if len(turn_completed) == 1 else {}
        usage = {
            "provider": PROVIDER,
            "model": MODEL,
            "api_calls": len(turn_started),
            "input_tokens": usage_event.get("input_tokens"),
            "cached_input_tokens": usage_event.get("cached_input_tokens"),
            "output_tokens": usage_event.get("output_tokens"),
            "estimated_cost_usd": 0.0,
            "cost_status": "included",
            "completed": result.returncode == 0 and len(turn_completed) == 1 and not failures,
            "failed": result.returncode != 0 or bool(failures),
        }
        if (
            result.returncode != 0
            or len(turn_started) != 1
            or len(turn_completed) != 1
            or failures
            or len(messages) != 1
            or not isinstance(messages[0], str)
        ):
            detail = json.dumps(failures[-2:], ensure_ascii=False)[-500:] or result.stderr[-500:]
            raise SynthesisError(f"{PRINCIPAL} native Codex Luna synthesis failed: {detail}", usage)
        raw = messages[0].strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SynthesisError(f"{PRINCIPAL} native Codex Luna returned invalid JSON", usage) from exc
    cards = value.get("cards") if isinstance(value, dict) else None
    expected = {item["key"] for item in items}
    returned: set[str] = set()
    if not isinstance(cards, list) or len(cards) != len(items):
        raise SynthesisError(f"{PRINCIPAL} Luna returned an incomplete card set", usage)
    for card in cards:
        if (
            not isinstance(card, dict)
            or set(card) != {"key", "topic", "outcome", "summary"}
            or not all(isinstance(card.get(key), str) for key in card)
            or card["outcome"] not in {"shipped", "blocked", "explored", "abandoned", "unknown"}
            or not card["topic"].strip()
            or len(card["topic"]) > 120
            or not card["summary"].strip()
            or len(card["summary"]) > 700
        ):
            raise SynthesisError(f"{PRINCIPAL} Luna returned an invalid card", usage)
        returned.add(card["key"])
    if returned != expected:
        raise SynthesisError(f"{PRINCIPAL} Luna card keys do not match the batch", usage)
    return cards, usage


def deterministic_cards(items: list[dict[str, str]]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for item in items:
        topic = re.sub(r"[\x00-\x1f\x7f]", " ", item.get("title", ""))
        topic = re.sub(r"\s+", " ", topic).strip()[:120] or "Codex session handoff"
        summary = item.get("final_reply") or item.get("primary_request") or (
            "The sanitized source session is available for on-demand retrieval."
        )
        summary = re.sub(r"[\x00-\x1f\x7f]", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()[:700]
        values.append(
            {
                "key": item["key"],
                "topic": topic,
                "outcome": "unknown",
                "summary": summary,
            }
        )
    return values


def fallback_content(item: dict[str, str], card: dict[str, str]) -> tuple[str, str]:
    summary = "The sanitized Codex session is available for on-demand retrieval."
    topic = "Codex session handoff"
    content = (
        "---\n"
        "type: memory\n"
        "memory_type: handoff\n"
        f"source: {PRINCIPAL}-native-session-luna-carder\n"
        f"agent_source: {item['source']}\n"
        f"event_date: {item['date']}\n"
        f"record_id: {item['key']}\n"
        f"source_sha256: {item['sha256']}\n"
        "write_recovery: deterministic-session-summary\n"
        "---\n\n"
        f"# {item['source'].title()} session: {topic}\n\n"
        f"Outcome: {card['outcome']}\n\n{summary}\n"
    )
    return content, summary


def write_card(item: dict[str, str], card: dict[str, str]) -> dict[str, Any]:
    slug = f"memory/native-session-luna/{item['source']}/{item['key']}-{item['sha256'][:16]}"
    content = (
        "---\n"
        "type: memory\n"
        "memory_type: handoff\n"
        f"source: {PRINCIPAL}-native-session-luna-carder\n"
        f"agent_source: {item['source']}\n"
        f"event_date: {item['date']}\n"
        f"record_id: {item['key']}\n"
        f"source_sha256: {item['sha256']}\n"
        "---\n\n"
        f"# {item['source'].title()} session: {card['topic']}\n\n"
        f"Outcome: {card['outcome']}\n\n{card['summary']}\n\n"
        f"Source: `{item['source_slug']}`\n"
    )
    fallback = False
    expected_summary = card["summary"]
    with exclusive_lock(GBRAIN_LOCK, blocking=True):
        if item["key"] in DIRECT_WRITE_RECOVERY_RECORDS:
            fallback = True
            content, expected_summary = fallback_content(item, card)
            result = run([str(GBRAIN), "put", slug], stdin=content, timeout=120, cwd=VAULT)
        else:
            try:
                result = run([str(GBRAIN), "put", slug], stdin=content, timeout=90, cwd=VAULT)
            except subprocess.TimeoutExpired:
                fallback = True
                content, expected_summary = fallback_content(item, card)
                time.sleep(15)
                result = run([str(GBRAIN), "put", slug], stdin=content, timeout=120, cwd=VAULT)
        if result.returncode != 0:
            raise RuntimeError(f"{PRINCIPAL} GBrain card write failed")
        readback = run([str(GBRAIN), "get", slug], timeout=120, cwd=VAULT)
        commit_card(slug)
    if (
        readback.returncode != 0
        or item["key"] not in readback.stdout
        or item["sha256"] not in readback.stdout
        or expected_summary not in readback.stdout
    ):
        raise RuntimeError(f"{PRINCIPAL} GBrain card readback failed")
    return {"slug": slug, "fallback": fallback}


def card(limit: int) -> dict[str, Any]:
    require_marker()
    for path in (GBRAIN, SESSION_SYNC):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"required {PRINCIPAL} carder dependency is unavailable: {path}")
    with exclusive_lock(RUN_LOCK):
        state = load_state()
        day = datetime.now(timezone.utc).date().isoformat()
        counters = dict(state["daily"].get(day, {"attempts": 0, "cards": 0}))
        items = candidates(state, min(limit, RUN_CAP))
        receipt_path = RECEIPT_ROOT / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}.json"
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "applying" if items else "verified",
            "started_at": iso(),
            "provider": PROVIDER,
            "model": MODEL,
            "run_cap": RUN_CAP,
            "candidate_count": len(items),
            "cards": [],
        }
        atomic_json(receipt_path, receipt)
        if items:
            counters["attempts"] = int(counters.get("attempts", 0)) + 1
            state["daily"] = {day: counters}
            atomic_json(STATE_FILE, state)
            try:
                try:
                    synthesized, usage = synthesize(items)
                except SynthesisError as exc:
                    synthesized = deterministic_cards(items)
                    usage = exc.usage or {
                        "provider": PROVIDER,
                        "model": MODEL,
                        "api_calls": 1,
                        "completed": False,
                        "failed": True,
                    }
                    receipt["synthesis_fallback"] = "deterministic-session-summary"
                    receipt["synthesis_error"] = str(exc)[:500]
                receipt["usage"] = usage
                atomic_json(receipt_path, receipt)
                by_key = {item["key"]: item for item in synthesized}
                for item in items:
                    write = write_card(item, by_key[item["key"]])
                    receipt["cards"].append(
                        {
                            "record_id": item["key"],
                            "source_sha256": item["sha256"],
                            "slug": write["slug"],
                            "write_fallback": write["fallback"],
                        }
                    )
                    state["processed"][item["key"]] = item["sha256"]
                    counters["cards"] = int(counters.get("cards", 0)) + 1
                    state["daily"] = {day: counters}
                    atomic_json(STATE_FILE, state)
                    atomic_json(receipt_path, receipt)
            except Exception as exc:
                if isinstance(exc, SynthesisError) and exc.usage is not None:
                    receipt["usage"] = exc.usage
                receipt.update({"status": "failed", "error": type(exc).__name__, "finished_at": iso()})
                atomic_json(receipt_path, receipt)
                raise
        receipt.update(
            {
                "status": "verified",
                "finished_at": iso(),
                "written": len(receipt["cards"]),
                "pending_count": len(candidates(state, RUN_CAP)),
                "daily_attempts": int(state["daily"].get(day, {}).get("attempts", 0)),
                "daily_count": int(state["daily"].get(day, {}).get("cards", 0)),
            }
        )
        atomic_json(receipt_path, receipt)
        return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=RUN_CAP)
    args = parser.parse_args(argv)
    if not 1 <= args.max <= RUN_CAP:
        parser.error(f"--max must be between 1 and {RUN_CAP}")
    try:
        print(json.dumps(card(args.max), sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - JSON CLI boundary
        print(json.dumps({"status": "failed", "error": str(exc)[:800]}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
