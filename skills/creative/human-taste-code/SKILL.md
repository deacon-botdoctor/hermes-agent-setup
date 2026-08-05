---
name: human-taste-code
description: Evaluate code and software design quality through the lens of human taste -- abstraction depth, conceptual integrity, change cost, and structural elegance. Use this skill whenever the user asks you to review code quality, critique software architecture, assess whether code is well-designed, check for code smells, evaluate abstraction choices, judge whether code "feels right," or review AI-generated code for design quality. Also use it when comparing implementation approaches or assessing technical debt, even if the user does not explicitly mention "human taste."
---

# Human Taste: Code

Evaluate code and software design through human taste -- the trained judgment that detects whether abstractions are right-sized, complexity is managed, and the system will be cheap to change.

This complements the UX-focused `human-taste` skill. For full research citations see [references/research-sources.md](references/research-sources.md).

## Why This Matters

LLM-generated code is functional but measurably lower in design quality. Studies show 42-85% more code smells in AI-generated code compared to human-written code. Human taste for maintainability, abstraction quality, and structural elegance is what separates code that works from code that lasts.

Key insight: taste in code is not aesthetic preference -- it is the ability to anticipate future change cost and act on that foresight now.

## Quick Start

When asked to evaluate code:

1. **Identify scope** -- single function, module, class hierarchy, or system architecture
2. **Run the rubric** below across all six dimensions
3. **Produce a Human Taste: Code Report** using the output template
4. **Cite specific code** -- reference actual lines, names, and patterns

## Evaluation Rubric

Score each dimension 1-5. Anchor every score with concrete evidence from the code.

### 1. Abstraction Depth (weight: high)

Are modules deep -- simple interface, rich functionality hidden behind it?

| Score | Meaning |
|-------|---------|
| 1 | Shallow -- classes/functions expose implementation details; interface is as complex as internals |
| 2 | Leaky -- abstractions exist but callers need to know how things work inside |
| 3 | Adequate -- most modules hide internals, some leak |
| 4 | Deep -- simple interfaces hide substantial complexity (Unix file I/O pattern) |
| 5 | Elegant -- abstractions feel inevitable; you cannot imagine a simpler interface |

Look for: interface-to-implementation ratio, information hiding, whether callers need internal knowledge, general-purpose vs over-specialized APIs.

### 2. Conceptual Integrity (weight: high)

Does the codebase feel like one mind designed it?

| Score | Meaning |
|-------|---------|
| 1 | Fragmented -- multiple conflicting patterns, naming conventions, and styles |
| 2 | Inconsistent -- some unified areas but noticeable clashes |
| 3 | Mostly consistent -- follows conventions with occasional drift |
| 4 | Cohesive -- one clear style, one approach to common problems |
| 5 | Unified -- every part reinforces the same design philosophy |

Look for: naming consistency, error handling patterns, data flow conventions, one way to do common things vs many.

### 3. Change Cost (weight: high)

How expensive will it be to modify this code in six months?

| Score | Meaning |
|-------|---------|
| 1 | Brittle -- any change risks cascading failures; high coupling everywhere |
| 2 | Rigid -- changes require touching many files; dependencies are tangled |
| 3 | Manageable -- most changes are localized but some require careful coordination |
| 4 | Flexible -- clear boundaries; changes stay contained in their module |
| 5 | Supple -- designed for change; new requirements slot in naturally |

Look for: coupling between modules, dependency direction, use of interfaces/protocols, feature toggles, test coverage of boundaries.

### 4. Simplicity (weight: medium)

Is the code as simple as the problem allows -- and no simpler?

| Score | Meaning |
|-------|---------|
| 1 | Over-engineered -- abstractions for hypothetical futures; patterns for pattern's sake |
| 2 | Complex -- more indirection than the problem demands |
| 3 | Balanced -- complexity matches problem complexity |
| 4 | Clean -- direct solutions; easy to trace logic flow |
| 5 | Minimal -- nothing to remove; every line earns its place |

Look for: premature abstraction, unused generality, configuration surface area, inheritance depth vs composition, "astronaut architecture."

### 5. Readability (weight: medium)

Can a new team member understand this code without an oral tradition?

| Score | Meaning |
|-------|---------|
| 1 | Opaque -- requires significant effort to understand basic flow |
| 2 | Dense -- understandable with effort but easy to misread |
| 3 | Clear -- straightforward logic, reasonable naming |
| 4 | Transparent -- intent is obvious; naming tells the story |
| 5 | Self-documenting -- reads like well-written prose; no surprises |

Look for: naming precision, function length, nesting depth, comment quality (explains why, not what), consistent formatting.

### 6. Robustness (weight: low)

Does the code handle the real world -- not just the happy path?

| Score | Meaning |
|-------|---------|
| 1 | Fragile -- crashes on unexpected input; no error handling |
| 2 | Weak -- some error handling but inconsistent; edge cases ignored |
| 3 | Adequate -- common errors handled; some gaps |
| 4 | Solid -- errors handled consistently; graceful degradation |
| 5 | Resilient -- anticipates failure; recovers cleanly; observable |

Look for: input validation, error propagation strategy, timeout handling, null/undefined safety, logging, retry logic.

## Output Template

Produce your evaluation in this format:

```markdown
# Human Taste: Code Report

**Subject:** [what was evaluated -- file, module, system]
**Language:** [primary language]
**Date:** [date]
**Overall Score:** [weighted average, 1-5, one decimal] / 5

## Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Abstraction Depth | X/5 | [specific observation with code reference] |
| Conceptual Integrity | X/5 | [specific observation] |
| Change Cost | X/5 | [specific observation] |
| Simplicity | X/5 | [specific observation] |
| Readability | X/5 | [specific observation] |
| Robustness | X/5 | [specific observation] |

## Strengths
- [concrete strength citing specific code]
- [concrete strength citing specific code]

## Issues
- **[severity: Critical/Major/Minor]**: [specific issue] -- [why it harms long-term quality] -- [suggested refactor]

## Verdict
[2-3 sentences: what works, what does not, and the single highest-impact refactor]
```

**Weighted average formula:** `(AbstractionDepth*3 + ConceptualIntegrity*3 + ChangeCost*3 + Simplicity*2 + Readability*2 + Robustness*1) / 14`

## Comparing Implementations

When comparing two approaches:

1. Run the rubric on each independently
2. Add a **Comparison Table** with side-by-side scores
3. Identify which approach wins on change cost specifically -- this is usually the deciding factor
4. Note tradeoffs honestly -- sometimes the "uglier" code is the right choice for the constraint

## Reviewing AI-Generated Code

AI-generated code has specific taste failure modes:

- **Shallow modules** -- many small functions/classes that just pass data through without hiding complexity
- **Over-abstraction** -- interface + abstract class + factory + builder for a problem that needs one function
- **Inconsistent error handling** -- some functions throw, some return nulls, some use result types in the same codebase
- **Copy-paste variation** -- similar but slightly different implementations of the same pattern
- **Missing edge cases** -- happy path works perfectly; error paths are afterthoughts
- **Naming theater** -- verbose names that sound precise but don't help (`AbstractSingletonProxyFactoryBean`)

Flag these explicitly when you detect them.

## When Not to Use This Skill

- UX/visual design evaluation (use the `human-taste` skill instead)
- Writing/content quality (use the `human-taste-content` skill)
- Pure performance optimization (taste is about design, not benchmarks)
- Style-only reviews (formatting, linting -- those are automated)

## Additional Resources

- For full research citations and sources, see [references/research-sources.md](references/research-sources.md)
- For worked examples of the rubric in action, see [examples.md](examples.md)
