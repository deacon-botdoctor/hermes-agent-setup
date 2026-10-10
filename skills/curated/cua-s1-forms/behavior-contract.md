# CUA adapter behavior contract
Run the CLI with the prepared Python venv, --form Registration --entities-json FILE --elements-json FILE. Inputs are JSON entities as label/value pairs and elements with role,label,value,index,optional boolean checked.
1. An exact already-present selected value produces no fill action.
2. Checked or unknown-state checkboxes produce no click. Unknown state must not permit submit.
3. A plan contains at most one action, with recapture_and_replan_after_action=true. Fresh input indexes must be used after each rerun.
4. Submit is omitted unless --allow-submit is passed. An unresolved low-confidence decision must not allow submit.
5. Distinct empty phone/email fields can be filled over successive fresh CLI observations, skipping the first once filled.
6. Duplicate or negative indexes must not produce an executable plan.
Live execution is tested separately. Fleet activation is out of scope for this scorer test. Do not inspect source, tests, or diffs.

7. Dropdowns, duplicate editable labels, disabled controls, and read-only controls produce no action plan, review_required=true and a named handoff reason. These controls return to the main agent; this adapter does not choose dropdown options or guess duplicate-field context. JSON uses enabled/read_only boolean metadata; AX uses explicit disabled/readonly markers.
