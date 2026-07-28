#!/usr/bin/env python3
"""Keep lazy gateway AIAgent imports inside the assembled runtime root."""

from __future__ import annotations

import re
from pathlib import Path

MARKER = "HERMES_GATEWAY_RUNTIME_ROOT_GUARD_v1"
PATH_ANCHOR = "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
IMPORT_RE = re.compile(
    r"^(?P<indent>[ \t]*)from run_agent import AIAgent[ \t]*$",
    re.MULTILINE,
)
HELPER = f'''

def _load_runtime_ai_agent_class():
    """Resolve AIAgent from this gateway's immutable runtime or fail closed."""
    # [{MARKER}] A connected gateway is not coherent if a lazy turn import can
    # fall through to ~/.hermes/hermes-agent or another mutable checkout.
    _runtime_root = Path(__file__).resolve().parent.parent
    _runtime_entry = str(_runtime_root)
    _expected_run_agent = (_runtime_root / "run_agent.py").resolve()

    sys.path[:] = [
        _entry
        for _entry in sys.path
        if not (
            isinstance(_entry, str)
            and _entry
            and Path(_entry).resolve() == _runtime_root
        )
    ]
    sys.path.insert(0, _runtime_entry)

    _loaded_run_agent = sys.modules.get("run_agent")
    if _loaded_run_agent is not None:
        _loaded_origin = Path(
            getattr(_loaded_run_agent, "__file__", "")
        ).resolve()
        if _loaded_origin != _expected_run_agent:
            raise RuntimeError(
                "runtime coherence violation: run_agent already loaded from "
                f"{{_loaded_origin}}, expected {{_expected_run_agent}}"
            )

    from run_agent import AIAgent as _RuntimeAIAgent

    _actual_origin = Path(sys.modules["run_agent"].__file__).resolve()
    if _actual_origin != _expected_run_agent:
        raise RuntimeError(
            "runtime coherence violation: run_agent resolved from "
            f"{{_actual_origin}}, expected {{_expected_run_agent}}"
        )

    _agent_init = sys.modules.get("agent.agent_init")
    if _agent_init is not None:
        _agent_init_origin = Path(getattr(_agent_init, "__file__", "")).resolve()
        try:
            _agent_init_origin.relative_to(_runtime_root)
        except ValueError as _exc:
            raise RuntimeError(
                "runtime coherence violation: agent.agent_init resolved from "
                f"{{_agent_init_origin}}, expected root {{_runtime_root}}"
            ) from _exc

    return _RuntimeAIAgent
'''


def patch_gateway_runtime_root_guard_text(source: str) -> str:
    if MARKER in source:
        return source
    if source.count(PATH_ANCHOR) != 1:
        raise RuntimeError("gateway runtime-root path anchor drift")

    import_count = len(IMPORT_RE.findall(source))
    if import_count < 1:
        raise RuntimeError("gateway AIAgent import anchor drift")

    patched = source.replace(PATH_ANCHOR, PATH_ANCHOR + HELPER, 1)
    patched, replaced = IMPORT_RE.subn(
        lambda match: f"{match.group('indent')}AIAgent = _load_runtime_ai_agent_class()",
        patched,
    )
    if replaced != import_count:
        raise RuntimeError("gateway AIAgent import replacement drift")
    return patched


def patch_gateway_runtime_root_guard_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "gateway" / "run.py"
    original = target.read_text(encoding="utf-8")
    patched = patch_gateway_runtime_root_guard_text(original)
    if patched == original:
        return False
    target.write_text(patched, encoding="utf-8")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_gateway_runtime_root_guard_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
