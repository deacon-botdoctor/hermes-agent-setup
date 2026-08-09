# Image Generation Routing

## Default

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
