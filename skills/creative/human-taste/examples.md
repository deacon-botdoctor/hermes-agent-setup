# Human Taste Examples

Worked examples showing the rubric applied to realistic scenarios.

---

## Example 1: E-Commerce Checkout Page

**User prompt:** "Review the checkout flow on this page -- does it feel right?"

### Human Taste Report

**Subject:** E-commerce checkout page (single-page layout with shipping, payment, and order summary)
**Date:** 2026-02-25
**Overall Score:** 3.4 / 5

#### Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Cognitive Load | 3/5 | All fields visible at once without collapsible sections; 14 form fields on screen simultaneously |
| Visual Coherence | 4/5 | Consistent typography and spacing; color palette is disciplined (blue primary + neutral grays) |
| Interaction Clarity | 3/5 | "Place Order" button is clear, but error messages appear only after submission, not inline |
| Context Fit | 4/5 | Mobile-responsive layout appropriate for general consumer audience |
| Restraint | 3/5 | Promotional banner and newsletter signup compete with the checkout task |
| Emotional Response | 3/5 | Functional but impersonal; no trust signals near payment fields |

#### Strengths
- Consistent visual system with clear typographic hierarchy between section headers and field labels
- Mobile layout adapts well, stacking sections vertically without horizontal scroll

#### Issues
- **Major**: Inline validation missing -- users only see errors after clicking "Place Order," causing frustration -- add real-time field validation on blur
- **Major**: Promotional banner at top of checkout competes with the primary task -- remove or move below the fold
- **Minor**: No trust indicators (lock icon, security badge) near credit card fields -- add subtle trust signals beside the payment section

#### Verdict
Solid visual system undermined by interaction friction. The highest-impact fix is adding inline validation -- it directly reduces the error-correction loop that causes checkout abandonment.

---

## Example 2: Dashboard for Internal Analytics Tool

**User prompt:** "Our team built this analytics dashboard. Can you taste-test it?"

### Human Taste Report

**Subject:** Internal analytics dashboard with 6 chart panels, date filter, and sidebar navigation
**Date:** 2026-02-25
**Overall Score:** 2.6 / 5

#### Scores

| Dimension | Score | Key Evidence |
|-----------|-------|-------------|
| Cognitive Load | 2/5 | Six charts visible simultaneously with no visual hierarchy to guide the eye; three use different color schemes |
| Visual Coherence | 2/5 | Mixed chart libraries produce inconsistent tooltip styles, axis formatting, and font sizes |
| Interaction Clarity | 3/5 | Date filter works but chart interactions (hover, click-through) are inconsistent across panels |
| Context Fit | 3/5 | Appropriate data density for analyst audience, but lacks export and drill-down that analysts expect |
| Restraint | 2/5 | Every metric gets equal visual weight; no distinction between KPIs and supporting data |
| Emotional Response | 2/5 | Feels like a prototype -- gray background, no clear branding, no loading states |

#### Strengths
- Data is accurate and updates in real time
- Sidebar navigation groups sections logically (Overview, Sales, Users, Settings)

#### Issues
- **Critical**: Three different chart libraries produce inconsistent visual language -- standardize on one library and one color palette
- **Major**: All six panels compete for attention equally -- promote 2-3 KPI cards to the top with larger type, push detail charts below
- **Major**: No empty or loading states -- when data loads slowly, users see blank panels with no feedback -- add skeleton loaders
- **Minor**: Tooltip formatting differs between charts (some show percentages, some raw numbers for the same metric) -- normalize tooltip format

#### Verdict
The data is there but the presentation actively works against comprehension. Standardizing the chart library and creating a clear KPI-first hierarchy would transform this from a wall of charts into an actionable dashboard.

---

## Example 3: Comparing Two Landing Page Variants

**User prompt:** "We have two landing page options for our SaaS product. Which one has better taste?"

### Comparison Table

| Dimension | Variant A | Variant B |
|-----------|-----------|-----------|
| Cognitive Load | 4/5 | 2/5 |
| Visual Coherence | 4/5 | 3/5 |
| Interaction Clarity | 3/5 | 4/5 |
| Context Fit | 4/5 | 3/5 |
| Restraint | 5/5 | 2/5 |
| Emotional Response | 3/5 | 4/5 |
| **Overall** | **3.9** | **2.9** |

**Winner: Variant A** (3.9 vs 2.9)

**Tradeoff:** Variant B has a stronger emotional punch -- the hero animation and bold imagery create more excitement. But it packs too much into the viewport: testimonials, feature grid, pricing teaser, and a video all above the fold. Variant A's restraint (hero + single CTA + one supporting line) gives the eye a clear path and converts the initial impression into action more effectively.

**Recommendation:** Start with Variant A's structure but borrow Variant B's hero illustration style to lift the emotional score without sacrificing clarity.

---

## Example 4: Flagging AI-Generated Design Issues

**User prompt:** "I generated this settings page with an AI tool. Does it pass the human taste test?"

### Key Findings

The AI-generated settings page exhibits three common AI taste failures:

1. **Over-decoration**: Every card has a drop shadow, rounded corners, AND a gradient header. The cumulative effect is visual noise. Fix: pick one elevation style and apply consistently.

2. **Generic composition**: The three-column grid looks like a template. Every card is the same height regardless of content, creating awkward whitespace in cards with fewer options. Fix: let cards size to content; use a masonry or single-column layout.

3. **Missing edge states**: Toggle switches have on/off states but no disabled state, no loading state for toggles that trigger server calls, and no confirmation for destructive actions like "Delete Account." Fix: design the unhappy paths, not just the defaults.

**Overall Score: 2.7 / 5** -- Visually polished on the surface but structurally unconsidered. The highest-impact improvement is stripping the decorative layers and designing for content-first layout.
