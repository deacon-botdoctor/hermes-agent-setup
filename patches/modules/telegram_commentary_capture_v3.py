#!/usr/bin/env python3
"""Keep Telegram checkpoint commentary capture independent of display visibility."""

from __future__ import annotations

import shutil
from pathlib import Path

V2_MARKER = "HERMES_TELEGRAM_ORGANIC_CHECKPOINTS_v2"
MARKER = "HERMES_TELEGRAM_COMMENTARY_CAPTURE_v3"

RUN_MARKER_ANCHOR = f"# {V2_MARKER}\n"
RUN_MARKER_REPLACEMENT = f"# {V2_MARKER}\n# {MARKER}\n"

CAPTURE_ASSIGNMENT_ANCHOR = """        # Telegram checkpoints need commentary capture even when immediate
        # interim-message display is disabled.
        agent.interim_assistant_callback = (
"""
CAPTURE_ASSIGNMENT_REPLACEMENT = """        # Telegram checkpoints need commentary capture even when immediate
        # interim-message display is disabled. This flag does not make the
        # commentary visible; the gateway callback still enforces the surface
        # display policy after retaining a privacy-filtered checkpoint copy.
        agent._telegram_checkpoint_commentary_capture = (
            ctx.source.platform == Platform.TELEGRAM
        )
        agent.interim_assistant_callback = (
"""

RUN_AGENT_GUARD_ANCHOR = """        if not getattr(self, "show_commentary", True):
            # display.show_commentary=false — commentary stays on the
            # reasoning channel (pre-commentary-channel behavior).
            return []
"""
RUN_AGENT_GUARD_REPLACEMENT = f"""        # {MARKER}
        if not (
            getattr(self, "show_commentary", True)
            or getattr(self, "_telegram_checkpoint_commentary_capture", False)
        ):
            # display.show_commentary=false — commentary stays hidden unless
            # Telegram requested an internal checkpoint-only capture.
            return []
"""

APP_SERVER_GUARD_ANCHOR = """        if not getattr(agent, "show_commentary", True):
            return
"""
APP_SERVER_GUARD_REPLACEMENT = f"""        # {MARKER}
        if not (
            getattr(agent, "show_commentary", True)
            or getattr(agent, "_telegram_checkpoint_commentary_capture", False)
        ):
            return
"""

RESPONSES_GUARD_ANCHOR = """                            getattr(agent, "interim_assistant_callback", None) is not None
                            and getattr(agent, "show_commentary", True)
"""
RESPONSES_GUARD_REPLACEMENT = (
    '                            getattr(agent, "interim_assistant_callback", None) is not None\n'
    f"                            # {MARKER}\n"
    "                            and (\n"
    '                                getattr(agent, "show_commentary", True)\n'
    "                                or getattr(\n"
    "                                    agent,\n"
    '                                    "_telegram_checkpoint_commentary_capture",\n'
    "                                    False,\n"
    "                                )\n"
    "                            )\n"
)


def _replace_once(source: str, anchor: str, replacement: str, label: str) -> str:
    if source.count(anchor) != 1:
        raise RuntimeError(f"Telegram commentary capture v3 {label} anchor drift")
    return source.replace(anchor, replacement, 1)


def _patch_legacy_telegram_commentary_capture_v3(hermes_dir: Path) -> bool:
    """Allow hidden Telegram commentary to feed only the checkpoint formatter."""
    root = Path(hermes_dir)
    run_py = root / "gateway/run.py"
    run_agent_py = root / "run_agent.py"
    codex_runtime_py = root / "agent/codex_runtime.py"
    paths = (run_py, run_agent_py, codex_runtime_py)
    originals = {path: path.read_text(encoding="utf-8") for path in paths}

    marker_presence = {path: MARKER in source for path, source in originals.items()}
    if all(marker_presence.values()):
        return False
    if any(marker_presence.values()):
        raise RuntimeError("Telegram commentary capture v3 found a partial prior apply")
    if V2_MARKER not in originals[run_py]:
        raise RuntimeError("Telegram commentary capture v3 requires the v2 base")

    patched = {
        run_py: _replace_once(
            _replace_once(
                originals[run_py],
                RUN_MARKER_ANCHOR,
                RUN_MARKER_REPLACEMENT,
                "version marker",
            ),
            CAPTURE_ASSIGNMENT_ANCHOR,
            CAPTURE_ASSIGNMENT_REPLACEMENT,
            "gateway capture assignment",
        ),
        run_agent_py: _replace_once(
            originals[run_agent_py],
            RUN_AGENT_GUARD_ANCHOR,
            RUN_AGENT_GUARD_REPLACEMENT,
            "structured commentary guard",
        ),
        codex_runtime_py: _replace_once(
            _replace_once(
                originals[codex_runtime_py],
                APP_SERVER_GUARD_ANCHOR,
                APP_SERVER_GUARD_REPLACEMENT,
                "app-server commentary guard",
            ),
            RESPONSES_GUARD_ANCHOR,
            RESPONSES_GUARD_REPLACEMENT,
            "Responses commentary guard",
        ),
    }

    backups = {
        path: Path(str(path) + ".bak-pre-telegram-commentary-capture-v3")
        for path in paths
    }
    for path, backup in backups.items():
        shutil.copy2(path, backup)
    try:
        for path in paths:
            path.write_text(patched[path], encoding="utf-8")
    except Exception:
        for path, backup in backups.items():
            if backup.exists():
                shutil.copy2(backup, path)
                backup.unlink(missing_ok=True)
        raise
    return True


# Split d363 dispatch. The legacy three-file bridge above remains available for cb sources.
_D363_RUNNER_ANCHOR = """        agent.interim_assistant_callback = (
            interim_assistant_cb
            if (want_interim_messages or ctx.source.platform == Platform.TELEGRAM)
            else None
        )
"""
_D363_RUNNER_REPLACEMENT = """        # HERMES_TELEGRAM_COMMENTARY_CAPTURE_v3
        agent._telegram_checkpoint_commentary_capture = (ctx.source.platform == Platform.TELEGRAM)
        agent.interim_assistant_callback = (
            interim_assistant_cb
            if (want_interim_messages or ctx.source.platform == Platform.TELEGRAM)
            else None
        )
"""
_D363_COMPLETED_ANCHOR = '        if isinstance(text, str) and text.strip() and getattr(agent, "show_commentary", True):\n'
_D363_COMPLETED_REPLACEMENT = '''        if (
            isinstance(text, str) and text.strip()
            and (getattr(agent, "show_commentary", True)
                 or getattr(agent, "_telegram_checkpoint_commentary_capture", False))
        ):
'''
_D363_WANTS_ANCHOR = '    wants_commentary = getattr(agent, "interim_assistant_callback", None) is not None and show_commentary\n'
_D363_WANTS_REPLACEMENT = '''    wants_commentary = (
        getattr(agent, "interim_assistant_callback", None) is not None
        and (show_commentary or getattr(agent, "_telegram_checkpoint_commentary_capture", False))
    )
'''

def _patch_split_d363(root: Path) -> bool:
    run_py, runner_py, codex_py = root/'gateway/run_turn.py', root/'gateway/run_turn_runner.py', root/'agent/codex_runtime.py'
    run, runner, codex = (p.read_text(encoding='utf-8') for p in (run_py, runner_py, codex_py))
    if MARKER in run:
        return False
    if V2_MARKER not in run:
        raise RuntimeError('Telegram commentary capture v3 requires the d363 v2 base')
    runner = _replace_once(runner, _D363_RUNNER_ANCHOR, _D363_RUNNER_REPLACEMENT, 'runner callback')
    codex = _replace_once(codex, _D363_COMPLETED_ANCHOR, _D363_COMPLETED_REPLACEMENT, 'Codex completed callback')
    codex = _replace_once(codex, _D363_WANTS_ANCHOR, _D363_WANTS_REPLACEMENT, 'Codex stream callback')
    patched = {run_py: run + f'\n# {MARKER}\n', runner_py: runner + f'\n# {MARKER}\n', codex_py: codex + f'\n# {MARKER}\n'}
    backups = {p: Path(str(p)+'.bak-pre-telegram-commentary-capture-v3') for p in patched}
    for p,b in backups.items(): shutil.copy2(p,b)
    try:
        for p,text in patched.items(): p.write_text(text,encoding='utf-8')
    except Exception:
        for p,b in backups.items(): shutil.copy2(b,p); b.unlink(missing_ok=True)
        raise
    return True

def patch_telegram_commentary_capture_v3(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    if (root/'gateway/run_turn.py').exists():
        return _patch_split_d363(root)
    return _patch_legacy_telegram_commentary_capture_v3(root)
