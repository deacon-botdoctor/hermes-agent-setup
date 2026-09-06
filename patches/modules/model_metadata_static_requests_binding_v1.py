#!/usr/bin/env python3
"""Replace model-metadata's dynamic globals cache with a static module binding."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_MODEL_METADATA_STATIC_REQUESTS_BINDING_v1"

_DYNAMIC_BINDING = '''def _ensure_requests():
    if "requests" not in globals():
        import requests as _requests
        globals()["requests"] = _requests
    return globals()["requests"]
'''

_STATIC_BINDING = f'''# {MARKER}: preserve lazy import without dynamic namespace mutation.
def _ensure_requests():
    global requests
    try:
        return requests
    except NameError:
        import requests
        return requests
'''


class PatchError(RuntimeError):
    pass


def patch_source(source: str) -> str:
    if MARKER in source:
        return source
    count = source.count(_DYNAMIC_BINDING)
    if count != 1:
        raise PatchError(
            "required unique model-metadata requests binding missing: "
            f"found {count}"
        )
    return source.replace(_DYNAMIC_BINDING, _STATIC_BINDING, 1)


def patch_model_metadata_static_requests_binding_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "agent/model_metadata.py"
    if not target.is_file():
        raise PatchError(f"required file missing: {target}")
    before = target.read_text(encoding="utf-8")
    after = patch_source(before)
    if after == before:
        return False
    target.write_text(after, encoding="utf-8")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-dir", required=True, type=Path)
    args = parser.parse_args()
    changed = patch_model_metadata_static_requests_binding_v1(args.hermes_dir)
    print("patched" if changed else "already patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
