#!/usr/bin/env python3
"""Keep lazy gateway AIAgent imports inside the assembled runtime root."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_CLARIFY_MODULE_PATH = Path(__file__).with_name("clarify_prose_deadlock_guard_v1.py")
_CLARIFY_SPEC = importlib.util.spec_from_file_location(
    "hermes_golden_clarify_prose_deadlock_guard_v1",
    _CLARIFY_MODULE_PATH,
)
if _CLARIFY_SPEC is None or _CLARIFY_SPEC.loader is None:
    raise RuntimeError(f"cannot load clarify guard module: {_CLARIFY_MODULE_PATH}")
_CLARIFY_MODULE = importlib.util.module_from_spec(_CLARIFY_SPEC)
_CLARIFY_SPEC.loader.exec_module(_CLARIFY_MODULE)
patch_clarify_gateway_source = _CLARIFY_MODULE.patch_gateway_source
patch_clarify_gateway_test_source = _CLARIFY_MODULE.patch_gateway_test_source
patch_clarify_tools_source = _CLARIFY_MODULE.patch_tools_source
patch_clarify_tools_test_source = _CLARIFY_MODULE.patch_tools_test_source

MARKER = "HERMES_GATEWAY_RUNTIME_ROOT_GUARD_v1"
PATH_ANCHOR = "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
EARLY_IMPORT_RE = re.compile(
    r"^from pathlib import Path[ \t]*$",
    re.MULTILINE,
)
EARLY_PATH_MARKER = "HERMES_GATEWAY_RUNTIME_PATH_PRELOAD_v1"
EARLY_PATH_PRELOAD = f'''

# [{EARLY_PATH_MARKER}] Select this gateway's runtime root before any local
# agent or scheduler package can be imported through a stale service path.
sys.path.insert(0, str(Path(__file__).parent.parent))
'''
HELPER_RETURN_ANCHOR = "    return _RuntimeAIAgent\n"
EAGER_PRELOAD = "\n\n_load_runtime_ai_agent_class()\n"
IMPORT_RE = re.compile(
    r"^(?P<indent>[ \t]*)from run_agent import AIAgent[ \t]*$",
    re.MULTILINE,
)
API_IMPORT_BLOCK_ANCHOR = '''        from run_agent import AIAgent
        from gateway.run import (
            _checkpoint_agent_kwargs,
            _current_max_iterations,
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            GatewayRunner,
        )
'''
API_IMPORT_BLOCK_REPLACEMENT = '''        from gateway.run import (
            _checkpoint_agent_kwargs,
            _current_max_iterations,
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            _load_runtime_ai_agent_class,
            GatewayRunner,
        )
        AIAgent = _load_runtime_ai_agent_class()
'''
FAKE_RUNTIME_TEST_MARKER = "HERMES_TEST_FAKE_RUNTIME_ORIGIN_v1"
FAKE_RUNTIME_TEST_RE = re.compile(
    r'^(?P<indent>[ \t]*)fake_run_agent = types\.ModuleType\("run_agent"\)[ \t]*$',
    re.MULTILINE,
)
SIMPLE_RUNTIME_TEST_ANCHOR = (
    '    monkeypatch.setitem(sys.modules, "run_agent", '
    "types.SimpleNamespace(AIAgent=_PendingVoiceAgent))\n"
)
SIMPLE_RUNTIME_TEST_REPLACEMENT = '''    # HERMES_TEST_FAKE_RUNTIME_ORIGIN_v1 — keep the synthetic module
    # inside the assembled candidate root while the production guard stays strict.
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        types.SimpleNamespace(
            AIAgent=_PendingVoiceAgent,
            __file__=str(ROOT / "run_agent.py"),
        ),
    )
'''
FAKE_RUNTIME_TEST_FILES = (
    "test_clarify_progress_leak.py",
    "test_compression_failure_session_sync.py",
    "test_discord_channel_prompts.py",
    "test_fast_command.py",
    "test_queued_native_image_session_key.py",
    "test_reasoning_command.py",
    "test_run_cleanup_progress.py",
    "test_run_progress_interrupt.py",
    "test_run_progress_topics.py",
    "test_session_hygiene.py",
    "test_stale_finalize_suppression.py",
    "test_streaming_tts_gateway_regression.py",
    "test_telegram_voice_v0_regressions.py",
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


_load_runtime_ai_agent_class()
'''


def patch_gateway_runtime_root_guard_text(source: str, *, split_owner: bool = False) -> str:
    path_anchor_count = source.count(PATH_ANCHOR)
    early_preload_count = source.count(EARLY_PATH_PRELOAD)
    if early_preload_count == 0:
        if len(EARLY_IMPORT_RE.findall(source)) != 1:
            raise RuntimeError("gateway runtime-root early import anchor drift")
        source, replaced = EARLY_IMPORT_RE.subn(
            lambda match: match.group(0) + EARLY_PATH_PRELOAD,
            source,
            count=1,
        )
        if replaced != 1:
            raise RuntimeError("gateway runtime-root early import replacement drift")
    elif early_preload_count != 1:
        raise RuntimeError("gateway runtime-root early preload drift")

    if MARKER in source:
        preload_count = source.count(EAGER_PRELOAD)
        if preload_count == 1:
            return source
        if preload_count != 0:
            raise RuntimeError("gateway runtime-root eager preload drift")
        if source.count(HELPER_RETURN_ANCHOR) != 1:
            raise RuntimeError("gateway runtime-root guard helper drift")
        return source.replace(
            HELPER_RETURN_ANCHOR,
            HELPER_RETURN_ANCHOR + EAGER_PRELOAD,
            1,
        )
    if path_anchor_count != 1:
        raise RuntimeError("gateway runtime-root path anchor drift")

    import_count = len(IMPORT_RE.findall(source))
    if import_count < 1 and not split_owner:
        raise RuntimeError("gateway AIAgent import anchor drift")

    patched = source.replace(PATH_ANCHOR, PATH_ANCHOR + HELPER, 1)
    patched, replaced = IMPORT_RE.subn(
        lambda match: f"{match.group('indent')}AIAgent = _load_runtime_ai_agent_class()",
        patched,
    )
    if replaced != import_count:
        raise RuntimeError("gateway AIAgent import replacement drift")
    return patched


def patch_split_agent_imports(source: str, *, expected: int) -> str:
    replacement = "AIAgent = _load_runtime_ai_agent_class()"
    if source.count(replacement) == expected:
        return source
    if len(IMPORT_RE.findall(source)) != expected:
        raise RuntimeError("split gateway AIAgent import anchor drift")
    return IMPORT_RE.sub(
        lambda match: (
            f"{match.group('indent')}from gateway.run import _load_runtime_ai_agent_class\n"
            f"{match.group('indent')}{replacement}"
        ), source,
    )


def patch_api_server_text(source: str) -> str:
    """Keep API-server agent creation on the same guarded runtime loader."""
    if API_IMPORT_BLOCK_REPLACEMENT in source:
        return source
    if source.count(API_IMPORT_BLOCK_ANCHOR) != 1:
        return patch_split_agent_imports(source, expected=1)
    return source.replace(
        API_IMPORT_BLOCK_ANCHOR,
        API_IMPORT_BLOCK_REPLACEMENT,
        1,
    )


def patch_fake_runtime_test_text(source: str) -> str:
    """Give deliberate test doubles the exact candidate runtime origin."""
    if FAKE_RUNTIME_TEST_MARKER in source:
        return source
    if SIMPLE_RUNTIME_TEST_ANCHOR in source:
        return source.replace(
            SIMPLE_RUNTIME_TEST_ANCHOR,
            SIMPLE_RUNTIME_TEST_REPLACEMENT,
            1,
        )
    if not FAKE_RUNTIME_TEST_RE.search(source):
        return source

    def replacement(match: re.Match[str]) -> str:
        indent = match.group("indent")
        return (
            f'{indent}fake_run_agent = types.ModuleType("run_agent")\n'
            f"{indent}# HERMES_TEST_FAKE_RUNTIME_ORIGIN_v1 — the coherence "
            "guard still runs\n"
            f"{indent}# in focused tests, so bind the synthetic module to this "
            "candidate root.\n"
            f"{indent}fake_run_agent.__file__ = str(\n"
            f'{indent}    __import__("pathlib").Path(__file__).resolve().parents[2] '
            '/ "run_agent.py"\n'
            f"{indent})"
        )

    return FAKE_RUNTIME_TEST_RE.sub(
        replacement,
        source,
    )


def patch_gateway_runtime_root_guard_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    gateway_path = root / "gateway" / "run.py"
    split_path = root / "gateway/run_turn.py"
    split_owner = split_path.is_file()
    targets = {
        gateway_path: lambda source: patch_gateway_runtime_root_guard_text(source, split_owner=split_owner),
        root / "gateway" / "platforms" / "api_server.py": patch_api_server_text,
    }
    if split_owner:
        targets[split_path] = lambda source: patch_split_agent_imports(source, expected=3)
    gateway_tests = root / "tests" / "gateway"
    for name in FAKE_RUNTIME_TEST_FILES:
        path = gateway_tests / name
        if path.is_file():
            targets[path] = patch_fake_runtime_test_text
    clarify_targets = {
        root / "tools" / "clarify_gateway.py": patch_clarify_tools_source,
        root / "tests" / "tools" / "test_clarify_gateway.py": (patch_clarify_tools_test_source),
        root / "tests" / "gateway" / "test_clarify_thread_followup_not_swallowed.py": (
            patch_clarify_gateway_test_source
        ),
    }
    clarify_presence = [target.is_file() for target in clarify_targets]
    # d363 owns first-wins resolution and rejected-prose release natively;
    # its existing clarify suites exercise both contracts without Golden edits.
    if any(clarify_presence) and not split_owner:
        if not all(clarify_presence):
            raise RuntimeError("clarify deadlock guard target set is incomplete")
        targets[gateway_path] = lambda source: patch_clarify_gateway_source(
            patch_gateway_runtime_root_guard_text(source)
        )
        targets.update(clarify_targets)
    if not all(target.is_file() for target in targets):
        return False
    originals = {target: target.read_text(encoding="utf-8") for target in targets}
    patched = {target: patcher(originals[target]) for target, patcher in targets.items()}
    changed = [target for target in targets if patched[target] != originals[target]]
    if not changed:
        return False
    for target in changed:
        target.write_text(patched[target], encoding="utf-8")
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
