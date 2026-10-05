# Repository checks

Run the same check set locally and in pull-request automation:

```sh
uvx --from pre-commit==4.5.1 pre-commit run --show-diff-on-failure
```

The default command checks staged files and always runs the named regression
suites. Stage the intended changes first. For an already committed branch:

```sh
uvx --from pre-commit==4.5.1 pre-commit run --from-ref origin/main --to-ref HEAD --show-diff-on-failure
```

Use `--files path/to/file` for an unstaged file. `--all-files` audits existing
debt and can fail on unrelated historical files. It is not the adoption gate.
Python 3.12 or later, Git, and uv are required. Agent Desk also requires Node 22
or later. Hook environments are separate from runtime environments.

## Enforced rules

- Changed Python must parse and reject undefined names, invalid control flow,
  mutable argument defaults, control flow that masks an exception in `finally`,
  and direct `eval` calls. Ruff uses an explicit correctness-only rule set.
- Changed Bash/POSIX shell scripts must pass ShellCheck warnings and informational findings. Zsh files are
  outside ShellCheck's supported language set.
- JSON and YAML must parse. Merge markers, conflicting filename case, private
  keys, common raw credential paths, and generated caches cannot be committed.
- Named regression suites run even when only documentation changes. Missing
  tools, missing tests, collection errors, and failures produce nonzero exit.

The rules do not rewrite files. No automatic formatter, blanket complexity
limit, file-length limit, or broad suppression baseline is introduced.
Keep any intentional fixture exception scoped to its exact path and explain
its purpose. Existing debt remains visible through `--all-files`.

## Proof and limits

These checks establish source behavior. They do not prove deployment, tenant
access, or a native user journey. Existing release checks and code review
remain required. Private-key detection runs locally. The workflow also runs the pinned
TruffleHog credential scan; code-ship-gate retains its local secret scan before
publication. A configured workflow is not proof that the host has an active
runner or that branch protection requires its result.

Do not install a Git hook into a shared worktree's common Git directory without
checking its other users. The explicit command and committed automation use
the same configuration without changing another developer's hooks.

## Regression ownership

`release-contracts` verifies the public source manifest and runs every current
top-level installation/canary/receipt/control suite. Driver fixtures use the
current contract, with a separate exact-release assertion and wrong-version
rejection tests. The Windows package case uses synthetic package files; it does
not install a driver or establish native Windows behavior. Canary tests verify
that unrelated schedules survive the supported stdin crontab interface.

The public release stays bound to its original Golden payload. These checks do
not repair the October 2 candidate receipt-location defect or claim a complete
assembly. That fix requires a Golden patcher change and a correctly regenerated
public release; editing the copied payload alone would invalidate provenance.
