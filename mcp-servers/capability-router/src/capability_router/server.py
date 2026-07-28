"""capability-router MCP — discovery layer for Hermes' growing capability set.

Solves the 200-tool prompt-bloat problem: agents have ONE small MCP loaded
into their toolset (this one), and use it to discover which backend
capability matches their need. Then they invoke the backend MCP's tool
directly. The router is an INDEX, not a proxy: it reports whether a backend is
ready, cold but policy-allowed, or unavailable. Cold backends must be activated
in the current gateway with ``restart_mcp_server`` and searched again before
their newly registered tools can be invoked.

Tools:
  - list_categories()                                — broad capability categories
  - search_capabilities(query, category?)            — keyword search + ranking
  - describe_capability(capability_id)               — full schema for one capability
  - record_capability_outcome(capability_id, ...)    — log success/failure (Tier 3)
  - registry_status()                                — server health + registry summary

Ranking signals (in addition to keyword overlap):
  - `preferred_for` array on a capability boosts it when query tokens match.
  - `deprioritize_for` array penalizes it for those tokens.
  - Per-host usage ledger (sqlite) boosts capabilities that succeeded recently
    on this host, demotes ones that failed recently.

Registry: $HERMES_HOME/mcp-servers/capability-router/registry.json
Usage db: $HERMES_HOME/state/capability-router-usage.db
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

SERVER_NAME = "capability-router"
mcp = FastMCP(SERVER_NAME)

DEFAULT_HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()

REGISTRY_PATH = Path(
    os.environ.get(
        "CAPABILITY_REGISTRY",
        str(DEFAULT_HERMES_HOME / "mcp-servers" / "capability-router" / "registry.json"),
    )
).expanduser()

USAGE_DB_PATH = Path(
    os.environ.get(
        "CAPABILITY_USAGE_DB",
        str(DEFAULT_HERMES_HOME / "state" / "capability-router-usage.db"),
    )
).expanduser()

ACTIVATION_STATE_DIR = DEFAULT_HERMES_HOME / "state" / "mcp-activation"
ACTIVATION_STATE_MAX_AGE_S = 15.0

# Ranking constants — tuned so explicit preferences dominate keyword noise.
PREFERRED_BOOST = 10  # +10 if any query token matches a preferred_for entry
DEPRIO_PENALTY = 5  # -5  if any query token matches a deprioritize_for entry
USAGE_DECAY_DAYS = 30
USAGE_SUCCESS_BOOST = 3  # per recent success
USAGE_FAILURE_PENALTY = 2  # per recent failure
USAGE_BOOST_CAP = 6  # cap on positive usage signal per capability
USAGE_PENALTY_CAP = 6  # cap on negative usage signal per capability


# ---- registry loading ----


def _load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {
            "schema_version": 1,
            "categories": [],
            "capabilities": [],
            "_error": f"registry not found: {REGISTRY_PATH}",
        }
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "schema_version": 1,
            "categories": [],
            "capabilities": [],
            "_error": f"registry parse failed: {exc}",
        }


# === 2026-04-28: per-host self-filter ====================================
# _DECLARED_MCP_SERVERS_FILTER_SENTINEL
# Filter search_capabilities + describe_capability results by host's actual
# mcp_servers declarations. Fail-safe: empty declared (parse failure / no
# config.yaml / pyyaml missing) -> no filter, behavior unchanged.

_DECLARED_CACHE: dict[str, bool] | None = None
_DECLARED_CACHE_AT: float = 0.0
_DECLARED_CACHE_TTL_S = 600.0  # 10 min — gateway restart picks up config edits anyway
_ROUTER_CONFIG_CACHE: dict[str, Any] | None = None
_ROUTER_CONFIG_CACHE_AT: float = 0.0


def _load_router_config() -> dict[str, Any]:
    """Load policy/toolset config once per gateway cache window."""
    global _ROUTER_CONFIG_CACHE, _ROUTER_CONFIG_CACHE_AT
    now = time.time()
    if _ROUTER_CONFIG_CACHE is not None and (now - _ROUTER_CONFIG_CACHE_AT) < _DECLARED_CACHE_TTL_S:
        return _ROUTER_CONFIG_CACHE
    try:
        import yaml  # type: ignore[import-not-found]

        cfg = yaml.safe_load((DEFAULT_HERMES_HOME / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    _ROUTER_CONFIG_CACHE = cfg if isinstance(cfg, dict) else {}
    _ROUTER_CONFIG_CACHE_AT = now
    return _ROUTER_CONFIG_CACHE


def _load_mcp_server_states() -> dict[str, bool]:
    """Return configured MCP names mapped to their persistent enabled state.

    HERMES_HOME env var locates the config; falls back to ~/.hermes. If
    pyyaml is unavailable or the file can't be parsed, return an empty mapping
    (caller treats this as 'do not filter')."""
    global _DECLARED_CACHE, _DECLARED_CACHE_AT
    now = time.time()
    if _DECLARED_CACHE is not None and (now - _DECLARED_CACHE_AT) < _DECLARED_CACHE_TTL_S:
        return _DECLARED_CACHE
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        _DECLARED_CACHE = {}
        _DECLARED_CACHE_AT = now
        return _DECLARED_CACHE
    cfg_path = DEFAULT_HERMES_HOME / "config.yaml"
    if not cfg_path.exists():
        _DECLARED_CACHE = {}
        _DECLARED_CACHE_AT = now
        return _DECLARED_CACHE
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        _DECLARED_CACHE = {}
        _DECLARED_CACHE_AT = now
        return _DECLARED_CACHE
    if not isinstance(cfg, dict):
        _DECLARED_CACHE = {}
        _DECLARED_CACHE_AT = now
        return _DECLARED_CACHE
    mcp_servers = cfg.get("mcp_servers") or {}
    if not isinstance(mcp_servers, dict):
        _DECLARED_CACHE = {}
        _DECLARED_CACHE_AT = now
        return _DECLARED_CACHE
    states: dict[str, bool] = {}
    for name, block in mcp_servers.items():
        if isinstance(block, dict):
            states[str(name)] = block.get("enabled") is not False
    _DECLARED_CACHE = states
    _DECLARED_CACHE_AT = now
    return _DECLARED_CACHE


def _load_declared_mcp_servers() -> set[str]:
    """Return configured MCP servers that Hermes starts eagerly."""
    return {name for name, enabled in _load_mcp_server_states().items() if enabled}


def _load_configured_mcp_servers() -> set[str]:
    """Return all configured MCP servers, including cold on-demand entries."""
    return set(_load_mcp_server_states())


def _load_policy_allowed_mcp_servers() -> set[str]:
    cfg = _load_router_config()
    policy = cfg.get("mcp_policy") or {}
    if not isinstance(policy, dict):
        return set()
    allowed: set[str] = set()
    for key in ("on_demand", "active_enabled", "hot_path", "hot_path_enabled"):
        value = policy.get(key)
        if isinstance(value, (list, tuple, set)):
            allowed.update(str(item).strip() for item in value if str(item).strip())
    denied: set[str] = set()
    for key in ("disabled", "on_demand_disabled"):
        value = policy.get(key)
        if isinstance(value, (list, tuple, set)):
            denied.update(str(item).strip() for item in value if str(item).strip())
    return allowed - denied


def _router_platforms_with_backend(server_name: str) -> bool:
    cfg = _load_router_config()
    toolsets = cfg.get("platform_toolsets") or {}
    if not isinstance(toolsets, dict):
        return False
    router_platforms = [
        names
        for names in toolsets.values()
        if isinstance(names, (list, tuple, set)) and "mcp-capability-router" in names
    ]
    required = {"mcp-on-demand-control", f"mcp-{server_name}"}
    return bool(router_platforms) and all(required <= set(names) for names in router_platforms)


def _load_control_activatable_mcp_servers() -> set[str]:
    cfg = _load_router_config()
    plugins = cfg.get("plugins") or {}
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else []
    if not isinstance(enabled, list) or "mcp-on-demand-control" not in enabled:
        return set()
    return {name for name in _load_policy_allowed_mcp_servers() if _router_platforms_with_backend(name)}


def _load_runtime_active_mcp_servers() -> set[str]:
    host_pid = os.getppid()
    receipt_path = ACTIVATION_STATE_DIR / f"{host_pid}.json"
    try:
        state = json.loads(receipt_path.read_text(encoding="utf-8"))
        if int(state.get("host_pid")) != host_pid:
            return set()
        receipt_age = time.time() - float(state.get("verified_at"))
        if receipt_age < -5.0 or receipt_age > ACTIVATION_STATE_MAX_AGE_S:
            return set()
        os.kill(host_pid, 0)
        active = state.get("active_servers")
    except (OSError, ValueError, TypeError, AttributeError):
        return set()
    if not isinstance(active, list):
        return set()
    return {str(name) for name in active if str(name)}


def _is_installed(cap: dict[str, Any], declared: set[str]) -> bool:
    """Return the legacy ``installed`` view of current invocation readiness.

    A backend capability is ready when its server is eager or active in this
    gateway process. Catalog/meta entries without a server are always ready.
    """
    srv = cap.get("mcp_server")
    if not srv:
        return True
    return srv in declared


def _filter_caps_by_install(caps: list[dict[str, Any]], include_uninstalled: bool) -> list[dict[str, Any]]:
    """Filter capabilities to those configured on this host unless overridden.

    If server state cannot be read, preserve the historical fail-safe and do
    not filter; never strip the catalog because of a config-read glitch.
    """
    if include_uninstalled:
        return caps
    states = _load_mcp_server_states()
    if not states:
        return caps  # fail-safe
    configured = _load_configured_mcp_servers() | _load_runtime_active_mcp_servers()
    return [c for c in caps if _is_installed(c, configured)]


# === end 2026-04-28 self-filter ==========================================


def _capability_availability(cap: dict[str, Any], active: set[str] | None = None) -> dict[str, Any]:
    # Return per-host discovery state. Discovery is broad; invocation remains gated.
    if active is None:
        active = _load_runtime_active_mcp_servers()
    srv = cap.get("mcp_server")
    if not srv:
        return {
            "availability": "catalog",
            "can_invoke_now": True,
            "route_hint": "Catalog/meta capability; no backend MCP server required.",
        }
    if srv in active and str(srv) in _load_policy_allowed_mcp_servers() and _router_platforms_with_backend(str(srv)):
        return {
            "availability": "active",
            "can_invoke_now": True,
            "activation_required": False,
            "route_hint": f"Backend MCP {srv!r} is live in this gateway process.",
        }
    if srv in _load_configured_mcp_servers() and srv in _load_control_activatable_mcp_servers():
        return {
            "availability": "cold",
            "can_invoke_now": False,
            "activation_required": True,
            "status_tool": "mcp_server_status",
            "activation_tool": "restart_mcp_server",
            "route_hint": (
                f"Backend MCP {srv!r} is configured as cold. Use tool_search for "
                f"mcp_server_status and check server_name={srv!r}; if it is not connected, "
                f"invoke restart_mcp_server for that server, then run tool_search again "
                f"for the capability tool."
            ),
        }
    states = _load_mcp_server_states()
    if states.get(str(srv)) is True:
        return {
            "availability": "configured_unverified",
            "can_invoke_now": False,
            "activation_required": False,
            "route_hint": (
                f"Backend MCP {srv!r} is enabled in config but has no fresh live-gateway receipt. "
                "Treat it as unavailable and inspect gateway MCP health before invoking."
            ),
        }
    local_server_dir = DEFAULT_HERMES_HOME / "mcp-servers" / str(srv)
    if local_server_dir.exists():
        availability = "installed_not_declared"
        hint = (
            f"Backend MCP {srv!r} exists on disk but is not declared/enabled in config.yaml. "
            "Activate it before invoking."
        )
    else:
        availability = "not_declared"
        hint = (
            f"Backend MCP {srv!r} is not declared on this host. "
            "Route to the owning lane or install/enable intentionally before invoking."
        )
    return {
        "availability": availability,
        "can_invoke_now": False,
        "activation_required": False,
        "route_hint": hint,
    }


def _capability_summary(cap: dict[str, Any], active: set[str] | None = None) -> dict[str, Any]:
    availability = _capability_availability(cap, active)
    return {
        "id": cap.get("id"),
        "kind": cap.get("kind"),
        "category": cap.get("category"),
        "label": cap.get("label"),
        "summary": cap.get("summary"),
        "mcp_server": cap.get("mcp_server"),
        "tool_name": cap.get("tool_name"),
        "cost_per_call": cap.get("cost_per_call"),
        "requires_creds": cap.get("requires_creds") or [],
        "installed": availability["can_invoke_now"],
        "availability": availability["availability"],
        "can_invoke_now": availability["can_invoke_now"],
        "activation_required": availability.get("activation_required", False),
        "activation_tool": availability.get("activation_tool"),
        "status_tool": availability.get("status_tool"),
        "route_hint": availability["route_hint"],
        "preferred_for": cap.get("preferred_for") or [],
        "deprioritize_for": cap.get("deprioritize_for") or [],
    }


# ---- usage ledger (Tier 3) ----


def _usage_db_init() -> None:
    USAGE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                capability_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                query TEXT,
                error_class TEXT,
                duration_ms INTEGER,
                failure_detail TEXT,
                ts INTEGER NOT NULL
            )"""
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(outcomes)").fetchall()}
        if "duration_ms" not in existing:
            conn.execute("ALTER TABLE outcomes ADD COLUMN duration_ms INTEGER")
        if "failure_detail" not in existing:
            conn.execute("ALTER TABLE outcomes ADD COLUMN failure_detail TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cap_ts ON outcomes(capability_id, ts)")
        conn.commit()


@contextmanager
def _usage_conn():
    _usage_db_init()
    conn = sqlite3.connect(USAGE_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _usage_signal(capability_id: str, conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Return (signal, success_count, failure_count) for capability in decay window."""
    cutoff = int(time.time()) - USAGE_DECAY_DAYS * 86400
    rows = conn.execute(
        "SELECT outcome, COUNT(*) FROM outcomes WHERE capability_id=? AND ts>=? GROUP BY outcome",
        (capability_id, cutoff),
    ).fetchall()
    successes = 0
    failures = 0
    for outcome, count in rows:
        if outcome == "success":
            successes = count
        elif outcome == "failure":
            failures = count
    boost = min(successes * USAGE_SUCCESS_BOOST, USAGE_BOOST_CAP)
    penalty = min(failures * USAGE_FAILURE_PENALTY, USAGE_PENALTY_CAP)
    return (boost - penalty, successes, failures)


def _usage_summary_by_capability(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    cutoff = int(time.time()) - USAGE_DECAY_DAYS * 86400
    rows = conn.execute(
        """SELECT capability_id, outcome, COUNT(*)
           FROM outcomes
           WHERE ts >= ?
           GROUP BY capability_id, outcome
           ORDER BY capability_id""",
        (cutoff,),
    ).fetchall()
    by_cap: dict[str, dict[str, Any]] = {}
    for capability_id, outcome, count in rows:
        item = by_cap.setdefault(
            capability_id,
            {"capability_id": capability_id, "success": 0, "failure": 0, "other": 0},
        )
        if outcome == "success":
            item["success"] = count
        elif outcome == "failure":
            item["failure"] = count
        else:
            item["other"] += count
    out = []
    for capability_id, item in by_cap.items():
        signal, _, _ = _usage_signal(capability_id, conn)
        item["usage_score"] = signal
        item["total"] = item["success"] + item["failure"] + item["other"]
        out.append(item)
    out.sort(key=lambda x: (-x["total"], x["capability_id"]))
    return out[:limit]


# ---- ranking ----


def _score_capability(
    cap: dict[str, Any],
    q_tokens: list[str],
    usage_conn: sqlite3.Connection | None,
) -> tuple[int, dict[str, int]]:
    """Compute total score + breakdown for transparency."""
    haystack = " ".join(
        [
            str(cap.get("id", "")),
            str(cap.get("label", "")),
            str(cap.get("summary", "")),
            str(cap.get("category", "")),
        ]
    ).lower()

    keyword_score = sum(1 for t in q_tokens if t in haystack)

    preferred_for = [p.lower() for p in (cap.get("preferred_for") or [])]
    deprioritize_for = [p.lower() for p in (cap.get("deprioritize_for") or [])]

    preference_score = 0
    # Skip 1-2 char tokens (a, an, of, to, in, is, on) — they substring-match
    # bigger preferred entries like 'create'/'imagen' and inflate the score on
    # generic asks. Stop-list keeps ranking specific, not noisy.
    sig_tokens = [t for t in q_tokens if len(t) >= 3]
    if sig_tokens:
        if any(t in p for t in sig_tokens for p in preferred_for):
            preference_score += PREFERRED_BOOST
        if any(t in p for t in sig_tokens for p in deprioritize_for):
            preference_score -= DEPRIO_PENALTY

    usage_score = 0
    successes = 0
    failures = 0
    if usage_conn is not None:
        cap_id = cap.get("id", "")
        if cap_id:
            usage_score, successes, failures = _usage_signal(cap_id, usage_conn)

    total = keyword_score + preference_score + usage_score
    return (
        total,
        {
            "keyword": keyword_score,
            "preference": preference_score,
            "usage": usage_score,
            "usage_successes": successes,
            "usage_failures": failures,
        },
    )


# ---- tools ----


@mcp.tool()
def list_categories() -> dict[str, Any]:
    """List all capability categories with counts.

    Use this first to browse the capability landscape. Returns each category
    with its description and the count of capabilities in it. Then use
    `search_capabilities` with a category filter to drill in.
    """
    reg = _load_registry()
    cats = reg.get("categories") or []
    caps = reg.get("capabilities") or []
    counts: dict[str, int] = {}
    for cap in caps:
        counts[cap.get("category", "")] = counts.get(cap.get("category", ""), 0) + 1
    enriched = [
        {
            "id": c["id"],
            "label": c.get("label"),
            "description": c.get("description"),
            "capability_count": counts.get(c["id"], 0),
        }
        for c in cats
    ]
    return {
        "ok": True,
        "schema_version": reg.get("schema_version"),
        "registry_path": str(REGISTRY_PATH),
        "category_count": len(cats),
        "total_capabilities": len(caps),
        "categories": enriched,
        "error": reg.get("_error"),
    }


@mcp.tool()
def search_capabilities(
    query: str = "",
    category: str = "",
    max_hits: int = 10,
    include_uninstalled: bool = True,
) -> dict[str, Any]:
    """Find capabilities matching a query and/or category.

    Args:
        query: Keyword(s) to match against capability label, summary, and id.
               Empty string returns all (filtered by category if given).
        category: Optional category id filter (see `list_categories`).
        max_hits: Cap returned hits.
        include_uninstalled: Defaults true so discovery shows cold/other-lane capabilities.

    Ranking: keyword overlap + preferred_for boost + deprioritize_for penalty
    + usage history (recent success boost / recent failure demote).

    Concrete hits carry `mcp_server` + `tool_name` routing metadata. A cold
    server may instead return one activation marker with `tool_name=None`.
    Every hit includes a `score_breakdown` showing why it ranked where it did.
    Invoke directly only when `can_invoke_now` is true; cold backends include
    an activation route.
    """
    reg = _load_registry()
    caps = reg.get("capabilities") or []
    active = _load_runtime_active_mcp_servers()
    caps = _filter_caps_by_install(caps, include_uninstalled)
    if category:
        caps = [c for c in caps if c.get("category") == category]
    q_tokens = [t.lower() for t in re.split(r"\W+", query) if t]

    scored: list[tuple[int, dict[str, int], dict[str, Any]]] = []

    with _usage_conn() as usage_conn:
        for c in caps:
            score, breakdown = _score_capability(c, q_tokens, usage_conn)
            # When query is empty, surface everything (filtered by category).
            # When query is non-empty, require positive base interest:
            # either keyword hit OR explicit preference OR positive usage.
            if not q_tokens:
                scored.append((score, breakdown, c))
            elif breakdown["keyword"] > 0 or breakdown["preference"] > 0 or score > 0:
                scored.append((score, breakdown, c))

    scored.sort(key=lambda x: -x[0])

    hits = []
    for score, breakdown, c in scored[:max_hits]:
        h = _capability_summary(c, active)
        h["score"] = score
        h["score_breakdown"] = breakdown
        hits.append(h)

    return {
        "ok": True,
        "query": query,
        "category": category or None,
        "result_count": len(scored),
        "truncated": len(scored) > max_hits,
        "hits": hits,
        "next_step": (
            "Call describe_capability(capability_id) for full schema, OR "
            "invoke the underlying tool directly only when can_invoke_now=true. "
            "If can_invoke_now=false, follow route_hint instead. After invoking, call "
            "record_capability_outcome(capability_id, outcome) so the router "
            "learns what works on this host."
        ),
    }


@mcp.tool()
def describe_capability(capability_id: str, include_uninstalled: bool = False) -> dict[str, Any]:
    """Return full schema for a capability (inputs, outputs, cost, creds).

    Use after `search_capabilities` to inspect a concrete backend MCP tool. Its
    `mcp_server` + `tool_name` identify the actual function. A server activation
    marker has `tool_name=None` and documents discovery only, not cached tool
    schemas. By default a cold or unavailable backend returns its routing state
    instead of a schema; activate a cold backend first, or set
    `include_uninstalled=True` to inspect an available catalog entry.
    """
    reg = _load_registry()
    active = _load_runtime_active_mcp_servers()
    for c in reg.get("capabilities") or []:
        if c.get("id") != capability_id:
            continue
        # Capability found — check invocation state unless override. Preserve
        # the historical parse-failure fail-safe when both config views are
        # empty, but do not mislabel an explicitly configured cold server as
        # invokable merely because every configured server is cold.
        availability = _capability_availability(c, active)
        configured = _load_configured_mcp_servers()
        known_runtime_state = bool(active or configured)
        if not include_uninstalled and known_runtime_state and not availability["can_invoke_now"]:
            return {
                "ok": False,
                "error": "capability_not_installed",
                "capability_id": capability_id,
                "mcp_server_required": c.get("mcp_server"),
                "mcp_servers_active": sorted(active),
                "availability": availability["availability"],
                "activation_required": availability.get("activation_required", False),
                "activation_tool": availability.get("activation_tool"),
                "hint": availability["route_hint"],
            }
        # Found and installed (or override) — fall through to existing logic
        if True:
            return {"ok": True, "capability": c}
    available = [c.get("id") for c in (reg.get("capabilities") or [])]
    return {
        "ok": False,
        "error": f"capability not found: {capability_id}",
        "hint": "use search_capabilities to find available IDs",
        "available_ids_sample": available[:20],
    }


@mcp.tool()
def record_capability_outcome(
    capability_id: str,
    outcome: str = "",
    ok: bool | None = None,
    query: str = "",
    error_class: str = "",
    failure_class: str = "",
    failure_detail: str = "",
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Record whether a capability invocation succeeded or failed on this host.

    Call this after invoking a capability you found via `search_capabilities`,
    so the router can rank tools that actually work for you higher in future
    searches and demote ones that have been failing.

    Args:
        capability_id: The id from search_capabilities (e.g. "catalog.maton-calendar").
        outcome: "success" or "failure". Optional when ok is supplied.
        ok: Optional boolean alias for outcome; true records success, false records failure.
        query: Optional — the search query that led you here. Used by future
               improvements to make ranking query-aware.
        error_class: Optional — short tag for the error if outcome=="failure"
                     ("auth", "network", "ratelimit", "schema", "timeout", "other").
                     Helps disambiguate transient vs permanent failures later.
        failure_class: Alias for error_class used by runtime classifiers.
        failure_detail: Optional short failure detail for audit/debug.
        duration_ms: Optional duration of the underlying capability call.
    """
    if not capability_id:
        return {"ok": False, "error": "capability_id required"}
    if not outcome:
        if ok is True:
            outcome = "success"
        elif ok is False:
            outcome = "failure"
    if not outcome:
        return {"ok": False, "error": "outcome or ok required"}
    error_tag = error_class or failure_class
    now = int(time.time())
    try:
        with _usage_conn() as conn:
            conn.execute(
                """INSERT INTO outcomes
                   (capability_id, outcome, query, error_class, duration_ms, failure_detail, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    capability_id,
                    outcome,
                    query or None,
                    error_tag or None,
                    duration_ms,
                    failure_detail[:500] if failure_detail else None,
                    now,
                ),
            )
            conn.commit()
            recent = conn.execute(
                "SELECT outcome, COUNT(*) FROM outcomes WHERE capability_id=? AND ts >= ? GROUP BY outcome",
                (capability_id, now - USAGE_DECAY_DAYS * 86400),
            ).fetchall()
            signal, successes, failures = _usage_signal(capability_id, conn)
    except Exception as exc:
        return {
            "ok": False,
            "recorded": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "capability_id": capability_id,
        }
    return {
        "ok": True,
        "recorded": True,
        "record": {
            "capability_id": capability_id,
            "outcome": outcome,
            "query": query or None,
            "error_class": error_tag or None,
            "duration_ms": duration_ms,
            "ts": now,
        },
        "recent_window_days": USAGE_DECAY_DAYS,
        "recent_counts": {o: c for o, c in recent},
        "usage_score": signal,
        "usage_successes": successes,
        "usage_failures": failures,
    }


@mcp.tool()
def registry_status() -> dict[str, Any]:
    """Server status, registry path, and high-level counts."""
    reg = _load_registry()
    cats = reg.get("categories") or []
    caps = reg.get("capabilities") or []
    cap_by_server: dict[str, int] = {}
    for c in caps:
        srv = c.get("mcp_server", "")
        cap_by_server[srv] = cap_by_server.get(srv, 0) + 1
    preferred_count = sum(1 for c in caps if c.get("preferred_for"))
    deprio_count = sum(1 for c in caps if c.get("deprioritize_for"))

    usage_total = 0
    usage_successes = 0
    usage_failures = 0
    usage_by_capability: list[dict[str, Any]] = []
    try:
        with _usage_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()
            usage_total = row[0] if row else 0
            for o, c in conn.execute("SELECT outcome, COUNT(*) FROM outcomes GROUP BY outcome").fetchall():
                if o == "success":
                    usage_successes = c
                elif o == "failure":
                    usage_failures = c
            usage_by_capability = _usage_summary_by_capability(conn)
    except Exception:
        pass

    return {
        "ok": True,
        "server": SERVER_NAME,
        "version": "0.2.0",
        "registry_path": str(REGISTRY_PATH),
        "usage_db_path": str(USAGE_DB_PATH),
        "schema_version": reg.get("schema_version"),
        "registry_updated_at": reg.get("updated_at"),
        "category_count": len(cats),
        "capability_count": len(caps),
        "capabilities_with_preferred_for": preferred_count,
        "capabilities_with_deprioritize_for": deprio_count,
        "usage_records_total": usage_total,
        "usage_records_success": usage_successes,
        "usage_records_failure": usage_failures,
        "usage_by_capability": usage_by_capability,
        "capabilities_by_backend_mcp": cap_by_server,
        "registry_error": reg.get("_error"),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
