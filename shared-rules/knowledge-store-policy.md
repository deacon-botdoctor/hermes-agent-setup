# Knowledge Store Policy — GBrain + Native Hermes Memory

## Contract

- **GBrain** is the durable filed knowledge store. Put verified reports, source documents, decisions, research outputs, and other long-lived artifacts there.
- **Native Hermes memory** is the bounded conversational store. Put durable user preferences in `USER.md` and nearby continuity in `memories/MEMORY.md`, within the configured character limits.

Do not install, invoke, or route memory through Anamnesis, Qdrant, or another parallel worker-memory stack. Do not duplicate full reports or transcripts into native memory; keep a concise pointer to the canonical GBrain page only when ordinary turns need it.

If GBrain is unavailable, surface the lookup/write blocker. Native memory remains available for bounded continuity but is not a substitute for filed durable knowledge.
