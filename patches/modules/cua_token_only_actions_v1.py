"""Keep snapshot-bound input compatible with token-only driver schemas."""

from pathlib import Path


def patch_cua_token_only_actions_v1(hermes_dir: Path) -> bool:
    path = Path(hermes_dir) / "tools/computer_use/cua_backend.py"
    source = path.read_text(encoding="utf-8")
    before = '            args["element_token"] = token\n'
    after = before + (
        '            # HERMES_CUA_TOKEN_ONLY_ACTIONS_v1: newer drivers reject the legacy index argument.\n'
        '            if not self._session.supports_input_property(name, "element_index"):\n'
        '                args.pop("element_index", None)\n'
    )
    if source.count(after) == 1:
        return False
    if source.count(before) != 1:
        raise RuntimeError("CUA token-only action anchor drift")
    source = source.replace(before, after, 1)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    return True
