---
name: human-taste-content
description: Evaluate writing and content quality through the lens of human taste -- clarity, voice authenticity, information density, and tone fit. Use this skill whenever the user asks you to review copy, evaluate writing quality, check for AI slop, assess tone and voice, critique documentation, review microcopy, evaluate marketing text, check if content sounds human, or judge whether writing "reads well." Also use it when reviewing AI-generated text for naturalness or comparing content drafts, even if the user does not explicitly mention "human taste."
---

# Human Taste: Content

Evaluate writing and content through human taste -- the trained judgment that detects whether text is clear, authentic, appropriately dense, and tuned to its audience.

This complements the UX-focused `human-taste` and code-focused `human-taste-code` skills. For full citations see [references/research-sources.md](references/research-sources.md).

## Why This Matters

AI can generate fluent text, but fluency is not quality. Studies show AI text detectors achieve only ~58% accuracy -- meaning AI-generated content often passes surface inspection. The real differentiator is taste: the ability to detect when writing is generic, over-hedged, structurally predictable, or tonally flat, even when it is grammatically perfect.

Key insight: AI slop is not a vocabulary problem. It is a thought-structure problem -- predictable arcs, tidy conclusions, and the absence of genuine voice.

## Quick Start

When asked to evaluate content:

1. **Identify the content type** -- UI microcopy, documentation, marketing, blog post, error messages, or long-form
2. **Run the rubric** below across all six dimensions
3. **Produce a Human Taste: Content Report** using the output template
4. **Quote specific text** -- never give vague feedback like "needs more personality"

## Evaluation Rubric

Score each dimension 1-5. Anchor every score with quoted text from the content.

### 1. Clarity (weight: high)

Does every sentence earn its place and say exactly what it means?

| Score | Meaning |
|-------|---------|
| 1 | Muddled -- reader must re-read to extract meaning |
| 2 | Fuzzy -- intent is guessable but buried in filler |
| 3 | Functional -- gets the point across with some waste |
| 4 | Sharp -- each sentence has a clear job; no wasted words |
| 5 | Precise -- could not be said more clearly in fewer words |

Look for: filler phrases, throat-clearing openers, redundant qualifiers, passive voice overuse, sentence length variation.

### 2. Voice Authenticity (weight: high)

Does this sound like a specific human wrote it -- or like a language model?

| Score | Meaning |
|-------|---------|
| 1 | AI slop -- predictable structure, generic phrasing, no personality |
| 2 | Synthetic -- grammatically correct but reads like a template |
| 3 | Neutral -- neither distinctly human nor obviously AI |
| 4 | Personal -- has a recognizable voice with occasional character |
| 5 | Distinctive -- unmistakably human; you could identify the author |

Look for: AI slop markers (see checklist below), genuine opinions vs hedged statements, specific examples vs generic claims, humor or personality.

### 3. Information Density (weight: high)

Is the ratio of insight to words high?

| Score | Meaning |
|-------|---------|
| 1 | Padded -- says in a paragraph what could be said in a sentence |
| 2 | Diluted -- useful content buried in filler |
| 3 | Adequate -- reasonable density with some bloat |
| 4 | Dense -- most sentences carry new information |
| 5 | Concentrated -- every sentence teaches or moves the reader forward |

Look for: repeated ideas in different words, setup sentences that add nothing, bullet points that could be a single sentence, word-to-insight ratio.

### 4. Tone Fit (weight: medium)

Does the tone match the audience, context, and moment?

| Score | Meaning |
|-------|---------|
| 1 | Jarring -- tone is wrong for the situation (casual in crisis, formal in onboarding) |
| 2 | Off -- partially appropriate but inconsistent |
| 3 | Acceptable -- reasonable tone, no major clashes |
| 4 | Tuned -- tone adapts to context within a consistent voice |
| 5 | Perfect pitch -- tone is exactly right for this audience at this moment |

Look for: formality level vs audience, emotional calibration (error messages should be helpful not cute), consistency across touchpoints, tone spectrum (NN/g's four dimensions: formal/casual, serious/funny, respectful/irreverent, matter-of-fact/enthusiastic).

### 5. Structure (weight: medium)

Does the content architecture serve the reader's needs?

| Score | Meaning |
|-------|---------|
| 1 | Disorganized -- no clear flow; reader is lost |
| 2 | Jumbled -- some structure but information is in wrong places |
| 3 | Logical -- follows a reasonable order |
| 4 | Guided -- structure anticipates what the reader needs next |
| 5 | Effortless -- scannable, progressive, leads the reader exactly where they need to go |

Look for: heading hierarchy, progressive disclosure, scanability, front-loading of key information, logical flow between sections.

### 6. Specificity (weight: low)

Does the content use concrete details instead of abstract claims?

| Score | Meaning |
|-------|---------|
| 1 | Vague -- all abstract claims, no evidence or examples |
| 2 | Generic -- occasional examples but mostly platitudes |
| 3 | Mixed -- some concrete details, some hand-waving |
| 4 | Grounded -- claims are backed by specific examples or data |
| 5 | Vivid -- uses precise details that make abstract ideas tangible |

Look for: numbers vs "many/some/several," named examples vs generic references, specific scenarios vs broad claims, showing vs telling.

## AI Slop Checklist

Flag any of these patterns when you detect them. They individually are not proof of AI generation, but clusters of them indicate content lacks human taste.

**Word-level markers:**
- "Delve," "realm," "underscore," "meticulous," "commendable"
- "Seamless," "robust," "cutting-edge," "future-ready," "leverage"
- "Utilize" instead of "use," "implement" instead of "start/do"
- Excessive em-dashes used as all-purpose connectors

**Sentence-level patterns:**
- "In today's fast-paced world..." or "In the ever-evolving landscape of..."
- "It's not about X, it's about Y" binary contrasts
- "No X. No Y. Just Z." dramatic fragmentation
- Triple grouping: "fast, efficient, and reliable"
- Throat-clearing openers that delay the point

**Structure-level patterns:**
- Every section follows the same arc (setup, explanation, conclusion)
- Tidy paragraph structure with no variation in length
- Lists of exactly 3-5 items everywhere
- Conclusions that restate the introduction

**Voice-level markers:**
- Hedging without commitment ("It's worth considering...")
- Generic enthusiasm without specifics ("This is a game-changer!")
- Corporate verbs: "highlighting," "facilitating," "driving impact"
- No genuine opinions -- everything is balanced to the point of saying nothing

## Output Template

Produce your evaluation in this format:

```markdown
# Human Taste: Content Report

**Subject:** [what was evaluated]
**Content type:** [microcopy / docs / marketing / blog / error messages / long-form]
**Date:** [date]
**Overall Score:** [weighted average, 1-5, one decimal] / 5

## Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Clarity | X/5 | [quoted text as evidence] |
| Voice Authenticity | X/5 | [quoted text as evidence] |
| Information Density | X/5 | [quoted text as evidence] |
| Tone Fit | X/5 | [specific observation] |
| Structure | X/5 | [specific observation] |
| Specificity | X/5 | [quoted text as evidence] |

## AI Slop Flags
- [pattern detected] -- [quoted example from text]
(or "None detected" if clean)

## Strengths
- [concrete strength with quoted evidence]
- [concrete strength with quoted evidence]

## Issues
- **[severity: Critical/Major/Minor]**: [specific issue] -- [why it weakens the content] -- [rewrite suggestion]

## Verdict
[2-3 sentences: what works, what does not, and the single highest-impact edit]
```

**Weighted average formula:** `(Clarity*3 + VoiceAuthenticity*3 + InformationDensity*3 + ToneFit*2 + Structure*2 + Specificity*1) / 14`

## Comparing Content Versions

When comparing two drafts:

1. Run the rubric on each independently
2. Add a **Comparison Table** with side-by-side scores
3. Quote the strongest and weakest passage from each
4. Recommend which version to use as the starting point and what to borrow from the other

## Content-Type-Specific Guidance

**UI Microcopy:** Brevity is paramount. Score Clarity and Tone Fit highest. Error messages should be helpful, specific, and action-oriented -- never blame the user.

**Documentation:** Information Density and Structure matter most. Score against whether a reader can find and use the information without reading the entire page.

**Marketing copy:** Voice Authenticity and Specificity are critical. Generic claims ("world-class solution") score low. Concrete benefits ("saves 4 hours per week on invoice processing") score high.

**Blog / long-form:** All dimensions matter equally. Pay extra attention to the AI Slop Checklist -- long-form AI content tends to accumulate structural predictability.

## When Not to Use This Skill

- Code review (use `human-taste-code`)
- Visual/UX design evaluation (use `human-taste`)
- Grammar/spelling checks (those are automated)
- Translation quality (requires source language comparison)

## Additional Resources

- For full research citations and sources, see [references/research-sources.md](references/research-sources.md)
- For worked examples of the rubric in action, see [examples.md](examples.md)
