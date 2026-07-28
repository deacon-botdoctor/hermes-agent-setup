---
name: papercuts
description: Record small routing, update, tool, authentication, and dependency failures that an agent could otherwise work around silently. Use after the smallest safe recovery or when the issue prevents progress; especially during client updates and Windows rollouts.
---

# Papercuts

Use `papercut.py` once per distinct issue per task. It records locally without interrupting the work; operator-control collects the ledger separately.

Do not include secrets, tokens, full client messages, or private document contents. Summarize the failure and preserve only a short sanitized error.

```sh
python3 "${HERMES_HOME:-$HOME/.hermes}/bin/papercut.py" \
  --kind routing \
  --operation fleet-update \
  --route windows-host \
  --target example-agent \
  --summary "Active runtime path differed from the default Windows tree" \
  --evidence "Updater initially targeted C:\\Users\\Agent\\.hermes\\hermes-agent"
```

Choose the narrowest kind: `routing`, `update`, `tool`, `auth`, `dependency`, or `other`. Use `--severity error` only when client delivery, a rollout, or safe recovery is blocked.

After recording it, continue with the smallest safe recovery. A papercut is telemetry, not permission to widen scope, retry blindly, mutate another client, or reveal sensitive data.

The local ledger is also the agent's reflection inbox. Inspect it with:

```sh
python3 "${HERMES_HOME:-$HOME/.hermes}/bin/papercut_inbox.py"
```

Do not manually acknowledge events during normal work. The event-driven reflection harness triggers when the inbox is non-empty, includes the pending events in the agent's private reflection, and acknowledges them only after a valid reflection report is written. Repeated patterns should become a concrete repair, a proposal-first skill candidate, or an escalation—not a vague promise to “watch it.”

Remediation remains bounded: `skill_candidate` enters the inert proposal/draft lane, while `repair` and `escalate` create a local repair envelope for operator-control collection. Never execute model-authored shell text. Credentials, restarts, client/fleet mutation, destructive actions, and other hard stops still require their normal approval.
