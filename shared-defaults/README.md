# shared-defaults/

The smallest fleet-wide compatibility settings that Hermes cannot yet supply
natively.

Golden previously forced model routing, compression, approval, timeout, and
display preferences here. Those policies duplicated native Hermes behavior and
made effective client configuration depend on overlay history. They are retired
in favor of pinned-upstream defaults and client-local configuration.

## Files

- `config-mcp-on-demand-control.yaml` enables the policy-gated MCP activation
  tools while the cold-backend compatibility path remains necessary.
- `config-native-tool-search.yaml` turns on native tool-search deferral.
- `config-model-autonomy.yaml` enables Hermes' bounded intent-continuation
  seam for every provider while disabling only automatic post-turn review
  forks that can overlap the live session.
- `config-telegram-surface.yaml` sets Telegram reply-to off.
- `config-client-quiet-display.yaml` is the fleet client quiet surface:
  `busy_input_mode: steer`, all busy/steer acks off, no interim/long-running
  status nags, no timestamps/gateway status noise. Every client gets this on
  merge; kit template + provision-client match it for new installs.
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
- Every active default leaf replaces the same-keyed value unless the dotted
  path appears in the manifest's `overlay_config_exemptions`.
- MCP control is the narrow additive exception: it preserves client entries
  and order while reconciling authorized cold backends.
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
