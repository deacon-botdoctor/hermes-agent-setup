# Printing Press full discovery pass

Use this workflow when asked to print a service or find, set up, or build an integration.
The catalog command `printing-press` and engine `cli-printing-press` have different jobs.
Do not stop after a catalog miss or describe a catalog search as a completed Press pass.

## Resolve the owning runtime

Use the requesting client's runtime, installed capabilities, and existing authorized
connections. Record the engine path and version and the resolved skill path.
The managed executable is `$HERMES_HOME/bin/cli-printing-press` on POSIX and
`$env:HERMES_HOME\bin\cli-printing-press.exe` on Windows. If PATH does not resolve
it, invoke that exact profile path. Read `state/printing-press-install-v1.json`
under the same home for its checksum and rollback reference. Use the owning native
shell; translate upstream Bash examples when the runtime uses PowerShell.
Check `cli-printing-press --help` and each applicable subcommand's help before invocation.
The installed workflow must be version 3.0.0 with all phase and reference files.
Read the profile's actual skill and confirm `phases/01-preflight.md` exists; an older
monolithic skill or missing phase tree is a delivery gap. Do not claim the full
workflow is installed from source packaging or engine presence alone.
Run `skill-compat --skill "$HERMES_HOME/skills/printing-press/SKILL.md" --json`
with the managed engine on POSIX. On Windows, use
`skill-compat --skill "$env:HERMES_HOME\skills\printing-press\SKILL.md" --json`.
Require this runtime-bound check to pass. The `version --json` global skill search
does not resolve Hermes profile paths; retain unrelated global-copy warnings as
diagnostics without treating them as the profile's compatibility result.

The packaged engine version is 4.33.4+botdoctor.3. It omits generated test files,
test fixtures, and test helpers under the fleet testing policy. Run existing checks and exercise the generated CLI
directly. Do not create replacement tests or fixtures. The installer consumes a
checksum-verified archive staged with the Golden candidate.

A missing binary is a setup gap, not evidence
that the provider has no API. Do not silently install a different version or use
another client's credentials. Follow the owning installation route for repair.

A missing Go compiler blocks build/shipcheck, not public research or independent
engine legs. Record the gap and continue those lanes. Do not treat a preflight
failure as permission to skip discovery or claim a completed pass.

## Keep a discovery ledger

For each lane, record the actual query or command, source URL or artifact, observed
result, and next action. Mark a lane complete, failed, blocked, or not applicable.
A not-applicable lane needs a concrete reason. Continue independent lanes after failure.

1. Search the Press catalog and installed tools. Inspect plausible candidates.
2. Read official provider documentation, API specifications, SDKs, authentication,
   object coverage, limits, and mutation boundaries.
3. Search public source repositories and relevant package registries for CLIs,
   SDKs, and MCPs. Inspect candidates before rejecting or reusing them.
4. Inspect browser evidence when the requested behavior is not covered by a
   documented API. Use the authorized owning browser session. Missing login or
   capture authority blocks that lane; it does not erase public research.
5. Inventory the owning runtime's connectors, capability router, skills, and CLIs.

Then execute the applicable engine legs from the phase files: research, specification
resolution or authorized capture, generation, build, and shipcheck. Retain command
exit codes and actual output artifacts. Run a harmless live read when needed to prove
the selected or generated integration. Do not run mutation, billing, login changes,
public publishing, or client messages without authority for that exact action.

`vision` can produce an empty research template. A template is not completed research.
Populate and verify findings before treating it as evidence. A failed `print` spec
lookup or empty `crowd-sniff` result does not replace the remaining discovery lanes.
Do not claim that a CLI, MCP, or provider integration exists until its actual artifact
and supported behavior have been inspected and exercised.

## Report the observed result

- **Complete Press:** every applicable discovery lane and required verification
  gate passed. Name the selected artifact and the live behavior actually verified.
- **Partial Press:** useful research or an artifact exists, but a required lane or
  verification gate failed or remains unavailable. Name each missing piece of evidence.
- **Blocked Press:** no further applicable work can proceed through authorized routes.
  State the failed action, missing input or access, and exact resume condition.
- **Browser-session verified:** only the stated UI behavior passed. This does not
  establish a provider API, generated CLI, or MCP integration.

Keep engine output, source research, installation, invocation, and live verification
separate in the final report. File presence, a template, or a running gateway does
not establish integration success. Never put secrets, cookies, client identity, or
customer content in shared artifacts. Keep private evidence on the owning runtime.
