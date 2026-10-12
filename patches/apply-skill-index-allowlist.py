"""Apply prompt-only skill filtering after upstream resolves catalog precedence."""
from pathlib import Path

MARKER = "HERMES_SKILL_INDEX_ALLOWLIST_v1"


def patch_skill_index_allowlist_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "agent/prompt_builder.py"
    source = target.read_text()
    if MARKER in source:
        return False
    config_anchor = '    disabled = get_disabled_skill_names(_platform_hint or None)\n'
    config = '''    # HERMES_SKILL_INDEX_ALLOWLIST_v1: index filtering never disables skill_view.
    from agent.skill_utils import _load_raw_config, _normalize_string_set
    raw = _load_raw_config()
    skill_config = raw.get("skills", {}) if isinstance(raw, dict) else {}
    if not isinstance(skill_config, dict):
        skill_config = {}
    index_allowlist = (_normalize_string_set(skill_config["index_allowlist"])
                       if "index_allowlist" in skill_config else None)
    try:
        index_description_max = int(skill_config["index_description_max"])
    except (KeyError, TypeError, ValueError):
        index_description_max = -1
'''
    cache_anchor = '        _oneshot_prompt_variant(),\n'
    label_anchor = '    _label_visible_entries(visible_entries, skills_by_category)\n'
    filtering = '''    if index_allowlist is not None:
        visible_entries = [e for e in visible_entries
                           if e["name"] in index_allowlist or e.get("skill_name") in index_allowlist]
    if index_description_max >= 0:
        def shorten(value):
            text = str(value or "").strip()
            if len(text) <= index_description_max:
                return text
            if index_description_max == 0:
                return ""
            cut = text[:index_description_max - 1].rstrip()
            boundary = max(cut.rfind(" "), cut.rfind("\\t"), cut.rfind("\\n"))
            if boundary >= max(1, (index_description_max - 1) // 2):
                cut = cut[:boundary].rstrip()
            return cut + "…"
        visible_entries = [{**e, "description": shorten(e.get("description"))} for e in visible_entries]
'''
    for old,new in (
        (config_anchor,config_anchor+config),
        (cache_anchor,cache_anchor+'        tuple(sorted(index_allowlist)) if index_allowlist is not None else None, index_description_max,\n'),
        (label_anchor,filtering+label_anchor),
    ):
        if source.count(old)!=1:
            raise RuntimeError("native skill catalog owner drift")
        source=source.replace(old,new,1)
    compile(source,str(target),"exec")
    target.write_text(source)
    return True
