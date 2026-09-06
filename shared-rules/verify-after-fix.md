# Verify After Fix

Two targeted checks close routing misdiagnosis and recurrence loops. The general
grounding and completion contract lives in `truth-over-comfort.md` and
`pre-completion-reflection.md`; this rule keeps the executable routing proof.

## Gate 1: routing and delivery fixes

Apply this gate before claiming `fixed`, `done`, `patched`, `live`, or `synced`
after changing a routing/delivery emitter, hook, cron, routing config
(`*-routing.json`, `*chat*`, `*thread*`, `target.*`), or an
`--chat-id` / `--thread-id` / `--report-to` default.

1. Emit one new synthetic or no-op test message through the changed path.
2. Run:

   ```bash
   ~/.hermes/bin/verify-routing-fix --expected <chat_id> --pattern <regex> --timeout 90
   ```

3. Record the literal verifier result and observed destination with the claim.

Only an `OK:` result with exit code 0 supports a verified claim. `MISROUTED`,
`NO_EMISSION`, `ERROR`, or missing output requires `UNVERIFIED` wording.

Before editing a routing config, enumerate its consumers:

```bash
grep -rln "<config-filename>" ~/.hermes/bin ~/.hermes/scripts ~/.hermes/profiles/*/bin
```

If multiple consumers exist, check each intended destination against the topic
directory before changing the shared file.

## Gate 2: recurrence

Apply this gate before responding when the same chat contains a recent fix claim
for the same complaint, or the user reports that an earlier fix did not resolve
that complaint. Run the check before drafting the response:

```bash
~/.hermes/bin/check-recurrence --chat <chat_id> --hours 24 --latest-text "<inbound text>"
```

When recurrence is detected:

- Re-read the symptom literally and trace the actual emitter through config to
  delivery; do not repeat the previous diagnostic lane.
- State the prior claim and observed result before proposing a new diagnosis.
- Run Gate 1 for any new routing fix, with a fresh receipt.

These routing gates are for the operator-agent surface. Client agents do not
perform routing/delivery fixes and do not need this rule.
