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

## 3. Side-by-side native install

The official Hermes installer creates a new checkout. The agent records its
exact upstream SHA and runs `hermes doctor` before it can own the service. Since
the installer rewrites the user-facing launcher, the agent restores the old
launcher immediately and addresses the new checkout only by absolute path
until cutover.

## 4. Controlled switch and proof

With zero active turns, the agent drains the old gateway, binds the existing
service/profile to the new native command, and runs identity, messaging,
restart, continuity, memory, task, optional-tool, and rollback checks.
Immediately before cutover it takes a final consistent database snapshot.
Rollback stops both runtimes, restores that snapshot with its WAL sidecars
moved aside, verifies database integrity, and only then starts the old runtime.

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
| All MCPs hot | Replace with router/discovery plus cold optional MCPs |
| No-op compatibility plugins | Delete |
| Composio onboarding | Off until the user chooses an integration |
| Identity, `MEMORY.md`, `USER.md`, projects, skills | Preserve as local data |
| Credentials and messaging identity | Preserve; never publish or rotate implicitly |

## Stop conditions

Stop before switching if the live route, active turns, backup, service owner,
credential boundary, or rollback is ambiguous. Stop and roll back after the
switch on a traceback, model/provider error, missing table, restart loop,
identity mismatch, context leakage, missing task, or rollback drift.
