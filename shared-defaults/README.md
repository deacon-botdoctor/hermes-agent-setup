# shared-defaults/

The smallest fleet-wide settings that Golden must still reconcile beyond
Hermes' pinned native defaults.

Golden previously forced model routing, compression, approval, timeout, and
display preferences here. Those policies duplicated native Hermes behavior and
made effective client configuration depend on overlay history. They are retired
in favor of pinned-upstream defaults and client-local configuration.

## Files

- `retired-legacy-overrides-v1.yaml` deletes old per-agent tuning overrides so each
  agent uses the pinned build default (the operator, 2026-11-16: "simple is better").
  Recorded exemptions win; the file header lists what was deliberately kept.
- `config-fleet-consistency-baseline.yaml` holds the fleet-wide policy values
  that the operator approved on 2026-11-15 (GBrain
  `decisions/fleet-consistency-baseline-20261115`): approvals, checkpoints,
  session retention, turn and delegation limits, speech-to-text, memory sizes,
  reasoning effort, and terminal timeout. Several values equal the pinned v0.23
  defaults. They are explicit so that an old local override cannot keep one
  agent different. `templates/config.yaml.tmpl` carries the values that differ
  from native defaults, so new clients start on the baseline.
- `config-single-model-routing.yaml` turns model routing off (the operator,
  2026-11-16). Every native auxiliary task uses provider `auto` with an empty
  model, and delegation uses an empty provider and model. In pinned v0.23 both
  mean "the agent's own main model". The multi-model `/moa` preset is disabled.
  Changing an agent's model therefore changes everything it runs.
- `retired-model-routing-v1.yaml` deletes the old routing instructions:
  `smart_model_routing`, `codex_oauth_stack_routing`, `fallback_policy`,
  `model.fallback`, `agent.reasoning_overrides`, `compression.summary_*`, and
  model pins on auxiliary tasks outside the native table. The full-scope merge
  applies it unless a path is exempt. It may name protected routing paths
  because it only deletes. It can never name the primary model or
  `fallback_providers`.
- `fleet-exemptions.yaml` records each allowed per-agent difference from shared
  defaults, as a dotted path with a required reason. A path exempts that
  setting and everything under it. Run the merge with
  `--agent-id <fleet id>`. Without that flag, it uses the config's
  `client_identity`. A difference without a row is drift.
- `retired-inert-keys-v1.yaml` lists config paths that nothing in the pinned
  runtime or the fleet's plugins, scripts, cron jobs, hooks, or skills reads.
  The full-scope merge deletes them unless they are exempt. Protected
  client-owned routing paths cannot appear in it.
- `config-native-skill-approval.yaml` requires native review for skill-tool
  edits and disables automatic curator runs. Review with `/skills pending`,
  `/skills diff <id>`, `/skills approve <id>`, or `/skills reject <id>`.
  This gate does not restrict manual CLI or terminal writes. Native approval
  replays the stored skill name; it does not pin a package identity. Explicit
  curator commands remain available. Tenant exemptions use the normal merge rules.

- `config-x-search.yaml` adds `x_search` to `agent.disabled_toolsets` (the merge
  adds to the agent's own list). The native tool needs xAI Grok, which is
  reserved for Grok Bot. X research uses TReg through `skills/fleet/x-research`.

- `config-mcp-on-demand-control.yaml` retains its compatibility filename. Its
  active content adds `botdoctor-telegram` and `botdoctor-immersion`, exposes
  `image_gen` and `mcp-capability-router` on CLI, Telegram, and cron, and adds
  `skills` and `vision` on Telegram. It does not itself install an activation
  plugin or configure a backend. The merge preserves tenant entries and derives
  additional MCP toolsets from the tenant's existing policy.
- Native tool discovery and deferral use pinned Hermes defaults unless the
  tenant config overrides `tools.tool_search`. There is no separate Golden
  `config-native-tool-search.yaml` file. An explicit `tools.tool_search.defer`
  list replaces, rather than extends, Hermes' curated list. Discovery changes
  initial schema exposure, not tool authorization or backend readiness.
- `config-native-image-generation.yaml` configures Hermes' native
  `image_generate` provider and model at the top-level schema Hermes reads.
  A nonempty client-owned top-level `image_gen` mapping is preserved as a
  unit.
- There is no active `config-model-autonomy.yaml` file. Continuation, reflection,
  memory nudges, and title generation follow the pinned runtime and explicit
  tenant configuration; this directory does not enable those features.
- `config-subscription-failover.yaml` is input only to the dedicated subscription
  reconciliation path, not an ordinary default merge. Its route definitions are
  not the current primary-model policy. Preserve tenant routing and use the
  current principal-approved policy before any separately authorized activation.
- Tool-use enforcement is not a shared default. The
  [lean client seed](../README.md#lean-client-seed) owns its new-client default,
  while shared-default reconciliation leaves existing explicit modes unchanged.
- `config-telegram-surface.yaml` sets Telegram reply-to off.
- config-telegram-plain-updates.yaml keeps native Telegram interim messages and
  the native long-running heartbeat off. The `botdoctor-telegram` plugin, which
  `config-mcp-on-demand-control.yaml` enables, posts one task-specific progress
  message and edits it. With both on, users saw the same progress text twice
  (an internal agent, 2026-11-16). It adds no direction-card formatter
  or hidden commentary collector. Native progress cleanup still waits for an
  accepted primary final response and successful turn completion.
  The retired display.platforms.telegram.progress_on_typing override was consumed
  by the deleted card patches. The pinned native gateway does not read this key.
  Existing explicit values can remain and have no runtime effect. Reconciliation
  stops setting it; no live configuration is rewritten by this source change.
  Fresh shared-default reconciliation leaves it unset. The kit template retains
  its historical true value, which this native pin also ignores.
- `config-cron-wrap-response.yaml` delivers cron output without the
  `Cronjob Response` header, job id, or stop-job footer. Kit template and
  the lean seed match it for new installs; merge overwrites an inherited
  `true` on existing clients.
- `config-client-quiet-display.yaml` is the fleet client quiet surface:
  `busy_input_mode: steer`, supported busy/steer acks off, no interim or
  long-running status nags, and no timestamps. It intentionally omits retired
  notification keys that current Hermes does not consume. Every client gets
  this on merge; kit template + provision-client match it for new installs.
- `retired-policy-defaults-v1.yaml` is a one-release unapply manifest. It
  removes only leaves that still exactly match Golden's former values and
  preserves client-modified or exempted values. Remove it after fleet receipts
  prove the retired values are absent. Active quiet-display defaults re-apply
  after retirement so clients keep the silence suite.

## How merges work

Run:

```sh
scripts/merge-shared-defaults.py \
  --profile-root ~/.hermes \
  --manifest ~/.hermes/runtime-manifest.json
```

Semantics:

- In the default full-scope merge, retired Golden policy leaves are removed
  first when their live value still exactly matches the former forced value.
- Every active default leaf normally replaces the same-keyed value unless the
  dotted path is protected or appears in the manifest's
  `overlay_config_exemptions`.
- If `image_gen` is absent or empty, Golden supplies both provider and model.
  Any nonempty client `image_gen` mapping is preserved as a unit so Golden
  never creates a mixed provider/model route.
- In the full-scope merge, the MCP-control defaults merge is additive: it
  preserves client entries and order, adds governed native toolsets, and
  reconciles authorized cold backends.
- Client keys not named by an active or retired default remain untouched.
- Without `--manifest`, the merge reads `runtime-manifest.json` beside the
  target config when present. An explicit manifest takes precedence.
- The merge is idempotent.

For an atomic rollout that activates only the executable native-image route,
run:

```sh
scripts/merge-shared-defaults.py \
  --profile-root ~/.hermes \
  --manifest ~/.hermes/runtime-manifest.json \
  --scope native-image \
  --receipt-json \
  --quiet
```

This scope reads only the native-image provider/model and MCP tool-exposure
defaults. It skips retired policy and all unrelated shared defaults, preserves
a complete client-owned provider/model route, and only appends `image_gen` to
the CLI, Telegram, and cron toolsets. It does not reconcile plugins, MCP policy,
cold backends, or other platform toolsets. The command fails without writing if
the resulting provider/model route or any of those three exposures is
incomplete, including when an exemption protects a missing value.

With `--receipt-json`, a successful scoped write or no-op emits the before/after
config hashes, changed, skipped, and exempt managed paths, and the effective
route and platform exposure. A repeated merge is a no-op with identical hashes
and an empty `changed_paths` list.

For the Refero-only cold declaration, Operator Control invokes the bounded
scope with the exact candidate interpreter and an existing rollback directory:

```sh
scripts/merge-shared-defaults.py \
  --scope refero-styles \
  --profile-root /absolute/hermes-home \
  --hermes-python /absolute/candidate/venv/bin/python \
  --rollback-dir /existing/rollback-directory
```

This scope reads and may update only `config.yaml` and the capability router's
consumed `registry.json`. It adds an `enabled: false` Refero server declaration
when absent, allows only `refero-styles` on demand, exposes its backend only on
platforms that already expose both the router and on-demand control, and adds
the single exact Refero row from Golden's public floor registry. Explicit
disables and matching exemptions preserve both files unchanged. Custom server
declarations, alternate consumed-registry routes, or missing, malformed, and
duplicate registry/config structures fail closed without emitting their
contents.

Before either target is written, the command snapshots both files under
`refero-styles-registration/` in the rollback directory. Restore the byte-exact
preimage, including mode and ownership, with the same profile and rollback
directory:

```sh
scripts/merge-shared-defaults.py \
  --scope refero-styles \
  --profile-root /absolute/hermes-home \
  --rollback-dir /existing/rollback-directory \
  --restore
```

Restore refuses snapshot drift or unrelated intervening target edits. The
Refero scope emits only sanitized JSON receipts; it does not deliver source,
start the server, call tools or models, or perform fleet dispatch. Source
delivery is owned by `install_default_mcp_floor` as described in the
[runtime composition contract](../docs/runtime-composition-contract.md).

## Client opt-out

An exact dotted-path exemption protects both active defaults and matching
retired values:

```yaml
overlay_config_exemptions:
  - plugins.enabled
  - platform_toolsets.telegram
  - display.busy_ack
```

## Rollout

Golden owns the manifest-aware merge interface, not fleet dispatch. The
canonical operator-control rollout controller must consume each selected
profile's runtime manifest at its typed config-reconciliation boundary before
restart and preserve the same dotted-path exemption semantics. It may invoke
this CLI or execute a separately reviewed, receipt-bound equivalent for a
specific deployable default; Telegram checkpoint activation uses the latter in
operator-control PR #488. Golden's repository-local overlay orchestrator is not
the canonical fleet rollout path. Direct patch application alone does not merge
defaults.
