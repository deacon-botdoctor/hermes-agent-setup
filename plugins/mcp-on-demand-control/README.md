# MCP on-demand control

This plugin lets a long-lived Hermes gateway activate a configured cold MCP
backend without restarting the gateway or starting the backend in a separate
CLI process. Activation changes only the current process; it does not rewrite
`config.yaml` or make the backend hot at the next gateway start.

## Configuration contract

Enable the plugin and expose its toolset on every platform that may activate a
backend:

```yaml
plugins:
  enabled:
    - mcp-on-demand-control
platform_toolsets:
  telegram:
    - mcp-on-demand-control
```

A backend must be declared under `mcp_servers` and named in at least one of
these `mcp_policy` lists: `on_demand`, `active_enabled`, `hot_path`, or
`hot_path_enabled`. A normal cold backend uses `enabled: false`,
`tier: on_demand`, and `mcp_policy.on_demand`:

```yaml
mcp_servers:
  calendar:
    enabled: false
    tier: on_demand
mcp_policy:
  on_demand:
    - calendar
```

The backend's `mcp-<server-name>` toolset must also be exposed on the calling
platform so tools registered after activation can be invoked. The shared
default reconciler adds the control and policy-authorized backend toolsets on
CLI, Telegram, and cron while preserving client-specific entries. It also
repairs any additional platform that already exposes capability-router, so the
router, control tools, and authorized backend tools stay available together.

## Tools

- `mcp_server_status(server_name="")` returns sanitized state for one allowed
  backend, or all configured and allowed backends when the name is omitted.
  Public states are `cold`, `configured`, `connecting`, `connected`, `failed`,
  or `unavailable`; missing and disallowed names return `missing` and
  `not_allowed` with `ok: false`.
- `restart_mcp_server(server_name)` activates a cold backend or reconnects an
  existing one inside the current gateway process. Success returns
  `ok: true`, `status: connected`, the registered tool count, and a reminder
  to run tool search again before invoking the newly registered tool. After
  the control call completes, the runtime refreshes the current agent's tool
  snapshot before its next model request when registration changed, so search
  and invocation can continue in the same turn and session.

## Persisted legacy aliases

Persisted sessions may replay the retired flattened `mcp_server_tool` spelling
while a policy-authorized backend is still cold and its canonical
`mcp__server__tool` schema is absent. The compatibility bridge resolves that
request only when its server prefix identifies exactly one backend in the
current toolset scope (or exactly one policy-allowed backend when scope is
unrestricted), both control tools are exposed, and the backend is not disabled.
It then uses the existing restart control in ensure-active mode, refreshes the
scoped definitions, reuses that refreshed scope for the final scope check, and
dispatches the canonical action in the same request. The ensure-active signal
is trusted dispatcher context, not a model-facing tool argument; matching
values supplied in tool arguments are ignored. A backend that is already
connected is left running rather than reconnected. Canonical calls already in
the cached session scope stay on the normal fast path and do not rebuild the
cold catalog. Ambiguous prefixes, missing control tools, disabled or
out-of-scope backends, activation failures, and unresolved post-activation
aliases fail closed without dispatching the requested action. This is replay
compatibility only; new calls must use the canonical collision-safe name
returned by tool search.

Activation is policy-gated. A missing name, undeclared server, or server absent
from all four policy lists is rejected. `mcp_policy.disabled` and
`mcp_policy.on_demand_disabled` take precedence over every allowlist. Transport
failures are returned as sanitized, retryable failures; inspect the gateway MCP
log for transport detail.

Registry synchronization uses the same policy contract. A server declared in
`mcp_servers` and authorized by one of the four allowlists remains discoverable
even when `enabled: false`; `enabled`, `tier`, and metadata do not replace
policy authorization. The sync publishes one server marker for a cold
`enabled: false` backend and does not expand its cached tool schemas into the
always-hot router catalog. Enabled backends may publish cached tool entries;
those entries are deduplicated against canonical and local entries by
normalized server/tool identity. Denies win within each config. Generated
`autogen:tool-schema/*` extras are pruned when the selected config authorizes
their server as cold, while manual and canonical entries remain. They are also
pruned when readable policy across all root, profile, and explicit configs no
longer authorizes the server; unreadable or unsupported config fails safe by
preserving those extras. PyYAML is optional:
without it, sync still reads the standard top-level `mcp_servers` declarations,
their boolean `enabled` state, and block or inline forms of the six policy
lists.

The plugin publishes a per-process receipt under
`$HERMES_HOME/state/mcp-activation/` and refreshes it every five seconds. A
backend counts as active only while the current host process reports
`connected` with at least one registered tool; denied, disconnected, zero-tool,
and stale-process entries never make discovery report it as callable. Receipts
are removed when the host process exits normally.

The capability router reports policy-authorized cold backends with
`availability: cold`, `can_invoke_now: false`, `activation_required: true`, and
the status and activation tool names. After successful activation, search
again so the router and current agent tool snapshot reflect the connected
backend. This refresh does not override policy denies or platform toolset
exposure.
