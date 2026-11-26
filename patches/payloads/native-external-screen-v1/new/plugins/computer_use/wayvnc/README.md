# Optional WayVNC dedicated-workstation provider

This optional `ComputerUseProvider` is dormant unless `computer_use.backend=wayvnc`.
The required Golden native external Screen carrier supplies the canonical helper
and stages this package into the native `plugins/computer_use/wayvnc/` discovery
seam. The package never modifies native source. Golden's payload manifest owns
both this package and the carrier. No package/dependency/runtime installation or
activation is performed by this source artifact.

## One target and one authority

`bot_desktop.mode` defaults to `native`. Only explicit `external_rfb` selects the
external service. `bot_desktop.external_rfb.endpoint` is a normalized absolute
private UDS with no colon/display syntax or symlink components;
`bot_desktop.external_rfb.generation` is the operator-owned dedicated
workstation/session/relay binding. This is the sole endpoint/generation config.
`control_endpoint` is the existing native owner-private WayVNC control UDS in
the same private directory; `native_incarnation` is its explicit current
64-lowercase-hex server/Wayland generation. Both endpoints and stable
lease/activity/participant locks are pinned into the authority binding. Local
Linux transport/control custody is required: a remote relay cannot attest its
Mac origin and is unsupported. Capability is freshly checked around operations.
The provider namespace has **only** `computer_use.wayvnc.python`, the literal
package-local `.venv/bin/python` path. Old duplicated endpoint/output/generation
keys refuse; they are not aliases or fallback targets. Malformed config fails
closed rather than recovering a stale/default target.

The native `tools.bot_desktop.external` helper owns endpoint validation, canonical
profile binding, stable activity inode and identity resolution. Native Screen,
tickets and provider consume that exact helper. The native lease is the only
input authority. External mode requires affirmative `active-agent` or matching
`active-human`; missing, corrupt, quiescing or excluded state grants nothing.

Initialization is explicit `hermes computer-use screen external-initialize --json` under an
exclusive local drain, with a fresh authority incarnation and endpoint/profile
binding created exclusively. It **never** grants agent/human control. Do not run
it on an active profile, delete/recreate locks or authority records, hand-author
an active lease, or treat it as an active recovery route. The supported
`hermes computer-use screen external-reconcile --json` quiesces input, drains
all cooperating input-bearing links and consumes strictly validated completed
cleanup facts using each participant's immutable original tuple/nonce. Normal
agent and human close persist genuine `compositor_processed` proof while native
replay remains obtainable. Reconciliation does not require replay of a durable
completed fact after the native 60-second cache expires. These facts never grant
current ownership: current identity, origin, capability, live non-reappearance
guards, activity EX and a fresh serialized lease/grant are still required. First
Active additionally requires a real aggregate C-input baseline under its held
native admission fence and privileged Operator generation-origin evidence.
No caller clean flag, force, empty list, historical receipt alone, file deletion,
idle timeout or process death substitutes for either prerequisite. Schema-shaped
active JSON without the exact committed lease/grant binding is excluded.

### Privileged origin interface

Operator Control, not the lease caller, owns origin issuance and revocation.
The fixed Linux record is `/etc/hermes-screen-origins/<Identity.fingerprint>.json`.
Every ancestor must be a root-owned real directory without group/other write;
the record must be root-owned, single-link, regular, 0644, at most 16 KiB.
Root runtimes refuse because they collapse this separate trust boundary.
Missing, changed, revoked, malformed or wrong-identity/boot evidence excludes.
No config, environment variable, caller object or path can bypass this reader.

Exact top-level fields: integer `schema=1`, `issuer="Operator"`, complete exact
`identity=Identity.as_dict()`, current Linux `boot_id`, bounded opaque genuine
`wayland_generation`, `generation_creation_evidence_sha256`, `native_bundle_sha256`,
`managed_ingress`, and `other_input_producers`. Every SHA-256 is lowercase 64 hex
and must identify retained actual evidence, not a hash of a handwritten clean claim.
`managed_ingress` has exactly `policy="s2-shared-only-since-generation-origin"`
and `evidence_sha256`. The 1–32 unique producer objects have exactly `name`,
`disposition="disabled-since-generation-origin"`, and `evidence_sha256`; the
list must explicitly include `Cua` and `all-other-non-C-input`, plus every known
producer. Operator must prove completeness and continued enforcement.

Operator must establish a **genuine new compositor/Wayland generation** after
stopping all non-C input producers and retaining their exclusion. Stopping Cua
now, or starting a new WayVNC attach in an old unknown compositor, is insufficient.
Shared-only S2 custody must cover all supported private native ingress since that
origin; an unguarded raw relay does not qualify. No origin evidence is generated
by this consumer or carrier. Privileged custody identifies the attesting producer;
code cannot establish the truth of Operator's observations from JSON syntax.

Initial aggregate acquisition requires zero READY clients, including passive
watchers. Close watchers through their supported owners before bootstrap; this
consumer never evicts unknown clients to make the barrier pass. Subsequent
handoffs use per-client retirement and keep passive authenticated watchers.
The native C certificate is only same-owning-connection compositor processing,
not application consumption, global desktop cleanup, or a non-C producer barrier.
NeatVNC's unsafe unshared pre-READY teardown remains unfixed outside admitted
shared-only ingress. Malicious same-UID clients are not newly sandboxed.

`baseline.json` persists the exact native incarnation/nonce/fence before begin;
`grant.json` binds a completed publication to the exact lease and Operator origin.
Activity EX and serialized lease ownership remain held through final publication
and exact native release. Readers take nonblocking lease SH and require current
capabilities, released lineage, unchanged origin and the exact grant. No finally,
error, timeout or cancellation releases a native gate. Failed or ambiguous work
retains its journal and exclusion; only the existing exact native control release
can cancel that owned tuple after final excluded publication. There is no record
deletion, automatic reprovisioning or adoption of an uncertain released receipt.
An unrecoverable failed record or missing durable released lineage needs supported
genuine fresh provisioning, not a fresh nonce against an old fence.

### Explicit historical-unknown C recovery

After source admission, Operator may run
`hermes -p <existing-profile> computer-use screen external-recover --json`.
This is a separate genuinely measured event, not a force/clean switch, a missing
per-client receipt, or a retry that turns a failed native terminal successful.
It requires the same authentic root Operator origin and exact identity/native
incarnation, an existing released initial baseline, a preserved excluded lease,
complete known identified participant custody and **zero READY clients including
watchers**. Close watchers through their supported owners first. The operation
never disconnects an existing known/unknown native watcher to obtain zero.
The native outer-loop zero+admission/input fence, same-owning-connection sync,
completed certificate and exact successful release are the existing native API;
no native C rebuild or relaxed capability is required. A fenced late arrival can
be rejected by native admission; it is not adopted as a managed participant.

The additive `recovery.json` schema1, kind `aggregate-c-recovery`, retains the
original released baseline data, each event's original lease/grant/complete
participant data, fresh incarnation/nonce/fence persisted **before begin**, actual
certificate and release, and requested/processed/released phase. Each original
excluded interval is saved before quiescing can replace it; an unfinished event
also retains up to64 distinct excluded retry intervals, without eviction. The original
`baseline.json` bytes are not rewritten or migrated. Every original participant
tuple and receipt (including null/unknown) remains intact. Coverage of those old
input tuples comes from the new aggregate C event, never a fabricated historical
client receipt. The journal permits at most64 retained events /256KiB of actual
serialized data; capacity exhaustion excludes without eviction. Grant schema2
binds the unchanged initial baseline plus the exact released recovery target to
a fresh lease. Legacy grant schema1 remains strict only when no recovery exists.

Recovery holds activity EX and uses the existing quiesce/publication/commit path.
Only its explicit operation can resume a durable unfinished exact tuple. Every
unknown, failure, cancellation, supersession or ambiguous release retains the
journal and exclusion; no finally/error/cancel path releases a held native fence.
An unreleased failed native record is not replaced with a new nonce. If release
completed but its success evidence was lost/expired, this source does not adopt
the ambiguity. Once the new released event is durable, ordinary future handoffs
use its exact historical coverage plus later durable per-client facts and retain
passive viewers; ordinary Watch/Take never requests a zero-READY aggregate.
The certificate covers the current same-incarnation **C-input domain only**,
not application consumption, global/non-C cleanup or recovery across reboot.
Service persistence/rebind/origin issuance remains an Operator lifecycle task.

### Read-only same-profile generation consumer

The SDK exposes only `external-generation-status` under
`hermes -p <existing-profile> computer-use screen`, with `--json`. Status inspects
old baseline/root-origin lineage before a transaction and the verified new
identity after commit, without requiring a deleted old baseline or live socket.
It does not infer current permission. Generation prepare/commit/archive and
profile replacement belong in private Operator Control, not this runtime.

The fixed principal record is
`/etc/hermes-screen-generations/<sha256(canonical-home-UTF8)>.json`, with exactly
the same root ancestry, regular-file, single-link, 0644 and 16KiB custody as the
origin consumer. No path/env/config/CLI selector exists. Schema1/issuer Operator
bind exact home/UID, one 64-hex nonce, prepare/commit phase, old full identity,
old boot/compositor/origin scope, exact old canonical metadata SHA256 inventory,
and (commit only) distinct full new identity, genuine extinction evidence and
retained prepare/creation/native/ingress/producer digests. The new authority is
the root transaction nonce; actual native incarnation remains native-generated.
Operator Control owns observations, record production, services and every
generation journal/config/authority mutation. Golden is a read-only consumer
of this protocol; it does not ship the generation writer.

`generation.json` is a bounded32KiB durable journal. Every intermediate phase
excludes all readers. `generation-history/<nonce>/` retains exact0400 original
pre-exclusion bytes AND drained final old views for authority, lease,
participants, baseline, recovery and grant, plus the exact commit authorization,
manifest and prior journal chain. Each view is bounded256KiB; history is capped
by the Operator writer without eviction. Stable activity/lease/participant lock and
state directory inodes never change. Conversations, credentials, browser data
and state.db are not read/copied/replaced. Operator Control must use the native config backend for
the four literal target bindings and preserve unrelated config and comments.
An interrupted writer must retain the exact nonce, exclusion and evidence;
Golden refuses intermediate journals rather than repairing or adopting them.

Generation DATA views may be archived and replaced ONLY by Operator Control
after root-proved extinction and activity drain. Missing old
per-client receipts stay null/unknown in immutable history; extinction creates
a distinct generation, never an old cleanup success. No lock/profile reset,
force flag, mode change, fresh profile, manual rename or caller clean assertion
is supported. Incomplete archive, mixed view, drift, cancellation, unknown
publication and exhausted bounds remain excluded, with no finally-release or
successor invalidation. Existing same-generation recovery/cancellation rules
are unchanged.

After excluded commit, new RFB participant reservation remains blocked until
the existing validators see a genuine current root origin, released actual
initial C baseline and committed grant. This keeps reconnecting passive viewers
out of the first zero-READY acquisition. Afterwards normal passive watching
while excluded remains available. Only existing `external-reconcile` can grant
Active through its real native barrier; root authorization/journal alone cannot.

External Start, Stop and Install are unsupported and refuse before lease,
installer or service actions. Closing the Screen pane closes only its transport;
it does not stop the provider/service. Native X launcher/sandbox/env records
conflict with external mode; changing to native while an external incarnation
exists also refuses. No X/Wayland environment, PID, launcher or sandbox marker is
fabricated, and no guest path, SSH command or service configuration is shared.

## Scope and targeting

The resource is one **dedicated isolated workstation VM/user graphical session**,
not a personal/client mixed compositor or seat. The stream is one current
1280x800 RFB viewport, **not all outputs at once**, a permanent monitor pin, or
an authenticated app/focus/output/scale identity. WayVNC `-o` is initial source
selection only. Same-size source/focus changes are not attested by stock RFB.
Operator custody of the guest/session/relay remains mandatory.

Only `app=None` or the generic alias `screen` is accepted. Named outputs,
named applications, PID/window/AX/SOM identities, background input, focus/raise,
bring-to-front, and bounded capability mode refuse honestly. `som` supplies real
pixels without invented overlays; AX capture returns an unsupported note.
Geometry changes, mode/generation/authority/inode changes, timeouts and broken
links terminally invalidate the current worker/observation. Never replay input.
Worker loss durably excludes its matching epoch; fresh capture cannot silently
reconnect under an old active lease or replay an uncertain action.

Every foreground action needs existing native approval and a fresh complete
visible capture, consumed once, at most 30 seconds old. The full pre-input
geometry refresh is not a new caller-observed capture. Maintained vncdotool owns
RFB framing; no custom release encoder, clipboard replacement, `wtype`, shell
injection, or hidden typing fallback exists. US shortcut semantics and finite
coordinates/text/events are inherited from the v2 provider. Input enqueue is
`effect=unverifiable`, not server/application success.

## Dependencies and budgets

Worker Python 3.11–3.14 must be the exact package-local `.venv/bin/python`.
`requirements.txt` pins vncdotool 1.4.2, Twisted 26.10.0, Pillow 13.0.0 and their
recorded transitive dependencies. The isolated worker also imports the real native
authority/config readers: ruamel.yaml 0.18.16 supplies `hermes_yaml`, and
python-dotenv 1.2.4 preserves config-backend bootstrap and managed/secret-scope
imports. Their pins come from the admitted core `uv.lock`; ruamel.yaml.clib
0.2.15 is pinned only for its required CPython <3.14 metadata branch. On 3.14
the native YAML reader supports the pure parser without that optional
acceleration package. Native/web processes import no optional worker
dependencies. No environment is installed by the carrier; the parent must stage
and qualify the private dependency environment under its own authority.
Availability is filesystem/metadata only; doctor imports in a bounded child
without RFB/network activity. Missing dependencies/config produce unavailable.

Each connected worker holds its **own** native activity SH from before lease
admission/dial through actual maintained `connectionLost` and bounded durable
proof settlement. Neither condition substitutes for the other. It pins the admitted
epoch and canonical identity and polls them on the reactor every 100 ms even
while stdin is idle. Revocation invalidates pending frames/successes and aborts
the transport, independent of the parent backend mutex/global cache lock. No
later epoch is adopted by an old stream. Fork children close inherited copies,
not LOCK_UN; descriptors are non-inheritable and child spawn uses close_fds.

Every owner reserves atomic bounded metadata before bind/dial, binds a unique
private pathname through ordinary AF_UNIX APIs, records actual kernel bytes and
sockaddr length, then resolves exactly one native client after the maintained
handshake. Twisted adopts the same FD through `adoptStreamConnection`; asyncio
uses `open_unix_connection(sock=...)`. The native filter remains the only human
RFB gate. Viewer handshake needs no lease and no activity SH. Input epoch is
persisted before first eligible input. A partial record blocks reconciliation;
it is never removed to manufacture success. The ledger is capped at 64 retained
records / 256 KiB; there is no eviction or automatic clean reprovisioning.
Native missing/expired/failed/changed-generation receipts remain unknown.
Ordinary stop/EOF/revocation/spontaneous loss starts one retained non-daemon
proof worker, using the existing participant writer and four-second native
barrier. No new input write can cross the local retirement fence. A failed/crashed
proof worker does not create a receipt. Human close uses the existing retained
upstream owner and an off-loop proof thread; a synchronous per-link guard settles
late canceled admission before proof reads the participant row. Close waiters,
writers, shared descriptors and proof errors stay owned across cancellation.

Each agent operation also uses a separate private endpoint operation EX lock for
serialization. It is subordinate to the same canonical identity and is not a
second permission system. Human input-bearing streams each own separate SH;
watchers need none and retain frames during human/quiescing/excluded states.

Budgets: native dial 2s; close then abort/close waiter at most 2s each; lease and
activity locks 3s (human SH is nonblocking); reactor revoke poll 100ms; provider
RPC 3s, full-frame proxy 8s, capture/init/geometry 30s, action 20s,
typing at most 256 characters in chunks of 32. Each chunk repeats full parent and
worker identity/lease/participant admission on the same bound connection, then
sends paired maintained key events in one short reactor turn without per-key
RPCs, authority rereads or sleeps. Every write still checks transport, geometry
and the local retirement fence; any failure terminally retires the worker with
no replay. Revocation polling resumes between chunks. These are session/lease
checks, not app/focus attestation. Each successful chunk acknowledges its actual
key-event count, never application delivery. Other key/pointer actions are unchanged;
capture TTL 30s, no connected-worker idle expiry (revocation remains polled), request 16KiB,
response 8MiB, framebuffer rectangles 4096. Parent retirement wait is bounded by
existing close2s + lease3s + native-proof4s budgets, then forced worker wait1s;
this prevents premature killing of owned proof work, not delayed-takeover grace
or increased native retention. Normal release uses maintained key/pointer calls. Cleanup
waits for actual connectionLost; a timeout, EOF or process exit is **not proof**
that WayVNC/Hyprland processed held-input releases. Failed close retains local
lock custody until process exit; no active publication follows EX alone.
Each native IPC request has a one-second total deadline and a 64 KiB reply cap;
per-client receipt queries share a four-second deadline. Aggregate polling and
release each have a separate three-second total caller deadline; native pending
timeout is 2000 ms, capacity one, first-terminal retention 60000 ms. These are
source wait bounds, not a wall-clock guarantee for scheduler/filesystem stalls.
Participant-lock acquisition is bounded at 3s and is nonblocking for first input.

## Privacy and qualification

External thumbnails are unavailable. Renderer cache clears on ownership,
phase/epoch/mode/endpoint identity changes and rejects late old-frame responses.
Authenticated live read-only human watchers retain native frame visibility.
External inbound clipboard paste/resize is dropped in the existing native RFB
filter; the UI reports clipboard unavailable, with no hidden typing fallback.
Explicit `paste` requests return `paste_unavailable`, never success or a typed-key
fallback. Ordinary `type` remains its documented maintained key-event operation.
Server-to-viewer clipboard suppression is host-owned, not implemented by
dropping inbound messages. The maintained `wl-veil` candidate
must hide both data-control globals and exclude unfiltered inherited Wayland
FD/reattach paths; supported Hyprland keyboard cleanup and held-button behavior
need the parent's real host/service and source-blind qualification. Historical
bounded producer/clipboard passes do not qualify this changed consumer or grant
current authority. No native exercise is claimed for this source packet.

The native source carrier does **not** build or update Electron. Parent must
review/regenerate/build/update the canonical native app separately, then verify
its rendered behavior. Whole participating-runtime migration is mandatory;
rolling mixed old/new lease/input semantics is not qualified. Source compilation,
manifest membership and carrier application are not live behavior proof.
