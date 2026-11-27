---
name: capability-scout
description: Discover authorized APIs and skills by task. Use for research, identity resolution, people/company enrichment, web/news/social data, extraction, or a tool or quality gap. Search TReg before treating generic web search as the only route.
triggers:
  - capability gap
  - tool gap
  - skill gap
  - quality gap
---

# Capability Scout

For research, discover the API that matches the task before committing to a
familiar provider. Do not wait for a failure or for the principal to name TReg.
For other tasks, scout when a tool or output-quality gap needs another route.

## Research API discovery

The canonical procedure is GBrain `workflows/treg-research-discovery` in the
operator library. Retrieve it only through an authorized knowledge route;
client agents must not cross into another principal's vault. The following
runtime adapter supports native discovery without requiring operator access.

Data Patriot is retired. Do not call its research, status, brief, or deep APIs.
Older skills that route there are superseded for tool selection. Preserve their
unrelated analysis and source standards.

Use native `tool_search` to discover TReg and exact callable names. The custom
capability router is a secondary reference for cold tools and operating patterns,
not the live execution registry. Search TReg with `catalog_search` using the task,
then inspect `catalog_get` for the selected endpoint before `call`.

Choose the capability from the requested output and the identifiers you have:

| Input and task | Discover | Check before claiming a result |
|---|---|---|
| Person name plus company, no email | Name/company identity or profile search | Candidate profile and corroborated affiliation; never invent an email. |
| Known email | Reverse-email identity lookup | Email verification only checks deliverability. Corroborate the address-to-person and person-to-profile links. |
| Company domain and business facts | Company enrichment plus required official-source reads | Structured fields and their sources. An office, registered address or generic location is not automatically headquarters. |
| Known page URL and named fields or quotations | Single-page fetch or structured extraction | Actual page content, requested fields, supporting text and the source URL. Snippets or a company summary are insufficient. |
| Profile URL, records or social content | Profile enrichment, official-record or platform-specific data | The requested fields and attributable source evidence. |

Do not guess parameters or start a whole-site crawl for a single page unless needed.
If an enrichment response cannot supply a requested field or provenance, include
an appropriate follow-up source read and keep unresolved fields unknown.

Inspect availability and relevant outcome observations before choosing a provider.
When the catalog reports substantial repeated failures or classified misses,
inspect another fitting authorized option before selecting a first attempt.
State when observations are missing or do not represent the task; a metric or a
provider miss does not establish absence. Reject blocked or unconfigured routes.

Read input requirements, output, current price, miss semantics and async behavior.
Prefer an authorized installed route; compare fit before cost. An HTTP success
rate is not a match rate or research-quality score. Routed TReg endpoints may
try multiple providers: inspect their documented ceiling and charge semantics.
Use the requesting agent's configured identity. Disclose metered prices and use
existing spending authority; do not change caps, buy credits or borrow tokens.

Invoke the appropriate endpoint and read the returned sources. Verify identity
against known identifiers and source context. Preserve citations, job handles,
actual charges and concrete gaps. A completed job can contain incomplete research.

For transient failures or weak evidence, rediscover an independent suitable route
or direct source. Correct validation errors against the schema. Do not bypass a
cap or access refusal. A failed backend plus generic web searches is not exhaustive
research. Report which routes were checked and what remains unknown.

## Discovery

1. Check the visible tools and relevant installed skills. If the capability is
   deferred, follow `capability-discovery.md`: native `tool_search` first, then
   capability-router reference guidance when native search has no viable match.
2. Stop as soon as a suitable installed, authorized capability is found. Inspect
   its schema or instructions as needed; do not continue searching to fill a quota.
3. If the gap remains, search `~/.hermes/shared-defaults/skill-catalog-index.json`
   for an endorsed or baseline capability. Treat `candidate` entries as discovery
   only; they are not invokable or install authority. Search Skills Hub only if
   relevant installed options and the catalog do not resolve the gap; vary the
   query when the results warrant it.
4. For an unresolved tool or dependency gap, search the native plugin catalog
   with `hermes plugins search "<gap keywords>" --json`. Propose only a unique
   fit that does not duplicate an installed MCP or skill. Skip spending, PII
   transfer, or new OAuth unless already authorized. Never install, enable, or
   grant tool overrides from this scout.
5. Inspect promising candidates for actual fit, availability, authorization, and
   cost. Use a bounded smoke test only when needed and permitted. Select the
   viable route, propose an installation or internal skill candidate, or report
   the specific blocker.

For a browser lane, stop after two failed attempts on the selected lane. Report
the failing lane, concise diagnostic, and permitted recovery action. Scouting
does not authorize alternate browsers, profiles, daemons, or credential export
as a workaround for a readiness failure.

## Report

When discovery changes the path or leaves a blocker, briefly state the gap,
selected route, and supporting result. Name alternatives only when they help
the principal make a remaining choice. Ordinary successful discovery needs no
separate report.

## Safety

Do not install runtime-changing skills into client profiles without approval.
The compact Golden catalog is a discovery index, not permission to pull a Git
branch or activate a candidate. Do not add paid/external/network capabilities
as default client baseline without cost/safety review. Existing tools remain
subject to the task's authorization and tenant boundaries.
