"""Hermes unified search MCP.

One comprehensive research surface that bundles:
  - the FREE local stack (SearXNG keyword search + Firecrawl page-fetch),
  - the PAID Exa neural/semantic escalation layer,
  - login-walled archive-and-read via the Internet Archive (Wayback),
  - primary-source claim verification + an immutable audit trail,
  - the state-externalizing research harness (candidate pool, dedupe,
    quality-curation, verification records, durable artifacts).

Supersedes the prior split servers: web-search, deep-search, exa, verification,
wayback. Free-first by policy; Exa is selected per call via a mode parameter.
"""
