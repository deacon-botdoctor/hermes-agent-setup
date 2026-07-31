---
name: golden-computer-use-v2
description: Use semantic, background-only desktop control after the Golden readiness doctor passes.
category: fleet
status: active
---

# Golden computer use v2

Run the paired baseline
`check-semantic-computer-control.py --required --semantic-probe` audit against
the exact runtime candidate before claiming desktop control is ready.

- `not_applicable` is normal for headless or non-opted-in runtimes.
- `staged` means package/config wiring is incomplete; do not expose the tool.
- `blocked` means repair the named prerequisite; never substitute direct UI
  scripting or injected input.
- On a ready host, use capture → reason → one background semantic action →
  fresh capture.
- Use current element indices or typed-browser refs. Raw coordinates,
  foreground focus, unrestricted mode, direct driver calls, and generated UI
  scripts are forbidden.
- End the session when work finishes. Native API/CLI/file lanes remain valid
  for work that does not produce application input.
