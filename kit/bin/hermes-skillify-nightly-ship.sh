#!/usr/bin/env bash
# hermes-skillify-nightly-ship.sh — package Skillify drafts for audit review.
#
# Default transport is a client-local outbox so client runtimes never write into
# operator storage. Set SKILLIFY_SHIP_MODE=scp plus SKILLIFY_SPARK_TARGET and
# SKILLIFY_SPARK_INBOX for legacy remote transport.
# Kill switch: touch ~/.hermes/state/skillify-nightly-ship.pause
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DRAFTS_DIR="$HERMES_HOME/skills/drafts"
DRAFTS_V2_DIR="$HERMES_HOME/skills/drafts-v2"
STATE_FILE="$HERMES_HOME/state/skillify-drafts.jsonl"
STATE_V2_FILE="$HERMES_HOME/state/skillify-v2-drafts.jsonl"
CANDIDATE_FILE="$HERMES_HOME/state/skillify-candidates.jsonl"
CANDIDATE_V2_FILE="$HERMES_HOME/state/skillify-v2-candidates.jsonl"
LOG_DIR="$HERMES_HOME/logs"
STATE_DIR="$HERMES_HOME/state"
PAUSE_FILE="$STATE_DIR/skillify-nightly-ship.pause"
LOG="$LOG_DIR/skillify-nightly-ship.log"
mkdir -p "$LOG_DIR" "$STATE_DIR"

SKILLIFY_SHIP_MODE="${SKILLIFY_SHIP_MODE:-local_outbox}"
SKILLIFY_OUTBOX_DIR="${SKILLIFY_OUTBOX_DIR:-$STATE_DIR/skillify-outbox}"
SPARK_TARGET="${SKILLIFY_SPARK_TARGET:-}"
SPARK_INBOX="${SKILLIFY_SPARK_INBOX:-}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [skillify-ship] $*" >> "$LOG"; }

if [ -f "$PAUSE_FILE" ]; then
    log "paused by $PAUSE_FILE; exiting"
    exit 0
fi

has_drafts=0
for dir in "$DRAFTS_DIR" "$DRAFTS_V2_DIR"; do
    if [ -d "$dir" ] && [ -n "$(find "$dir" -mindepth 1 -maxdepth 1 ! -name '.*' -print -quit 2>/dev/null)" ]; then
        has_drafts=1
    fi
done
if [ "$has_drafts" -eq 0 ]; then
    log "draft dirs empty — nothing to ship"
    exit 0
fi

HOSTNAME_SHORT=$(hostname -s 2>/dev/null || echo unknown)
AGENT_ID="${HERMES_AGENT_ID:-unknown}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BASENAME="skillify-drafts-${HOSTNAME_SHORT}-${AGENT_ID}-${STAMP}"
TARBALL="/tmp/${BASENAME}.tar.gz"
METADATA_COPY="/tmp/${BASENAME}.meta.jsonl"
RECEIPT_COPY="/tmp/${BASENAME}.receipt.json"

# Invoked by the EXIT trap below.
# shellcheck disable=SC2329
cleanup() { rm -f "$TARBALL" "$METADATA_COPY" "$RECEIPT_COPY"; }
trap cleanup EXIT

log "packaging drafts from $DRAFTS_DIR and $DRAFTS_V2_DIR"
TAR_PATHS=()
[ -d "$DRAFTS_DIR" ] && TAR_PATHS+=(drafts)
[ -d "$DRAFTS_V2_DIR" ] && TAR_PATHS+=(drafts-v2)
if ! tar -czf "$TARBALL" -C "$HERMES_HOME/skills" "${TAR_PATHS[@]}" 2>>"$LOG"; then
    log "ERR tar failed"
    exit 1
fi

python3 - "$STATE_FILE" "$CANDIDATE_FILE" "$STATE_V2_FILE" "$CANDIDATE_V2_FILE" "$METADATA_COPY" "$HOSTNAME_SHORT" "$AGENT_ID" <<'PY'
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

state_file = Path(sys.argv[1])
candidate_file = Path(sys.argv[2])
state_v2_file = Path(sys.argv[3])
candidate_v2_file = Path(sys.argv[4])
out_file = Path(sys.argv[5])
hostname_short = sys.argv[6]
agent_id = sys.argv[7]

records: list[dict] = []
seen: set[tuple[str, str]] = set()

def add_record(rec: dict) -> None:
    name = str(rec.get("name") or "").strip()
    sig_hash = str(rec.get("signature_hash") or "").strip()
    key = (name, sig_hash)
    if not name or not sig_hash or key in seen:
        return
    seen.add(key)
    records.append(rec)

for source_file in (state_file, state_v2_file):
    if source_file.exists():
        for line in source_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            add_record(rec)

for source_file, schema_version in ((candidate_file, 1), (candidate_v2_file, 2)):
    if source_file.exists():
        for line in source_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            draft_dir = Path(str(rec.get("draft_dir") or ""))
            signature = str(rec.get("signature") or "").strip()
            name = str(rec.get("name") or draft_dir.name).strip()
            if not draft_dir.exists() or not signature or not name:
                continue
            trigger = rec.get("trigger")
            if not trigger:
                trigger = f"Passive scan observed {rec.get('count') or rec.get('occurrences') or 0} related items across {', '.join(rec.get('agents', []))}."
            synth = {
                "ts": rec.get("ts"),
                "name": name,
                "type": rec.get("type") or ("latent-v2" if schema_version == 2 else "latent"),
                "trigger": trigger,
                "signature": signature,
                "signature_hash": rec.get("signature_hash") or hashlib.sha256(signature.encode()).hexdigest()[:16],
                "draft_dir": str(draft_dir),
                "agent_id": rec.get("agent_id") or agent_id,
                "host": rec.get("host") or hostname_short,
                "has_script": any(p.name.startswith("script.") for p in draft_dir.iterdir()),
                "has_context": (draft_dir / "context.log").exists() or (draft_dir / "SKILL.md").exists(),
                "schema_version": schema_version,
                "promotion_score": rec.get("promotion_score") or rec.get("score"),
            }
            add_record(synth)

out_file.write_text("\n".join(json.dumps(rec) for rec in records) + ("\n" if records else ""), encoding="utf-8")
PY

python3 - "$RECEIPT_COPY" "$TARBALL" "$METADATA_COPY" "$HOSTNAME_SHORT" "$AGENT_ID" "$STAMP" "$SKILLIFY_SHIP_MODE" <<'PY'
from __future__ import annotations
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

receipt = Path(sys.argv[1])
files = [Path(sys.argv[2]), Path(sys.argv[3])]
host = sys.argv[4]
agent_id = sys.argv[5]
stamp = sys.argv[6]
mode = sys.argv[7]
items = []
for path in files:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    items.append({"name": path.name, "sha256": h.hexdigest(), "size": path.stat().st_size})
receipt.write_text(json.dumps({
    "schema_version": 1,
    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "agent_id": agent_id,
    "host": host,
    "stamp": stamp,
    "ship_mode": mode,
    "files": items,
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

case "$SKILLIFY_SHIP_MODE" in
    local_outbox)
        mkdir -p "$SKILLIFY_OUTBOX_DIR"
        chmod 700 "$SKILLIFY_OUTBOX_DIR" 2>/dev/null || true
        log "staging to local outbox $SKILLIFY_OUTBOX_DIR"
        if ! install -m 600 "$TARBALL" "$METADATA_COPY" "$RECEIPT_COPY" "$SKILLIFY_OUTBOX_DIR/" >>"$LOG" 2>&1; then
            log "ERR local outbox stage failed — drafts retained for next tick"
            exit 1
        fi
        ;;
    scp)
        if [ -z "$SPARK_TARGET" ] || [ -z "$SPARK_INBOX" ]; then
            log "ERR scp mode requires SKILLIFY_SPARK_TARGET and SKILLIFY_SPARK_INBOX"
            exit 2
        fi
        log "shipping to $SPARK_TARGET:$SPARK_INBOX/"
        if ! scp -o BatchMode=yes -o ConnectTimeout=30 \
            "$TARBALL" "$METADATA_COPY" "$RECEIPT_COPY" \
            "$SPARK_TARGET:$SPARK_INBOX/" >>"$LOG" 2>&1; then
            log "ERR scp failed — drafts retained for next tick"
            exit 1
        fi
        ;;
    *)
        log "ERR unsupported SKILLIFY_SHIP_MODE=$SKILLIFY_SHIP_MODE"
        exit 2
        ;;
esac

log "staged successfully via $SKILLIFY_SHIP_MODE"

archives=()
# Replaces skill_archive_exclusion_v2 for newly shipped archives.
archive_root="$STATE_DIR/skill-package-archives/skillify"
mkdir -p "$archive_root"
for dir in "$DRAFTS_DIR" "$DRAFTS_V2_DIR"; do
    if [ -d "$dir" ]; then
        shipped="$archive_root/$(basename "$dir").shipped-${STAMP}"
        [ ! -e "$shipped" ] || { log "ERR archive already exists: $shipped"; exit 1; }
        mv "$dir" "$shipped"
        mkdir -p "$dir"
        # Generated archive names use the fixed UTC stamp above.
        # shellcheck disable=SC2012
        ls -dt "$archive_root/$(basename "$dir").shipped-"* 2>/dev/null | tail -n +4 | xargs -I{} rm -rf {} 2>/dev/null || true
        archives+=("$shipped")
    fi
done

for f in "$STATE_FILE" "$STATE_V2_FILE" "$CANDIDATE_FILE" "$CANDIDATE_V2_FILE"; do
    if [ -f "$f" ]; then
        tail -50 "$f" > "${f}.tmp" && mv "${f}.tmp" "$f"
    fi
done

log "local archives: ${archives[*]}"
exit 0
