#!/usr/bin/env python3
"""Install and prove the tenant-local GBrain baseline for native-agent continuity."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA = "tenant-gbrain-baseline/v1"
MANIFEST_SCHEMA = "native-agent-continuity-manifest/v1"
SOURCE_ID = "tenant"


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


@contextlib.contextmanager
def process_lock(path: Path):
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
        raise RuntimeError("tenant GBrain baseline activation is already running") from exc
    try:
        yield
    finally:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    cwd: Path | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
        cwd=cwd,
        timeout=timeout,
    )


def platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def absolute(value: object, name: str) -> Path:
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
    principal = str(value.get("principal_id") or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", principal):
        raise RuntimeError("native-agent continuity principal_id is invalid")
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
    value["_paths"] = {
        name: absolute(paths.get(name), f"paths.{name}") for name in required
    }
    if value["_paths"]["home"].resolve() != Path.home().resolve():
        raise RuntimeError("native-agent continuity principal home does not match process owner")
    hermes_home = value["_paths"]["hermes_home"].resolve()
    try:
        hermes_home.relative_to(value["_paths"]["home"].resolve())
        for name in (
            "vault",
            "gbrain_home",
            "gbrain",
            "mcp_server",
            "lock_file",
            "baseline_receipt",
        ):
            value["_paths"][name].resolve().relative_to(hermes_home)
    except ValueError as exc:
        raise RuntimeError("native-agent continuity managed paths escape the principal runtime") from exc
    if value["_paths"]["baseline_receipt"] != (
        value["_paths"]["hermes_home"]
        / "state"
        / "native-agent-continuity"
        / "baseline.json"
    ):
        raise RuntimeError("native-agent continuity baseline receipt path is not canonical")
    return value


def load_contract(path: Path) -> dict[str, Any]:
    value = json.loads(regular(path).decode("utf-8"))
    release = value.get("gbrain_release") if isinstance(value, dict) else None
    if (
        value.get("capability") != "native-agent-continuity"
        or not isinstance(release, dict)
        or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(release.get("version") or ""))
        or not re.fullmatch(r"[0-9a-f]{40}", str(release.get("commit") or ""))
    ):
        raise RuntimeError("native-agent continuity GBrain release contract is invalid")
    assets = release.get("assets")
    if not isinstance(assets, dict) or any(
        not isinstance(assets.get(key), dict)
        or not str(assets[key].get("url") or "").startswith(
            f"https://github.com/garrytan/gbrain/releases/download/{release['tag']}/"
        )
        or not re.fullmatch(r"[0-9a-f]{64}", str(assets[key].get("sha256") or ""))
        for key in ("macos_arm64", "linux_x64")
    ):
        raise RuntimeError("native-agent continuity GBrain asset contract is invalid")
    return value


def gbrain_env(manifest: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["GBRAIN_HOME"] = str(manifest["_paths"]["gbrain_home"])
    env["GBRAIN_VAULT"] = str(manifest["_paths"]["vault"])
    env["NO_COLOR"] = "1"
    return env


def version_proof(binary: Path, expected: str, env: dict[str, str]) -> str:
    result = run([str(binary), "--version"], env=env, timeout=30)
    output = (result.stdout or result.stderr).strip()
    if result.returncode or expected not in output or "gbrain" not in output.lower():
        raise RuntimeError("tenant GBrain version proof failed")
    return output[:200]


def persistent_owners(binary: Path) -> list[str]:
    needle = str(binary).lower()
    commands: list[str] = []
    if os.name == "nt":
        result = run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine",
            ],
            timeout=60,
        )
    else:
        result = run(["ps", "-axo", "command="], timeout=30)
    if result.returncode:
        raise RuntimeError("persistent GBrain owner inventory failed")
    long_lived = re.compile(r"\b(serve|sync\s+--watch|jobs\s+work|autopilot)\b", re.I)
    for line in result.stdout.splitlines():
        lowered = line.lower()
        if needle in lowered and long_lived.search(line):
            commands.append(sha256_bytes(line.encode()))
    return sorted(set(commands))


def snapshot_binary(path: Path, backup_root: Path) -> dict[str, Any]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("tenant GBrain binary target is unsafe")
    existed = path.exists()
    before = path.read_bytes() if existed else b""
    backup = backup_root / "gbrain.before"
    if existed:
        atomic_bytes(backup, before, 0o700)
    return {
        "path": str(path),
        "existed": existed,
        "before_sha256": sha256_bytes(before) if existed else None,
        "backup": str(backup) if existed else None,
        "after_sha256": None,
    }


def _install_release(
    manifest: dict[str, Any], contract: dict[str, Any], record: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    paths = manifest["_paths"]
    binary = paths["gbrain"]
    release = contract["gbrain_release"]
    expected_version = str(release["version"])
    env = gbrain_env(manifest)
    try:
        current = version_proof(binary, expected_version, env) if binary.exists() else ""
    except RuntimeError:
        current = ""
    if current:
        record["after_sha256"] = sha256_bytes(regular(binary))
        return record, {"method": "preexisting_verified", "version": current}
    binary.parent.mkdir(parents=True, exist_ok=True)
    if platform_key() in {"macos", "linux"}:
        arch = platform.machine().lower()
        key = "macos_arm64" if platform_key() == "macos" and arch == "arm64" else "linux_x64" if platform_key() == "linux" and arch in {"x86_64", "amd64"} else ""
        asset = (release.get("assets") or {}).get(key)
        if not key or not isinstance(asset, dict):
            raise RuntimeError("no verified GBrain binary asset for this platform")
        with urllib.request.urlopen(str(asset["url"]), timeout=120) as response:
            payload = response.read(300_000_000)
        if sha256_bytes(payload) != asset.get("sha256"):
            raise RuntimeError("downloaded GBrain binary digest mismatch")
        atomic_bytes(binary, payload, 0o700)
        provenance = {"method": "official_release_asset", "asset": asset["name"]}
    else:
        git = shutil.which("git")
        bun = shutil.which("bun")
        if not git or not bun:
            raise RuntimeError("Windows GBrain source build requires existing git and bun")
        with tempfile.TemporaryDirectory(prefix="tenant-gbrain-build-") as temp:
            source = Path(temp) / "gbrain"
            clone = run(
                [git, "clone", "--depth", "1", "--branch", release["tag"], release["repo"], str(source)],
                timeout=300,
            )
            if clone.returncode:
                raise RuntimeError("pinned GBrain source clone failed")
            head = run([git, "rev-parse", "HEAD"], cwd=source, timeout=30)
            if head.returncode or head.stdout.strip() != release["commit"]:
                raise RuntimeError("pinned GBrain source identity mismatch")
            install = run([bun, "install", "--frozen-lockfile"], cwd=source, timeout=600)
            if install.returncode:
                raise RuntimeError("pinned GBrain dependency install failed")
            temporary = binary.with_name(f".{binary.name}.{secrets.token_hex(8)}.tmp.exe")
            build = run(
                [bun, "build", "--compile", "--outfile", str(temporary), "src/cli.ts"],
                cwd=source,
                timeout=600,
            )
            if build.returncode or not temporary.is_file():
                temporary.unlink(missing_ok=True)
                raise RuntimeError("pinned GBrain Windows build failed")
            os.replace(temporary, binary)
        provenance = {
            "method": "pinned_source_build",
            "commit": release["commit"],
            "bun": run([bun, "--version"], timeout=30).stdout.strip()[:100],
        }
    version = version_proof(binary, expected_version, env)
    record["after_sha256"] = sha256_bytes(regular(binary))
    return record, {**provenance, "version": version}


def install_release(
    manifest: dict[str, Any], contract: dict[str, Any], backup_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    binary = manifest["_paths"]["gbrain"]
    record = snapshot_binary(binary, backup_root)
    try:
        return _install_release(manifest, contract, record)
    except Exception:
        if record["existed"]:
            atomic_bytes(binary, regular(Path(record["backup"])), 0o700)
        else:
            binary.unlink(missing_ok=True)
        raise


def facade_call(
    manifest: dict[str, Any], method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    paths = manifest["_paths"]
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    env = gbrain_env(manifest)
    env["GBRAIN_PRINCIPAL_NAME"] = str(manifest["principal_name"])
    paths["lock_file"].parent.mkdir(parents=True, exist_ok=True)
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
        cwd=paths["vault"],
        stdin=json.dumps(request) + "\n",
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError("tenant GBrain MCP proof failed")
    for line in result.stdout.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(line)
            if isinstance(payload, dict) and payload.get("id") == 1:
                if payload.get("error"):
                    raise RuntimeError("tenant GBrain MCP returned an error")
                return payload
    raise RuntimeError("tenant GBrain MCP response is missing")


def ensure_vault_repo(vault: Path) -> dict[str, Any]:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("tenant GBrain vault initialization requires existing git")
    vault.mkdir(parents=True, exist_ok=True)
    dot_git = vault / ".git"
    if not dot_git.exists():
        if any(vault.iterdir()):
            raise RuntimeError("refusing to Git-initialize a non-empty unowned tenant vault")
        atomic_bytes(
            vault / "README.md",
            b"# Client Brain\n\nTenant-local durable context for this client runtime.\n",
            0o600,
        )
        atomic_bytes(vault / ".gitignore", b".DS_Store\n", 0o600)
        initialized = run([git, "init", "--initial-branch", "main"], cwd=vault, timeout=60)
        if initialized.returncode:
            raise RuntimeError("tenant vault Git initialization failed")
        for key, setting in (
            ("user.name", "Bot Doctor Continuity"),
            ("user.email", "continuity@local.invalid"),
        ):
            configured = run([git, "config", key, setting], cwd=vault, timeout=30)
            if configured.returncode:
                raise RuntimeError("tenant vault local Git identity setup failed")
        staged = run([git, "add", "README.md", ".gitignore"], cwd=vault, timeout=30)
        committed = run(
            [git, "commit", "-m", "Initialize tenant brain"], cwd=vault, timeout=60
        )
        if staged.returncode or committed.returncode:
            raise RuntimeError("tenant vault initial commit failed")
        created = True
    else:
        if dot_git.is_symlink() or not dot_git.is_dir():
            raise RuntimeError("tenant vault Git metadata is unsafe")
        created = False
    head = run([git, "rev-parse", "HEAD"], cwd=vault, timeout=30)
    status = run([git, "status", "--porcelain=v1"], cwd=vault, timeout=30)
    if head.returncode or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()):
        raise RuntimeError("tenant vault has no committed Git identity")
    if status.returncode or status.stdout.strip():
        raise RuntimeError("tenant vault is dirty before GBrain activation")
    return {"created": created, "head": head.stdout.strip()}


def initialize_brain(manifest: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    paths = manifest["_paths"]
    env = gbrain_env(manifest)
    vault_repo = ensure_vault_repo(paths["vault"])
    paths["gbrain_home"].mkdir(parents=True, exist_ok=True)
    init = run(
        [str(paths["gbrain"]), "init", "--pglite", "--no-embedding"],
        env=env,
        cwd=paths["vault"],
        timeout=300,
    )
    if init.returncode:
        raise RuntimeError("tenant GBrain PGLite initialization failed")
    listed = run(
        [str(paths["gbrain"]), "sources", "list", "--json"],
        env=env,
        cwd=paths["vault"],
        timeout=90,
    )
    try:
        sources = json.loads(listed.stdout).get("sources") if not listed.returncode else None
    except json.JSONDecodeError:
        sources = None
    if not isinstance(sources, list):
        raise RuntimeError("tenant GBrain source inventory failed")
    tenant = next((row for row in sources if row.get("id") == SOURCE_ID), None)
    if tenant is None:
        added = run(
            [str(paths["gbrain"]), "sources", "add", SOURCE_ID, "--path", str(paths["vault"])],
            env=env,
            cwd=paths["vault"],
            timeout=120,
        )
        if added.returncode:
            detail = (added.stdout + added.stderr).strip().splitlines()
            raise RuntimeError(
                "tenant GBrain vault registration failed: "
                + (detail[-1][:300] if detail else "no command detail")
            )
    elif Path(str(tenant.get("local_path") or "")).resolve() != paths["vault"].resolve():
        raise RuntimeError("tenant GBrain source points at a different vault")
    tools = ((facade_call(manifest, "tools/list").get("result") or {}).get("tools") or [])
    expected_tools = contract["mcp_tools"]
    if [row.get("name") for row in tools if isinstance(row, dict)] != expected_tools:
        raise RuntimeError("tenant GBrain MCP tool contract drifted")
    token = f"TENANT_GBRAIN_BASELINE_{secrets.token_hex(8).upper()}"
    slug = f"proofs/native-agent-continuity/{manifest['principal_id']}/baseline"
    content = f"---\ntype: proof\nsource: tenant-gbrain-baseline\n---\n\n# Tenant GBrain baseline\n\n{token}\n"
    value = facade_call(
        manifest,
        "tools/call",
        {"name": "put_page", "arguments": {"slug": slug, "content": content}},
    )
    result = value.get("result") or {}
    if result.get("isError") is not False:
        raise RuntimeError("tenant GBrain baseline put proof failed")
    readback = run(
        [str(paths["gbrain"]), "get", slug],
        env=env,
        cwd=paths["vault"],
        timeout=90,
    )
    proof_file = paths["vault"] / f"{slug}.md"
    if readback.returncode or token not in readback.stdout or token not in regular(proof_file).decode("utf-8"):
        raise RuntimeError("tenant GBrain vault write-through proof failed")
    git = shutil.which("git")
    head = run([str(git), "rev-parse", "HEAD"], cwd=paths["vault"], timeout=30) if git else None
    status = run([str(git), "status", "--porcelain=v1"], cwd=paths["vault"], timeout=30) if git else None
    if (
        head is None
        or status is None
        or head.returncode
        or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip())
        or status.returncode
        or status.stdout.strip()
    ):
        raise RuntimeError("tenant GBrain proof was not committed cleanly to the tenant vault")
    return {
        "engine": "pglite",
        "embedding_disabled": True,
        "source_id": SOURCE_ID,
        "vault_git": vault_repo,
        "vault": str(paths["vault"].resolve()),
        "mcp_tools": expected_tools,
        "proof_slug": slug,
        "proof_file": str(proof_file),
        "proof_sha256": sha256_bytes(regular(proof_file)),
        "token_sha256": sha256_bytes(token.encode()),
        "vault_head_after": head.stdout.strip(),
    }


def _apply(manifest: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    paths = manifest["_paths"]
    receipt_path = paths["baseline_receipt"]
    if receipt_path.is_file():
        existing = json.loads(regular(receipt_path).decode("utf-8"))
        if existing.get("schema") == SCHEMA and existing.get("status") == "verified":
            return verify(manifest, contract, existing)
        if existing.get("schema") != SCHEMA or existing.get("status") != "rollback_verified":
            raise RuntimeError("unverified tenant GBrain baseline receipt already exists")
    if paths["gbrain_home"].exists():
        raise RuntimeError("unreceipted tenant GBrain home already exists")
    rollout = receipt_path.parent / "receipts" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
    binary_record, release = install_release(manifest, contract, rollout / "backups")
    receipt = {
        "schema": SCHEMA,
        "status": "applying",
        "principal_id": manifest["principal_id"],
        "started_at": iso(),
        "binary": binary_record,
        "release": release,
        "persistent_gbrain_owners": persistent_owners(paths["gbrain"]),
    }
    transaction = rollout / "receipt.json"
    atomic_json(transaction, receipt)
    try:
        if receipt["persistent_gbrain_owners"]:
            raise RuntimeError("persistent GBrain owner exists before baseline activation")
        baseline = initialize_brain(manifest, contract)
        owners = persistent_owners(paths["gbrain"])
        if owners:
            raise RuntimeError("persistent GBrain owner remains after baseline activation")
        receipt.update(
            {
                "status": "verified",
                "verified_at": iso(),
                "persistent_gbrain_owners": 0,
                "baseline": baseline,
                "transaction_receipt": str(transaction),
            }
        )
        atomic_json(transaction, receipt)
        atomic_json(receipt_path, receipt)
        return receipt
    except Exception as exc:
        receipt.update({"status": "failed", "failed_at": iso(), "error": f"{type(exc).__name__}: {str(exc)[:800]}"})
        atomic_json(transaction, receipt)
        if paths["gbrain_home"].exists():
            os.replace(paths["gbrain_home"], rollout / "failed-gbrain-home")
        binary = paths["gbrain"]
        if binary_record["after_sha256"] and binary.exists() and sha256_bytes(regular(binary)) == binary_record["after_sha256"]:
            if binary_record["existed"]:
                atomic_bytes(binary, regular(Path(binary_record["backup"])), 0o700)
            else:
                binary.unlink()
        receipt["rollback_status"] = "baseline_quarantined_binary_restored"
        atomic_json(transaction, receipt)
        raise


def apply(manifest: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    root = manifest["_paths"]["hermes_home"] / "state" / "native-agent-continuity"
    with process_lock(root / "baseline.lock"):
        return _apply(manifest, contract)


def verify(manifest: dict[str, Any], contract: dict[str, Any], receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = manifest["_paths"]
    receipt = receipt or json.loads(regular(paths["baseline_receipt"]).decode("utf-8"))
    if receipt.get("schema") != SCHEMA or receipt.get("status") != "verified":
        raise RuntimeError("tenant GBrain baseline receipt is not verified")
    version_proof(paths["gbrain"], contract["gbrain_release"]["version"], gbrain_env(manifest))
    if persistent_owners(paths["gbrain"]):
        raise RuntimeError("tenant GBrain has a persistent owner")
    proof = receipt.get("baseline") or {}
    if sha256_bytes(regular(Path(str(proof.get("proof_file") or "")))) != proof.get("proof_sha256"):
        raise RuntimeError("tenant GBrain write-through proof drifted")
    tools = ((facade_call(manifest, "tools/list").get("result") or {}).get("tools") or [])
    if [row.get("name") for row in tools if isinstance(row, dict)] != contract["mcp_tools"]:
        raise RuntimeError("tenant GBrain MCP verification drifted")
    return {**receipt, "verification": "pass"}


def rollback(manifest: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    paths = manifest["_paths"]
    receipt_path = paths["baseline_receipt"]
    with process_lock(receipt_path.parent / "baseline.lock"):
        receipt = json.loads(regular(receipt_path).decode("utf-8"))
        if receipt.get("schema") != SCHEMA or receipt.get("status") != "verified":
            raise RuntimeError("tenant GBrain baseline receipt is not rollback-eligible")
        if persistent_owners(paths["gbrain"]):
            raise RuntimeError("tenant GBrain rollback refuses a persistent owner")
        baseline = receipt.get("baseline") or {}
        vault_git = baseline.get("vault_git") or {}
        before_head = str(vault_git.get("head") or "")
        after_head = str(baseline.get("vault_head_after") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", before_head) or not re.fullmatch(
            r"[0-9a-f]{40}", after_head
        ):
            raise RuntimeError("tenant GBrain rollback Git receipt is invalid")
        proof_file = Path(str(baseline.get("proof_file") or ""))
        if (
            proof_file.is_symlink()
            or not proof_file.is_file()
            or sha256_bytes(regular(proof_file)) != baseline.get("proof_sha256")
        ):
            raise RuntimeError("tenant GBrain rollback proof has advanced or disappeared")
        git = shutil.which("git")
        if not git:
            raise RuntimeError("tenant GBrain rollback requires existing git")
        current = run([git, "rev-parse", "HEAD"], cwd=paths["vault"], timeout=30)
        parent = run([git, "rev-parse", f"{after_head}^"], cwd=paths["vault"], timeout=30)
        status = run([git, "status", "--porcelain=v1"], cwd=paths["vault"], timeout=30)
        if (
            current.returncode
            or current.stdout.strip() != after_head
            or parent.returncode
            or parent.stdout.strip() != before_head
            or status.returncode
            or status.stdout.strip()
        ):
            raise RuntimeError("tenant GBrain rollback refuses an advanced or dirty vault")
        binary_record = receipt.get("binary") or {}
        binary = paths["gbrain"]
        if (
            binary.is_symlink()
            or not binary.is_file()
            or sha256_bytes(regular(binary)) != binary_record.get("after_sha256")
        ):
            raise RuntimeError("tenant GBrain rollback refuses an advanced binary")
        rollout = receipt_path.parent / "receipts" / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}-rollback"
        )
        rollout.mkdir(parents=True, exist_ok=False)
        quarantine = rollout / "rolled-back-gbrain-home"
        moved = False
        try:
            if paths["gbrain_home"].is_symlink() or not paths["gbrain_home"].is_dir():
                raise RuntimeError("tenant GBrain home is missing or unsafe")
            os.replace(paths["gbrain_home"], quarantine)
            moved = True
            reverted = run(
                [git, "revert", "--no-edit", after_head], cwd=paths["vault"], timeout=60
            )
            if reverted.returncode or proof_file.exists():
                raise RuntimeError("tenant GBrain vault proof revert failed")
            if binary_record.get("existed"):
                backup = Path(str(binary_record.get("backup") or ""))
                if sha256_bytes(regular(backup)) != binary_record.get("before_sha256"):
                    raise RuntimeError("tenant GBrain binary rollback backup drifted")
                atomic_bytes(binary, regular(backup), 0o700)
            else:
                binary.unlink()
        except Exception:
            if moved and quarantine.exists() and not paths["gbrain_home"].exists():
                os.replace(quarantine, paths["gbrain_home"])
            raise
        receipt.update(
            {
                "status": "rollback_verified",
                "rolled_back_at": iso(),
                "rollback_receipt": str(rollout / "receipt.json"),
                "quarantined_gbrain_home": str(quarantine),
            }
        )
        atomic_json(rollout / "receipt.json", receipt)
        atomic_json(receipt_path, receipt)
        transaction = Path(str(receipt.get("transaction_receipt") or ""))
        if transaction.is_file() and not transaction.is_symlink():
            atomic_json(transaction, receipt)
        return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")
    for name in ("apply", "verify", "rollback"):
        child = sub.add_parser(name)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--contract", type=Path, required=True)
        child.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            value = {"status": "pass", "schema": SCHEMA, "platform": platform_key()}
        elif args.command in {"apply", "verify", "rollback"}:
            manifest = load_manifest(args.manifest)
            contract = load_contract(args.contract)
            if args.command == "apply":
                value = apply(manifest, contract)
            elif args.command == "verify":
                value = verify(manifest, contract)
            else:
                value = rollback(manifest, contract)
        else:
            parser.error("apply, verify, rollback, or --self-test is required")
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - operator-facing CLI boundary
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
