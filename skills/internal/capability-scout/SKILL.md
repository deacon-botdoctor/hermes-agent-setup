---
name: capability-scout
description: Search installed skills, Skills Hub, and available tools before weak workarounds when a capability or quality gap appears.
triggers:
  - capability gap
  - tool gap
  - skill gap
  - better tool
  - can do this badly
  - quality gap
---

# Capability Scout

Use this when the agent lacks a strong path, when quality matters, or when the user corrects tooling/output quality.

## Trigger conditions

Invoke before improvising if any of these are true:

- “I don’t have the tool.”
- “I can do this, but badly.”
- The work is client-visible creative/design/document output.
- The user corrected quality, tool choice, or effort level.
- A visible tool failed and there is likely a better tool/skill/MCP.
- The task resembles a repeatable workflow.

## Required scout sequence

1. Check installed/local skills for the task category.
2. Search `~/.hermes/shared-defaults/skill-catalog-index.json` for an endorsed
   or baseline fleet capability. Treat `candidate` entries as discovery only;
   they are not invokable or install authority.
3. Search Skills Hub with 2–4 query variants.
4. Search available tools/MCP/capability router for concrete capabilities.
5. Inspect the top 2–3 candidates rather than trusting names.
6. If safe, run a sandbox smoke test.
7. Choose one:
   - use installed skill,
   - install/enable candidate with approval,
   - route to better tool/provider,
   - write a new internal skill candidate,
   - or report a real blocker.

## Output format

Keep it short unless the user asks for details:

- Gap: what was missing or weak.
- Candidates checked: 2–3 names.
- Decision: selected path.
- Proof: command/tool result or artifact path.

## Safety

Do not install runtime-changing skills into client profiles without approval.
The compact Golden catalog is a discovery index, not permission to pull a Git
branch or activate a candidate. Do not add paid/external/network capabilities
as default client baseline without cost/safety review.
