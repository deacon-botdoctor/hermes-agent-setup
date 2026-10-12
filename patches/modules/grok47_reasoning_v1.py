"""Preserve the requested Grok 4.7 reasoning effort on native Responses requests."""
from __future__ import annotations
import ast
from pathlib import Path

MARKER = "HERMES_GROK47_REASONING_v1"


def patch_grok47_reasoning_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "agent/model_metadata.py"
    source = target.read_text()
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id == "_GROK_EFFORT_CAPABLE_PREFIXES"
                 for t in node.targets)]
    if len(nodes) != 1:
        raise RuntimeError("Grok effort allowlist anchor drift")
    node = nodes[0]
    prefixes = ast.literal_eval(node.value)
    if not isinstance(prefixes, tuple) or not all(isinstance(p, str) for p in prefixes):
        raise RuntimeError("Grok effort allowlist shape drift")
    if "grok-4.7" in prefixes:
        return False
    lines = source.splitlines(keepends=True)
    replacement = f"_GROK_EFFORT_CAPABLE_PREFIXES = {prefixes + ('grok-4.7',)!r}  # {MARKER}\n"
    lines[node.lineno - 1:node.end_lineno] = [replacement]
    updated = "".join(lines)
    compile(updated, str(target), "exec")
    target.write_text(updated)
    return True
