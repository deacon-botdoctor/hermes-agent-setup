#!/usr/bin/env python3
"""
dream_consolidate.py — AKL decay scoring and memory pruning (client-ready version).

Reads MEMORY.md and USER.md, scores every entry on a 1-5 decay scale,
evicts stale entries, deduplicates, and writes back clean files.

Designed to run immediately after dream.py as a nightly cron job.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

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


def write_memory_entries(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    limit = char_limit_for_target("user" if "USER" in path.name else "memory")
    if len(render_memory_entries(entries)) > limit:
        raise ValueError("Memory exceeds its limit; preserve entries for reviewed consolidation")
    path.write_text(render_memory_entries(entries), encoding="utf-8")


def dedupe_entries(entries):
    return list(dict.fromkeys(e.strip() for e in entries if e.strip()))


logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [consolidate] %(message)s",
)
logger = logging.getLogger("consolidate")


def load_config():
    """Read model config from config.yaml."""
    try:
        import yaml
    except ImportError:
        return None, None, None

    config_path = HERMES_HOME / "config.yaml"
    if not config_path.exists():
        return None, None, None

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    aux = cfg.get("auxiliary", {}) or {}
    for key in ("dream", "compression", "session_search", "approval"):
        section = aux.get(key, {}) or {}
        provider = section.get("provider", "")
        base_url = section.get("base_url") or ""
        if not base_url and provider == "openrouter":
            base_url = "https://openrouter.ai/api/v1"
        if section.get("model") and base_url:
            model = section["model"]
            base_url = base_url.rstrip("/")
            api_key = section.get("api_key", "")
            if not api_key:
                env_key = section.get("api_key_env", "")
                if env_key:
                    api_key = os.environ.get(env_key, "")
                    if not api_key:
                        env_file = HERMES_HOME / ".env"
                        if env_file.exists():
                            for line in env_file.read_text(encoding="utf-8").splitlines():
                                if line.startswith(f"{env_key}="):
                                    api_key = line.split("=", 1)[1].strip().strip("'\"")
                                    break
            if model and base_url and api_key:
                return model, base_url, api_key

    m = cfg.get("model", {}) or {}
    model = m.get("default", "")
    base_url = m.get("base_url", "")
    api_key = ""
    env_key = m.get("api_key_env", "")
    if env_key:
        api_key = os.environ.get(env_key, "")
        if not api_key:
            env_file = HERMES_HOME / ".env"
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith(f"{env_key}="):
                        api_key = line.split("=", 1)[1].strip().strip("'\"")
                        break
    if not api_key:
        api_key = m.get("api_key", "")
    return model, base_url, api_key


def score_and_prune(entries, target, model, base_url, api_key):
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
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 512,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code != 200:
            logger.warning("API error %d during scoring: %s", resp.status_code, resp.text[:200])
            return entries  # Return unmodified on error

        data = resp.json()
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        content = message.get("content") or ""

        # Parse JSON
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        scores = json.loads(content.strip())
        if not isinstance(scores, list):
            return entries

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
        logger.warning("Scoring failed: %s", e)
        return entries  # Return unmodified on error


def main():
    if not automatic_memory_review_enabled():
        print("Memory maintenance skipped: native automatic review is disabled or unavailable.")
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Consolidation starting")

    model, base_url, api_key = load_config()
    if not model or not base_url or not api_key:
        logger.error("No model configured for consolidation")
        return

    for target, filename in [("memory", "MEMORY.md"), ("user", "USER.md")]:
        path = MEMORY_DIR / filename
        if not path.exists():
            continue

        entries = read_memory_entries(path)
        if not entries:
            continue

        # Deduplicate first
        deduped = dedupe_entries(entries)
        if len(deduped) < len(entries):
            logger.info("Deduped %s: %d -> %d entries", filename, len(entries), len(deduped))

        # Score and prune
        pruned = score_and_prune(deduped, target, model, base_url, api_key)

        # Capacity alone never authorizes deleting a preference or constraint.
        if len(render_memory_entries(pruned)) > char_limit_for_target(target):
            logger.warning("%s remains over limit; preserved for reviewed consolidation", filename)
            continue
        if pruned != entries:
            # Backup
            backup = path.with_suffix(f".bak-consolidate-{datetime.now().strftime('%Y%m%d')}")
            if path.exists() and not backup.exists():
                import shutil
                shutil.copy2(path, backup)
            elif not path.exists() and backup.exists():
                import shutil
                shutil.copy2(backup, path)

            write_memory_entries(path, pruned)
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
