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
  --manifest /path/to/CL-client/runtime-manifest.yaml
```

Semantics:

- Retired Golden policy leaves are removed first when their live value still
  exactly matches the former forced value.
- Every active default leaf normally replaces the same-keyed value unless the
  dotted path is protected or appears in the manifest's
  `overlay_config_exemptions`.
- If `image_gen` is absent or empty, Golden supplies both provider and model.
  Any nonempty client `image_gen` mapping is preserved as a unit so Golden
  never creates a mixed provider/model route.
- The MCP-control defaults merge is additive: it preserves client entries and
  order, adds governed native toolsets, and reconciles authorized cold
  backends.
- Client keys not named by an active or retired default remain untouched.
- The merge is idempotent.

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

`merge-shared-defaults.py` is called by `patches/apply-all-patches.py
--profile-root <path>` and by the overlay update path.
