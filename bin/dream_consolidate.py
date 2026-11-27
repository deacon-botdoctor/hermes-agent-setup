#!/usr/bin/env python3
"""
dream_consolidate.py — AKL decay scoring and memory pruning (client-ready version).

Reads MEMORY.md and USER.md, scores every entry on a 1-5 decay scale,
evicts stale entries, deduplicates, and writes back clean files.

Designed to run immediately after dream.py as a nightly cron job.
"""

import json
import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from operator_native_llm import call_native
except ModuleNotFoundError:
    from bin.operator_native_llm import call_native

try:
    from native_memory_limits import char_limit_for_target, automatic_memory_review_enabled
except ModuleNotFoundError:  # Imported from the repository by tests.
    from bin.native_memory_limits import char_limit_for_target, automatic_memory_review_enabled

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MEMORY_DIR = HERMES_HOME / "memories"
LOG_FILE = HERMES_HOME / "logs" / "dream.log"
ARTIFACT_AUDIT = HERMES_HOME / "bin" / "artifact-audit.py"

def read_memory_entries(path):
    if not path.exists():
        return []
    return [e.strip() for e in path.read_text(encoding="utf-8").split("\n§\n") if e.strip()]


def render_memory_entries(entries):
    return "\n§\n".join(e.strip() for e in entries if e.strip())


def write_memory_entries(path, entries, expected_source=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    limit = char_limit_for_target("user" if "USER" in path.name else "memory")
    if len(render_memory_entries(entries)) > limit:
        raise ValueError("Memory exceeds its limit; preserve entries for reviewed consolidation")
    if expected_source is not None and path.read_bytes() != expected_source:
        raise RuntimeError("memory changed during consolidation; preserve the newer version")
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".consolidation-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(render_memory_entries(entries)); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def dedupe_entries(entries):
    return list(dict.fromkeys(e.strip() for e in entries if e.strip()))


logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [consolidate] %(message)s",
)
logger = logging.getLogger("consolidate")


def score_and_prune(entries, target):
    """Send entries to the model for AKL decay scoring, return pruned list."""
    if not entries:
        return entries

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries_text = "\n".join(f"{i+1}. {e}" for i, e in enumerate(entries))

    system_prompt = (
        "You are a memory pruning system using Adaptive Knowledge Lifecycle (AKL) decay scoring. "
        "Score each entry 1-5:\n"
        "  5 = Critical operational rule, hard constraint, never remove\n"
        "  4 = Active project state, current client info\n"
        "  3 = Useful but potentially stale context\n"
        "  2 = Likely stale (completed tasks, resolved issues)\n"
        "  1 = Definitely stale (contradicted, irrelevant, outdated)\n\n"
        "Return ONLY a JSON array of objects with 'index' (1-based) and 'score' (1-5) keys. "
        "No prose, no explanation."
    )

    prompt = (
        f"Today is {today}. This is the '{target}' memory file. "
        f"Score each entry:\n\n{entries_text}"
    )

    try:
        content = call_native(system_prompt, prompt, 512, HERMES_HOME)
        if not content:
            raise RuntimeError("native dream scoring failed")

        # Parse JSON
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        scores = json.loads(content.strip())
        if (not isinstance(scores, list) or len(scores) != len(entries)
                or any(not isinstance(x, dict) or type(x.get("index")) is not int or type(x.get("score")) is not int or not 1 <= x["score"] <= 5 for x in scores)
                or {x["index"] for x in scores} != set(range(1, len(entries)+1))):
            raise ValueError("invalid or incomplete scoring verdict")

        # Build score map
        score_map = {}
        for item in scores:
            idx = item.get("index", 0) - 1  # Convert to 0-based
            score = item.get("score", 3)
            if 0 <= idx < len(entries):
                score_map[idx] = score

        # Evict entries with score 1-2 that have date provenance > 14 days old
        kept = []
        evicted = 0
        for i, entry in enumerate(entries):
            score = score_map.get(i, 3)  # Default to 3 if not scored
            if score <= 2:
                # Check if entry has a date prefix
                if "[20" in entry:
                    try:
                        date_str = entry.split("[")[1].split(" ")[0]
                        entry_date = datetime.strptime(date_str, "%Y-%m-%d")
                        age_days = (datetime.now() - entry_date).days
                        if age_days > 14:
                            evicted += 1
                            logger.info("Evicted (score=%d, age=%dd): %s", score, age_days, entry[:80])
                            continue
                    except (ValueError, IndexError):
                        pass
                # No date prefix but score 1 — evict anyway
                if score <= 1:
                    evicted += 1
                    logger.info("Evicted (score=%d, no date): %s", score, entry[:80])
                    continue
            kept.append(entry)

        if evicted:
            logger.info("Pruned %d/%d entries from %s", evicted, len(entries), target)

        return kept

    except Exception as e:
        raise RuntimeError("native dream scoring failed; memory preserved") from e


def main():
    if not automatic_memory_review_enabled():
        print("Memory maintenance skipped: native automatic review is disabled or unavailable.")
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Consolidation starting")

    for target, filename in [("memory", "MEMORY.md"), ("user", "USER.md")]:
        path = MEMORY_DIR / filename
        if not path.exists():
            continue

        previous_bytes = path.read_bytes()
        previous = previous_bytes.decode("utf-8").replace("\r\n", "\n")
        entries = [e.strip() for e in previous.split("\n§\n") if e.strip()]
        if not entries:
            continue

        # Deduplicate first
        deduped = dedupe_entries(entries)
        if len(deduped) < len(entries):
            logger.info("Deduped %s: %d -> %d entries", filename, len(entries), len(deduped))

        # Score and prune
        pruned = score_and_prune(deduped, target)

        # Capacity alone never authorizes deleting a preference or constraint.
        if len(render_memory_entries(pruned)) > char_limit_for_target(target):
            logger.warning("%s remains over limit; preserved for reviewed consolidation", filename)
            continue
        if pruned != entries:
            recovery = os.environ.get("HERMES_DREAM_RECOVERY_RECEIPT")
            if recovery:
                receipt_path = Path(recovery)
                if receipt_path.is_symlink() or not receipt_path.resolve().is_relative_to(HERMES_HOME / "state/dream-preimages"):
                    raise RuntimeError("recovery receipt is not owned by this maintenance run")
                receipt = json.loads(receipt_path.read_text())
                items = [v for v in receipt["preimages"] if v.get("source") == str(path)]
                if len(items) != 1 or receipt.get("stage") != "before-consolidation":
                    raise RuntimeError("missing exact consolidation recovery preimage")
                backup = Path(items[0]["backup"])
                expected = hashlib.sha256(previous_bytes).hexdigest()
                if (backup.is_symlink() or not backup.resolve().is_relative_to(receipt_path.parent.resolve())
                        or items[0].get("sha256") != expected or hashlib.sha256(backup.read_bytes()).hexdigest() != expected):
                    raise RuntimeError("consolidation recovery preimage does not match")
            else:
                # Backup
                backup = path.with_suffix(f".bak-consolidate-{datetime.now().strftime('%Y%m%d')}")
                if path.exists() and not backup.exists():
                    import shutil
                    shutil.copy2(path, backup)
                elif not path.exists() and backup.exists():
                    import shutil
                    shutil.copy2(backup, path)

            write_memory_entries(path, pruned, previous_bytes)
            logger.info("Consolidated %s: %d -> %d entries", filename, len(entries), len(pruned))
        else:
            logger.info("%s: no changes needed (%d entries)", filename, len(entries))

    if ARTIFACT_AUDIT.exists():
        try:
            import subprocess
            subprocess.run(
                [
                    sys.executable,
                    str(ARTIFACT_AUDIT),
                    "--json-out",
                    str(HERMES_HOME / "state" / "artifact-audit-report.json"),
                    "--md-out",
                    str(HERMES_HOME / "state" / "artifact-audit-report.md"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception as e:
            logger.warning("artifact audit failed: %s", e)

    logger.info("Consolidation complete")


if __name__ == "__main__":
    main()
