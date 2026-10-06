---
name: cua-s1-forms
description: Match supplied document values to unambiguous editable form fields using the local CUA-S1 scorer when its pinned runtime is available.
---

# CUA form scoring

Use the existing owning browser or computer-use route. This skill does not change tool priority, permissions, or target ownership. Prefer an owning API when available.

1. Capture the exact authorized form. Preserve labels, roles, values, checkbox state, enabled/read-only state, and fieldset context. Keep current native tokens or browser refs in the executor; do not put credentials in evidence.
2. Extract labeled source values without inventing missing values. If the local pinned scorer is unavailable, continue with the main agent. Do not install dependencies or download weights during an ordinary form task.
3. Run `python scripts/score.py --checkpoint PATH_TO_SAFETENSORS --form TITLE --entities-json ENTITIES --elements-json OBSERVATIONS`. Python must come from the prepared scorer environment. JSON observations use role,label,value,index,checked,enabled,read_only. Submit remains omitted unless explicitly requested and `--allow-submit` is supplied.
4. If review_required is true, use the main agent and full observation to resolve the named reason. Dropdowns require native selection, duplicate labels require ancestor context, and disabled/read-only controls must not be changed. Never remove unsupported controls to obtain a submit plan.
5. Execute only the one proposed action. Bind its index to the current capture's exact window and token or browser ref. Never send a bare index to a token-based driver. If the target changes, discard the plan. The existing Hermes CUA backend attaches tokens from its most recent capture; confirm that capability before use.
6. Recapture and read back the actual value after every action. Driver ok=true or effect=unverifiable is not proof. On an uncertain result, inspect state or the exact receipt before retrying. Never repeat a submission merely because its response was lost.
7. Replan from the fresh observation. An empty plan is not completion evidence. Verify every requested field and the requested final receipt.

## Qualification limits

The local scorer is advisory. It does not execute, identify windows, or validate business authorization. Numeric confidence is not an action grant. macOS elements_complete=false does not invalidate observed fields; it prevents claiming that unobserved fields are absent.

macOS native and Mini browser fixture proofs are filed in the release evidence. Linux native GTK and CPU scorer proofs are separate. Windows activation requires an explicit platform qualification receipt; package presence is not activation. Missing capture or state means return to the owning agent, not fallback to blind coordinates.

## Windows modal recovery

When a Windows button opens a modal file picker, UIA Invoke can block the capture provider in drivers 0.22.0 and 0.28.2. For this observed case, use `press_key` with Space on the fresh button token. Resolve the new dialog by its exact owner PID and window ID. Fill the observed file-name field, recapture, then use Return in that dialog. Verify the selected path in the calling app before any retry. This route passed on both versions; it is not a general replacement for native clicks.

A delivery escalation hint can accompany a successful action. Read back the result before escalating. Keep background delivery first. A drag receipt with global_input and unknown delivery mode does not establish background isolation; use that method only on an authorized isolated desktop until its focus behavior is qualified.
