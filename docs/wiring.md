# How it all wires together

The pieces (config, overlay, plugins, MCP, memory, GBrain) aren't independent — they're stages a
message passes through. This traces one turn end to end so you can see where each piece sits and
why it's on the ladder rung it's on.

## The stack, bottom to top

```
                        ┌─────────────────────────────────────────┐
   client message  ──▶  │ PLATFORM ADAPTER (bundled Telegram)       │  plugin hardening inactive
                        └───────────────────┬─────────────────────┘
                                            ▼
                        ┌─────────────────────────────────────────┐
                        │ GATEWAY  (stock runtime + OVERLAY)       │  redaction, durable runtime,
                        │                                          │  resume scheduler, active-task
                        └───────────────────┬─────────────────────┘
                                            ▼
          prefetch  ◀── MEMORY (plugins/memory)  ── recall relevant facts into context
          lookup    ◀── GBRAIN (gbrain/)         ── canonical facts, on demand ("don't guess")
          route     ◀── CAPABILITY ROUTER        ── hot catalog search, shared tools lazy
                                            ▼
                        ┌─────────────────────────────────────────┐
                        │ LLM REQUEST  (immersion llm_request mw)  │  elide stale tool results,
                        │                                          │  drop orphan tool calls
                        └───────────────────┬─────────────────────┘
                                            ▼
                                     model provider
                                            ▼
                        ┌─────────────────────────────────────────┐
                        │ OUTPUT TRANSFORM (immersion hooks)       │  strip internal notes,
                        │                                          │  suppress interrupted, mask
                        │                                          │  failures  → clean reply
                        └───────────────────┬─────────────────────┘
                                            ▼
                        ┌─────────────────────────────────────────┐
                        │ REPLY RULES (config)                     │  no streaming, no tool
                        │                                          │  chatter, plain messages
                        └───────────────────┬─────────────────────┘
                                            ▼
                                      client reply
                        SESSION WRITE (overlay redaction) ── scrub secrets before persist
                        MEMORY sync_turn ── persist durable facts learned this turn
```

## A turn, step by step

1. **Message arrives** at the bundled platform adapter. `plugins/telegram_platform` is a disabled
   placeholder, so media timeout, liveness, PDF/document ingest, and reply-media hardening are not
   active until it subclasses or wraps the real bundled Telegram adapter.
2. **Gateway + overlay.** The stock runtime runs, with the overlay's irreducible patches active:
   redaction guards the write boundary, durable runtime and the resume scheduler keep the turn
   alive across restarts, the active-task anchor pins what you asked for.
3. **Context assembly.** Memory (`plugins/memory`) prefetches relevant recalled facts. When the
   turn needs an authoritative fact, the agent looks it up in **GBrain** (`gbrain/`) rather than
   guessing — that's the canonical layer.
4. **Capability routing.** The hot `capability-router` MCP searches the bundled catalog before
   heavier shared-floor MCPs load on demand. Its registry lives in
   `mcp-servers/capability-router/registry.json`; `record_capability_outcome` stores
   success/failure feedback in the usage DB so working tools rank up and failing tools rank down.
5. **LLM request middleware** (`immersion/middleware.py`) cleans the request history: stale
   oversized tool results get elided (the tool-mute on the context side), orphan tool outputs get
   dropped so the provider doesn't reject them.
6. **Model call**, using config `model.default` / `model.provider` and the
   `fallback_providers` chain guarded by `providers.fallback`, so a provider blip degrades
   instead of failing.
7. **Output transform** (`immersion/hooks.py`) cleans the outbound message: internal notes
   stripped, interrupted-placeholders suppressed, provider failures masked to a clean generic.
8. **Reply rules** (config) decide *how* it's delivered: no streaming, no intermediate tool-step
   chatter, plain (not rich) messages. This is why a client sees clean output with no operator
   tuning.
9. **Session write** persists the transcript — with redaction scrubbing any secrets first — and
   **memory `sync_turn`** stores durable facts the agent learned.

## Why each piece is where it is (the ladder in practice)

| Concern | Lives in | Rung | Why |
|---|---|---|---|
| Reply rules (streaming, mute, rich) | `config/` | CONFIG | Upstream knobs exist; a config edit is the most durable form. |
| Message-quality transforms (strip notes, suppress interrupted, mask failures, redact secrets from output) | `plugins/immersion` | PLUGIN | `transform_llm_output` is a supported seam. This is where the old message-quality *patches* belong. |
| Tool-result history hygiene | `plugins/immersion` | PLUGIN | `llm_request` middleware — same reason. |
| 10-min human-voiced progress updates | `bin/progress-compose.py` + `tasks/` | TOOL | A scheduled tool, not a gateway patch. |
| Shared tool routing | `mcp_policy` + `mcp_servers` + `mcp-servers/capability-router/` | CONFIG + SIDECAR | Capability-router stays hot; heavier floor MCPs load on demand; usage outcomes adjust catalog ranking. |
| Media / adapter hardening | `plugins/telegram_platform` | PLUGIN | Disabled placeholder; hardening is not active until it wraps the bundled adapter. |
| Memory | `plugins/memory` | PLUGIN | Memory-provider is a first-class seam. |
| Canonical knowledge | `gbrain/` | EXTERNAL | A product you install and wire, not code you own. |
| Write-boundary redaction, durable runtime, resume, active-task, autoraise-notice | `overlay/` | PATCH | No seam exists at these layers; the ~5 irreducible patches. |

The pattern: **push everything as high up the ladder as it will go.** The reply rules that make
the agent client-ready live in config and the immersion plugin — durable, drag-and-drop — not in
fragile patches. Only the handful of behaviors with no seam stay as overlay patches.
