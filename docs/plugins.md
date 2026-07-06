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

### Memory provider
**Seam:** the memory-provider interface (`sync_turn` / `prefetch` / session-lifecycle hooks).
**What:** Long-term recall across sessions — the agent remembers durable facts and prior work,
not just the current conversation.
**Why a plugin:** memory is a first-class extension point. There's no reason to patch for it.
The provider hooks the turn cycle to write memories and prefetches relevant ones into context.

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

### Immersion / responsiveness
**Seam:** `transform_llm_output` / status hooks.
**What:** Makes the agent feel present — acknowledging input while it's working, status emission,
busy-input handling.
**Why a plugin:** these are output/lifecycle transforms, exactly what the hook seams are for.
**Note:** some "immersion" behaviors are actually **config** now (a busy-input mode knob), and a
few have no hook and stay patches. Check config first.

## How plugins load

User plugins live in a plugins directory and/or are discovered as installed packages. A user
plugin registered under the same key as a bundled plugin wins discovery and replaces it — that's
what makes the subclass-override pattern work. Keep each plugin a self-contained package with its
own install step so it's idempotent to (re)install.
