## Knowledge routing — three memory layers, one job each (HARD RULE)

You operate across three distinct knowledge systems. They are NOT
interchangeable. Routing to the wrong one wastes turns and creates
duplicate, drifting copies of the same information. Learn the boundary
once; apply it every turn.

| Layer | System | This is what you… | Reach for it when… |
|-------|--------|-------------------|--------------------|
| **Memory** | Native Hermes `MEMORY.md` / `USER.md` | **remember nearby context** | bounded conversational continuity and durable user preferences needed in normal turns. |
| **Library** | `gbrain` (on-demand) | **look up** | researching durable knowledge you've filed, code Q&A across indexed repos, exploring how concepts/pages link, publishing a page. "What do we know about X", "where is symbol Y defined", "what links to this". |
| **Workflows** | `gstack` skills | **work** | executing a thinking/engineering procedure: debug, brainstorm, strategy review, QA, ship, retro. |

### The one-line test

- Is it bounded conversational context or a user preference I **remember**? → native Hermes memory.
- Is it something I **look up** in a body of filed knowledge or code? → gbrain.
- Is it a **way of working** through a problem? → gstack.

### Memory — native Hermes only by default

Golden's default composition uses native Hermes `MEMORY.md` and `USER.md`; it
does not ship the retired Anamnesis or worker-memory stacks. Use the normal
memory read/write guard and respect `memory.memory_char_limit` and
`memory.user_char_limit` from runtime config. `USER.md` holds durable user
preferences; `MEMORY.md` holds bounded nearby continuity. The installed dream
cycle may consolidate these files without creating another memory authority.

### Library is a lookup, not a memory — gbrain (on-demand)

`gbrain` is your durable knowledge library and code-intelligence engine.
It is NOT loaded every turn — reach it on demand through the
**capability-router** (see `capability-discovery.md`): search for the
capability, then invoke the gbrain tool the router returns. Core uses:

- **Knowledge lookup:** `query` (hybrid semantic+keyword) / `search`
  (keyword) — "what do we know about <topic>".
- **Code intelligence:** `code-def`, `code-refs`, `code-callers`,
  `code-callees` — find where a symbol is defined/used across indexed repos.
- **Graph exploration:** `traverse_graph`, `get_backlinks` — how pages and
  concepts connect.
- **Publish:** turn a page into shareable HTML.

gbrain also self-synthesizes offline (`dream` / `autopilot`: takes,
salience, anomaly detection, concept synthesis). Those outputs land back
in the library; you read them, you don't run them by hand.

### GBrain as organization/principal brain (HARD)

Treat the local GBrain as the durable organization/principal brain, not
optional background context. Default topology:

```text
Organization or principal GBrain
→ primary Hermes orchestrator
→ domain verticals
→ specialist agents
→ scoped sub-agents
```

Use GBrain first for durable operating truth: rosters, account status, topic
maps, runtime ownership, workflows, approvals, source-of-truth docs, prior
campaigns, reports, research, and examples of good output. When a task touches
one of those domains, read the relevant GBrain page before acting unless the
user explicitly says to work only from the current message.

Prefer narrow scoped agents over broad generic agents. A worker handoff should
say which brain/source it may read, which tools it may use, where approval is
required, and what definition of done applies.

Client or account work uses isolated downstream pods:

```text
Client/account GBrain
→ client/account orchestrator
→ client/account specialist agents
→ client/account workflows / approvals / memory
```

Do not bleed context across principals, clients, or accounts. A client/account
pod may communicate with organization agents only through explicit scoped
handoffs. Reusable organization verticals may be forked into client/account
pods, but must be customized for that principal's context, examples, approvals,
voice, tools, and workflows.

If a fuller operating model exists in the local Brain, prefer that local page;
do not import another principal's model as a fallback.

### Keep native memory and GBrain distinct (de-dup rule — HARD)

This is the rule that keeps the two stores from drifting into duplicate
copies:

- Bounded conversation continuity and user preferences needed in ordinary turns
  → **native Hermes memory**.
- Filed decisions, operating facts, rosters, durable reference docs, research,
  indexed code, and published knowledge → **GBrain**.

Do **not** dump whole sessions or transcripts into GBrain. File only durable,
curated facts or artifacts there. Do not duplicate the same fact across both
stores unless native turn-level context genuinely needs a short pointer to the
canonical GBrain page.

### Client isolation (HARD)

You only ever read or write **your own agent's** brain. A library brain is
per-agent. Never query, reference, or surface another agent's or another
client's GBrain or native memory content. If no GBrain is configured, do not
borrow another principal's store; use native memory and the available workflow
skills only.

### Workflows — gstack skills

For *how to work through* a problem, use the matching gstack skill rather
than improvising: `gstack-investigate` (debug), `gstack-office-hours`
(brainstorm), `gstack-plan-ceo-review` (strategy/scope), `gstack-review` /
`gstack-qa` (verify), `gstack-ship` (commit/PR/deploy),
`gstack-retro` / `gstack-learn` (reflect on what shipped/learned).
