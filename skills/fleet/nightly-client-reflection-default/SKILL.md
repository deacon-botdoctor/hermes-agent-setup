---
name: nightly-client-reflection-default
description: "Opt-in workflow for explicitly authorized nightly self-reflection and day-review closeout."
version: 2.0.0
tags: [hermes, default, nightly-reflection, client-day-review, skillify]
category: fleet-default
status: active
---

# Nightly Client Reflection (Opt-in)

Automatic reflection is disabled by default. Use this workflow only for an
explicitly approved opt-in whose configuration has
`auxiliary.background_review.enabled: true`. Do not enable reflection, create
a schedule, or infer approval from this skill being installed.

## Hard rule

Do not Skillify the generic nightly reflection mechanism itself. It is already a
shared procedure for approved opt-ins. Record a candidate only when reflection reveals a specific
reusable workflow beyond the reflection job. The nightly runtime does not create
local drafts, archives, or active skills: its findings stay in the day-review
report until the central collector accepts a content-free proposal envelope.

## Opt-in workflow

1. Start from deterministic day-review signals: scores, verdict, client-visible harm, escalations, open/promised asks, existing deterministic Skillify candidates, and every pending local papercut.
2. Read the bounded recent-skill inventory produced from successful local skill loads. Treat it as use evidence only, never proof that a skill caused an outcome.
3. Reflect honestly in the standard JSON shape: `did_it_suck`, `narrative`, `failures`, `proposal_inputs`, `open_promises`, `role_alignment`, `operator_systems_lessons`, `papercut_actions`, and `escalate_to_doc`.
4. Keep green days plain and short; do not invent work to justify the reflection.
5. Convert yellow/red findings into specific next actions: proof closeout, client-safe response hygiene, routing repair, tool/auth repair, or Doc/operator escalation.
6. Suppress generic candidates named like nightly reflection, self-review, reflection JSON, or agent reflection; those are covered by this default.
7. Emit `proposal_inputs` only for concrete repeated workflows with a trigger and why it matters. A proposal about an existing skill must copy its exact `skill_id`, `skill_version`, and `skill_sha256` from the recent inventory and classify the evidence as repeated failure, operator correction, client rework, or repeated success. Mismatches are discarded.
8. An actionable outcome may carry task-contract metadata only when the day-review contract has exactly one complete, canonical `client-chat-help` declaration. The metadata is fixed, contains no transcript/request text, and requests independent client acceptance or Doc certification; it is not a success verdict and must not be hand-tuned.
9. For each papercut pattern, choose a disposition: monitor once, repair a concrete cause, create a proposal-first skill candidate for repeated procedure, or escalate a real blocker. For a tool/dependency papercut or repeated tool gap, follow the papercuts skill’s plugin-catalog step: installed capability first, a unique catalog proposal second, and no automatic installation or new schedule. A written reflection report is the acknowledgement boundary; failed or missing reflection leaves the inbox pending.
10. Remediation is routed, not improvised: skill candidates enter the inert central lane; task-contract signatures stay at the `defer` rung for central certification, and repair/escalate actions become local repair envelopes for operator-control collection. Never edit or promote a skill from reflection, never execute model-authored shell text, and preserve approval gates for credentials, restarts, fleet/client mutation, and destructive actions.

## Approval boundary

This skill grants no rollout or scheduling authority. Enabling reflection
requires an explicit operator decision. Any client-visible runtime/config
change discovered during reflection retains its normal approval boundary.
