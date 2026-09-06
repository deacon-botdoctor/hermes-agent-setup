# Hermes Agent Setup

The public, reproducible setup for the current Bot Doctor Hermes runtime.

The release remains native-first: it starts from one exact
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
commit, keeps native memory, sessions, compaction, tasks, gateway, and service
tooling, then applies only the source-visible compatibility gaps listed in
[`patches/registry.yaml`](patches/registry.yaml).

[`release.json`](release.json) pins the current upstream and Golden identities,
runtime-payload digest, and assembled-runtime fingerprint. The sanitized source
manifest allowlists every shipped runtime file by Git blob identity. Public
local health/readiness tools are included; private fleet routes, credentials,
client identities, private data, and operator-control implementation are not.

This release also carries the separately pinned native-agent-continuity overlay
and advances the general Hermes/Golden runtime to the fleet-proven host-health,
capability-readiness, and cron self-remediation build. Managed new-client
provisioning writes the tenant manifest that activates continuity through the
existing self-heal scheduler; installations without that manifest receive the
verified files but are not enrolled or changed automatically.

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

### Semantic computer-control readiness

The lazy skill and native `computer_use` tool are discoverable on CLI and
Telegram by default. Profile installation now installs and verifies the exact
Golden-pinned Cua Driver version through native Hermes. A headless host may
record a degraded desktop doctor while retaining normal automation; the
stricter semantic-only guard stays off unless a local GUI host passes the
public, read-only wiring audit:

```bash
"$candidate/venv/bin/python" bin/check-semantic-computer-control.py \
  --home "$HERMES_HOME" \
  --runtime-root "$candidate" \
  --required \
  --semantic-probe \
  --json
```

The audit checks the Golden guard plugin, lazy skill, explicit platform
exposure, standard-mode upstream seam, and the real Hermes tool path using only
`list_windows`. It never clicks, types, focuses, raises a window, writes config,
restarts a service, or authorizes rollout. On a GUI installation, pass
`--require-computer-use-ready` to `bin/install-profile.py`; macOS may still
require the user or managed-device policy to grant Accessibility and Screen
Recording to the stable `CuaDriver.app` identity.

## Choose your path

### Fresh headless agent

Use the fresh POSIX or Windows path in [`AGENTS.md`](AGENTS.md). Both paths:

1. verify the public source manifest;
2. install the pinned upstream dependencies into a side-by-side candidate;
3. apply and fingerprint the public runtime;
4. run native `hermes setup`;
5. install only manifest-owned profile files with a local rollback;
6. install and receipt the pinned native Cua Driver without restarting the
   gateway;
7. run native `hermes doctor` and the local host-capacity/readiness self-check;
8. prove each manifest-declared capability and canary through its real user
   path;
9. install the native gateway service only after verification; and
10. install the release-pinned recurring runtime-coherence check for the host's
   native scheduler.

For a managed new client, the provisioner also declares
`native-agent-continuity`. The next existing self-heal run installs and verifies
the tenant-local GBrain baseline, binds an authenticated Codex installation to
the 15-tool read/write MCP, and drains the sanitized session backlog into GBrain
and Luna-authored cards. A Luna request is capped at eight cards, but the runner
continues until the current backlog is empty; there is no cards-per-day limit.
Claude and Gemini can be detected without being silently authenticated or
enrolled.

There are no generic fleet installs. Before activation, a fresh agent must also
have an exact client-owned runtime manifest that declares its principal,
purpose, expected transports, workload capabilities, scheduled workflows, and
the canaries that prove those workflows. Install every capability required by
that manifest, run the real user path, and prove that each required canary is
fresh and centrally visible to Doc. A deliberately smaller installation is an
exception and must be recorded in the manifest with its reason. Browser
automation, Composio, and additional MCP servers remain cold unless the
client's actual workload requires them. See [DEFAULTS.md](DEFAULTS.md).

### Fully decked does not mean always loaded

The old setup eagerly loaded optional tools, MCP servers, browser wiring, and
duplicate memory/context layers into every session. That increased prompt and
tool-list size, obscured routing, and created more always-on failure surfaces.
The native-first setup keeps session exposure small while provisioning the
client's complete declared capability set and discovering cold capabilities on
demand.

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

### Recurring runtime-coherence check

The release includes one small host-side check for macOS, Linux, and Windows.
It periodically proves that the service-owned runtime root, Python, and
assembled `AIAgent`/`init_agent` contract still agree. It writes a local 0600
receipt, does not restart Hermes, and does not inspect conversation content.

The installer and probe bytes are pinned by SHA-256 in `release.json`; its
baseline source commit records upstream lineage. Import checks use an empty
temporary HOME and HERMES_HOME, then keep only the final receipt.
Fresh installs run it after native service activation; upgrades run it after
the controlled cutover succeeds. Exact commands and rollback behavior are in
[AGENTS.md](AGENTS.md#install-the-recurring-runtime-coherence-check).

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

## Self-check and self-repair defaults

The profile installs a cross-platform local self-check, canary reconciler,
disk-retention guard, tool-readiness probe, and host-health operating rule. An
agent checks fresh disk, memory, process, and swap evidence before heavy work,
explains constrained headroom, and keeps the requested job moving with bounded
checkpoints. Pressure is not an automatic large-job veto. Cleanup remains
limited to task-owned or retention-policy-covered data; unknown processes,
unique data, active runtimes, and rollback artifacts are protected.

Routine scheduled-job failures are repair work, not an operator assignment.
After saving the original receipt, Hermes may run exactly one bounded repair
turn inside the owning runtime for script, configuration, runtime, timeout, or
execution failures, then verify and append the result. Successful script jobs
remain model-free, repair cannot recurse, and credentials, billing,
permissions, destructive data, provider limits, and uncertain external side
effects remain hard stops. The operator receives either a verified recovery or
the concrete boundary that stopped automatic repair—never an instruction to
repair the agent's script or configuration.
