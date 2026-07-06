# telegram-admin MCP

Clean-room directory lookup for Telegram delivery metadata. It does not call Telegram directly;
it reads a local scrubbed directory file and returns only routing-safe fields.

## Environment

- `TELEGRAM_DIRECTORY` defaults to `telegram-directory.json`

## Directory shape

The file may be either a top-level channel list or `{"channels":[...]}`. Results are scrubbed to
`id`, `name`, `purpose`, `thread_key`, `message_thread_id`, `boundary_class`, and `client_lock`;
hidden fields are neither returned nor searched.

## Tools

- `telegram_admin_lookup(query, limit=8)` searches channel/topic metadata.
- `telegram_admin_status()` reports directory path and count.

Run with:

```bash
PYTHONPATH=mcp-servers/telegram-admin/src python -m telegram_admin_mcp.server
```
