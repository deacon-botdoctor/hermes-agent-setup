#!/usr/bin/env python3
"""Preserve strict JSON Schema contracts on the Codex auxiliary wire."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_CODEX_STRICT_STRUCTURED_OUTPUT_v1"

HELPER_ANCHOR = "\n\nclass _CodexCompletionsAdapter:\n"
HELPER_SOURCE = f'''

# {MARKER}
def _codex_response_format_from_kwargs(kwargs: Dict[str, Any]) -> Any:
    """Return the caller's OpenAI response format, with extra_body winning."""
    response_format = kwargs.get("response_format")
    extra_body = kwargs.get("extra_body")
    if isinstance(extra_body, dict) and "response_format" in extra_body:
        response_format = extra_body.get("response_format")
    return response_format


def _strict_structured_output_requested(kwargs: Dict[str, Any]) -> bool:
    """True only for an explicitly strict OpenAI JSON Schema contract."""
    response_format = _codex_response_format_from_kwargs(kwargs)
    if not isinstance(response_format, dict):
        return False
    if response_format.get("type") != "json_schema":
        return False
    json_schema = response_format.get("json_schema")
    return isinstance(json_schema, dict) and json_schema.get("strict") is True


def _apply_codex_response_format(
    resp_kwargs: Dict[str, Any], kwargs: Dict[str, Any]
) -> None:
    """Translate OpenAI chat JSON Schema into Codex Responses text.format."""
    response_format = _codex_response_format_from_kwargs(kwargs)
    if not isinstance(response_format, dict):
        return
    if response_format.get("type") != "json_schema":
        return

    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, dict):
        raise ValueError("Codex json_schema response_format requires json_schema")
    name = json_schema.get("name")
    description = json_schema.get("description")
    strict = json_schema.get("strict")
    schema = json_schema.get("schema")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Codex json_schema response_format requires a non-empty name")
    if strict is not None and not isinstance(strict, bool):
        raise ValueError(
            "Codex json_schema response_format requires boolean or null strict"
        )
    if not isinstance(schema, dict):
        raise ValueError("Codex json_schema response_format requires an object schema")

    native_format = {{
        "type": "json_schema",
        "name": name,
        "schema": copy.deepcopy(schema),
    }}
    if description is not None:
        native_format["description"] = description
    if strict is not None:
        native_format["strict"] = strict
    resp_kwargs["text"] = {{"format": native_format}}
'''

REQUEST_ANCHOR = '\n        # Forward the chat.completions timeout; otherwise a Codex stream can sit behind a\n'
REQUEST_REPLACEMENT = '\n        _apply_codex_response_format(resp_kwargs, kwargs)\n' + REQUEST_ANCHOR
STRIP_ANCHOR = '    retry_kwargs = dict(kwargs)\n    changed = retry_kwargs.pop("response_format", None) is not None\n'
STRIP_REPLACEMENT = '    if _strict_structured_output_requested(kwargs):\n        return None\n' + STRIP_ANCHOR


class PatchError(RuntimeError):
    pass


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if new in source:
        return source
    if source.count(old) != 1:
        raise PatchError(f"required unique anchor missing: {label}")
    return source.replace(old, new, 1)


def patch_auxiliary_source(source: str) -> str:
    source = _replace_once(source, HELPER_ANCHOR, HELPER_SOURCE + HELPER_ANCHOR,
                           label="Codex completions adapter")
    source = _replace_once(source, REQUEST_ANCHOR, REQUEST_REPLACEMENT,
                           label="Codex Responses request")
    return _replace_once(source, STRIP_ANCHOR, STRIP_REPLACEMENT,
                         label="strict format stripping guard")


def patch_codex_strict_structured_output_v1(hermes_dir: Path) -> bool:
    path = Path(hermes_dir) / "agent" / "auxiliary_client.py"
    if not path.exists():
        raise PatchError(f"required file missing: {path}")
    before = path.read_text(encoding="utf-8")
    after = patch_auxiliary_source(before)
    if after == before:
        return False
    path.write_text(after, encoding="utf-8")
    return True
