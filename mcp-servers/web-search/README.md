# web-search MCP

Clean-room web-search MCP surface for the shared client floor. It intentionally stays separate
from `search` because real client runtimes configure both MCP servers.

## Backends

- `SEARXNG_URL` defaults to `http://127.0.0.1:8080`
- `FIRECRAWL_URL` defaults to `http://127.0.0.1:3002`
- `FIRECRAWL_API_KEY` is optional for hosted Firecrawl
- `WEB_SEARCH_MCP_TIMEOUT` defaults to `20` seconds

## Tools

- `web_search(query, max_results=8)` searches SearXNG and returns normalized results.
- `scrape(url, only_main_content=True)` scrapes a page through Firecrawl.
- `web_search_status()` reports configured backend endpoints without exposing secrets.

Run with:

```bash
PYTHONPATH=mcp-servers/web-search/src python -m web_search_mcp.server
```
