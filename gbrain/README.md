# gbrain/ — wiring the canonical-knowledge layer

GBrain is the canonical-knowledge engine: the source of truth for durable facts the agent must
get *right* (identities, decisions, config source-of-truth). It's distinct from the agent's own
memory (`plugins/memory/`) — memory is what the agent recalls, GBrain is what's authoritative.
On conflict, GBrain wins.

**GBrain is a separate product** — https://github.com/garrytan/gbrain — that you install and run.
This directory is the *wiring*, not a copy of it. Install GBrain per its own docs; the glue below
points the agent at your running brain.

## The standard wiring block

Every client gets the same four wiring decisions:

1. **Compiled-binary launcher, never a source checkout.** Run the packaged `gbrain` binary, not a
   dev/bun checkout — the runtime should depend on a stable launcher on PATH.
2. **Capability-router registration.** Register GBrain as the agent's canonical-lookup capability
   so the "look it up, don't guess" rule (see `../config/AGENTS.example.md`) has something to call.
3. **Chat model for the router.** A small, cheap model (e.g. a `gpt-4o-mini`-class model) drives
   GBrain's own query routing — you don't need a frontier model for lookups.
4. **Postgres engine with `ANALYZE`'d content.** Use the Postgres engine and keep statistics
   fresh (`ANALYZE content_chunks`) so hybrid search stays fast as the brain grows.

## Two ways the agent reaches the brain

- **As a tool / MCP** — expose `gbrain query` / `gbrain search` / `gbrain get` to the agent so it
  can look facts up mid-turn. This is what the AGENTS.md "look it up, don't guess" rule invokes.
- **As a memory backend** — point the memory plugin at GBrain (`plugins/memory/gbrain_provider.py`)
  so facts the agent learns in chat feed the same brain. Optional; the local SQLite provider is a
  fine default if you keep memory and canonical knowledge separate.

## Config

See `../config/config.example.yaml`:

```yaml
gbrain:
  enabled: true
  chat_model: <provider:model>   # small/cheap router model
  engine: postgres
```

## The rule that makes it worth wiring

> For any filed, durable fact, look it up in GBrain before answering. On conflict, GBrain wins.
> If it's unreachable, say the lookup didn't run — do not substitute a stale guess.

That rule lives in the agent's instructions (`../config/AGENTS.example.md`). Without it, a brain
full of correct facts still loses to the model's confident guess. The wiring and the rule are one
feature.
