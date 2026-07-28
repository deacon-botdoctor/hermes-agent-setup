#!/usr/bin/env python3
"""Build the exact public Bot Doctor runtime from its pinned upstream commit.

This command creates a side-by-side candidate. It never edits HERMES_HOME,
changes a service, stops a gateway, or switches a live command route.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))


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


def prepare_posix_dependencies(output: Path, profile_home: Path) -> None:
    if os.name == "nt":
        raise ValueError(
            "--prepare-home is POSIX-only; use the documented isolated "
            "PowerShell installer contract on Windows"
        )
    profile_home = profile_home.expanduser().resolve()
    profile_home.mkdir(parents=True, exist_ok=True)
    isolated_user_home = profile_home / ".installer-user"
    isolated_user_home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
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
        ],
        env=env,
        timeout=1800,
    )


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

    if not args.use_existing_clean_runtime:
        clone_upstream(
            output,
            args.upstream_source,
            args.upstream_url,
            str(RELEASE["canonical_upstream_sha"]),
        )
        if args.prepare_home:
            prepare_posix_dependencies(output, args.prepare_home)
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
