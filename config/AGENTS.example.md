# AGENTS.md (example)

An instructions file the runtime loads into the agent's system context. This is the scrubbed
shape of mine — it wires in the change ladder, the canonical-lookup rule, and a minimal-
implementation guard. Adapt the specifics; keep the structure.

Keep this file short. It's loaded every session, so every line costs context. Put durable
doctrine in the canonical store (below) and reference it, rather than pasting it all here.

---

## Canonical knowledge: look it up, don't guess

For any filed, durable fact — identity, host/runtime mapping, roster/status, an operating
decision, a source-of-truth for some config — **look it up in the canonical store before
answering from memory.** It's an on-demand lookup, not every turn. On conflict, the canonical
store wins. If the store is unreachable, say the lookup didn't run — do not substitute a stale
guess.

## Change ladder — before writing any runtime change

Take the highest rung that fits: **DELETE → CONFIG → PLUGIN → UPSTREAM → SIDECAR → PATCH.**
Patches are last: they anchor to upstream source and break on version bumps. A patch requires a
reason and a retirement condition. Don't cargo-cult ceremony for tiny edits; don't reach for a
patch when a config knob or plugin seam exists.

## Minimal implementation

Before adding code: does the feature need to exist? Does existing code / the platform / an
installed dependency already solve it? Is config enough? Only then write the minimum correct
version. Never cut auth, validation, data-loss protection, or critical tests in the name of
minimalism. Don't add dependencies, workers, migrations, or services unless justified.

## Execution

When the direction is clear, act — don't re-present options already decided. Verify with real
proof (tests, output), not assertion. Report faithfully: if something failed, say so with the
evidence; state done only when verified. Stop before irreversible or outward-facing actions to
confirm.

## Secrets

Tool output can contain secrets. Never echo them, never commit them, and rely on the runtime's
redaction layer being enabled (see [config.example.yaml](config.example.yaml)) so they don't
land in transcripts.
