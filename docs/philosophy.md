# Philosophy

Four principles. Everything else is application.

## 1. Minimal overlay

The runtime you deploy should differ from upstream by the smallest set of changes that solves
your real problems — and no more. Every custom change is a liability that has to be re-verified
on each upstream bump. A feature that doesn't map to a problem you actually have is negative
value: it's pure maintenance cost.

Concretely: the default count of custom patches trends *down* over time, not up. When upstream
absorbs something, delete your version. When a config knob appears, route to it and delete the
patch. The health metric isn't "how much have I customized" — it's "how boring is the next
upstream bump."

## 2. The change ladder

(Repeated from the README because it's the load-bearing idea.) Before writing any custom code,
take the highest rung that fits: **DELETE > CONFIG > PLUGIN > UPSTREAM > SIDECAR > PATCH.**

Patches are last because they're anchored to specific upstream source lines. When upstream
moves those lines, the patch either fails loudly (good) or applies to the wrong place silently
(catastrophic). A patch that silently mis-applies looks like it worked until the behavior is
subtly wrong in production. So: earn your way down the ladder, and treat every patch as debt
with a retirement condition.

## 3. Own the logic, not the injection

<a name="own-the-logic"></a>
When you *must* change runtime behavior and there's no clean seam, separate the **logic** you're
adding from the **wiring** that connects it.

- Put your logic in a **file you own** — a module you ship into the tree. A new file has no
  anchor into upstream source, so an upstream bump can't break it.
- Keep the **wiring** — the call site that invokes your logic — as thin as possible: ideally a
  one-line call at a single chokepoint.

The contrast: the naive approach injects a hundred lines of your logic directly into an upstream
file. Now that hundred-line block is anchored to upstream source and has to be re-verified and
re-anchored on every bump. The owned-logic approach injects one line and keeps the hundred lines
in your own file. Same behavior, a fraction of the fragility.

Corollary: when one *feature* is spread across many patches, consolidate it into one owned
module. That's simultaneously a reduction in patch count and an improvement in quality, because
the feature's policy now lives in one place you can test.

## 4. Canonical knowledge — look it up, don't guess

An agent (and the human running it) accumulates two very different kinds of memory:

- **Session/working memory** — what happened in this conversation, notes to self. Local, fast,
  yours.
- **Canonical knowledge** — durable facts that must be *correct*: who a client is, which host
  runs what, an operating decision, the source-of-truth for some config. Shared, authoritative.

The mistake is answering canonical questions from session memory (which goes stale) or from the
model's training (which never knew). The rule: **for any durable, filed fact, look it up in the
canonical store before answering.** It's an on-demand lookup, not something you consult every
turn — but when the question is "what's the source of truth for X," you query, you don't guess.

On conflict between the canonical store and anything else (including code comments or your own
memory), the canonical store wins. If the canonical store is unreachable, you say the lookup
didn't run — you do **not** substitute a stale guess.

See [canonical-knowledge.md](canonical-knowledge.md) for how this is wired.
