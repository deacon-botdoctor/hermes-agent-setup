# Firecrawl — web scraping/crawl, run locally

The agent's `web` backend is [Firecrawl](https://github.com/firecrawl/firecrawl): it fetches and
cleans web pages into model-ready text. You can point it at the hosted API or run it **locally**
with no API key and no per-request cost. Local is the default in `config/config.example.yaml`.

Firecrawl is a separate open-source project you install and run — this is the wiring, not a copy.

## Local self-hosted (default)

Run Firecrawl on your host and point the agent at it:

```bash
# 1. get Firecrawl and start it (Docker Compose is the supported path)
git clone https://github.com/firecrawl/firecrawl
cd firecrawl
cp apps/api/.env.example apps/api/.env    # defaults are fine for local use
docker compose up -d                       # brings up the API on :3002

# 2. confirm it's up
curl -s http://127.0.0.1:3002/test         # or the health path in their README
```

Then in your runtime config (already set in the example):

```yaml
web:
  backend: firecrawl
  firecrawl_url: http://127.0.0.1:3002
```

That's it — the agent's `web` tool now scrapes through your local instance. No key, no quota, and
the pages you crawl never leave your host.

## Hosted API (alternative)

If you'd rather not run it yourself, use the hosted service:

```yaml
web:
  backend: firecrawl
  firecrawl_url: https://api.firecrawl.dev
```

and set `FIRECRAWL_API_KEY` in your environment (never commit it). Same tool surface, someone
else runs the crawler.

## Which to pick

- **Local** — no cost, no key, data stays on your host, but you run and maintain the container.
- **Hosted** — zero-ops, but you're metered and pages transit their service.

For a client bundle that should work out of the box without signing up for anything, local is the
better default; switch the one `firecrawl_url` line to go hosted.
