from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
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
    assert release["golden_deployment_digest"] == (
        manifest["golden_deployment_digest"]
    )
    assert (
        release["runtime_payload_digest"]
        == manifest["components"]["runtime_payload"]["digest"]
    )
    assert manifest["components"]["runtime_payload"]["file_count"] == 103
    assert set(manifest["components"]) == {"runtime_payload"}
    assert release["source_scope"] == "sanitized_runtime_payload_only"
    assert release["assembled_runtime_fingerprint"] == {
        "digest": "1c168feb84dbc1111e093bb52e75072aa7b1d79ff00028654bfbab6a87c0de66",
        "file_count": 77,
    }
    assert manifest["assembled_runtime_fingerprint"]["digest"] == (
        release["assembled_runtime_fingerprint"]["digest"]
    )
    assert manifest["assembled_runtime_fingerprint"]["file_count"] == 77
    assert len(manifest["assembled_runtime_fingerprint"]["files"]) == 77
    assert set(release) == {
        "schema_version",
        "release",
        "status",
        "source_manifest",
        "golden_sha",
        "canonical_upstream_sha",
        "source_scope",
        "runtime_payload_digest",
        "deployment_digest",
        "golden_deployment_digest",
        "assembled_runtime_fingerprint",
        "verification",
        "cua_driver",
        "runtime_coherence",
        "update_contract",
    }
    assert set(release["verification"]) == {
        "golden_suite",
        "clean_upstream_rehearsal",
    }


def test_release_pins_the_golden_cua_driver_contract():
    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    driver = release["cua_driver"]
    helper = ROOT / driver["helper"]["path"]
    contract_path = ROOT / driver["contract"]["path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert driver["version"] == "0.14.2"
    assert driver["tag"] == "cua-driver-rs-v0.14.2"
    assert driver["baseline_acceptance"] == "exact_version_present"
    assert driver["gui_acceptance"] == "doctor_ready_and_list_windows"
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == driver["helper"]["sha256"]
    assert (
        hashlib.sha256(contract_path.read_bytes()).hexdigest()
        == driver["contract"]["sha256"]
    )
    assert contract["release"]["version"] == driver["version"]
    assert contract["release"]["source_commit"] == driver["source_commit"]


def test_release_pins_the_cross_platform_runtime_coherence_package():
    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    contract = release["runtime_coherence"]
    files = [
        "maintenance/bin/install-runtime-coherence.py",
        "maintenance/launchd/com.hermes.runtime-coherence.plist.template",
        "maintenance/systemd/hermes-runtime-coherence@.service",
        "maintenance/systemd/hermes-runtime-coherence@.timer",
        "maintenance/windows/hermes-runtime-coherence-task.ps1.template",
        "checks/agent-runtime-coherence.py",
        "spec/runtime-coherence.json",
    ]
    canonical = ""
    for relative in files:
        digest = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        canonical += f"{relative}\0{digest}\n"
    assert contract["source_commit"] == (
        "183de949ad32611e77a50b9de042c76e2044cd14"
    )
    assert contract["platforms"] == ["macos", "linux", "windows"]
    assert contract["file_count"] == len(files)
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        contract["package_digest"]
    )


def test_service_proof_writes_exact_runtime_binding_receipt(tmp_path: Path):
    binder = load_script("public_runtime_binding", "bind-service-circuit.py")
    home = tmp_path / "home/.hermes"
    runtime = home / "state/runtime-candidates/release"
    python = runtime / "venv/bin/python"
    definition = tmp_path / "Library/LaunchAgents/ai.hermes.gateway.plist"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    definition.parent.mkdir(parents=True)
    definition.write_bytes(b"plist-v1")

    receipt_path, rollback = binder._write_runtime_binding(
        home,
        runtime,
        service_kind="launchd-user",
        service_owner="agent",
        definition_path=definition,
        definition_bytes=definition.read_bytes(),
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 1
    assert receipt["kind"] == "botdoctor_runtime_binding"
    assert receipt["status"] == "active"
    assert receipt["hermes_home"] == str(home)
    assert receipt["runtime_root"] == str(runtime)
    assert receipt["runtime_python"] == str(python)
    assert receipt["service"] == {
        "kind": "launchd-user",
        "owner": "agent",
        "definition_path": str(definition),
        "definition_sha256": hashlib.sha256(b"plist-v1").hexdigest(),
        "launchers": [],
    }
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert rollback.is_dir()
    binder._restore_runtime_binding(home, rollback)
    assert not receipt_path.exists()


def test_runtime_binding_rollback_restores_prior_receipt_exactly(tmp_path: Path):
    binder = load_script("public_runtime_binding_rollback", "bind-service-circuit.py")
    home = tmp_path / "home/.hermes"
    first_runtime = home / "state/runtime-candidates/first"
    second_runtime = home / "state/runtime-candidates/second"
    definition = tmp_path / "Library/LaunchAgents/ai.hermes.gateway.plist"
    for runtime in (first_runtime, second_runtime):
        python = runtime / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"python")
    definition.parent.mkdir(parents=True)
    definition.write_bytes(b"plist")
    receipt_path, _ = binder._write_runtime_binding(
        home,
        first_runtime,
        service_kind="launchd-user",
        service_owner="agent",
        definition_path=definition,
        definition_bytes=definition.read_bytes(),
    )
    before = receipt_path.read_bytes()

    _, rollback = binder._write_runtime_binding(
        home,
        second_runtime,
        service_kind="launchd-user",
        service_owner="agent",
        definition_path=definition,
        definition_bytes=definition.read_bytes(),
    )
    binder._restore_runtime_binding(home, rollback)

    assert receipt_path.read_bytes() == before


def test_runtime_coherence_scheduler_rejects_monitored_candidate_python(
    tmp_path: Path,
):
    home = tmp_path / "home/.hermes"
    runtime = home / "state/runtime-candidates/release"
    python = runtime / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "maintenance/bin/install-runtime-coherence.py"),
            "plan",
            "--agent-id",
            "test",
            "--home",
            str(home),
            "--runtime-root",
            str(runtime),
            "--runtime-python",
            str(python),
            "--scheduler-python",
            str(python),
            "--runtime-user",
            "agent",
            "--user-home",
            str(tmp_path / "home"),
            "--platform",
            "macos",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "scheduler Python must be outside the monitored runtime root" in proc.stdout


def test_stable_coherence_scheduler_reports_removed_candidate(tmp_path: Path):
    home = tmp_path / "home/.hermes"
    runtime = home / "state/runtime-candidates/release"
    runtime_python = runtime / "venv/bin/python"
    definition = tmp_path / "Library/LaunchAgents/ai.hermes.gateway.plist"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"python")
    definition.parent.mkdir(parents=True)
    definition.write_bytes(b"plist")
    binder = load_script("removed_candidate_binding", "bind-service-circuit.py")
    binder._write_runtime_binding(
        home,
        runtime,
        service_kind="launchd-user",
        service_owner="agent",
        definition_path=definition,
        definition_bytes=definition.read_bytes(),
    )
    receipt = home / "state/health/runtime-coherence.json"
    shutil.rmtree(runtime)

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "checks/agent-runtime-coherence.py"),
            "--home",
            str(home),
            "--runtime-root",
            str(runtime),
            "--runtime-python",
            str(runtime_python),
            "--agent-id",
            "test",
            "--receipt",
            str(receipt),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert json.loads(proc.stdout)["kind"] == "binding_missing"
    assert json.loads(receipt.read_text(encoding="utf-8"))["kind"] == "binding_missing"


def test_runtime_coherence_fails_closed_on_service_definition_drift(tmp_path: Path):
    check = load_path(
        "public_runtime_coherence",
        ROOT / "checks/agent-runtime-coherence.py",
    )
    home = tmp_path / "home/.hermes"
    runtime = home / "state/runtime-candidates/release"
    python = runtime / "venv/bin/python"
    definition = tmp_path / "Library/LaunchAgents/ai.hermes.gateway.plist"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    definition.parent.mkdir(parents=True)
    definition.write_bytes(b"expected")
    binding = home / "state/runtime-binding.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "botdoctor_runtime_binding",
                "status": "active",
                "hermes_home": str(home.resolve()),
                "runtime_root": str(runtime.resolve()),
                "runtime_python": str(python.absolute()),
                "service": {
                    "kind": "launchd-user",
                    "owner": "agent",
                    "definition_path": str(definition),
                    "definition_sha256": hashlib.sha256(b"expected").hexdigest(),
                    "launchers": [],
                },
            }
        ),
        encoding="utf-8",
    )

    valid = check.validate_runtime_binding(
        binding_path=binding,
        runtime_root=runtime,
        runtime_python=python,
        hermes_home=home,
    )
    definition.write_bytes(b"overwritten")
    drift = check.validate_runtime_binding(
        binding_path=binding,
        runtime_root=runtime,
        runtime_python=python,
        hermes_home=home,
    )

    assert valid["ok"] is True
    assert drift == {
        "path": str(binding),
        "ok": False,
        "reason": "binding_definition_drift",
    }


def test_release_payload_keeps_critical_blobs():
    manifest = json.loads(
        (ROOT / "runtime-payload-source-manifest.json").read_text(encoding="utf-8")
    )
    blobs = {
        entry["path"]: entry["blob"]
        for component in manifest["components"].values()
        for entry in component["files"]
    }
    assert (
        blobs["patches/modules/codex_401_paid_fallback_circuit_v1.py"]
        == "47a67853f523b7af13aecc2188086ff0eedb7d44"
    )
    assert (
        blobs["patches/modules/telegram_dm_topic_recovery_root_guard_v1.py"]
        == "ba7fde1f94ae40f3ed3eb219826f369b5c646279"
    )


def test_registry_has_only_explained_retirable_patches():
    registry = yaml.safe_load(
        (ROOT / "patches" / "registry.yaml").read_text(encoding="utf-8")
    )
    patches = registry["patches"]
    assert len(patches) == 23
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
    assert "plugins/semantic-computer-control-guard/plugin.yaml" in destinations
    assert "skills/fleet/golden-computer-use-v2/SKILL.md" in destinations
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
                "skills": {"index_allowlist": ["user-skill"]},
                "platform_toolsets": {
                    "cli": ["terminal"],
                    "telegram": ["web"],
                    "cron": ["todo"],
                },
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
    assert "semantic-computer-control-guard" not in config["plugins"]["enabled"]
    assert config["skills"]["index_allowlist"] == [
        "user-skill",
        "golden-computer-use-v2",
    ]
    assert config["platform_toolsets"]["cli"] == ["terminal", "computer_use"]
    assert config["platform_toolsets"]["telegram"] == ["web", "computer_use"]
    assert config["platform_toolsets"]["cron"] == ["todo"]
    router = config["mcp_servers"]["capability-router"]
    assert router["command"] == str(python)
    assert router["args"] == ["-m", "capability_router.server"]
    assert router["enabled"] is True
    assert router["env"]["HERMES_HOME"] == str(tmp_path)
    assert router["env"]["CUSTOM"] == "kept"
    assert router["timeout"] == 45
    assert "mcp_servers.capability-router" in changed


def test_profile_installer_requires_pinned_driver_before_profile_mutation(
    tmp_path, monkeypatch
):
    installer = load_script("public_install_driver", "install-profile.py")
    runtime_python = tmp_path / "candidate" / "venv" / "bin" / "python"
    home = tmp_path / "profile"
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "installed",
                    "after": {"installed": True, "version": "0.14.2"},
                    "doctor_ready": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    receipt = installer.ensure_cua_driver(runtime_python, home)

    assert receipt["after"]["version"] == "0.14.2"
    assert calls[0][0] == sys.executable
    assert calls[0][1] == str(ROOT / "bin" / "ensure-cua-driver.py")
    assert calls[0][calls[0].index("--hermes-python") + 1] == str(runtime_python)
    assert calls[0][calls[0].index("--hermes-home") + 1] == str(home)
    assert "--require-ready" not in calls[0]


def test_profile_installer_can_require_gui_driver_readiness(tmp_path, monkeypatch):
    installer = load_script("public_install_driver_ready", "install-profile.py")

    def fake_run(command, **_kwargs):
        assert "--require-ready" in command
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "status": "blocked_not_ready",
                    "after": {"installed": True, "version": "0.14.2"},
                    "doctor_ready": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="blocked_not_ready"):
        installer.ensure_cua_driver(
            tmp_path / "python",
            tmp_path / "profile",
            require_ready=True,
        )


def test_profile_semantic_awareness_does_not_create_restrictive_lists(tmp_path):
    installer = load_script("public_install_semantic_minimal", "install-profile.py")
    config_path = tmp_path / "config.yaml"
    python = tmp_path / "runtime" / "venv" / "bin" / "python"
    config_path.write_text("plugins: {}\n", encoding="utf-8")

    installer.ensure_public_config(config_path, tmp_path, python)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert "skills" not in config
    assert "platform_toolsets" not in config
    assert "semantic-computer-control-guard" not in config["plugins"]["enabled"]


def test_unknown_existing_plugin_selections_are_preserved(tmp_path):
    installer = load_script("public_install_existing_plugins", "install-profile.py")
    config_path = tmp_path / "config.yaml"
    python = tmp_path / "runtime" / "venv" / "bin" / "python"
    existing = ["task-ledger", "telegram-transcript", "user-plugin"]
    config_path.write_text(
        yaml.safe_dump({"plugins": {"enabled": existing}}, sort_keys=False),
        encoding="utf-8",
    )

    installer.ensure_public_config(config_path, tmp_path, python)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["plugins"]["enabled"][:3] == existing


def test_profile_paths_reject_symlinked_parents(tmp_path):
    installer = load_script("public_install_paths", "install-profile.py")
    home = tmp_path / "profile"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "plugins").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="contains a symlink"):
        installer.validate_profile_path(home, home / "plugins" / "plugin.py")


def test_profile_paths_reject_reparse_parents(tmp_path, monkeypatch):
    installer = load_script("public_install_reparse", "install-profile.py")
    home = tmp_path / "profile"
    parent = home / "plugins"
    parent.mkdir(parents=True)
    monkeypatch.setattr(
        installer, "_is_reparse_point", lambda path: path == parent
    )

    with pytest.raises(RuntimeError, match="contains a reparse point"):
        installer.validate_profile_path(home, parent / "plugin.py")


def test_staging_initializer_is_explicit_and_credential_free(tmp_path):
    installer = load_script("public_install_staging", "install-profile.py")
    home = tmp_path / "staging"
    config_path = home / "config.yaml"

    installer.initialize_staging_config(home, config_path)

    assert config_path.read_bytes() == b"{}\n"
    assert config_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="already exists"):
        installer.initialize_staging_config(home, config_path)
    backup = home / "state" / "public-setup-backups" / "receipt"
    (backup / "files").mkdir(parents=True)
    installer.restore([], backup, home, config_path, False, None, None)
    assert not config_path.exists()

    scaffolded_home = tmp_path / "scaffolded"
    (scaffolded_home / "sessions").mkdir(parents=True)
    (scaffolded_home / "memories").mkdir()
    installer.initialize_staging_config(
        scaffolded_home, scaffolded_home / "config.yaml"
    )

    marked_home = tmp_path / "marked"
    marked_home.mkdir()
    (marked_home / "sessions").mkdir()
    (marked_home / "sessions" / "live.json").write_text("{}\n")
    with pytest.raises(RuntimeError, match="live state or service markers"):
        installer.initialize_staging_config(
            marked_home, marked_home / "config.yaml"
        )


def test_restore_preflights_all_hashes_before_writing(tmp_path, monkeypatch):
    installer = load_script("public_install_restore", "install-profile.py")
    home = tmp_path / "profile"
    backup = home / "state" / "public-setup-backups" / "receipt"
    saved_dir = backup / "files"
    saved_dir.mkdir(parents=True)
    config_path = home / "config.yaml"
    config_path.write_text("after\n", encoding="utf-8")
    config_data = b"before-config\n"
    (backup / "config.yaml.before").write_bytes(config_data)
    destinations = [home / "plugins" / "first.py", home / "plugins" / "second.py"]
    rows = []
    for index, destination in enumerate(destinations):
        key = f"{index + 1:064x}"
        saved = saved_dir / key
        data = f"before-{index}\n".encode()
        saved.write_bytes(data)
        rows.append(
            {
                "destination": str(destination),
                "source": f"plugins/example-{index}.py",
                "mode": "0644",
                "backup_key": key,
                "existed": True,
                "before_mode": "0644",
                "before_sha256": (
                    installer.sha256(saved)
                    if index == 0
                    else "0" * 64
                ),
            }
        )
    writes = []
    monkeypatch.setattr(
        installer,
        "atomic_bytes",
        lambda path, data, mode: writes.append((path, data, mode)),
    )

    with pytest.raises(RuntimeError, match="hash does not match receipt"):
        installer.restore(
            rows,
            backup,
            home,
            config_path,
            True,
            hashlib.sha256(config_data).hexdigest(),
            "0640",
        )

    assert writes == []


def test_restore_preserves_config_mode(tmp_path):
    installer = load_script("public_install_config_mode", "install-profile.py")
    home = tmp_path / "profile"
    backup = home / "state" / "public-setup-backups" / "receipt"
    (backup / "files").mkdir(parents=True)
    config_path = home / "config.yaml"
    config_path.write_text("after\n", encoding="utf-8")
    before = b"before\n"
    (backup / "config.yaml.before").write_bytes(before)

    installer.restore(
        [],
        backup,
        home,
        config_path,
        True,
        hashlib.sha256(before).hexdigest(),
        "0640",
    )

    assert config_path.read_bytes() == before
    assert config_path.stat().st_mode & 0o777 == 0o640


def test_profile_environment_binding_is_opaque_and_reversible(tmp_path):
    binder = load_script("public_profile_binding", "bind-service-circuit.py")
    home = tmp_path / "profile"
    home.mkdir()
    env_path = home / ".env"
    original = b"PROVIDER_SECRET=\xffprivate-value\n"
    env_path.write_bytes(original)
    env_path.chmod(0o640)

    original_owner = binder._path_owner(env_path)
    backup = binder._bind_environment(home)
    assert backup is not None
    assert env_path.read_bytes().startswith(original)
    assert env_path.stat().st_mode & 0o777 == 0o640
    assert binder._path_owner(env_path) == original_owner
    assert binder._bind_environment(home) is None
    receipt = (backup / "receipt.json").read_text(encoding="utf-8")
    assert "PROVIDER_SECRET" not in receipt
    assert binder.KEY not in receipt
    assert (backup / ".env.before").read_bytes() == original
    assert (backup / ".env.before").stat().st_mode & 0o777 == 0o600

    binder._restore_backup(home, backup)
    assert env_path.read_bytes() == original
    assert env_path.stat().st_mode & 0o777 == 0o640
    assert binder._path_owner(env_path) == original_owner


def test_profile_environment_binding_rejects_conflicts(tmp_path):
    binder = load_script("public_profile_conflict", "bind-service-circuit.py")
    home = tmp_path / "profile"
    home.mkdir()
    env_path = home / ".env"
    env_path.write_text(f"{binder.KEY}=wrong\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="conflicts"):
        binder._bind_environment(home)

    env_path.write_text(
        f"{binder.KEY}=one\nexport {binder.KEY}=two\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="duplicated"):
        binder._bind_environment(home)


def test_new_profile_environment_uses_profile_owner(tmp_path):
    binder = load_script("public_profile_owner", "bind-service-circuit.py")
    home = tmp_path / "profile"
    home.mkdir()
    expected_owner = binder._path_owner(home)

    backup = binder._bind_environment(home)

    assert backup is not None
    assert binder._path_owner(home / ".env") == expected_owner
    assert (home / ".env").stat().st_mode & 0o777 == 0o600
    binder._restore_backup(home, backup)
    assert not (home / ".env").exists()


def test_systemd_properties_reload_and_use_selected_scope():
    binder = load_script("public_systemd_properties", "bind-service-circuit.py")
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        output = ""
        if "show" in argv:
            output = (
                "ExecStart={ path=/candidate/venv/bin/python ; "
                "argv[]=/candidate/venv/bin/python -m hermes_cli.main "
                "gateway run ; }\n"
                "ExecStartEx={ path=/candidate/venv/bin/python ; "
                "argv[]=/candidate/venv/bin/python -m hermes_cli.main "
                "gateway run ; flags= ; }\n"
                "Environment=HERMES_HOME=/profile\n"
                "EnvironmentFiles=\n"
                "PassEnvironment=\n"
                "User=\n"
                "FragmentPath=/unit.service\n"
                "DropInPaths=\n"
            )
        return subprocess.CompletedProcess(argv, 0, output, "")

    properties = binder._systemd_properties(
        "hermes-gateway", "user", run=fake_run
    )
    manager_environment = binder._systemd_manager_environment(
        "user", run=fake_run
    )

    assert calls[0] == ["systemctl", "--user", "daemon-reload"]
    assert calls[1][:4] == [
        "systemctl",
        "--user",
        "show",
        "hermes-gateway",
    ]
    assert properties["DropInPaths"] == ""
    assert manager_environment == {"PYTHONPATH": ""}
    assert calls[2] == ["systemctl", "--user", "show-environment"]


def test_runtime_value_uses_unambiguous_sentinel(tmp_path):
    binder = load_script("public_runtime_sentinel", "bind-service-circuit.py")
    runtime = tmp_path / "candidate"
    python = runtime / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")

    def resolved_run(argv, **_kwargs):
        marker = re.search(r"_proof_marker='([^']+)'", argv[-1]).group(1)
        return subprocess.CompletedProcess(
            argv, 0, f"generator chatter\n{marker}\"resolved\"\n", ""
        )

    def ambiguous_run(argv, **_kwargs):
        marker = re.search(r"_proof_marker='([^']+)'", argv[-1]).group(1)
        return subprocess.CompletedProcess(
            argv, 0, f"{marker}\"one\"\n{marker}\"two\"\n", ""
        )

    assert (
        binder._runtime_value(
            runtime,
            tmp_path,
            "_proof_emit('resolved')",
            run=resolved_run,
            token="fixed",
        )
        == "resolved"
    )
    with pytest.raises(RuntimeError, match="resolution failed"):
        binder._runtime_value(
            runtime,
            tmp_path,
            "_proof_emit('one');_proof_emit('two')",
            run=ambiguous_run,
            token="fixed",
        )


def test_launchd_state_queries_the_proven_gui_domain():
    binder = load_script("public_launchd_domain", "bind-service-circuit.py")
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        output = "loaded job" if "print" in argv else "/candidate/imports\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    loaded, pythonpath = binder._loaded_launchd_state(
        "com.example.hermes",
        run=fake_run,
        uid=501,
        launchctl="/bin/launchctl",
    )

    assert loaded == "loaded job"
    assert pythonpath == "/candidate/imports"
    assert calls == [
        [
            "/bin/launchctl",
            "print",
            "gui/501/com.example.hermes",
        ],
        [
            "/bin/launchctl",
            "asuser",
            "501",
            "/bin/launchctl",
            "getenv",
            "PYTHONPATH",
        ],
    ]

    def inaccessible_domain(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            1 if "asuser" in argv else 0,
            "",
            "",
        )

    with pytest.raises(RuntimeError, match="domain environment"):
        binder._loaded_launchd_state(
            "com.example.hermes",
            run=inaccessible_domain,
            uid=501,
            launchctl="/bin/launchctl",
        )


def test_windows_uv_launch_spec_uses_base_pythonw(tmp_path):
    binder = load_script("public_windows_uv_spec", "bind-service-circuit.py")
    runtime = tmp_path / "candidate"
    venv = runtime / "venv"
    scripts = venv / "Scripts"
    site_packages = venv / "Lib" / "site-packages"
    base = tmp_path / "base-python"
    scripts.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    base.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (base / "pythonw.exe").write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(
        f"home = {base}\nuv = 0.8.0\n",
        encoding="utf-8",
    )

    executable, virtual_env, pythonpath = binder._windows_launch_spec(runtime)

    assert executable == str(base / "pythonw.exe")
    assert virtual_env == str(venv)
    assert pythonpath == (str(runtime), str(site_packages))


def test_native_service_proofs_require_exact_runtime_and_owner():
    binder = load_script("public_service_proof", "bind-service-circuit.py")
    home = Path("/profiles/agent")
    runtime = Path("/runtimes/candidate")
    owner = "agent-user"
    unit = (
        "[Service]\n"
        f"ExecStart={runtime}/venv/bin/python -m hermes_cli.main gateway run\n"
        f'Environment="VIRTUAL_ENV={runtime}/venv"\n'
        f'Environment="HERMES_HOME={home}"\n'
        f"User={owner}\n"
    ).encode()
    expected_argv = [
        str(runtime / "venv" / "bin" / "python"),
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
    ]
    expected_environment = {
        "HERMES_HOME": str(home),
        "VIRTUAL_ENV": str(runtime / "venv"),
    }
    binder._prove_systemd(
        unit,
        home,
        runtime,
        "system",
        owner,
        expected_argv,
        expected_environment,
    )
    for prefix in ("+", "!", "!!"):
        with pytest.raises(RuntimeError, match="execution flags"):
            binder._prove_systemd(
                unit.replace(
                    f"ExecStart={runtime}".encode(),
                    f"ExecStart={prefix}{runtime}".encode(),
                ),
                home,
                runtime,
                "system",
                owner,
                expected_argv,
                expected_environment,
            )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_systemd(
            unit.replace(b"/runtimes/candidate/", b"/runtimes/candidate-old/"),
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="proven owner"):
        binder._prove_systemd(
            unit,
            home,
            runtime,
            "system",
            "another-user",
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_systemd(
            unit.replace(b"gateway run", b"/old-runtime/gateway.py"),
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_systemd(
            unit.replace(
                f'Environment="HERMES_HOME={home}"'.encode(),
                (
                    f'Environment="HERMES_HOME={home}"\n'
                    'Environment="PYTHONPATH=/old-runtime"'
                ).encode(),
            ),
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    definition = Path("/etc/systemd/system/hermes-gateway.service")
    effective = {
        "ExecStart": (
            "{ path=/runtimes/candidate/venv/bin/python ; "
            "argv[]=/runtimes/candidate/venv/bin/python -m hermes_cli.main "
            "gateway run ; }"
        ),
        "ExecStartEx": (
            "{ path=/runtimes/candidate/venv/bin/python ; "
            "argv[]=/runtimes/candidate/venv/bin/python -m hermes_cli.main "
            "gateway run ; flags= ; }"
        ),
        "Environment": (
            f"HERMES_HOME={home} VIRTUAL_ENV={runtime}/venv "
            "PYTHONUNBUFFERED=1"
        ),
        "EnvironmentFiles": "",
        "PassEnvironment": "",
        "User": owner,
        "FragmentPath": str(definition),
        "DropInPaths": "",
    }
    binder._prove_effective_systemd(
        effective,
        definition,
        home,
        runtime,
        "system",
        owner,
        expected_argv,
        expected_environment,
    )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_effective_systemd(
            {
                **effective,
                "ExecStartEx": effective["ExecStartEx"].replace(
                    "flags= ;", "flags=privileged ;"
                ),
            },
            definition,
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_effective_systemd(
            {
                **effective,
                "ExecStartEx": effective["ExecStartEx"].replace(
                    "path=/runtimes/candidate/",
                    "path=/runtimes/candidate-old/",
                ),
            },
            definition,
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="drop-in"):
        binder._prove_effective_systemd(
            {**effective, "DropInPaths": "/etc/systemd/system/override.conf"},
            definition,
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_effective_systemd(
            {
                **effective,
                "ExecStart": effective["ExecStart"].replace(
                    "/runtimes/candidate/", "/runtimes/candidate-old/"
                ),
            },
            definition,
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_effective_systemd(
            {
                **effective,
                "ExecStart": effective["ExecStart"].replace(
                    "gateway run", "/old-runtime/gateway.py"
                ),
            },
            definition,
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    with pytest.raises(RuntimeError, match="environment sources"):
        binder._prove_effective_systemd(
            {**effective, "EnvironmentFiles": "/etc/hermes.env"},
            definition,
            home,
            runtime,
            "system",
            owner,
            expected_argv,
            expected_environment,
        )
    user_effective = {**effective, "User": ""}
    binder._prove_effective_systemd(
        user_effective,
        definition,
        home,
        runtime,
        "user",
        owner,
        expected_argv,
        expected_environment,
        current_user=owner,
        manager_environment={"PYTHONPATH": str(runtime / "imports")},
    )
    for inherited in ("/old-runtime", f":{runtime}", f"{runtime}:"):
        with pytest.raises(
            RuntimeError,
            match="manager PYTHONPATH",
        ):
            binder._prove_effective_systemd(
                user_effective,
                definition,
                home,
                runtime,
                "user",
                owner,
                expected_argv,
                expected_environment,
                current_user=owner,
                manager_environment={"PYTHONPATH": inherited},
            )

    launchd_argv = [*expected_argv, "--replace"]
    plist_payload = {
        "ProgramArguments": launchd_argv,
        "EnvironmentVariables": expected_environment,
    }
    plist = binder.plistlib.dumps(plist_payload)
    binder._prove_launchd(
        plist,
        home,
        runtime,
        owner,
        launchd_argv,
        expected_environment,
        current_user=owner,
    )
    with pytest.raises(RuntimeError, match="runtime owner"):
        binder._prove_launchd(
            plist.replace(b"/runtimes/candidate/", b"/runtimes/candidate-old/"),
            home,
            runtime,
            owner,
            launchd_argv,
            expected_environment,
            current_user=owner,
        )
    label = "com.example.hermes"
    loaded_launchd = (
        f"gui/501/{label} = {{\n"
        f"\tpath = /Users/agent/Library/LaunchAgents/{label}.plist\n"
        f"\tprogram = {launchd_argv[0]}\n"
        "\targuments = {\n"
        + "".join(f"\t\t{argument}\n" for argument in launchd_argv)
        + "\t}\n"
        "\tinherited environment = {\n"
        "\t}\n"
        "\tdefault environment = {\n"
        "\t\tPATH => /usr/bin:/bin\n"
        "\t}\n"
        "\tenvironment = {\n"
        f"\t\tHERMES_HOME => {home}\n"
        f"\t\tVIRTUAL_ENV => {runtime}/venv\n"
        "\t\tXPC_SERVICE_NAME => com.example.hermes\n"
        "\t}\n"
        "}\n"
    )
    launchd_definition = Path(
        f"/Users/agent/Library/LaunchAgents/{label}.plist"
    )
    binder._prove_loaded_launchd(
        loaded_launchd,
        str(runtime / "domain-imports"),
        launchd_definition,
        runtime,
        launchd_argv,
        expected_environment,
        label,
        uid=501,
    )
    with pytest.raises(RuntimeError, match="pinned launch spec"):
        binder._prove_loaded_launchd(
            loaded_launchd.replace("gateway\n", "/old-runtime/gateway.py\n"),
            "",
            launchd_definition,
            runtime,
            launchd_argv,
            expected_environment,
            label,
            uid=501,
        )
    with pytest.raises(RuntimeError, match="domain PYTHONPATH"):
        binder._prove_loaded_launchd(
            loaded_launchd,
            "/old-runtime",
            launchd_definition,
            runtime,
            launchd_argv,
            expected_environment,
            label,
            uid=501,
        )
    with pytest.raises(RuntimeError, match="runtime owner"):
        binder._prove_launchd(
            binder.plistlib.dumps(
                {
                    **plist_payload,
                    "ProgramArguments": [
                        *launchd_argv[:-2],
                        "/old-runtime/gateway.py",
                        "--replace",
                    ],
                }
            ),
            home,
            runtime,
            owner,
            launchd_argv,
            expected_environment,
            current_user=owner,
        )
    with pytest.raises(RuntimeError, match="runtime owner"):
        binder._prove_launchd(
            binder.plistlib.dumps(
                {
                    **plist_payload,
                    "EnvironmentVariables": {
                        **expected_environment,
                        "PYTHONPATH": "/old-runtime",
                    },
                }
            ),
            home,
            runtime,
            owner,
            launchd_argv,
            expected_environment,
            current_user=owner,
        )

    windows_home = Path(r"C:\Users\Agent\.hermes")
    windows_runtime = Path(r"C:\Hermes\candidate")
    venv = rf"{windows_runtime}\venv"
    site_packages = rf"{venv}\Lib\site-packages"
    python = r"C:\Python\pythonw.exe"
    static_pythonpath = f"{windows_runtime};{site_packages}"
    cmd = (
        "@echo off\r\n"
        f'set "HERMES_HOME={windows_home}"\r\n'
        f'set "VIRTUAL_ENV={venv}"\r\n'
        f'set "PYTHONPATH={static_pythonpath};%PYTHONPATH%"\r\n'
        f'"{python}" -m hermes_cli.main gateway run\r\n'
    ).encode()
    vbs = (
        f'env.Item("HERMES_HOME") = "{windows_home}"\r\n'
        f'env.Item("VIRTUAL_ENV") = "{venv}"\r\n'
        'existing_pp = env.Item("PYTHONPATH")\r\n'
        f'env.Item("PYTHONPATH") = "{static_pythonpath};" & existing_pp\r\n'
        f'env.Item("PYTHONPATH") = "{static_pythonpath}"\r\n'
        f'sh.Run """{python}"" -m hermes_cli.main gateway run", 0, False\r\n'
    ).encode()
    binder._prove_windows_launchers(
        cmd,
        vbs,
        windows_home,
        windows_runtime,
        python,
        venv,
        (str(windows_runtime), site_packages),
        {
            "process": "",
            "user": str(windows_runtime / "tools"),
            "system": site_packages,
        },
    )
    with pytest.raises(RuntimeError, match="profile|candidate|PYTHONPATH"):
        binder._prove_windows_launchers(
            cmd.replace(b"candidate\\", b"candidate-old\\"),
            vbs.replace(b"candidate\\", b"candidate-old\\"),
            windows_home,
            windows_runtime,
            python,
            venv,
            (str(windows_runtime), site_packages),
            {"process": "", "user": "", "system": ""},
        )
    with pytest.raises(RuntimeError, match="omits"):
        binder._prove_windows_launchers(
            cmd.replace(f";{site_packages}".encode(), b""),
            vbs.replace(f";{site_packages}".encode(), b""),
            windows_home,
            windows_runtime,
            python,
            venv,
            (str(windows_runtime), site_packages),
            {"process": "", "user": "", "system": ""},
        )
    with pytest.raises(RuntimeError, match="inherited Windows PYTHONPATH"):
        binder._prove_windows_launchers(
            cmd,
            vbs,
            windows_home,
            windows_runtime,
            python,
            venv,
            (str(windows_runtime), site_packages),
            {
                "process": "",
                "user": r"C:\old-runtime",
                "system": "",
            },
        )
    for inherited in (";", f";{windows_runtime}", f"{windows_runtime};"):
        with pytest.raises(
            RuntimeError,
            match="inherited Windows PYTHONPATH",
        ):
            binder._prove_windows_launchers(
                cmd,
                vbs,
                windows_home,
                windows_runtime,
                python,
                venv,
                (str(windows_runtime), site_packages),
                {"process": inherited, "user": "", "system": ""},
            )
    with pytest.raises(RuntimeError, match="VBS PYTHONPATH"):
        binder._prove_windows_launchers(
            cmd,
            vbs.replace(
                b'existing_pp = env.Item("PYTHONPATH")\r\n',
                b"",
            ),
            windows_home,
            windows_runtime,
            python,
            venv,
            (str(windows_runtime), site_packages),
            {"process": "", "user": "", "system": ""},
        )
    vbs_path = Path(r"C:\Users\Agent\.hermes\gateway-service\Hermes.vbs")
    task = (
        "<Task><Principals><Principal><UserId>DOMAIN\\Agent</UserId>"
        "</Principal></Principals><Actions><Exec><Command>wscript.exe</Command>"
        f'<Arguments>//B //Nologo "{vbs_path}"</Arguments>'
        "</Exec></Actions></Task>"
    )
    binder._task_proof(task, vbs_path, r"DOMAIN\Agent")
    with pytest.raises(RuntimeError, match="proven owner"):
        binder._task_proof(task, vbs_path, r"DOMAIN\Other")
    binder._prove_windows_operator(
        r"DOMAIN\Agent",
        process_sid="S-1-5-21-1000",
        owner_sid="s-1-5-21-1000",
    )
    with pytest.raises(RuntimeError, match="operator"):
        binder._prove_windows_operator(
            r"DOMAIN\Agent",
            process_sid="S-1-5-21-1000",
            owner_sid="S-1-5-21-2000",
        )
    assert (
        binder._literal_windows_registry_pythonpath(
            str(windows_runtime),
            1,
            1,
        )
        == str(windows_runtime)
    )
    for value, kind in (
        (r"%IMPORT_ROOT%\package", 1),
        (r"!IMPORT_ROOT!\package", 1),
        (str(windows_runtime), 2),
    ):
        with pytest.raises(RuntimeError, match="not literal"):
            binder._literal_windows_registry_pythonpath(value, kind, 1)


def test_prepare_home_rejects_reused_staging(tmp_path):
    assembler = load_script("public_unique_staging", "assemble-runtime.py")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / ".env").write_text("LIVE=1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unique empty"):
        assembler.prepare_posix_dependencies(tmp_path / "runtime", staging)


def test_windows_source_canonicalization_accepts_only_crlf():
    verifier = load_script("public_verify_crlf", "verify-release.py")
    canonical = b"alpha\nbeta\n"
    blob = verifier._git_blob_sha1(canonical)

    assert (
        verifier._canonical_source_data(b"alpha\r\nbeta\r\n", blob, True)
        == canonical
    )
    assert verifier._canonical_source_data(b"alpha\r\nchanged\r\n", blob, True) is None
    assert verifier._canonical_source_data(b"alpha\r\nbeta\r\n", blob, False) is None


def test_windows_runtime_identity_accepts_only_declared_crlf(tmp_path):
    runtime_manifest = load_script(
        "public_runtime_crlf", "runtime-payload-manifest.py"
    )
    path = tmp_path / "runtime.py"
    canonical = b"alpha\nbeta\n"
    declared = {
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "mode": "100755",
        "type": "blob",
    }
    path.write_bytes(b"alpha\r\nbeta\r\n")

    identity, reason = runtime_manifest._runtime_file_identity(
        path, declared, True
    )
    assert reason is None
    assert identity == declared

    path.write_bytes(b"alpha\r\nchanged\r\n")
    identity, reason = runtime_manifest._runtime_file_identity(
        path, declared, True
    )
    assert identity is None
    assert "content hash" in reason


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
        "ensure_cua_driver",
        lambda *_args, **_kwargs: {
            "ok": True,
            "after": {"version": "0.14.2"},
            "doctor_ready": False,
        },
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
    assert receipt["config_mode_before"] == "0644"
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


def test_gbrain_is_opt_in_and_telegram_continuity_stays_enabled(monkeypatch):
    monkeypatch.delenv("HERMES_ENABLE_GBRAIN_CAPTURE", raising=False)
    gbrain = load_script(
        "public_gbrain_capture", "../hooks/gbrain-capture/handler.py"
    )
    assert gbrain._capture_enabled() is False
    monkeypatch.setattr(
        gbrain,
        "_do_capture",
        lambda _context: pytest.fail("disabled GBrain hook executed"),
    )
    asyncio.run(gbrain.handle("agent:end", {"platform": "telegram"}))
    transcript_source = (
        ROOT / "hooks" / "telegram-transcript" / "handler.py"
    ).read_text(encoding="utf-8")
    transcript_hook = yaml.safe_load(
        (ROOT / "hooks" / "telegram-transcript" / "HOOK.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert "HERMES_ENABLE_TELEGRAM_TRANSCRIPT" not in transcript_source
    assert transcript_hook["name"] == "telegram-transcript"
    assert set(transcript_hook["events"]) == {
        "agent:start",
        "agent:end",
        "processing:complete",
    }


def test_windows_installer_is_pinned_and_paths_are_split():
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert (
        "raw.githubusercontent.com/NousResearch/hermes-agent/"
        "3c27eb6234bf91b8ceee9e9071591b31e9b148cb/scripts/install.ps1"
        in instructions
    )
    assert (
        "4dcbf2b665750cb578f69a6efa40770659e21821a463746f86da68af0d2bb31c"
        in instructions
    )
    assert "-m hermes_cli.main setup" in instructions
    assert "gateway install" in instructions
    assert "gateway status" in instructions
    assert '$InstallMode = "<fresh-or-existing>"' in instructions
    assert "$ProvenHermesHome" in instructions
    assert "$ProvenServiceOwner" in instructions
    assert "$ExistingInstall" not in instructions
    assert (
        '& "$Candidate\\venv\\Scripts\\python.exe" .\\bin\\assemble-runtime.py'
        in instructions
    )
    assert "core.autocrlf=false" in instructions
    assert instructions.count("HERMES_CODEX_401_CIRCUIT_STATE") >= 4
    assert "bind-service-circuit.py" in instructions
    assert '[Guid]::NewGuid().ToString("N")' in instructions
    assert "--prove-kind windows" in instructions
    assert "--service-owner $ProvenServiceOwner" in instructions
    assert "$ProfileHome = $StagingHome" in instructions
    assert "profile-environment-backups" not in instructions
    assert "gateway install --no-start-now" in instructions


def test_documented_pinned_installer_scaffold_is_not_reinitialized():
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert instructions.count("--initialize-staging") == 2
    assert (
        "`--initialize-staging`\n"
        "is only for a genuinely empty staging home"
    ) in instructions
    assert (
        "Do not\n"
        "pass `--initialize-staging`"
    ) in instructions


def test_public_text_has_no_private_runtime_routes():
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
    posix_runtime_route = re.compile(
        r"/(?:Users|home)/[^/\s]+/(?:\.hermes|hermes-agent)"
    )
    windows_runtime_route = re.compile(
        r"C:\\+Users\\+([^\\\s]+)\\+\.hermes", re.IGNORECASE
    )
    numeric_identity_fixture = re.compile(
        r"(?:chat_id|user_id)=[\"']\d{6,}"
    )
    for relative in sorted(paths):
        text = (ROOT / relative).read_text(encoding="utf-8", errors="ignore")
        assert posix_runtime_route.search(text) is None, (
            f"{relative} contains a private POSIX runtime route"
        )
        for match in windows_runtime_route.finditer(text):
            assert match.group(1) == "Agent", (
                f"{relative} contains a non-neutral Windows runtime route"
            )
        if relative != "patches/modules/telegram_dm_topic_recovery_root_guard_v1.py":
            assert numeric_identity_fixture.search(text) is None, (
                f"{relative} contains a numeric identity fixture"
            )

    papercuts = (ROOT / "skills" / "fleet" / "papercuts" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    operating_patterns = json.loads(
        (
            ROOT
            / "mcp-servers"
            / "capability-router"
            / "operating-patterns.capability-entry.json"
        ).read_text(encoding="utf-8")
    )
    reflection = (
        ROOT / "skills" / "fleet" / "nightly-client-reflection-default" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "--route windows-host" in papercuts
    assert "--target example-agent" in papercuts
    assert "C:\\\\Users\\\\Agent\\\\.hermes" in papercuts
    durable_work = next(
        item
        for item in operating_patterns["capabilities"]
        if item["id"] == "ops-pattern.durable-work"
    )
    coding_worktree = next(
        item
        for item in operating_patterns["capabilities"]
        if item["id"] == "ops-pattern.coding-worktree"
    )
    assert durable_work["label"] == "Durable Work Queue"
    assert "the operator explicitly authorizes" in coding_worktree[
        "routing_policy"
    ]["dirty_repo_rule"]
    assert "escalate_to_doc" in reflection
    assert "preserve approval gates" in reflection
