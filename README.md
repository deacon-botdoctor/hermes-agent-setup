# hermes-agent-setup

How I set up a fresh [Hermes](https://github.com/) agent runtime, and **why** — the config
edits, the overlay features I keep, the plugins I run, and the canonical-knowledge layer that
keeps the agent from guessing.

This is opinionated documentation of one working setup, not a fork. Every choice here started
as a default and earned its place by fixing a real problem. Where a default was fine, there's
nothing to say.

## The one rule that shapes everything: the Change Ladder

Before adding any custom code to an agent runtime, walk this ladder top-down and take the
**highest rung that fits**:

1. **DELETE** — is the thing even wanted? Has upstream absorbed it?
2. **CONFIG** — is there already a config knob for this?
3. **PLUGIN** — can a user plugin do it via a supported extension seam?
4. **UPSTREAM** — is this generally useful? Send it upstream.
5. **SIDECAR** — an external script that touches no upstream file.
6. **PATCH** — last resort. A source-level change to upstream. Requires a reason and a
   retirement condition.

Patches are the expensive rung: they're anchored to specific lines of upstream source, so every
upstream version bump can break them silently. The whole point of this setup is to keep the
patch count near its irreducible floor, so upstream bumps stay boring. Everything below is
organized by which rung it lives on.

## What's here

| Path | What |
|------|------|
| [`docs/philosophy.md`](docs/philosophy.md) | The principles: minimal overlay, own-the-logic, canonical knowledge |
| [`docs/features.md`](docs/features.md) | Every overlay feature I keep and **why** |
| [`docs/plugins.md`](docs/plugins.md) | The plugins I run and what seam each uses |
| [`docs/canonical-knowledge.md`](docs/canonical-knowledge.md) | The canonical-knowledge ("look it up, don't guess") layer |
| [`config/config.example.yaml`](config/config.example.yaml) | A commented fresh-install config with every meaningful edit |
| [`config/AGENTS.example.md`](config/AGENTS.example.md) | The agent instructions that wire in the change-ladder + canonical-lookup |
| [`install/fresh-install.md`](install/fresh-install.md) | Step-by-step: from a bare install to this setup |

## Fast start

1. Read [`docs/philosophy.md`](docs/philosophy.md) — it's short and explains the mindset.
2. Copy [`config/config.example.yaml`](config/config.example.yaml), fill in the `<PLACEHOLDER>`
   values, and diff it against your install's default config.
3. Skim [`docs/features.md`](docs/features.md) and turn on only what maps to a problem you
   actually have.

## A note on secrets

Nothing in this repo is a secret. Tokens, keys, chat IDs, and hostnames are all
`<PLACEHOLDER>` values — replace them locally, never commit the real ones. The single most
important feature in [`docs/features.md`](docs/features.md) exists precisely because secrets
leak into transcripts if you let them.
