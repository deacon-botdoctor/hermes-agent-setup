---
name: self-improvement-loop-gates
description: "Operator gates for Hermes self-improvement: archive first, accept with held-out proof, promote only by approval."
version: 1.1.0
tags: [internal, self-improvement, safety, promotion]
category: internal
---

# Self-Improvement Loop Gates

Use this when reviewing, mining, drafting, accepting, or promoting a Hermes self-improvement candidate.

## Hard rules

1. The fleet runtime emits only a content-free proposal envelope; it never creates an archive, draft, or live skill.
2. Archive first at the central owner. Every collected candidate needs a durable `self-edit-archive` record before any draft or promotion decision.
3. No live mutation by default. Any central draft stays inert and must include `.DO_NOT_EXECUTE` until accepted and approved.
4. Held-out proof is mandatory. Promotion needs held-in improvement and held-out non-regression.
5. Lowest viable rung wins: memory, then skill/shared-rule, then config/plugin/sidecar, then patch.
6. Outside-loop surfaces are locked. The improver path cannot edit archive, acceptance, miner, promotion, approval, credential, spend, or client-isolation gates.
7. Background review is proposal-first. Ordinary sessions should usually produce no proposal; repeated class-level evidence can become `proposal_inputs`.
8. Skill attribution is exact and non-causal. A skill-specific proposal must match a recently loaded local skill's declared version and SHA-256 digest. Successful load evidence alone never proves an outcome.

## Workflow

1. Mine repeated weakness signals into redacted clusters.
2. Convert eligible clusters into `proposal_inputs` with `target_rung` and `held_out_status`.
3. Emit the bounded, content-free proposal envelope; the runtime retains the source report and makes no archive/draft mutation.
4. After collection, archive the candidate and evidence pointers at the central owner.
5. Write only inert central drafts for skill/shared-rule rungs. Memory-rung candidates remain archive-only.
6. Run two-split acceptance: held-in must improve, held-out must not fail or regress.
7. Emit a schema-compatible approval card only for accepted candidates that need operator judgment.
8. Promote only after approval/canary, with rollback plan, retirement condition, and watch window recorded.

## Reject by default when

- there is no archive record,
- evidence is narrative-only,
- held-out cases are missing,
- held-in ties or held-out regresses,
- the proposal touches locked self-edit control surfaces,
- the proposed rung is higher than necessary,
- the candidate is caused by outage or capability limit rather than harness defect.

## Reference paths

- `shared-rules/self-edit-outside-loop.md`
- `bin/self_editing_v2_archive.py`
- `bin/self_editing_v2_miner.py`
- `bin/self_editing_v2_acceptance.py`
- `bin/self_editing_v2_promotion.py`
- `bin/hermes-reflect-candidates-to-drafts.py`
- `bin/self-improvement-trigger.py`
