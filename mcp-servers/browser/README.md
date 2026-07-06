# browser MCP

Clean-room MCP surface for a running Chromium DevTools Protocol endpoint.

## Environment

- `BROWSER_CDP_URL` defaults to `http://127.0.0.1:9230`
- `BROWSER_MCP_TIMEOUT` defaults to `10`

## Tools

- `browser_status()` reads `/json/version`.
- `list_targets()` reads `/json/list`.
- `cdp_json(path)` reads a JSON CDP HTTP endpoint.

Run with:

```bash
PYTHONPATH=mcp-servers/browser/src python -m browser_mcp.server
```
