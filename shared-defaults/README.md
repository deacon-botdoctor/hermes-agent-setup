# shared-defaults/

The smallest fleet-wide settings that Golden must still reconcile beyond
Hermes' pinned native defaults.

Golden previously forced model routing, compression, approval, timeout, and
display preferences here. Those policies duplicated native Hermes behavior and
made effective client configuration depend on overlay history. They are retired
in favor of pinned-upstream defaults and client-local configuration.

## Files

- `config-mcp-on-demand-control.yaml` enables the policy-gated MCP activation
  tools while the cold-backend compatibility path remains necessary, and
  additively exposes `image_gen` on CLI, Telegram, and cron surfaces.
- `config-native-tool-search.yaml` turns on native tool-search deferral.
- `config-native-image-generation.yaml` configures Hermes' native
  `image_generate` provider and model at the top-level schema Hermes reads.
  A nonempty client-owned top-level `image_gen` mapping is preserved as a
  unit.
- `config-model-autonomy.yaml` enables Hermes' bounded intent-continuation
  seam for every provider while disabling only automatic post-turn review
  forks that can overlap the live session.
- Tool-use enforcement is not a shared default. The
  [lean client seed](../README.md#lean-client-seed) owns its new-client default,
  while shared-default reconciliation leaves existing explicit modes unchanged.
- `config-telegram-surface.yaml` sets Telegram reply-to off.
- `config-telegram-organic-checkpoints.yaml` enables Telegram-only factual
  long-turn checkpoints at a ten-minute cadence. Short turns stay silent;
  later checkpoints edit the same message and fail closed when no safe,
  model-authored narration exists. The card is deleted only after the primary
  final response is accepted and the turn completes successfully; missing
  acceptance, cancellation, delivery failure, and later processing failure
  retain it. A failed voice attempt followed by successful primary text uses
  the native fallback and permits cleanup; supplemental voice success does not
  hide a primary delivery failure. Tool telemetry never writes client copy.
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
