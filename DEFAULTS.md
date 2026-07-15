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
