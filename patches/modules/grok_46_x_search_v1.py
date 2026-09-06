#!/usr/bin/env python3
"""Preserve the Grok 4.6 X Search default and validated model override."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_GROK_46_X_SEARCH_v1"


class PatchError(RuntimeError):
    pass


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise PatchError(f"required unique anchor missing for {label}: found {count}")
    return source.replace(old, new, 1)


def patch_models_source(source: str) -> str:
    if MARKER in source:
        return source
    # d363 owns the 4.6 fallback, curated-extra, and top-model entries. Its
    # catalog finalization changed during the upstream refactor, so do not
    # couple this recognition to a particular merge/finalize implementation.
    if (
        source.count('"grok-4.6"') >= 2
        and '_XAI_TOP_MODEL = "grok-4.6"' in source
        and "_XAI_STATIC_FALLBACK: list[str] = [" in source
        and "_XAI_CURATED_EXTRAS: list[str] = [" in source
    ):
        anchor = "_XAI_STATIC_FALLBACK: list[str] = [\n"
        if source.count(anchor) != 1:
            raise PatchError("native xAI catalog marker anchor drift")
        return source.replace(
            anchor,
            f"# {MARKER}: upstream owns the Grok 4.6 catalog; Golden owns the X Search override.\n" + anchor,
            1,
        )
    source = _replace_once(
        source,
        '_XAI_STATIC_FALLBACK: list[str] = [\n    "grok-build-0.1",',
        f"# {MARKER}: xAI OAuth live-canary verified exact grok-4.6.\n"
        '_XAI_STATIC_FALLBACK: list[str] = [\n    "grok-4.6",\n    "grok-build-0.1",',
        "xAI static fallback",
    )
    source = _replace_once(
        source,
        '_XAI_CURATED_EXTRAS: list[str] = [\n    "grok-4.5",',
        "_XAI_CURATED_EXTRAS: list[str] = [\n"
        '    "grok-4.6",  # GA 2026-08; absent from some cached catalogs\n'
        '    "grok-4.5",',
        "xAI curated extras",
    )
    source = _replace_once(
        source,
        '_XAI_TOP_MODEL = "grok-build-0.1"',
        '_XAI_TOP_MODEL = "grok-4.6"',
        "xAI top model",
    )
    source = _replace_once(
        source,
        "return _xai_merge_curated_extras(_xai_promote_top(sorted(ids)))",
        "return _xai_promote_top(_xai_merge_curated_extras(sorted(ids)))",
        "cached xAI catalog promotion order",
    )
    source = _replace_once(
        source,
        "return _xai_merge_curated_extras(list(_XAI_STATIC_FALLBACK))",
        "return _xai_promote_top(_xai_merge_curated_extras(list(_XAI_STATIC_FALLBACK)))",
        "fallback xAI catalog promotion order",
    )
    return source


def patch_x_search_source(source: str) -> str:
    if MARKER in source:
        return source
    # d363 inlined the configured model lookup in the request payload. Keep
    # its helper layout and port only the default plus validated call override.
    d363_payload = '''        payload = {
            "model": str(_load_x_search_config().get("model") or "").strip() or DEFAULT_X_SEARCH_MODEL,
'''
    if d363_payload in source:
        source = _replace_once(
            source,
            'DEFAULT_X_SEARCH_MODEL = "grok-4.5"',
            f'# {MARKER}: default and explicit caller selection use Grok 4.6.\nDEFAULT_X_SEARCH_MODEL = "grok-4.6"',
            "d363 X Search default",
        )
        source = _replace_once(
            source,
            '''\n\ndef _get_x_search_reasoning_effort() -> Optional[str]:
''',
            '''\n\ndef _resolve_x_search_model(override: str = "") -> str:
    """Resolve an optional caller override without permitting non-Grok routes."""
    model = str(override or "").strip() or str(_load_x_search_config().get("model") or "").strip()
    model = model or DEFAULT_X_SEARCH_MODEL
    if not model.lower().startswith("grok-"):
        raise ValueError("x_search model must be a Grok model ID")
    return model


def _get_x_search_reasoning_effort() -> Optional[str]:
''',
            "d363 X Search model resolver",
        )
        source = _replace_once(
            source,
            '''    enable_video_understanding: bool = False,
) -> str:
''',
            '''    enable_video_understanding: bool = False,
    model: str = "",
) -> str:
''',
            "d363 X Search model argument",
        )
        source = _replace_once(
            source,
            '''        reasoning_effort = _get_x_search_reasoning_effort()
''',
            '''        selected_model = _resolve_x_search_model(model)
        reasoning_effort = _get_x_search_reasoning_effort()
''',
            "d363 X Search model selection",
        )
        return _replace_once(
            source,
            d363_payload,
            '''        payload = {
            "model": selected_model,
''',
            "d363 X Search request model",
        )
    source = _replace_once(
        source,
        'DEFAULT_X_SEARCH_MODEL = "grok-4.5"',
        f'# {MARKER}: default and explicit CLI selection use Grok 4.6.\nDEFAULT_X_SEARCH_MODEL = "grok-4.6"',
        "X Search default",
    )
    source = _replace_once(
        source,
        """def _get_x_search_model() -> str:
    cfg = _load_x_search_config()
    return (str(cfg.get("model") or "").strip() or DEFAULT_X_SEARCH_MODEL)
""",
        '''def _get_x_search_model() -> str:
    cfg = _load_x_search_config()
    return (str(cfg.get("model") or "").strip() or DEFAULT_X_SEARCH_MODEL)


def _resolve_x_search_model(override: str = "") -> str:
    """Resolve an optional caller override without permitting non-Grok routes."""
    model = str(override or "").strip() or _get_x_search_model()
    if not model.lower().startswith("grok-"):
        raise ValueError("x_search model must be a Grok model ID")
    return model
''',
        "X Search model resolver",
    )
    source = _replace_once(
        source,
        """    enable_image_understanding: bool = False,
    enable_video_understanding: bool = False,
) -> str:
""",
        """    enable_image_understanding: bool = False,
    enable_video_understanding: bool = False,
    model: str = "",
) -> str:
""",
        "X Search model argument",
    )
    source = _replace_once(
        source,
        """        payload = {
            "model": _get_x_search_model(),
""",
        """        try:
            selected_model = _resolve_x_search_model(model)
        except ValueError as exc:
            return tool_error(str(exc))

        payload = {
            "model": selected_model,
""",
        "X Search request model",
    )
    return source


def patch_config_defaults_source(source: str) -> str:
    if MARKER in source:
        return source
    d363_anchor = '''        # xAI model for the Responses call; any Grok model with x_search access works.
        "model": "grok-4.5",
'''
    if d363_anchor in source:
        return _replace_once(
            source,
            d363_anchor,
            f'''        # {MARKER}: exact-model OAuth and native-X canaries passed.
        # xAI model for the Responses call; any Grok model with x_search access works.
        "model": "grok-4.6",
''',
            "d363 X Search config default",
        )
    source = _replace_once(
        source,
        """        # xAI model used for the Responses call. grok-4.5 is the
        # recommended default; any Grok model with x_search tool
        # access works.
        "model": "grok-4.5",
""",
        f"""        # {MARKER}: exact-model OAuth and native-X canaries passed.
        # xAI model used for the Responses call. grok-4.6 is the
        # recommended default; any Grok model with x_search tool
        # access works.
        "model": "grok-4.6",
""",
        "X Search config default",
    )
    return source


def patch_model_metadata_source(source: str) -> str:
    if MARKER in source:
        return source
    if '"grok-4.6": 500000' in source and "def grok_supports_reasoning_effort" in source:
        anchor = '    "grok-4.6": 500000'
        if source.count(anchor) != 1:
            raise PatchError("native Grok 4.6 metadata marker anchor drift")
        return source.replace(
            anchor,
            f"    # {MARKER}: native Grok 4.6 context and effort metadata verified.\n" + anchor,
            1,
        )
    source = _replace_once(
        source,
        '    "grok-4.5": 500000,',
        f'    # {MARKER}: official 500K Grok 4.6 context.\n    "grok-4.6": 500000,\n    "grok-4.5": 500000,',
        "Grok 4.6 context metadata",
    )
    source = _replace_once(
        source,
        """    "grok-4.5",
)


def grok_supports_reasoning_effort""",
        """    "grok-4.5",
    "grok-4.6",
)


def grok_supports_reasoning_effort""",
        "Grok 4.6 reasoning effort metadata",
    )
    return source


def patch_reasoning_timeouts_source(source: str) -> str:
    if MARKER in source:
        return source
    if '("grok-4.6", 300)' in source:
        anchor = '    ("grok-4.6", 300),'
        if source.count(anchor) != 1:
            raise PatchError("native Grok 4.6 timeout marker anchor drift")
        return source.replace(
            anchor,
            f"    # {MARKER}: native Grok 4.6 reasoning timeout verified.\n" + anchor,
            1,
        )
    # d363 changed the timeout table from ``(model, seconds)`` pairs to
    # second-keyed model tuples; the Grok 4.6 floor is already native.
    d363_anchor = '"grok-4.5", "grok-4.6",\n'
    if d363_anchor in source:
        return _replace_once(
            source,
            d363_anchor,
            f'"grok-4.5",  # {MARKER}: native Grok 4.6 reasoning timeout verified.\n        "grok-4.6",\n',
            "d363 Grok 4.6 reasoning timeout marker",
        )
    return _replace_once(
        source,
        '    ("grok-4.5", 300),',
        f'    # {MARKER}: Grok 4.6 is a long-reasoning model.\n    ("grok-4.5", 300),\n    ("grok-4.6", 300),',
        "Grok 4.6 reasoning timeout",
    )


def patch_x_search_test_source(source: str) -> str:
    if MARKER in source:
        return source
    return _replace_once(
        source,
        '    assert captured["json"]["model"] == "grok-4.5"',
        f"    # {MARKER}: the runtime default must be observable at the HTTP boundary.\n"
        '    assert captured["json"]["model"] == "grok-4.6"',
        "X Search default regression",
    )


PATCHERS = {
    Path("hermes_cli/models.py"): patch_models_source,
    Path("tools/x_search_tool.py"): patch_x_search_source,
    Path("hermes_cli/config_defaults.py"): patch_config_defaults_source,
    Path("agent/model_metadata.py"): patch_model_metadata_source,
    Path("agent/reasoning_timeouts.py"): patch_reasoning_timeouts_source,
    Path("tests/tools/test_x_search_tool.py"): patch_x_search_test_source,
}


def patch_grok_46_x_search_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    pending: dict[Path, str] = {}
    for relative, patcher in PATCHERS.items():
        # d363 moved the static provider floors out of the orchestration
        # module. Prefer that owned catalog when present; cb retains models.py.
        target = root / relative
        if relative == Path("hermes_cli/models.py"):
            native_catalog = root / "hermes_cli/models_catalog_static.py"
            if native_catalog.is_file():
                target = native_catalog
        if not target.is_file():
            raise PatchError(f"required file missing: {target}")
        before = target.read_text(encoding="utf-8")
        after = patcher(before)
        if after != before:
            pending[target] = after
    for target, content in pending.items():
        target.write_text(content, encoding="utf-8")
    return bool(pending)
