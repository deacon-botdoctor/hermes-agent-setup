# hermes-agent-setup

A minimal, maintainable overlay for a stock agent runtime, and the machinery to apply it safely.
Clone it, point it at your runtime, and it will rehearse the overlay before it writes anything.

This is a working kit, not just docs. The apply engine, the rehearsal harness, and the reference
patch module all run. The registry lists the full shape of a mature overlay so you can see the
whole map; you fill in each module when you actually hit its problem.

Open [`index.html`](index.html) for the visual build map.

## The idea in one line

Differ from upstream by the smallest set of changes that solves your real problems, so upgrading
the runtime stays boring. Everything here serves that. See [`docs/philosophy.md`](docs/philosophy.md).

## Layout

```
overlay/
  apply.py         # the apply engine, overlays the registry onto a runtime tree (runs)
  rehearse.py      # the rehearsal harness, verify against a pristine version first (runs)
  registry.yaml    # the manifest: one entry per change, with its ladder rung + retirement
  modules/
    suppress_codex_autoraise_notice.py   # a real, working patch module (reference)
    _example_patch.py                     # copy this to write a new one
config/
  config.example.yaml   # the config keys that matter, with the why for each
  AGENTS.example.md     # instructions that wire the change ladder + look-it-up rule
plugins/
  memory/               # long-term recall provider (skeleton, real seam)
  platform_override/    # thin chat-adapter override (skeleton, real seam)
docs/                   # the reasoning: philosophy, features, plugins, canonical knowledge
install.sh              # runs the pipeline: rehearse, then apply
index.html              # the build map
```

## Quick start

You need two trees: a **pristine** checkout of your runtime version (untouched, for rehearsal)
and the **runtime** you actually deploy. Same version.

```bash
# 1. see it run against a throwaway copy of pristine, writes nothing
python overlay/rehearse.py --upstream /path/to/pristine-checkout

# 2. when rehearsal is green, apply to your runtime
python overlay/apply.py --hermes-dir /path/to/runtime

# or do both through the wrapper (rehearse-only by default; --apply to write):
./install.sh --upstream /path/to/pristine-checkout --runtime /path/to/runtime --apply
```

Then copy `config/config.example.yaml` into your runtime config, fill the `<placeholders>`,
install the `plugins/` you want where your runtime discovers user plugins, and restart.

Requires Python 3 and PyYAML (`pip install pyyaml`).

## How a patch module works

Each module in `overlay/modules/` exposes three things the engine understands:

- `MARKER`, a unique grep-able string the patch leaves behind. The engine uses it for
  idempotency, so re-applying is a no-op.
- `TARGET`, the file it edits, relative to the runtime root.
- `apply(target_path, *, dry_run=False)`, returns `"applied"` or `"anchor-miss"`.

Anchor on the smallest stable upstream substring. When an upstream bump moves that line, `apply`
returns `anchor-miss`, `rehearse` goes red, and you fix it in a sandbox instead of in production.
See [`overlay/modules/suppress_codex_autoraise_notice.py`](overlay/modules/suppress_codex_autoraise_notice.py)
for a complete one and [`overlay/modules/_example_patch.py`](overlay/modules/_example_patch.py)
to start your own.

## The rule the whole thing follows

Before adding any change, take the highest rung that fits:

**DELETE, then CONFIG, then PLUGIN, then UPSTREAM, then SIDECAR, then PATCH.**

Patches are last because they anchor to upstream source and break on version bumps. Every
registry entry records its `rung` and a `retire_when` condition so debt has an exit. Full
reasoning in [`docs/philosophy.md`](docs/philosophy.md); every overlay feature and its why in
[`docs/features.md`](docs/features.md).

## Safety

Nothing here is a secret. Tokens, keys, IDs, and hostnames are all `<placeholders>`. Never
commit the real ones. The first registry entry you should implement is redaction, precisely
because secrets in tool output otherwise persist into transcripts and get re-sent every turn.

## The maintenance loop

On every upstream bump: rehearse against the new version first; delete anything upstream now
does natively; route anything that grew a config knob to config and drop the patch; deploy one
host at a time, verify, then propagate. There is no safe push-to-everything-at-once.
