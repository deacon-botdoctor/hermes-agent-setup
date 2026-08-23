#!/bin/sh
set -eu

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STATE_FILE="$HERMES_HOME/state/client-selfheal-heartbeat-state.json"
LOG_FILE="$HERMES_HOME/logs/client-selfheal-heartbeat.log"
EXPECTED_GATEWAYS="${EXPECTED_GATEWAYS:-1}"
SERVICE_LABEL="${SERVICE_LABEL:-ai.hermes.gateway}"
REQUIRE_TELEGRAM="${REQUIRE_TELEGRAM:-1}"
STALE_SECONDS="${STALE_SECONDS:-86400}"  # 24h, bumped 2026-05-26 from 600 to eliminate false-positive cascade
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-900}"

mkdir -p "$HERMES_HOME/state" "$HERMES_HOME/logs"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_FILE"
}

json_probe() {
python3 - <<'PY2'
import json, os, time
from pathlib import Path
home = Path(os.environ['HERMES_HOME'])
state_path = home / 'gateway_state.json'
state = 'missing'
telegram = 'unknown'
age = 10**9
if state_path.exists():
    try:
        payload = json.loads(state_path.read_text())
        state = str(payload.get('gateway_state') or 'unknown')
        platforms = payload.get('platforms') or {}
        telegram = str((platforms.get('telegram') or {}).get('state') or 'unknown')
        updated_at = payload.get('updated_at')
        if updated_at:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(updated_at).replace('Z', '+00:00')).astimezone(timezone.utc)
            age = int(time.time() - dt.timestamp())
    except Exception:
        state = 'parse_error'
# HERMES_TELEGRAM_LIVENESS_v1 — extract polling-liveness age as a separate signal.
poll_age = 10**9
try:
    poll_at = (platforms.get('telegram') or {}).get('last_successful_poll_at')
    if poll_at:
        from datetime import datetime, timezone
        pdt = datetime.fromisoformat(str(poll_at).replace('Z', '+00:00')).astimezone(timezone.utc)
        poll_age = int(time.time() - pdt.timestamp())
except Exception:
    pass
print(f"{state}|{telegram}|{age}|{poll_age}")
PY2
}

count_gateways() {
  ps aux | egrep -i 'python.*gateway|hermes.*gateway|gateway.py' | egrep -v 'egrep|client-selfheal-heartbeat' | wc -l | tr -d ' '
}

read_last_attempt() {
python3 - <<'PY2'
import json, os
from pathlib import Path
path = Path(os.environ['STATE_FILE'])
if not path.exists():
    print(0)
    raise SystemExit
try:
    payload = json.loads(path.read_text())
    print(int(payload.get('last_attempt_epoch') or 0))
except Exception:
    print(0)
PY2
}

write_state() {
  python3 - <<'PY2'
import json, os, time
from pathlib import Path
path = Path(os.environ['STATE_FILE'])
payload = {
    'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'last_attempt_epoch': int(time.time()),
    'status': os.environ['WRITE_STATUS'],
    'reason': os.environ['WRITE_REASON'],
    'process_count': int(os.environ['WRITE_PROCESS_COUNT'] or 0),
}
path.write_text(json.dumps(payload, indent=2) + '\n')
PY2
}

remediate() {
  log "remediation requested: $1"
  if command -v launchctl >/dev/null 2>&1; then
    if [ -f "$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist" ]; then
      launchctl print "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist" >/dev/null 2>&1 || true
      launchctl kickstart -k "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || launchctl start "$SERVICE_LABEL" >/dev/null 2>&1 || true
      sleep 5
    fi
  fi
  if [ "$(count_gateways)" -lt "$EXPECTED_GATEWAYS" ]; then
    if [ -x "$HERMES_HOME/bin/hermes-gateway-start.sh" ]; then
      nohup "$HERMES_HOME/bin/hermes-gateway-start.sh" >> "$LOG_FILE" 2>&1 </dev/null &
      sleep 6
    elif [ -x "$HERMES_HOME/start-hermes.sh" ]; then
      nohup "$HERMES_HOME/start-hermes.sh" >> "$LOG_FILE" 2>&1 </dev/null &
      sleep 6
    fi
  fi
}


operator_codes() {
python3 - <<'PY2'
import json, os, re
from pathlib import Path
path = Path(os.environ['HERMES_HOME']) / 'state' / 'operator-status.json'
codes = []
try:
    payload = json.loads(path.read_text())
    for key in ('active_reasons', 'codes', 'blockers'):
        value = payload.get(key)
        if isinstance(value, list):
            codes.extend(str(item) for item in value if item)
    checks = payload.get('checks')
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict) and item.get('code'):
                codes.append(str(item['code']))
    reasons = payload.get('reasons')
    if isinstance(reasons, list):
        for item in reasons:
            if isinstance(item, dict) and item.get('code'):
                codes.append(str(item['code']))
    summary = str(payload.get('operator_summary') or '')
    match = re.search(r'blockers=([^\n"]+)', summary)
    if match:
        codes.extend(part.strip().strip('.') for part in match.group(1).split(';') if part.strip())
except Exception:
    pass
print(','.join(sorted(set(codes))))
PY2
}

operator_codes_soft_only() {
  OPERATOR_CODES="$1" python3 -c 'import os; soft={"gateway_state_stale","transcript_stale","stale_job_execution","client_friction_elevated","client_anger_elevated","auth_continuity_thin","dream_consolidation_broken","session_reset_noise","session_reset_chatter"}; codes={c.strip() for c in os.environ.get("OPERATOR_CODES", "").split(",") if c.strip()}; print("yes" if codes and codes <= soft else "no")'
} # HERMES_SOFT_SIGNAL_NO_RESTART_v2

run_operator_watch() {
  if ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  if [ ! -x "$HERMES_HOME/bin/operator-status.py" ] || [ ! -x "$HERMES_HOME/bin/operator-remediation.py" ]; then
    return 0
  fi
  python3 "$HERMES_HOME/bin/operator-status.py" --write --format json --hermes-home "$HERMES_HOME" >/dev/null 2>>"$LOG_FILE" || {
    log "operator-status probe failed"
    return 0
  }
  operator_status="$(python3 - <<'PY2'
import json, os
from pathlib import Path
path = Path(os.environ['HERMES_HOME']) / 'state' / 'operator-status.json'
if not path.exists():
    print('unknown')
    raise SystemExit
try:
    payload = json.loads(path.read_text())
    print(str(payload.get('overall_status') or 'unknown'))
except Exception:
    print('unknown')
PY2
)"
  if [ "$operator_status" = "healthy" ] || [ "$operator_status" = "unknown" ]; then
    return 0
  fi
  codes="$(operator_codes)"
  if [ "$(operator_codes_soft_only "$codes")" = "yes" ]; then
    log "operator-remediation skipped soft-only status=$operator_status codes=$codes"
    return 0
  fi
  log "operator-remediation requested status=$operator_status codes=$codes"
  python3 "$HERMES_HOME/bin/operator-remediation.py" --hermes-home "$HERMES_HOME" >/dev/null 2>>"$LOG_FILE" || log "operator-remediation failed status=$operator_status codes=$codes"
}

run_native_agent_continuity() {
  continuity="$HERMES_HOME/bin/native-agent-continuity.py"
  session_runner="$HERMES_HOME/bin/native-session-runner.py"
  baseline="$HERMES_HOME/bin/tenant-gbrain-baseline.py"
  contract="$HERMES_HOME/config/native-agent-continuity-v1.json"
  baseline_receipt="$HERMES_HOME/state/native-agent-continuity/baseline.json"
  manifest="$HERMES_HOME/state/native-agent-continuity/manifest.json"
  continuity_python="$HERMES_HOME/hermes-agent/venv/bin/python"
  if [ ! -x "$continuity_python" ]; then
    continuity_python="$(command -v python3 || true)"
  fi
  if [ -n "$continuity_python" ] && [ -x "$continuity" ] && [ -f "$manifest" ]; then
    if [ ! -f "$baseline_receipt" ]; then
      if [ ! -x "$baseline" ] || [ ! -f "$contract" ] || \
         ! "$continuity_python" "$baseline" apply --manifest "$manifest" --contract "$contract" --json >/dev/null 2>>"$LOG_FILE"; then
        log "native-agent tenant GBrain baseline activation failed"
        return 0
      fi
    fi
    if "$continuity_python" "$continuity" reconcile --manifest "$manifest" --json >/dev/null 2>>"$LOG_FILE"; then
      if [ -x "$session_runner" ]; then
        "$continuity_python" "$session_runner" --manifest "$manifest" --json >/dev/null 2>>"$LOG_FILE" || \
          log "native-agent session projection failed"
      fi
    else
      log "native-agent continuity reconcile failed"
    fi
  fi
}

export HERMES_HOME STATE_FILE
probe="$(json_probe)"
state_val="$(printf '%s' "$probe" | cut -d'|' -f1)"
telegram_val="$(printf '%s' "$probe" | cut -d'|' -f2)"
age_val="$(printf '%s' "$probe" | cut -d'|' -f3)"
# HERMES_TELEGRAM_LIVENESS_v1
poll_age_val="$(printf '%s' "$probe" | cut -d'|' -f4)"
[ -z "$poll_age_val" ] && poll_age_val=999999999
POLL_STALE_SECONDS="${POLL_STALE_SECONDS:-300}"
process_count="$(count_gateways)"
healthy=1
reason="healthy"

if [ "$process_count" -lt "$EXPECTED_GATEWAYS" ]; then
  healthy=0
  reason="gateway process count below expected"
elif [ "$state_val" != "running" ]; then
  healthy=0
  reason="gateway_state not running"
# DISABLED 2026-04-26 — gateway_state.json refreshed only on state
# transitions; periodic mtime check fires falsely. process_count +
# telegram_val are sufficient liveness signals.
# elif [ "$age_val" -gt "$STALE_SECONDS" ]; then
#   healthy=0
#   reason="gateway_state stale"
elif [ "$REQUIRE_TELEGRAM" = "1" ] && [ "$telegram_val" != "connected" ]; then
  healthy=0
  reason="telegram not connected"
fi

if [ "$healthy" = "1" ]; then
  export WRITE_STATUS=healthy WRITE_REASON="$reason" WRITE_PROCESS_COUNT="$process_count"
  write_state
  log "healthy process_count=$process_count state=$state_val telegram=$telegram_val age=$age_val"
  run_operator_watch
  run_native_agent_continuity
  exit 0
fi

# HERMES_TELEGRAM_LIVENESS_v1 — stale polling stamp implies wedged httpx pool.
if [ "$REQUIRE_TELEGRAM" = "1" ] && [ "$poll_age_val" -gt "$POLL_STALE_SECONDS" ] && [ "$reason" = "" ]; then
  reason="telegram_polling_stale(${poll_age_val}s>${POLL_STALE_SECONDS}s)"
fi

now_epoch="$(date +%s)"
last_attempt="$(read_last_attempt)"
if [ $((now_epoch - last_attempt)) -lt "$COOLDOWN_SECONDS" ]; then
  export WRITE_STATUS=cooldown WRITE_REASON="$reason" WRITE_PROCESS_COUNT="$process_count"
  write_state
  log "cooldown reason=$reason process_count=$process_count state=$state_val telegram=$telegram_val age=$age_val"
  exit 0
fi

remediate "$reason"
probe="$(json_probe)"
state_val="$(printf '%s' "$probe" | cut -d'|' -f1)"
telegram_val="$(printf '%s' "$probe" | cut -d'|' -f2)"
age_val="$(printf '%s' "$probe" | cut -d'|' -f3)"
# HERMES_TELEGRAM_LIVENESS_v1
poll_age_val="$(printf '%s' "$probe" | cut -d'|' -f4)"
[ -z "$poll_age_val" ] && poll_age_val=999999999
process_count="$(count_gateways)"
final_status=degraded
if [ "$process_count" -ge "$EXPECTED_GATEWAYS" ] && [ "$state_val" = "running" ] && [ "$age_val" -le "$STALE_SECONDS" ]; then
  if [ "$REQUIRE_TELEGRAM" != "1" ] || [ "$telegram_val" = "connected" ]; then
    final_status=recovered
  fi
fi
export WRITE_STATUS="$final_status" WRITE_REASON="$reason" WRITE_PROCESS_COUNT="$process_count"
write_state
log "$final_status reason=$reason process_count=$process_count state=$state_val telegram=$telegram_val age=$age_val"
run_operator_watch
exit 0
