# What the current runtime changes

## Exact release

The current public release is `botdoctor-hermes-2026.07.28`:

- upstream Hermes: `f228e145ba35cbbf785eded2021ae6682285b91b`;
- public Golden source: `1e9cf546cdeff7be26109a2c2c407510a8f3726d`;
- runtime payload: 79 files;
- assembled runtime fingerprint:
  `7be0ac0aaf681d0e09e9357d227da5b78afb6a98294ebdcd708802b03954b03d`.

Machines do not install “latest.” They build and verify these exact identities.
Private fleet health probes, host routes, and service adapters are deliberately
not part of this public source manifest; native service tooling owns public
installations.

## Native first

Hermes still owns the model loop, native Codex compaction, sessions,
`state.db`, memory/search, tasks, goals, cron, gateway, platforms, service
installation, and most tool behavior.

The public layer does not add LCM, Anamnesis, AutoDream, a general-purpose
conversation recall database, automatic broad replay, or a second scheduler.
Its bounded Telegram continuity index is profile-local and reads only the exact
chat/topic needed for fresh-topic rehydration and current-topic search. It
keeps optional MCPs and browser automation cold until a real request needs them.

## Three small configuration choices

- Native Tool Search is on so large skill/MCP catalogs can be discovered
  lazily instead of injected into every model request.
- Telegram replies remain unthreaded by default.
- The MCP control and capability-router tools stay visible while optional
  backends remain cold.

The setup preserves local model/provider choices and removes retired Golden
values only when they still exactly match the old defaults.

## Runtime additions

- **Immediate Telegram typing:** acknowledge accepted work before slow
  pre-model preparation.
- **Model-authored long-work checkpoints:** periodic Telegram updates summarize
  real model commentary; they do not invent tool progress or make a second
  model call.
- **Topic/session continuity:** fresh Telegram topics and session search remain
  scoped to the exact chat/thread.
- **Durable restart drain:** accepted messages, scheduled work, and restart
  recovery have one durable lifecycle.
- **Contextual interruption recovery:** after an interrupted restart, ask the
  originating user what to do next instead of silently repeating an uncertain
  action.
- **Lazy MCP activation:** Tool Search can discover a cold approved backend,
  activate it, refresh schemas, and continue in the same turn.
- **Runtime-root coherence:** lazy imports cannot combine the active gateway
  with an older mutable checkout.
- **Telegram transaction proof:** a local ledger can join exact ingress to final
  delivery for canaries and incident diagnosis.
- **Windows task identity:** named profiles stay bound to their explicitly owned
  Scheduled Task and state namespace.
- **Codex 401 spend guard:** one bounded client-turn fallback may run while
  recursive/internal paid fallbacks remain blocked.
- **LLM attempt receipts:** each observed main or auxiliary provider attempt
  writes a content-free local receipt with route, outcome, and usage evidence.
  Receipt failures are logged but never block the model path; reconciliation
  resolves the active installed runtime and treats missing, empty, or zero usage
  as unavailable while retaining a provider-reported cost.

The authoritative per-patch reason, target, test, rollback, and retirement
condition are in [`patches/registry.yaml`](patches/registry.yaml). There are 17
cohesive registry entries in this release.

## Profile additions

The bounded profile installer places only manifest-owned files:

- two enabled plugins for output hygiene and on-demand MCP control, plus
  disabled task-ledger and Telegram transcript read-tool plugins;
- the capability-router MCP and its public registry;
- one bounded topic-local Telegram continuity hook and one opt-in GBrain
  capture hook;
- shared isolation, content, file-delivery, truth, and operating rules;
- reflection and papercut skills/scripts;
- runtime-owned reflection, transaction, LLM-receipt reconciliation, and
  papercut helpers.

It backs up every replaced destination, preserves unrelated local files, never
reads `.env`, and does not switch or restart a service.

## Why the rollout contract matters

An exact artifact is not enough if an update interrupts live work. Existing
agents therefore switch only after admission is closed and every runtime-owned
turn, tool call, delegated job, cron/API run, compaction lease, media task, and
delivery transaction has finished or reached a durable replayable checkpoint.

A client whose state cannot be proven safe stays on its old generation while
independent clients continue. Startup restores checkpoints and queued messages
exactly once before admission reopens.
