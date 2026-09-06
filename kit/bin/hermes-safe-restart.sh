#!/bin/bash
# hermes-safe-restart.sh — safe gateway restart
#
# Profiles:
#   doc      Linux/Spark: systemd unit hermes-doc (Doc agent, with compile-gate + sanity checks)
#   gateway  Platform-detected client gateway (Linux/systemd or macOS/launchd)
#
# Usage: hermes-safe-restart.sh {doc|gateway|<profile-name>}
set -euo pipefail

export HOME="${HOME:-$(eval echo ~"$(id -un)")}"
HERMES_HOME="$HOME/.hermes"
AGENT_ROOT="$HERMES_HOME/hermes-agent"
PYTHON="$AGENT_ROOT/venv/bin/python3"
LOG="$HERMES_HOME/logs/safe-restart.log"
RESTART_TRIGGER="$HERMES_HOME/logs/.restart-trigger"
RESTART_INTENT_DIR="$HERMES_HOME/state/restart-intents"
HEALTH_CHECK="$HERMES_HOME/bin/gateway-health-probe.sh"
POST_RESTART_RESUME="$HERMES_HOME/bin/hermes-post-restart-resume.sh"
TOOL_READINESS_POST_RESTART="$HERMES_HOME/bin/tool-readiness-post-restart.sh"
COMPILE_GATE_CHECK="$HERMES_HOME/bin/compile-gate-check.py"
DOC_SANITY_CHECK="$HERMES_HOME/bin/doc-profile-sanity-check.py"

usage() {
    echo "usage: $0 {doc|gateway|<profile-name>}" >&2
    echo "  doc       — Linux/Spark systemd unit hermes-doc" >&2
    echo "  gateway   — platform-detected client gateway (Linux/systemd or macOS/launchd)" >&2
    echo "  <name>    — named gateway profile; Linux/systemd or macOS/launchd" >&2
    echo "              (macOS label: ai.hermes.gateway-<name>)" >&2
    exit "${1:-2}"
}

PROFILE="${1:-}"
[ "$PROFILE" != "--help" ] && [ "$PROFILE" != "-h" ] || usage 0
[ -n "$PROFILE" ] || usage

# Profile names containing characters that would break a service label or shell
# expansion are rejected.
case "$PROFILE" in
  *[!a-zA-Z0-9_-]*) usage ;;
esac

case "$PROFILE" in
  doc)
    UNIT="${HERMES_GATEWAY_UNIT:-hermes-doc}"
    PROFILE_HOME="$HERMES_HOME/profiles/doc"
    ;;
  gateway)
    UNIT="${HERMES_GATEWAY_UNIT:-ai.hermes.gateway}"
    PROFILE_HOME="$HERMES_HOME"
    ;;
  enoch)
    # Mini Enoch uses the single-gateway LaunchAgent when no dedicated
    # ai.hermes.gateway-enoch.plist exists.
    if [ -z "${HERMES_GATEWAY_UNIT:-}" ] \
        && [ -f "$HOME/Library/LaunchAgents/ai.hermes.gateway.plist" ] \
        && [ ! -f "$HOME/Library/LaunchAgents/ai.hermes.gateway-enoch.plist" ]; then
      UNIT="ai.hermes.gateway"
    else
      UNIT="${HERMES_GATEWAY_UNIT:-ai.hermes.gateway-enoch}"
    fi
    PROFILE_HOME="$HERMES_HOME"
    ;;
  *)
    # Named profiles use the common Linux systemd route or, on multi-profile
    # Macs, launchd label ai.hermes.gateway-<profile>. Prefer a real profile
    # home when present; otherwise preserve the single-tree client layout.
    # HERMES_GATEWAY_UNIT overrides the platform's default unit or label.
    UNIT="${HERMES_GATEWAY_UNIT:-ai.hermes.gateway-$PROFILE}"
    if [ -d "$HERMES_HOME/profiles/$PROFILE" ]; then
      PROFILE_HOME="$HERMES_HOME/profiles/$PROFILE"
    else
      PROFILE_HOME="$HERMES_HOME"
    fi
    ;;
esac

# The rollout executor uses this same target-local lease. Acquire it before
# any restart mutation, then keep it through health and binding verification.
runtime_mutation_lease_is_held() {
    [ "${HERMES_RUNTIME_MUTATION_LEASE_HELD:-0}" = "1" ] || return 1
    [ "${HERMES_RUNTIME_MUTATION_LEASE_FD:-}" = "9" ] || return 1
    python3 - "$PROFILE_HOME/state/promotion/executor/runtime-mutation.lock" <<'PY' >/dev/null 2>&1
import os
import sys

expected = os.stat(sys.argv[1])
actual = os.fstat(9)
raise SystemExit(0 if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino) else 1)
PY
}

if ! runtime_mutation_lease_is_held \
    && [ "${HERMES_SAFE_RESTART_DRY_RUN:-0}" != "1" ]; then
    exec python3 - "$PROFILE_HOME/state/promotion/executor/runtime-mutation.lock" "$0" "$@" <<'PY'
import fcntl
import os
import sys
import time
from pathlib import Path

lock_path = Path(sys.argv[1])
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+", encoding="utf-8") as lock:
    deadline = time.monotonic() + 10.0
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                print("runtime_mutation_busy")
                raise SystemExit(75)
            time.sleep(0.1)
    environment = os.environ.copy()
    environment["HERMES_RUNTIME_MUTATION_LEASE_HELD"] = "1"
    # Keep the advisory lock on the same process across exec. Cancellation can
    # no longer release the lease while a separately spawned restart survives.
    os.dup2(lock.fileno(), 9, inheritable=True)
    environment["HERMES_RUNTIME_MUTATION_LEASE_FD"] = "9"
    os.execvpe("bash", ["bash", *sys.argv[2:]], environment)
PY
fi

release_runtime_mutation_lease() {
    if [ "${HERMES_RUNTIME_MUTATION_LEASE_FD:-}" = "9" ]; then
        exec 9>&-
        unset HERMES_RUNTIME_MUTATION_LEASE_FD
        unset HERMES_RUNTIME_MUTATION_LEASE_HELD
    fi
}

mkdir -p "$HERMES_HOME/logs" "$RESTART_INTENT_DIR"
RESTART_REASON="${HERMES_RESTART_REASON:-manual}"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [safe-restart][$PROFILE] $*" >> "$LOG"; }

# [HERMES_SAFE_RESTART_SINGLE_FLIGHT_v1]
# Prevent overlapping queued restarts from repeatedly SIGTERMing a freshly
# relaunched gateway. Empty, malformed, and dead-owner locks are stale.
mkdir -p "$HERMES_HOME/state/restart-locks"
LOCK_DIR="$HERMES_HOME/state/restart-locks/$PROFILE.lock"
_lock_is_stale() {
    [ -d "$LOCK_DIR" ] || return 0
    local pid="" ts=""
    [ -f "$LOCK_DIR/pid" ] && pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    [ -f "$LOCK_DIR/ts" ] && ts="$(cat "$LOCK_DIR/ts" 2>/dev/null || true)"
    if [ -z "$pid" ] || [ -z "$ts" ]; then
        sleep 1
        [ -f "$LOCK_DIR/pid" ] && pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
        [ -f "$LOCK_DIR/ts" ] && ts="$(cat "$LOCK_DIR/ts" 2>/dev/null || true)"
    fi
    case "$pid" in
      ''|*[!0-9]*) return 0 ;;
      *) kill -0 "$pid" 2>/dev/null && return 1 ;;
    esac
    return 0
}
_release_restart_lock() {
    [ -d "$LOCK_DIR" ] || return 0
    local expected_token="${1:-}" current_token=""
    [ -f "$LOCK_DIR/token" ] && current_token="$(cat "$LOCK_DIR/token" 2>/dev/null || true)"
    [ -z "$expected_token" ] || [ "$current_token" = "$expected_token" ] || return 0
    find "$LOCK_DIR" -mindepth 1 -maxdepth 1 -type f \
        \( -name pid -o -name ts -o -name token \) -delete 2>/dev/null || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
LOCK_ACQUIRED=0
if mkdir "$LOCK_DIR" 2>/dev/null; then
    LOCK_ACQUIRED=1
else
    if _lock_is_stale; then
        log "removing stale or malformed restart lock $LOCK_DIR"
        _release_restart_lock
    fi
fi
if [ "$LOCK_ACQUIRED" = "1" ] || mkdir "$LOCK_DIR" 2>/dev/null; then
    LOCK_TOKEN="$$-$(date +%s)-$RANDOM"
    printf '%s\n' "$LOCK_TOKEN" > "$LOCK_DIR/token"
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    date +%s > "$LOCK_DIR/ts"
    trap '_release_restart_lock "$LOCK_TOKEN"' EXIT
    log "acquired restart lock $LOCK_DIR"
else
    log "restart already in progress for $PROFILE; refusing overlapping restart"
    echo "restart_already_running"
    exit 75
fi


# [HERMES_SAFE_RESTART_DRAIN_ALIGNED_GRACE_v1]
# Compute SIGTERM grace from the gateway drain budget so safe-restart allows
# the configured drain window to complete before failing closed. Operators
# may override with HERMES_SIGTERM_GRACE_SECONDS. Fallback preserves the old 90s.
_sigterm_grace_seconds() {
    case "${HERMES_SIGTERM_GRACE_SECONDS:-}" in
        ''|*[!0-9]*) ;;
        *) echo "$HERMES_SIGTERM_GRACE_SECONDS"; return 0 ;;
    esac

    _cfg="$PROFILE_HOME/config.yaml"
    [ -f "$_cfg" ] || _cfg="$HERMES_HOME/config.yaml"
    _value=""
    if [ -x "$PYTHON" ] && [ -f "$_cfg" ]; then
        _value=$("$PYTHON" - "$_cfg" <<'PY' 2>/dev/null || true
import math
import sys
try:
    import yaml
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    raw = agent.get("restart_drain_timeout")
    if raw is None:
        raw = cfg.get("restart_drain_timeout")
    drain = float(raw) if raw is not None else 60.0
    print(int(math.ceil(max(90.0, drain + 30.0))))
except Exception:
    pass
PY
)
    fi
    case "$_value" in
        ''|*[!0-9]*) echo 90 ;;
        *) echo "$_value" ;;
    esac
}


# HERMES_SAFE_RESTART_HEALTHY_COOLDOWN_v1 — repeated operator/watchdog
# callers must not bounce an already-healthy gateway every few minutes.
healthy_restart_cooldown_guard() {
    [ "${HERMES_SAFE_RESTART_FORCE:-0}" = "1" ] && return 0
    [ "$PROFILE" = "doc" ] && return 0
    case "$RESTART_REASON" in
        *watchdog*) ;;
        *) return 0 ;;
    esac
    local cooldown="${HERMES_SAFE_RESTART_HEALTHY_COOLDOWN_SECONDS:-1800}"
    local intent="$RESTART_INTENT_DIR/$PROFILE.json"
    [ -f "$intent" ] || return 0
    local last now age
    if stat -f %m "$intent" >/dev/null 2>&1; then
        last=$(stat -f %m "$intent")
    else
        last=$(stat -c %Y "$intent" 2>/dev/null || echo 0)
    fi
    now=$(date -u +%s)
    age=$((now - last))
    [ "$age" -lt "$cooldown" ] || return 0
    if python3 - "$PROFILE_HOME/gateway_state.json" <<'PYCHECK' >/dev/null 2>&1
import json, os, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(1)
data = json.loads(path.read_text())
platforms = data.get("platforms") or {}
telegram = platforms.get("telegram") or {}
pid = int(data.get("pid") or 0)
if data.get("gateway_state") != "running":
    raise SystemExit(1)
if telegram and telegram.get("state") not in (None, "connected"):
    raise SystemExit(1)
if pid <= 0:
    raise SystemExit(1)
os.kill(pid, 0)
PYCHECK
    then
        log "skipping restart: gateway healthy and last restart intent age=${age}s < cooldown=${cooldown}s (set HERMES_SAFE_RESTART_FORCE=1 to override)"
        exit 0
    fi
}


# [HERMES_SAFE_RESTART_FIND_PID_v5] profile-owned state + PID fingerprint
_find_gw_pid() {
    _find_all_gw_pids | head -1
}

_find_all_gw_pids() {
    local state_path="$PROFILE_HOME/gateway_state.json" state_pid="" command=""
    state_pid="$(python3 - "$state_path" <<'PY' 2>/dev/null || true
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

state = json.load(open(sys.argv[1], encoding="utf-8"))
pid = int(state.get("pid") or 0)
recorded = int(state.get("start_time") or 0)
if pid <= 0 or recorded <= 0:
    raise SystemExit(1)
if sys.platform.startswith("linux"):
    actual = int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
    if actual != recorded:
        raise SystemExit(1)
else:
    actual_text = subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "lstart="], text=True, stderr=subprocess.DEVNULL
    ).strip()
    actual = int(
        round(
            datetime.strptime(actual_text, "%a %b %d %H:%M:%S %Y")
            .astimezone()
            .timestamp()
            * 100
        )
    )
    if abs(actual - recorded) > 200:
        raise SystemExit(1)
print(pid)
PY
)"
    case "$state_pid" in
      ''|0|*[!0-9]*) return 0 ;;
    esac
    if kill -0 "$state_pid" 2>/dev/null; then
        command="$(ps -p "$state_pid" -o command= 2>/dev/null || true)"
        if printf '%s\n' "$command" | grep -Eq '(^|[[:space:]])gateway[[:space:]]+run([[:space:]]|$)'; then
            printf '%s\n' "$state_pid"
        fi
    fi
    return 0
}

checkpoint_durable_db() {
    local state_db="$HERMES_HOME/state.db" checker="python3" quick_result
    [ -x "$PYTHON" ] && checker="$PYTHON"
    if [ -f "$state_db" ]; then
        if ! quick_result=$("$checker" - "$state_db" 2>&1 <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as connection:
    rows = connection.execute("PRAGMA quick_check").fetchall()
if rows != [("ok",)]:
    raise SystemExit("; ".join(str(row[0]) for row in rows[:8]))
print("ok")
PY
        ); then
            log "state.db quick_check failed; refusing restart: $quick_result"
            echo "state_db_integrity_failed"
            return 1
        fi
        log "state.db quick_check: ok"
    fi
    local db="$PROFILE_HOME/data/durable-threads.db"
    [ -f "$db" ] || return 0
    command -v sqlite3 >/dev/null 2>&1 || { log "sqlite3 not in PATH; skipping WAL checkpoint"; return 0; }
    local result wal_bytes=0
    result=$(sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>&1 || true)
    [ -f "${db}-wal" ] && wal_bytes=$(wc -c < "${db}-wal" 2>/dev/null || echo 0) || true
    log "WAL checkpoint: $result (wal_bytes_after=${wal_bytes})"
}

cleanup_request_dumps() {
    local d count=0
    for d in "$PROFILE_HOME/sessions" "$HERMES_HOME/sessions"; do
        [ -d "$d" ] || continue
        local n
        n=$(find "$d" -maxdepth 1 -name "request_dump_*.json" -mtime +3 2>/dev/null | wc -l | tr -d " \t")
        n=${n:-0}
        if [ "$n" -gt 0 ]; then
            find "$d" -maxdepth 1 -name "request_dump_*.json" -mtime +3 -delete 2>/dev/null || true
            count=$((count + n))
        fi
    done
    # Use explicit if/fi — under `set -e`, the short-circuit chain
    # `[ X ] && Y || true` can trigger function early-exit. Same fix as
    # spark-client edition (2026-05-08).
    if [ "$count" -gt 0 ]; then
        log "cleaned $count stale request_dump files (>3d)"
    fi
    # Non-fatal: even if the function triggers a set -e abort somewhere
    # (observed on bash 5.2 + sudo -u + pipe substitutions), the caller
    # uses `|| true` so it can't kill the whole restart.
    return 0
}

signal_post_restart_resume() {
    mkdir -p "$(dirname "$RESTART_TRIGGER")"
    : > "$RESTART_TRIGGER"
    touch "$RESTART_TRIGGER"
    log "updated post-restart trigger at $RESTART_TRIGGER"
}

# [HERMES_SAFE_RESTART_RUNTIME_BINDING_REFRESH_v1]
# A same-runtime restart changes the process identity without changing the
# immutable release.  Refresh only that process tuple after the new gateway is
# healthy, while revalidating every release-owned launcher/service byte.
refresh_runtime_binding() {
    local new_pid="$1" mode="${2:-refresh}" binding="$PROFILE_HOME/state/runtime-binding.json"
    [ -f "$binding" ] || return 0

    if ! python3 - "$binding" "$new_pid" "$PROFILE_HOME" "$mode" <<'PY'
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

binding_path = Path(sys.argv[1])
profile_home = Path(sys.argv[3]).resolve()
mode = sys.argv[4]


def fail(message: str) -> None:
    raise SystemExit(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if binding_path.is_symlink() or not binding_path.is_file() or binding_path.resolve() != binding_path:
    fail("runtime binding path is unsafe")
raw = binding_path.read_bytes()
try:
    binding = json.loads(raw)
except Exception:
    fail("runtime binding is invalid JSON")
if not isinstance(binding, dict) or binding.get("schema_version") != 1:
    fail("runtime binding schema is invalid")
if binding.get("kind") != "botdoctor_runtime_binding" or binding.get("status") != "active":
    fail("runtime binding is not active")
runtime_root = Path(str(binding.get("runtime_root") or ""))
if not runtime_root.is_absolute() or not runtime_root.is_dir() or runtime_root.resolve() != runtime_root:
    fail("runtime root is invalid")
runtime_python = Path(str(binding.get("runtime_python") or ""))
expected_python = runtime_root / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
if (
    not runtime_python.is_absolute()
    or runtime_python != expected_python
    or not runtime_python.is_file()
):
    fail("runtime interpreter binding is invalid")
bound_home = Path(str(binding.get("hermes_home") or ""))
if not bound_home.is_absolute() or bound_home.resolve() != profile_home:
    fail("runtime binding belongs to another Hermes home")
release = binding.get("release")
target_sha = str(binding.get("target_sha") or "")
if (
    not isinstance(release, dict)
    or not re.fullmatch(r"[0-9a-f]{40}", str(release.get("base_sha") or ""))
    or not re.fullmatch(r"[0-9a-f]{40}", str(release.get("target_sha") or ""))
    or target_sha != release.get("target_sha")
):
    fail("runtime release binding is invalid")
fingerprint_digest = str(binding.get("runtime_fingerprint_digest") or "")
fingerprint_path = runtime_root / ".operator-control/runtime-fingerprint.json"
if fingerprint_path.is_symlink() or not fingerprint_path.is_file():
    fail("runtime fingerprint manifest is unavailable")
try:
    fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
except Exception:
    fail("runtime fingerprint manifest is invalid")
files = fingerprint.get("files") if isinstance(fingerprint, dict) else None
if (
    fingerprint.get("verified") is not True
    or fingerprint.get("exact_assembly") is not True
    or fingerprint.get("runtime_dir") != str(runtime_root)
    or fingerprint.get("golden_sha") != target_sha
    or fingerprint.get("digest") != fingerprint_digest
    or not isinstance(files, dict)
    or fingerprint.get("file_count") != len(files)
):
    fail("runtime fingerprint identity is invalid")
canonical_rows = []
for relative, row in sorted(files.items()):
    path = Path(str(relative))
    if path.is_absolute() or ".." in path.parts or not isinstance(row, dict):
        fail("runtime fingerprint row is invalid")
    target = runtime_root / path
    kind = row.get("type")
    file_mode = row.get("mode")
    expected = row.get("sha256")
    if kind == "deleted":
        if target.exists() or target.is_symlink() or file_mode != "000000" or expected:
            fail("runtime fingerprint deletion drifted")
        canonical_rows.append(f"{relative}\t000000\tdeleted\t-\n")
        continue
    if kind != "blob" or file_mode not in {"100644", "100755", "120000"}:
        fail("runtime fingerprint row is invalid")
    if file_mode == "120000":
        if not target.is_symlink():
            fail("runtime fingerprint symlink drifted")
        content = os.readlink(target).encode()
    else:
        if target.is_symlink() or not target.is_file():
            fail("runtime fingerprint file drifted")
        actual_mode = "100755" if target.stat().st_mode & 0o100 else "100644"
        if actual_mode != file_mode:
            fail("runtime fingerprint mode drifted")
        content = target.read_bytes()
    actual_sha = hashlib.sha256(content).hexdigest()
    if actual_sha != expected:
        fail("runtime fingerprint content drifted")
    canonical_rows.append(f"{relative}\t{file_mode}\t{kind}\t{expected}\n")
if hashlib.sha256("".join(canonical_rows).encode()).hexdigest() != fingerprint_digest:
    fail("runtime fingerprint digest does not match manifest")

service = binding.get("service")
if not isinstance(service, dict):
    fail("runtime service binding is invalid")
definition_path = service.get("definition_path")
definition_sha = service.get("definition_sha256")
if definition_path is not None or definition_sha is not None:
    definition = Path(str(definition_path or ""))
    if (
        not definition.is_absolute()
        or definition.is_symlink()
        or not definition.is_file()
        or definition.resolve() != definition
        or sha256(definition) != str(definition_sha or "")
    ):
        fail("runtime service definition drifted")
launchers = service.get("launchers")
if not isinstance(launchers, list):
    fail("runtime launcher binding is invalid")
if not launchers and definition_path is None:
    fail("runtime service binding has no verified executable owner")
launcher_rows = []
for row in launchers:
    if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
        fail("runtime launcher row is invalid")
    path = Path(str(row["path"]))
    expected = str(row["sha256"])
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.resolve() != path
        or sha256(path) != expected
    ):
        fail("runtime launcher drifted")
    launcher_rows.append((str(path), expected))

if mode == "preflight":
    raise SystemExit(0)
if mode != "refresh":
    fail("runtime binding operation is invalid")
pid = int(sys.argv[2])

try:
    os.kill(pid, 0)
except OSError:
    fail("new gateway process is not alive")
runtime_environment_bound = False
if sys.platform.startswith("linux"):
    proc = Path(f"/proc/{pid}")
    command = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    environment = {}
    for item in proc.joinpath("environ").read_bytes().split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            environment[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    if Path(environment.get("HERMES_HOME", "")).resolve() != profile_home:
        fail("new gateway Hermes home does not match binding")
    try:
        if not proc.joinpath("exe").samefile(runtime_python):
            fail("new gateway interpreter does not match binding")
    except OSError:
        fail("new gateway interpreter is unavailable")
    fields = proc.joinpath("stat").read_text(encoding="utf-8").split()
    ticks = int(fields[21])
    boot = next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
        if line.startswith("btime ")
    )
    started_at = boot + ticks / os.sysconf("SC_CLK_TCK")
else:
    command = subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "command="], text=True, stderr=subprocess.DEVNULL
    ).strip()
    started_text = subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "lstart="], text=True, stderr=subprocess.DEVNULL
    ).strip()
    started_at = datetime.strptime(started_text, "%a %b %d %H:%M:%S %Y").astimezone().timestamp()
    environment_command = subprocess.check_output(
        ["ps", "eww", "-p", str(pid), "-o", "command="],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    def environment_has_exact(key: str, value: Path) -> bool:
        return re.search(
            rf"(?:^|\s){re.escape(key)}={re.escape(str(value))}(?=\s|$)",
            environment_command,
        ) is not None

    def environment_path(key: str):
        match = re.search(
            rf"(?:^|\s){re.escape(key)}=(\S+)(?=\s|$)",
            environment_command,
        )
        return Path(match.group(1)) if match else None

    if not environment_has_exact("HERMES_HOME", profile_home):
        fail("new gateway Hermes home does not match binding")
    executable_rows = subprocess.check_output(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).splitlines()
    executable_paths = [Path(row[1:]) for row in executable_rows if row.startswith("n")]
    try:
        bound_executables = [runtime_python]
        if sys.platform == "darwin":
            base_executable = Path(
                subprocess.check_output(
                    [
                        str(runtime_python),
                        "-I",
                        "-c",
                        (
                            "import os,sys; "
                            "print(os.path.realpath(getattr(sys, '_base_executable', sys.executable)))"
                        ),
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                ).strip()
            )
            if (
                not base_executable.is_absolute()
                or not base_executable.is_file()
                or not base_executable.samefile(runtime_python)
            ):
                fail("bound runtime interpreter reported an invalid base executable")
            framework_executable = (
                base_executable.parent.parent
                / "Resources/Python.app/Contents/MacOS/Python"
            )
            if framework_executable.is_file():
                bound_executables.append(framework_executable)
        if not any(
            process_path.samefile(bound_executable)
            for process_path in executable_paths
            for bound_executable in bound_executables
        ):
            fail("new gateway interpreter does not match binding")
    except (OSError, subprocess.SubprocessError):
        fail("new gateway interpreter is unavailable")
    if sys.platform == "darwin" and environment_has_exact(
        "VIRTUAL_ENV", runtime_root / "venv"
    ):
        process_launcher = environment_path("__PYVENV_LAUNCHER__")
        try:
            runtime_environment_bound = bool(
                process_launcher
                and process_launcher.is_absolute()
                and process_launcher.parent == runtime_python.parent
                and process_launcher.is_file()
                and process_launcher.samefile(runtime_python)
            )
        except OSError:
            runtime_environment_bound = False
if (
    "gateway run" not in " ".join(command.split())
    or (str(runtime_root) not in command and not runtime_environment_bound)
):
    fail("new process is not the bound gateway runtime")

generation = hashlib.sha256(
    json.dumps(
        {"pid": pid, "started_at": float(started_at), "launchers": launcher_rows},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()
binding["process"] = {
    "pid": pid,
    "started_at": float(started_at),
    "generation": generation,
}
payload = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
stat = binding_path.stat()


def atomic_binding_write(content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{binding_path.name}.", dir=binding_path.parent)
    try:
        os.fchmod(fd, stat.st_mode & 0o777)
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, binding_path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    directory_fd = os.open(binding_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


atomic_binding_write(payload)
try:
    os.kill(pid, 0)
    persisted = json.loads(binding_path.read_text(encoding="utf-8"))
    if persisted.get("process") != binding["process"]:
        raise RuntimeError("runtime binding refresh did not persist")
except (OSError, ValueError, RuntimeError) as exc:
    atomic_binding_write(raw)
    fail(f"runtime binding refresh rolled back ({exc})")
PY
    then
        log "runtime binding refresh failed for pid=$new_pid"
        echo "runtime_binding_refresh_failed"
        return 1
    fi
    log "refreshed runtime binding for pid=$new_pid"
}

preflight_runtime_binding() {
    refresh_runtime_binding 0 preflight
}

gateway_state_is_healthy_for_pid() {
    local expected_pid="$1" state_file
    for state_file in \
        "$PROFILE_HOME/gateway_state.json" \
        "$HERMES_HOME/gateway_state.json" \
        "$HERMES_HOME/state/gateway_state.json"; do
        [ -f "$state_file" ] || continue
        if python3 - "$state_file" "$expected_pid" <<'PY' >/dev/null 2>&1
import json
import os
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
pid = int(state.get("pid") or 0)
platforms = state.get("platforms") or {}
telegram_row = platforms.get("telegram")
telegram = telegram_row.get("state") if isinstance(telegram_row, dict) else None
if pid != int(sys.argv[2]) or state.get("gateway_state") != "running":
    raise SystemExit(1)
if telegram_row is not None and telegram != "connected":
    raise SystemExit(1)
if telegram_row is None and not any(
    isinstance(row, dict) and row.get("state") == "connected"
    for row in platforms.values()
):
    raise SystemExit(1)
os.kill(pid, 0)
PY
        then
            return 0
        fi
    done
    return 1
}

# [HERMES_SAFE_RESTART_LINUX_GATEWAY_ROUTE_v1]
# The general helper is distributed by several Golden sync paths. A Linux
# client must never fall through to the launchd branch merely because it was
# given the common `gateway` profile name.
restart_linux_client_gateway() {
    local systemd_unit="${HERMES_GATEWAY_UNIT:-hermes-gateway.service}"
    local attempts="${HERMES_HEALTH_ATTEMPTS:-24}"
    local sleep_seconds="${HERMES_HEALTH_SLEEP:-5}"
    local agent_dir="" cand old_pid new_pid

    if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -d "$XDG_RUNTIME_DIR" ]; then
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    fi
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    fi

    for cand in "$HOME/live-hermes" "$HOME/hermes-agent" "$HERMES_HOME/hermes-agent"; do
        if [ -d "$cand" ]; then
            agent_dir="$cand"
            break
        fi
    done

    python3 - "$RESTART_INTENT_DIR/$PROFILE.json" "$PROFILE" "$RESTART_REASON" <<'PY'
import json, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({"profile": sys.argv[2], "reason": sys.argv[3], "ts": int(time.time())}))
PY

    if [ -n "$agent_dir" ] && ! python3 -m compileall -q "$agent_dir" >/dev/null 2>&1; then
        log "compile check failed against $agent_dir; refusing Linux gateway restart"
        echo "compile_check_failed"
        return 1
    fi
    preflight_runtime_binding || return 1
    checkpoint_durable_db
    cleanup_request_dumps || true
    log "restarting $systemd_unit via systemd (reason=$RESTART_REASON)"
    old_pid=$(systemctl --user show -p MainPID --value "$systemd_unit" 2>/dev/null || true)
    if ! systemctl --user restart "$systemd_unit" 2>>"$LOG"; then
        log "systemctl --user restart failed for $systemd_unit"
        echo "systemctl_restart_failed"
        return 1
    fi

    local try state_file state_line gateway_state telegram_state state_pid
    for try in $(seq 1 "$attempts"); do
        if [ "$(systemctl --user is-active "$systemd_unit" 2>/dev/null || true)" = "active" ]; then
            new_pid=$(systemctl --user show -p MainPID --value "$systemd_unit" 2>/dev/null || true)
            if [ -n "$new_pid" ] && [ "$new_pid" != "0" ] && [ "$new_pid" != "$old_pid" ] \
                && case "$new_pid" in *[!0-9]*) false ;; *) true ;; esac \
                && kill -0 "$new_pid" 2>/dev/null; then
                for state_file in \
                "$PROFILE_HOME/gateway_state.json" \
                "$HERMES_HOME/gateway_state.json" \
                "$HERMES_HOME/state/gateway_state.json"; do
                [ -f "$state_file" ] || continue
                state_line=$(python3 - "$state_file" <<'PY' 2>/dev/null || true
import json, sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    raise SystemExit
telegram = ((data.get("platforms") or {}).get("telegram") or {}).get("state") or "?"
print((data.get("gateway_state") or "?") + " " + telegram + " " + str(data.get("pid") or 0))
PY
)
                gateway_state=${state_line%% *}
                state_line=${state_line#* }
                telegram_state=${state_line%% *}
                state_pid=${state_line#* }
                if [ "$gateway_state" = "running" ] && [ "$telegram_state" = "connected" ] && [ "$state_pid" = "$new_pid" ]; then
                    refresh_runtime_binding "$new_pid" || return 1
                    signal_post_restart_resume
                    release_runtime_mutation_lease
                    log "verified healthy after Linux gateway restart (unit=$systemd_unit state_file=$state_file attempt=$try)"
                    echo "ok"
                    return 0
                fi
                done
            fi
        fi
        [ "$try" -ge "$attempts" ] || sleep "$sleep_seconds"
    done
    log "Linux gateway restart unverified after $attempts attempts (unit=$systemd_unit)"
    echo "restart_unverified"
    return 1
}

if [ "${HERMES_SAFE_RESTART_DRY_RUN:-0}" = "1" ]; then
    PIDS="$(_find_all_gw_pids | tr "\n" " ")"
    log "dry-run profile=$PROFILE unit=$UNIT profile_home=$PROFILE_HOME pids=${PIDS% }"
    echo "dry_run profile=$PROFILE unit=$UNIT profile_home=$PROFILE_HOME pids=${PIDS% }"
    exit 0
fi

if [ "$PROFILE" != "doc" ] && [ "$(uname -s)" = "Linux" ]; then
    restart_linux_client_gateway
    exit $?
fi

# ── gateway profile (macOS launchd) ──────────────────────────────────────────
# 2026-05-08: also runs for any non-doc profile (multi-profile Mac fallthrough,
# where PROFILE maps to UNIT=ai.hermes.gateway-<profile>).
if [ "$PROFILE" != "doc" ]; then
    healthy_restart_cooldown_guard
    python3 - "$RESTART_INTENT_DIR/$PROFILE.json" "$PROFILE" "$RESTART_REASON" <<'PY'
import json, sys, time
from pathlib import Path
path = Path(sys.argv[1])
payload = {"profile": sys.argv[2], "reason": sys.argv[3], "ts": int(time.time())}
path.write_text(json.dumps(payload))
PY

    preflight_runtime_binding || exit 1
    signal_post_restart_resume

    checkpoint_durable_db
    cleanup_request_dumps
    # Capture ALL matching gateway PIDs (often just one, but multi-process
    # races can leave 2 transiently). SIGTERM all; then watch for a PID
    # that is NOT in the original set. Bug fix 2026-05-08: previously
    # head -1 + single-PID compare could be fooled by a sibling leftover.
    OLD_PIDS_LIST=$(_find_all_gw_pids | tr "\n" " ")
    if [ -n "${OLD_PIDS_LIST// /}" ]; then
        log "sending SIGTERM to gateway pid(s)=${OLD_PIDS_LIST% }"
        for _p in $OLD_PIDS_LIST; do
            kill -SIGTERM "$_p" 2>/dev/null || true
        done
    else
        # Try gui/<uid>/ first (LaunchAgent in user GUI domain — the common case
        # for Mac client minis). If it returns 125 (Domain does not support
        # specified action), fall back to system/ domain (LaunchDaemon — used on
        # multi-user Mac hosts like client-mac-host where secondary user gateways
        # are loaded as system services rather than user agents). 2026-05-08.
        log "no running gateway found; triggering launchctl kickstart"
        if ! launchctl kickstart "gui/$(id -u)/$UNIT" 2>/dev/null; then
            AGENT_PLIST="$HOME/Library/LaunchAgents/$UNIT.plist"
            if [ -f "$AGENT_PLIST" ]; then
                log "gui/$(id -u)/$UNIT not loaded; bootstrapping $AGENT_PLIST"
                launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST" 2>/dev/null || true
                launchctl kickstart "gui/$(id -u)/$UNIT" 2>/dev/null || true
            fi
            if ! launchctl print "gui/$(id -u)/$UNIT" >/dev/null 2>&1; then
                log "gui/$(id -u)/$UNIT failed; trying system/$UNIT"
                sudo launchctl kickstart "system/$UNIT" 2>/dev/null || true
            fi
        fi
    fi

    # Grace window 1: wait for graceful SIGTERM exit + launchd respawn.
    # [HERMES_SAFE_RESTART_DRAIN_ALIGNED_GRACE_v1] Align this with the gateway
    # drain budget (restart_drain_timeout + 30s, floor 90s) so safe-restart
    # allows the configured drain to complete before failing closed.
    SIGTERM_GRACE_SECONDS="$(_sigterm_grace_seconds)"
    SIGTERM_GRACE_LOOPS=$(( (SIGTERM_GRACE_SECONDS + 1) / 2 ))
    log "waiting for gateway to restart (up to ${SIGTERM_GRACE_SECONDS}s)..."
    NEW_PID=""
    for _i in $(seq 1 "$SIGTERM_GRACE_LOOPS"); do
        sleep 2
        for _cp in $(_find_all_gw_pids); do
            case " $OLD_PIDS_LIST " in
                *" $_cp "*) ;;
                *) NEW_PID="$_cp"; break 2 ;;
            esac
        done
    done

    if [ -z "$NEW_PID" ]; then
        log "gateway did not restart within the graceful drain window; refusing to hard-kill"
        echo "restart_timeout"
        exit 1
    fi

    log "gateway restarted pid=$NEW_PID"

    _health_attempts="${HERMES_HEALTH_ATTEMPTS:-24}"
    _health_sleep="${HERMES_HEALTH_SLEEP:-2}"
    for _i in $(seq 1 "$_health_attempts"); do
        gateway_state_is_healthy_for_pid "$NEW_PID" && break
        [ "$_i" -ge "$_health_attempts" ] || sleep "$_health_sleep"
    done
    if ! gateway_state_is_healthy_for_pid "$NEW_PID"; then
        log "gateway process did not reach healthy state pid=$NEW_PID"
        echo "restart_unverified"
        exit 1
    fi
    refresh_runtime_binding "$NEW_PID" || exit 1
    release_runtime_mutation_lease

    if [ -f "$POST_RESTART_RESUME" ]; then
        HERMES_HOME="$PROFILE_HOME" HERMES_RESUME_WAIT_SECONDS="${HERMES_RESUME_WAIT_SECONDS:-12}" \
            nohup python3 "$POST_RESTART_RESUME" >/dev/null 2>&1 &
        log "launched post-restart resume helper"
    fi
    if [ -x "$TOOL_READINESS_POST_RESTART" ]; then
        HERMES_HOME="$PROFILE_HOME" nohup bash "$TOOL_READINESS_POST_RESTART" >/dev/null 2>&1 &
        log "launched tool readiness post-restart probe"
    fi

    echo "ok"
    exit 0
fi

# ── doc profile (Linux systemd) ───────────────────────────────────────────────

doc_sanity_detail() {
  python3 - "$PROFILE_HOME/state/doc-profile-sanity-latest.json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print("doc_profile_sanity_missing_output")
    raise SystemExit(0)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("doc_profile_sanity_unreadable_output")
    raise SystemExit(0)
findings = data.get("findings") or []
if findings:
    print(" | ".join(str(item) for item in findings))
else:
    print(str(data.get("detail") or "doc_profile_sanity_failed"))
PY
}

run_doc_sanity() {
  local output rc
  if [ ! -x "$DOC_SANITY_CHECK" ]; then
    log "doc sanity checker missing at $DOC_SANITY_CHECK"
    return 1
  fi
  set +e
  output="$(python3 "$DOC_SANITY_CHECK" "$PROFILE_HOME" 2>&1)"
  rc=$?
  set -e
  if [ -n "$output" ]; then
    printf '%s\n' "$output" >> "$LOG"
  fi
  return $rc
}

restore_doc_config_backup() {
  local latest_backup
  latest_backup="$(ls -1t "$PROFILE_HOME"/config.yaml.bak-* 2>/dev/null | head -n 1 || true)"
  if [ -z "$latest_backup" ]; then
    log "doc self-heal found no config backup"
    return 1
  fi
  cp "$latest_backup" "$PROFILE_HOME/config.yaml"
  log "doc self-heal restored config from $latest_backup"
}

launch_post_restart_hooks() {
    signal_post_restart_resume
    if [ -f "$POST_RESTART_RESUME" ]; then
        HERMES_HOME="$PROFILE_HOME" HERMES_RESUME_WAIT_SECONDS="${HERMES_RESUME_WAIT_SECONDS:-12}" \
            nohup python3 "$POST_RESTART_RESUME" >/dev/null 2>&1 &
        log "launched post-restart resume helper"
    else
        log "post-restart resume helper missing at $POST_RESTART_RESUME"
    fi
    if [ -x "$TOOL_READINESS_POST_RESTART" ]; then
        HERMES_HOME="$PROFILE_HOME" nohup bash "$TOOL_READINESS_POST_RESTART" >/dev/null 2>&1 &
        log "launched tool readiness post-restart probe"
    fi
}

verify_profile_health() {
    local ATTEMPTS="${1:-24}"
    local SLEEP_SECONDS="${2:-5}"
    local TRY=1
    local RESULT=""
    if [ ! -x "$HEALTH_CHECK" ]; then
        log "health check missing at $HEALTH_CHECK"
        echo "health_check_missing"
        return 1
    fi
    while [ "$TRY" -le "$ATTEMPTS" ]; do
        RESULT=$(bash "$HEALTH_CHECK" "$PROFILE" 2>/dev/null) && {
            [ "$RESULT" = "ok" ] && return 0
        }
        if [ "$TRY" -lt "$ATTEMPTS" ]; then
            sleep "$SLEEP_SECONDS"
        fi
        TRY=$((TRY + 1))
    done
    echo "${RESULT:-health_check_failed}"
    return 1
}

python3 - "$RESTART_INTENT_DIR/$PROFILE.json" "$PROFILE" "$RESTART_REASON" <<'PY'
import json, sys, time
from pathlib import Path
path = Path(sys.argv[1])
payload = {"profile": sys.argv[2], "reason": sys.argv[3], "ts": int(time.time())}
path.write_text(json.dumps(payload))
PY

cd "$AGENT_ROOT"
if ! run_doc_sanity; then
  if python3 "$DOC_SANITY_CHECK" "$PROFILE_HOME" 2>&1 | grep -Eq "^(memory|stt)\.provider is '"; then
    if restore_doc_config_backup && run_doc_sanity; then
      log "doc self-heal recovered provider drift before restart"
    else
      SANITY_DETAIL="$(doc_sanity_detail)"
      log "doc profile sanity failed before restart ($SANITY_DETAIL)"
      echo "doc_profile_sanity_failed:$SANITY_DETAIL"
      exit 1
    fi
  else
    SANITY_DETAIL="$(doc_sanity_detail)"
    log "doc profile sanity failed before restart ($SANITY_DETAIL)"
    echo "doc_profile_sanity_failed:$SANITY_DETAIL"
    exit 1
  fi
fi

COMPILE_GATE_OUTPUT="$HERMES_HOME/state/compile-gate/latest.json"
if ! python3 "$COMPILE_GATE_CHECK" --hermes-home "$HERMES_HOME" --out "$COMPILE_GATE_OUTPUT" --json >>"$LOG" 2>&1; then
  COMPILE_DETAIL=$(python3 - "$COMPILE_GATE_OUTPUT" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print("compile_gate_missing_output")
else:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print("compile_gate_unreadable_output")
    else:
        print(str(data.get("detail") or "compile_gate_failed"))
PY
)
  log "compileall failed; refusing restart ($COMPILE_DETAIL)"
  echo "compile_check_failed"
  exit 1
fi

python3 - "$PROFILE_HOME/gateway_state.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if p.exists():
    data = json.loads(p.read_text())
    if data.get("gateway_state") == "running":
        data["exit_reason"] = None
        p.write_text(json.dumps(data))
PY

# Clear stale PID file before restart to prevent PID race loop
PROFILE_PID_FILE="$PROFILE_HOME/gateway.pid"
if [ -f "$PROFILE_PID_FILE" ]; then
  STALE_PID=$(python3 -c "import json; d=json.load(open('$PROFILE_PID_FILE')); print(d.get('pid',''))" 2>/dev/null || true)
  if [ -n "$STALE_PID" ] && ! kill -0 "$STALE_PID" 2>/dev/null; then
    rm -f "$PROFILE_PID_FILE"
    log "cleared stale pid file (dead pid=$STALE_PID)"
  fi
fi
checkpoint_durable_db
cleanup_request_dumps
preflight_runtime_binding || exit 1
log "compile check passed; restarting $UNIT.service (reason=$RESTART_REASON)"
systemctl --user restart "$UNIT.service"
if VERIFY_RESULT=$(verify_profile_health); then
  DOC_NEW_PID=$(systemctl --user show -p MainPID --value "$UNIT.service" 2>/dev/null || true)
  case "$DOC_NEW_PID" in
    ''|0|*[!0-9]*)
      log "Doc restart did not expose a valid MainPID"
      echo "runtime_binding_refresh_failed"
      exit 1
      ;;
  esac
  kill -0 "$DOC_NEW_PID" 2>/dev/null || {
    log "Doc restart MainPID is not alive pid=$DOC_NEW_PID"
    echo "runtime_binding_refresh_failed"
    exit 1
  }
  refresh_runtime_binding "$DOC_NEW_PID" || exit 1
  release_runtime_mutation_lease
  launch_post_restart_hooks
  log "restart verified healthy via systemd"
  echo "ok"
else
  log "restart verification failed after systemd restart ($VERIFY_RESULT)"
  echo "restart_unverified:$VERIFY_RESULT"
  exit 1
fi
