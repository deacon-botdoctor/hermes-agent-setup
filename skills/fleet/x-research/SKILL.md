---
name: x-research
description: Search X (Twitter) posts, accounts and replies through TReg, without Grok.
tags: [research, x, twitter, treg, social]
---

# X research through TReg

Use this when current X (Twitter) posts, an account's recent posts, a post's
replies, or a public profile matter to the task. It reads public data only. It
never posts, follows, likes or logs in.

The native `x_search` tool is disabled on every fleet runtime. It only works
through xAI Grok, and Grok is reserved for Grok Bot (the operator, 2026-11-08 and
2026-11-16). Do not re-enable it or reach for an xAI key.

## Route

Use the runtime's own TReg connection: the `treg` MCP server, which may need
on-demand activation with `restart_mcp_server(server_name="treg")`. Call
`call` with one of these routed endpoints. TReg tries the cheapest working
provider first and needs no X account or API key.

| Need | `endpoint_id` | `params` |
|---|---|---|
| Posts matching a keyword or query | `treg.x.search.posts` | `{"q": "<words or X search syntax>"}` |
| An account's recent posts | `treg.x.user.posts` | `{"username": "<handle without @>"}` |
| Replies to a post | `treg.x.post.comments` | `{"tweet_id": "<post id>"}` |
| A public profile | `treg.x.user.profile` | `{"username": "<handle>"}` |

A call costs about $0.0005–$0.001 (measured Nov 16, 2026: a sample political
search returned 20 same-day posts for $0.00049). When parameters are unclear,
read `catalog_get(<endpoint_id>)` first. Page with the returned `next_cursor`.
Each answer is `{output, raw, _treg}`; `_treg.served_by` names the provider
that answered.

## Rules

- Keep each post's URL (`https://x.com/<user>/status/<id>`), author, time and
  text before you summarize. Distinguish what a post says from your inference.
- An empty `posts` list is a miss, not proof that nobody posted. Rephrase once
  or widen the date window before you report nothing found.
- Post engagement and follower counts are snapshots at retrieval time.
- If TReg is unavailable on this runtime, report the X leg as blocked with the
  returned error. Do not borrow another runtime's credentials.
