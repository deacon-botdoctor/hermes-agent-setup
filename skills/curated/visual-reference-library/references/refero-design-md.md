# Refero DESIGN.md library

Public ideation source for agents and visual staff when a client wants more visual ideas.

- Browse: https://styles.refero.design/
- MCP: `refero-styles` (read-only). Tools: `refero_search`, `refero_get`, `refero_similar`, `refero_list`, `refero_design_md`, `refero_status`.
- CLI: `${HERMES_HOME:-$HOME/.hermes}/bin/refero-styles search "warm editorial saas"`
- List API: `GET https://styles.refero.design/api/styles?page=N`
- Detail API: `GET https://styles.refero.design/api/styles/{uuid}`
- Source note: https://x.com/Voxyz_ai/status/2093766772029559077

Each style includes colors/tokens, type, spacing, components, and do/don't rules. There is no `/design.md` endpoint. The MCP synthesizes DESIGN.md from `fullResult.designSystem` and never writes it to disk.

## When to use

Client or visual staff asks for more ideas, options, a different vibe, or "not generic SaaS."

## How

1. Prefer the `refero-styles` MCP. If it is not loaded, use the CLI. Do not install `fidgetcoding/refero-design-mcp` or any MCP that writes DESIGN.md into a project.
2. Match the brief to 1–3 styles by `siteName`, `northStar`, `colorScheme`, and industry. Do not dump the catalog.
3. Pull detail for those IDs only.
4. Extract tokens, type hierarchy, spacing rhythm, and do/don't.
5. Client overlay still wins: approved logo, HEX, fonts, examples.
6. Produce in the normal visual-reference lane.

## Do not

- Copy Linear, Notion, ElevenLabs, Lamborghini, Apple, or any named product as the client identity.
- Save a third-party DESIGN.md into a client repo as the brand system.
- Copy Refero screenshots or videos into client artifacts.
- Mirror the full catalog into a client runtime.
- Let this source override brand rules, asset rights, or QA gates.

## Staff

Visual design staff can browse the site directly. Pick a style, read the rules, take direction. Do not paste the file into a client brand folder.
