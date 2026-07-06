# visual-identity MCP

Clean-room manifest-backed visual asset lookup for the shared client floor.

## Environment

- `VISUAL_IDENTITY_MANIFEST` defaults to `visual-assets.json`
- `VISUAL_IDENTITY_ROOT` defaults to the current directory

## Manifest shape

```json
{"assets":[{"id":"logo-primary","label":"Primary logo","kind":"logo","tags":["logo"],"path":"logos/logo.png"}]}
```

## Tools

- `visual_identity_search(query, limit=8)` searches asset metadata.
- `visual_identity_get(asset_id)` returns one scrubbed asset.
- `visual_identity_status()` reports manifest path and asset count.

Run with:

```bash
PYTHONPATH=mcp-servers/visual-identity/src python -m visual_identity_mcp.server
```
