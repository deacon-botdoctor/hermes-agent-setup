---
name: golden-computer-use-v2
description: Drive desktops with fresh semantic state.
category: fleet
status: active
---

# Golden Computer Use v2 Skill

Use the existing `computer_use` tool to operate eligible desktops from fresh
semantic state. It does not authorize raw coordinates, direct input scripts,
or unrestricted desktop control.

## When to Use

Use this skill after the Golden readiness doctor marks the exact runtime and
driver ready for semantic computer control.

## Prerequisites

Run the paired baseline
`check-semantic-computer-control.py --required --semantic-probe` audit against
the exact runtime candidate before claiming desktop control is ready.

- `not_applicable` is normal for headless or non-opted-in runtimes.
- `staged` means package/config wiring is incomplete; do not expose the tool.
- `blocked` means repair the named prerequisite; never substitute direct UI
  scripting or injected input.

## Procedure

1. Capture the named app using semantic mode.
2. Reason from current element indices or typed-browser refs.
3. Use one background semantic action, then capture fresh state.
4. If background control explicitly escalates, cannot verify input, or reaches
   a native sheet/file picker that requires focus, use foreground delivery and
   raise the window only when the profile has
   `allow_foreground_escalation: true`.
5. After two unverified attempts at the same transition, do not repeat it a
   third time. Recapture, change delivery mode or semantic route, and continue
   from verified state.

## Pitfalls

- Raw coordinates, unrestricted mode, direct driver calls, and generated UI
  scripts remain forbidden even when foreground escalation is enabled.
- Do not replace a failed semantic action with AppleScript, injected input, or
  a legacy desktop-control tool.
- End the session when work finishes. Native API/CLI/file lanes remain valid
  for work that does not produce application input.

## Verification

Verify the requested application state with a fresh capture after every
state-changing action. Report success only from that verified state.
