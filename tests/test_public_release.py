from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
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


def test_profile_defaults_and_router_binding_are_reconciled(tmp_path):
    installer = load_script("public_install_config", "install-profile.py")
    config_path = tmp_path / "config.yaml"
    python = tmp_path / "runtime" / "venv" / "bin" / "python"
    config_path.write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["user-plugin"]},
                "mcp_servers": {
                    "capability-router": {
                        "command": "/old/runtime/python",
                        "args": ["old"],
                        "env": {"CUSTOM": "kept", "HERMES_HOME": "/old/home"},
                        "enabled": False,
                        "timeout": 45,
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    changed = installer.ensure_public_config(config_path, tmp_path, python)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["plugins"]["enabled"] == [
        "user-plugin",
        "botdoctor-immersion",
        "mcp-on-demand-control",
    ]
    assert "task-ledger" not in config["plugins"]["enabled"]
    assert "telegram-transcript" not in config["plugins"]["enabled"]
    router = config["mcp_servers"]["capability-router"]
    assert router["command"] == str(python)
    assert router["args"] == ["-m", "capability_router.server"]
    assert router["enabled"] is True
    assert router["env"]["HERMES_HOME"] == str(tmp_path)
    assert router["env"]["CUSTOM"] == "kept"
    assert router["timeout"] == 45
    assert "mcp_servers.capability-router" in changed


def test_profile_install_interrupt_restores_from_pending_receipt(
    tmp_path, monkeypatch
):
    installer = load_script("public_install_interrupt", "install-profile.py")
    home = tmp_path / "profile"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    destinations = [
        home / "plugins" / "first.py",
        home / "plugins" / "second.py",
    ]
    for index, destination in enumerate(destinations, 1):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"before-{index}\n", encoding="utf-8")
    sources = [
        ROOT / "plugins" / "botdoctor-immersion" / "__init__.py",
        ROOT / "plugins" / "task-ledger" / "__init__.py",
    ]

    monkeypatch.setattr(installer, "verify_runtime", lambda _runtime: None)
    monkeypatch.setattr(
        installer, "runtime_python", lambda _runtime, _explicit: Path(sys.executable)
    )
    monkeypatch.setattr(
        installer,
        "profile_files",
        lambda _home: [
            (sources[0], destinations[0], 0o644),
            (sources[1], destinations[1], 0o644),
        ],
    )
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    original_atomic_bytes = installer.atomic_bytes
    writes = 0

    def interrupt_second_destination(path, data, mode):
        nonlocal writes
        if path in destinations:
            writes += 1
            if writes == 2:
                raise KeyboardInterrupt
        original_atomic_bytes(path, data, mode)

    monkeypatch.setattr(installer, "atomic_bytes", interrupt_second_destination)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install-profile.py",
            "--hermes-home",
            str(home),
            "--runtime-dir",
            str(runtime),
        ],
    )

    with pytest.raises(KeyboardInterrupt):
        installer.main()

    assert destinations[0].read_text(encoding="utf-8") == "before-1\n"
    assert destinations[1].read_text(encoding="utf-8") == "before-2\n"
    backups = list((home / "state" / "public-setup-backups").iterdir())
    assert len(backups) == 1
    receipt = json.loads((backups[0] / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "pending"
    assert len(receipt["files"]) == 2


def test_shared_default_merge_preserves_config_mode(tmp_path):
    config_path = tmp_path / "config.yaml"
    defaults_dir = tmp_path / "defaults"
    defaults_dir.mkdir()
    config_path.write_text("display:\n  compact: false\n", encoding="utf-8")
    config_path.chmod(0o600)
    (defaults_dir / "config-test.yaml").write_text(
        "display:\n  compact: true\n", encoding="utf-8"
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "merge-shared-defaults.py"),
            "--config-path",
            str(config_path),
            "--defaults-dir",
            str(defaults_dir),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_optional_capture_hooks_are_off_without_profile_opt_in(monkeypatch):
    monkeypatch.delenv("HERMES_ENABLE_GBRAIN_CAPTURE", raising=False)
    monkeypatch.delenv("HERMES_ENABLE_TELEGRAM_TRANSCRIPT", raising=False)
    gbrain = load_script(
        "public_gbrain_capture", "../hooks/gbrain-capture/handler.py"
    )
    transcript = load_script(
        "public_telegram_capture", "../hooks/telegram-transcript/handler.py"
    )
    assert gbrain._capture_enabled() is False
    assert transcript._capture_enabled() is False
    monkeypatch.setattr(
        gbrain,
        "_do_capture",
        lambda _context: pytest.fail("disabled GBrain hook executed"),
    )
    monkeypatch.setattr(
        transcript,
        "_get_db",
        lambda: pytest.fail("disabled transcript hook opened its database"),
    )
    asyncio.run(gbrain.handle("agent:end", {"platform": "telegram"}))
    asyncio.run(
        transcript.handle(
            "agent:start",
            {"platform": "telegram", "chat_id": "123", "message": "hello"},
        )
    )


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
    forbidden = (
        "/Users/deacon",
        "/home/spark-",
        "@botdoctor.io",
        "208214988",
        "-5217351028",
    )
    private_terms = re.compile(
        r"\b(?:deacon|enoch|minions|ridley|spark|escalate_to_doc)\b",
        re.IGNORECASE,
    )
    private_roles = re.compile(r"\b(?:Doc|Mini)\b")
    for relative in sorted(paths):
        text = (ROOT / relative).read_text(encoding="utf-8", errors="ignore")
        for value in forbidden:
            assert value not in text, f"{relative} contains private route {value}"
        assert private_terms.search(text) is None, (
            f"{relative} contains private control-plane terminology"
        )
        assert private_roles.search(text) is None, (
            f"{relative} contains private control-plane role names"
        )
