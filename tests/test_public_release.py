from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_source_verifies_exactly():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "verify-release.py"), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert result["errors"] == []


def test_release_identity_matches_source_manifest():
    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "runtime-payload-source-manifest.json").read_text(encoding="utf-8")
    )
    assert release["golden_sha"] == manifest["golden_sha"]
    assert release["canonical_upstream_sha"] == manifest["canonical_upstream_sha"]
    assert release["deployment_digest"] == manifest["deployment_digest"]
    assert (
        release["runtime_payload_digest"]
        == manifest["components"]["runtime_payload"]["digest"]
    )
    assert manifest["components"]["runtime_payload"]["file_count"] == 77
    assert set(manifest["components"]) == {"runtime_payload"}
    assert release["source_scope"] == "sanitized_runtime_payload_only"


def test_registry_has_only_explained_retirable_patches():
    registry = yaml.safe_load(
        (ROOT / "patches" / "registry.yaml").read_text(encoding="utf-8")
    )
    patches = registry["patches"]
    assert len(patches) == 16
    for patch in patches:
        assert patch["reason"].strip()
        assert patch["retirement_condition"].strip()
        assert patch["test"].strip()
        assert patch["rollback"].strip()


def test_profile_installer_maps_only_bounded_profile_files(tmp_path):
    installer = load_script("public_install_profile", "install-profile.py")
    mappings = installer.profile_files(tmp_path)
    destinations = {
        destination.relative_to(tmp_path).as_posix()
        for _source, destination, _mode in mappings
    }
    assert "plugins/botdoctor-immersion/plugin.yaml" in destinations
    assert "hooks/telegram-transcript/handler.py" in destinations
    assert "mcp-servers/capability-router/registry.json" in destinations
    assert "bin/telegram-transaction-canary.py" in destinations
    assert not any(path.startswith("patches/") for path in destinations)
    assert not any(path.startswith("shared-defaults/") for path in destinations)
    assert not any(path.startswith("kit/systemd/") for path in destinations)


def test_public_text_has_no_private_absolute_runtime_routes():
    manifest = json.loads(
        (ROOT / "runtime-payload-source-manifest.json").read_text(encoding="utf-8")
    )
    paths = {
        entry["path"]
        for component in manifest["components"].values()
        for entry in component["files"]
    }
    paths.update(
        {
            "README.md",
            "AGENTS.md",
            "DEFAULTS.md",
            "MIGRATION.md",
            "RUNTIME.md",
        }
    )
    forbidden = ("/Users/deacon", "/home/spark-", "@botdoctor.io")
    for relative in sorted(paths):
        text = (ROOT / relative).read_text(encoding="utf-8", errors="ignore")
        for value in forbidden:
            assert value not in text, f"{relative} contains private route {value}"
