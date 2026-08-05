---
name: human-taste
description: Evaluate UX and product design quality through the lens of human taste -- cognitive load, visual coherence, interaction clarity, and context fit. Use this skill whenever the user asks you to review a UI, critique a design, do a UX audit, assess aesthetic quality, check cognitive load, run a taste test on a product, evaluate user flow, or judge whether a design "feels right." Also use it when reviewing AI-generated designs or comparing design alternatives, even if the user does not explicitly mention "human taste."
---

# Human Taste

Evaluate UX and product design through human taste -- the trained judgment that detects whether a design reduces cognitive friction, feels coherent, and fits its audience.

This skill is grounded in research from cognitive psychology, HCI, and design practice. For full citations see [references/research-sources.md](references/research-sources.md).

## Why This Matters

LLMs can generate designs, but aesthetic judgment involves empathy, cultural awareness, and pattern recognition that require human-calibrated evaluation. Research shows:
- Users form aesthetic impressions within milliseconds (eye-tracking studies)
- Interfaces that reduce cognitive load are perceived as more beautiful (Processing Fluency Theory)
- Taste develops through repeated exposure and operates at a pre-conscious perceptual level
- Good taste means choosing simplicity over mere familiarity (Hickey's Simple vs Easy)

This skill provides a structured protocol so agents can approximate that judgment systematically.

## Quick Start

When asked to evaluate a design:

1. **Identify what you are evaluating** -- screenshot, wireframe, live page, component, or described flow
2. **Run the rubric** below across all six dimensions
3. **Produce a Human Taste Report** using the output template
4. **Cite specific elements** -- never give vague praise or criticism

## Evaluation Rubric

Score each dimension 1-5. Anchor your score with concrete evidence from the design.

### 1. Cognitive Load (weight: high)

Does the design minimize unnecessary mental effort?

| Score | Meaning |
|-------|---------|
| 1 | Overwhelming -- too many competing elements, no clear entry point |
| 2 | Heavy -- user must work to understand the hierarchy |
| 3 | Moderate -- some unnecessary complexity but functional |
| 4 | Light -- clear hierarchy, minimal distractions |
| 5 | Effortless -- information is exactly where you expect it |

Look for: element count per view, competing focal points, label clarity, progressive disclosure, information grouping.

### 2. Visual Coherence (weight: high)

Does the design feel unified rather than assembled from parts?

| Score | Meaning |
|-------|---------|
| 1 | Fragmented -- inconsistent spacing, colors, typography |
| 2 | Patchy -- some consistency but noticeable breaks |
| 3 | Adequate -- follows a system with minor deviations |
| 4 | Cohesive -- strong visual rhythm, clear design system |
| 5 | Seamless -- every element reinforces the whole |

Look for: spacing consistency, color palette discipline, typographic scale, alignment grid, icon style unity.

### 3. Interaction Clarity (weight: high)

Can a user predict what happens next at every step?

| Score | Meaning |
|-------|---------|
| 1 | Opaque -- controls are ambiguous, outcomes unclear |
| 2 | Confusing -- some actions have surprising results |
| 3 | Functional -- most flows are predictable |
| 4 | Clear -- affordances are obvious, feedback is immediate |
| 5 | Intuitive -- zero learning curve, flows feel inevitable |

Look for: button labels, hover/focus states, loading indicators, error messages, navigation predictability, undo availability.

### 4. Context Fit (weight: medium)

Does the design match its audience and environment?

| Score | Meaning |
|-------|---------|
| 1 | Mismatch -- tone, density, or style wrong for the audience |
| 2 | Off -- partially appropriate but feels generic |
| 3 | Acceptable -- reasonable for the context |
| 4 | Tailored -- shows awareness of user needs and setting |
| 5 | Perfect fit -- feels like it was made for exactly this audience |

Look for: reading level, information density vs audience expertise, platform conventions, accessibility, cultural appropriateness.

### 5. Restraint (weight: medium)

Does the design know what to leave out?

| Score | Meaning |
|-------|---------|
| 1 | Bloated -- every feature is visible, nothing is prioritized |
| 2 | Cluttered -- too many options competing for attention |
| 3 | Balanced -- reasonable feature surface |
| 4 | Disciplined -- clear priorities, secondary items recede |
| 5 | Minimal -- only the essential, nothing to remove |

Look for: feature density, progressive disclosure, empty states, whitespace usage, hidden-by-default patterns.

### 6. Emotional Response (weight: low)

Does the design evoke the intended feeling?

| Score | Meaning |
|-------|---------|
| 1 | Repellent -- actively unpleasant |
| 2 | Flat -- no emotional register |
| 3 | Neutral -- inoffensive |
| 4 | Warm -- creates mild positive engagement |
| 5 | Delightful -- memorable, evokes trust or joy |

Look for: micro-interactions, illustration style, copy tone, color warmth, motion design, personality.

## Output Template

Produce your evaluation in this format:

```markdown
# Human Taste Report

**Subject:** [what was evaluated]
**Date:** [date]
**Overall Score:** [weighted average, 1-5, one decimal] / 5

## Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Cognitive Load | X/5 | [specific observation] |
| Visual Coherence | X/5 | [specific observation] |
| Interaction Clarity | X/5 | [specific observation] |
| Context Fit | X/5 | [specific observation] |
| Restraint | X/5 | [specific observation] |
| Emotional Response | X/5 | [specific observation] |

## Strengths
- [concrete strength with evidence]
- [concrete strength with evidence]

## Issues
- **[severity: Critical/Major/Minor]**: [specific issue] -- [why it matters] -- [suggested fix]

## Verdict
[2-3 sentence summary: what works, what does not, and the single highest-impact improvement]
```

**Weighted average formula:** `(CognitiveLoad*3 + VisualCoherence*3 + InteractionClarity*3 + ContextFit*2 + Restraint*2 + EmotionalResponse*1) / 14`

## Comparing Alternatives

When comparing two or more designs:

1. Run the rubric on each independently
2. Add a **Comparison Table** showing side-by-side scores
3. Declare a winner per dimension and overall
4. Explain the tradeoffs -- a lower-scoring design may still be right for a specific audience

## Reviewing AI-Generated Designs

AI-generated UI often has specific taste failure modes:

- **Over-decoration** -- gradients, shadows, and effects without purpose
- **Generic composition** -- layouts that feel template-driven rather than content-driven
- **Inconsistent density** -- mixing spacious and cramped sections
- **Missing edge states** -- empty states, error states, loading states not considered
- **Surface polish without structural clarity** -- looks good at first glance but confusing to use

Flag these explicitly when you detect them.

## When Not to Use This Skill

- Pure backend/API design with no user-facing component
- Code review for logic correctness (use a code-review skill instead)
- Accessibility audits (this skill covers taste, not WCAG compliance -- though the two overlap)

## Additional Resources

- For full research citations and sources, see [references/research-sources.md](references/research-sources.md)
- For worked examples of the rubric in action, see [examples.md](examples.md)
