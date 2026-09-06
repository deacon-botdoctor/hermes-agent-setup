## Capability discovery — native execution first

Hermes' native `tool_search` is the execution source of truth. It searches the
tools registered in the current gateway, returns exact callable names, and
preserves the normal authorization, plugin, and audit path through `tool_call`.
Use the custom capability router only as a secondary reference index for cold,
uninstalled, other-lane, or operating-pattern guidance.

### Hard rule

For a request that needs a capability not already visible in the hot toolset:

1. Call native `tool_search` with the user's intent.
2. If it returns the right tool, use `tool_describe` when its schema is not
   obvious, then invoke it with `tool_call`.
3. If native search has no viable match, search for the capability-router tools
   through native `tool_search`, then call
   `mcp__capability_router__search_capabilities` for broad inventory and routing
   guidance.
4. Invoke a router result only when it names an exact backend tool and reports
   `can_invoke_now: true`. Reference entries are never callable.

Do not guess tool names from memory. Do not tell the user a capability is
missing before both the current executable catalog and the relevant reference
route have been checked.

### Denial reflexes to avoid

Do not default to statements such as:

- "I am a text-only agent."
- "I cannot see images, files, your screen, or a browser."
- "That is not something I can do."
- "I would need credentials, access, or a tool" without naming the discovery
  and readiness checks actually run.

Discover first. If the route is genuinely unavailable, report the exact failed
or missing prerequisite and the smallest owner repair.

### Hot defaults

Use already-visible core tools directly, including native Hermes memory and
profile-specific always-on tools. Native image generation, MCP tools, and
non-core plugin tools may be deferred behind `tool_search`; that does not mean
they are unavailable.

Do not call either discovery layer for a trivial request whose tool is already
visible.

### Native executable search

Native Hermes exposes three progressive-disclosure tools:

- `tool_search` finds connected MCP/plugin tools by intent.
- `tool_describe` returns the exact schema for a deferred tool.
- `tool_call` invokes that deferred tool through the ordinary Hermes execution
  path.

Search results describe tools registered in the current gateway. They are the
authoritative answer to "what can run now?" A stale catalog entry must never
override native executable state.

### Capability-router reference search

The capability router remains useful for fleet-wide discovery that native
search cannot answer: policy-allowed cold servers, capabilities installed on a
different lane, credential prerequisites, and operating patterns.

Router results include `availability`, `can_invoke_now`,
`activation_required`, `activation_tool`, `status_tool`, `mcp_server`,
`tool_name`, and a ranked score breakdown.

- `can_invoke_now: true` requires an exact callable backend in the live gateway.
- `availability: cold` may be activated only when `activation_required: true`
  and the returned server is policy-allowed. Check status, activate it, then
  rerun native `tool_search` before invocation.
- `availability: reference` is guidance only. Read its routing policy and use
  the named native tool, skill, script, or owner lane.
- `configured_unverified`, `installed_not_declared`, and `not_declared` are not
  invocation permission.

The router is an index, not a proxy. Never invent or directly call a name that
only appears in reference metadata.

### Operating patterns

Operating-pattern entries have no backend tool and are always reference-only.
Use `describe_capability` to inspect their routing policy, then select the
underlying lane they name. Important examples include:

- `ops-pattern.durable-work` only for an explicit background/overnight request,
  monitoring, or restart-survival need; duration, complexity, and auditability alone do not move ownership
  out of the live conversation;
- GBrain local administration through its bounded operator lane;
- isolated worktrees for repo-writing work;
- resource-capacity gates for local inference;
- artifact-first closeout for substantial research;
- the bounded public-web/browser routing contract.

### Failure ladder

When the first tool fails:

1. Record the exact failure class.
2. Try the next viable result from the same native search.
3. If executable results are exhausted, consult the router for a policy-allowed
   cold backend or an alternate owner lane.
4. Stop only after every viable in-scope route has been tried, or when a real
   hard stop such as missing credentials or authorization is reached.

After a router-selected capability is invoked, call
`record_capability_outcome` with success or failure so host-local ranking can
learn. Do not record outcomes for native results that did not originate in the
router.

Web work is the exception to broad fallback: follow the web-routing operating
pattern and stop after two failures on the selected lane. Do not fan out across
alternate browsers, profiles, daemons, or improvised wrappers.

### Specialized routes

- Image generation and reference editing follow `image-generation-routing.md`:
  use Hermes' native `image_generate` route first, then escalate only on
  readiness or QA failure.
- Public extraction follows the canonical Firecrawl route; interactive work
  uses the isolated owner browser lane.
- Mail, calendar, documents, and tenant services use the dedicated runtime's
  authenticated connector rather than a hand-rolled API script.
- Filed knowledge uses the configured client-isolated GBrain lane, never an
  unrelated media or desktop MCP.

### Spend and provenance

Paid capabilities must use the accountable receipt/provenance seam. A tool's
presence in search results is not permission to bypass task, session, cron,
tool, or delegation provenance, and it is not permission to make an otherwise
unauthorized billed call. Emergency OpenRouter routes remain fail-closed when
their work reference is missing.

### Known bad routes

- Do not call `mac-control/read_resource`; that server exposes no resources.
- Do not use `local-media/file_text_extract` on knowledge-store paths; its root
  is the media store.
- Do not hand-roll mail or calendar send scripts around the authenticated
  connector and its audit path.
- Do not retry the same wrong tool with cosmetic path or argument changes more
  than once. Move to the next viable result.
- Do not treat first-attempt failure as proof that the capability is absent.

When a new always-wrong route is proven, add it here with its correct owner
lane so the fleet does not rediscover the same failure.
