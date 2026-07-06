# Plugins — what I run and which seam each uses

Plugins are rung 3 on the change ladder: real behavior changes that survive upstream bumps
because they attach to *supported* extension points instead of anchoring into source. Prefer a
plugin over a patch whenever a seam exists.

## The extension seams worth knowing

The runtime exposes far more extension surface than most people use. A user plugin can:

- `register_tool` / `register_skill` — add a tool or skill the agent can call.
- `register_platform` — add or override a chat platform adapter. A user plugin registered under
  the same key as a bundled one **replaces** it, so you can subclass the bundled adapter and
  override just the methods you need.
- `register_context_engine` — swap the context-management/compression engine.
- `register_hook(name)` / `register_middleware(kind)` — generic named hooks
  (`pre/post_tool_call`, `pre/post_llm_call`, session-lifecycle events) and middleware.
- Provider registrations — web search, browser, TTS, transcription, image/video generation.
- `register_command` — add slash commands.

The seams it does **not** offer: the transcript-*write* path, and some deep internal control
flow. Those are where you're forced down to a patch. Knowing where the seams are is what lets
you keep the patch count low.

## The plugin surfaces in this bundle

The example config also names canonical-floor runtime plugins (`composio-onboarding`,
`hermes-lcm`, `Task Ledger`, `Telegram Transcript`, `autoDream`). This repo bundles importable
floor placeholders for them under `plugins/composio_onboarding`, `plugins/hermes_lcm`,
`plugins/task_ledger`, `plugins/telegram_transcript`, and `plugins/autodream`. They register as
no-op placeholders and log what real runtime hook belongs there, so copying the whole `plugins/`
tree satisfies discovery without activating unfinished behavior.

### Memory provider
**Seam:** the memory-provider interface (`sync_turn` / `prefetch` / session-lifecycle hooks).
**What:** Long-term recall across sessions — the agent remembers durable facts and prior work,
not just the current conversation.
**Why a plugin:** memory is a first-class extension point. There's no reason to patch for it.
The provider hooks the turn cycle to write memories and prefetches relevant ones into context.
`plugins/memory.register(ctx)` now defaults to the local SQLite provider when
`register_memory_provider` exists, and logs a warning instead of crashing if the runtime has no
matching registration API.

### Platform adapter override placeholder
**Seam:** `register_platform` (same-key override of the bundled adapter).
**What:** `plugins/telegram_platform` is currently a disabled placeholder. It does not register
or activate media send timeouts, connection-liveness writes, PDF/document ingest, reply-media,
or similar delivery hardening until it subclasses or wraps the real bundled Telegram adapter.
**Why a plugin:** because same-key registration cleanly replaces the bundled adapter, a thin
subclass gets all the upstream behavior for free and overrides only what needs changing. A full
adapter copy would be pure maintenance debt — the subclass is the right shape.
**Rule:** override methods, don't fork files. When a change is a clean method override it's a
plugin; only the parts with no method seam stay as (thin) patches.

### Immersion / message quality
**Seam:** `transform_llm_output` / `llm_request` middleware / `register_command`.
**What:** Cleans client-bound output, trims stale tool-result history before provider calls, and
registers `/mode queue|interrupt` when the runtime exposes a command API.
**Why a plugin:** these are output and request-history transforms, exactly what the hook seams are
for.
**Note:** some "immersion" behaviors are actually **config** now (a busy-input mode knob), and a
few have no hook and stay patches. Check config first.
`plugins/immersion.register(ctx)` wires output transforms, request middleware, and `/mode` on a
best-effort basis when those runtime APIs exist; missing APIs log or skip instead of failing
plugin discovery.

## How plugins load

User plugins live in a plugins directory and/or are discovered as installed packages. A user
plugin registered under the same key as a bundled plugin wins discovery and replaces it — that's
what makes the subclass-override pattern work. Keep each plugin a self-contained package with its
own install step so it's idempotent to (re)install. When using the bundled tree directly, copy
`plugins/*` together; the floor placeholders share `plugins/_floor.py` for their no-op
registration helper.
