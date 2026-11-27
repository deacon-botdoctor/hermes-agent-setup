#!/usr/bin/env bash
set -euo pipefail

if [ -f "$HOME/.hermes/bin/load-keychain-env.sh" ]; then
  # shellcheck disable=SC1091
  source "$HOME/.hermes/bin/load-keychain-env.sh" >/dev/null 2>&1
fi

if [ -z "${COMPOSIO_API_KEY:-}" ]; then
  echo "COMPOSIO_API_KEY is not set" >&2
  exit 1
fi

: "${COMPOSIO_MCP_URL:?set COMPOSIO_MCP_URL to your own Composio MCP server URL}"

exec /opt/homebrew/bin/node "$HOME/.hermes/vendor/composio-mcp/node_modules/@composio/mcp/dist/index" start --url "$COMPOSIO_MCP_URL"
