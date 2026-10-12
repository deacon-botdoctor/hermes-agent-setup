"""Preserve the pre-turn idle clock without weakening watchdog startup."""
import ast
from pathlib import Path

MARKER = "HERMES_IDLE_COMPACTION_CLOCK_v1"
PAYLOAD = Path(__file__).resolve().parents[1] / "payloads/idle-compaction-clock-v1/idle_compaction_clock.py"


def replace(source, old, new):
    if new in source:
        return source
    if source.count(old) != 1:
        raise RuntimeError("idle compaction clock anchor changed")
    result = source.replace(old, new)
    ast.parse(result)
    return result


def patch_idle_compaction_clock_v1(root: Path) -> bool:
    edits = {}
    anchors = {
        "gateway/run.py": (
            '        if interrupt_depth == 0:\n            agent._last_activity_ts = time.time()',
            '        if interrupt_depth == 0:\n            # ' + MARKER + '\n            from agent.idle_compaction_clock import capture\n            capture(agent)\n            agent._last_activity_ts = time.time()',
        ),
        "agent/turn_facade.py": (
            '            _review_queue.note_turn_started()',
            '            # ' + MARKER + '\n            from agent.idle_compaction_clock import capture\n            capture(self)\n            _review_queue.note_turn_started()',
        ),
        "agent/turn_context_compaction.py": (
            '    _idle_gap = time.time() - getattr(agent, "_last_activity_ts", time.time())',
            '    # ' + MARKER + '\n    from agent.idle_compaction_clock import consume\n    _idle_gap = time.time() - consume(agent)',
        ),
    }
    for name, (old, new) in anchors.items():
        path = root / name
        edits[path] = replace(path.read_text(), old, new)
    facade = root / "agent/turn_facade.py"
    edits[facade] = replace(edits[facade],
        "                    # Always clear mid-turn labels on exit",
        '                    self.__dict__.pop("_idle_compaction_previous_activity", None)\n'
        "                    # Always clear mid-turn labels on exit")
    payload = PAYLOAD.read_text()
    ast.parse(payload)
    for path, content in edits.items():
        path.write_text(content)
    (root / "agent/idle_compaction_clock.py").write_text(payload)
    return True
