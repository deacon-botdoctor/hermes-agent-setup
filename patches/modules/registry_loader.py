#!/usr/bin/env python3
"""Registry-driven patch loader.

Reads patches/registry.yaml and resolves each standalone entry to a callable
patch function. The active orchestrator supplies no monolith patch map, so a
legacy ``module: monolith`` entry cannot resolve and required entries fail
closed.

Usage (from apply-all-patches.py main()):
    from patches.modules.registry_loader import load_registry, resolve_patch_fn

    registry = load_registry()
    for entry in registry:
        fn = resolve_patch_fn(entry)
        if fn:
            fn(hermes_dir)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def load_registry(registry_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load and return the patches list from registry.yaml.

    Returns an empty list if the file is missing or pyyaml is unavailable.
    """
    if registry_path is None:
        registry_path = Path(__file__).parent.parent / "registry.yaml"

    if not registry_path.exists():
        return []

    try:
        import yaml
    except ImportError:
        return _load_registry_without_yaml(registry_path)

    with open(registry_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "patches" not in data:
        return []

    return data["patches"]


def _unquote_registry_value(value: str) -> str:
    value = value.strip()
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _load_registry_without_yaml(registry_path: Path) -> List[Dict[str, Any]]:
    """Minimal parser for the flat registry.yaml shape used by apply-all."""
    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    in_patches = False
    for line in registry_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("patches:"):
            in_patches = True
            continue
        if not in_patches or not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - name:"):
            current = {"name": _unquote_registry_value(line.split(":", 1)[1])}
            entries.append(current)
            continue
        if current is None or not line.startswith("    ") or ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        if key in {
            "module",
            "function",
            "target",
            "owner",
            "idempotency",
            "test",
            "rollback",
            "client_impact",
            "status",
            "priority",
            "smoke",
        }:
            current[key] = _unquote_registry_value(value)
        elif key == "optional":
            current[key] = _unquote_registry_value(value).lower() in {"true", "yes", "1"}
        elif key == "tags":
            raw = _unquote_registry_value(value)
            if raw.startswith("[") and raw.endswith("]"):
                current[key] = [part.strip().strip("'\"") for part in raw[1:-1].split(",") if part.strip()]
    return entries


def resolve_patch_fn(
    entry: Dict[str, Any],
    monolith_patches: Optional[Dict[str, Callable]] = None,
    patches_dir: Optional[Path] = None,
) -> Optional[Callable]:
    """Resolve a registry entry to a callable patch function.

    Resolution order:
    1. If module == "monolith", look up in monolith_patches dict
    2. If module points to patches/modules/<name>.py, import dynamically
    3. If module points to patches/apply-<name>.py, import dynamically
    4. Return None if unresolvable

    The returned callable has signature: (hermes_dir: Path) -> bool
    """
    if patches_dir is None:
        patches_dir = Path(__file__).parent.parent

    module_ref = entry.get("module", "")
    function_name = entry.get("function", "")
    patch_name = entry.get("name", "unknown")

    # Case 1: still in the monolith
    if module_ref == "monolith":
        if monolith_patches and function_name in monolith_patches:
            return monolith_patches[function_name]
        if monolith_patches and patch_name in monolith_patches:
            return monolith_patches[patch_name]
        return None

    # Case 2/3: standalone module file
    # module_ref can be like "modules/memory_tool_fcntl.py"
    module_path = patches_dir / module_ref
    if not module_path.exists():
        # Try without .py extension
        if not module_ref.endswith(".py"):
            module_path = patches_dir / (module_ref + ".py")
        if not module_path.exists():
            print(f"[registry_loader] WARN: module not found for {patch_name}: {module_ref}")
            return None

    try:
        spec = importlib.util.spec_from_file_location(f"patches.dynamic.{patch_name}", module_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[registry_loader] ERROR loading module for {patch_name}: {e}")
        return None

    # Look up the function
    fn = getattr(mod, function_name, None)
    if fn is None:
        # Try the patch_<name> convention
        alt_name = f"patch_{patch_name}"
        fn = getattr(mod, alt_name, None)
    if fn is None:
        print(f"[registry_loader] WARN: function {function_name} not found in {module_path}")

    return fn


def build_ordered_patch_list(
    registry_entries: List[Dict[str, Any]],
    monolith_patches: Optional[Dict[str, Callable]] = None,
    patches_dir: Optional[Path] = None,
    skip_names: Optional[set] = None,
) -> List[tuple]:
    """Build an ordered list of (name, callable) tuples from the registry.

    This is the authoritative registry-driven patch list.
    Entries that can't be resolved or are in skip_names are omitted with a warning.
    """
    if skip_names is None:
        skip_names = set()

    result = []
    for entry in registry_entries:
        name = entry.get("name", "unknown")
        aliases = entry.get("aliases") or []
        if name in skip_names and not aliases:
            continue
        fn = resolve_patch_fn(entry, monolith_patches, patches_dir)
        if fn is not None:
            result.append((name, fn))
        else:
            print(f"[registry_loader] SKIP: could not resolve {name}")

    return result


def load_skip_list(hermes_home: Optional[Path] = None) -> set:
    """Load patch names to skip from ~/.hermes/state/skip-patches.txt."""
    if hermes_home is None:
        hermes_home = Path.home() / ".hermes"
    skip_file = hermes_home / "state" / "skip-patches.txt"
    if not skip_file.exists():
        return set()
    names = set()
    for line in skip_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names
