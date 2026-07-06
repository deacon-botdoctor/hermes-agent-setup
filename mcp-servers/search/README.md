# search MCP

Clean-room MCP surface for search plus page scraping.

## Backends

- `SEARXNG_URL` defaults to `http://127.0.0.1:8080`
- `FIRECRAWL_URL` defaults to `http://127.0.0.1:3002`
- `FIRECRAWL_API_KEY` is optional for hosted Firecrawl
- `SEARCH_MCP_TIMEOUT` defaults to `20` seconds

## Tools

- `search(query, max_results=8)` searches SearXNG and returns normalized results.
- `scrape_url(url, only_main_content=True)` scrapes a page through Firecrawl.
- `search_status()` reports configured backend endpoints without exposing secrets.

Run with:

```bash
PYTHONPATH=mcp-servers/search/src python -m search_mcp.server
```
