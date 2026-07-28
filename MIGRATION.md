# Updating an existing Hermes agent

This is the human-readable summary. The agent executes the detailed contract in
[`AGENTS.md`](AGENTS.md).

## 1. Prove what is live

The active process, imported source root, service definition, command route,
profile, and `HERMES_HOME` must agree. A folder name or Git remote is not proof.

The agent records active turns and background work, restart history, exact code
state, config/service hashes, database and identity paths, and legacy
components. It never prints credential values.

## 2. Preserve a real rollback

Before changing live state, the agent captures:

- code SHA and dirty changes;
- configuration, launcher, service, and command route;
- a SQLite-consistent `state.db` backup with integrity result;
- identity, projects, skills, and client-local data;
- exact stop, restore, and start commands.

The old runtime remains intact. A raw copy of a live SQLite database without
its WAL state is not a rollback.

## 3. Build the release separately

The public source manifest is verified first. The exact upstream commit and
public Golden payload are assembled into a new candidate directory. The
candidate must match the runtime fingerprint in `release.json`.

Dependencies and profile wiring are prepared under a staging home. The live
checkout, data, command route, and service remain untouched.

## 4. Let active work settle

The agent receives a short maintenance event: finish the current atomic step,
avoid new delegation, or checkpoint the exact next action. New executable work
stops while inbound messages remain durably queued per chat/topic.

The controller waits for turns, tools, delegated tasks, cron/API work,
compression, media handling, and delivery transactions to finish or become
durably replayable. Readiness must be stable twice. Uncertain work means this
machine stays on the old runtime; it is never force-killed for rollout speed.

## 5. Switch one generation

After the final consistent database snapshot:

1. install the manifest-owned profile files, preserving their local rollback;
2. stop the old gateway through its actual service owner;
3. bind the same service scope and profile to the candidate;
4. start exactly one new generation;
5. restore checkpoints and replay queued messages once; and
6. reopen admission only after health and continuation proof.

The two generations never open the same database concurrently.

## 6. Verify and rehearse rollback

Required proof includes:

- exact runtime fingerprint and module origins;
- native doctor and process health;
- unchanged messaging identity and allowlist;
- real private ingress-to-egress;
- restart/continuation without duplicates;
- cross-topic isolation;
- native memory, search, tasks, goals, and cron;
- native Tool Search plus cold capability activation;
- no retired duplicate memory/context services;
- successful rollback to the old runtime and restoration to the new one.

Only obsolete code/service bindings may be removed after acceptance. User data
and one sealed rollback remain for the retention window.
