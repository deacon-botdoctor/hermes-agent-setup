#!/usr/bin/env python3
"""Capability-router registry-sync.

Merges a canonical registry (fleet-wide source of truth) with local extras
(runtime-specific entries that don't belong in canonical) into the working
registry that the capability-router MCP reads.

Locations (Hermes default):
  CANONICAL:    ~/.hermes/state/registry-canonical.json    (fleet-wide; pulled from
                 a shared source — overlay, remote sync, or local snapshot)
  LOCAL EXTRAS: ~/.hermes/state/registry-local-extras.json (per-runtime additions;
                 optional — file may not exist on most runtimes)
  WORKING:      ~/.hermes/mcp-servers/capability-router/registry.json (consumed)
  CONFIG:       ~/.hermes/config.yaml (or HERMES_CONFIG)
  SCHEMA CACHE: ~/.hermes/state/mcp/tool-schemas (or HERMES_MCP_SCHEMA_CACHE)

Merge semantics:
  - Categories union by id; local-extras categories override canonical's labels.
  - Capabilities union by id; local-extras override canonical for the same id
    (lets a runtime locally tag a fleet capability as PREFERRED, etc.).
  - Policy-authorized MCP servers in the selected config gain a discovery
    marker. Enabled backends may also publish cached tool entries; cold
    (enabled: false) backends stay bounded to the marker until activation.
  - MCP policy allowlists are authoritative for generated discovery; disabled
    and on_demand_disabled override allowlists within the same config.
  - Autogen:tool-schema local extras are removed when the selected config
    authorizes their server as cold, or when no root, profile, or explicit
    config still authorizes it. If contributing config cannot be read safely,
    generated extras are preserved.
  - Existing tools are deduplicated by normalized server/tool identity, not
    only by capability id.
  - schema_version: take max.
  - updated_at: stamped at sync time.

Idempotent: safe to run repeatedly, including in parallel (atomic write via
tempfile + os.replace).

Exit codes:
  0 = sync succeeded (working registry written, possibly unchanged)
  1 = canonical missing
  2 = canonical malformed
  3 = working registry could not be written
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - runtime dependency is optional
    yaml = None

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
CANONICAL = Path(os.environ.get("REGISTRY_CANONICAL", HERMES_HOME / "state" / "registry-canonical.json"))
LOCAL_EXTRAS = Path(os.environ.get("REGISTRY_LOCAL_EXTRAS", HERMES_HOME / "state" / "registry-local-extras.json"))
WORKING = Path(os.environ.get("REGISTRY_WORKING", HERMES_HOME / "mcp-servers" / "capability-router" / "registry.json"))
CONFIG = Path(os.environ.get("HERMES_CONFIG", HERMES_HOME / "config.yaml"))
MCP_SCHEMA_CACHE = Path(os.environ.get("HERMES_MCP_SCHEMA_CACHE", HERMES_HOME / "state" / "mcp" / "tool-schemas"))


def _log(level: str, msg: str) -> None:
    print(f"[registry-sync] {level}: {msg}", file=sys.stderr)


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log("ERROR", f"malformed JSON at {path}: {e}")
        return None


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".registry.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
        raise


# [HERMES_AUTO_MCP_DISCOVERY_v1]
def _slug(value: str) -> str:
    out = []
    for ch in str(value).lower():
        out.append(ch if ch.isalnum() else "-")
    return "-".join(part for part in "".join(out).split("-") if part)


def _fallback_mcp_config(text: str) -> dict | None:
    servers: dict[str, dict] = {}
    policy: dict[str, list[str]] = {}
    policy_keys = {"on_demand", "active_enabled", "hot_path", "hot_path_enabled", "disabled", "on_demand_disabled"}
    section: str | None = None
    section_indent = -1
    child_indent: int | None = None
    active_policy_key: str | None = None
    active_server: str | None = None
    server_field_indent: int | None = None
    name_pattern = r"(?:[A-Za-z0-9_.-]+|'[A-Za-z0-9_.-]+'|\"[A-Za-z0-9_.-]+\")"

    def name(value: str) -> str | None:
        value = value.strip()
        if not re.fullmatch(name_pattern, value):
            return None
        if value[:1] in {"'", '"'}:
            return value[1:-1]
        return value

    def inline_names(value: str) -> list[str] | None:
        value = re.sub(r"\s+#.*$", "", value).strip()
        if not (value.startswith("[") and value.endswith("]")):
            return None
        body = value[1:-1].strip()
        if not body:
            return []
        values = [name(item) for item in body.split(",")]
        return None if any(item is None for item in values) else values

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        top = re.fullmatch(r"(mcp_servers|mcp_policy):\s*(.*?)", stripped) if indent == 0 else None
        if top:
            value = re.sub(r"\s+#.*$", "", top.group(2)).strip()
            if value not in {"", "{}"}:
                return None
            section = top.group(1) if not value else None
            section_indent = indent
            child_indent = None
            active_policy_key = None
            active_server = None
            server_field_indent = None
            continue
        if section is None or indent <= section_indent:
            section = None
            active_policy_key = None
            active_server = None
            continue

        if section == "mcp_servers":
            match = re.fullmatch(rf"({name_pattern}):\s*(?:{{}})?\s*(?:#.*)?", stripped)
            if match and (child_indent is None or indent == child_indent):
                child_indent = indent
                server_name = name(match.group(1))
                if server_name is None:
                    return None
                servers[server_name] = {}
                active_server = server_name
                server_field_indent = None
                continue
            field_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)", stripped)
            if active_server is not None and child_indent is not None and indent > child_indent and field_match:
                if server_field_indent is None:
                    server_field_indent = indent
                key, value = field_match.groups()
                if indent == server_field_indent and key == "enabled":
                    value = re.sub(r"\s+#.*$", "", value).strip().lower()
                    if value not in {"true", "false"}:
                        return None
                    servers[active_server]["enabled"] = value == "true"
            continue

        key_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)", stripped)
        if key_match and (child_indent is None or indent == child_indent):
            child_indent = indent
            key, value = key_match.groups()
            active_policy_key = None
            if key not in policy_keys:
                continue
            value = re.sub(r"\s+#.*$", "", value).strip()
            if not value:
                policy[key] = []
                active_policy_key = key
                continue
            values = inline_names(value)
            if values is None:
                return None
            policy[key] = values
            continue
        if active_policy_key and child_indent is not None and indent >= child_indent:
            item_match = re.fullmatch(rf"-\s*({name_pattern})\s*(?:#.*)?", stripped)
            if not item_match:
                return None
            item = name(item_match.group(1))
            if item is None:
                return None
            policy[active_policy_key].append(item)

    return {"mcp_servers": servers, "mcp_policy": policy}


def _load_config(path: Path | None = None) -> dict | None:
    path = CONFIG if path is None else path
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if yaml is not None else _fallback_mcp_config(text)
        if data is None:
            raise ValueError("unsupported MCP config structure")
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        _log("WARN", f"could not load MCP config at {path}: {exc}")
        return None


def _string_set(value) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _policy_server_sets(policy) -> tuple[set[str], set[str]]:
    if not isinstance(policy, dict):
        return set(), set()
    allowed = set().union(
        *(_string_set(policy.get(key)) for key in ("on_demand", "active_enabled", "hot_path", "hot_path_enabled"))
    )
    denied = set().union(*(_string_set(policy.get(key)) for key in ("disabled", "on_demand_disabled")))
    return allowed - denied, denied


def _policy_allowed_mcp_servers(config: dict) -> dict[str, dict]:
    servers = config.get("mcp_servers") or {}
    policy = config.get("mcp_policy") or {}
    if not isinstance(servers, dict) or not isinstance(policy, dict):
        return {}
    allowed, _ = _policy_server_sets(policy)
    return {str(name): block for name, block in servers.items() if isinstance(block, dict) and str(name) in allowed}


def _contributing_config_paths() -> list[Path]:
    candidates = [HERMES_HOME / "config.yaml"]
    profiles = HERMES_HOME / "profiles"
    if profiles.exists():
        candidates.extend(sorted(profiles.glob("*/config.yaml")))
    candidates.append(CONFIG)

    paths: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if not path.exists():
            continue
        resolved = path.resolve()
        if resolved not in seen:
            paths.append(path)
            seen.add(resolved)
    return paths


def _mcp_policy_state_across_configs() -> tuple[set[str], set[str]] | None:
    paths = _contributing_config_paths()
    if not paths:
        return None

    allowed_servers: set[str] = set()
    selected_cold_servers: set[str] = set()
    selected_config = CONFIG.resolve()
    for path in paths:
        config = _load_config(path)
        if config is None:
            return None
        servers = config.get("mcp_servers") or {}
        policy = config.get("mcp_policy") or {}
        if not isinstance(servers, dict) or not isinstance(policy, dict):
            return None
        authorized = _policy_allowed_mcp_servers(config)
        allowed_servers.update(authorized)
        if path.resolve() == selected_config:
            selected_cold_servers.update(name for name, block in authorized.items() if block.get("enabled") is False)
    return allowed_servers, selected_cold_servers


def _configured_mcp_servers() -> dict[str, dict]:
    cfg = _load_config()
    if cfg is None:
        return {}
    return _policy_allowed_mcp_servers(cfg)


def _strip_mcp_prefix(server: str, tool_name: str) -> str:
    raw = str(tool_name)
    prefix = "mcp_" + server.replace("-", "_") + "_"
    if raw.lower().startswith(prefix.lower()):
        return raw[len(prefix) :]
    return raw


def _mcp_tool_key(server, tool_name) -> tuple[str, str] | None:
    if not str(server or "").strip() or not str(tool_name or "").strip():
        return None
    return _slug(str(server)), _slug(_strip_mcp_prefix(str(server), str(tool_name)))


def _cache_tools(server: str, scfg: dict) -> list[dict]:
    path = MCP_SCHEMA_CACHE / f"{server}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log("WARN", f"could not read MCP schema cache {path}: {exc}")
        return []
    tools = data.get("tools") if isinstance(data, dict) else None
    return tools if isinstance(tools, list) else []


def _auto_mcp_capabilities(
    existing_ids: set[str], existing_servers: set[str], existing_tool_keys: set[tuple[str, str]]
) -> tuple[list[dict], list[dict]]:
    """Generate local discoverability caps from authorized MCP config and cache.

    Canonical registry entries still win. This only covers local or newly added
    MCP servers so agents can find hot or cold capabilities immediately after
    installation.
    """
    cats = [
        {
            "id": "runtime-mcp",
            "label": "Runtime MCP",
            "description": "MCP servers discovered from this runtime's config and schema cache.",
        }
    ]
    caps: list[dict] = []
    for server, scfg in sorted(_configured_mcp_servers().items()):
        meta = scfg.get("metadata") or {}
        desc = str(meta.get("description") or f"{server} MCP server configured on this runtime.")
        # Cold backends need one bounded activation marker, not hundreds of
        # cached tool entries in the always-hot router catalog. Their concrete
        # tools become searchable through tool_search after activation.
        cached_tools = [] if scfg.get("enabled") is False else _cache_tools(server, scfg)
        for tool in cached_tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            tool_name = _strip_mcp_prefix(server, str(tool.get("name")))
            cap_id = f"auto.{_slug(server)}.{_slug(tool_name)}"
            tool_key = _mcp_tool_key(server, tool_name)
            if cap_id in existing_ids or tool_key in existing_tool_keys:
                continue
            caps.append(
                {
                    "id": cap_id,
                    "category": "runtime-mcp",
                    "label": f"{server}: {tool_name}",
                    "summary": (tool.get("description") or desc or f"Tool {tool_name} from MCP server {server}.")[
                        :1200
                    ],
                    "mcp_server": server,
                    "tool_name": tool_name,
                    "inputs": tool.get("parameters") or {"type": "object", "properties": {}},
                    "outputs": "MCP tool result.",
                    "cost_per_call": "runtime dependent",
                    "rate_limit": "runtime dependent",
                    "requires_creds": [],
                    "routing_weight": 8,
                    "source": "auto:mcp-schema-cache",
                }
            )
            existing_ids.add(cap_id)
            if tool_key is not None:
                existing_tool_keys.add(tool_key)
        if scfg.get("enabled") is False or server not in existing_servers:
            cap_id = f"mcp-server.{_slug(server)}"
            if cap_id not in existing_ids:
                caps.append(
                    {
                        "id": cap_id,
                        "category": "runtime-mcp",
                        "label": f"MCP server: {server}",
                        "summary": (
                            f"DISCOVERY for {server}. {desc} Configured as "
                            f"tier={scfg.get('tier') or 'unspecified'}; use this when looking "
                            f"for whether the runtime has {server}, then load or invoke its "
                            "concrete tools when schema cache is present."
                        ),
                        "mcp_server": server,
                        "tool_name": None,
                        "inputs": {"type": "object", "properties": {}},
                        "outputs": "Discoverability marker only; not directly invokable.",
                        "cost_per_call": "none",
                        "rate_limit": "none",
                        "requires_creds": [],
                        "routing_weight": 5,
                        "preferred_for": [server, desc],
                        "source": "auto:mcp-config",
                    }
                )
                existing_ids.add(cap_id)
    return cats, caps


def _merge(canonical: dict, extras: dict | None) -> dict:
    extras = extras or {"capabilities": [], "categories": []}
    policy_state = _mcp_policy_state_across_configs()
    if policy_state is not None:
        allowed_servers, selected_cold_servers = policy_state
        extras = dict(extras)
        extras["capabilities"] = [
            cap
            for cap in extras.get("capabilities") or []
            if not (
                isinstance(cap, dict)
                and str(cap.get("source") or "").startswith("autogen:tool-schema/")
                and (
                    str(cap.get("mcp_server") or "") not in allowed_servers
                    or str(cap.get("mcp_server") or "") in selected_cold_servers
                )
            )
        ]
    existing_ids = {c.get("id") for c in (canonical.get("capabilities") or []) if isinstance(c, dict)} | {
        c.get("id") for c in (extras.get("capabilities") or []) if isinstance(c, dict)
    }
    existing_servers = {c.get("mcp_server") for c in (canonical.get("capabilities") or []) if isinstance(c, dict)} | {
        c.get("mcp_server") for c in (extras.get("capabilities") or []) if isinstance(c, dict)
    }
    existing_tool_keys = {
        key
        for cap in list(canonical.get("capabilities") or []) + list(extras.get("capabilities") or [])
        if isinstance(cap, dict)
        for key in [_mcp_tool_key(cap.get("mcp_server"), cap.get("tool_name"))]
        if key is not None
    }
    auto_categories, auto_caps = _auto_mcp_capabilities(
        set(existing_ids), set(existing_servers), set(existing_tool_keys)
    )
    if auto_caps:
        _log("INFO", f"auto-discovered {len(auto_caps)} MCP capability marker(s)/tool(s) from config/cache")
    extras = dict(extras)
    extras["categories"] = list(extras.get("categories") or []) + auto_categories
    extras["capabilities"] = list(extras.get("capabilities") or []) + auto_caps

    # Categories: union by id, extras win on collision
    cat_by_id = {c["id"]: c for c in canonical.get("categories") or []}
    for c in extras.get("categories") or []:
        cat_by_id[c["id"]] = c

    # Capabilities: union by id, extras win on collision
    cap_by_id = {c["id"]: c for c in canonical.get("capabilities") or []}
    extras_caps = extras.get("capabilities") or []
    overrides = sum(1 for c in extras_caps if c["id"] in cap_by_id)
    for c in extras_caps:
        cap_by_id[c["id"]] = c

    schema_version = max(
        canonical.get("schema_version", 1),
        extras.get("schema_version", 1),
    )

    return {
        "schema_version": schema_version,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "synced_from_canonical": canonical.get("updated_at"),
        "categories": list(cat_by_id.values()),
        "capabilities": list(cap_by_id.values()),
        "_sync_meta": {
            "canonical_caps": len(canonical.get("capabilities") or []),
            "extras_caps": len(extras_caps),
            "extras_overrides": overrides,
            "canonical_path": str(CANONICAL),
            "extras_path": str(LOCAL_EXTRAS) if LOCAL_EXTRAS.exists() else None,
        },
    }


def main() -> int:
    canonical = _load(CANONICAL)
    if canonical is None:
        _log("ERROR", f"canonical registry missing at {CANONICAL} — install/pull it first")
        return 1
    if "capabilities" not in canonical:
        _log("ERROR", f"canonical at {CANONICAL} missing 'capabilities' key")
        return 2

    extras = _load(LOCAL_EXTRAS)
    if extras is not None:
        _log("INFO", f"loaded {len(extras.get('capabilities') or [])} local-extras caps from {LOCAL_EXTRAS}")
    else:
        _log("INFO", f"no local-extras at {LOCAL_EXTRAS} (optional)")

    merged = _merge(canonical, extras)

    # Compare to current working to know if anything changed
    current = _load(WORKING)
    current_ids = {c["id"] for c in (current.get("capabilities") if current else []) or []}
    new_ids = {c["id"] for c in merged["capabilities"]}
    added = new_ids - current_ids
    removed = current_ids - new_ids

    try:
        _atomic_write(WORKING, merged)
    except Exception as e:
        _log("ERROR", f"could not write working registry at {WORKING}: {e}")
        return 3

    _log(
        "OK",
        f"merged {len(merged['capabilities'])} caps "
        f"({len(merged['_sync_meta']['extras_overrides'] if False else added)} added, "
        f"{len(removed)} removed) -> {WORKING}",
    )
    if added:
        _log("INFO", f"added: {sorted(added)[:8]}{' ...' if len(added) > 8 else ''}")
    if removed:
        _log("INFO", f"removed: {sorted(removed)[:8]}{' ...' if len(removed) > 8 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
