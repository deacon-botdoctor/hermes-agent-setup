#!/usr/bin/env python3
"""Persist content-free capability env presence from the live gateway."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_GATEWAY_CAPABILITY_ENV_PRESENCE_v1"
START_MARKER = "HERMES_GATEWAY_CAPABILITY_ENV_PRESENCE_START_v1"
HELPER_ANCHOR = "\ndef load_hermes_dotenv(\n"
CALL_ANCHOR = """    _reapply_terminal_config_bridge(home_path)

    return loaded
"""
INVALIDATE_ANCHOR = """    project_env_path = Path(project_env) if project_env else None

    # Normalize safe formatting and remove invalid NUL bytes before parsing.
"""
D363_INVALIDATE_ANCHOR = """    project_env_path = Path(project_env) if project_env else None

    if user_env.exists():  # normalize formatting / strip NULs before parsing
"""
HELPER_BLOCK = f'''

def _write_gateway_capability_env_presence(home_path: Path) -> None:
    """Write names-only env presence for the exact gateway process.

    [{MARKER}] Values are never serialized. The receipt is rebound after each
    managed dotenv refresh and is ignored for non-gateway processes or another
    Hermes home.
    """
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return
    try:
        import hashlib
        import json
        import re
        import tempfile

        from gateway.status import (
            _build_pid_record,
            _get_process_hermes_home,
            _record_looks_like_gateway,
        )
        from utils import atomic_json_write

        requested_home = Path(home_path).resolve(strict=True)
        record = _build_pid_record()
        if not _record_looks_like_gateway(record):
            return
        process_home = Path(_get_process_hermes_home()).resolve(strict=True)
        if process_home != requested_home:
            return
        active = json.loads((requested_home / "gateway.pid").read_text(encoding="utf-8"))
        if not isinstance(active, dict) or not _record_looks_like_gateway(active):
            return
        if (
            active.get("pid") != record.get("pid")
            or active.get("start_time") != record.get("start_time")
        ):
            return
        config_path = requested_home / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8-sig", errors="replace")
        observed_keys = sorted(
            key
            for key in set(re.findall(r"\\$\\{{([A-Za-z_][A-Za-z0-9_]*)\\}}", config_text))
            if any(token in key for token in ("API_KEY", "TOKEN", "SECRET", "OAUTH"))
        )
        present_keys = sorted(
            key for key in observed_keys if str(os.environ.get(key) or "").strip()
        )
        state_dir = requested_home / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        if state_dir.is_symlink() or state_dir.resolve(strict=True).parent != requested_home:
            return
        receipt_path = state_dir / "gateway-capability-env-presence.json"
        staged_fd, staged_name = tempfile.mkstemp(
            dir=str(state_dir), prefix=".gateway-capability-env-presence.", suffix=".tmp"
        )
        os.close(staged_fd)
        staged_path = Path(staged_name)
        try:
            atomic_json_write(
                staged_path,
                {{
                    "schema": "botdoctor.gateway-capability-env-presence.v1",
                    "pid": record.get("pid"),
                    "start_time": record.get("start_time"),
                    "hermes_home": str(requested_home),
                    "config_sha256": hashlib.sha256(config_text.encode("utf-8")).hexdigest(),
                    "observed_keys": observed_keys,
                    "present_keys": present_keys,
                }},
                mode=0o600,
                sort_keys=True,
            )
            # Raw os.replace replaces a hostile symlink directory entry instead
            # of following it to an arbitrary target (atomic_json_write's
            # compatibility path deliberately follows existing symlinks).
            os.replace(staged_path, receipt_path)
            try:
                os.chmod(receipt_path, 0o600)
            except OSError:
                pass
        finally:
            staged_path.unlink(missing_ok=True)
    except Exception:
        # A pre-refresh invalidation already removed this generation's prior
        # receipt. A failed rebuild therefore stays fail-closed.
        return


def _invalidate_gateway_capability_env_presence(home_path: Path) -> None:
    """Remove this gateway generation's receipt before mutating its env.

    Unlink failures intentionally propagate: retaining a valid-looking old
    receipt while changing the environment would create a false green.
    """
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return
    try:
        import json

        from gateway.status import (
            _build_pid_record,
            _get_process_hermes_home,
            _record_looks_like_gateway,
        )

        requested_home = Path(home_path).resolve(strict=True)
        record = _build_pid_record()
        if not _record_looks_like_gateway(record):
            return
        process_home = Path(_get_process_hermes_home()).resolve(strict=True)
        if process_home != requested_home:
            return
        receipt_path = requested_home / "state" / "gateway-capability-env-presence.json"
        if not receipt_path.exists():
            return
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return
        receipt_home = Path(str(receipt.get("hermes_home") or "")).resolve(strict=True)
        if (
            receipt_home == requested_home
            and receipt.get("pid") == record.get("pid")
            and receipt.get("start_time") == record.get("start_time")
        ):
            receipt_path.unlink()
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return
'''
CALL_BLOCK = """    _reapply_terminal_config_bridge(home_path)
    _write_gateway_capability_env_presence(home_path)

    return loaded
"""
RUN_ANCHOR = """    atexit.register(remove_pid_file)
    atexit.register(release_gateway_runtime_lock)

    # Control socket (#92091 step 1) — the gateway-owned identify/status
"""
D363_RUN_ANCHOR = """    if not _start_gateway_claim_pid_file():
        return False

    # Right after the PID claim (which makes us authoritative); non-fatal — consumers fall back to scan.
"""
RUN_BLOCK = f"""    atexit.register(remove_pid_file)
    atexit.register(release_gateway_runtime_lock)
    # [{START_MARKER}] Publish only after this process owns the canonical PID
    # file/runtime lock. Rejected duplicate starts must never overwrite the
    # active gateway's content-free capability receipt.
    from hermes_cli.env_loader import _write_gateway_capability_env_presence
    _write_gateway_capability_env_presence(_hermes_home)

    # Control socket (#92091 step 1) — the gateway-owned identify/status
"""


def patch_env_loader_text(source: str) -> str:
    if MARKER in source:
        return source
    invalidation_anchor = (
        D363_INVALIDATE_ANCHOR
        if D363_INVALIDATE_ANCHOR in source
        else INVALIDATE_ANCHOR
    )
    if (
        source.count(HELPER_ANCHOR) != 1
        or source.count(CALL_ANCHOR) != 1
        or source.count(invalidation_anchor) != 1
    ):
        raise RuntimeError("gateway capability env presence anchor drift")
    source = source.replace(HELPER_ANCHOR, HELPER_BLOCK + HELPER_ANCHOR, 1)
    source = source.replace(
        invalidation_anchor,
        ("""    project_env_path = Path(project_env) if project_env else None
    _invalidate_gateway_capability_env_presence(home_path)

""" + (
            "    if user_env.exists():  # normalize formatting / strip NULs before parsing\n"
            if invalidation_anchor == D363_INVALIDATE_ANCHOR
            else "    # Normalize safe formatting and remove invalid NUL bytes before parsing.\n"
        )),
        1,
    )
    return source.replace(CALL_ANCHOR, CALL_BLOCK, 1)


def patch_gateway_text(source: str) -> str:
    if START_MARKER in source:
        return source
    if source.count(D363_RUN_ANCHOR) == 1:
        return source.replace(
            D363_RUN_ANCHOR,
            f"""    if not _start_gateway_claim_pid_file():
        return False
    # [{START_MARKER}] The split startup path has won the PID/lock claim.
    from hermes_cli.env_loader import _write_gateway_capability_env_presence
    _write_gateway_capability_env_presence(_hermes_home)

    # Right after the PID claim (which makes us authoritative); non-fatal — consumers fall back to scan.
""",
            1,
        )
    if source.count(RUN_ANCHOR) != 1:
        raise RuntimeError("gateway capability env startup anchor drift")
    return source.replace(RUN_ANCHOR, RUN_BLOCK, 1)


def patch_gateway_capability_env_presence_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    env_target = root / "hermes_cli" / "env_loader.py"
    gateway_target = root / "gateway" / "run.py"
    env_original = env_target.read_text(encoding="utf-8")
    gateway_original = gateway_target.read_text(encoding="utf-8")
    env_patched = patch_env_loader_text(env_original)
    gateway_patched = patch_gateway_text(gateway_original)
    if env_patched == env_original and gateway_patched == gateway_original:
        return False
    env_target.write_text(env_patched, encoding="utf-8")
    gateway_target.write_text(gateway_patched, encoding="utf-8")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_gateway_capability_env_presence_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
