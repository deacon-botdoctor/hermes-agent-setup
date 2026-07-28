# Hermes Agent Setup

The public, reproducible setup for the current Bot Doctor Hermes runtime.

The release remains native-first: it starts from one exact
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
commit, keeps native memory, sessions, compaction, tasks, gateway, and service
tooling, then applies only the source-visible compatibility gaps listed in
[`patches/registry.yaml`](patches/registry.yaml).

[`release.json`](release.json) pins the current upstream and Golden identities,
runtime-payload digest, and assembled-runtime fingerprint. The sanitized source
manifest allowlists every shipped runtime file by Git blob identity. Fleet
health/start adapters, routes, credentials, client identities, private data,
and operator control are not included.

## Start here

Give the agent access to this repository and say:

> Follow AGENTS.md. Install or update to the exact release in release.json,
> preserve my data and unfinished work, and do not switch the gateway until the
> maintenance-quiescence and rollback checks pass.

The agent will select the fresh-install or existing-runtime path in
[`AGENTS.md`](AGENTS.md). Before doing anything else it should run:

```bash
python3 bin/verify-release.py
```

To reproduce the exact source candidate without touching a live profile:

```bash
python3 bin/assemble-runtime.py \
  --output /absolute/path/to/new-runtime-candidate
```

The assembler refuses an existing output path, checks out the pinned upstream
commit, applies the public payload, and verifies the exact fleet-tested runtime
fingerprint. It does not stop or install a gateway.

See [`RUNTIME.md`](RUNTIME.md) for what this release changes and why.

## Choose your path

### Fresh headless agent

Use the fresh POSIX or Windows path in [`AGENTS.md`](AGENTS.md). Both paths:

1. verify the public source manifest;
2. install the pinned upstream dependencies into a side-by-side candidate;
3. apply and fingerprint the public runtime;
4. run native `hermes setup`;
5. install only manifest-owned profile files with a local rollback;
6. run native `hermes doctor`; and
7. install the native gateway service only after verification.

Browser automation, Composio, and additional MCP servers should be enabled only
when the agent has a real use for them. See [DEFAULTS.md](DEFAULTS.md).

### Lean does not mean less capable

The old setup eagerly loaded optional tools, MCP servers, browser wiring, and
duplicate memory/context layers into every session. That increased prompt and
tool-list size, obscured routing, and created more always-on failure surfaces.
The native-first setup keeps the base runtime small and discovers optional
capabilities when a request needs them.

A cold capability is not a missing capability. Skills, configured MCP servers,
approved connectors, and browser automation remain discoverable and can be
loaded on demand. Before saying a task cannot be done, the agent must inspect
its capability inventory and run a safe health or connection check. The full
routing and failure-state doctrine is in [DEFAULTS.md](DEFAULTS.md).

### Agent already running an older build

The same contract handles official/native installs, the previous public build,
and older customized runtimes. It inventories the real process and service,
builds the candidate separately, preserves a database-consistent rollback,
waits for active work to finish or checkpoint, switches one runtime generation,
then proves continuation and rollback.

The human-readable gate summary is in [MIGRATION.md](MIGRATION.md).

## What changed from the old public setup

The old public setup contained duplicate context/memory systems, placeholder
plugins, copied MCPs, and permanent browser wiring. Those remain deleted.
The current bundle adds back only the exact, fleet-tested compatibility payload
that pinned native Hermes does not yet provide.

The replacement has five rules:

1. Use pinned official Hermes as the base.
2. Preserve user data and identity separately from code.
3. Prefer native behavior, configuration, and plugins before source patches.
4. Add optional capabilities only for a proven consumer.
5. Never remove the old runtime until current-release health and rollback pass.

## Ownership model

| Layer | Owner |
|---|---|
| Base runtime, memory, sessions, gateway, service tooling | Upstream Hermes |
| Public compatibility payload and exact release manifest | This repository |
| `config.yaml`, `.env`, `state.db`, `MEMORY.md`, `USER.md`, sessions, projects, skills | The local agent/user |
| Optional tools and MCPs | Local configuration, cold by default |
| Fleet orchestration, credentials, shared databases | Outside this public repository |

This repository contains a deterministic assembler and a bounded profile
installer, not a fleet control plane. Native setup, doctor, gateway services,
and local user data remain owned by Hermes and the user.
