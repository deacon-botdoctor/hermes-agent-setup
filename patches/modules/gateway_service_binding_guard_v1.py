#!/usr/bin/env python3
"""Preserve a proven active runtime binding across native gateway lifecycle calls."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_GATEWAY_SERVICE_BINDING_GUARD_v1"

HELPER_ANCHOR = "def systemd_unit_is_current(system: bool = False) -> bool:\n"
HELPER = f'''def _operator_runtime_binding(service_kind: str):
    """Return a verified profile-local runtime binding or fail closed."""
    # [{MARKER}] A checkout-local generated service definition cannot overrule
    # an exact runtime/service tuple that an activation controller already proved.
    import hashlib

    home = Path(get_hermes_home()).resolve()
    receipt_path = home / "state/runtime-binding.json"
    if not receipt_path.exists():
        return None
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise RuntimeError(f"active runtime binding receipt is unsafe: {{receipt_path}}")
    try:
        binding = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"active runtime binding receipt is invalid: {{receipt_path}}") from exc
    runtime_root = Path(str(binding.get("runtime_root") or "")).resolve()
    runtime_python = Path(str(binding.get("runtime_python") or "")).absolute()
    service = binding.get("service")
    if (
        binding.get("schema_version") != 1
        or binding.get("kind") != "botdoctor_runtime_binding"
        or binding.get("status") != "active"
        or binding.get("hermes_home") != str(home)
        or not runtime_root.is_dir()
        or not runtime_python.is_file()
        or not isinstance(service, dict)
        or service.get("kind") != service_kind
        or not isinstance(service.get("owner"), str)
        or not service.get("owner").strip()
    ):
        raise RuntimeError(
            "active runtime binding is broken; refusing checkout-local service fallback"
        )
    try:
        runtime_python.relative_to(runtime_root)
    except ValueError as exc:
        raise RuntimeError("active runtime interpreter is outside its runtime root") from exc
    definition_raw = service.get("definition_path")
    expected_sha = service.get("definition_sha256")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        raise RuntimeError("active runtime binding definition identity is invalid")
    if definition_raw is not None:
        definition = Path(str(definition_raw)).absolute()
        if (
            not definition.is_file()
            or definition.is_symlink()
            or hashlib.sha256(definition.read_bytes()).hexdigest() != expected_sha
        ):
            raise RuntimeError(
                "active runtime service definition drifted; refusing checkout-local service fallback"
            )
    launchers = service.get("launchers")
    if not isinstance(launchers, list) or (definition_raw is None and not launchers):
        raise RuntimeError("active runtime binding launcher set is invalid")
    for row in launchers:
        if not isinstance(row, dict):
            raise RuntimeError("active runtime binding launcher set is invalid")
        launcher = Path(str(row.get("path") or "")).absolute()
        if (
            not launcher.is_file()
            or launcher.is_symlink()
            or hashlib.sha256(launcher.read_bytes()).hexdigest() != row.get("sha256")
        ):
            raise RuntimeError(
                "active runtime launcher drifted; refusing checkout-local service fallback"
            )
    return binding


def _operator_runtime_binding_preserves_service(service_kind: str) -> bool:
    return _operator_runtime_binding(service_kind) is not None


def _operator_runtime_binding_requires_current_runtime(service_kind: str) -> None:
    binding = _operator_runtime_binding(service_kind)
    if binding is None:
        return
    if Path(str(binding["runtime_root"])).resolve() != PROJECT_ROOT:
        raise RuntimeError(
            "gateway lifecycle invoked from a non-active runtime; use the bound runtime or approved activation path"
        )


'''

SYSTEMD_CURRENT_ANCHOR = '''    if not unit_path.exists():
        return False

    installed = unit_path.read_text(encoding="utf-8")
'''
SYSTEMD_CURRENT_REPLACEMENT = '''    if not unit_path.exists():
        if _operator_runtime_binding(service_kind="systemd-system" if system else "systemd-user") is not None:
            raise RuntimeError("active runtime systemd definition is missing")
        return False
    if _operator_runtime_binding_preserves_service(
        "systemd-system" if system else "systemd-user"
    ):
        return True

    installed = unit_path.read_text(encoding="utf-8")
'''

SYSTEMD_INSTALL_ANCHOR = '''    unit_path = get_systemd_unit_path(system=system)
    scope_flag = " --system" if system else ""

    # Existing system units already pin HERMES_HOME; adopt it before any
'''
SYSTEMD_INSTALL_REPLACEMENT = '''    unit_path = get_systemd_unit_path(system=system)
    scope_flag = " --system" if system else ""
    if _operator_runtime_binding_preserves_service(
        "systemd-system" if system else "systemd-user"
    ):
        print(f"Service is protected by the active runtime binding: {unit_path}")
        return

    # Existing system units already pin HERMES_HOME; adopt it before any
'''

LAUNCHD_CURRENT_ANCHOR = '''    if not plist_path.exists():
        return False

    installed = plist_path.read_text(encoding="utf-8")
'''
LAUNCHD_CURRENT_REPLACEMENT = '''    if not plist_path.exists():
        if _operator_runtime_binding(service_kind="launchd-user") is not None:
            raise RuntimeError("active runtime launchd definition is missing")
        return False
    if _operator_runtime_binding_preserves_service("launchd-user"):
        return True

    installed = plist_path.read_text(encoding="utf-8")
'''

LAUNCHD_INSTALL_ANCHOR = '''def launchd_install(force: bool = False):
    plist_path = get_launchd_plist_path()

    if plist_path.exists() and not force:
'''
LAUNCHD_INSTALL_REPLACEMENT = '''def launchd_install(force: bool = False):
    plist_path = get_launchd_plist_path()
    if _operator_runtime_binding_preserves_service("launchd-user"):
        print(f"Service is protected by the active runtime binding: {plist_path}")
        return

    if plist_path.exists() and not force:
'''

LAUNCHD_START_ANCHOR = '''def launchd_start():
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()

    # Self-heal if the plist is missing entirely (e.g., manual cleanup, failed upgrade)
'''
LAUNCHD_START_REPLACEMENT = '''def launchd_start():
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    _operator_runtime_binding(service_kind="launchd-user")

    # Self-heal if the plist is missing entirely (e.g., manual cleanup, failed upgrade)
'''

WINDOWS_INSTALL_ANCHOR = '''    _assert_windows()
    start_now, start_on_login = _prompt_install_choices(start_now, start_on_login)
'''
WINDOWS_INSTALL_REPLACEMENT = '''    _assert_windows()
    from hermes_cli.gateway import _operator_runtime_binding_preserves_service
    if _operator_runtime_binding_preserves_service("windows-scheduled-task"):
        print("Service is protected by the active runtime binding")
        return
    start_now, start_on_login = _prompt_install_choices(start_now, start_on_login)
'''

WINDOWS_START_ANCHOR = '''def start() -> None:
    """Start the gateway using the canonical detached Windows launch path."""
    _assert_windows()
'''
WINDOWS_START_REPLACEMENT = '''def start() -> None:
    """Start the gateway using the canonical detached Windows launch path."""
    _assert_windows()
    from hermes_cli.gateway import _operator_runtime_binding_requires_current_runtime
    _operator_runtime_binding_requires_current_runtime("windows-scheduled-task")
'''

WINDOWS_RESTART_ANCHOR = '''    _assert_windows()

    stop()

    if not _wait_for_gateway_absent(timeout_s=30.0):
'''
WINDOWS_RESTART_REPLACEMENT = '''    _assert_windows()
    from hermes_cli.gateway import _operator_runtime_binding_requires_current_runtime
    _operator_runtime_binding_requires_current_runtime("windows-scheduled-task")

    stop()

    if not _wait_for_gateway_absent(timeout_s=30.0):
'''


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    if replacement in source:
        return source
    if source.count(anchor) != 1:
        raise RuntimeError(f"gateway service binding {label} anchor drift")
    return source.replace(anchor, replacement, 1)


def patch_gateway_source(source: str) -> str:
    if MARKER not in source:
        if source.count(HELPER_ANCHOR) != 1:
            raise RuntimeError("gateway service binding helper anchor drift")
        source = source.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
    for anchor, replacement, label in (
        (SYSTEMD_CURRENT_ANCHOR, SYSTEMD_CURRENT_REPLACEMENT, "systemd current"),
        (SYSTEMD_INSTALL_ANCHOR, SYSTEMD_INSTALL_REPLACEMENT, "systemd install"),
        (LAUNCHD_CURRENT_ANCHOR, LAUNCHD_CURRENT_REPLACEMENT, "launchd current"),
        (LAUNCHD_INSTALL_ANCHOR, LAUNCHD_INSTALL_REPLACEMENT, "launchd install"),
        (LAUNCHD_START_ANCHOR, LAUNCHD_START_REPLACEMENT, "launchd start"),
    ):
        source = _replace_once(source, anchor, replacement, label)
    return source


def patch_windows_source(source: str) -> str:
    for anchor, replacement, label in (
        (WINDOWS_INSTALL_ANCHOR, WINDOWS_INSTALL_REPLACEMENT, "windows install"),
        (WINDOWS_START_ANCHOR, WINDOWS_START_REPLACEMENT, "windows start"),
        (WINDOWS_RESTART_ANCHOR, WINDOWS_RESTART_REPLACEMENT, "windows restart"),
    ):
        source = _replace_once(source, anchor, replacement, label)
    return source


def patch_gateway_service_binding_guard_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    targets = {
        root / "hermes_cli/gateway.py": patch_gateway_source,
        root / "hermes_cli/gateway_windows.py": patch_windows_source,
    }
    if not all(path.is_file() for path in targets):
        return False
    original = {path: path.read_text(encoding="utf-8") for path in targets}
    patched = {path: patcher(original[path]) for path, patcher in targets.items()}
    changed = [path for path in targets if patched[path] != original[path]]
    for path in changed:
        path.write_text(patched[path], encoding="utf-8")
    return bool(changed)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    print("patched" if patch_gateway_service_binding_guard_v1(args.hermes_dir) else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
