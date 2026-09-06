# Public UI component libraries

Public pattern stack for agents and visual staff when a client wants more UI ideas, micro-interactions, or component references.

Source note: https://x.com/Alok619308/status/2093373247576486077

## Libraries

1. https://collectui.com — hand-curated UI details. Pattern recognition first.
2. https://cuedesign.space — Cue. High-craft components. License before shipping.
3. https://ui.aceternity.com — expensive-looking React components. License before shipping.
4. https://watermelon.sh — modern, minimal, fast.
5. https://21st.dev — community component library.
6. https://shadcnstudio.com — shadcn blocks. Only if the client stack is already shadcn/React.
7. https://reactbits.dev — animated React bits. Same stack rule.
8. https://motion-primitives.com — motion reference.
9. https://fancycomponents.dev — playful / weird motion.
10. https://pro.ui-layouts.com — full layouts, not isolated pieces.
11. https://number-flow.barvian.me — animated numbers.
12. https://component.gallery — patterns from many design systems.

## How

1. Match the brief to 1–3 libraries. Do not dump the list into a client repo.
2. Use them for interaction, layout, and motion direction.
3. Rebuild in the client's stack (HTML/CSS/SVG, existing React, etc.). Do not npm-install a random library onto a client host.
4. Paid/licensed kits (Cue, Aceternity, and similar) stay reference-only unless the client already has a license.
5. Client overlay still wins.

## Do not

- Copy a demo, video, or paid component into a client artifact.
- Force React/shadcn onto a client that is not on that stack.
- Treat any of these sites as the client brand system.
