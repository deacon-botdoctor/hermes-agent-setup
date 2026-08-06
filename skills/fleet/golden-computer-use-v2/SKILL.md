---
name: golden-computer-use-v2
description: Drive desktops with fresh semantic state.
category: fleet
status: active
---

# Golden Computer Use v2 Skill

Use the existing `computer_use` tool to operate eligible desktops from fresh
semantic state. Raw coordinates and direct input scripts remain unavailable.
The driver posture is selected by runtime configuration, never by a model turn:

- `standard` uses normal CUA approval handling.
- `dedicated_principal` uses unrestricted CUA only when the configured
  principal, agent, device, and `outcome_scoped` authority exactly match the
  live runtime.

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

For `dedicated_principal`, readiness must additionally prove the exact
`client_identity`, `HERMES_AGENT_ID`/`HERMES_PROFILE`, and host name binding.
Any missing or mismatched field blocks computer input without disabling the
agent's non-UI tools.

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
6. A clear request to do something in a web UI authorizes the configured
   client-isolated browser/computer tools for that task. Use them without asking
   whether to use the browser or adding driver-consent ceremony. At human
   verification, put the checkpoint on screen, state that it is ready, retain
   state, and resume after clearance. Preserve outcome gates for purchases,
   destructive deletion, final legal/financial submission, public/client
   messages, account-security changes, credential export, and human-present
   verification.

## Pitfalls

- Raw coordinates, direct driver calls, and generated UI scripts remain
  forbidden. Unrestricted driver mode is valid only through an exact
  `dedicated_principal` runtime binding; a tool call cannot broaden its own
  permission posture.
- Do not replace a failed semantic action with AppleScript, injected input, or
  a legacy desktop-control tool.
- End the session when work finishes. Native API/CLI/file lanes remain valid
  for work that does not produce application input.

## Verification

Verify the requested application state with a fresh capture after every
state-changing action. Report success only from that verified state.
