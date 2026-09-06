---
name: focused-specialist-coordinator
description: Coordinate a bounded task assigned to a named specialist profile with its own board and authority contract, including durable ownership, five-minute progress, artifact review, a recorded verdict, manager-safe delivery, and credentialless idle. Use for contracted specialist-profile dispatch and terminal handoff; it grants no specialist data, client, messaging, or repair authority.
---

# Focused Specialist Coordinator

The owning root agent remains the accountable user-facing manager and the only client-facing voice. A specialist is a narrow worker with one card, one boundary, and one auditable handoff—not another executive or messaging bot.

This skill provides only shared coordination mechanics. Load the named specialist's coordinator skill or exact profile contract for its board, allowed inputs, tools, output schema, and authority. If no specialist-specific contract exists, do not dispatch.

## Choose the execution path

Handle straightforward work directly within the owning agent's authority. For a temporary subtask that may use the same tools and credentials as its parent, use native delegation with a bounded goal, necessary context, expected result, verification, and budget. This skill's profile, board, and receipt prerequisites apply only when dispatching a contracted specialist profile.

Use the full contract below when the assignment requires a named specialist profile or its restricted tools, credentials, data boundary, or durable board workflow. A native child's separate conversation does not establish those restrictions. Keep the contracted specialist route until equivalent authority and lifecycle enforcement is verified. The root remains accountable for checking the result and reporting it on either path.

## Ownership contract

- The owning root agent owns acknowledgement, task scope, acceptance criteria, originating-conversation subscription, dispatch, progress visibility, review, verdict, recovery, and the final user/client report.
- The specialist owns the bounded work, honest insufficiency, saved artifacts, timely terminal state, and compliance with its narrow output contract.
- Never silently redo or take back the whole assignment. Investigate only a specific failed claim. If the manager must assume critical work, preserve the worker outcome, record `manager_override`, and make the correction attributable in the internal receipt.
- The manager may record only `accepted`, `rework`, `needs-human`, or `retired` as the coordinator verdict.

## Preflight and dispatch

Before creating the card:

1. Resolve the owning root agent's current runtime from the active runtime binding and verify the gateway PID executes that same runtime. Never hard-code a `runtime-candidates` path in a coordinator skill.
2. Confirm the specialist-specific board, profile, input boundary, terminal validator, maximum runtime, and retry limit.
3. Confirm the originating conversation has a durable terminal subscription. If the current surface cannot preserve that route, do not dispatch.
4. Confirm the specialist profile is stopped and credentialless at idle. Profile-local `auth.json`, credential assignments, a running worker gateway, or an unexplained live PID is a hard stop.
5. Snapshot the coordinator/root auth stable fingerprint, ownership, modes, and byte hashes without printing content.
6. Confirm the private specialist-management event ledger is writable. A manager
   gate that cannot preserve its content-free terminal event must fail closed
   before worker-derived content is delivered.

Tell the originating user which named specialist is taking the bounded work and that the root agent owns review and reporting. Create exactly one card on the explicit specialist board with:

- one task and acceptance criteria;
- exact assignee and input references;
- originating conversation subscription;
- idempotency key tied to the request;
- bounded runtime and one failure limit;
- explicit no-go actions and terminal output contract.

Dispatch only that board, at most one ready task, using the native Kanban command from the currently bound runtime. Do not enable the shared gateway dispatcher, create a second dispatcher, create a schedule, or dispatch unrelated ready cards.

## Five-minute progress ownership

The existing native notifier owns progress while the specialist works; the root agent does not need to remain in a model turn.

- Send nothing before 300 seconds.
- At 300 seconds, send one meaningful progress message to the originating conversation using observed task state: specialist name, bounded task, elapsed time, current phase or saved checkpoint, and whether it is running, blocked, or awaiting review.
- At each later 300-second milestone, edit that same message ID in place. Never create a new progress thread for the same run.
- Progress ticks must not call or wake a model. Only terminal state wakes the owning root agent for review.
- Preserve independent progress and terminal cursors so a restart cannot duplicate a message or lose the terminal handback.
- A generic “still working” message without current task state is not a valid checkpoint.

If the notifier contract is unavailable on the active runtime, disclose that before dispatch; do not imply that five-minute updates are active.

## Terminal review

On the single terminal wake:

1. Use native `kanban_show` on the exact board and task.
2. Inspect the saved result, run/events, terminal reason, hashes, and every actual attachment or artifact required by the specialist contract. A filename or model summary is not artifact inspection.
3. Run the specialist's deterministic terminal validator. Do not accept prose that is not bound to the exact input and artifact hashes.
4. Record exactly one coordinator verdict: `accepted`, `rework`, `needs-human`, or `retired`.
5. Run `scripts/specialist-manager-gate.py` over the exact worker, validation, manager, and proposed-delivery receipt.
6. Report in the root agent's voice, naming the specialist's contribution when useful and making the manager's review clear. Preserve failures and rework in the internal record; never forward raw worker output or internal failure text.

The manager-gate CLI appends one mode-private,
`specialist-management-event/v1` record to
`~/.hermes/state/specialist-management/events.jsonl`. It contains only task and
profile identities, terminal outcome, manager verdict/action, delivery booleans,
error codes, and evidence hashes. It must never contain client text, prompts,
artifact paths, raw worker output, or credentials.

`rework` must identify the smallest unsupported claim or missing artifact and reuse the same evidence when valid. `needs-human` names the exact decision or authority boundary. `retired` stops further dispatch for repeated quality or boundary failure.

## Manager-owned delivery gate

The worker never talks directly to the user or client. A terminal worker result is internal evidence until the manager-safe delivery gate passes.

- Never copy a raw worker response, exception, stack trace, tool failure, schema error, or unreviewed artifact into the originating conversation.
- An `accepted` delivery requires a valid specialist terminal receipt, inspection of every required actual artifact, exact hash binding, worker exit, credentialless idle, and zero side-effect drift.
- A correctable content defect receives one bounded `rework` with the exact failed claim or artifact. The manager keeps ownership and progress visibility while the retry runs.
- If the worker or coordinator machinery fails but the root agent can safely complete the bounded job inside its existing authority, the root agent may do so once as a `manager_override`. Preserve the failed worker attempt, validate the manager's replacement artifact, and report the reviewed outcome—not the worker failure—as the user-facing result.
- A manager override is not worker success and must not hide repeated employee failure. Repeated need for override triggers `retired` and profile repair before more dispatch.
- If completion truly requires outside authority, credentials, billing, destructive action, a principal decision, or an unavailable external system, use `needs-human`. The user-safe report states the intended outcome, what the manager already verified or attempted, the protected boundary, and the smallest action needed. It never says merely that an agent or tool is broken.
- The gate guarantees that unreviewed worker output is not delivered; it does not authorize the manager to invent facts or conceal a real external blocker.

The packaged `scripts/specialist-manager-gate.py` is the final deterministic delivery permission check. It recomputes hashes from the actual worker artifact, validator receipt, lifecycle receipt, and any manager-override artifact; caller-supplied status booleans are not evidence. The platform-aware launcher must produce the lifecycle receipt, including POSIX private-mode or Windows private-ACL proof. `delivery_permitted:false` means no worker-derived content may leave the manager boundary.

## Bounded parallel group join

When one request benefits from independent work, the manager may dispatch two or
three declared specialists at once. Do not add a dispatcher or let specialists
delegate to each other. Each member keeps its own card, lifecycle receipt,
validator, and manager-gate payload. After every member reaches terminal state,
run `scripts/specialist-group-join.py` with the exact shared request hash, member
gate payloads and receipts, and the manager's synthesis artifact. The join fails
closed on a missing member, mismatched request, duplicate task or role, invalid
manager gate, raw worker forwarding, or an unbound synthesis artifact. Only the
single manager synthesis may be delivered; individual worker deliveries remain
internal even when their own gates are valid.

## Versioned client Teams manager loop

Do not describe a provisioned or lane-proven specialist bench as a functioning
client Team. E1 means stopped credentialless seats exist. E2 means one or more
specialist lanes have passed isolated manager review. Only E3 means the tenant's
complete manager loop has passed its activity map and all ten certification
fixtures.

For every consequential instruction, the manager creates or supersedes one
`teams.state/v1` card. Impact-map every affected specialist and deterministic
surface, then issue the smallest `teams.packet/v1` packet carrying the current
decision version and fingerprint. Workers return `teams.receipt/v1`; a process
exit or worker success claim is only an acceptance candidate.

Immediately before any worker or deterministic service mutates state, run
`scripts/client-teams-manager-loop.py --preflight-packet` against the packet and
the current canonical Team State Card. Only a passing
`teams.packet-preflight-receipt/v1` permits mutation. A changed version,
fingerprint, tenant, affected lane, closed/superseded/blocked state, or
terminal/superseded packet blocks before the action begins. Preserve the
blocking receipt and request a current packet; never reinterpret stale work.

Before any client-facing completion, run
`scripts/client-teams-manager-loop.py` over the current card, packets, receipts,
manager inspections, deterministic-surface proof, approval state, rollback
proof, and the manager's proposed closeout. The join fails closed on missing,
stale, contradictory, superseded, uninspected, cross-tenant, credential-bearing,
unapproved, or rollback-incomplete work. A new decision version stops stale
workers before mutation. Timers, classifiers, scripts, publishers, queues, and
public surfaces must either read the canonical decision version or appear as an
explicit versioned lane.

The Team loop reuses the existing cards, notifier, worker lifecycle, manager
gate, and bounded group join. It does not add a dispatcher, queue, dashboard,
mesh, or schedule. Only the manager may deliver a passing closeout receipt; raw
worker output remains internal.

For each worker closeout, pass
`--manager-gate /absolute/gate-payload.json /absolute/gate-receipt.json`; repeat
for multiple workers. Use the packet ID as the gate's task ID, its assigned role
as the gate's profile, and the current State Card decision fingerprint as the
validation input hash. Include the gate's worker artifact hash in the Teams
receipt. The loop re-runs the existing manager gate against the current artifact
files and matches the exact supplied receipt file hash. A hash-shaped string
alone is not evidence. Deterministic-only decisions need no gate pair.

The group join likewise revalidates each gate and applies the manager gate's
existing text-overlap check to both the synthesis artifact and its summary.
This check catches verbatim forwarding, including added headings and whitespace;
semantic accuracy and paraphrase review remain the manager's responsibility.

## Staff and job map

Before routine or scheduled use, require one tenant-local staff map conforming to
[`references/staff-map.schema.json`](references/staff-map.schema.json). Every enabled
Hermes job and in-scope OS schedule must have exactly one disposition and one
side-effect owner. A specialist disposition must name a declared role, while the
deterministic executor retains all sends, writes, suppression, locks, filing, and
other side effects. An empty role list is valid when the manager has no justified
specialist; unowned jobs are not.

Run the packaged `scripts/staff-map-validate.py` before any profile binding or
job reassignment. JSON Schema shape alone is insufficient: the semantic validator
rejects duplicate roles or jobs, missing role references, active retired jobs,
retired specialists, new dispatch or schedule infrastructure, and any specialist
job whose deterministic executor is not also the side-effect owner.

## Credentialless terminal gate

After every terminal state, including crash or timeout:

1. Confirm the worker PID exited and its gateway remains stopped.
2. Scrub ambient provider/token/secret variables from any child-process environment except the one bounded inference route explicitly supplied by the owning launcher. Never expose values in logs or receipts.
3. If Hermes materialized profile-local `auth.json` or another credential file, move the exact bytes intact to an operator-owned mode-0600 quarantine outside the profile. Record only path, hash, owner, mode, and reason; never print content or discard the only rollback copy.
4. Recheck the profile has no auth file or credential assignment at idle.
5. Compare root/coordinator auth stable fingerprint, ownership, and configuration. A known volatile OAuth refresh may change bytes only if the existing stable-auth verifier accepts it; any other drift blocks acceptance.

A live worker PID, failed cleanup, credential residue, wrong owner/mode, unexplained root-auth drift, missing artifact, or validator failure invalidates the run and prevents an `accepted` verdict.

## Retry and retirement

One bounded retry is allowed only for a corrected transient or exact rework card. Never silently broaden data, tools, runtime, time, or authority. Preserve every attempt. Repeated poor quality, boundary failure, or review cost comparable to redoing the work should trigger `retired` and a profile/SOUL/model/tool review before further employment.

## Daily staff accountability

The existing nightly client-day-review reads the content-free management ledger
and produces a per-profile `retain`, `watch`, `redesign`, or `retire`
recommendation. The manager must reflect on exact observed profile and event IDs;
it cannot erase the deterministic recommendation with prose. Repeated rework,
manager overrides, worker failure/timeout, or any invalid delivery gate warrants
redesign review. An already-recorded retired verdict remains retired. The
existing fleet-oversight sweep receives redesign/retire alerts for independent
diagnosis; the specialist does not contact the oversight runtime or the client.
