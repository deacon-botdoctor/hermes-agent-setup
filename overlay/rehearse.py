#!/usr/bin/env python3
"""rehearse.py — the rehearsal harness (the "Verify" stage).

Applies the overlay to a throwaway copy of a pristine upstream tree and reports whether every
patch still lands cleanly. This is what you run BEFORE deploying, especially before an upstream
version bump: if an anchor moved, you find out here, in a sandbox, not in production.

Point --upstream at a clean checkout of the exact runtime version you intend to deploy onto.

Usage:
  python overlay/rehearse.py --upstream /path/to/pristine-runtime
  python overlay/rehearse.py --upstream /path/to/pristine-runtime --keep   # leave the sandbox

Exit code 0 = every patch applied cleanly. Non-zero = at least one anchor-miss or error; do not
deploy until it's green.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Rehearse the overlay against a pristine upstream tree.")
    ap.add_argument("--upstream", required=True, type=Path, help="clean checkout of the target version")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox copy for inspection")
    args = ap.parse_args()

    if not args.upstream.exists():
        print(f"upstream tree not found: {args.upstream}")
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="overlay-rehearsal-"))
    work = sandbox / "runtime"
    print(f"rehearsing → {work}")
    # Copy the pristine tree so we never touch the real checkout.
    subprocess.run(["cp", "-a", str(args.upstream), str(work)], check=True)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "apply.py"), "--hermes-dir", str(work)],
        capture_output=True, text=True,
    )
    print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)

    if not args.keep:
        subprocess.run(["rm", "-rf", str(sandbox)], check=False)
    else:
        print(f"\nsandbox kept at {sandbox}")

    if proc.returncode == 0:
        print("\nREHEARSAL GREEN — every patch applied cleanly. Safe to deploy.")
    else:
        print("\nREHEARSAL RED — fix the flagged anchors before deploying.")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
