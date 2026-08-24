# Video Production Routing

## Default

For every request to create, edit, animate, inspect, validate, or render video,
load the installed `hyperframes` skill first. HyperFrames is the default
composition and rendering framework unless the user explicitly requests a
different framework or asks only to record a browser session.

Route by deliverable through HyperFrames' owning workflow. Common cases:

- short, unnarrated kinetic text or stat hit: `motion-graphics`;
- plain captions on existing footage: `embedded-captions`;
- designed overlays on talking-head footage: `talking-head-recut`;
- product or website showcase: `product-launch-video`;
- topic explainer without product capture: `faceless-explainer`;
- custom or mixed-format production: `general-video`.

Use approved generative-video providers only to create source media when the
composition needs it. They do not replace HyperFrames' edit, layout, timing,
caption, and delivery workflow.

## Production quality gate

Before calling a video final:

1. Preserve exact copy, dates, citations, disclosures, logos, and source media.
2. Run HyperFrames lint and checks for runtime, layout, motion, and contrast.
3. Generate representative snapshots or a contact sheet and inspect them.
4. Render the requested aspect ratios and verify dimensions, duration, audio,
   safe areas, and legibility on the actual output.
5. Iterate on any obvious visual, timing, overflow, contrast, or artifact defect.

FFmpeg may transcode, mux, inspect, or finish a render. Pillow or frame-by-frame
FFmpeg composites are not the primary motion-design path when HyperFrames is
available. Never silently downgrade to a crude local animation because a
dependency is missing: name the exact blocker and use another lane only when it
can meet the same inspection and verification standard.

Do not publish, post, boost, spend, or send beyond the originating conversation
without the authority required for that external action.
