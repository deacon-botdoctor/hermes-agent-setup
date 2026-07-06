#!/usr/bin/env python3
"""apply.py — the overlay apply engine.

Reads registry.yaml, applies each listed patch module to a target runtime tree, and reports
what happened. Every apply is idempotent (a module that is already applied is skipped via its
grep-able marker) and every failure is loud (a module whose anchor no longer matches upstream
reports ANCHOR-MISS rather than silently doing nothing).

This is the "Apply" stage of the pipeline. Run it against a pristine upstream checkout, not a
tree that already has an older overlay on it.

Usage:
  python overlay/apply.py --hermes-dir /path/to/runtime
  python overlay/apply.py --hermes-dir /path/to/runtime --only suppress_codex_autoraise_notice
  python overlay/apply.py --hermes-dir /path/to/runtime --dry-run
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("apply.py needs PyYAML:  pip install pyyaml")

ROOT = Path(__file__).resolve().parent
MODULES_DIR = ROOT / "modules"


# ── module contract ──────────────────────────────────────────────────────────
# Each file in modules/ exposes:
#   MARKER   : str            a unique, grep-able string the patch leaves in the file
#   TARGET   : str            path to the file it edits, relative to the runtime root
#   apply(target_path, *, dry_run=False) -> str
#       returns one of: "applied" | "already" | "anchor-miss"
# The engine handles discovery, idempotency reporting, and result aggregation.
# ─────────────────────────────────────────────────────────────────────────────


def _load_module(name: str):
    path = MODULES_DIR / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"module not found: {path}")
    spec = importlib.util.spec_from_file_location(f"overlay_mod_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("MARKER", "TARGET", "apply"):
        if not hasattr(mod, attr):
            raise AttributeError(f"module {name} is missing required '{attr}'")
    return mod


def load_registry() -> list[dict]:
    data = yaml.safe_load((ROOT / "registry.yaml").read_text())
    return data.get("patches", [])


def run(hermes_dir: Path, only: str | None, dry_run: bool) -> int:
    entries = load_registry()
    if only:
        entries = [e for e in entries if e["name"] == only]
        if not entries:
            print(f"no registry entry named {only!r}")
            return 2

    results = {"applied": 0, "already": 0, "anchor-miss": 0, "error": 0}
    width = max((len(e["name"]) for e in entries), default=10)

    for entry in entries:
        name = entry["name"]
        try:
            mod = _load_module(entry.get("module", name))
            target = hermes_dir / mod.TARGET
            if not target.exists():
                print(f"  {name:<{width}}  SKIP (target absent: {mod.TARGET})")
                continue
            # Idempotency is reported by the engine so every module doesn't reimplement it.
            already = mod.MARKER in target.read_text(encoding="utf-8")
            status = "already" if already else mod.apply(target, dry_run=dry_run)
            results[status] = results.get(status, 0) + 1
            flag = {"applied": "OK ", "already": "-- ", "anchor-miss": "!! "}.get(status, "?? ")
            print(f"  {name:<{width}}  {flag}{status}")
        except Exception as e:  # a broken module must not abort the whole run
            results["error"] += 1
            print(f"  {name:<{width}}  XX  error: {e}")

    print()
    print("  " + "  ".join(f"{k}={v}" for k, v in results.items() if v))
    breakage = results["anchor-miss"] + results["error"]
    if breakage:
        print(f"\n  {breakage} patch(es) did not apply cleanly. Fix anchors before deploying.")
    return 1 if breakage else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply the overlay registry to a runtime tree.")
    ap.add_argument("--hermes-dir", required=True, type=Path, help="runtime root to patch")
    ap.add_argument("--only", help="apply just one entry by name")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()
    if not args.hermes_dir.exists():
        print(f"runtime dir not found: {args.hermes_dir}")
        return 2
    print(f"applying overlay → {args.hermes_dir}{'  (dry-run)' if args.dry_run else ''}\n")
    return run(args.hermes_dir, args.only, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
