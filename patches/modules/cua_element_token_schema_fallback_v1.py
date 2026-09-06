#!/usr/bin/env python3
"""Attach CUA element tokens when the live tool schema supports them."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_CUA_ELEMENT_TOKEN_SCHEMA_FALLBACK_v1"
TARGET = Path("tools/computer_use/cua_backend.py")


class PatchError(RuntimeError):
    pass


def patch_source(source: str) -> str:
    if MARKER in source:
        return source
    old = '''        if not self._session.supports_capability(
            "accessibility.element_tokens", tool=tool
        ):
            return
        args["element_token"] = token
'''
    new = f'''        # {MARKER}: cua-driver 0.22 advertises element_token in the
        # strict action schema but omits the older capability metadata. The
        # schema is already Hermes' compatibility source of truth for action
        # properties, so accept either signal and still fail closed when both
        # are absent.
        supports_token = self._session.supports_capability(
            "accessibility.element_tokens", tool=tool
        ) or self._session.supports_input_property(tool, "element_token")
        if not supports_token:
            return
        args["element_token"] = token
'''
    count = source.count(old)
    if count == 1:
        return source.replace(old, new, 1)

    d363_old = '''        if token and self._session.supports_capability("accessibility.element_tokens", tool=name):
            args["element_token"] = token
'''
    d363_new = f'''        # {MARKER}: cua-driver may expose element_token in a strict action
        # schema without the older capability metadata. Both signals remain
        # per-tool and a missing schema still fails closed.
        supports_token = token and (
            self._session.supports_capability("accessibility.element_tokens", tool=name)
            or self._session.supports_input_property(name, "element_token")
        )
        if supports_token:
            args["element_token"] = token
'''
    d363_count = source.count(d363_old)
    if d363_count != 1:
        raise PatchError(
            "required unique element-token capability anchor missing: "
            f"legacy={count}, d363={d363_count}"
        )
    return source.replace(d363_old, d363_new, 1)


def patch_cua_element_token_schema_fallback_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / TARGET
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
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_cua_element_token_schema_fallback_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
