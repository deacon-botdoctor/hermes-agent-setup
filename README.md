# Hermes Agent Setup

A native-first setup and migration guide for
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

This repository does **not** fork Hermes, patch its source, copy MCP servers, or
ship another memory system. It tells a fresh agent how to install Hermes and
tells an agent on the old Bot Doctor-style runtime how to move safely to native
Hermes without losing its identity, conversations, memory, configuration,
projects, skills, or credentials.

## Choose your path

### Fresh headless agent

Linux, macOS, WSL2, or Termux:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh \
  | bash -s -- --skip-browser
```

Start a new login shell (or reload its profile) so the installed command is on
`PATH`, then run:

```bash
hermes setup
hermes doctor
hermes gateway install
hermes gateway status
```

Native Windows PowerShell:

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

Open a new PowerShell window, then run:

```powershell
hermes doctor
hermes gateway install
hermes gateway status
```

The current official Windows installer always installs browser npm
dependencies and Playwright Chromium; it does not yet expose the POSIX
`--skip-browser` option. Keep browser automation inactive until it has a real
consumer.

Browser automation, Composio, and additional MCP servers should be enabled only
when the agent has a real use for them. See [DEFAULTS.md](DEFAULTS.md).

### Agent already running the old customized runtime

Open [AGENTS.md](AGENTS.md) in the agent's coding environment and say:

> Follow the migration contract in AGENTS.md. Start with the read-only
> inventory and stop before switching the live gateway unless I explicitly
> authorize the switch.

The detailed human-readable procedure is in [MIGRATION.md](MIGRATION.md).

## What changed from the old public setup

The previous repository contained a second runtime layer: source patches,
placeholder plugins, copied MCP servers, custom SQLite memory, Anamnesis, LCM,
AutoDream, transcript storage, and permanent browser wiring. Current Hermes
already owns the core session, memory, search, compaction, task, gateway, and
service behavior. Those duplicate implementations have been deleted from the
active repository.

The replacement has four rules:

1. Use official Hermes code.
2. Preserve user data and identity separately from code.
3. Add optional capabilities only for a proven consumer.
4. Never remove the old runtime until native health and rollback both pass.

## Ownership model

| Layer | Owner |
|---|---|
| Hermes code, installer, memory, sessions, gateway, service tooling | Upstream Hermes |
| `config.yaml`, `.env`, `state.db`, `MEMORY.md`, `USER.md`, sessions, projects, skills | The local agent/user |
| Optional tools and MCPs | Local configuration, cold by default |
| Fleet orchestration, credentials, shared databases | Outside this public repository |

This repository intentionally contains instructions, not a new installer or
control plane. The official installer and `hermes doctor` remain the executable
source of truth.
