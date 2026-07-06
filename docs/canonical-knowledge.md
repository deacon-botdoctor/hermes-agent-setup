# The canonical-knowledge layer ("look it up, don't guess")

An always-on agent needs a place to keep **authoritative, durable facts** that is separate from
its conversational memory. This documents the *pattern* — not any particular knowledge base's
contents — for wiring one in.

## Why a separate layer at all

Three memory tiers, three jobs:

| Tier | Holds | Trust for durable facts |
|------|-------|-------------------------|
| Model weights | General knowledge, frozen at training time | No — never knew your specifics |
| Session / working memory | This conversation, scratch notes | No — goes stale, is local |
| **Canonical store** | Filed facts that must be correct | **Yes — this is the source of truth** |

The failure mode is answering a canonical question ("which host runs X", "what did we decide
about Y", "what's the config source-of-truth for Z") from the wrong tier — the model guesses,
or session memory is stale. The canonical store exists so the agent has somewhere authoritative
to *look it up*.

## The rule

> For any filed, durable fact, **look it up in the canonical store before answering.** It's an
> on-demand lookup, not something you consult every turn. On conflict between the canonical
> store and anything else — code comments, your own memory, the model's prior — **the canonical
> store wins.** If it's unreachable, say the lookup didn't run; do **not** substitute a stale
> guess.

That last sentence is the important one. A confident stale answer is worse than "I couldn't
look it up" — it's wrong *and* it looks right.

## How to wire it

You need three pieces:

1. **A store** with a query interface the agent can call — a CLI, an MCP server, a tool.
   Anything the agent can invoke to ask "what's the canonical answer to X." Keep it fast enough
   that looking up is cheaper than guessing wrong.
2. **An instruction** in the agent's system prompt / instructions file that names *when* to look
   up (durable/filed facts) versus when session memory is fine (conversational context). See
   [`../config/AGENTS.example.md`](../config/AGENTS.example.md).
3. **A discipline** for keeping it canonical: edit facts in the store, not in the agent's local
   copies. Mirrors go stale; the store is the one you trust.

## The change-ladder lives here too

The store is also where cross-cutting *doctrine* lives — the change ladder, deploy rules,
operating decisions. When the agent (or you) is about to make a structural change, the rule to
"take the highest ladder rung that fits" is a canonical doctrine it can look up, not a thing it
has to re-derive each time. Filing doctrine centrally is how a fleet of agents stays consistent
without copying the same rules into every runtime.

## A lightweight file-based memory (complements the store)

Separate from the canonical store, each agent benefits from a small **local** memory for the
things *it* learned — one fact per file, plus a single index file loaded at session start:

```
memory/
  INDEX.md          # one line per fact: "- [Title](slug.md) — one-line hook"
  <slug>.md         # one durable fact, with light frontmatter (type: user|project|feedback|reference)
```

Keep this for what the agent learned that the code/history doesn't already record. Don't
duplicate into it what's already in the repo or the canonical store. This is *working* memory
that persists across sessions — distinct from the *authoritative* canonical store. When the two
disagree, canonical wins.
