"""Keep the previous session activity separate from the live watchdog clock."""
import logging
import math
import time


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def capture(agent):
    """Capture once before gateway or facade startup refreshes activity."""
    if getattr(agent, "compression_idle_compact_after_seconds", 0) <= 0:
        agent.__dict__.pop("_idle_compaction_previous_activity", None)
        return
    sid = getattr(agent, "session_id", None)
    existing = getattr(agent, "_idle_compaction_previous_activity", None)
    if isinstance(existing, tuple) and existing[0] == sid:
        return
    previous = _timestamp(getattr(agent, "_last_activity_ts", None))
    # A rebuilt agent's initialization timestamp is not session activity.
    if getattr(agent, "_last_activity_desc", None) == "initializing":
        previous = None
        db = getattr(agent, "_session_db", None)
        if db is not None and sid:
            try:
                row = db.get_session(sid) or {}
                messages = db.get_messages(sid, limit=1, latest=True)
                stamps = [_timestamp(row.get("last_activity_at"))]
                stamps += [_timestamp(message.get("timestamp")) for message in messages]
                previous = max((stamp for stamp in stamps if stamp is not None), default=None)
            except Exception:
                logging.getLogger("run_agent").warning(
                    "Idle compaction activity lookup unavailable; skipping idle trigger"
                )
    agent._idle_compaction_previous_activity = (sid, previous or time.time())


def consume(agent):
    """Consume this turn's snapshot, never carrying it into another turn."""
    saved = agent.__dict__.pop("_idle_compaction_previous_activity", None)
    if isinstance(saved, tuple) and saved[0] == getattr(agent, "session_id", None):
        return saved[1]
    return getattr(agent, "_last_activity_ts", time.time())
