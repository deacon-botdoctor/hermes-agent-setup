# Visual review contract

Create one JSON object bound to the gate's mechanical pass:

```json
{
  "schema_version": 1,
  "artifact_sha256": "64 lowercase hex characters",
  "preview_sha256s": ["one hash per rendered preview, in any order"],
  "brand_context_sha256": "64 lowercase hex characters",
  "inspected_at": "2026-08-16T08:00:00Z",
  "reviewer": "agent or human reviewer identity",
  "review_frameworks": ["impeccable-craft-floor", "human-taste"],
  "repair_round": 0,
  "scores": {
    "cognitive_load": {"score": 4, "evidence": "Visible hierarchy has one entry point."},
    "visual_coherence": {"score": 4, "evidence": "Spacing and type repeat consistently."},
    "interaction_clarity": {"score": 4, "evidence": "CTA and reading order are unambiguous."},
    "context_fit": {"score": 4, "evidence": "Density and tone match the audience."},
    "restraint": {"score": 4, "evidence": "Secondary material recedes."},
    "emotional_response": {"score": 3, "evidence": "The intended trust signal is present."}
  },
  "craft_floor": {
    "contrast": {"status": "PASS", "evidence": "Body text contrast was checked on the render."},
    "spacing": {"status": "PASS", "evidence": "Groups and section gaps are even."},
    "type": {"status": "PASS", "evidence": "Real copy does not clip or overflow."},
    "coverage": {"status": "PASS", "evidence": "Every brief requirement is visible."},
    "restraint": {"status": "PASS", "evidence": "No unearned visual effects or lazy card scaffold."},
    "channel_fit": {"status": "PASS", "evidence": "The artifact follows its delivery-channel rules."}
  },
  "client_overlay": {
    "status": "PASS",
    "source_sha256": "same value as brand_context_sha256",
    "checks": [
      {"status": "PASS", "evidence": "Color, type, voice, and locked layout match the client context."}
    ]
  },
  "findings": [],
  "decision": "PASS"
}
```

Rules:

- Scores are integers from 1–5 with nonempty visible evidence.
- Weighted Human Taste score is `(cognitive_load×3 + visual_coherence×3 + interaction_clarity×3 + context_fit×2 + restraint×2 + emotional_response) / 14`.
- Pass requires weighted score ≥4.0; cognitive load, visual coherence, context fit, and restraint ≥4; interaction clarity and emotional response ≥3.
- Every craft-floor and client-overlay check must be `PASS` with evidence.
- Findings are objects with `severity` (`critical`, `major`, or `minor`) and `text`. Any critical or major finding blocks pass.
- `repair_round` is 0, 1, or 2. The artifact stays blocked after an unresolved second round.
- The gate accepts only a review no older than 48 hours and exact byte hashes for the artifact, all previews, and brand context.
- Email/newsletter reviews must not list `design-taste-frontend` or `gpt-taste`.
