---
name: golden-visual-qa
description: Render and fail-closed review compiled client-facing visuals before delivery. Use for PDF, HTML, email HTML, newsletter, social-frame image, or slide-deck artifacts when an agent is about to show, send, post, publish, or hand the artifact to Deacon or a client.
---

# Golden visual QA

Treat a successful compile as the start of closeout, not a visual pass. Never deliver a compiled visual without a current `PASS` report from `golden-visual-qa` bound to the exact artifact, previews, and brand context.

## Closeout

1. Load the live client brand/context file. If none exists, create a bounded artifact brief and use that file as `--brand-context`.
2. Render the actual compiled bytes at their delivery sizes:
   - PDF: one PNG/JPEG preview per page.
   - Deck: one preview per slide.
   - Email or newsletter: 680 px desktop and 390 px mobile previews.
   - Social frame: the final image itself; default lock is 1080×1080 unless the brief names another size.
   - General HTML: at least one intended viewport.
3. Run the gate once with `--write-review-template`. An `UNKNOWN` verdict is expected until visual review is supplied; structure or render failures must be fixed first.
4. Inspect every preview. Apply only Impeccable's `reference/craft-floor.md` checks plus the `human-taste` rubric. Do not start an Impeccable live editing session. Do not use `design-taste-frontend` or `gpt-taste` for email or newsletter work.
5. Fill the bound review receipt using [references/review-contract.md](references/review-contract.md). Cite visible evidence for every score and check. Include the client overlay/brand comparison.
6. Run the gate again. Deliver only when `verdict` is `PASS` and the command exits zero.

```bash
golden-visual-qa ARTIFACT \
  --kind email-html \
  --brand-context CLIENT-BRAND.md \
  --preview email-680.png \
  --preview email-390.png \
  --review-receipt email.visual-review.json
```

The JSON report defaults beside the artifact as `ARTIFACT.visual-qa.json` and always contains `structure`, `render`, `visual_review`, and `verdict`.

## Repair ceiling

Fix findings in one batch, recompile, rerender, and rerun. Stop after two repair rounds. A remaining failure stays blocked and is shown truthfully; do not soften it to a pass or silently start a third loop.

## Channel rules

- Email/newsletter: table layout, durable HTTPS images, no data URLs or local/tunnel hosts, unsubscribe placeholder present, desktop and mobile previews present.
- Social: honor the brief's size; otherwise require 1080×1080.
- PDF/deck: preview every page/slide, not only the first.
- All channels: unknown evidence, stale review, mismatched hashes, missing brand context, major findings, and low taste scores block delivery.
