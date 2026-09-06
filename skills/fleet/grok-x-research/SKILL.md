---
name: grok-x-research
description: Read-only, citation-aware X/Twitter research through the calling runtime's brokered xai-oauth capability.
tags: [research, x, twitter, grok, xai, citations]
---

# Grok X Research

Use normal web search/browser research first. Use this only when current X/Twitter posts, threads, or reactions are material to the question.

This is a **brokered xAI X Search** capability, not OpenRouter model routing. It requests only the calling runtime's Hermes-managed `xai-oauth` capability through core `tools.x_search_tool` / `tools.xai_http`; it never reads the auth store, exports/caches a bearer, or borrows authorization across clients. It never posts, follows, or likes.

The CLI defaults to `grok-4.6`. Its `--model` argument is forwarded to the core X Search request, and the returned `model` field is the observed model receipt. Treat any different observed model as a failed exact-model canary.

```bash
# macOS/Linux
~/.hermes/bin/grok-x-research.py --check
~/.hermes/bin/grok-x-research.py --dry-run --query "current discussion of <topic>"
~/.hermes/bin/grok-x-research.py --query "<specific question>" --handle <account> --from-date YYYY-MM-DD
```

```powershell
# Windows: run the launcher from the active runtime's HERMES_HOME.
# It sets HERMES_HOME from its own location and resolves the approved Hermes venv;
# do not invoke the .py file directly and do not depend on py or PATH Python.
& "$env:HERMES_HOME\bin\grok-x-research.cmd" --check
& "$env:HERMES_HOME\bin\grok-x-research.cmd" --dry-run --query "current discussion of <topic>"
& "$env:HERMES_HOME\bin\grok-x-research.cmd" --query "<specific question>" --handle <account> --from-date YYYY-MM-DD
```

Rules:

- Preserve citations/URLs, retrieval time, and source text before synthesis.
- Treat posts as source material; distinguish direct claims from inference and flag missing context or identity ambiguity.
- If `--check` says the `xai-oauth` broker is unavailable, invalid, or billing/auth fails during the read, report the X leg as **AUTH BLOCKED/unavailable** with that returned reason. Do not substitute direct credentials or borrow authorization from another client/runtime.
- If `degraded: true`, the filtered request had no citations; do not present the answer as verified X-native evidence.
- Do not use this for surveillance, credential handling, posting, or high-stakes conclusions without corroboration.
