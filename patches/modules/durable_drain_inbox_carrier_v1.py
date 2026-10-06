#!/usr/bin/env python3
"""Install the durable inbox at the pinned split gateway owners."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

# This reviewed payload still supplies the current inbox and replay-store code.
NATIVE_PAYLOAD_DIR = Path(__file__).resolve().parents[1] / "payloads" / "durable-drain-inbox-d363-v1"
CURRENT_MARKER = "HERMES_DURABLE_DRAIN_CURRENT_SPLIT_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_added_file(payload: Path, relative: str) -> str:
    """Return one complete new-file image embedded in the reviewed native payload.

    The full inbox implementation remains the reviewed, checksummed carrier
    payload.  Current Hermes retained its invariants but moved the lifecycle
    owners, so this helper deliberately reuses that source image instead of
    re-creating a second, subtly different mailbox implementation.
    """
    header = f"diff --git a/{relative} b/{relative}\\n".replace("\\n", "\n")
    source = payload.read_text(encoding="utf-8")
    start = source.find(header)
    if start < 0:
        raise RuntimeError(f"durable current source payload is missing {relative}")
    end = source.find("\\ndiff --git ".replace("\\n", "\n"), start + len(header))
    section = source[start:] if end < 0 else source[start:end]
    lines = []
    for line in section.splitlines(keepends=True):
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    result = "".join(lines)
    if not result.startswith('"""'):
        raise RuntimeError(f"durable current source image is malformed: {relative}")
    compile(result, relative, "exec")
    return result


def _current_replace_once(source: str, old: str, new: str, label: str) -> str:
    # Keep the many multi-line anchors legible in this carrier source while
    # accepting escaped newlines from the generated patch module.
    old = old.replace("\\n", "\n")
    new = new.replace("\\n", "\n")
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"durable current {label} anchor count is {count}, expected 1")
    return source.replace(old, new, 1)


def _current_replace_one_of(
    source: str, replacements: tuple[tuple[str, str], ...], label: str
) -> str:
    """Apply exactly one reviewed current-shape alternative."""
    matches = []
    for old, new in replacements:
        old = old.replace("\\n", "\n")
        new = new.replace("\\n", "\n")
        if source.count(old) == 1:
            matches.append((old, new))
    if len(matches) != 1:
        raise RuntimeError(f"durable current {label} compatible anchors are {len(matches)}, expected 1")
    old, new = matches[0]
    return source.replace(old, new, 1)


def _current_write(path: Path, before: str, after: str) -> None:
    compile(after, str(path), "exec")
    backup = path.with_name(path.name + ".bak-pre-durable-drain-current-v1")
    if not backup.exists():
        backup.write_text(before, encoding="utf-8")
    path.write_text(after, encoding="utf-8")


def _current_replay_store_updates(root: Path, payload: Path) -> list[tuple[Path, str, str]]:
    """Port the reviewed replay-store methods omitted by the split carrier.

    Validate both owners before writing either; an installed runtime marker
    cannot stand in for the persistence contract used by admission and replay.
    """
    patch = payload.read_text(encoding="utf-8")
    updates = []
    for relative, owner, anchor, names in (
        ("gateway/session_transcript.py", "SessionTranscriptMixin", "    def rewrite_transcript(",
         {"replay_marker_status", "replay_marker_status_for_session_key", "persist_replay_marker"}),
        ("hermes_state_messages.py", "SessionMessagesMixin", "    def has_platform_message_id(",
         {"has_platform_message_id_for_session_key"}),
    ):
        section = patch.split(f"+++ b/{relative}\n", 1)[1].split("\ndiff --git ", 1)[0]
        methods = "".join(line[1:] for line in section.splitlines(keepends=True)
                          if line.startswith("+") and not line.startswith("+++"))
        expected = ast.parse(f"class {owner}:\n" + methods).body[0]
        expected_methods = {node.name: ast.dump(node) for node in expected.body
                            if isinstance(node, ast.FunctionDef)}
        if set(expected_methods) != names:
            raise RuntimeError(f"durable replay payload owner drift: {relative}")
        path = root / relative
        before = path.read_text(encoding="utf-8")
        classes = [node for node in ast.parse(before).body
                   if isinstance(node, ast.ClassDef) and node.name == owner]
        if len(classes) != 1:
            raise RuntimeError(f"durable replay store owner drift: {relative}")
        existing = {node.name: ast.dump(node) for node in classes[0].body
                    if isinstance(node, ast.FunctionDef) and node.name in names}
        if existing:
            if existing != expected_methods:
                raise RuntimeError(f"durable replay store method drift: {relative}")
            continue
        after = _current_replace_once(before, anchor, methods + anchor, f"replay store {relative}")
        compile(after, str(path), "exec")
        updates.append((path, before, after))
    return updates


def _patch_current_split_durable_drain(root: Path) -> bool:
    """Re-ground the durable inbox at the current split gateway owners.

    This is intentionally an all-or-nothing source transformation: an event is
    durably claimed before a side-effecting hook or turn, a claimed crash stays
    ambiguous, and a completed replay gets a canonical session marker before
    acknowledgement.  The current shutdown spool is complementary recovery for
    in-memory pending text; it cannot substitute for those admission invariants.
    """
    root = Path(root).resolve()
    run = root / "gateway/run.py"
    inbound = root / "gateway/run_inbound.py"
    startup = root / "gateway/run_startup.py"
    shutdown = root / "gateway/run_shutdown.py"
    adapters = root / "gateway/run_adapters.py"
    base = root / "gateway/platforms/base.py"
    required = (run, inbound, startup, shutdown, adapters, base)
    if not all(path.is_file() for path in required):
        raise RuntimeError("durable current split owners are incomplete")
    payload = NATIVE_PAYLOAD_DIR / "native-durable-drain.patch"
    manifest = json.loads((NATIVE_PAYLOAD_DIR / "manifest.json").read_text(encoding="utf-8"))
    if _sha256(payload) != manifest.get("patch_sha256"):
        raise RuntimeError("durable current source payload checksum mismatch")
    replay_updates = _current_replay_store_updates(root, payload)
    if CURRENT_MARKER in run.read_text(encoding="utf-8"):
        for path, before, after in replay_updates:
            _current_write(path, before, after)
        return bool(replay_updates)
    additions = {
        relative: _extract_added_file(payload, relative).replace("_adapter_for_source", "_delivery_adapter_for")
        for relative in ("gateway/drain_inbox.py", "gateway/run_durable_drain.py")
    }
    async_dispatch_hook = "async def _hm_pre_gateway_dispatch_hook(" in inbound.read_text(encoding="utf-8")
    if async_dispatch_hook:
        additions = {relative: content.replace("event = self._hm_pre_gateway_dispatch_hook(",
                                               "event = await self._hm_pre_gateway_dispatch_hook(")
                     for relative, content in additions.items()}
    for relative, content in additions.items():
        target = root / relative
        if target.exists():
            raise RuntimeError(f"durable current carrier refuses existing owner: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    event_owner = root / "gateway/platforms/event.py"
    if not event_owner.is_file():
        event_owner = base
    event_before = event_owner.read_text(encoding="utf-8")
    event_after = _current_replace_once(
        event_before,
        "    allow_gateway_control: bool = True\\n",
        "    allow_gateway_control: bool = True\\n"
        "    durable_ingress: bool = False\\n"
        "    admission_checked: bool = False\\n"
        "    pre_dispatch_attempted: bool = False\\n"
        "    durable_replay: bool = False\\n"
        "    processing_receipt: dict = field(default_factory=dict)\\n\\n"
        "    @property\\n"
        "    def durable_deferred(self) -> bool:\\n"
        "        return bool(self.processing_receipt.get(\"deferred\"))\\n\\n"
        "    @durable_deferred.setter\\n"
        "    def durable_deferred(self, value: bool) -> None:\\n"
        "        self.processing_receipt[\"deferred\"] = bool(value)\\n\\n"
        "    @property\\n"
        "    def handler_succeeded(self) -> bool:\\n"
        "        return bool(self.processing_receipt.get(\"succeeded\"))\\n\\n"
        "    @handler_succeeded.setter\\n"
        "    def handler_succeeded(self, value: bool) -> None:\\n"
        "        self.processing_receipt[\"succeeded\"] = bool(value)\\n\\n"
        "",
        "MessageEvent durable fields",
    )
    base_before = base.read_text(encoding="utf-8")
    base_after = event_after if event_owner == base else base_before
    base_after = _current_replace_once(
        base_after,
        "    async def handle_message(self, event: MessageEvent) -> None:\\n"
        "        \"\"\"Process an incoming message; returns quickly by spawning a background\\n",
        "    def set_startup_gate_handler(self, handler) -> None:\\n"
        "        self._startup_gate_handler = handler\\n\\n"
        "    async def _preflight_startup_gate(self, event: MessageEvent) -> bool:\\n"
        "        if event.admission_checked or event.durable_replay:\\n"
        "            return False\\n"
        "        if event.allow_gateway_control:\\n"
        "            coerce_plaintext_gateway_command(event)\\n"
        "        owner = self._session_key_profile(event.source)\\n"
        "        if owner and not event.source.profile:\\n"
        "            event.source.profile = owner\\n"
        "        handler = getattr(self, \"_startup_gate_handler\", None)\\n"
        "        if handler is not None and await handler(event, self._event_session_key(event)):\\n"
        "            return True\\n"
        "        event.admission_checked = True\\n"
        "        return False\\n\\n"
        "    async def handle_message(self, event: MessageEvent) -> None:\\n"
        "        \"\"\"Process an incoming message; returns quickly by spawning a background\\n",
        "BasePlatformAdapter preflight",
    )
    base_after = _current_replace_once(
        base_after,
        '        if event.allow_gateway_control:\\n            coerce_plaintext_gateway_command(event)\\n        # Identity FIRST: every key below (routing check, guard lookup, batch lane) derives from it.\\n        if self._drop_unresolved(event):\\n            return\\n',
        '        if self._drop_unresolved(event):\\n            return\\n        if await self._preflight_startup_gate(event):\\n            return\\n',
        "BasePlatformAdapter preflight call",
    )
    base_after = _current_replace_once(
        base_after,
        "        if session_key in self._active_sessions:\\n"
        "            await self._handle_message_while_active(event, session_key)\\n",
        "        if session_key in self._active_sessions:\\n"
        "            if event.durable_replay:\\n"
        "                event.durable_deferred = True\\n"
        "                return\\n"
        "            await self._handle_message_while_active(event, session_key)\\n",
        "BasePlatformAdapter durable busy replay",
    )
    base_after = _current_replace_once(
        base_after,
        "            response = await self._message_handler(event)\n",
        "            response = await self._message_handler(event)\n"
        "            event.handler_succeeded = True\n",
        "BasePlatformAdapter durable success receipt",
    )

    inbound_before = inbound.read_text(encoding="utf-8")
    inbound_after = _current_replace_once(
        inbound_before,
        "    async def _hm_admit_event(\\n        self, event: \"MessageEvent\"\\n    )",
        "    async def _hm_admit_event(\\n        self, event: \"MessageEvent\", *, durable_admission: bool = False, durable_finalize: bool = False\\n    )",
        "inbound signature",
    )
    inbound_after = _current_replace_once(
        inbound_after,
        "        if (\\n            getattr(self, \"_startup_restore_in_progress\", False)\\n"
        "            and not is_internal\\n"
        "            and not getattr(event, \"_hermes_startup_restore_replay\", False)\\n"
        "        ):\\n            self._queue_startup_restore_event(event)\\n            return None\\n\\n"
        "        if is_internal:\\n",
        "        if event.durable_replay and (\\n"
        "            self._draining or self._external_drain_active\\n"
        "            or getattr(self, \"_startup_restore_in_progress\", False)\\n"
        "        ):\\n            event.durable_deferred = True\\n            return None\\n"
        "        if durable_admission:\\n            return await self._durable_admit_event(event)\\n"
        "        if (not durable_finalize and not event.durable_replay\\n"
        "                and self._durable_gate_active()\\n"
        "                and not self._durable_control_event(event, self._session_key_for_source(source))):\\n"
        "            self._durable_setup()\\n"
        "            async with self._durable_admission_lock:\\n"
        "                return await self._durable_admit_event(event)\\n\\n"
        "        if is_internal:\\n",
        "inbound startup gate",
    )
    hook_call = ("await " if async_dispatch_hook else "") + "self._hm_pre_gateway_dispatch_hook(event, source)"
    inbound_after = _current_replace_once(
        inbound_after,
        "        event = " + hook_call + "\n",
        "        if not event.pre_dispatch_attempted:\n"
        "            event = " + hook_call + "\n",
        "inbound pre-dispatch receipt",
    )
    if event_owner != base:
        _current_write(event_owner, event_before, event_after)
    _current_write(base, base_before, base_after)
    _current_write(inbound, inbound_before, inbound_after)

    adapters_before = adapters.read_text(encoding="utf-8")
    adapters_after = _current_replace_once(
        adapters_before,
        "        adapter.set_session_store(self.session_store)\\n",
        "        adapter.set_session_store(self.session_store)\\n"
        "        adapter.set_startup_gate_handler(self._make_durable_admission_handler(adapter))\\n",
        "adapter admission handler",
    )
    _current_write(adapters, adapters_before, adapters_after)

    run_before = run.read_text(encoding="utf-8")
    run_after = _current_replace_once(
        run_before,
        "from gateway.run_startup import GatewayStartupMixin\\n",
        "from gateway.run_startup import GatewayStartupMixin\\n"
        "from gateway.run_durable_drain import GatewayDurableDrainMixin\\n",
        "GatewayRunner durable import",
    )
    mro_tail = "    GatewayAgentCacheMixin, GatewayProfileReconcileMixin):\\n" if "GatewayAgentCacheMixin, GatewayProfileReconcileMixin):" in run_after else "    GatewayAgentCacheMixin):\\n"
    if "GatewayAgentCacheMixin, GatewayProfileReconcileMixin, GatewayPluginRewireMixin):" in run_after:
        mro_tail = "    GatewayAgentCacheMixin, GatewayProfileReconcileMixin, GatewayPluginRewireMixin):\n"
    run_after = _current_replace_once(
        run_after,
        mro_tail,
        mro_tail.replace("):", ", GatewayDurableDrainMixin):"),
        "GatewayRunner durable MRO",
    )
    run_after = _current_replace_once(
        run_after,
        "        self._startup_restore_queue: List[MessageEvent] = []\\n",
        f"        self._init_durable_drain()  # {CURRENT_MARKER}\\n",
        "GatewayRunner durable initialization",
    )
    _current_write(run, run_before, run_after)

    startup_before = startup.read_text(encoding="utf-8")
    startup_after = startup_before
    startup_after = _current_replace_once(
        startup_after,
        "            self._startup_restore_in_progress = False\n        if drained:\n",
        "            self._startup_restore_in_progress = False\n            self._schedule_durable_replay()\n        if drained:\n",
        "startup durable replay release",
    )
    startup_after = _current_replace_once(
        startup_after,
        "        self._startup_restore_queue = []\\n"
        "        self._startup_restore_tasks = []\\n",
        "        await self._claim_durable_producer()\\n"
        "        self._startup_restore_tasks = []\\n",
        "startup durable lease",
    )
    _current_write(startup, startup_before, startup_after)

    shutdown_before = shutdown.read_text(encoding="utf-8")
    shutdown_after = _current_replace_once(
        shutdown_before,
        "        self._update_runtime_status(self._serving_state())\\n"
        "\\n    async def _drain_control_watcher",
        "        self._update_runtime_status(self._serving_state())\\n"
        "        self._schedule_durable_replay()\\n"
        "\\n    async def _drain_control_watcher",
        "shutdown durable replay",
    )
    _current_write(shutdown, shutdown_before, shutdown_after)
    for path, before, after in replay_updates:
        _current_write(path, before, after)
    return True


def _patch_external_drain_fixture(root: Path) -> bool:
    """Prove Golden's durable admission instead of upstream's text refusal."""
    path = root / "tests/gateway/test_external_drain_control.py"
    if not path.is_file():
        return False
    before = path.read_text(encoding="utf-8")
    if "# Golden durable drain admission proof" in before:
        return False
    old = '        assert result is not None\n        assert "draining" in result.lower()\n'
    if old not in before:
        return False
    setup = "        runner._external_drain_active = True\n        event = MessageEvent("
    after = _current_replace_once(before, setup,
        "        runner._external_drain_active = True\n"
        "        runner.session_store.replay_marker_status_for_session_key.return_value = False\n"
        "        await runner._claim_durable_producer()\n"
        "        event = MessageEvent(", "external drain producer fixture")
    after = _current_replace_once(after, old,
        "        # Golden durable drain admission proof\n"
        "        assert result is None\n"
        "        from gateway.drain_inbox import pending_records\n"
        "        records = pending_records(runner._durable_inbox_path)\n"
        "        assert len(records) == 1\n"
        "        assert records[0][\"message_id\"] == \"m1\"\n"
        "        assert records[0][\"text\"] == \"hello\"\n"
        "        assert records[0][\"state\"] == \"queued\"\n",
        "external drain durable proof")
    compile(after, str(path), "exec")
    path.write_text(after, encoding="utf-8")
    return True


def patch_durable_drain_runtime_v1(root: Path) -> bool:
    """Apply the current lifecycle carrier; incomplete owners fail before writes."""
    root = Path(root)
    changed = _patch_current_split_durable_drain(root)
    return _patch_external_drain_fixture(root) or changed
