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
import secrets
import shutil
import subprocess
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


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"required regular file is missing or unsafe: {path}")
    return path.read_bytes()


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
    owners = receipt.get("persistent_gbrain_owners")
    if owners not in (0, []):
        raise RuntimeError("tenant brain baseline permits a persistent GBrain owner")
    for name in ("gbrain", "mcp_python", "mcp_server"):
        regular(paths[name])
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
        if path.is_file() and not path.is_symlink():
            if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
                comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
                return [comspec, "/d", "/s", "/c", str(path)]
            return [str(path)]
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
        ]
    )


def codex_instruction_body(manifest: dict[str, Any]) -> str:
    return (
        "## Tenant brain continuity\n\n"
        f"Use the local `gbrain` MCP before guessing about {manifest['principal_name']}'s durable context, prior decisions, or indexed work. "
        "The tenant vault is the sole client-facing source of truth; local GBrain is its indexed, bounded write-through layer. "
        "Write durable client facts, decisions, and handoffs through GBrain when relevant. "
        "Raw transcripts, reasoning, tool payloads, credentials, URLs, and absolute paths remain local and must never be copied into an operator-wide brain."
    )


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
        provider = observed["providers"]["codex"]
        if provider["status"] == "absent":
            write_observation(manifest, {"status": "ready_no_providers", "capability_live": True, "providers": observed["providers"], "baseline": observed["baseline"]})
            return observed
        if not provider["logged_in"]:
            write_observation(manifest, {"status": "detected_not_enrolled", "capability_live": True, "providers": observed["providers"], "baseline": observed["baseline"]})
            return observed
        config, instructions = codex_paths(manifest)
        config_text = provider_text(config)
        instruction_text = provider_text(instructions)
        next_config = managed_block(config_text, CONFIG_START, CONFIG_END, codex_config_body(manifest), r"^\s*\[mcp_servers\.gbrain\]\s*$")
        next_instructions = managed_block(instruction_text, INSTRUCTION_START, INSTRUCTION_END, codex_instruction_body(manifest))
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
            write_observation(manifest, {"status": "verified", "capability_live": True, "providers": {"codex": {"status": "verified", "command": provider["command"], "receipt": str(receipt_path)}}, "baseline": observed["baseline"]})
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
