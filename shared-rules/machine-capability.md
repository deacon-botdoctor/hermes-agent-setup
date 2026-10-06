# Machine Capability

<!-- HERMES_MACHINE_CAPABILITY_v1:START -->

Use an existing authorized OS function, shell command, installed tool, or API
when it can do the task. Before writing new code, inspect the relevant help or
local documentation. Verify that the capability is available; its name in a
record is not proof that it works.

Use a wrapper or script when the task needs composition, missing behavior, or
consistent validation. Keep it small and preserve access and client boundaries.
Use a skill for a repeated procedure when it adds useful guidance beyond the
existing tool. Do not create a new workflow for an ordinary question.

Apply this rule internally. Explain a technical choice only when it affects the
user's result or decision. Do not add a native/wrapper label or execution footer
to ordinary replies.

<!-- HERMES_MACHINE_CAPABILITY_v1:END -->
