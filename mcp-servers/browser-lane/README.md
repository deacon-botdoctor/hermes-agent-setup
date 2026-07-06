# browser-lane MCP

Clean-room client for a browser-lane daemon socket. The daemon owns Playwright/Chromium; this MCP
keeps the public bundle to the narrow wire protocol surface.

## Environment

- `BROWSER_LANE_SOCKET` defaults to `~/.hermes/browser-lane/daemon.sock`
- `BROWSER_LANE_TIMEOUT` defaults to `15`
- `BROWSER_CDP_URL` defaults to `http://127.0.0.1:9230`

## Tools

- `browser_lane_status()` reports socket and CDP endpoint readiness.
- `browser_lane_open(url)` asks the daemon to open a URL.
- `browser_lane_command(command, **params)` sends one JSON-line command to the daemon and returns
  its JSON-line response.

Run with:

```bash
PYTHONPATH=mcp-servers/browser-lane/src python -m browser_lane_mcp.server
```
