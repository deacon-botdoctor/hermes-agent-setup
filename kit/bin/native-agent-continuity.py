#!/usr/bin/env python3
"""Enroll supported native coding agents into an existing tenant brain.

This reconciler deliberately does not install providers, authenticate accounts,
or create a second brain. Client onboarding must first produce a verified local
brain receipt. The reconciler then owns only provider discovery, managed config
blocks, one native MCP canary, idempotent drift checks, and exact rollback.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import runpy
import secrets
import shutil
import subprocess
import threading
import queue
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA = "native-agent-continuity/v1"
MANIFEST_SCHEMA = "native-agent-continuity-manifest/v1"
STATE_SCHEMA = "native-agent-continuity-state/v1"
CAPABILITY = "native-agent-continuity"
RECONCILE_INTERVAL_SECONDS = 900
TOOLS = (
    "get_page",
    "list_pages",
    "search",
    "query",
    "get_stats",
    "get_health",
    "backlinks",
    "graph",
    "code_def",
    "put_page",
    "tag_page",
    "untag_page",
    "link_pages",
    "unlink_pages",
    "timeline_add",
)
CONFIG_START = "# >>> botdoctor:native-agent-continuity:gbrain >>>"
CONFIG_END = "# <<< botdoctor:native-agent-continuity:gbrain <<<"
INSTRUCTION_START = "<!-- botdoctor:native-agent-continuity:gbrain:start -->"
INSTRUCTION_END = "<!-- botdoctor:native-agent-continuity:gbrain:end -->"
HERMES_MCP_NAME = "gbrain"
BRAIN_TOOLS = ("get_page", "search")  # every GBrain MCP flavor in the fleet serves these


def serves_brain(tools: list[str] | None) -> bool:
    """True when a probe saw the brain tools, or a live single-writer brain held by the gateway."""
    return bool(tools) and (set(BRAIN_TOOLS) <= set(tools) or tools == ["gbrain_status", "lock_held"])
LEGACY_INSTRUCTION_START = "<!-- BEGIN BOT DOCTOR PRINCIPAL GBRAIN -->"
LEGACY_INSTRUCTION_END = "<!-- END BOT DOCTOR PRINCIPAL GBRAIN -->"
LEGACY_READONLY_TOOLS = (
    "get_brain_identity",
    "get_chunks",
    "get_health",
    "get_page",
    "get_stats",
    "list_pages",
    "query",
    "resolve_slugs",
    "search",
)


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"required regular file is missing or unsafe: {path}")
    return path.read_bytes()


def executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"required executable is missing or unsafe: {path}") from exc
    if not resolved.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"required executable is missing or unsafe: {path}")
    return resolved


def atomic_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(value)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: int = 120,
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
        env=env,
    )


def platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def absolute_path(value: object, name: str) -> Path:
    text = str(value or "")
    if not text or "\x00" in text or len(text) > 4096:
        raise ValueError(f"{name} is invalid")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(regular(path).decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        raise RuntimeError("native-agent continuity manifest is invalid")
    if value.get("platform") != platform_key():
        raise RuntimeError("native-agent continuity platform does not match this host")
    principal_id = str(value.get("principal_id") or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", principal_id):
        raise RuntimeError("native-agent continuity principal_id is invalid")
    principal_name = str(value.get("principal_name") or "").strip()
    if not principal_name or len(principal_name) > 100 or any(ord(c) < 32 for c in principal_name):
        raise RuntimeError("native-agent continuity principal_name is invalid")
    paths = value.get("paths")
    if not isinstance(paths, dict):
        raise RuntimeError("native-agent continuity paths are missing")
    required = (
        "home",
        "hermes_home",
        "vault",
        "gbrain_home",
        "gbrain",
        "mcp_python",
        "mcp_server",
        "lock_file",
        "baseline_receipt",
    )
    resolved = {name: absolute_path(paths.get(name), f"paths.{name}") for name in required}
    if resolved["home"].resolve() != Path.home().resolve():
        raise RuntimeError("native-agent continuity principal home does not match the process owner")
    providers = value.get("providers")
    if not isinstance(providers, dict) or providers.get("codex") is not True:
        raise RuntimeError("native-agent continuity first release requires providers.codex=true")
    value["_paths"] = resolved
    return value


def state_root(manifest: dict[str, Any]) -> Path:
    return manifest["_paths"]["hermes_home"] / "state" / "native-agent-continuity"


@contextlib.contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("native-agent continuity reconcile is already running") from exc
    try:
        yield
    finally:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def facade_inventory(manifest: dict[str, Any]) -> list[str]:
    paths = manifest["_paths"]
    env = os.environ.copy()
    env["GBRAIN_HOME"] = str(paths["gbrain_home"])
    env["GBRAIN_PRINCIPAL_NAME"] = str(manifest["principal_name"])
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    result = run(
        [
            str(paths["mcp_python"]),
            str(paths["mcp_server"]),
            "--gbrain",
            str(paths["gbrain"]),
            "--lock-file",
            str(paths["lock_file"]),
        ],
        env=env,
        stdin=request,
        timeout=90,
    )
    if result.returncode:
        raise RuntimeError("tenant GBrain MCP inventory failed")
    payload = None
    for line in result.stdout.splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("id") == 1:
            payload = candidate
            break
    rows = ((payload or {}).get("result") or {}).get("tools")
    values = [row.get("name") for row in rows or [] if isinstance(row, dict)]
    if values != list(TOOLS):
        raise RuntimeError("tenant GBrain MCP tool contract drifted")
    return values


def baseline_health(manifest: dict[str, Any]) -> dict[str, Any]:
    paths = manifest["_paths"]
    receipt = json.loads(regular(paths["baseline_receipt"]).decode("utf-8"))
    if receipt.get("status") != "verified":
        raise RuntimeError("tenant brain baseline receipt is not verified")
    if manifest.get("storage_mode") == "existing_remote":
        baseline = runpy.run_path(str(Path(__file__).with_name("tenant-gbrain-baseline.py")))
        baseline["verify"](manifest, {}, receipt)
    owners = receipt.get("persistent_gbrain_owners")
    if owners not in (0, []):
        raise RuntimeError("tenant brain baseline permits a persistent GBrain owner")
    for name in ("gbrain", "mcp_server"):
        regular(paths[name])
    executable(paths["mcp_python"])
    vault = paths["vault"].resolve(strict=True)
    if not vault.is_dir() or not os.access(vault, os.W_OK):
        raise RuntimeError("tenant vault is not a writable directory")
    env = os.environ.copy()
    env["GBRAIN_HOME"] = str(paths["gbrain_home"])
    version = run([str(paths["gbrain"]), "--version"], env=env, timeout=30)
    if version.returncode or "gbrain" not in (version.stdout + version.stderr).lower():
        raise RuntimeError("tenant GBrain executable proof failed")
    return {
        "status": "verified",
        "baseline_receipt": str(paths["baseline_receipt"]),
        "vault": str(vault),
        "gbrain_version": (version.stdout or version.stderr).strip()[:200],
        "mcp_tools": facade_inventory(manifest),
    }


def codex_command(manifest: dict[str, Any]) -> list[str] | None:
    configured = manifest.get("codex") or {}
    explicit = configured.get("command") if isinstance(configured, dict) else None
    candidates: list[str] = []
    if isinstance(explicit, list) and explicit and all(isinstance(v, str) and v for v in explicit):
        return list(explicit)
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(discovered)
    home = manifest["_paths"]["home"]
    if platform_key() == "macos":
        candidates.extend(
            [
                "/Applications/Codex.app/Contents/Resources/codex",
                "/Applications/ChatGPT.app/Contents/Resources/codex",
                str(home / ".local" / "bin" / "codex"),
            ]
        )
    elif platform_key() == "windows":
        local = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(str(Path(appdata) / "npm" / "codex.cmd"))
        if local:
            candidates.append(str(Path(local) / "Programs" / "Codex" / "codex.exe"))
    else:
        candidates.extend([str(home / ".local" / "bin" / "codex"), "/usr/local/bin/codex"])
    for raw in candidates:
        path = Path(raw).expanduser()
        try:
            resolved = executable(path)
        except RuntimeError:
            continue
        if resolved.is_file():
            if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
                comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
                return [comspec, "/d", "/s", "/c", str(path)]
            return [str(resolved)]
    return None


def codex_paths(manifest: dict[str, Any]) -> tuple[Path, Path]:
    configured = manifest.get("codex") or {}
    home = manifest["_paths"]["home"]
    config_value = configured.get("config") if isinstance(configured, dict) else None
    instruction_value = configured.get("instructions") if isinstance(configured, dict) else None
    config = absolute_path(config_value or home / ".codex" / "config.toml", "codex.config")
    instructions = absolute_path(
        instruction_value or home / ".codex" / "AGENTS.md", "codex.instructions"
    )
    return config, instructions


def provider_text(path: Path) -> str:
    if path.is_symlink():
        raise RuntimeError(f"refusing unsafe provider config symlink: {path}")
    if not path.exists():
        return ""
    if not path.is_file():
        raise RuntimeError(f"provider config is not a regular file: {path}")
    return path.read_text(encoding="utf-8-sig")


def managed_block(current: str, start: str, end: str, body: str, conflict: str | None = None) -> str:
    if current.count(start) != current.count(end) or current.count(start) > 1:
        raise RuntimeError("native-agent continuity managed marker structure is malformed")
    block = f"{start}\n{body.rstrip()}\n{end}"
    if start in current:
        begin = current.index(start)
        finish = current.index(end, begin) + len(end)
        prefix = current[:begin].rstrip()
        value = (prefix + "\n\n" if prefix else "") + block
        tail = current[finish:].strip("\n")
        if tail:
            value += "\n\n" + tail
        return value + "\n"
    if conflict and re.search(conflict, current, flags=re.MULTILINE):
        raise RuntimeError("an unmanaged conflicting GBrain MCP binding exists")
    prefix = current.rstrip()
    return (prefix + "\n\n" if prefix else "") + block + "\n"


def strip_legacy_readonly_config(
    current: str, manifest: dict[str, Any]
) -> tuple[str, bool]:
    parsed = tomllib.loads(current) if current.strip() else {}
    gbrain = ((parsed.get("mcp_servers") or {}).get("gbrain"))
    if gbrain is None:
        return current, False
    expected = {
        "command": "/usr/bin/python3",
        "args": [
            str(manifest["_paths"]["hermes_home"] / "bin/gbrain-readonly-mcp.py")
        ],
        "enabled_tools": list(LEGACY_READONLY_TOOLS),
        "default_tools_approval_mode": "approve",
    }
    if gbrain != expected:
        return current, False
    header = re.compile(r"(?m)^\s*\[mcp_servers\.gbrain\]\s*$")
    match = header.search(current)
    if match is None:
        raise RuntimeError("legacy GBrain MCP binding cannot be located exactly")
    next_table = re.search(r"(?m)^\s*\[", current[match.end() :])
    finish = match.end() + (next_table.start() if next_table else len(current[match.end() :]))
    value = (current[: match.start()].rstrip() + "\n\n" + current[finish:].lstrip()).strip()
    return (value + "\n" if value else ""), True


def strip_legacy_readonly_instructions(current: str) -> tuple[str, bool]:
    starts = current.count(LEGACY_INSTRUCTION_START)
    ends = current.count(LEGACY_INSTRUCTION_END)
    if starts != ends or starts > 1:
        raise RuntimeError("legacy principal GBrain instruction markers are malformed")
    if starts == 0:
        return current, False
    begin = current.index(LEGACY_INSTRUCTION_START)
    finish = current.index(LEGACY_INSTRUCTION_END, begin) + len(
        LEGACY_INSTRUCTION_END
    )
    value = (current[:begin].rstrip() + "\n\n" + current[finish:].lstrip()).strip()
    return (value + "\n" if value else ""), True


def codex_config_body(manifest: dict[str, Any]) -> str:
    paths = manifest["_paths"]
    args = [
        str(paths["mcp_server"]),
        "--gbrain",
        str(paths["gbrain"]),
        "--lock-file",
        str(paths["lock_file"]),
    ]
    return "\n".join(
        [
            "[mcp_servers.gbrain]",
            f"command = {json.dumps(str(paths['mcp_python']))}",
            f"args = {json.dumps(args, separators=(',', ':'))}",
            f"cwd = {json.dumps(str(paths['vault']))}",
            f"enabled_tools = {json.dumps(list(TOOLS), separators=(',', ':'))}",
            'default_tools_approval_mode = "approve"',
            "startup_timeout_sec = 120",
            "",
            "[mcp_servers.gbrain.env]",
            f"GBRAIN_HOME = {json.dumps(str(paths['gbrain_home']))}",
            f"GBRAIN_PRINCIPAL_NAME = {json.dumps(str(manifest['principal_name']))}",
            f"GBRAIN_VAULT = {json.dumps(str(paths['vault']))}",
            *(['GBRAIN_STORAGE_MODE = "existing_remote"']
              if manifest.get("storage_mode") == "existing_remote" else []),
        ]
    )


def codex_instruction_body(manifest: dict[str, Any]) -> str:
    storage = (
        "The existing remote tenant brain is the source of truth; do not create another brain or treat local files as its database. "
        if manifest.get("storage_mode") == "existing_remote" else
        "The tenant vault is the sole client-facing source of truth; local GBrain is its indexed, bounded write-through layer. "
    )
    return (
        "## Tenant brain continuity\n\n"
        f"Use the local `gbrain` MCP before guessing about {manifest['principal_name']}'s durable context, prior decisions, or indexed work. "
        + storage +
        "Write durable client facts, decisions, and handoffs through GBrain when relevant. "
        "Raw transcripts, reasoning, tool payloads, credentials, URLs, and absolute paths remain local and must never be copied into an operator-wide brain."
    )


def hermes_binding_block(manifest: dict[str, Any], newline: str) -> str:
    """Render the gateway's own GBrain MCP entry; JSON strings are valid YAML scalars."""
    paths = manifest["_paths"]
    args = [str(paths["mcp_server"]), "--gbrain", str(paths["gbrain"]), "--lock-file", str(paths["lock_file"])]
    env = {
        "GBRAIN_HOME": str(paths["gbrain_home"]),
        "GBRAIN_PRINCIPAL_NAME": str(manifest["principal_name"]),
        "GBRAIN_VAULT": str(paths["vault"]),
    }
    if manifest.get("storage_mode") == "existing_remote":
        env["GBRAIN_STORAGE_MODE"] = "existing_remote"
    lines = [f"  {HERMES_MCP_NAME}:", f"    command: {json.dumps(str(paths['mcp_python']))}", "    args:"]
    lines += [f"    - {json.dumps(arg)}" for arg in args]
    lines += ["    env:"] + [f"      {key}: {json.dumps(value)}" for key, value in env.items()]
    lines += ["    enabled: true", "    tier: on_demand", "    timeout: 120"]
    return newline.join(lines) + newline


def binding_targets_principal(entry: dict[str, Any], home: Path, base: Path, expected: list[Path]) -> bool:
    """True when an MCP entry can run and opens one of the ``expected`` GBrain stores.

    A store is identified by the engine and database named in ``<GBRAIN_HOME>/.gbrain/config.json``
    (GBRAIN_HOME, else HOME, from the entry's env), so two homes that share one database match and
    an alternate store under the same home does not. Remote URL entries carry no verifiable store
    identity and never satisfy the binding. Every path the entry passes must stay inside ``home``.
    Relative values resolve against the entry's cwd (or ``base``). hermes-local-selfcheck.py keeps an identical copy because it is deployed and run separately.
    """
    if entry.get("url"):
        return False
    cwd = Path(os.path.expanduser(str(entry.get("cwd") or base)))
    cwd = cwd if cwd.is_absolute() else base / cwd

    def resolve(value: object) -> Path | None:
        text = os.path.expanduser(str(value))
        if os.path.isabs(text):
            return Path(text).resolve()
        if "/" in text or "\\" in text or text.startswith("."):
            return (cwd / text).resolve()
        return None

    command = str(entry.get("command") or "")
    if not command:
        return False
    command_path = resolve(command)
    if command_path is None:
        if shutil.which(command) is None:
            return False
    elif not (command_path.is_file() and os.access(command_path, os.X_OK)):
        return False
    root = Path(home).resolve()
    env = entry.get("env")
    env = env if isinstance(env, dict) else {}
    for value in [*(entry.get("args") or []), *env.values()]:
        path = resolve(value)
        if path is not None and not path.is_relative_to(root):
            return False
    store_home = resolve(env.get("GBRAIN_HOME") or env.get("HOME") or "")
    if store_home is None or not store_home.is_relative_to(root):
        return False
    wanted = [Path(path).resolve() for path in expected]
    if store_home in wanted:
        return True
    identity = brain_store_identity(store_home)
    return identity is not None and identity in {brain_store_identity(path) for path in wanted}


def brain_store_identity(gbrain_home: Path) -> tuple[str, str, str] | None:
    """(engine, database) from a GBrain home's config, or None when it has no readable config."""
    config_dir = Path(gbrain_home) / ".gbrain"
    try:
        config = json.loads((config_dir / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    if config.get("database_url"):
        return (str(config.get("engine") or ""), "url", str(config["database_url"]))
    if config.get("database_path"):
        db = Path(os.path.expanduser(str(config["database_path"])))
        return (str(config.get("engine") or ""), "path", str((db if db.is_absolute() else config_dir / db).resolve()))
    return None


def enable_disabled_entry(text: str) -> str:
    """Flip the block-style `enabled:` key of mcp_servers.gbrain to true, preserving everything else.

    Accepts any YAML 1.1 false spelling, an inline comment, CRLF, and a missing final newline.
    The caller re-parses the result and refuses any other change.
    """
    lines = text.splitlines(keepends=True)

    def body(index: int) -> str:
        return lines[index].rstrip("\r\n")

    def indent(value: str) -> int:
        return len(value) - len(value.lstrip())

    def children(start: int, parent: int) -> list[int]:
        rows: list[int] = []
        for index in range(start + 1, len(lines)):
            value = body(index)
            if not value.strip() or value.lstrip().startswith("#"):
                continue
            if indent(value) <= parent:
                break
            rows.append(index)
        return rows

    def direct(rows: list[int]) -> list[int]:
        depth = min((indent(body(i)) for i in rows), default=0)
        return [i for i in rows if indent(body(i)) == depth]

    root = next((i for i in range(len(lines)) if re.match(r"^mcp_servers:[ \t]*(?:#.*)?$", body(i))), None)
    if root is None:
        raise RuntimeError("Hermes config.yaml has no block-style mcp_servers mapping")
    entry = next(
        (i for i in direct(children(root, 0)) if re.match(rf"^[ \t]+{HERMES_MCP_NAME}:[ \t]*(?:#.*)?$", body(i))),
        None,
    )
    if entry is None:
        raise RuntimeError("disabled Hermes GBrain MCP entry cannot be located")
    key = re.compile(
        r"^(?P<lead>[ \t]+enabled:[ \t]*)(?:false|False|FALSE|no|No|NO|off|Off|OFF)(?P<tail>[ \t]*(?:#.*)?)$"
    )
    hits = [(i, m) for i in direct(children(entry, indent(body(entry)))) if (m := key.match(body(i)))]
    if len(hits) != 1:
        raise RuntimeError("disabled Hermes GBrain MCP entry has no single block-style enabled key")
    index, match = hits[0]
    ending = lines[index][len(body(index)) :]
    lines[index] = match.group("lead") + "true" + match.group("tail") + ending
    return "".join(lines)


def probe_brain_mcp(entry: dict[str, Any], base: Path, timeout: int = 60) -> list[str] | None:
    """Launch a stdio MCP entry exactly as configured and return its tool names.

    Waits for each reply before the next message and closes stdin last, as a gateway does. Returns None when the process
    cannot start, times out, or answers without a tool list. hermes-local-selfcheck.py keeps an identical copy.
    """
    command = str(entry.get("command") or "")
    args = [str(value) for value in (entry.get("args") or [])]
    env = os.environ.copy()
    if isinstance(entry.get("env"), dict):
        env.update({str(key): str(value) for key, value in entry["env"].items()})
    cwd = Path(os.path.expanduser(str(entry.get("cwd") or base)))
    cwd = cwd if cwd.is_absolute() else base / cwd
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "botdoctor-brain-binding-probe", "version": "1"}}}
    try:
        proc = subprocess.Popen(
            [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env, cwd=str(cwd) if cwd.is_dir() else None,
        )
    except OSError:
        return None
    replies: queue.Queue = queue.Queue()
    errors: list[str] = []
    threading.Thread(target=lambda: [replies.put(line) for line in proc.stdout], daemon=True).start()
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(message) -> bool:
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def reply(wanted):
        while time.monotonic() < deadline:
            try:
                line = replies.get(timeout=max(0.05, min(1.0, deadline - time.monotonic())))
            except queue.Empty:
                if proc.poll() is not None and replies.empty():
                    return None
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == wanted:
                return message
        return None

    tools = None
    try:
        if send(initialize) and reply(1) is not None:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            if send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}):
                answer = reply(2)
                tools = ((answer or {}).get("result") or {}).get("tools")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if not isinstance(tools, list):
        return None
    names = [str(row.get("name")) for row in tools if isinstance(row, dict)]
    # A single-writer brain already open in the live gateway's own server answers in
    # status-only mode with reason lock_held. That proves this entry launches the real
    # server for an existing, healthy store; any other status-mode reason is a failure.
    if names == ["gbrain_status"] and '"reason":"lock_held"' in "".join(errors).replace(" ", ""):
        return ["gbrain_status", "lock_held"]
    return names


def plan_hermes_binding(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the gateway config text that binds the principal's brain, or why none is needed.

    An enabled gbrain entry is left untouched: runtimes may already bind the same
    brain through `gbrain serve`. Only a missing or disabled entry is repaired.
    """
    import yaml

    path = manifest["_paths"]["hermes_home"] / "config.yaml"
    provider_text(path)  # symlink and regular-file safety
    # Read bytes so Windows text mode cannot rewrite CRLF line endings.
    raw = path.read_bytes() if path.exists() else b""
    bom = "\ufeff" if raw.startswith(b"\xef\xbb\xbf") else ""
    text = raw.decode("utf-8-sig")
    if not text.strip():
        return {"status": "no_hermes_config", "path": str(path)}
    config = yaml.safe_load(text)
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    if not isinstance(config, dict) or not isinstance(servers, (dict, type(None))):
        raise RuntimeError("Hermes config.yaml mcp_servers layout is invalid")
    servers = servers or {}
    home = manifest["_paths"]["home"]
    agent = config.get("agent")
    disabled = set((agent.get("disabled_toolsets") or []) if isinstance(agent, dict) else [])
    if any(
        str(name).startswith(HERMES_MCP_NAME) and isinstance(entry, dict)
        and entry.get("enabled", True) is not False and f"mcp-{name}" not in disabled
        and binding_targets_principal(entry, home, path.parent, [manifest["_paths"]["gbrain_home"]])
        and serves_brain(probe_brain_mcp(entry, path.parent))
        for name, entry in servers.items()
    ):
        return {"status": "present", "path": str(path)}
    if f"mcp-{HERMES_MCP_NAME}" in disabled:
        raise RuntimeError("agent.disabled_toolsets contains mcp-gbrain; remove it to bind the principal brain")
    current = servers.get(HERMES_MCP_NAME)
    if isinstance(current, dict) and current.get("enabled", True) is not False:
        raise RuntimeError("existing mcp_servers.gbrain is enabled but does not serve this principal's brain")
    if isinstance(current, dict) and not binding_targets_principal(current, home, path.parent, [manifest["_paths"]["gbrain_home"]]):
        raise RuntimeError("existing mcp_servers.gbrain does not target this principal's brain")
    newline = "\r\n" if "\r\n" in text else "\n"
    if current is None:
        block = hermes_binding_block(manifest, newline)
        header = re.search(r"(?m)^mcp_servers:[ \t]*(?:#.*)?\r?\n", text)
        empty = re.search(r"(?m)^mcp_servers:[ \t]*\{\}[ \t]*\r?$", text)
        if header:
            next_text = text[: header.end()] + block + text[header.end() :]
        elif empty:
            next_text = text[: empty.start()] + "mcp_servers:" + newline + block.rstrip("\r\n") + text[empty.end() :]
        elif "mcp_servers" in config:
            raise RuntimeError("Hermes config.yaml mcp_servers layout is not editable")
        else:
            next_text = text.rstrip("\r\n") + newline + "mcp_servers:" + newline + block
        action = "added"
    else:
        next_text = enable_disabled_entry(text)
        action = "enabled"
    after = yaml.safe_load(next_text)
    after_servers = after.get("mcp_servers") or {}
    if (
        after_servers.get(HERMES_MCP_NAME, {}).get("enabled") is not True
        or {k: v for k, v in after.items() if k != "mcp_servers"} != {k: v for k, v in config.items() if k != "mcp_servers"}
        or {k: v for k, v in after_servers.items() if k != HERMES_MCP_NAME} != {k: v for k, v in servers.items() if k != HERMES_MCP_NAME}
    ):
        raise RuntimeError("Hermes GBrain MCP binding would change unrelated configuration")
    return {"status": action, "path": str(path), "next_text": bom + next_text, "source_sha256": sha256_bytes(raw)}


def apply_hermes_binding(manifest: dict[str, Any]) -> dict[str, Any]:
    """Bind the Hermes gateway itself to the same brain Codex uses; the gateway hot-reconciles MCP config."""
    plan = plan_hermes_binding(manifest)
    if "next_text" not in plan:
        return {"status": plan["status"]}
    path = Path(plan["path"])
    # Prove the new entry serves the brain before touching config.yaml, so a failure needs no
    # restore and cannot race a concurrent edit; the write itself is atomic.
    import yaml

    entry = (yaml.safe_load(plan["next_text"].lstrip("\ufeff")).get("mcp_servers") or {}).get(HERMES_MCP_NAME) or {}
    if not serves_brain(probe_brain_mcp(entry, path.parent)):
        raise RuntimeError("new Hermes GBrain MCP binding did not serve the brain; config.yaml left unchanged")
    if sha256_bytes(path.read_bytes()) != plan["source_sha256"]:
        raise RuntimeError("config.yaml changed during the binding probe; rerun reconcile")
    rollout = state_root(manifest) / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}-hermes-gateway"
    record = snapshot(path, rollout / "backups")
    atomic_bytes(path, plan["next_text"].encode("utf-8"), mode=path.stat().st_mode & 0o777)
    record["after_sha256"] = sha256_bytes(path.read_bytes())
    receipt_path = rollout / "receipt.json"
    atomic_json(
        receipt_path,
        {
            "schema": SCHEMA,
            "status": "verified",
            "capability": CAPABILITY,
            "provider": "hermes_gateway",
            "principal_id": manifest["principal_id"],
            "action": plan["status"],
            "verified_at": iso(),
            "files": [record],
        },
    )
    return {"status": plan["status"], "receipt": str(receipt_path)}


def codex_login(command: list[str]) -> tuple[bool, str]:
    result = run([*command, "login", "status"], timeout=90)
    text = (result.stdout + result.stderr).strip()
    return result.returncode == 0 and "logged in" in text.lower(), text[-500:]


def native_canary(manifest: dict[str, Any], command: list[str]) -> dict[str, Any]:
    token = f"NATIVE_AGENT_CONTINUITY_{secrets.token_hex(8).upper()}"
    principal = manifest["principal_id"]
    slug = f"proofs/native-agent-continuity/{principal}/codex-{token.lower()}"
    content = (
        "---\n"
        "type: proof\n"
        "source: native-agent-continuity\n"
        f"date: {datetime.now(timezone.utc).date().isoformat()}\n"
        "---\n\n"
        "# Native Codex continuity proof\n\n"
        f"{token}\n"
    )
    prompt = (
        "Perform one bounded local integration proof using only the configured GBrain MCP. "
        f"Call gbrain.put_page with slug `{slug}` and this exact Markdown content:\n\n{content}\n"
        f"Then call gbrain.get_page for `{slug}`. Only if the returned page contains `{token}`, reply exactly `{token}`. "
        "Do not inspect or disclose any other page."
    )
    configured = manifest.get("codex") or {}
    model = str(configured.get("canary_model") or "gpt-5.6-terra")
    result = run(
        [
            *command,
            "exec",
            "-m",
            model,
            "-c",
            'approval_policy="never"',
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "--color",
            "never",
            "--json",
            "-",
        ],
        stdin=prompt,
        timeout=600,
    )
    tools: list[str] = []
    final = ""
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item, dict) or payload.get("type") != "item.completed":
            continue
        if item.get("type") == "mcp_tool_call" and item.get("server") == "gbrain":
            tools.append(str(item.get("tool") or ""))
        elif item.get("type") == "agent_message":
            final = str(item.get("text") or "").strip()
    if result.returncode or tools != ["put_page", "get_page"] or final != token:
        raise RuntimeError("native Codex GBrain write/readback canary failed")
    return {"status": "verified", "model": model, "slug": slug, "tools": tools, "token_sha256": sha256_bytes(token.encode())}


def snapshot(path: Path, backup_root: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"refusing unsafe provider config symlink: {path}")
    existed = path.exists()
    if existed and not path.is_file():
        raise RuntimeError(f"provider config is not a regular file: {path}")
    value = path.read_bytes() if existed else b""
    backup = backup_root / f"{sha256_bytes(str(path).encode())}.bak"
    if existed:
        atomic_bytes(backup, value)
    return {"path": str(path), "existed": existed, "before_sha256": sha256_bytes(value) if existed else None, "backup": str(backup) if existed else None}


def restore(record: dict[str, Any]) -> None:
    path = Path(record["path"])
    expected = record.get("after_sha256")
    if path.exists():
        if path.is_symlink() or not path.is_file() or sha256_bytes(path.read_bytes()) != expected:
            raise RuntimeError(f"rollback refuses advanced provider config: {path}")
    elif expected is not None:
        raise RuntimeError(f"rollback provider config disappeared: {path}")
    if record["existed"]:
        atomic_bytes(path, regular(Path(record["backup"])))
    else:
        path.unlink(missing_ok=True)


def current_state(manifest: dict[str, Any]) -> dict[str, Any] | None:
    path = state_root(manifest) / "state.json"
    if not path.exists():
        return None
    value = json.loads(regular(path).decode("utf-8"))
    return value if isinstance(value, dict) and value.get("schema") == STATE_SCHEMA else None


def write_observation(manifest: dict[str, Any], value: dict[str, Any]) -> None:
    observed_at = iso()
    state = {
        "schema": STATE_SCHEMA,
        "capability": CAPABILITY,
        "principal_id": manifest["principal_id"],
        "updated_at": observed_at,
        **value,
    }
    atomic_json(state_root(manifest) / "state.json", state)
    if value.get("capability_live") is True:
        atomic_json(
            manifest["_paths"]["hermes_home"]
            / "state"
            / "capability-receipts"
            / f"{CAPABILITY}.json",
            {
                "schema_version": 1,
                "capability": CAPABILITY,
                "status": "live",
                "identity_verified": True,
                "smoke_verified": True,
                "credential_values_recorded": False,
                "observed_at": observed_at,
                "observed_status": value.get("status"),
            },
        )


def verified_prior_receipt(prior: dict[str, Any] | None) -> tuple[Path, dict[str, Any]] | None:
    if not prior or prior.get("status") != "verified":
        return None
    providers = prior.get("providers")
    codex = providers.get("codex") if isinstance(providers, dict) else None
    raw_path = codex.get("receipt") if isinstance(codex, dict) else None
    if not raw_path:
        return None
    path = absolute_path(raw_path, "prior Codex receipt")
    receipt = json.loads(regular(path).decode("utf-8"))
    if (
        receipt.get("schema") != SCHEMA
        or receipt.get("status") != "verified"
        or receipt.get("provider") != "codex"
    ):
        return None
    return path, receipt


def scan(manifest: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_health(manifest)
    command = codex_command(manifest)
    if command is None:
        return {"schema": SCHEMA, "status": "ready_no_providers", "capability_live": True, "baseline": baseline, "providers": {"codex": {"status": "absent"}}}
    logged_in, detail = codex_login(command)
    status = "detected" if logged_in else "detected_not_enrolled"
    return {
        "schema": SCHEMA,
        "status": status,
        "capability_live": True,
        "baseline": baseline,
        "providers": {"codex": {"status": status, "command": command, "logged_in": logged_in, "detail_sha256": sha256_bytes(detail.encode())}},
    }


def reconcile(manifest: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    root = state_root(manifest)
    with process_lock(root / "reconcile.lock"):
        prior = current_state(manifest)
        if not force and prior:
            updated = datetime.fromisoformat(str(prior["updated_at"]).replace("Z", "+00:00"))
            if time.time() - updated.timestamp() < RECONCILE_INTERVAL_SECONDS:
                return {"schema": SCHEMA, "status": "rate_limited", "capability_live": prior.get("capability_live") is True, "prior_status": prior.get("status")}
        observed = scan(manifest)
        try:
            observed["hermes_gateway"] = apply_hermes_binding(manifest)
        except Exception as exc:  # noqa: BLE001 - keep Codex enrollment; selfcheck reports the gap
            observed["hermes_gateway"] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        provider = observed["providers"]["codex"]
        if provider["status"] == "absent":
            write_observation(manifest, {"status": "ready_no_providers", "capability_live": True, "providers": observed["providers"], "baseline": observed["baseline"], "hermes_gateway": observed["hermes_gateway"]})
            return observed
        if not provider["logged_in"]:
            write_observation(manifest, {"status": "detected_not_enrolled", "capability_live": True, "providers": observed["providers"], "baseline": observed["baseline"], "hermes_gateway": observed["hermes_gateway"]})
            return observed
        config, instructions = codex_paths(manifest)
        config_text = provider_text(config)
        instruction_text = provider_text(instructions)
        config_base, migrated_config = strip_legacy_readonly_config(
            config_text, manifest
        )
        instruction_base, migrated_instructions = strip_legacy_readonly_instructions(
            instruction_text
        )
        next_config = managed_block(config_base, CONFIG_START, CONFIG_END, codex_config_body(manifest), r"^\s*\[mcp_servers\.gbrain\]\s*$")
        next_instructions = managed_block(instruction_base, INSTRUCTION_START, INSTRUCTION_END, codex_instruction_body(manifest))
        tomllib.loads(next_config)
        prior_receipt = verified_prior_receipt(prior)
        if (
            prior_receipt is not None
            and config_text == next_config
            and instruction_text == next_instructions
        ):
            receipt_path, receipt = prior_receipt
            write_observation(
                manifest,
                {
                    "status": "verified",
                    "capability_live": True,
                    "providers": {
                        "codex": {
                            "status": "verified",
                            "command": provider["command"],
                            "receipt": str(receipt_path),
                        }
                    },
                    "baseline": observed["baseline"],
                    "hermes_gateway": observed["hermes_gateway"],
                },
            )
            return {
                "schema": SCHEMA,
                "status": "verified",
                "capability_live": True,
                "provider": "codex",
                "idempotent": True,
                "receipt": str(receipt_path),
                "native_canary": receipt.get("native_canary"),
            }
        rollout = root / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
        backups = rollout / "backups"
        records = [snapshot(config, backups), snapshot(instructions, backups)]
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "applying",
            "capability": CAPABILITY,
            "principal_id": manifest["principal_id"],
            "started_at": iso(),
            "manifest_sha256": sha256_bytes(regular(Path(manifest["_manifest_path"]))),
            "files": records,
            "baseline": observed["baseline"],
            "migration": {
                "legacy_readonly_config": migrated_config,
                "legacy_readonly_instructions": migrated_instructions,
            },
        }
        receipt_path = rollout / "receipt.json"
        atomic_json(receipt_path, receipt)
        try:
            atomic_bytes(config, next_config.encode())
            atomic_bytes(instructions, next_instructions.encode())
            for record in records:
                path = Path(record["path"])
                record["after_sha256"] = sha256_bytes(path.read_bytes())
            canary = native_canary(manifest, provider["command"])
            receipt.update({"status": "verified", "verified_at": iso(), "provider": "codex", "native_canary": canary, "files": records, "capability_live": True})
            atomic_json(receipt_path, receipt)
            write_observation(manifest, {"status": "verified", "capability_live": True, "providers": {"codex": {"status": "verified", "command": provider["command"], "receipt": str(receipt_path)}}, "baseline": observed["baseline"], "hermes_gateway": observed["hermes_gateway"]})
            return receipt
        except Exception as exc:
            receipt.update({"status": "failed", "failed_at": iso(), "error": f"{type(exc).__name__}: {str(exc)[:1000]}", "files": records})
            atomic_json(receipt_path, receipt)
            for record in reversed(records):
                if record.get("after_sha256") is None:
                    path = Path(record["path"])
                    record["after_sha256"] = sha256_bytes(path.read_bytes()) if path.exists() else None
                restore(record)
            receipt["rollback_status"] = "files_restored"
            receipt["rolled_back_at"] = iso()
            atomic_json(receipt_path, receipt)
            raise


def verify(manifest: dict[str, Any]) -> dict[str, Any]:
    observed = scan(manifest)
    state = current_state(manifest)
    command = codex_command(manifest)
    if command is None:
        return {**observed, "verification": "pass"}
    if not state or state.get("status") != "verified":
        raise RuntimeError("detected Codex has no verified continuity activation")
    config, instructions = codex_paths(manifest)
    config_text = provider_text(config)
    instruction_text = provider_text(instructions)
    expected_config = managed_block(config_text, CONFIG_START, CONFIG_END, codex_config_body(manifest), r"^\s*\[mcp_servers\.gbrain\]\s*$")
    expected_instructions = managed_block(instruction_text, INSTRUCTION_START, INSTRUCTION_END, codex_instruction_body(manifest))
    if config_text != expected_config:
        raise RuntimeError("Codex GBrain managed config drifted")
    if instruction_text != expected_instructions:
        raise RuntimeError("Codex GBrain managed instructions drifted")
    tomllib.loads(config_text)
    return {**observed, "status": "verified", "verification": "pass", "state": state}


def rollback(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(regular(receipt_path).decode("utf-8"))
    if receipt.get("schema") != SCHEMA or receipt.get("status") != "verified":
        raise RuntimeError("native-agent continuity receipt is not rollback-eligible")
    for record in reversed(receipt.get("files") or []):
        restore(record)
    receipt.update({"status": "rollback_verified", "rolled_back_at": iso()})
    atomic_json(receipt_path, receipt)
    return receipt


def self_test() -> dict[str, Any]:
    body = managed_block("title = \"test\"\n", CONFIG_START, CONFIG_END, "[mcp_servers.gbrain]\ncommand = \"python\"")
    tomllib.loads(body)
    return {"status": "pass", "schema_version": 1, "capability": CAPABILITY, "mcp_tool_count": len(TOOLS)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")
    for name in ("scan", "reconcile", "verify"):
        child = sub.add_parser(name)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--json", action="store_true")
        if name == "reconcile":
            child.add_argument("--force", action="store_true")
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("--receipt", type=Path, required=True)
    rollback_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            value = self_test()
        elif args.command == "rollback":
            value = rollback(args.receipt)
        elif args.command in {"scan", "reconcile", "verify"}:
            manifest = load_manifest(args.manifest)
            manifest["_manifest_path"] = str(args.manifest)
            if args.command == "scan":
                value = scan(manifest)
            elif args.command == "reconcile":
                value = reconcile(manifest, force=args.force)
            else:
                value = verify(manifest)
        else:
            parser.error("a command or --self-test is required")
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - operator-facing CLI boundary
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
