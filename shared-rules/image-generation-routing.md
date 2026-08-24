# Image Generation Routing

## Classify the operation before choosing a tool

Image work has four different lanes. Do not collapse them into "make the file
bigger."

1. **Geometry-only raster work** — crop, pad, rotate, format conversion,
   compositing, deterministic captions, or exporting an already adequate
   source. Pillow, ImageMagick, canvas, and deterministic renderers are valid.
   They do not create source detail and must not be described as restoration,
   sharpening, upscaling, high definition, or higher resolution.
2. **Restoration or detail recovery** — requests such as "higher resolution,"
   "HD," "upscale," "restore," "enhance," "sharpen," "deblur," or "remove
   pixelation." Use the native reference-capable image tool or an approved
   specialized restoration/upscale lane. Resizing onto a larger canvas is not
   a fallback for this lane.
3. **Exact logos, wordmarks, icons, and brand marks** — first locate the
   approved SVG, PDF, EPS, AI, or other vector master. If none exists, faithfully
   reconstruct clean editable SVG/vector geometry and verify the lettering.
   Generative editing is appropriate only for an explicitly requested redesign
   or for a review draft whose deviations are clearly labeled; it is not the
   source of truth for exact brand geometry or text.
4. **New or transformed creative artwork** — use Hermes' native
   `image_generate` tool with the Codex subscription-backed provider first.

If the required lane is unavailable, say which capability is missing and use
the approved escalation route. Do not silently substitute a lower-capability
local operation and claim the requested outcome.

## Native creative and restoration route

Image generation and reference editing use Hermes' native `image_generate`
tool with the Codex subscription-backed provider first:

```yaml
image_gen:
  provider: openai-codex
  model: gpt-image-2-high
```

Agents should not ask which image model to use for routine creative prompts.
Route ordinary images, posters, concepts, moodboards, illustrative graphics,
and source/reference edits to the native tool first. The native Codex provider
accepts source and reference images; do not substitute Pillow, canvas, or a
local compositor for generative editing merely because a reference is present.

## Effective-resolution proof

Before describing an output as high-resolution, HD, restored, enhanced, sharp,
crisp, or production-ready:

1. Record the source format and pixel dimensions, plus whether a vector master
   exists.
2. State which operation created new usable detail or replaced raster geometry
   with vector geometry. A larger width/height alone is not evidence.
3. Inspect representative edges and small lettering at 200–400% zoom against
   the source. Reject block repetition, stair-stepping, ringing, smeared text,
   hallucinated strokes, and changed logo geometry.
4. For exact text or wordmarks, run visual/OCR comparison and retain an editable
   source when reconstruction was required.

If only the canvas dimensions changed, label the result `enlarged derivative —
no new source detail`; never call it high-resolution.

## Escalation triggers

Use a paid or specialized auxiliary lane only when the native attempt is
unavailable, fails visual QA, or the task needs a capability it does not cover:

- the user asks for premium quality, best quality, a quality push, model comparison, or publication polish;
- Codex output fails QA for text, facts, composition, artifacts, or style fit;
- the user asks for variants, options, side-by-side comparisons, or multiple visual directions;
- the request is production finalization for a public, client-visible, or brand-sensitive asset.

Auxiliary lanes remain available for those escalation cases. A local
compositor is appropriate for deterministic text/layout finishing, not as a
silent replacement for an available generative image/editing tool.

## Production standard

For publishable assets, no image model is the final authority on exact text or facts:

1. Generate or edit the visual with native Codex high unless that attempt is unavailable or fails QA.
2. Rebuild exact words, dates, addresses, URLs, logos, and captions as deterministic/editable layers when precision matters.
3. Run OCR or visual QA before calling the asset final.
4. Escalate to a paid or specialized lane when QA fails or final polish requires it.
5. Deliver lossless masters through the platform's document/file route. On
   Telegram, prefix the final response with `[[as_document]]` before the
   `MEDIA:<absolute-path>` attachment so Telegram does not recompress the image
   as a photo.
