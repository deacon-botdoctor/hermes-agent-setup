#!/usr/bin/env python3
"""Bind native Windows gateway commands to an explicit owned task identity."""

from __future__ import annotations

import shutil
from pathlib import Path


MARKER = "HERMES_WINDOWS_GATEWAY_TASK_IDENTITY_v1"
CONFIG_MARKER = "HERMES_WINDOWS_GATEWAY_TASK_IDENTITY_CONFIG_v1"
CONFIG_ANCHOR = '''    "gateway": {
'''
CONFIG_REPLACEMENT = '''    "gateway": {
        # Windows only: manage this exact Scheduled Task instead of the
        # profile-derived Hermes_Gateway[_profile] default.
        "windows_task_name": "",  # HERMES_WINDOWS_GATEWAY_TASK_IDENTITY_CONFIG_v1

'''
CONFIG_EXAMPLE_MARKER = "windows_task_name: My_Hermes_Gateway"
CONFIG_EXAMPLE_ANCHOR = '''group_sessions_per_user: true

# ─────────────────────────────────────────────────────────────────────────────
# API Server — per-client model routing
'''
CONFIG_EXAMPLE_REPLACEMENT = '''group_sessions_per_user: true

# Optional Windows Scheduled Task identity. Profiles default to
# Hermes_Gateway_<profile>; set this only when Hermes must manage an existing
# externally named task.
# gateway:
#   windows_task_name: My_Hermes_Gateway

# ─────────────────────────────────────────────────────────────────────────────
# API Server — per-client model routing
'''
CONSTANT_ANCHOR = '''_TASK_NAME_DEFAULT = "Hermes_Gateway"
_TASK_DESCRIPTION = "Hermes Agent Gateway - Messaging Platform Integration"
'''
CONSTANT_REPLACEMENT = f'''_TASK_NAME_DEFAULT = "Hermes_Gateway"
_TASK_DESCRIPTION = "Hermes Agent Gateway - Messaging Platform Integration"
_TASK_NAME_OVERRIDE_ENV = "HERMES_GATEWAY_TASK_NAME"  # {MARKER}
'''
TASK_ANCHOR = '''    # Local import to avoid circular module initialization during hermes_cli boot.
    from hermes_cli.gateway import _profile_suffix

    suffix = _profile_suffix()
    if not suffix:
        return _TASK_NAME_DEFAULT
    return f"{_TASK_NAME_DEFAULT}_{suffix}"
'''
TASK_REPLACEMENT_V1 = '''    # Local imports avoid circular module initialization during hermes_cli boot.
    from hermes_cli.gateway import _profile_suffix

    suffix = _profile_suffix()
    override = os.environ.get(_TASK_NAME_OVERRIDE_ENV, "").strip()
    if not override:
        try:
            import json
            from hermes_constants import get_default_hermes_root

            settings_path = get_default_hermes_root() / "state" / "safe-restart-settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            configured_profile = str(settings.get("Profile") or "root").strip()
            if configured_profile.lower() == (suffix or "root").lower():
                override = str(settings.get("TaskName") or "").strip()
        except (OSError, UnicodeDecodeError, ValueError, TypeError, AttributeError):
            pass
    if override:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", override):
            raise ValueError("invalid HERMES_GATEWAY_TASK_NAME")
        return override
    if not suffix:
        return _TASK_NAME_DEFAULT
    return f"{_TASK_NAME_DEFAULT}_{suffix}"
'''
TASK_REPLACEMENT = '''    # Local imports avoid circular module initialization during hermes_cli boot.
    from hermes_cli.config import load_config_readonly
    from hermes_cli.gateway import _profile_suffix

    suffix = _profile_suffix()
    override_source = _TASK_NAME_OVERRIDE_ENV
    override = os.environ.get(_TASK_NAME_OVERRIDE_ENV, "").strip()
    if not override:
        try:
            config = load_config_readonly()
            gateway_config = (
                config.get("gateway") if isinstance(config, dict) else None
            )
            override = (
                str(gateway_config.get("windows_task_name") or "").strip()
                if isinstance(gateway_config, dict)
                else ""
            )
            override_source = "gateway.windows_task_name"
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            pass
    if not override:
        try:
            import json
            from hermes_constants import get_default_hermes_root

            settings_path = get_default_hermes_root() / "state" / "safe-restart-settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            configured_profile = str(settings.get("Profile") or "root").strip()
            if configured_profile.lower() == (suffix or "root").lower():
                override = str(settings.get("TaskName") or "").strip()
                override_source = "safe-restart-settings TaskName"
        except (OSError, UnicodeDecodeError, ValueError, TypeError, AttributeError):
            pass
    if override:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", override):
            raise ValueError(f"invalid {override_source}")
        return override
    if not suffix:
        return _TASK_NAME_DEFAULT
    return f"{_TASK_NAME_DEFAULT}_{suffix}"
'''
CMD_ENV_ANCHOR = '''    lines.append(f'set "HERMES_HOME={hermes_home}"')
    lines.append('set "PYTHONIOENCODING=utf-8"')
'''
CMD_ENV_REPLACEMENT = '''    lines.append(f'set "HERMES_HOME={hermes_home}"')
    lines.append(f'set "HERMES_GATEWAY_TASK_NAME={task_name or _TASK_NAME_DEFAULT}"')
    lines.append('set "PYTHONIOENCODING=utf-8"')
'''
VBS_ENV_ANCHOR = '''        f"env.Item({_quote_vbs_string('HERMES_HOME')}) = {_quote_vbs_string(hermes_home)}",
        f"env.Item({_quote_vbs_string('PYTHONIOENCODING')}) = {_quote_vbs_string('utf-8')}",
'''
VBS_ENV_REPLACEMENT = '''        f"env.Item({_quote_vbs_string('HERMES_HOME')}) = {_quote_vbs_string(hermes_home)}",
        f"env.Item({_quote_vbs_string('HERMES_GATEWAY_TASK_NAME')}) = {_quote_vbs_string(task_name or _TASK_NAME_DEFAULT)}",
        f"env.Item({_quote_vbs_string('PYTHONIOENCODING')}) = {_quote_vbs_string('utf-8')}",
'''
CMD_SIGNATURE_ANCHOR = '''def _build_gateway_cmd_script(
    python_path: str,
    working_dir: str,
    hermes_home: str,
    profile_arg: str,
) -> str:
'''
CMD_SIGNATURE_REPLACEMENT = '''def _build_gateway_cmd_script(
    python_path: str,
    working_dir: str,
    hermes_home: str,
    profile_arg: str,
    task_name: str = "",
) -> str:
'''
VBS_SIGNATURE_ANCHOR = '''def _build_gateway_vbs_script(
    python_path: str,
    working_dir: str,
    hermes_home: str,
    profile_arg: str,
) -> str:
'''
VBS_SIGNATURE_REPLACEMENT = '''def _build_gateway_vbs_script(
    python_path: str,
    working_dir: str,
    hermes_home: str,
    profile_arg: str,
    task_name: str = "",
) -> str:
'''
CMD_CALL_ANCHOR = '''    content = _build_gateway_cmd_script(python_path, working_dir, hermes_home, profile_arg)
'''
CMD_CALL_REPLACEMENT = '''    task_name = get_task_name()
    content = _build_gateway_cmd_script(python_path, working_dir, hermes_home, profile_arg, task_name)
'''
VBS_CALL_ANCHOR = '''    vbs_content = _build_gateway_vbs_script(python_path, working_dir, hermes_home, profile_arg)
'''
VBS_CALL_REPLACEMENT = '''    vbs_content = _build_gateway_vbs_script(python_path, working_dir, hermes_home, profile_arg, task_name)
'''


def patch_gateway_windows_text(original: str) -> str:
    """Add config support while upgrading already-v1-patched runtimes."""
    if MARKER in original:
        if TASK_REPLACEMENT in original:
            return original
        if original.count(TASK_REPLACEMENT_V1) != 1:
            raise RuntimeError("Windows gateway task resolver upgrade drift")
        return original.replace(
            TASK_REPLACEMENT_V1,
            TASK_REPLACEMENT,
            1,
        )
    replacements = (
        (CONSTANT_ANCHOR, CONSTANT_REPLACEMENT, "constant"),
        (TASK_ANCHOR, TASK_REPLACEMENT, "task resolver"),
        (CMD_SIGNATURE_ANCHOR, CMD_SIGNATURE_REPLACEMENT, "cmd signature"),
        (VBS_SIGNATURE_ANCHOR, VBS_SIGNATURE_REPLACEMENT, "vbs signature"),
        (CMD_ENV_ANCHOR, CMD_ENV_REPLACEMENT, "cmd environment"),
        (VBS_ENV_ANCHOR, VBS_ENV_REPLACEMENT, "vbs environment"),
        (CMD_CALL_ANCHOR, CMD_CALL_REPLACEMENT, "cmd call"),
        (VBS_CALL_ANCHOR, VBS_CALL_REPLACEMENT, "vbs call"),
    )
    patched = original
    for anchor, replacement, label in replacements:
        if patched.count(anchor) != 1:
            raise RuntimeError(f"Windows gateway {label} anchor drift")
        patched = patched.replace(anchor, replacement, 1)
    return patched


def patch_config_defaults_text(original: str) -> str:
    if CONFIG_MARKER in original:
        return original
    if original.count(CONFIG_ANCHOR) != 1:
        raise RuntimeError("Windows gateway config default anchor drift")
    return original.replace(
        CONFIG_ANCHOR,
        CONFIG_REPLACEMENT,
        1,
    )


def patch_config_example_text(original: str) -> str:
    if CONFIG_EXAMPLE_MARKER in original:
        return original
    if original.count(CONFIG_EXAMPLE_ANCHOR) != 1:
        raise RuntimeError("Windows gateway config example anchor drift")
    return original.replace(
        CONFIG_EXAMPLE_ANCHOR,
        CONFIG_EXAMPLE_REPLACEMENT,
        1,
    )


def patch_windows_gateway_task_identity_v1(root: Path) -> bool:
    """Map the owned task identity to native config and restart state."""
    targets = {
        Path(root) / "hermes_cli/gateway_windows.py": (
            patch_gateway_windows_text
        ),
        Path(root) / "hermes_cli/config_defaults.py": (
            patch_config_defaults_text
        ),
        Path(root) / "cli-config.yaml.example": patch_config_example_text,
    }
    if not all(target.is_file() for target in targets):
        return False
    originals = {
        target: target.read_text(encoding="utf-8")
        for target in targets
    }
    patched = {
        target: patcher(originals[target])
        for target, patcher in targets.items()
    }
    changed = [
        target for target in targets
        if patched[target] != originals[target]
    ]
    if not changed:
        return False
    backups = {
        target: Path(
            str(target)
            + ".bak-pre-windows-gateway-task-identity-v1"
        )
        for target in changed
    }
    for target, backup in backups.items():
        shutil.copy2(target, backup)
    try:
        for target in changed:
            target.write_text(patched[target], encoding="utf-8")
    except Exception:
        for target, backup in backups.items():
            shutil.copy2(backup, target)
            backup.unlink(missing_ok=True)
        raise
    return True
