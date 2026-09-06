---
name: visual-reference-library
description: Load client-neutral visual patterns, production lanes, required inputs, and QA gates before generating visual artifacts. Use when a client wants more visual ideas or design directions.
version: 1.2.0
platforms:
- linux
- macos
- windows
metadata:
  hermes:
    tags:
    - visual
    - design
    - qa
    - reference
---

# Visual Reference Library

Use this skill before generating visual artifacts: reports, PDFs, social frames, diagrams, landing sections, UI comps, icons, or logo/vector concepts.

## Purpose
Retrieve client-neutral visual patterns, production lanes, required inputs, and QA gates so artifacts are produced from reusable design knowledge instead of generic prompt slop.

## Boundary rules
- Use this shared library only for generic/golden-safe patterns and rules.
- Load the current client's local overlay separately when one exists.
- Never use another client's overlay, examples, screenshots, or private assets.
- If approved client brand assets are missing, label the result: `Layout draft only — not production-ready`.
- Agency-only/private visual research must not be copied into client runtimes.

## Default workflow
1. Identify artifact type, channel, client, and style goal.
2. If the ask needs originality, direction-setting, more ideas, or feels generic, run a creative-ideation pass before production: generate 3-5 distinct concept directions using named creative/design methodologies, then choose the best feasible direction. When the client wants more visual ideas, pull 1-3 styles from the Refero DESIGN.md library (VDR-011) via the `refero-styles` MCP or `${HERMES_HOME:-$HOME/.hermes}/bin/refero-styles`. For UI/motion/component ideas, use the public component stack (VDR-012). Extract tokens and do/don't. Do not clone Linear, Notion, Lamborghini, Cue, or Aceternity as the client brand, do not drop a third-party DESIGN.md into the client repo, and do not npm-install a random component library onto a client host. Client overlay still wins. See `references/refero-design-md.md` and `references/ui-component-libraries.md`.
3. Run the installed `visual-reference-lookup` CLI with artifact type, channel, and style goal. On POSIX shells use `${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup`; on Windows PowerShell use `python (Join-Path $env:HERMES_HOME 'bin\visual-reference-lookup')`, or replace `$env:HERMES_HOME` with the runtime `.hermes` path. Include words like `creative`, `ideation`, `methodology`, or the selected creative frame when relevant so the lookup can surface ideation workflows.
4. Load a client-local overlay only if the caller provides the path or the runtime has an explicit local config for that client.
5. Use returned patterns as structure/reference, not as copy targets.
6. Gather returned required inputs before producing.
7. Produce in the recommended lane.
8. Run every returned QA gate.
9. Save source file plus rendered preview.

## Creative methodology routing
Agents may use a Nous-style creative-ideation approach: route the brief through a deliberate creative/design methodology to break same-y output, then constrain the chosen direction through feasibility, brand safety, and production QA.

Use it for:
- reports/PDFs that need a stronger visual metaphor or structure
- landing/social artifacts that are drifting into generic SaaS visuals
- UI/visual variants where the user wants options, novelty, or a different vibe
- early direction-setting before committing to HTML/CSS/SVG, Draw.io, Figma, or another lane

Guardrails:
- Do not copy a named artist's protected style directly; use methodology/lens, not imitation.
- Do not let the creative method override client brand rules, asset rights, or QA gates.
- Final artifacts still require rendered preview/screenshot review.

## CLI

```bash
${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup --artifact-type report --channel client-pdf --style-goal "premium diagnostic"
${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup --artifact-type landing-section --channel web --style-goal "editorial high trust"
${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup --artifact-type diagram --channel internal --style-goal "clear workflow"
${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup --artifact-type social --channel linkedin --style-goal "restrained owner-led"
${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup --artifact-type logo --channel web --style-goal "clean editable vector"
```

JSON mode for agents:

```bash
${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup --artifact-type report --channel client-pdf --style-goal "premium diagnostic" --json
```

With explicit client-local overlay:

```bash
${HERMES_HOME:-$HOME/.hermes}/bin/visual-reference-lookup --artifact-type report --client-id example --channel client-pdf --overlay /path/to/client/visual-overlay.json --json
```

Windows PowerShell example:

```powershell
$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME '.hermes' }
python (Join-Path $HermesHome 'bin\visual-reference-lookup') --artifact-type report --channel client-pdf --style-goal "premium diagnostic" --json
```

## Default lanes
- Reports/PDFs: template/editor lane or HTML/CSS/SVG, with rendered preview QA.
- Diagrams/workflows: Draw.io for reliable source/export; Mermaid for fast internal drafts.
- Landing/social visuals: HTML/CSS/SVG with browser screenshot inspection.
- Logos/icons: reject raster-only AI output; require clean editable SVG/layer inspection.
