---
name: creative-output-escalation
description: Escalate visual, image, animation, design, and brand-output requests to approved high-quality tools and rich prompts instead of low-effort composites.
triggers:
  - image generation
  - make a visual
  - high resolution
  - higher resolution
  - HD image
  - upscale image
  - restore image
  - enhance image
  - sharpen image
  - remove pixelation
  - animate it
  - poster
  - flyer
  - logo
  - wordmark
  - vector
  - character visual
  - profile picture
  - brand creative
---

# Creative Output Escalation

Use this for creative output where quality matters: visuals, image generation, animation, poster/flyer concepts, branded graphics, profile-character art, and client-visible design.

## Rule

Do not phone it in with a local placeholder or crude composite when better image/video/design tools are available.

## Required workflow

1. Classify the requested operation using
   `shared-rules/image-generation-routing.md`:
   - geometry-only raster work,
   - restoration/detail recovery,
   - exact logo/wordmark/vector reconstruction, or
   - new/transformed creative artwork.
   Never answer a restoration or vector request with a larger Pillow canvas.

2. Identify source assets:
   - profile photo / logo / brand guide / existing design / product image.
   - preserve identity-critical elements unless user asks for a redesign.

3. Determine target output:
   - static image, animation/video, PDF/poster, web/design mockup, or prompt package.

4. Scout tools/providers:
   - image generation and editing: follow `shared-rules/image-generation-routing.md` for provider selection and the QA/escalation ladder.
   - exact logos/wordmarks: locate an approved vector master or reconstruct
     editable SVG before raster export.
   - video creation, editing, animation, captioning, and rendering: load the installed `hyperframes` skill first and follow `shared-rules/video-production-routing.md`.
   - approved generative-video providers: use only for source shots or plates needed by the HyperFrames composition.
   - deterministic/local compositor only for geometry-only work, exact layout,
     captions, print prep, or final export from an adequate/vector source.

5. Write a rich prompt when generation/editing is the selected lane:
   - subject identity and reference use
   - scene/action/composition
   - lighting/style/quality bar
   - exact negative constraints
   - output format/aspect ratio

6. Generate or reconstruct, then run the effective-resolution proof from the
   shared rule. Pixel dimensions, centering, and file existence are not enough.

7. Deliver the best artifact with a short note. Use `[[as_document]]` for
   lossless Telegram image masters. If animation was requested or obvious,
   produce video/GIF when supported.

## Quality bar

Client/family creative should feel like a polished concept-art or production asset, not clip art or a third-grade collage.

## Safety / approvals

Do not use private client/person images outside approved providers/workflows. Do not publish or send externally beyond the originating chat without approval. Track costs for premium video/image generation.

## Standard creative path

For any client-visible creative artifact, follow `creative-output-standard-path.md` semantics even when that shared rule is not loaded directly.

### Image routing

`shared-rules/image-generation-routing.md` is authoritative for native image
generation, reference editing, auxiliary escalation, and publishable QA.

### Video routing

`shared-rules/video-production-routing.md` is authoritative for video workflow
selection and production QA. HyperFrames owns composition, motion, captions,
layout, snapshots, render, and inspection by default. Do not silently substitute
a crude Pillow/FFmpeg frame generator when that route is available.

### Brand prep is not enough

Brand, voice, and scraped marketing materials are necessary source inputs, but they are not the same as approved examples. Approved examples and rejected examples are the taste-calibration layer. If they are missing, say so before producing and label the result honestly.

### Confidence gate

Before producing flyers, social graphics, video concepts, ads, posters, one-pagers, pitch PDFs, or other polished marketing collateral, classify confidence:

- **High confidence:** close approved example/template exists, clean assets exist, audience/CTA are clear.
- **Medium confidence:** brand/template exists, but approved examples or production photos are weak/missing.
- **Low confidence:** no close example, weak/missing assets, unclear audience/CTA, or prior drafts were rejected.

Do not call medium/low-confidence creative `final`, `polished`, `super clean`, or `production-ready`. Use `best-shot review draft` or `layout draft only`.

### Proactive intake

When confidence is medium/low, ask for or name the missing inputs:

- 2-3 examples the operator likes,
- 1-2 examples they dislike,
- clean logo/photo/product assets,
- channel/spec/aspect ratio,
- the single CTA,
- any prior accepted/rejected draft.

Offer two paths: make a best-shot review draft now, or wait for examples/assets to get closer on the first pass.

### Production surface selection

Do not assume the chat model should invent layout from scratch. Prefer, in order:

1. Approved client template or swipe-file layout.
2. Canva/Figma/Adobe/template-library workflow when available for polished marketing collateral.
3. Deterministic HTML/CSS/PDF only when brand system, template, assets, and visual QA are strong.
4. Image-generation/editing tools for generative visuals and reference work, with exact text rendered separately.

### Variant and QA rule

For important creative, produce or propose 2-3 directions before declaring a winner. Render/inspect the actual preview or delegate visual QA to a vision-capable worker/tool. If visual QA is blocked, hold delivery or label draft-only.

### Feedback capture

After delivery, ask for targeted feedback buckets: too generic, too busy, wrong tone, weak imagery, wrong CTA, not premium enough. Save accepted/rejected preferences and examples in the client brain so the operator does not repeat themselves.
