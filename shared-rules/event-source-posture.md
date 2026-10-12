# Event-source posture

External event sources use deterministic scripts; the conversational agent owns the result.

- Declare each required source in the tenant's seed manifest. Keep provider URLs, account identifiers, credentials, and secrets in the tenant-owned runtime.
- Prefer provider events or webhooks over polling when the provider supports them. A poller is an explicit fallback, not the default.
- Bind each source to its tenant-owned script and operational route, not a standing specialist. The conversational agent handles decisions and exceptions.
- Routine success is silent. Exceptions go to the declared operational route; source events do not directly notify the operator.
- The tenant adapter owns provider configuration and live readback. Golden only evaluates its secret-free readiness receipt.
- Activation requires a recent receipt bound to the exact seed manifest and a hashed tenant source instance, plus proof of subscribed-event coverage, source authenticity, live delivery, durable enqueue, and idempotent handling. Never infer readiness from configuration presence alone.
