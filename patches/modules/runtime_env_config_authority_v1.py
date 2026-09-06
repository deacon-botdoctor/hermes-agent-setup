#!/usr/bin/env python3
"""Preserve config-authoritative gateway controls across per-turn .env reloads."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_RUNTIME_ENV_CONFIG_AUTHORITY_v1"
BRIDGE_BLOCK = f"""    # [{MARKER}] Per-turn credential refresh reloads .env into the
    # process. Re-bridge every config-authoritative agent control, not only
    # max_turns, so stale env values cannot silently disable timeouts or alter
    # progress/restart behavior after gateway startup.
    if isinstance(agent_cfg, dict):
        for _config_key, _env_name in (
            ("gateway_timeout", "HERMES_AGENT_TIMEOUT"),
            ("gateway_timeout_warning", "HERMES_AGENT_TIMEOUT_WARNING"),
            ("gateway_notify_interval", "HERMES_AGENT_NOTIFY_INTERVAL"),
            ("session_stall_timeout", "HERMES_SESSION_STALL_TIMEOUT"),
            ("restart_drain_timeout", "HERMES_RESTART_DRAIN_TIMEOUT"),
            ("gateway_auto_continue_freshness", "HERMES_AUTO_CONTINUE_FRESHNESS"),
            ("gateway_startup_restore_drain_timeout", "HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT"),
        ):
            if _config_key in agent_cfg:
                os.environ[_env_name] = str(agent_cfg[_config_key])
"""
ANCHOR = """    if isinstance(agent_cfg, dict) and "max_turns" in agent_cfg:
        os.environ["HERMES_MAX_ITERATIONS"] = str(agent_cfg["max_turns"])
"""
REPLACEMENT = ANCHOR + BRIDGE_BLOCK
NATIVE_ANCHOR = """    # config-authoritative knobs for the session-search index (config.yaml
"""


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
    if source.count(ANCHOR) == 1:
        return source.replace(ANCHOR, REPLACEMENT, 1)
    if source.count(NATIVE_ANCHOR) == 1:
        return source.replace(NATIVE_ANCHOR, BRIDGE_BLOCK + NATIVE_ANCHOR, 1)
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
