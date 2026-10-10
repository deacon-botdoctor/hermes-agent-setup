# Browser Piloting

## Route before opening a browser

- Read/extract a public page, bulk scrape, or public fact-finding → `web_extract` / local Firecrawl first.
- Click, type, authenticate, upload, download behind login, or inspect interactive UI → the existing per-client `browser_*` harness.
- Known brittle workflow with a tested domain harness → query capability-router and prefer that harness.
- Never create a new browser/profile/daemon merely because the selected lane failed.

The generic interactive harness is per-client state. Its browser lane may be a dedicated automation profile or a personal profile that the principal has authorized. “Per-client” is the ownership and isolation boundary, not a requirement to keep the principal's own agent out of the principal's authorized browser. Never share a profile, cookies, tabs, credentials, or CDP endpoint with another client.

## Choose an authorized browser lane

- A clear request to do something in a web UI implicitly authorizes the configured client-isolated browser lane and an already connected principal-owned session needed for that task. Browser/tool selection is an implementation detail, not an approval boundary. Open, navigate, click, type, and read without asking whether to use the browser.
- If multiple principal-owned identities or profiles are available and the choice changes identity or scope, identify them without exposing credentials and ask only which identity to use. Do not ask whether browser use is allowed.
- Task-specific wording is task-scoped. “Use this by default,” “always,” or “going forward” creates standing authorization for matching browser work. Record only that authorization in the existing client-local preference surface, never a credential, cookie, or other secret.
- Within the authorized scope, existing sessions, saved-password autofill, and ordinary login submission are usable. Never reveal, export, transcribe, log, or move a credential to another surface.
- Windows Hello, biometrics, hardware-backed passkeys, unhandled MFA, ambiguous identity selection, and account recovery remain human-present steps. Authentication does not approve a consequential post-login action.

Browser is an action tool. Pilot it when the principal asks you to do something on the web.

## Pilot when
- Behind login in a profile the principal authorized
- Multi-step: form fills, clicks, navigation
- Downloads gated by auth or session
- Cookies, JS, or sequential interaction required

## Don't pilot when
- Reading a single page → Firecrawl
- Bulk scraping → Firecrawl
- Public fact-finding → Firecrawl

"Read X" = Firecrawl. "Do X for me" = pilot.

## Tab hygiene
Reuse a tab when possible. Close tabs you opened when the task is done. Tabs are session state, not exhaust — accumulation degrades the principal's browser. Native agent-browser sessions are task-owned and inactivity-reaped; a cleanup failure means diagnose the existing session, not spawn another browser.

## Recover from an obstruction

A covered window, dismissible popup, stale element reference, or missed click
is an observation to resolve, not proof that the task needs the principal.
Capture fresh state. Identify the intended tab/window and the obstruction.
Use the existing authorized control lane to dismiss a non-consequential popup,
select the task tab, or focus the target window when foreground delivery is
permitted. Verify the changed state, then continue the original task without
asking the principal to repeat the request or do an available agent action.
Do not close unrelated windows or type into an unverified target. A dialog
that grants consent, spends money, changes access, or requires human
verification keeps its existing approval boundary.

## Failure budget

After two unverified attempts at the same transition, stop repeating that
method. This is not a two-action limit for the whole task. Capture fresh state
and choose a materially different, supported recovery within the authorized
lane. Continue while verified state advances. If distinct supported recoveries
leave the same state unchanged, stop the loop and report the evidence, the
remaining prerequisite, and the smallest next action; do not invent new
variants of the same failed click.

Distinguish an interaction failure from a readiness or access failure. A failed
desktop driver does not establish that an independently configured browser
connector is unavailable. Use another existing, authorized route for the same
tenant and task only when it does not bypass the failed route's protected
boundary. Never retry an explicit access denial or create another browser,
profile, daemon, or custom input script to bypass readiness or approval checks.
Preserve completed work. Verify whether an external action succeeded before
retrying it.

## Human verification checkpoints

At a CAPTCHA, SMS/2FA, biometric, unusual-activity, device-bound, or other
human-verification checkpoint, finish every permitted surrounding step and put
the exact checkpoint on screen. State only that verification is ready; do not
ask permission to proceed or to use the browser. Retain state and resume
immediately after clearance. Never refuse the surrounding workflow because one
step needs a human, and never automate or bypass the human-verification step.

## Doctrine
Not a scraper. Interactive is in-scope. Do not decline or ask whether to use
the browser — execute, pausing only the exact human-present step.

## Tools
`browser_navigate`, `browser_click`, `browser_type`, `browser_snapshot`, `browser_select`, `browser_scroll`, `browser_wait`.
Domain harness present? (`mls.*`, `dotloop.*`, `td.*`, `fb.*`) → query capability-router, prefer those.
Tab tools: `tabs_close_orphans`, `tabs_close_by_url_match` (browser-tabs MCP) for cleanup.
