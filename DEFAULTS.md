# Native-first default agent

The default assumes a headless machine and one user-owned Hermes runtime.

## Keep native

- Official Hermes installer and update path.
- Native `state.db`, session identity, compaction, session search, memory,
  `MEMORY.md`, `USER.md`, per-session task tracking, persistent goals, cron,
  gateway, and service tooling.
- One local config/data boundary per agent.
- Built-in skills and official extension points.
- Exact topic/session isolation on messaging platforms.

## Configure

- Start with the smallest toolset the user needs.
- Manage MCPs through native `hermes mcp` configuration; enable only the
  servers and tools the user needs.
- Keep browser automation off until a real workflow needs it.
- Keep Composio onboarding off until the user selects an account/integration.
- Store secrets only in the platform-supported local secret/config path.
- Use an external knowledge system only as an explicitly declared boundary;
  never silently substitute a local database for a shared canonical one.

## Capability model

Lean means **deferred**, not removed. Keep the default prompt and tool surface
small, then discover and activate a capability for the request that consumes
it. This lowers prompt/tool bloat, makes routing easier to inspect, and reduces
idle dependencies while preserving the same approved capability boundary.

Use this loop:

1. **Inventory/discover:** inspect the native tool and skill inventory plus any
   configured MCP or approved connector catalog relevant to the request.
2. **Activate/load:** enable or load only the selected capability and its
   narrow credential/data scope.
3. **Invoke:** perform the smallest safe operation that satisfies the request.
4. **Verify/retry:** check the result; retry only when the failure is transient
   or the corrected input is understood.
5. **Approved fallback:** move to the next route below only when it is allowed
   and materially better suited.
6. **Incident:** stop and report the evidence when authorization, credentials,
   health, or transport cannot be safely recovered.

Classify the state before choosing a remedy:

| State | Meaning | Next action |
| --- | --- | --- |
| Cold/unloaded | Discoverable but not active in this session | Load or enable it for the request |
| Unavailable | Not installed, configured, or supported here | Use an approved alternative or report the gap |
| Unauthenticated | The route exists but lacks a valid login/token | Run its safe auth/connection check; request authentication if needed |
| Unauthorized | Identity is known but lacks permission for this action | Stop; never widen scope or infer consent |
| Transport failure | The approved route is configured but unreachable or timing out | Verify health, make a bounded retry, then use an approved fallback or report an incident |

### Route doctrine

Route in this order:

1. native Hermes or a local user-owned capability;
2. connected SaaS through the user's approved connector or Composio surface;
3. browser automation only when the task requires UI interaction or the
   approved API/connector has a verified gap.

Availability never grants permission. A visible tool, account, page, token, or
connector does not authorize a read or mutation beyond the user's request and
the configured trust boundary.

Before saying **“I can't”**, inspect the capability router/inventory available
in the current agent surface and run a non-mutating doctor or connection check.
For native Hermes, the supported inspection commands include:

```bash
hermes tools list
hermes skills list
hermes mcp list
hermes doctor
```

Use only the commands relevant to the suspected route. These checks prove
presence and health; they do not grant authorization or justify installing a
new integration.

## Do not install by default

- LCM;
- Anamnesis;
- AutoDream or nightly-dream jobs;
- Qdrant/Ollama solely to recreate old memory behavior;
- a Telegram transcript database;
- permanent browser daemons;
- all MCP servers at startup;
- copied upstream source or placeholder compatibility plugins;
- direct broad GitHub credentials.

## Desktop exception

On a user-operated desktop, a local notes application or search launcher may be
useful. That is a user-interface choice, not a runtime memory requirement. Keep
the underlying files portable and do not make the agent depend on a GUI being
open.

## Adding something back

Add an optional component only when all five are known:

1. the current user/workflow that consumes it;
2. why native Hermes or configuration is insufficient;
3. its data and credential boundary;
4. its health check;
5. its independent rollback.

If those answers are missing, leave it out.
