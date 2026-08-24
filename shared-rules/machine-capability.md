# Machine Capability

<!-- HERMES_MACHINE_CAPABILITY_v1:START -->

Before writing a wrapper, skill, loop, or "agent does it" path, name what the computer can already do.

The baseline is the OS, the shell, the installed CLI, the live API, and the existing script. Not a new skill.

## Rule

1. Name the native path first: command, API, file, or existing CLI.
2. If that path exists, use it. Do not reconstruct it with searches, Python loops, or a new skill.
3. If you cannot name the native path, say `not_observed` and look — `--help`, `man`, `which`, repo docs, live API — before inventing a wrapper.
4. Reject your own output when it is beneath the machine: a double loop over a vectorized job, a backup scored as live, a 2,000-line skill around a 20-line command.
5. A skill or rule is allowed only when it adds a policy the computer will not enforce (approval, tenant isolation, evidence class). If it restates `ls` or `curl`, it is theater.

## Closeout

Say whether the path used was `native` or `wrapper`. If wrapper, one line on why the native path was not enough.

<!-- HERMES_MACHINE_CAPABILITY_v1:END -->

## Not this rule

- Finding a Hermes tool that already exists: `capability-discovery.md`
- Closing a genuine missing capability: look with `--help` / live API, then say `not_observed`. Do not invent a wrapper.
