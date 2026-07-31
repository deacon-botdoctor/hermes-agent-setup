#!/usr/bin/env python3
"""Patcher: [HERMES_SKILL_INDEX_ALLOWLIST_v1]

Add prompt-index-only skill curation knobs:

* ``skills.index_allowlist`` limits only the rendered ``<available_skills>``
  block to allowlisted live skills.
* ``skills.index_description_max`` truncates only descriptions emitted in that
  prompt block.

U1 finding: do not overload ``skills.disabled``. In current hermes-agent,
``tools.skills_tool.skill_view`` refuses disabled skills and
``agent.skill_commands.scan_skill_commands`` removes disabled slash commands,
so disabled means uninvokable through normal on-demand skill paths. This patch
therefore reads a separate index-only allowlist inside ``prompt_builder``.
"""
from __future__ import annotations

import argparse
import ast
import shutil
import time
from pathlib import Path

MARKER = "HERMES_SKILL_INDEX_ALLOWLIST_v1"

HELPERS = r'''
# HERMES_SKILL_INDEX_ALLOWLIST_v1
def _skill_index_config() -> tuple["set[str] | None", "int | None"]:
    """Read prompt-index-only skill controls from config.yaml.

    ``skills.disabled`` is intentionally not reused here: disabled skills are
    rejected by ``skill_view`` and omitted from slash-command discovery, so it
    is not safe as a prompt-index suppression mechanism.
    """
    try:
        from agent.skill_utils import _load_raw_config, _normalize_string_set

        parsed = _load_raw_config()
        skills_cfg = parsed.get("skills") if isinstance(parsed, dict) else None
        if not isinstance(skills_cfg, dict):
            return None, None
        allowlist = (
            _normalize_string_set(skills_cfg.get("index_allowlist"))
            if "index_allowlist" in skills_cfg
            else None
        )
        max_raw = skills_cfg.get("index_description_max")
        max_len = int(max_raw) if max_raw is not None else None
        if max_len is not None and max_len < 0:
            max_len = None
        return allowlist, max_len
    except Exception:
        return None, None


def _skill_index_allowed(frontmatter_name: str, skill_name: str, allowlist: "set[str] | None") -> bool:
    if allowlist is None:
        return True
    return frontmatter_name in allowlist or skill_name in allowlist


def _truncate_skill_index_description(description: str, max_len: "int | None") -> str:
    desc = str(description or "").strip()
    if max_len is None or len(desc) <= max_len:
        return desc
    if max_len <= 0:
        return ""
    if max_len == 1:
        return "…"
    cut = desc[: max_len - 1].rstrip()
    boundary = max(cut.rfind(" "), cut.rfind("\t"), cut.rfind("\n"))
    if boundary >= max(1, (max_len - 1) // 2):
        cut = cut[:boundary].rstrip()
    return cut + "…"

'''


def _replace_once(src: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in src:
        return src, False
    if old not in src:
        raise RuntimeError(f"skill index allowlist anchor not found: {label}")
    return src.replace(old, new, 1), True


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    patched = src
    changed = False

    if MARKER not in patched:
        anchor = "\ndef build_skills_system_prompt(\n"
        if anchor not in patched:
            raise RuntimeError("skill index allowlist helper anchor not found")
        patched = patched.replace(anchor, "\n" + HELPERS + anchor.lstrip("\n"), 1)
        changed = True

    patched, did = _replace_once(
        patched,
        '''    disabled = get_disabled_skill_names(_platform_hint or None)
    cache_key = (
''',
        '''    disabled = get_disabled_skill_names(_platform_hint or None)
    index_allowlist, index_description_max = _skill_index_config()
    cache_key = (
''',
        "config read",
    )
    changed = changed or did

    patched, did = _replace_once(
        patched,
        '''        tuple(sorted(disabled)),
        tuple(sorted(compact_categories or ())),
''',
        '''        tuple(sorted(disabled)),
        tuple(sorted(index_allowlist)) if index_allowlist is not None else None,
        index_description_max,
        tuple(sorted(compact_categories or ())),
''',
        "cache key",
    )
    changed = changed or did

    patched, did = _replace_once(
        patched,
        '''            if frontmatter_name in disabled or skill_name in disabled:
                continue
            if not _skill_should_show(
''',
        '''            if frontmatter_name in disabled or skill_name in disabled:
                continue
            if not _skill_index_allowed(frontmatter_name, skill_name, index_allowlist):
                continue
            if not _skill_should_show(
''',
        "snapshot allowlist",
    )
    changed = changed or did

    unified_description_old = '''    for entry in visible_entries:
        fm = entry.get("frontmatter_name") or entry.get("skill_name") or ""
        desc = entry.get("description", "")
'''
    unified_description_new = '''    for entry in visible_entries:
        fm = entry.get("frontmatter_name") or entry.get("skill_name") or ""
        desc = _truncate_skill_index_description(
            entry.get("description", ""), index_description_max
        )
'''
    unified_render = (
        unified_description_old in patched or unified_description_new in patched
    )
    if unified_render:
        patched, did = _replace_once(
            patched,
            unified_description_old,
            unified_description_new,
            "unified description truncate",
        )
        changed = changed or did
    else:
        patched, did = _replace_once(
            patched,
            '''            skills_by_category.setdefault(category, []).append(
                (frontmatter_name, entry.get("description", ""))
            )
''',
            '''            skills_by_category.setdefault(category, []).append(
                (
                    frontmatter_name,
                    _truncate_skill_index_description(
                        entry.get("description", ""), index_description_max
                    ),
                )
            )
''',
            "snapshot truncate",
        )
        changed = changed or did

    patched, did = _replace_once(
        patched,
        '''            if entry["frontmatter_name"] in disabled or skill_name in disabled:
                continue
            if not _skill_should_show(
''',
        '''            if entry["frontmatter_name"] in disabled or skill_name in disabled:
                continue
            if not _skill_index_allowed(entry["frontmatter_name"], skill_name, index_allowlist):
                continue
            if not _skill_should_show(
''',
        "cold allowlist",
    )
    changed = changed or did

    if not unified_render:
        patched, did = _replace_once(
            patched,
            '''            skills_by_category.setdefault(entry["category"], []).append(
                (entry["frontmatter_name"], entry["description"])
            )
''',
            '''            skills_by_category.setdefault(entry["category"], []).append(
                (
                    entry["frontmatter_name"],
                    _truncate_skill_index_description(
                        entry["description"], index_description_max
                    ),
                )
            )
''',
            "cold truncate",
        )
        changed = changed or did

    patched, did = _replace_once(
        patched,
        '''                if frontmatter_name in disabled or skill_name in disabled:
                    continue
                if not _skill_should_show(
''',
        '''                if frontmatter_name in disabled or skill_name in disabled:
                    continue
                if not _skill_index_allowed(frontmatter_name, skill_name, index_allowlist):
                    continue
                if not _skill_should_show(
''',
        "external allowlist",
    )
    changed = changed or did

    patched, did = _replace_once(
        patched,
        '''                skills_by_category.setdefault(entry["category"], []).append(
                    (frontmatter_name, entry["description"])
                )
''',
        '''                skills_by_category.setdefault(entry["category"], []).append(
                    (
                        frontmatter_name,
                        _truncate_skill_index_description(
                            entry["description"], index_description_max
                        ),
                    )
                )
''',
        "external truncate",
    )
    changed = changed or did

    if not changed:
        return False
    ast.parse(patched)
    backup = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}-skill-index-allowlist")
    shutil.copy2(path, backup)
    path.write_text(patched, encoding="utf-8")
    print(f"WROTE {path} (backup: {backup.name})")
    return True


def patch_skill_index_allowlist_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "agent" / "prompt_builder.py"
    if not target.exists():
        target = Path(hermes_dir) / "hermes-agent" / "agent" / "prompt_builder.py"
    return patch(target)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="path to agent/prompt_builder.py")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    target = Path(args.target)
    if not target.exists():
        print(f"FAIL: target not found: {target}")
        return 2
    before = target.read_text(encoding="utf-8")
    changed = patch(target)
    if args.dry_run:
        target.write_text(before, encoding="utf-8")
        print("DRY_RUN OK")
        return 0
    print("OK: patched" if changed else "OK: already patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
