#!/bin/bash
# Compatibility entry point: preserve live sessions and use the shared retention owner.
set -euo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
exec /bin/bash "$HERMES_HOME/bin/hermes-disk-monitor.sh"
