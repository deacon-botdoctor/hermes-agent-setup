# What the current runtime changes

## Exact release

The current public release is `botdoctor-hermes-2026.08.04-golden-b8f90b14`:

- upstream Hermes `0.20.0`: `3c27eb6234bf91b8ceee9e9071591b31e9b148cb`;
- public Golden source: `b8f90b14f9267af63ea067c1fde07786af9b294a`;
- runtime payload: 103 files;
- assembled runtime fingerprint:
  `1c168feb84dbc1111e093bb52e75072aa7b1d79ff00028654bfbab6a87c0de66`.

Machines do not install “latest.” They build and verify these exact identities.
The profile installer also pins Cua Driver `0.14.2`; exact-version presence is
required on macOS, Windows, and Linux, while GUI readiness remains a separate
native doctor/session/permission gate.
Private fleet health probes, host routes, and service adapters are deliberately
not part of this public source manifest; native service tooling owns public
installations.

## What changed in this release

- The immutable Hermes `v0.20.0` base remains pinned while the public
  overlays repair the fleet-discovered compatibility gaps.
- Native compaction owns summary acceptance again; the retired custom
  postcondition patch no longer rejects valid Codex summaries.
- Task completion now falls back to a profile-local append-only record when
  the optional operator changelog backend is absent.
- Malformed lifecycle script references fail closed without crashing the turn.
- Durable task-ledger transitions and the open-loop detector now ship together,
  so accepted work cannot silently disappear between model turns or restarts.
- Silent drain recovery remains durable without leaking internal lifecycle text
  into client chats, and Telegram group ingress stays out of the recovery
  executor that owns replayed work.
- Explicit attachment resends, voice-memo delivery, contextual reset, and
  runtime-root checks are aligned with the v0.20.0 assembled contracts.

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
- **Quiet native context lifecycle:** native Codex compaction stays
  authoritative. Automatic compaction uses an 85% threshold with a 240K-token
  cap, retains the most recent 20 messages, and keeps automatic compression,
  reset, and blocked-context notices out of human chats. Manual operator
  diagnostics remain visible.
- **Contextual interruption recovery:** after an interrupted restart, ask the
  originating user what to do next instead of silently repeating an uncertain
  action.
- **Lazy MCP activation:** Tool Search can discover a cold approved backend,
  activate it, refresh schemas, and continue in the same turn.
- **Runtime-root coherence:** lazy imports cannot combine the active gateway
  with an older mutable checkout.
- **Recurring coherence receipt:** a release-pinned host scheduler rechecks the
  exact runtime root, Python, and assembled initializer contract without
  restarting Hermes or reading conversations.
- **Telegram transaction proof:** a local ledger can join exact ingress to final
  delivery for canaries and incident diagnosis.
- **Version-aware media delivery:** replacing an attachment at the same path
  sends the new bytes while an unchanged version remains deduplicated.
- **Telegram media retry:** transient attachment-download failures retry within
  a bounded window instead of silently losing the accepted message.
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
- **Client friction review:** the daily review emits a content-safe scored
  papercut summary so repeated routing, update, tool, auth, and dependency
  friction can be fixed before the user has to report it manually.

The authoritative per-patch reason, target, test, rollback, and retirement
condition are in [`patches/registry.yaml`](patches/registry.yaml). There are 19
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
