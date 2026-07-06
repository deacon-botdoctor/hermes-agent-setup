# anamnesis MCP

Clean-room local memory MCP for the shared client floor. It stores durable memory rows in a local
SQLite FTS database.

## Environment

- `ANAMNESIS_DB` defaults to `~/.hermes/state/anamnesis.db`

## Tools

- `memory_record(content, kind="memory", source=None)` stores one memory.
- `memory_search(query, limit=8)` searches memory content.
- `memory_status()` reports database path and count.

Run with:

```bash
PYTHONPATH=mcp-servers/anamnesis/src python -m anamnesis_mcp.server
```
