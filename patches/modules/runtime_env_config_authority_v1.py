#!/usr/bin/env python3
"""Preserve config-authoritative gateway controls across per-turn .env reloads."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_RUNTIME_ENV_CONFIG_AUTHORITY_v1"

def patch_gateway_text(source: str) -> str:
    if MARKER in source:
        return source
    # Refactored Hermes owns the mapping/helper, but calls it only at startup.
    # Reuse it after each dotenv reload instead of copying the control list.
    refactored_anchor = '    _bridge_max_turns_to_env(cfg.get("agent", {}))\n'
    if source.count(refactored_anchor) == 1 and "def _bridge_section_to_env(" in source and "_AGENT_ENV_BRIDGE = {" in source:
        return source.replace(
            refactored_anchor,
            refactored_anchor + f"    # {MARKER}: restore config controls after dotenv reload.\n"
            '    _bridge_section_to_env(cfg.get("agent", {}), _AGENT_ENV_BRIDGE)\n',
            1,
        )
    raise RuntimeError("runtime env authority anchor drift")


def patch_runtime_env_config_authority_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "gateway" / "run.py"
    original = target.read_text(encoding="utf-8")
    patched = patch_gateway_text(original)
    if patched == original:
        return False
    target.write_text(patched, encoding="utf-8")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_runtime_env_config_authority_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
