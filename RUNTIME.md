# What the current runtime changes

## Exact release

[`release.json`](release.json) is the authoritative source for the release
identity, the upstream and Golden SHAs, the component digests, the
desktop-driver contract, and the assembled-runtime fingerprint. The linked
[`runtime-payload-source-manifest.json`](runtime-payload-source-manifest.json)
owns the exact component file inventories, counts, and Git blob identities.
Machines do not install "latest." They build and verify those exact identities.

| Item | Value |
|---|---|
| Release | `botdoctor-hermes-2026.11.26-golden-0e9165b3` |
| Upstream | NousResearch/hermes-agent `v0.23.0`, `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` |
| Golden | `0e9165b3fc38a7514b11a7f1d579fd01d14db54e` (fleet surgical release) |
| Assembled runtime | 142 changed files, fingerprint `ec69a5a3…0f6076` |
| Patch registry | 10 entries (the previous release carried several dozen) |

The previous public release pinned upstream `v2026.9.24` and Golden
`e34072ce`. This release moves to the runtime that the managed fleet runs now.

Private fleet health probes, host routes, rollout controllers, and service
adapters are not part of this public source manifest. Native service tooling
owns public installations.

## Fleet identity and the public sanitization delta

A clean assembly of Golden `0e9165b3` on upstream `2bd378ed` matched the
live fleet runtime byte for byte on all 142 changed runtime files.

The public payload differs from that Golden source in a small, listed set of
files. Each change removes a private identifier (a client name, a client host
name, a person's name in an attribution comment, a fleet agent row, or an
account-specific default URL). No change alters runtime logic.

- **Runtime tree:** exactly two files differ from the fleet assembly, both
  non-functional: one code comment in `tools/bot_desktop/baseline.py` named a
  client host, and `plugins/computer_use/wayvnc/plugin.yaml` now lists
  `author: Bot Doctor`. The carrier inventory hashes are updated to match.
  This is why the public fingerprint (`ec69a5a3…`) differs from the fleet
  fingerprint of the same Golden commit.
- **Profile and defaults files:** `shared-defaults/fleet-exemptions.yaml`
  ships with no agent rows (`agents: {}`); add your own rows. Attribution
  comments in `shared-defaults/*.yaml`, `shared-defaults/README.md`,
  `scripts/merge-shared-defaults.py`, and `skills/fleet/x-research/SKILL.md`
  name "the operator" instead of a person. One retired-value plugin name in
  `retired-policy-defaults-v1.yaml` is generalized. Two comments in
  `bin/hermes-local-selfcheck.py` and
  `bin/hermes-windows-capacity-containment.ps1` no longer name a client.
- **Client and operator names in tools:** Windows self-heal lanes use generic
  profile names (`primary`, `secondary`) instead of client agent names, in
  both `kit/bin/` and the native-continuity package (its contract and package
  digest are updated). Operator-specific profile names in
  `bin/telegram-send-artifact`, `kit/bin/hermes-safe-restart.sh`, and the
  Windows health probes are now `operator`. Comments, docstrings, and default
  messages name "the operator" instead of a person. One client-specific
  protected folder is removed from `bin/hermes-fileprovider-online-only.swift`.
- **Vendored Printing Press skill:** two third-party example email addresses
  in `references/secret-protection.md` are replaced with `example.invalid`
  placeholders, recorded in its `UPSTREAM.json` local patches.
- **Composio wrapper:** `bin/composio-remote-mcp-with-env.sh` now requires
  `COMPOSIO_MCP_URL`, and `bin/composio-remote-mcp-verify.py` uses
  placeholders. The earlier release carried an account-specific default URL.

## What changed in this release

- **Upstream v0.23.0.** The base moves from `v2026.9.24` to the stable
  `v0.23.0` tag. Golden retired most of the old public patches (durable drain,
  compaction lifecycle, Telegram checkpoints, cron delivery, LLM attempt
  receipts, MCP liveness, and others). In each case either upstream now owns
  the behavior or the fleet no longer uses it; the per-patch reasons are in
  the Golden registry history, not in this public bundle.
- **Ten remaining patches.** Each has a reason, a test, a rollback, and a
  retirement condition in [`patches/registry.yaml`](patches/registry.yaml):

  | Patch | Why it remains |
  |---|---|
  | `anthropic_403_pool_rotation_v1` | Rotate the Anthropic credential pool on a credential-shaped 403 before provider fallback. |
  | `broker_oauth_pool_resync_v1` | Resync broker-pushed (access-only) OAuth pool rows after a mid-turn rotation, instead of marking the row exhausted. |
  | `subscription_rollover_v1` | Roll over an exhausted subscription credential through the governed order. |
  | `broker_codex_jobs_v1` | Broker-managed Codex CLI jobs and request handling for access-only consumers. |
  | `conditional_api_admission_v1` | Linearize local permission, freshness, and control fences with native enqueue. |
  | `acp_edit_approval_outcomes_v1` | Preserve timeout and transport-failure outcomes in ACP edit approval. |
  | `session_url_storage_redaction_v1` | Keep temporary URL grants out of saved transcripts. |
  | `gateway_progress_renderer_v1` | Presentation hook for heartbeat progress. |
  | `native_external_screen_v1` | Cross-process admission and privacy ordering for the external screen. |
  | `native_cua_lock_v1` | Pin the approved desktop-driver release in the native package lock. |

- **Shared defaults.** New files set one model per agent
  (`config-single-model-routing.yaml`), a fleet consistency baseline
  (`config-fleet-consistency-baseline.yaml`), native skill-write approval with
  the curator off (`config-native-skill-approval.yaml`), plain Telegram
  updates, and the governed subscription backup order
  (`config-subscription-failover.yaml`). Retired keys and old model-routing
  instructions are removed only when they still equal the former value.
- **Shared rules.** `truth-over-comfort.md` carries the current grounding and
  delivery floors. `printing-press.md` is new.
- **Skills.** Adds `fleet/x-research`, `curated/news-watch`, and the vendored
  Printing Press skill. Retires plugins and hooks that upstream now covers
  (task ledger, Telegram transcript tools and hook, composio onboarding,
  topic post, email-card QA plugin, skill-category recovery).

## Not in this release

- Later Golden main commits that are not yet fleet-released, for example the
  xAI broker pool-row preservation fix and the scheduled self-check runtime
  binding. A later public release picks them up after fleet rollout.

## Known issues carried from the Golden source

These three files ship unchanged from Golden `0e9165b3`. They still refer to
components that this release retired. Fix them in Golden, not as a public-only
divergence.

- `bin/llm-attempt-reconcile.py` exits 1 on a fresh profile because its
  fallback module (the retired LLM-attempt-receipts payload) is absent.
- `kit/bin/telegram_delivery_history.py` requires the retired Telegram
  transcript hook and leaves its receipt pending on a fresh profile.
- `bin/hermes-local-selfcheck.py` warns that the retired Telegram transcript
  hook is missing.

## Native first

Hermes owns the model loop, compaction, sessions, `state.db`, memory and
search, tasks, goals, cron, the gateway, platforms, service installation, and
most tool behavior.

The public layer does not add LCM, Anamnesis, AutoDream, a general-purpose
conversation recall database, automatic broad replay, or a second scheduler.
It keeps optional MCP servers and browser automation cold until a real request
needs them.

## Configuration choices

- Native Tool Search is on, so large skill and MCP catalogs are discovered
  lazily instead of injected into every request.
- The MCP control and capability-router tools stay visible while optional
  backends stay cold.
- Every auxiliary task and subagent follows the agent's main model, unless a
  row in `fleet-exemptions.yaml` records a reason.
- Skill writes from the agent go through native approval.
- **Desktop default:** every computer should install **Agent Desktop**
  (`vercel-labs/agent-desktop`) and default all agents to Agent Desktop over
  other desktop or browser UI tools. The Hermes-native `cua-driver` fallback is
  aligned at `0.34.1` across `contracts/cua-driver-release-v1.json`,
  `release.json`, and `pm/lock.json` (`native_cua_lock_v1`).

The setup preserves local model and provider choices. It removes retired
Golden values only when they still exactly match the old defaults.

## Profile additions

The bounded profile installer places only manifest-owned files: `bin/` and
`kit/bin/` helpers into `$HERMES_HOME/bin`, and `hooks/`, `plugins/`,
`shared-rules/`, `skills/`, and `mcp-servers/` into the same relative paths.
It also places the integrity-pinned native-agent-continuity files, which stay
dormant unless a tenant provisioning manifest opts the profile in.

It backs up every replaced destination, preserves unrelated local files, never
reads `.env`, and does not switch or restart a service.

## Why the rollout contract matters

An exact artifact is not enough if an update interrupts live work. Existing
agents switch only after admission is closed and every runtime-owned turn,
tool call, delegated job, cron or API run, compaction lease, media task, and
delivery transaction has finished or reached a durable, replayable checkpoint.

The live gateway must report the same positive PID and zero active operations
twice across a stable interval. A stale counter is reconciled, never ignored.
A client whose state cannot be proven safe stays on its old generation while
independent clients continue. Startup restores checkpoints and queued messages
exactly once before admission reopens.

## The drop-in guide

[`docs/drop-in-guide.md`](docs/drop-in-guide.md) (and the rendered
[`docs/drop-in-guide.pdf`](docs/drop-in-guide.pdf)) explains the doctrine and
the architecture around this runtime: the covenant and operating floors, the
writing style, memory and knowledge, the auth broker pattern, model routing,
the multi-machine setup, shipping, scheduled jobs, isolation, and self-repair.
