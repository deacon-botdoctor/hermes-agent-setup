---
name: capability-scout
description: Find a suitable authorized tool or skill when an unresolved capability or output-quality gap prevents a viable task path.
triggers:
  - capability gap
  - tool gap
  - skill gap
  - quality gap
---

# Capability Scout

Use this when available tools do not provide a viable path, or an observed
output-quality failure requires a different capability. A client-visible
artifact, repeatable task, or general desire for quality alone does not require
scouting. If a suitable installed authorized tool or skill is already known,
use it directly.

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
