#!/usr/bin/env python3
"""Install deploy-time host artifacts from a Hermes golden payload.

This is deliberately separate from patches/apply-all-patches.py. The patch
registry is frozen for source patches; this lane installs host artifacts that
ship in the deploy payload and writes its own receipts.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_INSTALLED = "INSTALLED"
STATUS_IDEMPOTENT = "IDEMPOTENT"
STATUS_SKIPPED = "SKIPPED"
STATUS_ANCHOR_MISS = "ANCHOR-MISS"
STATUS_FAILED = "FAILED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised in bare envs
        raise SystemExit(
            "PyYAML is required to read installers.yaml. Install with: python -m pip install pyyaml"
        ) from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a YAML mapping")
    return data


def platform_key() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system.startswith("win"):
        return "windows"
    return "linux"


def hermes_home_from(hermes_dir: Path | None) -> Path:
    # Explicit --hermes-dir wins over an ambient HERMES_HOME env var:
    # a caller that names the target must not be overridden by process env.
    if hermes_dir is not None:
        if hermes_dir.name == "hermes-agent":
            return hermes_dir.parent
        return hermes_dir.parent
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser()
    return Path.home() / ".hermes"


def expand_value(value: str, *, repo: Path, hermes_home: Path, hermes_dir: Path) -> Path:
    rendered = value.format(
        repo=repo,
        home=Path.home(),
        hermes_home=hermes_home,
        hermes_agent=hermes_dir,
    )
    return Path(rendered).expanduser()


def source_path(value: str, repo: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo / path


def resolve_hermes_python(hermes_dir: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    candidates = (
        hermes_dir / "venv" / "bin" / "python",
        hermes_dir / ".venv" / "bin" / "python",
        hermes_dir.parent / "venv" / "bin" / "python",
        hermes_dir.parent / ".venv" / "bin" / "python",
        hermes_dir / "venv" / "Scripts" / "python.exe",
        hermes_dir / ".venv" / "Scripts" / "python.exe",
        hermes_dir.parent / "venv" / "Scripts" / "python.exe",
        hermes_dir.parent / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(
        f"could not resolve the exact Hermes runtime Python for {hermes_dir}; "
        "pass --hermes-python"
    )


def set_mode(path: Path, mode: Any, source: Path | None = None) -> None:
    if mode in (None, "preserve_existing", "preserve"):
        return
    try:
        if mode == "preserve_source" and source is not None:
            shutil.copymode(source, path)
        elif isinstance(mode, str):
            path.chmod(int(mode, 8))
    except Exception:
        pass


def backup_path(path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.bak-host-artifact-{ts}")


def copy_file(src: Path, dst: Path, *, mode: Any, dry_run: bool) -> tuple[str, dict[str, Any]]:
    src_bytes = src.read_bytes()
    src_sha = sha256_bytes(src_bytes)
    if dst.exists() and sha256_bytes(dst.read_bytes()) == src_sha:
        return STATUS_IDEMPOTENT, {"source_sha256": src_sha, "destination": str(dst)}
    before_sha = sha256_bytes(dst.read_bytes()) if dst.exists() else None
    backup = str(backup_path(dst)) if dst.exists() else None
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.copy2(dst, backup)
        dst.write_bytes(src_bytes)
        set_mode(dst, mode, src)
    return STATUS_INSTALLED, {
        "source_sha256": src_sha,
        "previous_sha256": before_sha,
        "destination": str(dst),
        "backup": backup,
    }


def discover_codex_wrappers() -> list[Path]:
    candidates: list[Path] = []

    def add(path: Path | None) -> None:
        if not path:
            return
        if path.suffix.lower() in {".ps1", ".cmd", ".bat"}:
            return
        try:
            if path.is_symlink():
                path = Path(os.path.realpath(path))
        except Exception:
            pass
        if path.exists() and path not in candidates:
            candidates.append(path)

    codex = shutil.which("codex")
    if codex:
        shim = Path(codex)
        if shim.suffix.lower() in {".ps1", ".cmd", ".bat"}:
            try:
                text = shim.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                text = ""
            if "node_modules/@openai/codex/bin/codex.js" in text:
                add(shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
        else:
            add(Path(os.path.realpath(codex)))
            add(shim)
    appdata = os.environ.get("APPDATA")
    if appdata:
        add(Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    add(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    add(Path("/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js"))
    add(Path("/usr/local/lib/node_modules/@openai/codex/bin/codex.js"))
    add(Path("/usr/lib/node_modules/@openai/codex/bin/codex.js"))
    return candidates


def check_post(check: dict[str, Any] | None, *, repo: Path, hermes_home: Path, hermes_dir: Path) -> bool:
    if not check:
        return True
    kind = check.get("type")
    if kind == "executable":
        path = expand_value(check["path"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
        return path.is_file() and os.access(path, os.X_OK)
    if kind == "contains":
        path = expand_value(check["path"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
        return path.is_file() and check.get("text", "") in path.read_text(encoding="utf-8", errors="ignore")
    if kind == "files_exist":
        return all(
            expand_value(p, repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir).is_file()
            for p in check.get("paths", [])
        )
    if kind == "state_file_exists":
        path = expand_value(check["path"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
        return path.is_file()
    if kind == "contains_discovered":
        text = check.get("text", "")
        return any(text in path.read_text(encoding="utf-8", errors="ignore") for path in discover_codex_wrappers())
    return True


def apply_insert(
    entry: dict[str, Any],
    *,
    repo: Path,
    hermes_home: Path,
    hermes_dir: Path,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    dst = expand_value(entry["destination"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
    if entry.get("existing_only") and not dst.exists():
        return STATUS_SKIPPED, {"reason": "destination missing", "destination": str(dst)}
    text = dst.read_text(encoding="utf-8")
    marker = entry.get("already_present")
    if marker and marker in text:
        return STATUS_IDEMPOTENT, {"destination": str(dst)}
    anchor = entry["anchor_text"]
    if anchor not in text:
        return STATUS_ANCHOR_MISS, {"reason": "anchor not found", "destination": str(dst), "anchor": anchor}
    fragment = source_path(entry["source"], repo).read_text(encoding="utf-8").rstrip() + "\n\n"
    new_text = text.replace(anchor, fragment + anchor, 1)
    backup = str(backup_path(dst))
    if not dry_run:
        shutil.copy2(dst, backup)
        dst.write_text(new_text, encoding="utf-8")
        set_mode(dst, entry.get("mode"), None)
        if dst.suffix == ".sh":
            subprocess.run(["bash", "-n", str(dst)], check=True, capture_output=True, text=True)
    return STATUS_INSTALLED, {"destination": str(dst), "backup": backup}


def apply_replace_block(
    entry: dict[str, Any],
    *,
    repo: Path,
    hermes_home: Path,
    hermes_dir: Path,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    dst = expand_value(entry["destination"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
    if entry.get("existing_only") and not dst.exists():
        return STATUS_SKIPPED, {"reason": "destination missing", "destination": str(dst)}
    text = dst.read_text(encoding="utf-8")
    marker = entry.get("already_present")
    if marker and marker in text:
        return STATUS_IDEMPOTENT, {"destination": str(dst)}
    anchor = entry["anchor_text"]
    start = text.find(anchor)
    if start < 0:
        return STATUS_ANCHOR_MISS, {"reason": "anchor not found", "destination": str(dst), "anchor": anchor}
    end = text.find("\n}\n", start)
    if end < 0:
        return STATUS_ANCHOR_MISS, {"reason": "block end not found", "destination": str(dst)}
    end += len("\n}\n")
    fragment = source_path(entry["source"], repo).read_text(encoding="utf-8").rstrip() + "\n"
    new_text = text[:start] + fragment + text[end:]
    backup = str(backup_path(dst))
    if not dry_run:
        shutil.copy2(dst, backup)
        dst.write_text(new_text, encoding="utf-8")
        set_mode(dst, entry.get("mode"), None)
        if dst.suffix == ".sh":
            subprocess.run(["bash", "-n", str(dst)], check=True, capture_output=True, text=True)
    return STATUS_INSTALLED, {"destination": str(dst), "backup": backup}


def apply_ensure_cua_driver(
    entry: dict[str, Any],
    *,
    repo: Path,
    hermes_home: Path,
    hermes_dir: Path,
    hermes_python: Path | None,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    helper = source_path(entry["helper"], repo)
    contract = source_path(entry["contract"], repo)
    if not helper.is_file() or not contract.is_file():
        missing = [str(path) for path in (helper, contract) if not path.is_file()]
        return STATUS_FAILED, {"reason": "driver payload missing", "missing": missing}
    python = resolve_hermes_python(hermes_dir, hermes_python)
    command = [
        sys.executable,
        str(helper),
        "--hermes-python",
        str(python),
        "--hermes-home",
        str(hermes_home),
        "--contract",
        str(contract),
    ]
    if dry_run:
        command.append("--dry-run")
    if entry.get("require_ready") is True:
        command.append("--require-ready")
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(entry.get("timeout_seconds", 960)),
        check=False,
    )
    try:
        receipt = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return STATUS_FAILED, {
            "reason": "driver helper returned invalid JSON",
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[-500:],
        }
    helper_status = receipt.get("status")
    if proc.returncode != 0 or receipt.get("ok") is not True:
        status = STATUS_FAILED
    elif helper_status in {"idempotent"}:
        status = STATUS_IDEMPOTENT
    elif helper_status == "would_install":
        status = STATUS_SKIPPED
    else:
        status = STATUS_INSTALLED
    return status, {
        "hermes_python": str(python),
        "driver_receipt": receipt,
    }


def apply_ensure_gateway_watchdog(
    entry: dict[str, Any],
    *,
    repo: Path,
    hermes_home: Path,
    hermes_dir: Path,
    hermes_python: Path | None,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    helper = source_path(entry["helper"], repo)
    source = source_path(entry["source"], repo)
    if not helper.is_file() or not source.is_file():
        missing = [str(path) for path in (helper, source) if not path.is_file()]
        return STATUS_FAILED, {"reason": "watchdog payload missing", "missing": missing}
    python = resolve_hermes_python(hermes_dir, hermes_python)
    command = [
        str(python),
        str(helper),
        "--source",
        str(source),
        "--hermes-home",
        str(hermes_home),
        "--hermes-python",
        str(python),
    ]
    if dry_run:
        command.append("--dry-run")
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(entry.get("timeout_seconds", 120)),
        check=False,
    )
    try:
        receipt = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return STATUS_FAILED, {
            "reason": "watchdog helper returned invalid JSON",
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[-500:],
        }
    helper_status = receipt.get("status")
    if proc.returncode != 0 or receipt.get("ok") is not True:
        status = STATUS_FAILED
    elif helper_status == "idempotent":
        status = STATUS_IDEMPOTENT
    elif helper_status == "would_install":
        status = STATUS_SKIPPED
    else:
        status = STATUS_INSTALLED
    return status, {
        "hermes_python": str(python),
        "watchdog_receipt": receipt,
    }


def apply_retire_legacy_memory_surfaces(
    entry: dict[str, Any],
    *,
    repo: Path,
    hermes_home: Path,
    hermes_dir: Path,
    hermes_python: Path | None,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    helper = source_path(entry["helper"], repo)
    rules_source = source_path(entry["rules_source"], repo)
    if not helper.is_file() or not rules_source.is_dir():
        missing = [str(path) for path in (helper, rules_source) if not path.exists()]
        return STATUS_FAILED, {"reason": "retired-memory payload missing", "missing": missing}
    python = resolve_hermes_python(hermes_dir, hermes_python)
    command = [
        str(python),
        str(helper),
        "--hermes-home",
        str(hermes_home),
        "--rules-source",
        str(rules_source),
        "--dry-run" if dry_run else "--apply",
    ]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(entry.get("timeout_seconds", 120)),
        check=False,
    )
    try:
        receipt = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return STATUS_FAILED, {
            "reason": "retired-memory helper returned invalid JSON",
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[-500:],
        }
    helper_status = receipt.get("status")
    if proc.returncode != 0 or receipt.get("ok") is not True:
        status = STATUS_FAILED
    elif helper_status == "idempotent":
        status = STATUS_IDEMPOTENT
    elif helper_status == "would_update":
        status = STATUS_SKIPPED
    else:
        status = STATUS_INSTALLED
    return status, {
        "hermes_python": str(python),
        "retired_memory_receipt": receipt,
    }


def apply_enforce_gbrain_provider_routing(
    entry: dict[str, Any],
    *,
    repo: Path,
    hermes_python: Path | None,
    hermes_dir: Path,
    hermes_home: Path,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    helper = source_path(entry["helper"], repo)
    if not helper.is_file():
        return STATUS_FAILED, {"reason": "GBrain provider helper missing", "missing": str(helper)}
    python = resolve_hermes_python(hermes_dir, hermes_python)
    command = [
        str(python),
        str(helper),
        "--hermes-home",
        str(hermes_home),
        "--dry-run" if dry_run else "--apply",
    ]
    env = os.environ.copy()
    env.pop("ZEROENTROPY_API_KEY", None)
    env.pop("HERMES_ZEROENTROPY_API_KEY", None)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(entry.get("timeout_seconds", 120)),
        check=False,
        env=env,
    )
    try:
        receipt = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return STATUS_FAILED, {
            "reason": "GBrain provider helper returned invalid JSON",
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[-500:],
        }
    helper_status = receipt.get("status")
    if proc.returncode != 0 or receipt.get("ok") is not True:
        status = STATUS_FAILED
    elif helper_status in {"compliant", "idempotent"}:
        status = STATUS_IDEMPOTENT
    elif helper_status in {"would_update", "not_applicable"}:
        status = STATUS_SKIPPED
    else:
        status = STATUS_INSTALLED
    return status, {"hermes_python": str(python), "gbrain_provider_receipt": receipt}


def apply_entry(
    entry: dict[str, Any],
    *,
    repo: Path,
    hermes_home: Path,
    hermes_dir: Path,
    hermes_python: Path | None,
    current_platform: str,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    if current_platform not in entry.get("platforms", []):
        return STATUS_SKIPPED, {"reason": f"platform {current_platform} not selected"}
    op = entry["operation"]
    if op == "copy":
        dst = expand_value(entry["destination"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
        if entry.get("existing_only") and not dst.exists():
            return STATUS_SKIPPED, {"reason": "destination missing", "destination": str(dst)}
        return copy_file(source_path(entry["source"], repo), dst, mode=entry.get("mode"), dry_run=dry_run)
    if op == "copy_many":
        results = []
        changed = False
        for src_value in entry["sources"]:
            src = source_path(src_value, repo)
            dst = (
                expand_value(entry["destination_dir"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
                / src.name
            )
            status, meta = copy_file(src, dst, mode=entry.get("mode"), dry_run=dry_run)
            changed = changed or status == STATUS_INSTALLED
            results.append({"source": str(src), "status": status, **meta})
        return (STATUS_INSTALLED if changed else STATUS_IDEMPOTENT), {"files": results}
    if op == "copy_discovered":
        targets = discover_codex_wrappers()
        if not targets:
            return STATUS_SKIPPED, {"reason": "codex wrapper not found"}
        src = source_path(entry["source"], repo)
        results = []
        changed = False
        for dst in targets:
            status, meta = copy_file(src, dst, mode=entry.get("mode"), dry_run=dry_run)
            changed = changed or status == STATUS_INSTALLED
            results.append({"status": status, **meta})
        return (STATUS_INSTALLED if changed else STATUS_IDEMPOTENT), {"files": results}
    if op == "insert_before":
        return apply_insert(entry, repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir, dry_run=dry_run)
    if op == "replace_block":
        return apply_replace_block(entry, repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir, dry_run=dry_run)
    if op == "ensure_cua_driver":
        return apply_ensure_cua_driver(
            entry,
            repo=repo,
            hermes_home=hermes_home,
            hermes_dir=hermes_dir,
            hermes_python=hermes_python,
            dry_run=dry_run,
        )
    if op == "ensure_gateway_watchdog":
        return apply_ensure_gateway_watchdog(
            entry,
            repo=repo,
            hermes_home=hermes_home,
            hermes_dir=hermes_dir,
            hermes_python=hermes_python,
            dry_run=dry_run,
        )
    if op == "retire_legacy_memory_surfaces":
        return apply_retire_legacy_memory_surfaces(
            entry,
            repo=repo,
            hermes_home=hermes_home,
            hermes_dir=hermes_dir,
            hermes_python=hermes_python,
            dry_run=dry_run,
        )
    if op == "enforce_gbrain_provider_routing":
        return apply_enforce_gbrain_provider_routing(
            entry,
            repo=repo,
            hermes_python=hermes_python,
            hermes_dir=hermes_dir,
            hermes_home=hermes_home,
            dry_run=dry_run,
        )
    if op == "sync_glob":
        destination_dir = expand_value(
            entry["destination_dir"],
            repo=repo,
            hermes_home=hermes_home,
            hermes_dir=hermes_dir,
        )
        if not destination_dir.is_dir():
            return STATUS_SKIPPED, {"reason": "destination_dir missing", "destination_dir": str(destination_dir)}
        excludes: set[str] = set()
        exclude_file = source_path(entry.get("exclude_from_file", ""), repo) if entry.get("exclude_from_file") else None
        if exclude_file and exclude_file.exists():
            excludes = {
                line.strip()
                for line in exclude_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            }
        state_file = expand_value(entry["state_file"], repo=repo, hermes_home=hermes_home, hermes_dir=hermes_dir)
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        files = []
        changed = False
        for src_name in sorted(glob.glob(str(source_path(entry["source"], repo)))):
            src = Path(src_name)
            if not src.is_file() or src.name.startswith(".") or src.name in excludes or ".bak" in src.name:
                continue
            dst = destination_dir / src.name
            if entry.get("existing_only") and not dst.exists():
                files.append({"source": str(src), "status": STATUS_SKIPPED, "reason": "destination missing"})
                continue
            status, meta = copy_file(src, dst, mode=entry.get("mode"), dry_run=dry_run)
            changed = changed or status == STATUS_INSTALLED
            state[src.name] = {
                "canon_sha": meta.get("source_sha256"),
                "dst_sha": meta.get("source_sha256"),
                "status": status,
                "last_seen_at": utc_now(),
            }
            files.append({"source": str(src), "status": status, **meta})
        if not dry_run:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        return (STATUS_INSTALLED if changed else STATUS_IDEMPOTENT), {"files": files, "state_file": str(state_file)}
    raise ValueError(f"unsupported operation: {op}")


def write_receipt(
    receipt: dict[str, Any],
    hermes_home: Path,
    roster_key: str,
    filename: str,
) -> None:
    state_dir = hermes_home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / filename).write_text(
        json.dumps(receipt, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    receipts_dir = state_dir / "host-artifact-receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{roster_key}.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--hermes-dir",
        type=Path,
        default=None,
        help="Hermes agent directory, typically ~/.hermes/hermes-agent",
    )
    parser.add_argument(
        "--roster-key",
        default=None,
        help="Receipt roster key. Defaults to HERMES_ROSTER_KEY or hostname.",
    )
    parser.add_argument(
        "--hermes-python",
        type=Path,
        default=None,
        help="Exact candidate runtime Python. Auto-resolved only from --hermes-dir peers.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Install only named entry. May be passed multiple times.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    manifest_path = args.manifest or (repo / "installers.yaml")
    manifest = load_yaml(manifest_path)
    hermes_home = hermes_home_from(args.hermes_dir)
    hermes_dir = args.hermes_dir.expanduser() if args.hermes_dir else hermes_home / "hermes-agent"
    roster_key = (
        args.roster_key
        or os.environ.get(manifest.get("receipt", {}).get("roster_key_env", "HERMES_ROSTER_KEY"))
        or socket.gethostname()
    )
    current_platform = platform_key()
    selected = set(args.only)
    results = []
    failures = 0

    for entry in manifest.get("installers", []):
        if selected and entry.get("name") not in selected:
            continue
        try:
            status, meta = apply_entry(
                entry,
                repo=repo,
                hermes_home=hermes_home,
                hermes_dir=hermes_dir,
                hermes_python=args.hermes_python,
                current_platform=current_platform,
                dry_run=args.dry_run,
            )
            verified = args.dry_run or status == STATUS_SKIPPED or check_post(
                entry.get("post_install_check"),
                repo=repo,
                hermes_home=hermes_home,
                hermes_dir=hermes_dir,
            )
            if status in {STATUS_ANCHOR_MISS, STATUS_FAILED} or not verified:
                failures += 1
            results.append({"name": entry["name"], "status": status, "verified": verified, **meta})
        except Exception as exc:
            failures += 1
            results.append({"name": entry.get("name"), "status": STATUS_FAILED, "verified": False, "error": str(exc)})

    receipt = {
        "schema_version": 1,
        "kind": "host_artifact_install_receipt",
        "generated_at": utc_now(),
        "repo": str(repo),
        "manifest": str(manifest_path),
        "hermes_home": str(hermes_home),
        "hermes_dir": str(hermes_dir),
        "roster_key": roster_key,
        "platform": current_platform,
        "dry_run": args.dry_run,
        "results": results,
        "summary": {
            "total": len(results),
            "installed": sum(1 for r in results if r["status"] == STATUS_INSTALLED),
            "idempotent": sum(1 for r in results if r["status"] == STATUS_IDEMPOTENT),
            "skipped": sum(1 for r in results if r["status"] == STATUS_SKIPPED),
            "failures": failures,
        },
    }
    if not args.dry_run:
        write_receipt(
            receipt,
            hermes_home,
            roster_key,
            manifest.get("receipt", {}).get("filename", "last-installed-host-artifacts.json"),
        )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
