#!/bin/bash
# start-hermes.sh — Single source of truth wrapper
# Reads Anthropic token from auth-profiles.json (canonical store)
# and exports as ANTHROPIC_TOKEN before launching Hermes gateway.
set -euo pipefail

AUTH_PROFILES="$HOME/.hermes/auth-profiles.json"
LOG="$HOME/.hermes/logs/start-hermes.log"
mkdir -p "$(dirname "$LOG")"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [start-hermes] $1" >> "$LOG"; }

# Read token from auth-profiles.json (anthropic:default)
if [ -f "$AUTH_PROFILES" ]; then
    TOKEN=$(python3 -c "
import json, sys
with open(\"$AUTH_PROFILES\") as f:
    d = json.load(f)
p = d.get(\"profiles\", {})
# Try lastGood first, then default
lg = d.get(\"lastGood\", {}).get(\"anthropic\", \"anthropic:default\")
t = p.get(lg, {}).get(\"token\", \"\")
if not t:
    t = p.get(\"anthropic:default\", {}).get(\"token\", \"\")
print(t)
" 2>/dev/null || true)
fi

if [ -n "${TOKEN:-}" ]; then
    export ANTHROPIC_TOKEN="$TOKEN"
    log "Loaded token from auth-profiles.json"
else
    log "WARNING: No token found in auth-profiles.json — Hermes will use other env vars"
fi

# Source .env for non-token vars (TELEGRAM_ALLOWED_USERS, etc.)
if [ -f "$HOME/.hermes/.env" ]; then
    set -a
    while IFS= read -r line; do
        [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
        key="${line%%=*}"
        # Skip ANTHROPIC_TOKEN — we already set it from auth-profiles
        [ "$key" = "ANTHROPIC_TOKEN" ] && continue
        eval "export $line" 2>/dev/null || true
    done < "$HOME/.hermes/.env"
    set +a
fi

# Keep stock PDF MCP output deliverable after retiring the source patch that
# hard-coded ~/.hermes/state/pdfs into gateway media validation.
export HERMES_MEDIA_ALLOW_DIRS="$HOME/.hermes/state/pdfs${HERMES_MEDIA_ALLOW_DIRS:+,$HERMES_MEDIA_ALLOW_DIRS}"

# Keep host-local context and tool execution out of an immutable runtime
# candidate. An explicit client-local TERMINAL_CWD still wins.
export TERMINAL_CWD="${TERMINAL_CWD:-$HOME/.hermes}"

log "Starting Hermes gateway..."
cd "$HOME/.hermes"

# Phase 1 tool-doctor boot self-test. This is intentionally best-effort:
# gateway startup must not block on a broken MCP server, and report consumers
# read ~/.hermes/state/tool-health.json instead of scraping this log.
if [ -x "$HOME/.hermes/bin/tool-doctor.py" ]; then
    (
        TOOL_DOCTOR_MCP_TIMEOUT="${TOOL_DOCTOR_BOOT_SERVER_TIMEOUT:-8}" \
            "$HOME/.hermes/hermes-agent/venv/bin/python3" \
            "$HOME/.hermes/bin/tool-doctor.py" probe --write-health --json \
            >> "$HOME/.hermes/logs/tool-doctor-boot.log" 2>&1 || true
    ) &
    log "Started tool-doctor boot self-test in background"
fi

exec "$HOME/.hermes/hermes-agent/venv/bin/python3" \
     "$HOME/.hermes/hermes-agent/venv/bin/hermes" \
     gateway run --replace
