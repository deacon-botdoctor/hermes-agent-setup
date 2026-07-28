#!/usr/bin/env python3
"""Merge shared-defaults/*.yaml into a client's config.yaml.

Every leaf key in each defaults file REPLACES the same-keyed value in the client's
config.yaml unless the dotted path appears in the manifest's `overlay_config_exemptions`
list. The MCP control default instead reconciles its governed plugin and platform
toolset lists additively. Missing intermediate dicts are created. Keys present in the
client config but not in defaults are preserved untouched. Idempotent.

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

Exit codes:
    0  — merge applied (changes written) or no-op (already in sync)
    1  — error (missing files, parse failure, etc.)
"""

from __future__ import annotations

import argparse
import difflib
import sys
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
# clobber where a leftover client-routing default reverted multiple agents.
#
# Auxiliary lanes are different: they are fleet-owned routine tool routes. Drift
# here breaks tools silently (for example Codex-mini vision on ChatGPT accounts).
# Apply shared-default auxiliary routing unless a manifest explicitly exempts a
# dotted auxiliary path.
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
    for dotted, value in _walk_leaves("", defaults):
        if dotted in exempt_set or _is_protected(dotted):
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
    elif isinstance(plugin_additions, list):
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
            wanted = current + [item for item in governed if item not in current]
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
            wanted = current + [item for item in governed if item not in current]
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-root", type=Path, help="Client HERMES_HOME (contains config.yaml)")
    parser.add_argument("--config-path", type=Path, help="Explicit path to client config.yaml")
    parser.add_argument("--manifest", type=Path, help="Path to manifest yaml (for overlay_config_exemptions)")
    parser.add_argument("--exemption", action="append", default=[], help="Extra dotted-path exemption; repeatable")
    parser.add_argument(
        "--defaults-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "shared-defaults",
        help="Directory of config-*.yaml default sets",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show diff, do not write")
    parser.add_argument("--quiet", action="store_true", help="Suppress informational output")
    args = parser.parse_args(argv)

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
    if retirement_path.exists():
        try:
            retirement = _load_yaml(retirement_path)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if retirement.get("schema_version") != 1 or not isinstance(
            retirement.get("retired_defaults"), dict
        ):
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
            merged, applied, skipped = reconcile_mcp_control_defaults(merged, defaults, exemptions)
        else:
            merged, applied, skipped = merge(merged, defaults, exemptions)
        for k in applied:
            all_applied.append((defaults_path.name, k))
        for k in skipped:
            all_skipped.append((defaults_path.name, k))

    if merged == client_config:
        if not args.quiet:
            print(f"[merge-shared-defaults] {config_path}: already in sync (no changes)")
        return 0

    new_text = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
    old_text = config_path.read_text()

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

    if not args.quiet:
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
