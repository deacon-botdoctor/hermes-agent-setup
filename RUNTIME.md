# What the current runtime changes

## Exact release

The current public release is `botdoctor-hermes-2026.07.31-semantic-control-v2-cua-driver-v1`:

- upstream Hermes `0.19.1`: `cc4cab2f592e60a197e796506de9168f74baf3ea`;
- public Golden source: `7354b56138ff377bba0c188ea0732e70be1e2440`;
- runtime payload: 86 files;
- assembled runtime fingerprint:
  `4eabe33da88bce2b0b415bcee8e0b5e814787ea4c70cca78336c75176a4e8ebd`.

Machines do not install “latest.” They build and verify these exact identities.
The profile installer also pins Cua Driver `0.14.2`; exact-version presence is
required on macOS, Windows, and Linux, while GUI readiness remains a separate
native doctor/session/permission gate.
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
- **Quiet native compaction:** native Codex compaction stays authoritative while
  its internal completion signal is kept out of the user transcript.
- **Contextual interruption recovery:** after an interrupted restart, ask the
  originating user what to do next instead of silently repeating an uncertain
  action.
- **Lazy MCP activation:** Tool Search can discover a cold approved backend,
  activate it, refresh schemas, and continue in the same turn.
- **Runtime-root coherence:** lazy imports cannot combine the active gateway
  with an older mutable checkout.
- **Telegram transaction proof:** a local ledger can join exact ingress to final
  delivery for canaries and incident diagnosis.
- **Version-aware media delivery:** replacing an attachment at the same path
  sends the new bytes while an unchanged version remains deduplicated.
- **Windows task identity:** named profiles stay bound to their explicitly owned
  Scheduled Task and state namespace.
- **Codex 401 spend guard:** one bounded client-turn fallback may run while
  recursive/internal paid fallbacks remain blocked.
- **LLM attempt receipts:** each observed main or auxiliary provider attempt
  writes a content-free local receipt with route, outcome, and usage evidence.
  A missing start receipt fails closed before provider spend; reconciliation
  treats missing, empty, or zero usage as unavailable while retaining a
  provider-reported cost.
- **Deferred-tool safety:** a model may defer an approved capability, but a
  bridge guard prevents an unresolved deferred tool from being misreported as
  completed work.
- **Capability-aware semantic computer control:** the lazy skill and native
  tool are discoverable on CLI/Telegram by default. The public installer uses
  native Hermes to install the exact Golden-pinned Cua Driver and records its
  native doctor result. Headless/session-limited hosts keep native
  API/CLI/browser fallback; the stricter guard is still a doctor-green host
  opt-in and keeps UI control background-first and semantic-only.
- **Abandoned-stream cleanup:** LLM attempt receipts now close partially
  consumed provider streams across sync finalization, async loop shutdown, and
  context-manager failures without blocking receipt enrichment.

The authoritative per-patch reason, target, test, rollback, and retirement
condition are in [`patches/registry.yaml`](patches/registry.yaml). There are 18
cohesive registry entries in this release.

## Profile additions

The bounded profile installer places only manifest-owned files:

- two enabled plugins for output hygiene and on-demand MCP control, plus
  disabled task-ledger, Telegram transcript read-tool, and semantic-control
  plugins;
- the capability-router MCP and its public registry;
- one bounded topic-local Telegram continuity hook and one opt-in GBrain
  capture hook;
- shared isolation, content, file-delivery, truth, and operating rules;
- reflection and papercut skills/scripts, plus the lazy semantic-control skill;
- runtime-owned reflection, transaction, LLM-receipt reconciliation, and
  papercut helpers.

It backs up every replaced destination, preserves unrelated local files, never
reads `.env`, and does not switch or restart a service. Before profile mutation,
it requires exact pinned-driver presence; GUI installs can additionally require
doctor-green readiness with `--require-computer-use-ready`.

## Why the rollout contract matters

An exact artifact is not enough if an update interrupts live work. Existing
agents therefore switch only after admission is closed and every runtime-owned
turn, tool call, delegated job, cron/API run, compaction lease, media task, and
delivery transaction has finished or reached a durable replayable checkpoint.

The live gateway must report the same positive PID and zero active operations
twice across a stable interval. A stale counter is reconciled, never ignored.
A client whose state cannot be proven safe stays on its old generation while
independent clients continue. Startup restores checkpoints and queued messages
exactly once before admission reopens. Windows acceptance additionally proves
that the named Scheduled Task, readiness task, launchers, profile, and live
process all resolve the same immutable runtime.
