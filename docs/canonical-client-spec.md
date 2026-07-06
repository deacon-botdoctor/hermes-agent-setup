# Canonical client spec checklist

This is the public, scrubbed checklist for rebuilding the client-ready bundle from real
client-standard runtimes, not a test agent alone. It records section and capability names only,
never tokens, chat ids, hostnames, client names, private paths, or live values.

## Source rule

- Build the bundle from the shared floor across multiple real client-standard runtimes.
- Treat a test agent as a smoke-test target, not as the shape of the product.
- Put client-specific capabilities in an overlay or local config, not in the public default.
- Keep Printing Press and other proprietary systems out of this bundle.

## Shared floor observed across real clients

### Config sections

`_config_version`, `agent`, `approvals`, `auxiliary`, `client_identity`, `compression`,
`context`, `delegation`, `display`, `durable_runtime`, `durable_runtime_stage_b`,
`durable_runtime_stage_c`, `fallback_providers`, `gateway`, `hooks`, `hooks_auto_accept`,
`mcp_policy`, `mcp_proxy`, `mcp_servers`, `memory`, `model`, `notifications`,
`operator_alerts`, `platform_toolsets`, `platforms`, `plugins`, `providers`,
`session_reset`, `skills`, `smart_model_routing`, `telegram`, `terminal`, `tools`,
`toolsets`, `web`.

### MCP servers

The shared floor is:

- `capability-router`
- `anamnesis`
- `browser`
- `browser-lane`
- `gbrain`
- `local-document-tools`
- `search`
- `telegram-admin`
- `visual-identity`
- `web-search`

`capability-router` is hot-path. The other floor MCPs should be on-demand via `mcp_policy`.
The bundled `browser` MCP talks to a Chromium DevTools Protocol endpoint (`BROWSER_CDP_URL`),
while `browser-lane` talks to the browser-lane daemon socket (`BROWSER_LANE_SOCKET`) and reports
the same CDP endpoint for lane readiness checks.
The bundled `local-document-tools` MCP is a safe local document starter: allowlist readable roots
with `LOCAL_DOCUMENT_TOOLS_ROOTS`, then extract or merge text/HTML while leaving PDF/OCR/rich
conversion to optional adapters.
The bundled `search` and `web-search` implementations are distinct shared-floor MCP surfaces:
both search SearXNG and scrape pages through Firecrawl, but expose the real client tool names
separately.
The bundled `anamnesis` MCP stores local SQLite-FTS memories at `ANAMNESIS_DB`; `telegram-admin`
reads only scrubbed Telegram directory fields from `TELEGRAM_DIRECTORY`; `visual-identity` reads
approved asset metadata from `VISUAL_IDENTITY_MANIFEST` and returns only paths under
`VISUAL_IDENTITY_ROOT`.

### Plugins

The shared floor is:

- `composio-onboarding`
- `hermes-lcm`
- `Task Ledger`
- `Telegram Transcript`
- `autoDream`

Optional plugin surfaces observed in real clients include `unified-memory`, `telegram`, and
`telegram_history`. Keep them out of the public default unless the runtime wiring is present and
tested.

### Skills config keys

The shared floor is `index_allowlist` and `index_description_max`. Optional observed keys include
`creation_nudge_interval`, `external_dirs`, `guard_agent_created`, `inline_shell`,
`inline_shell_timeout`, and `template_vars`.

## Optional observed sections

The larger real-client union includes platform or client-specific sections such as `bedrock`,
`browser`, `checkpoints`, `cron`, `dashboard`, `discord`, `honcho`, `human_delay`, `matrix`,
`mattermost`, `mcp_profiles`, `personalities`, `privacy`, `slack`, `stt`, `tts`, `voice`,
`whatsapp`, and workflow-specialist sections. These are rebuild targets, but not all belong in
the public default skeleton.

## Rebuild wave order

1. Client behavior core: reply rules, model chain, fallback, memory, context, LCM.
2. Tools/MCP: capability-router, lazy-load policy, search, browser, documents, web.
3. Knowledge/memory: GBrain resources, anamnesis, visual-identity, dream cycle.
4. Voice/audio: TTS, STT, voice, human delay, talkie-style surfaces.
5. Platforms: Telegram first, then Discord/WhatsApp/webhook where generic.
6. Governance/health: security, approvals, hooks, operator alerts, probes, cron, self-improvement.
