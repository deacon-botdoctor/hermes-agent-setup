# Migrating an old customized Hermes runtime

This is the human-readable companion to [AGENTS.md](AGENTS.md). The agent should
execute the detailed contract; the operator should expect these five gates.

## 1. Inventory

The agent proves what currently owns the live process. A folder named
`hermes-agent`, a stale Git remote, or a service file by itself is not proof.
The active process, imported modules, service definition, and runtime home must
agree.

## 2. Backup and rollback

The agent creates a local, permission-restricted backup of code state, config,
service wiring, identity, and data. The receipt contains hashes—not secrets.
`state.db` is captured through SQLite's backup API and integrity-checked; a raw
copy of a live database is insufficient. The old runtime is not modified or
deleted.

## 3. Isolated native install

The official Hermes installer creates a new checkout and managed dependencies
under a separate staging `HERMES_HOME`; it does not touch the live data home.
On POSIX, it also runs with a staging-only `HOME`, leaving the old launcher
untouched. The agent verifies that launcher or restores and verifies the
Windows User and process environment route, records the exact upstream SHA,
and runs `hermes doctor` against staging. The new checkout is addressed only
by absolute path until cutover.

## 4. Controlled switch and proof

With zero active turns, the agent drains the old gateway, binds the existing
service/profile to the new native command, and runs identity, messaging,
restart, continuity, memory, built-in task-tool, persistent-goal, optional-tool,
and rollback checks.
From a clean session after restart, it also proves that an ordinary request can
discover a cold capability, select the approved native/connector route, invoke
it, and verify the result. A cold capability must not be reported as missing;
browser automation is a fallback for a verified connector/API gap, not the
default SaaS route.
Immediately before cutover it takes a final consistent database snapshot.
Rollback stops both runtimes, moves the live database and its WAL sidecars
aside, restores that snapshot, verifies database integrity, and only then
starts the old runtime.

Any failure restores the old service. A credential, database-schema, identity,
or network change is not part of this migration unless the operator separately
requests it.

## 5. Retire obsolete code

Only after acceptance may the old patch/plugin/MCP code and obsolete service
bindings be removed. User data and the sealed rollback remain.

## Migration map

| Old surface | Native-first result |
|---|---|
| Source overlay / patch registry | Delete; follow upstream Hermes |
| Custom durable runtime / replay | Delete; use native sessions and resume |
| LCM | Delete; use native compaction/context |
| Anamnesis / second recall DB | Delete; use native memory/search |
| Telegram transcript recall DB | Delete; use native `state.db` |
| AutoDream/nightly dream | Off; add no replacement by default |
| Always-on browser lane | Off; cold-start for a verified consumer |
| All MCPs hot | Keep only required servers/tools enabled in native MCP configuration |
| No-op compatibility plugins | Delete |
| Composio onboarding | Off until the user chooses an integration |
| Identity, `MEMORY.md`, `USER.md`, projects, skills | Preserve as local data |
| Credentials and messaging identity | Preserve; never publish or rotate implicitly |

## Stop conditions

Stop before switching if the live route, active turns, backup, service owner,
credential boundary, or rollback is ambiguous. Stop and roll back after the
switch on a traceback, model/provider error, missing table, restart loop,
identity mismatch, context leakage, missing scheduled task, or rollback drift.
