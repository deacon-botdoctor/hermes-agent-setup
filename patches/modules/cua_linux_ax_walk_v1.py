"""Keep Linux application-wide accessibility walks from dropping later windows."""

from pathlib import Path


def patch_cua_linux_ax_walk_v1(hermes_dir: Path) -> bool:
    replacements = {
        "hermes_cli/config_defaults.py": [
            ("\n\n#: Image every container", "\n\nimport sys\n\n#: Image every container"),
            ('        "ax_max_elements": 200,',
             '        # HERMES_CUA_LINUX_AX_WALK_v1: AT-SPI scans earlier app windows before the target.\n'
             '        "ax_max_elements": 0 if sys.platform == "linux" else 200,'),
        ],
        "tools/computer_use/cua_backend.py": [
            ('_DEFAULT_AX_MAX_ELEMENTS = 200',
             '# HERMES_CUA_LINUX_AX_WALK_v1: retain the driver default for application-wide AT-SPI walks.\n'
             '_DEFAULT_AX_MAX_ELEMENTS = 0 if sys.platform == "linux" else 200'),
        ],
        "tools/computer_use/cua_backend_capture.py": [
            ('        if capped := _cb._cua_configured_ax_max_elements():\n',
             '        # HERMES_CUA_LINUX_AX_WALK_v1: file chooser controls exceed the newer driver one-second budget.\n'
             '        if _cb.sys.platform == "linux" and self._session.supports_input_property("get_window_state", "timeout_ms"):\n'
             '            args["timeout_ms"] = 15000\n'
             '        if capped := _cb._cua_configured_ax_max_elements():\n'),
        ],
    }
    outputs = {}
    for relative, changes in replacements.items():
        path = Path(hermes_dir) / relative
        original = source = path.read_text(encoding="utf-8")
        for before, after in changes:
            if source.count(after) == 1:
                continue
            if source.count(before) != 1:
                raise RuntimeError(f"Linux CUA accessibility default anchor drift: {relative}")
            source = source.replace(before, after, 1)
        compile(source, str(path), "exec")
        if source != original:
            outputs[path] = source
    for path, source in outputs.items():
        path.write_text(source, encoding="utf-8")
    return bool(outputs)
