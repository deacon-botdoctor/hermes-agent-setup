#!/usr/bin/env python3
"""Merge shared-defaults/*.yaml into a client's config.yaml.

By default, each leaf in a defaults file replaces the same-keyed client value unless
the dotted path is exempt or protected. A nonempty native image block is client-owned
as a unit; Golden supplies both provider and model only when that block is absent or
empty. The MCP control default reconciles its governed plugin and platform toolset
lists additively. Missing intermediate dicts are created. Keys absent from the defaults
remain untouched. Idempotent.

Usage:
    merge-shared-defaults.py \\
        --profile-root ~/.hermes \\
        --manifest /path/to/CL-client/runtime-manifest.yaml \\
        --defaults-dir /path/to/overlay/shared-defaults

Or against a raw config file:
    merge-shared-defaults.py \\
        --config-path ~/.hermes/config.yaml \\
        --exemption platforms.telegram.reply_to_mode \\
        --defaults-dir /path/to/overlay/shared-defaults

Refero-only cold registration (no other defaults and no server startup):
    merge-shared-defaults.py --scope refero-styles --profile-root /absolute/home \\
        --hermes-python /absolute/candidate/venv/bin/python --rollback-dir /existing/rollback
    Add --restore with the same profile-root and rollback-dir to restore both snapshots.

Exit codes:
    0  — merge applied (changes written) or no-op (already in sync)
    1  — error (missing files, parse failure, etc.)
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    print("error: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)


def _walk_leaves(prefix: str, node: Any):
    """Yield (dotted_path, value) for every leaf in a nested mapping."""
    if isinstance(node, dict):
        for k, v in node.items():
            sub = f"{prefix}.{k}" if prefix else k
            yield from _walk_leaves(sub, v)
    else:
        yield prefix, node


def _set_dotted(target: dict, dotted: str, value: Any) -> bool:
    """Set dotted path on target dict. Return True if value changed."""
    parts = dotted.split(".")
    cur = target
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            cur[part] = {}
            nxt = cur[part]
        cur = nxt
    last = parts[-1]
    before = cur.get(last, _SENTINEL)
    if before == value:
        return False
    cur[last] = value
    return True


_SENTINEL = object()


# Primary client model-routing keys are owned by the client config, never by shared
# defaults. merge-sd must NEVER overwrite these even if a (possibly stale)
# defaults file declares them. Hard floor against the 2026-06-07 Codex->openrouter
# clobber where a leftover config-routing-client-enoch.yaml reverted 6 agents.
#
# Auxiliary lanes are different: they are fleet-owned routine tool routes. Drift
# here breaks tools silently (for example Codex-mini vision on ChatGPT accounts).
# Apply shared-default auxiliary routing unless a manifest explicitly exempts a
# dotted auxiliary path. Native image generation is a default-if-missing lane:
# a nonempty top-level mapping is client-owned as one provider/model route.
PROTECTED_PREFIXES = (
    "model.default",
    "model.provider",
    "model.base_url",
    "model.api_key_env",
    "fallback_providers",
    "smart_model_routing",
    "custom_providers",
    "compression.summary_model",
    "compression.summary_provider",
    "compression.summary_base_url",
)

IMAGE_GEN_LEAVES = {"image_gen.provider", "image_gen.model"}


def _is_protected(dotted: str) -> bool:
    """True if dotted path is a client-owned routing key shared defaults must not touch."""
    for p in PROTECTED_PREFIXES:
        if dotted == p or dotted.startswith(p + "."):
            return True
    return False


def merge(
    client_config: dict,
    defaults: dict,
    exemptions: Iterable[str],
) -> tuple[dict, list[str], list[str]]:
    """Return (merged_config, applied_paths, skipped_paths)."""
    exempt_set = set(exemptions)
    applied: list[str] = []
    skipped: list[str] = []

    merged = _deep_copy(client_config)
    configured_image_block = client_config.get("image_gen")
    preserve_image_block = isinstance(configured_image_block, dict) and bool(configured_image_block)
    for dotted, value in _walk_leaves("", defaults):
        if dotted in exempt_set or _is_protected(dotted):
            skipped.append(dotted)
            continue
        if dotted in IMAGE_GEN_LEAVES and preserve_image_block:
            skipped.append(dotted)
            continue
        if _set_dotted(merged, dotted, value):
            applied.append(dotted)
    return merged, applied, skipped


def reconcile_mcp_control_defaults(
    client_config: dict,
    defaults: dict,
    exemptions: Iterable[str],
) -> tuple[dict, list[str], list[str]]:
    """Add only the governed MCP control scopes without replacing client lists."""
    exempt_set = set(exemptions)
    merged = _deep_copy(client_config)
    applied: list[str] = []
    skipped: list[str] = []

    plugin_defaults = defaults.get("plugins") or {}
    plugin_additions = plugin_defaults.get("enabled", []) if isinstance(plugin_defaults, dict) else []
    plugin_path = "plugins.enabled"
    if plugin_path in exempt_set:
        skipped.append(plugin_path)
    elif isinstance(plugin_additions, list) and plugin_additions:
        plugins = merged.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = {}
            merged["plugins"] = plugins
        current = plugins.get("enabled")
        current = list(current) if isinstance(current, list) else []
        wanted = current + [item for item in plugin_additions if item not in current]
        if current != wanted or plugins.get("enabled") != wanted:
            plugins["enabled"] = wanted
            applied.append(plugin_path)

    policy = merged.get("mcp_policy") or {}
    allowed_names: list[str] = []
    if isinstance(policy, dict):
        for key in ("on_demand", "active_enabled", "hot_path", "hot_path_enabled"):
            values = policy.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                name = str(value).strip()
                if name and name not in allowed_names:
                    allowed_names.append(name)
    denied_names: set[str] = set()
    if isinstance(policy, dict):
        for key in ("disabled", "on_demand_disabled"):
            values = policy.get(key)
            if isinstance(values, list):
                denied_names.update(str(value).strip() for value in values if str(value).strip())
    allowed_names = [name for name in allowed_names if name not in denied_names]
    backend_toolsets = [f"mcp-{name}" for name in allowed_names]
    servers = merged.get("mcp_servers") or {}
    cold_toolsets = {
        f"mcp-{name}" for name, config in servers.items() if isinstance(config, dict) and config.get("enabled") is False
    }
    denied_toolsets = {f"mcp-{name}" for name in denied_names}
    revoked_toolsets = (cold_toolsets - set(backend_toolsets)) | denied_toolsets
    platform_defaults = defaults.get("platform_toolsets") or {}
    if isinstance(platform_defaults, dict):
        platforms = merged.setdefault("platform_toolsets", {})
        if not isinstance(platforms, dict):
            platforms = {}
            merged["platform_toolsets"] = platforms
        for platform, additions in platform_defaults.items():
            dotted = f"platform_toolsets.{platform}"
            if dotted in exempt_set:
                skipped.append(dotted)
                continue
            if not isinstance(additions, list):
                continue
            current = platforms.get(platform)
            current = [item for item in current if item not in revoked_toolsets] if isinstance(current, list) else []
            governed = additions + backend_toolsets
            wanted = list(current)
            for item in governed:
                if item not in wanted:
                    wanted.append(item)
            if current != wanted or platforms.get(platform) != wanted:
                platforms[platform] = wanted
                applied.append(dotted)
        for platform, current_value in list(platforms.items()):
            if platform in platform_defaults:
                continue
            dotted = f"platform_toolsets.{platform}"
            if not isinstance(current_value, list) or "mcp-capability-router" not in current_value:
                continue
            if dotted in exempt_set:
                skipped.append(dotted)
                continue
            current = [item for item in current_value if item not in revoked_toolsets]
            governed = ["mcp-on-demand-control", *backend_toolsets]
            wanted = list(current)
            for item in governed:
                if item not in wanted:
                    wanted.append(item)
            if current_value != wanted:
                platforms[platform] = wanted
                applied.append(dotted)
    return merged, applied, skipped


def _deep_copy(node):
    if isinstance(node, dict):
        return {k: _deep_copy(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_deep_copy(v) for v in node]
    return node


def retire_matching_defaults(
    client_config: dict,
    retired_defaults: dict,
    exemptions: list[str],
) -> tuple[dict, list[str], list[str]]:
    """Remove retired Golden leaves without clobbering client-owned values."""
    merged = _deep_copy(client_config)
    removed: list[str] = []
    skipped: list[str] = []
    exempt_set = set(exemptions)

    def walk(current: dict, retired: dict, prefix: str = "") -> None:
        for key, retired_value in retired.items():
            if key not in current:
                continue
            dotted = f"{prefix}.{key}" if prefix else key
            current_value = current[key]
            if isinstance(retired_value, dict) and retired_value:
                if not isinstance(current_value, dict):
                    continue
                before = len(removed)
                walk(current_value, retired_value, dotted)
                if len(removed) > before and not current_value:
                    del current[key]
                continue
            if dotted in exempt_set:
                skipped.append(dotted)
            elif current_value == retired_value:
                del current[key]
                removed.append(dotted)

    walk(merged, retired_defaults)
    return merged, removed, skipped


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    return data


def _discover_defaults_files(defaults_dir: Path) -> list[Path]:
    if not defaults_dir.exists():
        return []
    files = sorted(p for p in defaults_dir.glob("config-*.yaml") if p.is_file())
    return files


NATIVE_IMAGE_DEFAULT_NAMES = {
    "config-native-image-generation.yaml",
    "config-mcp-on-demand-control.yaml",
}

NATIVE_IMAGE_REQUIRED_TOOLS = {
    "cli": ("image_gen",),
    "telegram": ("image_gen", "skills", "vision"),
    "cron": ("image_gen",),
}


def _reconcile_native_image_exposure(
    client_config: dict,
    defaults: dict,
    exemptions: Iterable[str],
) -> tuple[dict, list[str], list[str]]:
    """Add the executable image surface without reconciling unrelated MCP policy."""
    platform_toolsets = defaults.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        raise ValueError("native-image MCP defaults omit platform_toolsets")

    exempt_set = set(exemptions)
    merged = _deep_copy(client_config)
    platforms = merged.get("platform_toolsets")
    if not isinstance(platforms, dict):
        platforms = {}
        merged["platform_toolsets"] = platforms
    applied: list[str] = []
    skipped: list[str] = []
    for platform, required_tools in NATIVE_IMAGE_REQUIRED_TOOLS.items():
        values = platform_toolsets.get(platform)
        if not isinstance(values, list) or any(
            tool not in values for tool in required_tools
        ):
            missing = [
                tool
                for tool in required_tools
                if not isinstance(values, list) or tool not in values
            ]
            raise ValueError(
                f"native-image MCP defaults omit {', '.join(missing)} for {platform}"
            )
        dotted = f"platform_toolsets.{platform}"
        if dotted in exempt_set:
            skipped.append(dotted)
            continue
        current_value = platforms.get(platform)
        current = list(current_value) if isinstance(current_value, list) else []
        wanted = [*current, *(tool for tool in required_tools if tool not in current)]
        if current_value != wanted:
            platforms[platform] = wanted
            applied.append(dotted)
    return merged, applied, skipped


def _native_image_receipt(
    merged: dict,
    exemptions: list[str],
    applied: list[tuple[str, str]],
    skipped: list[tuple[str, str]],
) -> dict:
    image_gen = merged.get("image_gen")
    toolsets = merged.get("platform_toolsets")
    route_ready = (
        isinstance(image_gen, dict)
        and isinstance(image_gen.get("provider"), str)
        and bool(image_gen["provider"].strip())
        and isinstance(image_gen.get("model"), str)
        and bool(image_gen["model"].strip())
    )
    exposure = {
        platform: all(
            isinstance(toolsets, dict)
            and isinstance(toolsets.get(platform), list)
            and tool in toolsets[platform]
            for tool in required_tools
        )
        for platform, required_tools in NATIVE_IMAGE_REQUIRED_TOOLS.items()
    }
    if not route_ready or not all(exposure.values()):
        raise ValueError("effective native-image capability is incomplete")
    managed = {
        "image_gen.provider",
        "image_gen.model",
        "platform_toolsets.cli",
        "platform_toolsets.telegram",
        "platform_toolsets.cron",
    }
    return {
        "status": "pass",
        "scope": "native-image",
        "changed_paths": sorted({path for _, path in applied} & managed),
        "skipped_paths": sorted({path for _, path in skipped} & managed),
        "exemptions": sorted(set(exemptions) & managed),
        "effective_image_gen": image_gen,
        "effective_platform_exposure": exposure,
    }


REFERO_ID = "visual.refero-styles"
REFERO_REGISTRY = "mcp-servers/capability-router/registry.json"
REFERO_CANONICAL = "state/registry-canonical.json"
REFERO_EXTRAS = "state/registry-local-extras.json"
REFERO_SYNC = "bin/registry-sync.py"
REFERO_ROLLBACK = "refero-styles-registration"


def _refero_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Refero registration refuses duplicate mapping keys")
        result[key] = value
    return result


def _refero_yaml(payload: bytes):
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        loader.flatten_mapping(node)
        return _refero_pairs((loader.construct_object(k), loader.construct_object(v)) for k, v in node.value)

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(payload, Loader=UniqueLoader)


def _refero_json(payload):
    return json.loads(payload, object_pairs_hook=_refero_pairs)


def _refero_names(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("Refero registration requires string-list configuration")
    return value


def _refero_registry(value: Any) -> list[dict]:
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] not in {1, 2}
    ):
        raise ValueError("Refero registration requires registry schema 1 or 2")
    for key in ("categories", "capabilities"):
        rows = value.get(key)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
            for row in rows
        ):
            raise ValueError("Refero registration requires registry rows with string identities")
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError("Refero registration refuses duplicate registry identities")
    return value["capabilities"]


def _refero_managed_declaration(value: Any, hermes_home: str, package_home: str | None = None) -> bool:
    """Recognize the exact cold declaration written by an earlier Golden runtime."""
    if not isinstance(value, dict) or set(value) != {"command", "args", "env", "enabled"}:
        return False
    command = value.get("command")
    environment = value.get("env")
    home = Path(hermes_home)
    if (
        value.get("enabled") is not False
        or not isinstance(command, str)
        or not isinstance(environment, dict)
        or set(environment) != {"HERMES_HOME", "HERMES_PYTHON"}
        or environment.get("HERMES_HOME") != hermes_home
        or environment.get("HERMES_PYTHON") != command
        or value.get("args") not in ([str(root / "mcp-servers/refero-styles/bin/launch")] for root in (home, Path(package_home or home)))
    ):
        return False
    command_path = Path(command)
    roots = [home / "state/runtime-candidates"]
    if command_path.parts[-3:] == ("venv", "Scripts", "python.exe"):
        roots.append(home / "runtime-candidates")
    relative = next((command_path.relative_to(root) for root in roots if command_path.is_relative_to(root)), None)
    if relative is None:
        return False
    return (
        command_path.is_absolute()
        and ".." not in command_path.parts
        and len(relative.parts) >= 4
        and relative.parts[-3:] in (("venv", "bin", "python"), ("venv", "Scripts", "python.exe"))
    )


def _refero_legacy_hot_declaration(value: Any, hermes_home: str, package_home: str | None = None) -> bool:
    """Recognize only the retired direct-launch Refero declaration."""
    home = Path(hermes_home)
    return (
        isinstance(value, dict)
        and set(value) == {"command", "env", "connect_timeout", "enabled"}
        and value.get("command")
        in {str(root / "mcp-servers/refero-styles/bin/launch") for root in (home, Path(package_home or home))}
        and value.get("env") == {"HERMES_HOME": hermes_home}
        and type(value.get("connect_timeout")) is float
        and value.get("connect_timeout") == 20.0
        and value.get("enabled") is True
    )


def reconcile_refero_styles(
    client_config: dict,
    registry: dict,
    source_row: dict,
    *,
    hermes_home: str,
    hermes_python: str,
    package_home: str | None = None,
    registry_home: str | None = None,
    exemptions: Iterable[str] = (),
) -> tuple[dict, dict, dict]:
    """Pure, Refero-only cold registration; never enable a plugin or server."""
    if not isinstance(client_config, dict):
        raise ValueError("Refero registration requires a config mapping")
    package_home = package_home or hermes_home
    registry_home = registry_home or package_home
    for value in (hermes_home, hermes_python, package_home, registry_home):
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("Refero registration requires explicit absolute runtime paths")
    if package_home != hermes_home and Path(hermes_home).parent != Path(package_home) / "profiles":
        raise ValueError("Refero package root must own the exact named profile")
    _refero_validate_registry_root(Path(hermes_home), Path(package_home), Path(registry_home))
    exempt_set = set(_refero_names(list(exemptions)))
    rows = _refero_registry(registry)
    source_keys = {"id", "category", "label", "summary", "mcp_server", "tool_name", "preferred_for"}
    if not isinstance(source_row, dict) or set(source_row) != source_keys or any(
        not isinstance(source_row[key], str) or not source_row[key]
        for key in source_keys - {"preferred_for"}
    ):
        raise ValueError("Refero registration requires the exact source-owned capability shape")
    _refero_names(source_row["preferred_for"])
    if (source_row["id"], source_row["mcp_server"], source_row["tool_name"], source_row["category"]) != (
        REFERO_ID, "refero-styles", "refero_search", "visual"
    ):
        raise ValueError("Refero registration refuses a foreign source capability")
    registry_schema = registry["schema_version"]
    category_ids = {row["id"] for row in registry["categories"]}
    target_category = "visual" if registry_schema == 1 else "docs-content"
    expected_row = {**_deep_copy(source_row), "category": target_category}
    existing_row = next((row for row in rows if row["id"] == REFERO_ID), None)
    legacy_row = _deep_copy(source_row) if registry_schema == 2 else None
    if existing_row is not None and existing_row not in (expected_row, legacy_row):
        raise ValueError("Refero registration refuses a conflicting capability")

    merged = _deep_copy(client_config)
    merged_registry = _deep_copy(registry)
    receipt = {"scope": "refero-styles", "status": "unchanged", "changed_paths": []}

    def preserve(status: str):
        return _deep_copy(client_config), _deep_copy(registry), {**receipt, "status": status}

    servers = merged.get("mcp_servers", {})
    policy = merged.get("mcp_policy", {})
    if not isinstance(servers, dict) or not isinstance(policy, dict):
        raise ValueError("Refero registration requires MCP config mappings")
    allow_keys = ("on_demand", "active_enabled", "hot_path", "hot_path_enabled")
    deny_keys = ("disabled", "on_demand_disabled")
    names = {key: _refero_names(policy.get(key, [])) for key in (*allow_keys, *deny_keys)}
    # Match the existing control plugin's whitespace handling without rewriting
    # any client-owned list entries.
    denied = {name.strip() for key in deny_keys for name in names[key]}
    allowed = {name.strip() for key in allow_keys for name in names[key]}
    existing = servers.get("refero-styles", _SENTINEL)
    if "refero-styles" in denied or "*" in denied:
        return preserve("preserved_opt_out")
    if existing is not _SENTINEL and not isinstance(existing, dict):
        raise ValueError("Refero registration refuses a non-mapping server declaration")
    if isinstance(existing, dict) and existing.get("enabled") is False and "refero-styles" not in allowed:
        return preserve("preserved_opt_out")
    declaration = {
        "command": hermes_python,
        "args": [str(Path(package_home) / "mcp-servers/refero-styles/bin/launch")],
        "env": {"HERMES_HOME": hermes_home, "HERMES_PYTHON": hermes_python},
        "enabled": False,
    }
    if (
        existing is not _SENTINEL
        and existing != declaration
        and not _refero_managed_declaration(existing, hermes_home, package_home)
        and not _refero_legacy_hot_declaration(existing, hermes_home, package_home)
    ):
        raise ValueError("Refero registration refuses a custom server declaration")

    router = servers.get("capability-router")
    if not isinstance(router, dict) or not isinstance(router.get("env", {}), dict):
        raise ValueError("Refero registration requires the existing capability-router declaration")
    router_env = router.get("env", {})
    effective_router_home = router_env.get("HERMES_HOME", hermes_home)
    if effective_router_home not in {hermes_home, registry_home} or router_env.get(
        "CAPABILITY_REGISTRY", str(Path(effective_router_home) / REFERO_REGISTRY)
    ) != str(Path(registry_home) / REFERO_REGISTRY):
        raise ValueError("Refero registration refuses an alternate consumed registry")
    if registry_home != hermes_home and router.get("args") != [str(Path(registry_home) / "mcp-servers/capability-router/src/capability_router/server.py")]:
        raise ValueError("Refero split registry requires the exact consumed router script")

    plugins = merged.get("plugins")
    platforms = merged.get("platform_toolsets")
    if not isinstance(plugins, dict) or "mcp-on-demand-control" not in _refero_names(plugins.get("enabled", [])):
        raise ValueError("Refero registration requires the existing on-demand-control plugin")
    if not isinstance(platforms, dict) or any(not isinstance(key, str) or not key for key in platforms):
        raise ValueError("Refero registration requires platform toolset mappings")
    exposed = []
    for platform, tools in platforms.items():
        _refero_names(tools)
        if "mcp-capability-router" not in tools:
            continue
        if "mcp-on-demand-control" not in tools:
            raise ValueError("Refero registration requires existing on-demand control on router platforms")
        exposed.append(platform)
    if not exposed:
        raise ValueError("Refero registration requires an already-exposed capability router")
    if target_category not in category_ids:
        raise ValueError(
            f"Refero registration requires the existing {target_category} registry category"
        )

    changed = []
    if effective_router_home != hermes_home:
        merged["mcp_servers"]["capability-router"].setdefault("env", {})["HERMES_HOME"] = hermes_home
        changed.append("mcp_servers.capability-router.env.HERMES_HOME")
    if registry_home != hermes_home and "CAPABILITY_REGISTRY" not in router_env:
        merged["mcp_servers"]["capability-router"].setdefault("env", {})["CAPABILITY_REGISTRY"] = str(Path(registry_home) / REFERO_REGISTRY)
        changed.append("mcp_servers.capability-router.env.CAPABILITY_REGISTRY")
    if existing is _SENTINEL or existing != declaration:
        merged.setdefault("mcp_servers", {})["refero-styles"] = declaration
        changed.append("mcp_servers.refero-styles")
    if "refero-styles" not in {name.strip() for name in names["on_demand"]}:
        merged.setdefault("mcp_policy", {})["on_demand"] = [*names["on_demand"], "refero-styles"]
        changed.append("mcp_policy.on_demand")
    for platform in exposed:
        if "mcp-refero-styles" not in platforms[platform]:
            platforms[platform].append("mcp-refero-styles")
            changed.append(f"platform_toolsets.{platform}")
    if any(
        path == exempt or path.startswith(exempt + ".") or exempt.startswith(path + ".")
        for path in changed for exempt in exempt_set
    ):
        return preserve("preserved_exemption")
    if existing_row is None:
        merged_registry["capabilities"].append(expected_row)
        changed.append(f"{REFERO_REGISTRY}#{REFERO_ID}")
    elif existing_row == legacy_row and existing_row != expected_row:
        merged_registry["capabilities"] = [
            expected_row if row["id"] == REFERO_ID else row
            for row in merged_registry["capabilities"]
        ]
        changed.append(f"{REFERO_REGISTRY}#{REFERO_ID}")
    receipt.update(status="changed" if changed else "unchanged", changed_paths=sorted(changed))
    return merged, merged_registry, receipt


def _refero_atomic_write(path: Path, payload: bytes, mode: int, uid=None, gid=None) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".refero-registration-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if uid is not None and hasattr(os, "fchown"):
                current = os.fstat(handle.fileno())
                if (current.st_uid, current.st_gid) != (uid, gid):
                    os.fchown(handle.fileno(), uid, gid)
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            else:
                os.chmod(temporary, mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _refero_validate_registry_root(home: Path, package: Path, registry: Path) -> None:
    if registry == package:
        return
    if home != package or home != registry.parent / "AppData/Local/hermes/spark-runtime" or registry.name != ".hermes":
        raise ValueError("Refero registry root is not the conventional same-user Windows layout")


def restore_refero_styles(hermes_home: Path, rollback_dir: Path, package_home: Path | None = None, registry_home: Path | None = None) -> dict:
    """Restore exact snapshots, including the schema-2 registry source."""
    home = hermes_home.resolve()
    package = (package_home or home).resolve()
    registry = (registry_home or package).resolve()
    _refero_validate_registry_root(home, package, registry)
    if package != home and home.parent != package / "profiles":
        raise ValueError("Refero package root must own the exact named profile")
    snapshot = rollback_dir / REFERO_ROLLBACK
    if snapshot.is_symlink() or (snapshot / "receipt.json").is_symlink():
        raise ValueError("Refero rollback refuses symlink snapshots")
    metadata = _refero_json((snapshot / "receipt.json").read_bytes())
    if not isinstance(metadata, dict):
        raise ValueError("Refero rollback requires snapshot metadata")
    file_rows = metadata.get("files", {})
    if metadata.get("hermes_home") != str(home) or metadata.get("package_home", str(home)) != str(package) or metadata.get("registry_home", str(package)) != str(registry) or set(file_rows) not in (
        {"config.yaml", REFERO_REGISTRY},
        {"config.yaml", REFERO_REGISTRY, REFERO_EXTRAS},
    ):
        raise ValueError("Refero rollback metadata does not match the exact targets")
    filenames = {
        "config.yaml": "config.before",
        REFERO_REGISTRY: "registry.before",
        REFERO_EXTRAS: "extras.before",
    }
    saved = []
    for relative, row in file_rows.items():
        root = home if relative == "config.yaml" else registry
        path = root / relative
        backup = snapshot / filenames[relative]
        path.parent.resolve(strict=True).relative_to(root)
        if path.is_symlink() or backup.is_symlink() or not backup.is_file():
            raise ValueError("Refero rollback requires regular targets and snapshots")
        existed = row.get("existed")
        if not isinstance(row, dict) or not isinstance(existed, bool) or any(
            not isinstance(row.get(key), int) or isinstance(row[key], bool) or row[key] < 0
            for key in ("mode", "uid", "gid")
        ) or row["mode"] > 0o7777:
            raise ValueError("Refero rollback metadata is invalid")
        if not existed and relative != REFERO_EXTRAS:
            raise ValueError("Refero rollback refuses an absent required preimage")
        payload = backup.read_bytes()
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if hashlib.sha256(payload).hexdigest() != row["before_sha256"] or current_hash not in (
            row["before_sha256"], row["after_sha256"]
        ):
            raise ValueError("Refero rollback refuses changed snapshot or intervening target edits")
        saved.append((path, payload, row["mode"], row["uid"], row["gid"], existed))
    errors = []
    for path, payload, mode, uid, gid, existed in saved:
        try:
            if existed:
                _refero_atomic_write(path, payload, mode, uid, gid)
            else:
                path.unlink()
        except OSError as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError("Refero rollback could not restore both targets")
    return {"scope": "refero-styles", "status": "restored", "changed_paths": sorted(file_rows),
            "sha256": {relative: row["before_sha256"] for relative, row in file_rows.items()}}


def _run_refero_scope(args) -> int:
    """Narrow cold-registration transaction; output never includes config values."""
    try:
        if args.profile_root is None or args.config_path is not None or args.rollback_dir is None:
            raise ValueError("Refero scope requires profile-root and rollback-dir only")
        home = args.profile_root.expanduser().resolve(strict=True)
        package = (args.package_root or args.profile_root).expanduser().resolve(strict=True)
        registry_home = (args.registry_root or args.package_root or args.profile_root).expanduser().resolve(strict=True)
        _refero_validate_registry_root(home, package, registry_home)
        if package != home and home.parent != package / "profiles":
            raise ValueError("Refero package root must own the exact named profile")
        rollback = args.rollback_dir.expanduser().resolve(strict=True)
        if not home.is_dir() or not rollback.is_dir():
            raise ValueError("Refero scope requires existing home and rollback directories")
        if args.restore:
            if args.dry_run:
                raise ValueError("Refero restore does not accept dry-run")
            print(json.dumps(restore_refero_styles(home, rollback, package, registry_home), sort_keys=True))
            return 0
        python = args.hermes_python
        python_path = Path(python) if python else None
        if (
            python_path is None
            or not python_path.is_absolute()
            or not python_path.is_file()
            or not os.access(python_path, os.X_OK)
        ):
            raise ValueError("Refero scope requires the explicit candidate Python")
        paths = {"config.yaml": home / "config.yaml", REFERO_REGISTRY: registry_home / REFERO_REGISTRY}
        if any(path.is_symlink() or not path.is_file() for path in paths.values()):
            raise ValueError("Refero scope requires existing regular config and consumed registry")
        for relative, path in paths.items():
            path.parent.resolve(strict=True).relative_to(home if relative == "config.yaml" else registry_home)
        original = {relative: path.read_bytes() for relative, path in paths.items()}
        config = _refero_yaml(original["config.yaml"])
        registry = _refero_json(original[REFERO_REGISTRY])
        source = _refero_json(args.refero_source_registry.read_bytes())
        source_rows = [row for row in _refero_registry(source) if row["id"] == REFERO_ID]
        if len(source_rows) != 1:
            raise ValueError("Refero source registry must contain exactly one owned row")
        exemptions = list(args.exemption)
        if args.manifest:
            manifest = _refero_yaml(args.manifest.read_bytes())
            if not isinstance(manifest, dict):
                raise ValueError("Refero manifest must be a mapping")
            exemptions += _refero_names(manifest.get("overlay_config_exemptions", []))
        merged, merged_registry, receipt = reconcile_refero_styles(
            config, registry, source_rows[0], hermes_home=str(home),
            hermes_python=python, package_home=str(package), registry_home=str(registry_home), exemptions=exemptions,
        )
        schema_two = (
            registry.get("schema_version") == 2
            and receipt["status"] not in {"preserved_opt_out", "preserved_exemption"}
        )
        expected_row = next(
            (row for row in merged_registry["capabilities"] if row["id"] == REFERO_ID),
            None,
        )
        merged_extras = None
        if schema_two:
            sync_meta = registry.get("_sync_meta")
            canonical = registry_home / REFERO_CANONICAL
            extras = registry_home / REFERO_EXTRAS
            # The transaction runs before the runtime switch, so the installed
            # helper may belong to the predecessor.  Verify the registry with
            # the exact candidate helper shipped beside this merger.
            candidate_root = Path(__file__).resolve().parent.parent
            sync = candidate_root / REFERO_SYNC
            extras_existed = extras.is_file() and not extras.is_symlink()
            if (
                not isinstance(sync_meta, dict)
                or Path(str(sync_meta.get("canonical_path") or "")) != canonical
                or sync_meta.get("extras_path") not in (None, str(extras))
                or any(path.is_symlink() or not path.is_file() for path in (canonical, sync))
                or (extras.exists() and not extras_existed)
            ):
                raise ValueError("Refero schema-2 registry source route is invalid")
            canonical.parent.resolve(strict=True).relative_to(registry_home)
            sync.parent.resolve(strict=True).relative_to(candidate_root)
            extras.parent.resolve(strict=True).relative_to(registry_home)
            paths[REFERO_EXTRAS] = extras
            original[REFERO_EXTRAS] = extras.read_bytes() if extras_existed else b""
            extras_doc = (
                _refero_json(original[REFERO_EXTRAS])
                if extras_existed else
                {"schema_version": 1, "categories": [], "capabilities": []}
            )
            if extras_doc.get("schema_version") not in (1, 2) or expected_row is None:
                raise ValueError("Refero schema-2 local extras source is invalid")
            extra_rows = _refero_registry(extras_doc)
            existing_extra = [row for row in extra_rows if row["id"] == REFERO_ID]
            if existing_extra and existing_extra not in ([expected_row], [source_rows[0]]):
                raise ValueError("Refero schema-2 local extras ownership conflicts")
            merged_extras = _deep_copy(extras_doc)
            if not existing_extra:
                merged_extras["capabilities"].append(expected_row)
                receipt["changed_paths"].append(f"{REFERO_EXTRAS}#{REFERO_ID}")
                receipt["status"] = "changed"
            elif existing_extra == [source_rows[0]] and existing_extra != [expected_row]:
                merged_extras["capabilities"] = [
                    expected_row if row["id"] == REFERO_ID else row
                    for row in merged_extras["capabilities"]
                ]
                receipt["changed_paths"].append(f"{REFERO_EXTRAS}#{REFERO_ID}")
                receipt["status"] = "changed"
        updated = {
            "config.yaml": (
                yaml.safe_dump(merged, sort_keys=False, allow_unicode=True).encode()
                if merged != config else original["config.yaml"]
            ),
            REFERO_REGISTRY: (
                (json.dumps(merged_registry, indent=2, ensure_ascii=False) + "\n").encode()
                if merged_registry != registry and not schema_two else original[REFERO_REGISTRY]
            ),
        }
        if schema_two:
            updated[REFERO_EXTRAS] = (
                json.dumps(merged_extras, indent=2, ensure_ascii=False) + "\n"
            ).encode() if merged_extras != extras_doc else original[REFERO_EXTRAS]

        def stage_schema_two(directory: Path) -> bytes:
            staged_config = directory / "config.after.staged"
            staged_extras = directory / "extras.after.staged"
            staged_registry = directory / "registry.after.staged"
            _refero_atomic_write(staged_config, updated["config.yaml"], 0o600)
            _refero_atomic_write(staged_extras, updated[REFERO_EXTRAS], 0o600)
            env = dict(os.environ)
            env.update(
                HERMES_HOME=str(registry_home), HERMES_CONFIG=str(staged_config),
                REGISTRY_CANONICAL=str(registry_home / REFERO_CANONICAL),
                REGISTRY_LOCAL_EXTRAS=str(staged_extras),
                REGISTRY_WORKING=str(staged_registry),
            )
            synced = subprocess.run(
                [python, str(sync)], capture_output=True,
                text=True, timeout=45, env=env, check=False,
            )
            if synced.returncode or not staged_registry.is_file():
                raise ValueError("Refero registry sync failed closed")
            generated_registry = _refero_json(staged_registry.read_bytes())
            generated_meta = generated_registry.get("_sync_meta")
            if not isinstance(generated_meta, dict):
                raise ValueError("Refero registry sync metadata is invalid")
            generated_rows = _refero_registry(generated_registry)
            prior_by_id = {row["id"]: row for row in _refero_registry(registry)}
            generated_by_id = {row["id"]: row for row in generated_rows}
            marker_id = "mcp-server.refero-styles"
            marker = generated_by_id.get(marker_id)
            if (
                generated_by_id.get(REFERO_ID) != expected_row
                or not isinstance(marker, dict)
                or marker.get("mcp_server") != "refero-styles"
                or marker.get("tool_name") is not None
                or marker.get("category") != "runtime-mcp"
                or marker.get("source") != "auto:mcp-config"
                or generated_registry.get("categories") != registry.get("categories")
            ):
                raise ValueError("Refero registry sync could not prove the owned registration")
            # registry-sync also reconciles unrelated runtime MCP markers. Refero owns
            # neither those markers nor the registry's derived metadata, so stage its
            # output as proof only and persist the exact two Refero-owned additions.
            # This keeps an existing schema-2 working registry byte-for-byte intact
            # outside the cold Refero registration.
            post_registry = _deep_copy(registry)
            post_by_id = {row["id"]: row for row in post_registry["capabilities"]}
            if post_by_id.get(REFERO_ID) != expected_row:
                if REFERO_ID in post_by_id:
                    # reconcile_refero_styles already accepted this only as the
                    # exact legacy schema-2 source row; promote that one owned
                    # row without rewriting any other working-registry entry.
                    post_registry["capabilities"] = [
                        expected_row if row["id"] == REFERO_ID else row
                        for row in post_registry["capabilities"]
                    ]
                else:
                    post_registry["capabilities"].append(expected_row)
            if marker_id not in post_by_id:
                post_registry["capabilities"].append(marker)
                receipt["changed_paths"].append(
                    f"{REFERO_REGISTRY}#{marker_id}"
                )
            return (json.dumps(post_registry, indent=2, ensure_ascii=False) + "\n").encode()

        if schema_two and receipt["changed_paths"]:
            with tempfile.TemporaryDirectory(prefix=".refero-stage-", dir=rollback) as temporary:
                updated[REFERO_REGISTRY] = stage_schema_two(Path(temporary))
        files = {
            relative: {
                "before_sha256": hashlib.sha256(original[relative]).hexdigest(),
                "after_sha256": hashlib.sha256(updated[relative]).hexdigest(),
                "existed": path.exists(),
                "mode": stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600,
                "uid": path.stat().st_uid if path.exists() else paths[REFERO_REGISTRY].stat().st_uid,
                "gid": path.stat().st_gid if path.exists() else paths[REFERO_REGISTRY].stat().st_gid,
            }
            for relative, path in paths.items()
        }
        if receipt["changed_paths"] and not args.dry_run:
            snapshot = rollback / REFERO_ROLLBACK
            snapshot.mkdir(mode=0o700)
            filenames = {
                "config.yaml": "config.before",
                REFERO_REGISTRY: "registry.before",
                REFERO_EXTRAS: "extras.before",
            }
            for relative in paths:
                _refero_atomic_write(snapshot / filenames[relative], original[relative], 0o600)
            try:
                for relative in files:
                    files[relative]["after_sha256"] = hashlib.sha256(
                        updated[relative]
                    ).hexdigest()
                _refero_atomic_write(
                    snapshot / "receipt.json",
                    json.dumps({"hermes_home": str(home), "package_home": str(package), "registry_home": str(registry_home), "files": files}, sort_keys=True).encode(),
                    0o600,
                )
                if any(
                    (path.read_bytes() if path.is_file() else b"") != original[relative]
                    for relative, path in paths.items()
                ):
                    raise ValueError("Refero targets changed while staging snapshots")
                for relative, path in paths.items():
                    if updated[relative] != original[relative]:
                        row = files[relative]
                        _refero_atomic_write(path, updated[relative], row["mode"], row["uid"], row["gid"])
            except Exception:
                unrelated = []
                for relative, path in paths.items():
                    current = path.read_bytes() if path.is_file() else b""
                    if hashlib.sha256(current).hexdigest() not in {
                        files[relative]["before_sha256"], files[relative]["after_sha256"]
                    }:
                        unrelated.append(relative)
                if unrelated:
                    raise RuntimeError(
                        "Refero registration detected concurrent target changes; recovery refused"
                    )
                restore_errors = []
                for relative, path in paths.items():
                    row = files[relative]
                    try:
                        if row["existed"]:
                            _refero_atomic_write(
                                path, original[relative], row["mode"], row["uid"], row["gid"]
                            )
                        else:
                            path.unlink(missing_ok=True)
                    except OSError as exc:
                        restore_errors.append(exc)
                if restore_errors:
                    raise RuntimeError("Refero registration rollback did not restore every target")
                raise
        receipt["files"] = {
            relative: {key: row[key] for key in ("before_sha256", "after_sha256")}
            for relative, row in files.items()
        }
        receipt["changed_paths"] = sorted(set(receipt["changed_paths"]))
        if args.dry_run:
            receipt["status"] = "dry_run"
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception:
        print("error: Refero registration/rollback failed closed; no configuration values emitted", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-root", type=Path, help="Client HERMES_HOME (contains config.yaml)")
    parser.add_argument("--package-root", type=Path, help="Refero package/registry root owning the named profile")
    parser.add_argument("--registry-root", type=Path, help="Refero consumed registry root in the conventional Windows layout")
    parser.add_argument("--config-path", type=Path, help="Explicit path to client config.yaml")
    parser.add_argument("--manifest", type=Path, help="Path to manifest yaml (for overlay_config_exemptions)")
    parser.add_argument("--exemption", action="append", default=[], help="Extra dotted-path exemption; repeatable")
    parser.add_argument(
        "--defaults-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "shared-defaults",
        help="Directory of config-*.yaml default sets",
    )
    parser.add_argument(
        "--receipt-json",
        action="store_true",
        help="Emit a machine-readable reconciliation receipt",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show diff, do not write")
    parser.add_argument("--quiet", action="store_true", help="Suppress informational output")
    parser.add_argument("--hermes-python", help="Exact candidate interpreter for Refero-only cold registration")
    parser.add_argument("--rollback-dir", type=Path, help="Existing rollback directory for Refero-only registration")
    parser.add_argument("--restore", action="store_true", help="Restore the Refero-only two-file snapshot")
    parser.add_argument(
        "--refero-source-registry", type=Path,
        default=Path(__file__).resolve().parent.parent / "mcp-servers/capability-router/public-floor-registry.json",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "native-image", "refero-styles"),
        default="all",
        help="Apply all defaults, native-image routing, or exact Refero-only cold registration",
    )
    args = parser.parse_args(argv)

    if args.scope == "refero-styles":
        return _run_refero_scope(args)
    if args.restore or args.hermes_python or args.rollback_dir or args.package_root or args.registry_root:
        parser.error("Refero registration options require --scope refero-styles")

    if args.config_path:
        config_path = args.config_path.expanduser().resolve()
    elif args.profile_root:
        config_path = (args.profile_root.expanduser() / "config.yaml").resolve()
    else:
        print("error: --profile-root or --config-path required", file=sys.stderr)
        return 1

    if not config_path.exists():
        print(f"error: client config not found: {config_path}", file=sys.stderr)
        return 1

    defaults_files = _discover_defaults_files(args.defaults_dir)
    if args.scope == "native-image":
        defaults_files = [path for path in defaults_files if path.name in NATIVE_IMAGE_DEFAULT_NAMES]
        found = {path.name for path in defaults_files}
        if found != NATIVE_IMAGE_DEFAULT_NAMES:
            missing = sorted(NATIVE_IMAGE_DEFAULT_NAMES - found)
            print(
                "error: incomplete native-image defaults: " + ", ".join(missing),
                file=sys.stderr,
            )
            return 1
    if not defaults_files:
        print(f"error: no defaults files in {args.defaults_dir}", file=sys.stderr)
        return 1

    exemptions: list[str] = list(args.exemption)
    if args.manifest:
        manifest_path = args.manifest.expanduser().resolve()
        try:
            manifest = _load_yaml(manifest_path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        m_exempts = manifest.get("overlay_config_exemptions") or []
        if not isinstance(m_exempts, list):
            print(f"error: {manifest_path}: overlay_config_exemptions must be a list", file=sys.stderr)
            return 1
        exemptions.extend(str(e) for e in m_exempts)

    try:
        client_config = _load_yaml(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    all_applied: list[tuple[str, str]] = []
    all_retired: list[tuple[str, str]] = []
    all_skipped: list[tuple[str, str]] = []
    merged = client_config
    retirement_path = args.defaults_dir / "retired-policy-defaults-v1.yaml"
    if args.scope == "all" and retirement_path.exists():
        try:
            retirement = _load_yaml(retirement_path)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if retirement.get("schema_version") != 1 or not isinstance(retirement.get("retired_defaults"), dict):
            print(f"error: {retirement_path}: invalid retirement manifest", file=sys.stderr)
            return 1
        merged, retired, skipped = retire_matching_defaults(
            merged,
            retirement["retired_defaults"],
            exemptions,
        )
        for key in retired:
            all_retired.append((retirement_path.name, key))
        for key in skipped:
            all_skipped.append((retirement_path.name, key))

    for defaults_path in defaults_files:
        try:
            defaults = _load_yaml(defaults_path)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if defaults_path.name == "config-mcp-on-demand-control.yaml":
            if args.scope == "native-image":
                try:
                    merged, applied, skipped = _reconcile_native_image_exposure(merged, defaults, exemptions)
                except ValueError as exc:
                    print(f"error: {defaults_path}: {exc}", file=sys.stderr)
                    return 1
            else:
                merged, applied, skipped = reconcile_mcp_control_defaults(merged, defaults, exemptions)
        else:
            merged, applied, skipped = merge(merged, defaults, exemptions)
        for k in applied:
            all_applied.append((defaults_path.name, k))
        for k in skipped:
            all_skipped.append((defaults_path.name, k))

    receipt = None
    if args.scope == "native-image":
        try:
            receipt = _native_image_receipt(merged, exemptions, all_applied, all_skipped)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    old_text = config_path.read_text()
    new_text = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
    if receipt is not None:
        receipt["sha256_before"] = hashlib.sha256(old_text.encode()).hexdigest()
        receipt["sha256_after"] = hashlib.sha256(new_text.encode()).hexdigest()

    if merged == client_config:
        if args.receipt_json and receipt is not None:
            receipt["sha256_after"] = receipt["sha256_before"]
            print(json.dumps(receipt, sort_keys=True))
        elif not args.quiet:
            print(f"[merge-shared-defaults] {config_path}: already in sync (no changes)")
        return 0

    if args.dry_run:
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(config_path),
            tofile=str(config_path) + " (after merge)",
        )
        sys.stdout.writelines(diff)
        if not args.quiet:
            changed = len(all_applied) + len(all_retired)
            print(f"\n[merge-shared-defaults] dry-run: {changed} key(s) would change")
        return 0

    # Atomic write: tmp + rename
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp-merge")
    tmp_path.write_text(new_text)
    tmp_path.chmod(config_path.stat().st_mode & 0o777)
    tmp_path.replace(config_path)

    if args.receipt_json and receipt is not None:
        print(json.dumps(receipt, sort_keys=True))
    elif not args.quiet:
        print(f"[merge-shared-defaults] wrote {config_path}")
        for fname, key in all_retired:
            print(f"  retired  {fname}: {key}")
        for fname, key in all_applied:
            print(f"  applied  {fname}: {key}")
        for fname, key in all_skipped:
            print(f"  skipped  {fname}: {key} (exempted)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
