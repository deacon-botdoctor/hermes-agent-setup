#!/usr/bin/env python3
"""Bind native Windows gateway commands to an explicit owned task identity."""

from __future__ import annotations

import shutil
from pathlib import Path


MARKER = "HERMES_WINDOWS_GATEWAY_TASK_IDENTITY_v1"
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
TASK_REPLACEMENT = '''    # Local imports avoid circular module initialization during hermes_cli boot.
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


def patch_windows_gateway_task_identity_v1(root: Path) -> bool:
    """Reuse the existing restart settings as the native task-name override."""
    target = Path(root) / "hermes_cli/gateway_windows.py"
    original = target.read_text(encoding="utf-8")
    if MARKER in original:
        return False
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
    backup = Path(str(target) + ".bak-pre-windows-gateway-task-identity-v1")
    shutil.copy2(target, backup)
    try:
        target.write_text(patched, encoding="utf-8")
    except Exception:
        shutil.copy2(backup, target)
        backup.unlink(missing_ok=True)
        raise
    return True
