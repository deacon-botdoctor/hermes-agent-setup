#!/usr/bin/env python3
"""Build the exact public Bot Doctor runtime from its pinned upstream commit.

This command creates a side-by-side candidate. It never edits a live
HERMES_HOME, changes a service, stops a gateway, or switches a command route.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import hashlib
import platform
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
MACOS_PYTHON_CATALOG = "contracts/python-downloads-macos-arm64.json"
MACOS_PYTHON_CATALOG_SHA256 = "cae33933f03d359951da43606430e447ccc8859fffc0940871d115443f18070d"


def run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=timeout,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()[-3000:]
        raise RuntimeError(f"{argv[0]} failed ({proc.returncode}): {detail}")
    return proc


def ensure_empty_target(path: Path) -> None:
    if path.exists():
        raise ValueError(
            f"output already exists: {path}; choose a new side-by-side candidate path"
        )
    path.parent.mkdir(parents=True, exist_ok=True)


def verify_clean_upstream(path: Path, upstream_sha: str) -> None:
    if not (path / ".git").exists():
        raise ValueError(f"runtime is not a Git checkout: {path}")
    head = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    if head != upstream_sha:
        raise RuntimeError(f"upstream checkout mismatch: {head} != {upstream_sha}")
    status = run(["git", "-C", str(path), "status", "--porcelain"]).stdout.strip()
    if status:
        raise RuntimeError("upstream checkout is not clean before assembly")


def restore_partial_clone_metadata(output: Path, source: Path) -> None:
    """Keep a local blobless source clone fetchable after cloning from it.

    Git does not propagate partial-clone promisor metadata when cloning from a
    local repository. Without restoring it before checkout, missing blobs are
    reported as deleted files and the clean-runtime gate fails misleadingly.
    """
    promisor = subprocess.run(
        ["git", "-C", str(source), "config", "--get", "remote.origin.promisor"],
        capture_output=True,
        text=True,
        check=False,
    )
    if promisor.returncode or promisor.stdout.strip().lower() != "true":
        return

    origin = run(["git", "-C", str(source), "remote", "get-url", "origin"])
    origin_url = origin.stdout.strip()
    if not origin_url:
        raise RuntimeError("partial upstream source has no fetchable origin remote")
    partial_filter = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "config",
            "--get",
            "remote.origin.partialclonefilter",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    filter_value = partial_filter.stdout.strip() or "blob:none"
    for command in (
        ["remote", "set-url", "origin", origin_url],
        ["config", "remote.origin.promisor", "true"],
        ["config", "remote.origin.partialclonefilter", filter_value],
        ["config", "extensions.partialClone", "origin"],
    ):
        run(["git", "-C", str(output), *command])


def clone_upstream(
    output: Path, source: Path | None, upstream_url: str, upstream_sha: str
) -> None:
    if source:
        source = source.expanduser().resolve()
        if not (source / ".git").exists():
            raise ValueError(f"upstream source is not a Git checkout: {source}")
        run(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(source),
                str(output),
            ]
        )
        restore_partial_clone_metadata(output, source)
    else:
        run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                upstream_url,
                str(output),
            ]
        )
    present = subprocess.run(
        ["git", "-C", str(output), "cat-file", "-e", f"{upstream_sha}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if present.returncode:
        run(
            [
                "git",
                "-C",
                str(output),
                "fetch",
                "--depth",
                "1",
                "origin",
                upstream_sha,
            ]
        )
    run(["git", "-C", str(output), "checkout", "--detach", upstream_sha])
    verify_clean_upstream(output, upstream_sha)


def candidate_python_proof(output: Path, env: dict[str, str]) -> dict:
    """Inspect only the candidate interpreter; never open a Hermes database."""
    python = output / "venv" / "bin" / "python"
    store = (output / ".hermes-runtime" / "python").resolve()
    if (not store.is_relative_to(output.resolve()) or not python.is_file()
            or not python.resolve().is_relative_to(store)):
        raise RuntimeError("candidate Python is not in its private managed store")
    script = (
        "import json, sqlite3, sys; print(json.dumps({"
        "'python': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
        "'python_version': list(sys.version_info[:3]), "
        "'sqlite_version': list(sqlite3.sqlite_version_info)}))"
    )
    with tempfile.TemporaryDirectory(prefix="hermes-python-proof-") as probe_home:
        probe_env = dict(env, HERMES_HOME=probe_home)
        result = run([str(python), "-I", "-c", script], env=probe_env, timeout=30)
    try:
        proof = json.loads(result.stdout)
        version = proof["sqlite_version"]
        if (not isinstance(version, list) or len(version) != 3
                or any(type(part) is not int for part in version)):
            raise ValueError("invalid SQLite version")
        if tuple(version) < (3, 51, 3):
            raise RuntimeError("candidate Python requires SQLite >= 3.51.3")
        if (Path(proof["prefix"]).resolve() != (output / "venv").resolve()
                or not Path(proof["base_prefix"]).resolve().is_relative_to(store)
                or Path(proof["python"]) != python):
            raise ValueError("candidate interpreter identity mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("candidate Python proof is invalid") from exc
    return proof


def prepare_posix_dependencies(output: Path, profile_home: Path) -> dict:
    if os.name == "nt":
        raise ValueError(
            "--prepare-home is POSIX-only; use the documented isolated "
            "PowerShell installer contract on Windows"
        )
    profile_home = profile_home.expanduser()
    if not profile_home.is_absolute():
        raise ValueError("--prepare-home must be absolute")
    if profile_home.is_symlink():
        raise ValueError("--prepare-home must not be a symlink")
    if profile_home.exists():
        if not profile_home.is_dir() or any(profile_home.iterdir()):
            raise ValueError("--prepare-home must be a unique empty directory")
    else:
        profile_home.mkdir(parents=True)
    profile_home = profile_home.resolve()
    isolated_user_home = profile_home / ".installer-user"
    isolated_user_home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # The pinned installer owns uv provisioning. Force its normal find/install
    # path into this candidate so a host's old Python cannot seed a new venv.
    for key in tuple(env):
        if key.startswith(("UV_", "PYTHON", "CONDA_")) or key == "VIRTUAL_ENV":
            env.pop(key)
    env.update({
        "UV_MANAGED_PYTHON": "1",
        "UV_PYTHON_INSTALL_DIR": str(output / ".hermes-runtime" / "python"),
        "UV_PYTHON_INSTALL_BIN": "0",
        "UV_PYTHON_INSTALL_REGISTRY": "0",
    })
    catalog_proof = None
    if sys.platform == "darwin" and platform.machine() == "arm64":
        # Python patch versions alone do not identify their bundled SQLite.
        # Older uv catalogs select a vulnerable build of this same 3.11.15.
        catalog = ROOT / MACOS_PYTHON_CATALOG
        digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
        if digest != MACOS_PYTHON_CATALOG_SHA256:
            raise RuntimeError("managed Python download catalog digest mismatch")
        env["UV_PYTHON_DOWNLOADS_JSON_URL"] = catalog.resolve().as_uri()
        catalog_proof = {"path": MACOS_PYTHON_CATALOG, "sha256": digest}
    env["HOME"] = str(isolated_user_home)
    env["HERMES_HOME"] = str(profile_home)
    run(
        [
            "bash",
            str(output / "scripts" / "install.sh"),
            "--skip-setup",
            "--skip-browser",
            "--dir",
            str(output),
            "--hermes-home",
            str(profile_home),
            "--commit",
            str(RELEASE["canonical_upstream_sha"]),
            "--force-commit",
        ],
        env=env,
        timeout=1800,
    )
    proof = candidate_python_proof(output, env)
    if catalog_proof is not None:
        proof["download_catalog"] = catalog_proof
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--upstream-source",
        type=Path,
        help="Optional existing clean NousResearch/hermes-agent checkout",
    )
    parser.add_argument(
        "--upstream-url",
        default="https://github.com/NousResearch/hermes-agent.git",
    )
    parser.add_argument(
        "--prepare-home",
        type=Path,
        help=(
            "POSIX only: use upstream's installer in an isolated HOME to create "
            "the candidate venv and profile scaffolding before applying Golden"
        ),
    )
    parser.add_argument(
        "--use-existing-clean-runtime",
        action="store_true",
        help=(
            "Apply to --output only when it is an existing clean checkout at "
            "the exact upstream pin; intended for an isolated Windows install"
        ),
    )
    args = parser.parse_args()

    output = args.output.expanduser().resolve()
    if args.use_existing_clean_runtime:
        if args.upstream_source or args.prepare_home:
            parser.error(
                "--use-existing-clean-runtime cannot be combined with "
                "--upstream-source or --prepare-home"
            )
        verify_clean_upstream(
            output, str(RELEASE["canonical_upstream_sha"])
        )
    else:
        ensure_empty_target(output)
    verify = run(
        [sys.executable, str(ROOT / "bin" / "verify-release.py"), "--json"]
    )
    if not json.loads(verify.stdout).get("ok"):
        raise RuntimeError("public release source verification failed")

    python_proof = None
    if not args.use_existing_clean_runtime:
        clone_upstream(
            output,
            args.upstream_source,
            args.upstream_url,
            str(RELEASE["canonical_upstream_sha"]),
        )
        if args.prepare_home:
            python_proof = prepare_posix_dependencies(output, args.prepare_home)
    env = os.environ.copy()
    env["HERMES_APPLY_SKIP_SNAPSHOT"] = "1"
    patch = run(
        [
            sys.executable,
            str(ROOT / "patches" / "apply-all-patches.py"),
            "--hermes-dir",
            str(output),
        ],
        env=env,
    )
    verified = run(
        [
            sys.executable,
            str(ROOT / "bin" / "verify-release.py"),
            "--runtime-dir",
            str(output),
            "--json",
        ]
    )
    proof = json.loads(verified.stdout)
    if not proof.get("ok"):
        raise RuntimeError(
            "assembled runtime failed exact verification: "
            + "; ".join(proof.get("errors") or [])
        )

    receipt = {
        "schema_version": 1,
        "kind": "botdoctor_public_runtime_assembly",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "release": RELEASE["release"],
        "golden_sha": RELEASE["golden_sha"],
        "upstream_sha": RELEASE["canonical_upstream_sha"],
        "deployment_digest": RELEASE["deployment_digest"],
        "runtime_fingerprint": proof["runtime_fingerprint"],
        "runtime_path": str(output),
        "patch_output_tail": patch.stdout[-2000:],
        "python_runtime": python_proof,
    }
    receipt_path = output.with_name(output.name + ".assembly.json")
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "runtime": str(output),
                "receipt": str(receipt_path),
                "release": RELEASE["release"],
                "fingerprint": proof["runtime_fingerprint"]["digest"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
