# hermes-agent-setup

A **client-ready runtime bundle** for a stock agent: drag it in, fill the placeholders, and the
agent behaves correctly for a client out of the box — right reply rules, muted tool chatter, no
streaming, clean output — without an operator tuning message quality by hand.

It's a working kit, not just docs. The apply engine, rehearsal harness, reference patch module,
and memory provider all run. The reply rules live where they belong (config + the immersion
plugin), so they survive upstream bumps instead of breaking.

Open [`index.html`](index.html) for the visual build map, and [`docs/wiring.md`](docs/wiring.md)
for how a turn flows through every piece.
The scrubbed real-client rebuild checklist lives in
[`docs/canonical-client-spec.md`](docs/canonical-client-spec.md).

## What makes it client-ready

The point of the bundle is that a client can run the agent unattended. That comes from three
layers, each at the highest ladder rung that fits:

- **Reply rules → config** ([`config/config.example.yaml`](config/config.example.yaml)): no
  streaming, `tool_step_intermediate: false` (no tool-call chatter), plain messages, queue mode,
  redaction on. The durable core.
- **Message quality → the immersion plugin** ([`plugins/immersion/`](plugins/immersion)):
  `transform_llm_output` strips internal notes, suppresses interrupted placeholders, and masks
  model-failure internals; `llm_request` middleware elides stale tool-result bloat; `/mode` flips
  queue/interrupt live. This is where message-quality lives as a plugin, not fragile patches.
- **The irreducible patches → the overlay** ([`overlay/`](overlay)): only what has no config knob
  or plugin seam — redaction at the write boundary, durable runtime, the resume scheduler, the
  active-task anchor.

Plus **memory** ([`plugins/memory/`](plugins/memory), a runnable local SQLite-FTS provider and a
GBrain-backed one) and the **canonical-knowledge** wiring ([`gbrain/`](gbrain)).

## Layout

```
config/            reply rules + runtime skeleton (the drag-and-drop core), AGENTS.md
plugins/
  immersion/       reply-rules plugin: output transforms + request middleware + /mode  (runs)
  memory/          memory providers: sqlite_provider (local, runs+tested) + gbrain_provider
  telegram_platform/  disabled placeholder; Telegram hardening is not active yet
mcp-servers/
  capability-router/  clean-room capability catalog + usage-ranked search (runs+tested)
overlay/
  apply.py         apply engine — overlays the registry onto a runtime tree (runs)
  rehearse.py      verify-before-deploy harness (runs)
  registry.yaml    the manifest: irreducible patches only; everything else routed to config/plugin
  modules/         one working reference module + a copy-me template
gbrain/            canonical-knowledge wiring (references garrytan/gbrain — not vendored)
docs/              philosophy, features, plugins, canonical-knowledge, and the wiring walkthrough
install.sh         runs the pipeline: rehearse, then apply
index.html         the build map
```

## Quick start

```bash
# 1. the reply rules: copy the config skeleton into your runtime, fill <placeholders>
cp config/config.example.yaml <your-runtime>/config.yaml

# 2. install the bundled local plugins where your runtime discovers user plugins
cp -r plugins/immersion plugins/memory plugins/telegram_platform <your-plugins-dir>/

# 2b. install or prune the canonical-floor plugins named in plugins.enabled
#     (composio-onboarding, hermes-lcm, Task Ledger, Telegram Transcript, autoDream)

# 3. put mcp-servers/capability-router on PYTHONPATH and set CAPABILITY_REGISTRY
#    to mcp-servers/capability-router/registry.json

# 4. the overlay: rehearse against a pristine checkout, then apply
python overlay/rehearse.py --upstream /path/to/pristine-checkout
python overlay/apply.py --hermes-dir /path/to/runtime

# 5. wire GBrain (install it separately) — see gbrain/README.md — and restart.
```

The memory provider runs standalone if you want to see it work: `python plugins/memory/sqlite_provider.py --demo`.
Requires Python 3 and PyYAML (`pip install pyyaml`).

`plugins/telegram_platform` is currently a disabled placeholder. It leaves the bundled Telegram
adapter in place, so media timeout, liveness, PDF/document ingest, and reply-media hardening are
not active until the adapter subclasses or wraps the real bundled adapter.

## Security warning

The example config intentionally gives Telegram the full toolset and sets approvals to `off`.
That exposes shell, code execution, and web access to client/Telegram users with no approval gate.
For untrusted clients, narrow the Telegram toolset by dropping `terminal`, `code_execution`, and
`web`, or set approvals to `on`.

## The rule the whole thing follows

Before any change, take the highest rung that fits: **DELETE, CONFIG, PLUGIN, UPSTREAM, SIDECAR,
PATCH.** Patches are last because they anchor to upstream source and break on version bumps. Every
overlay entry records its `rung` and a `retire_when` condition. See [`docs/philosophy.md`](docs/philosophy.md)
and [`docs/wiring.md`](docs/wiring.md).

## Safety

Nothing here is a secret. Tokens, keys, ids, and hostnames are all `<placeholders>`. Never commit
the real ones. Redaction is the first thing you turn on, precisely because secrets in tool output
otherwise persist into transcripts and get re-sent every turn.
