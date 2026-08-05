# Human Taste: Content Examples

Worked examples showing the content taste rubric applied to realistic scenarios.

---

## Example 1: SaaS Marketing Landing Page Copy

**User prompt:** "Review the copy on our landing page -- it feels generic."

### Human Taste: Content Report

**Subject:** Landing page hero section + feature blocks for a project management SaaS
**Content type:** Marketing
**Date:** 2026-02-25
**Overall Score:** 2.4 / 5

#### Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Clarity | 3/5 | Sentences are grammatically clean but "Empower your team to achieve more with seamless collaboration" says nothing specific |
| Voice Authenticity | 1/5 | Every sentence could appear on any SaaS landing page; zero personality or point of view |
| Information Density | 2/5 | Hero section uses 42 words to say "project management tool for teams" -- the rest is filler |
| Tone Fit | 3/5 | Professional tone is appropriate for B2B but feels corporate-by-numbers |
| Structure | 3/5 | Standard hero > features > CTA flow works but is predictable |
| Specificity | 2/5 | "Save hours every week" -- how many? Compared to what? No concrete claim |

#### AI Slop Flags
- "seamless collaboration" -- generic descriptor with no concrete meaning
- "In today's fast-paced business environment" -- classic throat-clearing opener
- "Empower your team to achieve more" -- triple AI slop: vague verb + vague object + vague promise
- "cutting-edge" and "robust" used within two paragraphs of each other

#### Strengths
- CTA button text ("Start Free Trial") is clear and action-oriented
- Feature section uses consistent heading structure

#### Issues
- **Critical**: Zero differentiation -- this copy could describe any project management tool -- rewrite with specific claims: "Teams using [Product] close projects 3 days faster on average"
- **Major**: Voice is absent -- the copy has no opinion about what's wrong with existing tools or why this approach is different -- add a point of view in the hero
- **Minor**: "Learn more" links under features are vague -- replace with specific actions: "See how kanban boards work"

#### Verdict
Grammatically flawless but strategically empty. The copy describes a category, not a product. The single highest-impact edit is replacing the hero with one specific, defensible claim about what makes this tool different.

---

## Example 2: Error Messages in a Banking App

**User prompt:** "Our users complain that error messages are confusing. Can you taste-test them?"

### Human Taste: Content Report

**Subject:** 8 error messages from a mobile banking application
**Content type:** Microcopy (error states)
**Date:** 2026-02-25
**Overall Score:** 2.1 / 5

#### Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Clarity | 2/5 | "Transaction could not be processed at this time" -- what should the user do? |
| Voice Authenticity | 3/5 | Not AI-sounding, but sterile and impersonal |
| Information Density | 2/5 | Messages explain what happened but never what to do next |
| Tone Fit | 1/5 | "Error 4012: Invalid request parameters" shown to retail banking customers -- technical jargon in a consumer app |
| Structure | 2/5 | Error messages are single sentences with no recovery action |
| Specificity | 2/5 | "Something went wrong" appears three times for different failure modes |

#### AI Slop Flags
- None detected -- these are human-written but poorly crafted

#### Strengths
- Messages are short (under 15 words each)
- No blame language ("you entered incorrectly" etc.)

#### Issues
- **Critical**: "Error 4012: Invalid request parameters" shown to end users -- replace with "We couldn't process this transfer. Please check the account number and try again."
- **Critical**: No recovery actions in any message -- every error should tell the user what to do next
- **Major**: "Something went wrong" used for insufficient funds, network timeout, AND session expiry -- each needs its own message with specific guidance
- **Minor**: Tone is cold and corporate for a product that handles people's money (anxiety context) -- add reassurance: "Your money is safe. The transfer didn't go through."

#### Verdict
Error messages are functional but hostile to the user experience. In a banking context, unclear errors create anxiety. The highest-impact fix is adding a recovery action to every error message: what happened + what to do + reassurance that their money is safe.

---

## Example 3: Comparing Two README Versions

**User prompt:** "We rewrote our project README. Is the new version better?"

### Comparison Table

| Dimension | Old README | New README |
|-----------|-----------|-----------|
| Clarity | 2/5 | 4/5 |
| Voice Authenticity | 3/5 | 4/5 |
| Information Density | 2/5 | 4/5 |
| Tone Fit | 3/5 | 4/5 |
| Structure | 2/5 | 5/5 |
| Specificity | 3/5 | 4/5 |
| **Overall** | **2.4** | **4.1** |

**Winner: New README** (4.1 vs 2.4)

**What improved:** The old README opened with three paragraphs of project philosophy before showing how to install. The new version leads with a one-line description, install command, and minimal example -- you can start using the library in 30 seconds. Philosophy moved to a "Why" section below the fold.

**What the new version does well:** The quickstart example is real, runnable code (not pseudo-code). Error messages in examples show actual error output. The API reference uses a consistent format: signature, one-sentence description, example, return type.

**One thing to borrow from the old version:** The old README had a "Common mistakes" section that the new one dropped. Bring it back -- it was the most useful section for onboarding.

---

## Example 4: AI-Generated Blog Post

**User prompt:** "I used ChatGPT to draft a blog post about our API migration. Does it pass the human taste test?"

### Key Findings

The 800-word blog post scores **1.9 / 5** and exhibits heavy AI slop:

1. **Structural predictability**: Every section follows setup-explanation-conclusion. All five sections are within 10 words of each other in length. Real writing has rhythm -- some sections are a single sentence, others are deep dives.

2. **Hedging epidemic**: "It's worth noting that," "One might argue that," "It's important to consider" -- the post has 7 hedging phrases in 800 words. The author (your engineering team) has opinions. State them directly: "We chose GraphQL over REST because..."

3. **Generic specificity**: "The migration resulted in significant improvements" -- what improvements? 40% faster queries? 3 fewer services to maintain? The team did this work and has real numbers. Use them.

4. **Absent voice**: Nothing in this post reveals that an actual team made hard tradeoffs. Where did they disagree? What surprised them? What would they do differently? These details are what make engineering blog posts worth reading.

**Recommendation:** Keep the structure as an outline but rewrite every section with concrete details from the actual migration. Interview the engineer who led it and use their words. A good engineering blog post reads like a specific war story, not a Wikipedia article.
