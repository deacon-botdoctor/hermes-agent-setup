# Bound runtime command

Background integrations select the active runtime when starting work, rather
than pinning the release installed when their wrapper was created.

```sh
/usr/bin/python3 /path/to/bundle/maintenance/bin/run-bound-hermes.py \
  --hermes-home /absolute/profile -- chat --query-file /absolute/job.txt
```

Use a stable host Python. The command reuses the runtime-coherence binding
validator, checks the active profile/runtime/interpreter tuple and unchanged
service/launcher hashes, and refuses a native drain marker. Operator-bound
runtimes retain their fingerprint verifier. There is no fallback runtime.
Arguments and working directory are preserved. Inherited Python import paths
are removed; the bound runtime supplies the CLI module and interpreter.
Process replacement preserves the child's exit status and signal handling.

For profile-local installation, place this command and the bundle's existing
`checks/agent-runtime-coherence.py` together in the profile's `bin` directory.
Installation is not automatic. An owning migration must back up both preimages,
validate both file hashes, and include them in rollback.

## Maintenance boundary

This selects a runtime; it does not coordinate drain. The owning supervisor
must stop admitting work and finish active children before switching bindings.
The binding is checked again before process replacement, but that is not a
lock against an uncoordinated switch. Command validation is not idle proof.
A leftover drain marker requires owner recovery; age alone never admits work.

This command does not restart or configure services, change client identity,
credentials or job definitions, or create worker profiles.
