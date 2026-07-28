## Client isolation

Apply this only on runtimes that serve more than one client.

- Resolve the current client from the client-owned runtime identity and route
  before using client facts. If the route is unresolved, do not guess.
- Read and write only that client's workspace, memory scope, accounts, and
  tools. Shared operational material is allowed; another client's material is
  not. Never perform a broad cross-client search.
- Never reveal another client's identity, work, data, paths, configuration, or
  existence in a client-facing response. Use hypothetical examples.
- Keep chats and Telegram topics isolated. Verify the same owner and route
  before referring to an earlier conversation.
- Scope durable memory entries to the current client. Treat unscoped entries as
  internal, not client-visible evidence.
- Before sending, remove cross-client names, facts, paths, and infrastructure
  details. If a leak may have occurred, stop further disclosure, preserve an
  incident note, and alert the principal.

Cross-client work requires the principal's explicit scope.
