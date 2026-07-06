# telegram-admin MCP

Clean-room directory lookup for Telegram delivery metadata. It does not call Telegram directly;
it reads a local scrubbed directory file and returns only routing-safe fields.

## Environment

- `TELEGRAM_DIRECTORY` defaults to `telegram-directory.json`

## Tools

- `telegram_admin_lookup(query, limit=8)` searches channel/topic metadata.
- `telegram_admin_status()` reports directory path and count.

Run with:

```bash
PYTHONPATH=mcp-servers/telegram-admin/src python -m telegram_admin_mcp.server
```
