from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "overlay" / "modules" / "telegram_dm_topic_recovery_root_guard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("telegram_dm_topic_recovery_root_guard", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _write_base_fixture(root: Path) -> Path:
    target = root / "gateway" / "platforms" / "base.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        '''import dataclasses

def _platform_name(platform):
    return getattr(platform, "value", platform)

class MessageEvent:
    pass

class BasePlatformAdapter:
    def _apply_topic_recovery(self, event: MessageEvent) -> None:
        """Rewrite ``event.source.thread_id`` in place if the hook returns one."""
        recover = getattr(self, "_topic_recovery_fn", None)
        if recover is None:
            return
        source = getattr(event, "source", None)
        if source is None:
            return
        try:
            recovered = recover(source)
        except Exception:
            logger.debug("topic recovery hook failed", exc_info=True)
            return
        if recovered is None or str(recovered) == str(source.thread_id or ""):
            return
        try:
            event.source = dataclasses.replace(source, thread_id=str(recovered))
        except Exception:
            logger.debug("topic recovery rewrite failed", exc_info=True)

    def set_busy_session_handler(self, handler):
        pass
''',
        encoding="utf-8",
    )
    return target


def test_patch_adds_plain_root_guard(tmp_path):
    module = _load_module()
    target = _write_base_fixture(tmp_path)

    assert module.apply(target) == "applied"
    patched = target.read_text(encoding="utf-8")

    assert module.MARKER in patched
    assert 'and not getattr(event, "reply_to_message_id", None)' in patched
    assert 'str(getattr(source, "thread_id", None) or "") in {"", "1"}' in patched


def test_patch_is_idempotent(tmp_path):
    module = _load_module()
    target = _write_base_fixture(tmp_path)

    assert module.apply(target) == "applied"
    assert module.apply(target) == "already"
