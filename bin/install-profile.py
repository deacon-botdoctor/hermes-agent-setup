#!/usr/bin/env python3
"""Install the public runtime's profile-owned files without switching services.

The command verifies the assembled runtime first, backs up every destination,
then installs only manifest-owned bins, plugins, hooks, rules, skills, and the
capability router. It preserves unrelated client-local files and never reads or
copies credential values.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELEASE = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
SOURCE_MANIFEST = json.loads(
    (ROOT / "runtime-payload-source-manifest.json").read_text(encoding="utf-8")
)
REQUIRED_PLUGINS = (
    "botdoctor-immersion",
    "mcp-on-demand-control",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def validate_profile_path(home: Path, path: Path) -> None:
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise RuntimeError(f"profile path escapes HERMES_HOME: {path}") from None
    if ".." in relative.parts:
        raise RuntimeError(f"profile path escapes HERMES_HOME: {path}")
    current = home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"profile path contains a symlink: {current}")
        if _is_reparse_point(current):
            raise RuntimeError(
                f"profile path contains a reparse point: {current}"
            )
        if current != path and current.exists() and not current.is_dir():
            raise RuntimeError(f"profile path parent is not a directory: {current}")
    resolved_parent = path.parent.resolve(strict=False)
    if not resolved_parent.is_relative_to(home):
        raise RuntimeError(f"profile path parent escapes HERMES_HOME: {path}")


def runtime_python(runtime: Path, explicit: Path | None) -> Path:
    if explicit:
        candidate = explicit.expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise ValueError(f"runtime Python does not exist: {candidate}")
    candidates = (
        runtime / "venv" / "bin" / "python",
        runtime / ".venv" / "bin" / "python",
        runtime / "venv" / "Scripts" / "python.exe",
        runtime / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError("assembled runtime has no managed Python; pass --runtime-python")


def verify_runtime(runtime: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "verify-release.py"),
            "--runtime-dir",
            str(runtime),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode:
        try:
            errors = json.loads(proc.stdout).get("errors") or []
        except Exception:
            errors = [(proc.stderr or proc.stdout).strip()[-1000:]]
        raise RuntimeError("runtime verification failed: " + "; ".join(errors))


def profile_files(home: Path) -> list[tuple[Path, Path, int]]:
    mappings: dict[Path, tuple[Path, int]] = {}
    components = SOURCE_MANIFEST.get("components") or {}
    for component in components.values():
        for entry in component.get("files") or []:
            relative = str(entry.get("path") or "")
            source = ROOT / relative
            destination: Path | None = None
            if relative.startswith("bin/"):
                destination = home / "bin" / source.name
            elif relative.startswith("kit/bin/"):
                destination = home / "bin" / source.name
            elif relative.startswith(
                ("hooks/", "plugins/", "shared-rules/", "skills/", "mcp-servers/")
            ):
                destination = home / relative
            if destination is None:
                continue
            mode = int(str(entry.get("mode") or "100644")[-3:], 8)
            mappings[destination] = (source, mode)

    router_floor = (
        ROOT
        / "mcp-servers"
        / "capability-router"
        / "public-floor-registry.json"
    )
    mappings[
        home / "mcp-servers" / "capability-router" / "registry.json"
    ] = (router_floor, 0o644)
    return [
        (source, destination, mode)
        for destination, (source, mode) in sorted(
            mappings.items(), key=lambda item: str(item[0])
        )
    ]


def load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise RuntimeError(
            "PyYAML is required for profile installation; run this command "
            "with the assembled Hermes Python"
        ) from None
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("config.yaml root must be a mapping")
    return value


def ensure_public_config(
    config_path: Path, home: Path, python: Path
) -> list[str]:
    import yaml

    config = load_config(config_path)
    changed: list[str] = []

    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("config plugins must be a mapping")
    enabled = plugins.get("enabled")
    enabled = list(enabled) if isinstance(enabled, list) else []
    wanted_plugins = enabled + [
        plugin for plugin in REQUIRED_PLUGINS if plugin not in enabled
    ]
    if wanted_plugins != enabled:
        plugins["enabled"] = wanted_plugins
        changed.append("plugins.enabled")

    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("config mcp_servers must be a mapping")
    current_router = servers.get("capability-router")
    if current_router is not None and not isinstance(current_router, dict):
        raise ValueError("config mcp_servers.capability-router must be a mapping")
    current_router = current_router or {}
    current_env = current_router.get("env")
    if current_env is not None and not isinstance(current_env, dict):
        raise ValueError("config mcp_servers.capability-router.env must be a mapping")
    wanted_router = {
        **current_router,
        "command": str(python),
        "args": ["-m", "capability_router.server"],
        "env": {
            **(current_env or {}),
            "PYTHONPATH": str(
                home / "mcp-servers" / "capability-router" / "src"
            ),
            "HERMES_HOME": str(home),
        },
        "enabled": True,
    }
    if wanted_router != current_router:
        servers["capability-router"] = wanted_router
        changed.append("mcp_servers.capability-router")

    policy = config.setdefault("mcp_policy", {})
    if not isinstance(policy, dict):
        raise ValueError("config mcp_policy must be a mapping")
    hot = policy.get("hot_path")
    hot = list(hot) if isinstance(hot, list) else []
    if "capability-router" not in hot:
        policy["hot_path"] = [*hot, "capability-router"]
        changed.append("mcp_policy.hot_path")

    rendered = yaml.safe_dump(
        config, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).encode()
    if rendered != config_path.read_bytes():
        atomic_bytes(config_path, rendered, 0o600)
    return changed


def _lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _receipt_sha256(value: object, field: str) -> str:
    digest = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"rollback receipt has invalid {field}")
    return digest


def _receipt_mode(value: object) -> int:
    raw = str(value or "")
    if re.fullmatch(r"0[0-7]{3}", raw) is None:
        raise ValueError("rollback receipt has invalid before_mode")
    return int(raw, 8)


def _preflight_restore(
    rows: list[dict[str, Any]],
    backup: Path,
    home: Path,
    config_path: Path,
    config_sha256_before: object,
) -> tuple[list[tuple[Path, bool, bytes | None, int]], bytes]:
    validate_profile_path(home, backup / "files")
    validate_profile_path(home, config_path)
    saved_config = backup / "config.yaml.before"
    validate_profile_path(home, saved_config)
    expected_config_sha = _receipt_sha256(
        config_sha256_before, "config_sha256_before"
    )
    if (
        not saved_config.is_file()
        or saved_config.is_symlink()
        or _is_reparse_point(saved_config)
    ):
        raise RuntimeError(f"rollback config is missing or unsafe: {saved_config}")
    config_data = saved_config.read_bytes()
    if hashlib.sha256(config_data).hexdigest() != expected_config_sha:
        raise RuntimeError("rollback config hash does not match receipt")

    checked: list[tuple[Path, bool, bytes | None, int]] = []
    seen: set[Path] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("rollback receipt file entry is invalid")
        raw_destination = row.get("destination")
        if not isinstance(raw_destination, str) or not Path(
            raw_destination
        ).is_absolute():
            raise ValueError("rollback receipt destination is invalid")
        destination = _lexical_path(Path(raw_destination))
        validate_profile_path(home, destination)
        if destination in seen:
            raise ValueError("rollback receipt has duplicate destinations")
        seen.add(destination)
        existed = row.get("existed")
        if not isinstance(existed, bool):
            raise ValueError("rollback receipt existed flag is invalid")
        mode = _receipt_mode(row.get("before_mode"))
        _receipt_mode(row.get("mode"))
        backup_key = _receipt_sha256(row.get("backup_key"), "backup_key")
        source = row.get("source")
        if (
            not isinstance(source, str)
            or not source
            or Path(source).is_absolute()
            or ".." in Path(source).parts
        ):
            raise ValueError("rollback receipt source is invalid")
        expected_sha = row.get("before_sha256")
        saved_data: bytes | None = None
        if existed:
            expected_sha = _receipt_sha256(expected_sha, "before_sha256")
            saved = backup / "files" / backup_key
            validate_profile_path(home, saved)
            if (
                not saved.is_file()
                or saved.is_symlink()
                or _is_reparse_point(saved)
            ):
                raise RuntimeError(f"rollback file is missing or unsafe: {saved}")
            saved_data = saved.read_bytes()
            if hashlib.sha256(saved_data).hexdigest() != expected_sha:
                raise RuntimeError(
                    f"rollback file hash does not match receipt: {saved}"
                )
        elif expected_sha is not None:
            raise ValueError(
                "rollback receipt has a hash for a nonexistent destination"
            )
        checked.append((destination, existed, saved_data, mode))
    return checked, config_data


def restore(
    rows: list[dict[str, Any]],
    backup: Path,
    home: Path,
    config_path: Path,
    config_sha256_before: object,
) -> None:
    checked, config_data = _preflight_restore(
        rows, backup, home, config_path, config_sha256_before
    )
    for destination, existed, saved_data, mode in reversed(checked):
        validate_profile_path(home, destination)
        if existed:
            assert saved_data is not None
            atomic_bytes(destination, saved_data, mode)
        elif destination.is_file() or destination.is_symlink():
            destination.unlink()
    atomic_bytes(config_path, config_data, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--runtime-python", type=Path)
    parser.add_argument(
        "--restore-backup",
        type=Path,
        help="Restore a backup created by this command; does not restart a service",
    )
    args = parser.parse_args()

    home = args.hermes_home.expanduser().resolve()
    config_path = home / "config.yaml"
    validate_profile_path(home, config_path)
    if args.restore_backup:
        backup = _lexical_path(args.restore_backup)
        backup_root = home / "state" / "public-setup-backups"
        validate_profile_path(home, backup)
        if not backup.is_relative_to(backup_root):
            raise ValueError("restore backup must be inside this HERMES_HOME")
        receipt_path = backup / "receipt.json"
        validate_profile_path(home, receipt_path)
        if (
            not receipt_path.is_file()
            or receipt_path.is_symlink()
            or _is_reparse_point(receipt_path)
        ):
            raise ValueError("restore receipt is missing or unsafe")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt.get("schema_version") != 1
            or receipt.get("status") not in {"pending", "completed"}
            or receipt.get("kind") != "botdoctor_public_profile_install"
            or _lexical_path(Path(str(receipt.get("hermes_home") or "")))
            != home
            or not isinstance(receipt.get("files"), list)
            or _lexical_path(Path(str(receipt.get("rollback") or "")))
            != backup
        ):
            raise ValueError("restore receipt is invalid or belongs to another profile")
        restore(
            receipt["files"],
            backup,
            home,
            config_path,
            receipt.get("config_sha256_before"),
        )
        current_receipt = home / "state" / "public-setup-current.json"
        validate_profile_path(home, current_receipt)
        current_receipt.unlink(missing_ok=True)
        print(
            json.dumps(
                {
                    "ok": True,
                    "restored": str(backup),
                    "files": len(receipt["files"]),
                    "service_switched": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.runtime_dir is None:
        parser.error("--runtime-dir is required unless --restore-backup is used")
    runtime = args.runtime_dir.expanduser().resolve()
    if not config_path.is_file():
        raise ValueError(
            f"missing {config_path}; run native hermes setup in this profile first"
        )
    verify_runtime(runtime)
    python = runtime_python(runtime, args.runtime_python)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / "state" / "public-setup-backups" / stamp
    if backup.exists():
        raise RuntimeError(f"backup path collision: {backup}")
    validate_profile_path(home, backup / "files")
    (backup / "files").mkdir(parents=True, mode=0o700)
    atomic_bytes(backup / "config.yaml.before", config_path.read_bytes(), 0o600)

    rows: list[dict[str, Any]] = []
    for source, destination, mode in profile_files(home):
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"unsafe or missing source: {source}")
        validate_profile_path(home, destination)
        existed = destination.exists()
        if destination.is_symlink() or (existed and not destination.is_file()):
            raise RuntimeError(f"unsafe destination: {destination}")
        before_mode = destination.stat().st_mode & 0o777 if existed else 0
        backup_key = hashlib.sha256(
            str(destination.relative_to(home)).encode()
        ).hexdigest()
        if existed:
            atomic_bytes(
                backup / "files" / backup_key,
                destination.read_bytes(),
                before_mode,
            )
        rows.append(
            {
                "destination": str(destination),
                "source": str(source.relative_to(ROOT)),
                "mode": format(mode, "04o"),
                "backup_key": backup_key,
                "existed": existed,
                "before_mode": format(before_mode, "04o"),
                "before_sha256": sha256(destination) if existed else None,
            }
        )

    receipt = {
        "schema_version": 1,
        "kind": "botdoctor_public_profile_install",
        "status": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "release": RELEASE["release"],
        "golden_sha": RELEASE["golden_sha"],
        "runtime_fingerprint": RELEASE["assembled_runtime_fingerprint"]["digest"],
        "hermes_home": str(home),
        "runtime_dir": str(runtime),
        "files": rows,
        "config_sha256_before": sha256(config_path),
        "credentials_read": False,
        "service_switched": False,
        "gateway_restarted": False,
        "rollback": str(backup),
    }
    atomic_bytes(
        backup / "receipt.json",
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )

    changed_config: list[str] = []
    try:
        for row in rows:
            source = ROOT / row["source"]
            destination = Path(row["destination"])
            validate_profile_path(home, destination)
            atomic_bytes(destination, source.read_bytes(), int(row["mode"], 8))
            row["after_sha256"] = sha256(destination)

        merge = subprocess.run(
            [
                str(python),
                str(ROOT / "scripts" / "merge-shared-defaults.py"),
                "--config-path",
                str(config_path),
                "--defaults-dir",
                str(ROOT / "shared-defaults"),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if merge.returncode:
            raise RuntimeError(
                "shared-default merge failed: "
                + (merge.stderr or merge.stdout).strip()[-1000:]
            )
        changed_config = ensure_public_config(config_path, home, python)
    except BaseException:
        restore(
            rows,
            backup,
            home,
            config_path,
            receipt["config_sha256_before"],
        )
        raise

    receipt.update(
        {
            "status": "completed",
            "config_sha256": sha256(config_path),
            "config_paths_added": changed_config,
        }
    )
    atomic_bytes(
        backup / "receipt.json",
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    current_receipt = home / "state" / "public-setup-current.json"
    validate_profile_path(home, current_receipt)
    atomic_bytes(
        current_receipt,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "release": RELEASE["release"],
                "files_installed": len(rows),
                "config_paths_added": changed_config,
                "backup": str(backup),
                "service_switched": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
