"""Keep LaunchServices CUA daemons attributed to their signed application."""

from pathlib import Path


def patch_cua_macos_app_identity_v1(hermes_dir: Path) -> bool:
    path = Path(hermes_dir) / "tools/computer_use/cua_backend_daemon.py"
    source = path.read_text(encoding="utf-8")
    before = '    _validate_cua_driver_app_signature(resolved_app)\n'
    after = before + (
        '    # HERMES_CUA_MACOS_APP_IDENTITY_v1: LaunchServices parents the signed app to launchd, not Hermes.\n'
        '    serve_args = [arg for arg in serve_args if arg != "--embedded"]\n'
    )
    if source.count(after) == 1:
        return False
    if source.count(before) != 1:
        raise RuntimeError("CUA macOS app identity anchor drift")
    source = source.replace(before, after, 1)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    return True
