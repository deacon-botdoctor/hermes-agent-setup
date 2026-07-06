# Fresh install — from bare runtime to this setup

Order matters less than the mindset: install the stock runtime, get it working *unmodified*
first, then add only what maps to a problem you actually hit. Resist configuring things you
don't yet need.

## 0. Stock first

Install the runtime per its own docs and get a plain agent talking on your platform with **no
customization**. This is your baseline and your fallback. If something later breaks, you can
always diff against pristine stock. (This is also how you tell scar tissue from a real need —
if stock already does the thing, you don't need an overlay for it.)

## 1. Config pass

Copy [`../config/config.example.yaml`](../config/config.example.yaml), and go key by key against
your generated default config. For each edit, confirm you have the problem it solves before
applying it. The high-value ones for almost anyone:

- **Redaction enabled** — do this first. Security.
- **Durable runtime** — if your agent does multi-minute work and restarts matter.
- **Input mode = queue** — if you don't want follow-up messages killing in-flight turns.
- **Web backend = Firecrawl** — local self-hosted is the example default; see
  [`../docs/firecrawl.md`](../docs/firecrawl.md) if you need web scraping/crawl.
- **Context / model / fallback** — keep `context.engine: lcm`, set the primary
  `model.default` / `model.provider`, and either wire or remove each `fallback_providers`
  entry you cannot authenticate.
- **MCP floor** — keep `capability-router` hot and the shared floor in `mcp_policy.on_demand`
  only when those MCP commands are installed; set `PYTHONPATH`, `CAPABILITY_REGISTRY`, and
  optionally `CAPABILITY_USAGE_DB` for the router; set `BROWSER_CDP_URL` and
  `BROWSER_LANE_SOCKET` when enabling the bundled browser MCPs; set
  `LOCAL_DOCUMENT_TOOLS_ROOTS` to the readable document/workspace roots when enabling
  `local-document-tools`; set `ANAMNESIS_DB`, `TELEGRAM_DIRECTORY`,
  `VISUAL_IDENTITY_MANIFEST`, and `VISUAL_IDENTITY_ROOT` when enabling the memory,
  Telegram-admin, and visual-identity MCPs; move client-specific MCPs into local config.
- **Plugins** — install or prune the canonical-floor names in `plugins.enabled`; the bundled
  local plugin directories cover immersion, memory, and the disabled Telegram placeholder only.
- **Approvals / toolsets** — the example leaves Telegram unattended with the full toolset.
  Keep that only for trusted lanes; for untrusted clients, narrow Telegram tools or turn
  approvals on.
- **Compression / autoraise** — leave autoraise on for large-context models (see the config
  comments).

## 2. Instructions

Drop in an [`AGENTS.md`](../config/AGENTS.example.md) that wires the change ladder and the
canonical-lookup rule. Keep it short.

## 3. Canonical knowledge (optional but recommended)

If you're running the agent for real work with durable facts it must get right, stand up a
canonical store and wire the lookup rule. See
[`../docs/canonical-knowledge.md`](../docs/canonical-knowledge.md). You can skip this for a toy
agent; you'll want it the moment "get this fact right" matters.

Use [`../docs/canonical-client-spec.md`](../docs/canonical-client-spec.md) as the scrubbed
checklist for deciding which real-client floor sections belong in your public/default skeleton
and which belong only in an overlay or local runtime config.

## 4. Plugins

Add plugins for behavior that has a supported seam — memory and output transforms today, with
platform overrides as a future seam. `plugins/telegram_platform` is currently a disabled
placeholder and leaves the bundled Telegram adapter active. See [`../docs/plugins.md`](../docs/plugins.md).
Prefer a plugin over a patch every time a seam exists. Install each as a self-contained,
idempotent package.

## 5. Overlay features (last resort)

Only now, and only for the behaviors that have **no** config or plugin path, reach for
source-level overlay features. See [`../docs/features.md`](../docs/features.md). Every one of
these is debt you'll re-verify on each upstream bump — so keep the set as small as it can be.

Track them in a small registry (name, target file, reason, retirement condition) and re-run
against a fresh upstream checkout on every bump to catch silent breakage early. The goal is a
tiny, well-understood overlay — small enough that upgrading the underlying runtime is boring.

## The maintenance loop

On every upstream bump:

1. Rehearse your overlay against the new version in a sandbox **before** deploying. Anything
   that fails to apply cleanly is a patch that needs re-anchoring — catch it here, not in
   production.
2. For anything upstream now does natively, delete your version.
3. For anything that grew a config knob, route to it and delete the patch.
4. Deploy one host at a time, verify, then propagate. There is no safe "push to everything at
   once" — a single bad push to every runtime is the failure mode that discipline prevents.
