"""Recover unknown skill categories through the native public listing API."""
import json
import logging

logger = logging.getLogger(__name__)
HINT = ("Category filters require an exact category name, not a search query. "
        "Retry skills_list without category or use one of available_categories.")


def transform_result(*, tool_name=None, args=None, result=None, task_id=None, **kwargs):
    if tool_name != "skills_list" or not (args or {}).get("category"):
        return None
    try:
        current = json.loads(result)
        if not (current.get("success") is True and current.get("count") == 0
                and current.get("skills") == []):
            return None
        from tools.skills_tool import skills_list
        visible = json.loads(skills_list(task_id=task_id))
        if visible.get("success") is not True or not visible.get("skills"):
            return None
        categories = sorted({s.get("category") for s in visible["skills"] if s.get("category")})
        if args["category"] in categories:
            return None
        return json.dumps({"success": True, "skills": [], "categories": [], "count": 0,
                           "available_categories": categories, "hint": HINT}, ensure_ascii=False)
    except Exception:
        # Recovery advice is optional; retain the native result on discovery failure.
        logger.warning("Skill category recovery could not read the visible inventory", exc_info=True)
        return None


def register(ctx):
    ctx.register_hook("transform_tool_result", transform_result)
