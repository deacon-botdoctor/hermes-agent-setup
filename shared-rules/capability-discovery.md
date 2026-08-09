## Capability discovery — call the router FIRST (HARD RULE)

You have a small "hot" toolset of MCPs that load by default each turn. For any request involving capabilities **not** already in your hot toolset, your FIRST stop is the **capability-router** MCP. Do not guess at tool names from memory and do not tell the user "I don't have that tool" before checking the router.

### HARD VIOLATION — deny reflexes to never invoke

The following denial patterns are hard violations of this rule. They are training-base reflexes; they are wrong every time on this fleet, because cap-router exists:

- "I am a text-only agent."
- "I do not have screen / vision / capture access."
- "I cannot see images / screenshots / your screen."
- "I am an AI; I do not have direct access to [your machine / files / browser / email]."
- "That is not something I can do."
- "I would need [credentials / access / a tool]." (without naming a router search you actually ran)

If you find yourself reaching for any of these, STOP. Call `mcp__capability_router__search_capabilities` with a keyword from the ask FIRST. Only after exhausting viable router hits is "I cannot" allowed, and the response must list what was tried.

The operator has paid for every capability the router exposes. Denying without checking is a system failure equivalent to going silent on a deliverable.

### Hot defaults (always available)

These are loaded every turn for fast, low-latency core work:

- native Hermes memory tools — bounded `MEMORY.md` / `USER.md` recall and writes
- `telegram-directory` — Telegram chat/topic routing
- Any `local-*` MCPs configured for this client (offline-only ops)
- Profile-specific always-on MCPs declared in your SOUL/AGENTS.md (e.g. `maton-gmail`, `maton-calendar` for clients who explicitly need them hot)

Everything else — image gen, web search, mail, calendar, sheets, drive, docs, browser, calendly, deep memory, vision analysis, document generation, etc. — is **on-demand via capability-router**.

### How to use the router

Four tools available on every Hermes agent in this fleet:

- `mcp__capability_router__search_capabilities(query="<what you want>")` — keyword search the 175+ catalogued tools. Returns hits with `mcp_server`, `tool_name`, `availability`, `can_invoke_now`, `activation_required`, `requires_creds`, plus `score` + `score_breakdown` showing why each ranked where it did. A cold server may appear as one activation marker with `tool_name: null`; activate it, then search the refreshed toolset for the concrete tool.
- `mcp__capability_router__describe_capability(capability_id="catalog.<id>")` — full schema (inputs, outputs, cost, creds) for a concrete capability after search narrows it down. For a cold concrete hit, activate it first or pass `include_uninstalled=true` to inspect its catalog schema before activation. An activation marker describes only the server, not its cached tool schemas.
- `mcp__capability_router__list_categories()` — browse the catalog by category when you don't know what you're looking for.
- `mcp__capability_router__record_capability_outcome(capability_id, outcome, query="", error_class="")` — call this AFTER you invoke a capability so the router learns. `outcome` is `"success"` or `"failure"`. `error_class` is a short tag if it failed (`"auth"`, `"network"`, `"ratelimit"`, `"schema"`, `"timeout"`, `"other"`). The router uses this history to boost capabilities that have worked for you and demote ones that haven't.

Search results carry `availability`, `can_invoke_now`, `activation_required`,
`activation_tool`, `status_tool`, `preferred_for`, `deprioritize_for`, and
`score_breakdown`. The legacy `installed` field mirrors `can_invoke_now`; it no
longer means merely present on disk. Invoke directly only when
`can_invoke_now: true`. An
allowlisted configured backend may instead be `availability: cold`; inspect it
with the returned status tool when needed, activate it with the returned
activation tool, then search again so its newly registered tools become
visible. Trust the score order: it already factors in keyword match, fleet
preferences, and your own usage history.

### After routing — invoke the underlying tool

Once the router returns the right capability, inspect its invocation state:

- If `can_invoke_now: true`, call its underlying tool directly by its bound name (`mcp__<server>__<tool>` in Hermes' collision-safe tool naming convention).
- If `activation_required: true`, call `restart_mcp_server(server_name="<mcp_server>")`, confirm it connected, then run capability/tool search again and invoke the newly registered tool in the same turn. The runtime refreshes the current agent's tool snapshot after the activation call; this does not bypass policy denies or platform toolset exposure.
- Otherwise skip it unless its install or credential prerequisite can be repaired within the authorized task.

Persisted sessions may replay a retired flattened `mcp_server_tool` action. The
runtime may activate and dispatch that call in one request only when its backend
is uniquely identified, policy-allowed, in scope, and the MCP control tools are
available; every ambiguous, disabled, out-of-scope, or failed case remains
blocked. This is a compatibility path, not an invocation convention: always use
the canonical `mcp__server__tool` name returned by current discovery for new
calls.

The router does not proxy the capability call; it reports whether the backing
server is ready now and, for policy-allowed cold servers, how to activate it.

Some router hits are **operating patterns** rather than callable MCP tools. These have `kind: operating_pattern` and no `mcp_server`. Treat them as routing doctrine: they tell you which lane to use, which safety rule applies, what closeout evidence is required, and when to queue durable work instead of acting in the live chat loop. Do not try to invoke an operating pattern as a tool. Use `describe_capability()` to read its `routing_policy`, then choose the underlying lane/tool/script/skill it names.

Important operating-pattern examples:

- `ops-pattern.durable-work` — use the durable workload lane only for an explicit background/overnight request, long-lived monitoring or waiting, or a concrete restart-survival requirement; duration, complexity, and auditability alone do not move ownership out of the live conversation.
- `ops-pattern.gbrain-local-admin` — local-only GBrain admin operations go through Minions shell jobs with `inherit`, never raw secret `env`.
- `ops-pattern.coding-worktree` — repo-writing coding work requires an isolated worktree/branch by default.
- `ops-pattern.gstack-claude` — planning/review/QA/security/build work should launch Claude Code with explicit gstack skill prompts.
- `ops-pattern.local-inference-gate` — Spark/local inference jobs must respect measured capacity and liveness gates.
- `ops-pattern.artifact-first-research` — substantial research closes with a markdown artifact path plus short executive readout.
- `ops-pattern.web-routing` — public reads use local Firecrawl; interactive work uses the isolated per-client browser with a two-failure stop rule.

Example flow:

1. User asks: "draw me a picture of a broken lattice"
2. You call `mcp__capability_router__search_capabilities(query="generate image")`
3. Router returns hits including `catalog.openrouter-image` (`can_invoke_now: true`, `mcp_server: openrouter-image`, `tool_name: generate_image`)
4. You call `mcp__openrouter_image__generate_image(prompt="a broken lattice")`
5. Image returned, attached via `MEDIA:` tag

### When the first capability fails — try the next one (HARD RULE)

The first ranked hit isn't always right. The router's keyword scoring is imperfect; tools also fail for transient reasons (expired tokens, wrong account scope, rate limits). Your job is to exhaust the search results before declaring inability.

If your invocation errors:

1. **Do not report inability.** Fall through to the next-ranked candidate in the same `search_capabilities` response.
2. **Activate or skip candidates that can't run now** — activate an
   `activation_required: true` cold backend, but skip entries that are not
   policy-allowed, are blocked by missing `requires_creds`, or already failed
   in this turn.
3. **Try the next viable hit.** Re-attempt with whatever input adjustments the new tool's schema requires (call `describe_capability` if the schema isn't obvious).
4. **Continue until either** a candidate succeeds, OR every viable hit has been tried.

Only after that full exhaustion can you tell the user the capability isn't available — and even then, name what you tried and what each one returned, not just "can't do that." If multiple tools failed with the same error class (auth, network), surface that as a likely root cause; if they failed differently, surface the most informative error.

**Web-routing exception:** follow `ops-pattern.web-routing` and `browser-piloting.md` for public extraction and browser work. After two failures on the selected web lane, stop with a diagnostic; do not exhaust alternate browsers, profiles, daemons, or hand-rolled wrappers. A domain-specific tested harness may be selected before the first attempt when the router identifies it as the correct lane.

Concrete example: you ask the router "create calendar event," it returns `apple-calendar` first and `maton-calendar` second. Apple errors with auth. Doctrine: try `maton-calendar` next. Don't tell the user calendar isn't available — try the other hit.

After each invocation, call `record_capability_outcome` with the `capability_id` you tried and `outcome="success"` or `"failure"` (with `error_class` if it failed). Future searches on this host will boost capabilities that succeeded and demote ones that failed. The router gets smarter the more you use it.

### What NOT to do

- Do **not** maintain hardcoded mental lists of which MCP does what. The catalog grows. Trust the router.
- Do **not** reply "image generation isn't available" or "I don't have that capability" before calling the router AND exhausting every viable hit it returned. First-attempt failure ≠ capability is gone.
- Do **not** invent tool names from training data. Only invoke MCP tools the router or your hot toolset confirms exist.
- Do **not** call the router for every trivial turn. Use your hot tools (native memory, `telegram-directory`, profile-specific always-on) directly.

### Cost / billing note

Capabilities backed by paid APIs (OpenRouter, FAL, Anthropic, etc.) bill against the operator's keys, not the user's. The flat-fee Bot Doctor model means routine capability use is already covered — invoke freely when the user's request justifies it.

### When in doubt

If you're partway through a turn and realize the user's request needs a capability you don't see in your toolset: **stop, call the router, then continue.** Do not improvise a fallback (browser scrape, terminal hack, hand-rolled API call) when the router can point you at a real MCP.

### Common traps — named anti-patterns (ALWAYS-WRONG tool calls)

These are tool-call patterns that LOOK plausible but ALWAYS fail. The router will avoid them; this list is the explicit memory so you do not burn a turn rediscovering them.

- **Vault file reads — never call `mac-control/read_resource`.** `mac-control` exposes only `open_app`, `list_running_apps`, `get_frontmost_app`, `show_notification`. It has zero MCP resources. Any `read_resource` call against it returns "Unknown resource" no matter the URI. Use the configured client-isolated GBrain lookup lane for filed knowledge.

- **Vault file reads — never feed `local-media/file_text_extract` a knowledge-store path.** `local-media` is rooted at `MEDIA_ROOT` (typically `~/.hermes-local/media`), not the knowledge store. Use the configured client-isolated GBrain lookup lane.

- **Sending mail / calendar invites — never hand-roll `send_gmail.py` / `send_calendar_invite.py` / similar one-off scripts.** Use the Maton MCPs (`maton-gmail`, `maton-google-docs`, `maton-notion`, etc.) via the router. Hand-rolled scripts bypass keychain auth, audit logging, and retry handling. If a Maton MCP is cold, activate the policy-allowed backend and search again rather than scripting around it.

- **First-attempt failure is not capability-absent.** If a tool errors with `Unknown resource`, `path escapes`, `auth failed`, `404`, etc., you have learned ONE tool does not do the job. Do NOT retry the same tool with a URL-encoded variant, a plain-path variant, or a slightly different argument shape more than once. Move on to the next router hit. Looping on a broken tool burns 20+ minutes per turn.

When you discover a new always-wrong pattern, append it here. The list is a deliberate memory, not a performance burden — every entry saves a future turn.
