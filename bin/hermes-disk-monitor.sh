#!/bin/bash
# Compatibility entry point. The existing retention owner controls all deletion.
set -euo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [[ ! -e "$HERMES_HOME/state/runtime-binding.json" && ! -L "$HERMES_HOME/state/runtime-binding.json" ]]; then
    printf '%s\n' '{"status":"skipped","reason":"runtime_binding_absent","deleted_count":0}'
    exit 0
fi
python=$(python3 - "$HERMES_HOME/state/runtime-binding.json" <<'PY'
import json
import sys
from pathlib import Path
binding = Path(sys.argv[1])
if not binding.is_file() or binding.is_symlink():
    raise SystemExit("disk retention requires a verified runtime binding")
value = json.loads(binding.read_text()).get("runtime_python", "")
if not value or not Path(value).is_file():
    raise SystemExit("disk retention runtime Python is unavailable")
print(value)
PY
)
exec "$python" "$HERMES_HOME/bin/hermes-disk-retention.py" \
    --home "$HOME" --hermes-home "$HERMES_HOME" --apply --json
