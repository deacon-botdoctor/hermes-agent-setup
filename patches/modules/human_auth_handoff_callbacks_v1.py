#!/usr/bin/env python3
"""Install the capability-bound ``hah:`` Telegram callback namespace."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_HUMAN_AUTH_HANDOFF_CALLBACKS_v1"
OLD_MARKER = "HERMES_HUMAN_AUTH_HANDOFF_CALLBACKS_v0"

BLOCK = r'''        # HERMES_HUMAN_AUTH_HANDOFF_CALLBACKS_v1
        # Human-only website auth handoff (hah:done|skip:<id>).
        if data.startswith("hah:"):
            import asyncio as _hah_asyncio
            import os as _hah_os
            import re as _hah_re
            import subprocess as _hah_subprocess
            import sys as _hah_sys
            from pathlib import Path as _HahPath

            parts = data.split(":", 3)
            if (
                len(parts) != 4
                or parts[1] not in {"done", "skip"}
                or not _hah_re.fullmatch(r"[a-f0-9]{24}", parts[2])
                or not _hah_re.fullmatch(r"[A-Za-z0-9_-]{22}", parts[3])
            ):
                await query.answer(text="Invalid login handoff data.")
                return

            caller_id = str(getattr(query.from_user, "id", ""))
            if not self._is_callback_user_authorized(
                caller_id,
                chat_id=query_chat_id,
                chat_type=str(query_chat_type) if query_chat_type is not None else None,
                thread_id=str(query_thread_id) if query_thread_id is not None else None,
                user_name=query_user_name,
            ):
                await query.answer(text="You are not authorized to resolve this login handoff.")
                return

            home = _HahPath(
                _hah_os.environ.get("HERMES_HOME") or (_HahPath.home() / ".hermes")
            ).expanduser()
            script = home / "bin/human_auth_handoff.py"

            def _hah_resolve():
                return _hah_subprocess.run(
                    [
                        _hah_sys.executable,
                        str(script),
                        "resolve",
                        "--handoff-id",
                        parts[2],
                        "--result",
                        parts[1],
                        "--caller-id",
                        caller_id,
                    ],
                    text=True,
                    capture_output=True,
                    input=parts[3] + "\n",
                    timeout=15,
                    check=False,
                )

            proc = await _hah_asyncio.to_thread(_hah_resolve)
            if proc.returncode != 0:
                await query.answer(text="This login handoff is stale or already resolved.")
                return

            label = "Done" if parts[1] == "done" else "Skipped"
            await query.answer(text=label)
            try:
                await query.edit_message_text(
                    text=f"{query.message.text or 'Login handoff'}\n\n{label}.",
                    reply_markup=None,
                )
            except Exception:
                pass
            return

'''


def patch_human_auth_handoff_callbacks_v1(hermes_dir: Path) -> bool:
    candidates = [
        hermes_dir / "plugins/platforms/telegram/adapter.py",
        hermes_dir / "gateway/platforms/telegram.py",
    ]
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        raise RuntimeError("Telegram adapter source not found")
    source = path.read_text(encoding="utf-8")
    anchor = '''        # --- Exec approval callbacks (ea:choice:id) ---
        if data.startswith("ea:"):
'''
    if MARKER in source:
        return False
    if OLD_MARKER in source:
        old_start = source.index(f"        # {OLD_MARKER}")
        hah_if = source.index('        if data.startswith("hah:"):', old_start)
        old_end = None
        cursor = source.find("\n", hah_if) + 1
        while cursor > 0 and cursor < len(source):
            next_line = source.find("\n", cursor)
            if next_line < 0:
                next_line = len(source)
            line = source[cursor:next_line]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= 8:
                old_end = cursor
                break
            cursor = next_line + 1
        if old_end is None:
            raise RuntimeError(f"v0 callback block end not found in {path}")
        updated = source[:old_start] + BLOCK + source[old_end:]
    else:
        if anchor not in source:
            raise RuntimeError(f"callback insertion anchor not found in {path}")
        updated = source.replace(anchor, BLOCK + anchor, 1)
    path.write_text(updated, encoding="utf-8")
    return True

D363_HANDLER_MARKER = "HERMES_HUMAN_AUTH_HANDOFF_CALLBACKS_v1_d363"
D363_DISPATCH_OLD = '''            ("gt:", self._handle_gmail_triage_callback), ("ea:", self._handle_exec_approval_callback),
'''
D363_DISPATCH_NEW = '''            ("gt:", self._handle_gmail_triage_callback), ("hah:", self._handle_human_auth_callback), ("ea:", self._handle_exec_approval_callback),
'''
D363_HANDLER = '''    async def _handle_human_auth_callback(self, query, data: str, cb: Dict[str, Any]) -> None:
        # HERMES_HUMAN_AUTH_HANDOFF_CALLBACKS_v1_d363: capability is stdin-only.
        parts = data.split(":", 3)
        if len(parts) != 4 or parts[1] not in {"done", "skip"}:
            await query.answer(text="Invalid login handoff data.")
            return
        import asyncio, os, re, subprocess, sys
        from pathlib import Path
        if not re.fullmatch(r"[a-f0-9]{24}", parts[2]) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", parts[3]):
            await query.answer(text="Invalid login handoff data.")
            return
        if not await self._callback_authorized(query, cb, "You are not authorized to resolve this login handoff."):
            return
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        def resolve():
            return subprocess.run([sys.executable, str(home / "bin" / "human_auth_handoff.py"), "resolve",
                                   "--handoff-id", parts[2], "--result", parts[1], "--caller-id",
                                   str(getattr(query.from_user, "id", ""))], input=parts[3] + "\\n",
                                  text=True, capture_output=True, timeout=15, check=False)
        proc = await asyncio.to_thread(resolve)
        if proc.returncode:
            await query.answer(text="This login handoff is stale or already resolved.")
            return
        await query.answer(text="Done" if parts[1] == "done" else "Skipped")
'''

def _patch_d363_callbacks(root: Path) -> bool:
    path = root / "plugins/platforms/telegram/adapter.py"
    source = path.read_text(encoding="utf-8")
    if D363_HANDLER_MARKER in source:
        return False
    if D363_DISPATCH_OLD not in source or "    async def _claim_callback_state(" not in source:
        raise RuntimeError("d363 Telegram callback anchor drift")
    patched = source.replace(D363_DISPATCH_OLD, D363_DISPATCH_NEW, 1).replace(
        "    async def _claim_callback_state(", D363_HANDLER + "\n    async def _claim_callback_state(", 1)
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8")
    return True

_old_callback_patch = patch_human_auth_handoff_callbacks_v1
def patch_human_auth_handoff_callbacks_v1(hermes_dir: Path) -> bool:
    path = Path(hermes_dir) / "plugins/platforms/telegram/adapter.py"
    source = path.read_text(encoding="utf-8") if path.is_file() else ""
    if D363_DISPATCH_OLD in source or D363_HANDLER_MARKER in source:
        return _patch_d363_callbacks(Path(hermes_dir))
    return _old_callback_patch(hermes_dir)
