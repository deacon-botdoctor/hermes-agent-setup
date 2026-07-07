# Overlay features — what I keep and why

Each feature below is something the stock runtime doesn't do (or doesn't do the way I need).
They're grouped by change-ladder rung. For each: **what it does**, **why I run it**, and **what
happens if you skip it**. If you don't have the problem it solves, don't add it.

The guiding metric: an overlay feature has to earn its line count. A feature that solves a
problem you'll never hit is negative value — it's one more thing to break on an upstream bump.

---

## Security / data-loss (never cut these)

### Secret redaction at the write boundary
**Rung:** patch (no upstream hook exists at the persistence layer)
**What:** Every message is scrubbed for secrets *before* it's persisted to the transcript
store. It walks the whole message — content, tool-call arguments, reasoning fields, nested
multimodal parts — and redacts anything shaped like an API key, bot token, or other credential.
A strict "survivor" pass catches secrets embedded in escaped JSON strings that a naive
word-boundary redactor misses.
**Why:** Tool output routinely contains secrets — an agent that reads a `.env` file, dumps a
keychain entry, or prints an auth response will otherwise write that verbatim into the
transcript. The transcript feeds back into the model's context on the next turn, so an
un-redacted secret doesn't just sit on disk, it gets re-sent to the LLM every turn and can be
echoed back. This came from a real incident: two live API keys landed in a transcript because
the write path had no redaction pass.
**Skip it and:** any secret a tool ever touches is one prompt away from being persisted and
re-surfaced.
**Design note:** I keep the redaction *logic* in a single owned module and wire it in at the
one place all transcript writes funnel through, rather than scattering the logic across every
write site. The logic is code I own (a file, not an anchored source edit); only the thin
wiring is version-fragile. See [philosophy.md](philosophy.md#own-the-logic).

### Display-stream redaction
**Rung:** patch
**What:** The same redaction, applied to spinner/status text before it can be captured into
logs.
**Why:** Secrets leak into log files the same way they leak into transcripts. Defense in depth.
**Skip it and:** your log files become a second copy of anything sensitive the agent displayed.

---

## Durable execution & clean restarts

### Durable runtime
**Rung:** patch (deep runtime behavior, no seam)
**What:** Long-running agent work survives a gateway restart — in-flight workloads are tracked
and resumed rather than lost.
**Why:** A restart (deploy, crash, OOM, host reboot) mid-task otherwise drops the work on the
floor. For an always-on agent doing multi-minute jobs, that's the difference between "picks up
where it left off" and "silently forgot what it was doing."
**Skip it and:** every restart is a lost task.

### Clean-restart resume scheduler
**Rung:** patch (one anchor)
**What:** A single scheduler owns the decision of what to do on startup — replay a queued
message, resume an interrupted turn, clear a stale resume marker, or (most often) do nothing.
It treats leftover breadcrumbs as *evidence*, not as a trigger.
**Why:** This started as ~7 separate patches that each hooked a different corner of the restart
path. Fragmenting one feature (durable resume) across seven anchored edits meant seven things
to re-anchor on every bump — and the interaction between them caused a restart-churn loop
(measured at dozens of restarts/day) where the agent kept "resuming" itself. Consolidating the
whole decision into one scheduler both cut the anchor count and *fixed the churn*, because the
policy is now in one place you can reason about and test.
**Skip it and:** if you don't need durable resume at all, you don't need this. If you do, don't
build it as scattered guards — build it as one decision.
**Lesson worth stealing:** when your patch count is high, look for one *feature* that's been
fragmented into many patches. Consolidating it is both the count reduction and the quality win.

### Active-task anchor
**Rung:** patch
**What:** Keeps the current task pinned so it survives context compression — the thing the user
just asked for doesn't get summarized away when the context window fills.
**Why:** Long conversations get compressed to fit the window. Naive compression can drop the
*active* task along with the stale history, and the agent "forgets" mid-job. This anchors it.
**Skip it and:** long sessions occasionally lose the thread right when they're busiest.

---

## Delivery & UX

### Media path normalization + reply media
**Rung:** patch (gateway leg) + planned plugin (adapter leg)
**What:** A planned adapter hardening surface. `plugins/telegram_platform` is currently a disabled
placeholder, so reply-media, media timeout, liveness, and PDF/document ingest hardening are not
active until it wraps the real bundled Telegram adapter.
**Why:** Reliable image/file/PDF delivery. **Caution from experience:** most media-delivery
problems are *not* a missing feature — the platform delivers media fine by default. Before
adding media machinery, check that some *other* overlay isn't corrupting the media path (e.g. a
sanitizer stripping the tags that mark an attachment). I spent real effort building
media-delivery "fixes" that were actually working around a bug I'd introduced elsewhere. Fix
the corruption, then see what media handling you still need.
**Skip it and:** possibly nothing — test the default first.

### Client-friendly long-running checkpoints
**Rung:** patch (plugin-routable — a candidate to move)
**What:** Emits periodic progress messages during long tasks so the user isn't staring at
silence.
**Why:** A multi-minute job with no output reads as "hung." Checkpoints keep it legibly alive.
**Skip it and:** long tasks feel frozen to the user.

---

## Tooling

### Web CLI hardening
**Rung:** patch (modifies the built-in web tool's internals)
**What:** Small hardening of the CLI's web server / tool config.
**Why:** If you expose the web tooling, you want the sane limits on. This one stays a patch
because it tweaks core internals of a built-in tool — there's no provider seam for it.
**Skip it and:** the web tool runs with looser defaults than I like.

### Custom session search
**Rung:** should be a plugin (`register_tool`)
**What:** A tool that searches prior session transcripts scoped to the current topic.
**Why:** Recall — "what did we decide about X last week" without re-explaining. This is a
personal preference feature I own.
**Best form:** a custom tool belongs in a `register_tool` plugin, not a source patch. If you
build it, build it as a plugin.

---

## Housekeeping / indexing

### Skip shipped skill archives
**Rung:** patch (no config knob upstream)
**What:** Excludes shipped/archived skill directories from the skill scan.
**Why:** Otherwise the agent indexes a pile of bundled/archived skills it will never use,
crowding the skill list.
**Skip it and:** a noisier skill index.

### Skill index allowlist
**Rung:** patch → wants to be config
**What:** A curated list of which skills the agent actually surfaces.
**Why:** A tight, relevant skill list beats an alphabetical dump. Start from a small core set
and grow it deliberately.
**Skip it and:** the agent sees every skill, useful or not.

### Visual reference library
**Rung:** sidecar + skill
**What:** Bundles `bin/visual-reference-lookup`, `bin/visual-reference-qa`, and the
`skills/curated/visual-reference-library` registry.
**Why:** Visual artifacts, UI, reports, diagrams, and social creatives should start from
client-safe design patterns, required inputs, and QA gates instead of generic prompt slop.
**Skip it and:** agents can still create visuals, but they lose the reusable taste/QA lookup lane.

---

## The floor

After all the deleting, config-routing, and plugin-moving, what's left is a small set of
genuinely irreducible patches: durable runtime, the resume scheduler's one wiring anchor, the
redaction wiring, the web-tool hardening, and a couple of continuity features. Everything else
either became config, moved to a plugin, or turned out to be scar tissue around a bug that
should just be fixed. That small floor is the goal — it's what makes upstream bumps boring.
