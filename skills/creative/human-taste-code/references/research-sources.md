# Research Sources: Human Taste in Code & Software Design

Curated references backing the human-taste-code skill.

---

## Academic Research

| # | Title | Quality | Key Takeaway | Link |
|---|-------|---------|--------------|------|
| 1 | Investigating The Smells of LLM Generated Code | Peer-reviewed (arXiv 2510.03029) | LLM-generated code shows 42-85% more code smells than human-written code across four models. Implementation smells account for 73% of the increase. | [arxiv.org/abs/2510.03029](https://arxiv.org/html/2510.03029v1) |
| 2 | Comparing Robustness Against Adversarial Attacks: LLM vs Human | Pre-print | Human-written code trains more resilient models in 75% of experimental combinations. | [scispace.com](https://scispace.com/papers/comparing-robustness-against-adversarial-attacks-in-code-396sti53niy8) |
| 3 | Vibe Checker: Aligning Code Evaluation with Human Preference | arXiv 2510.07315 | Instruction following (readability, elegance, intent preservation) is the primary differentiator in human preference for code, beyond functional correctness. | [arxiv.org/abs/2510.07315](https://arxiv.org/abs/2510.07315) |
| 4 | CodeQUEST: Code Quality Evaluation | arXiv 2502.07399 | Evaluates code across ten dimensions including readability, maintainability, efficiency. Achieves 52.6% mean relative improvement through iterative optimization. | [arxiv.org/abs/2502.07399](https://arxiv.org/abs/2502.07399) |
| 5 | Towards Comprehensive Assessment of Code Quality (CS1-Level) | IEEE 2024 | Structured rubric combining tools, rubrics, and refactoring rules for systematic code quality assessment. | [ieeexplore.ieee.org](https://ieeexplore.ieee.org/document/10578672/) |

---

## Books & Foundational Texts

| # | Author | Title | Key Idea |
|---|--------|-------|----------|
| 6 | John Ousterhout | A Philosophy of Software Design (2nd ed.) | Deep modules (simple interface, rich functionality) are the mark of taste. Over-specialization is the single greatest cause of complexity. General-purpose APIs are deeper than special-purpose ones. |
| 7 | Fred Brooks | The Mythical Man-Month (Ch. "Conceptual Integrity") | A system must reflect one coherent design vision. Conceptual integrity is the most important consideration in system design. |
| 8 | Martin Fowler | Refactoring (2nd ed.) | Code smells are heuristics for structural problems. Refactoring is the discipline of improving design without changing behavior. |
| 9 | David Parnas | On the Criteria To Be Used in Decomposing Systems into Modules (1972) | Information hiding as the foundation of modular design. Each module should encapsulate a design decision. |

---

## Practitioner Essays & Talks

| # | Author | Title | Key Idea | Link |
|---|--------|-------|----------|------|
| 10 | Rich Hickey | Simple Made Easy (Strange Loop 2011) | Simple = untangled, one concern. Easy = familiar. Good taste chooses simplicity over convenience. | [infoq.com/presentations/Simple-Made-Easy](https://www.infoq.com/presentations/Simple-Made-Easy) |
| 11 | Paul Graham | Taste for Makers (2002) | Good design = simplicity + clarity. Taste is learned by exposure. | [paulgraham.com/taste.html](https://paulgraham.com/taste.html) |
| 12 | Joe Duffy | The Role of Software Architects (2008) | Taste determines what to leave out. Conceptual integrity across a large system requires taste. | [joeduffyblog.com](https://joeduffyblog.com/2008/10/02/a-few-thoughts-on-the-role-of-software-architects/) |
| 13 | Sean Goedecke | Taste in software engineering | Developers with taste intuitively avoid anti-patterns, prioritizing context-fit values like readability. | [seangoedecke.com](https://seangoedecke.com) |
| 14 | Pavel Panchekha | Taste as value selection | Good taste means selecting the right engineering values (speed vs resiliency) for the specific project context. | [pavpanchekha.com](https://pavpanchekha.com) |
| 15 | Microsoft Engineering Playbook | Code Review Guidance | Effective reviews focus on architectural correctness, readability, maintainability, and design -- not style. | [microsoft.github.io](https://microsoft.github.io/code-with-engineering-playbook/code-reviews/process-guidance/reviewer-guidance/) |

---

## Cross-Domain Foundations

The six pillars of taste from cognitive psychology apply directly to code:

1. **Pattern recognition** -- experts instantly sense wrong abstractions
2. **Cognitive load sensitivity** -- taste detects unnecessary complexity
3. **Conceptual unity** -- coherent systems over feature patchworks
4. **Long-term thinking** -- anticipating change cost, not just current correctness
5. **Cultural calibration** -- idiomatic code for the language and team
6. **Emotional intuition** -- the "something feels off" signal that precedes conscious analysis
