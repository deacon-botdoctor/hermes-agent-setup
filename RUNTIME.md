# What the current runtime changes

## Exact release

[`release.json`](release.json) is the authoritative source for the current
release identity, upstream and Golden SHAs, component digests, Cua Driver
version, and assembled runtime fingerprint. The linked
[`runtime-payload-source-manifest.json`](runtime-payload-source-manifest.json)
owns the exact component file inventories, counts, and Git blob identities.
Machines do not install “latest.” They build and verify those exact identities.
Exact Cua Driver presence is required on macOS, Windows, and Linux, while GUI
readiness remains a separate native doctor/session/permission gate.
Private fleet health probes, host routes, and service adapters are deliberately
not part of this public source manifest; native service tooling owns public
installations.

The `native_agent_continuity` entry in `release.json` remains a narrow overlay
with its own source commit, file inventory, modes, and package digest. The
general runtime now advances independently to the exact Golden and upstream
pins recorded in the same release. Continuity activation remains
manifest-driven: managed provisioning opts a new tenant in, while an existing
or manual installation without that tenant manifest remains untouched.

## What changed in this release

- Fresh and existing profiles receive the same cross-platform capacity,
  disk-retention, canary-reconciliation, and tool-readiness floor. The local
  self-check emits a bounded `machine_profile` for disk, memory, process, and
  swap evidence; agents inspect pressure and explain headroom without using it
  as an automatic veto on requested large work.
- Routine cron script, configuration, runtime, timeout, and execution failures
  now receive one non-recursive repair turn in the owning runtime after the
  original receipt is saved. The result is verified and appended before human
  delivery. Successful script jobs remain model-free, and safety, auth,
  billing, permission, destructive-data, provider-limit, interruption, and
  uncertain-side-effect boundaries do not enter automatic mutation.
- The canary reconciler accepts only an exact active native-scheduler command
  or the exact canonical cron line, removes its owned duplicate cron entry
  after native-scheduler proof, and fails closed when crontab inspection fails.
- New managed clients declare native-agent continuity by default. Existing
  self-heal runs the GBrain baseline once, reconciles supported native coding
  agents, then exports, syncs, and cards new sessions on each normal heartbeat.
  Codex is enrolled only when its existing login is usable; provider install,
  login, billing, and credential changes remain out of scope. Claude and Gemini
  adapters are detection-only until an explicit adapter contract is enabled.

## Current public payload

- The immutable Hermes `v0.20.0` base remains pinned while the public
  overlays repair the fleet-discovered compatibility gaps.
- Provider routing now makes the dedicated OpenRouter auxiliary key the
  embedding path, removes retired ZeroEntropy credentials, and verifies the
  result without printing secrets.
- Legacy Anamnesis/Qdrant launch surfaces are retired only after native memory
  is ready, with reversible receipts for upgrades and new installations.
- Cross-platform host installers now carry the same watchdog, health-probe,
  runtime-binding, and rollback contracts used by the fleet release gate.
- Model-authored long-work checkpoints are assembly-checked so status updates
  remain real work summaries instead of generic timer messages.
- The complete visual fundamentals package includes Impeccable and the Human
  Taste skill family instead of leaving those capabilities to manual setup.
- Native compaction owns summary acceptance again; the retired custom
  postcondition patch no longer rejects valid Codex summaries.
- Task completion now falls back to a profile-local append-only record when
  the optional operator changelog backend is absent.
- Malformed lifecycle script references fail closed without crashing the turn.
- Native Hermes owns ordinary foreground work. An actionable acknowledgement
  without current-turn action evidence receives one bounded continuation retry;
  explicit durable work may use Task Ledger, but ordinary turns are not
  auto-captured into a second task system.
- The disconnected open-loop hook and task-follow-up sweeper are retired. They
  no longer inject recovery prompts, rewrite tool results, or compete with the
  model loop.
- Silent drain recovery remains durable without leaking internal lifecycle text
  into client chats, and Telegram group ingress stays out of the recovery
  executor that owns replayed work.
- Explicit attachment resends, voice-memo delivery, contextual reset, and
  runtime-root checks are aligned with the v0.20.0 assembled contracts.
- Platform delivery is part of the restart drain, so a normal response is not
  considered quiescent before its transport accepts the outbound message.
- Telegram checkpoints retain factual model commentary and observable
  lifecycle milestones while rejecting canned or tool-internal status text.
- Immediate Telegram typing is emitted at accepted ingress, before model-turn
  preparation, so slow setup cannot make a healthy agent appear unresponsive.
- A Codex 429 can use the configured OpenRouter emergency chain; 401/403
  authentication failures retain the existing spend circuit and safeguards.
- Composio activation binds only verified fresh connections to the canonical
  runtime route, and profile runtimes resolve GBrain through the shared
  canonical bridge while preserving an explicit profile-local wrapper.
- New profiles use native `tool_use_enforcement: auto`; stale explicit `false`
  values are treated as configuration drift instead of surviving a runtime
  update unnoticed.
- Native image generation is configured as a first-class capability, with
  visual-reference and escalation guidance that prefers the real image model
  over low-fidelity local compositing.
- Runtime health checks now share one portable floor for SQLite/WAL pressure,
  Telegram delivery, safe restart, active runtime identity, and canary state.
- Host-capacity checks distinguish allocated swap from active swap churn,
  inventory top-memory and oldest processes, and route sustained pressure for
  independent health review without killing unknown work or rebooting a host.
- Scheduled jobs self-remediate routine local failures once before escalation;
  their delivery contract rejects operator-directed repair instructions and
  keeps full paths, logs, IDs, and raw errors in the saved receipt.
- Golden no longer rewrites Hermes defaults already owned upstream; retired
  policy leaves are removed only when they still equal Golden's former value.

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
- **Lean agent loop:** current-turn action evidence prevents a bare “working on
  it” response from ending actionable work, while native Hermes retains tool
  execution, per-operation timeout recovery, foreground iteration, and final
  response ownership.
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
- **Machine-aware execution:** the local self-check publishes current capacity,
  process, disk, and swap evidence. Large jobs use that evidence for staging,
  checkpoint, and cleanup decisions rather than being categorically blocked.
- **Bounded cron recovery:** repairable local cron failures get one owning-
  runtime repair-and-verify attempt; recursive repair and unsafe authority
  expansion are rejected.
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
- **Tenant-native coding continuity:** a local, Git-backed GBrain vault exposes
  15 bounded read/write MCP tools to supported native agents. Session capture
  strips secrets, URLs, absolute paths, reasoning, and tool payloads before
  write-through. Luna generates at most eight cards per request, while the
  runner drains every current batch to zero pending with no daily ceiling. The
  baseline and agent binding both emit verification and rollback receipts and
  leave no persistent GBrain server process.

The authoritative per-patch reason, target, test, rollback, and retirement
condition are in [`patches/registry.yaml`](patches/registry.yaml). There are 33
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
- the integrity-pinned native-agent-continuity controller, session pipeline,
  GBrain MCP, cross-platform self-heal hooks, and contract. These files stay
  dormant unless a tenant provisioning manifest opts the profile in.

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
