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

REQUEST_ANCHOR = '''        resp_kwargs: Dict[str, Any] = {
            # Strip the Hermes-side ``-900k`` large-context picker suffix —
            # the Codex backend only knows the base slug (mirrors the main
            # transport in agent/transports/codex.py::build_kwargs).
            "model": _strip_codex_ctx_variant(model),
            "instructions": instructions,
            "input": input_items or [{"role": "user", "content": ""}],
            "store": False,
        }

        # Preserve the chat.completions timeout contract.'''

REQUEST_REPLACEMENT = '''        resp_kwargs: Dict[str, Any] = {
            # Strip the Hermes-side ``-900k`` large-context picker suffix —
            # the Codex backend only knows the base slug (mirrors the main
            # transport in agent/transports/codex.py::build_kwargs).
            "model": _strip_codex_ctx_variant(model),
            "instructions": instructions,
            "input": input_items or [{"role": "user", "content": ""}],
            "store": False,
        }
        _apply_codex_response_format(resp_kwargs, kwargs)

        # Preserve the chat.completions timeout contract.'''

RETRY_ANCHOR = "        if _is_structured_output_rejection(first_err):\n"
RETRY_REPLACEMENT = '''        if (
            _is_structured_output_rejection(first_err)
            and not _strict_structured_output_requested(kwargs)
        ):
'''


REFACTORED_REQUEST_ANCHOR = '''        resp_kwargs: Dict[str, Any] = {
            # Codex only knows the base slug; strip the Hermes ``-900k`` picker suffix.
            "model": _strip_codex_ctx_variant(model), "instructions": instructions,
            "input": input_items or [{"role": "user", "content": ""}], "store": False,
        }
'''
REFACTORED_REQUEST_REPLACEMENT = (
    REFACTORED_REQUEST_ANCHOR + "        _apply_codex_response_format(resp_kwargs, kwargs)\n"
)


class PatchError(RuntimeError):
    pass


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if new in source:
        return source
    if source.count(old) != 1:
        raise PatchError(f"required unique anchor missing: {label}")
    return source.replace(old, new, 1)


def _replace_exact(
    source: str, old: str, new: str, *, count: int, label: str
) -> str:
    if source.count(new) == count:
        return source
    if source.count(old) != count:
        raise PatchError(f"required exact anchors missing: {label} ({count})")
    return source.replace(old, new, count)


def patch_auxiliary_source(source: str) -> str:
    source = _replace_once(
        source,
        HELPER_ANCHOR,
        HELPER_SOURCE + HELPER_ANCHOR,
        label="Codex completions adapter",
    )
    # The refactor shares one ladder between sync and async calls. Preserve
    # the reviewed 0.21 output while preparing that single new owner.
    shared_ladder = "\ndef _ladder_parameter_rungs(" in source
    source = _replace_once(
        source,
        REFACTORED_REQUEST_ANCHOR if shared_ladder else REQUEST_ANCHOR,
        REFACTORED_REQUEST_REPLACEMENT if shared_ladder else REQUEST_REPLACEMENT,
        label="Codex Responses request",
    )
    return _replace_exact(
        source,
        RETRY_ANCHOR[4:] if shared_ladder else RETRY_ANCHOR,
        "".join(line[4:] for line in RETRY_REPLACEMENT.splitlines(keepends=True))
        if shared_ladder else RETRY_REPLACEMENT,
        count=1 if shared_ladder else 2,
        label="structured-output compatibility retry",
    )


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
