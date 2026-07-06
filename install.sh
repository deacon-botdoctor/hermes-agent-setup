#!/usr/bin/env bash
# install.sh — wire this overlay onto a runtime, the safe way.
#
# Runs the pipeline in order: rehearse against a pristine copy first, and only apply to the real
# runtime once rehearsal is green. Nothing is written to your runtime until the overlay has
# proven it applies cleanly.
#
# Usage:
#   ./install.sh --runtime /path/to/runtime --upstream /path/to/pristine-checkout
#
#   --runtime    the live runtime tree to overlay (a copy of the pristine one, same version)
#   --upstream   a clean, unmodified checkout of the same version (used for rehearsal)
#   --apply      actually write to --runtime (default is rehearse-only, so a bare run is safe)
set -euo pipefail

RUNTIME="" ; UPSTREAM="" ; DO_APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --runtime)  RUNTIME="$2"; shift 2;;
    --upstream) UPSTREAM="$2"; shift 2;;
    --apply)    DO_APPLY=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"

[ -n "$UPSTREAM" ] || { echo "need --upstream <pristine checkout>"; exit 2; }

echo "==> 1/3  config"
echo "    Copy config/config.example.yaml into your runtime config and fill the <placeholders>."
echo "    Redaction first. See docs/features.md for the why behind each key."

echo "==> 2/3  rehearse (verify the overlay applies to this exact version)"
"$PY" "$HERE/overlay/rehearse.py" --upstream "$UPSTREAM"

echo "==> 3/3  apply"
if [ "$DO_APPLY" -eq 1 ]; then
  [ -n "$RUNTIME" ] || { echo "need --runtime to apply"; exit 2; }
  echo "    rehearsal was green; applying to $RUNTIME"
  "$PY" "$HERE/overlay/apply.py" --hermes-dir "$RUNTIME"
  echo
  echo "    Plugins: install the packages under plugins/ where your runtime discovers user"
  echo "    plugins, then restart the runtime so it picks up config + overlay + plugins."
else
  echo "    (rehearse-only run — re-run with --apply --runtime <tree> to write)"
fi

echo "==> done"
