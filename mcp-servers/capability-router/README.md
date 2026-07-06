# capability-router

Clean-room MCP catalog for the shared floor. Keep this server on the hot path so the agent can
search for the right heavier MCP before loading on-demand servers.

## Runtime wiring

Point Python at the source tree and the server at the bundled registry:

```bash
export PYTHONPATH="$PWD/mcp-servers/capability-router/src:$PYTHONPATH"
export CAPABILITY_REGISTRY="$PWD/mcp-servers/capability-router/registry.json"
python -m capability_router.server
```

`CAPABILITY_USAGE_DB` is optional. When unset, usage records are written to
`$HERMES_HOME/state/capability-router-usage.db`, or `~/.hermes/state/capability-router-usage.db`
when `HERMES_HOME` is unset.

## Tools

- `search_capabilities(query, max_hits=8)` searches ids, categories, labels, summaries, MCP
  server names, tool names, and `preferred_for` phrases. Results include `availability:
  "catalog"`, `can_invoke_now: true`, `score`, and `score_breakdown`.
- `describe_capability(capability_id)` returns one registry entry by id, or
  `unknown_capability`.
- `list_categories()` returns the registry category list.
- `registry_status()` reports registry path, capability/category totals, usage row counts, and
  usage score by capability.
- `record_capability_outcome(capability_id, outcome|ok, query?, failure_class?,
  failure_detail?, duration_ms?)` records success/failure feedback. Success adds `+1` to future
  ranking; failure adds `-2`. Missing or invalid outcomes are rejected without writing a row.

## Registry format

The bundled `registry.json` uses `schema_version: 1`, a `categories` list, and a `capabilities`
list. Each capability should include at least:

```json
{
  "id": "web.browser",
  "category": "web",
  "label": "Browser MCP",
  "summary": "Use the plain browser MCP for browser, CDP, page navigation, and screenshot actions.",
  "mcp_server": "browser",
  "tool_name": "browser_open",
  "preferred_for": ["browser", "CDP", "page navigation", "screenshot", "interactive page"]
}
```

Extra fields are preserved and returned by `search_capabilities` and `describe_capability`, so
registry entries can carry routing metadata such as `routing_policy`.
