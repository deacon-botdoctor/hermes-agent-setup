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
    assert (
        release["baseline_wiring_digest"]
        == manifest["components"]["baseline_wiring"]["digest"]
    )
    assert (
        release["runtime_payload_digest"]
        == manifest["components"]["runtime_payload"]["digest"]
    )
    assert manifest["components"]["runtime_payload"]["file_count"] == 820
    assert manifest["components"]["baseline_wiring"]["file_count"] == 34
    assert set(manifest["components"]) == {"baseline_wiring", "runtime_payload"}
    assert release["source_scope"] == "sanitized_deployable_components"
    assert release["assembled_runtime_fingerprint"] == {
        "digest": "cda397cbce480c3106428871bbb68f088ccf14434317426137ab336422f9071f",
        "file_count": 125,
    }
    assert manifest["runtime_fingerprint"]["digest"] == (
        release["assembled_runtime_fingerprint"]["digest"]
    )
    assert manifest["runtime_fingerprint"]["file_count"] == 125
    assert manifest["runtime_fingerprint"]["golden_sha"] == release["golden_sha"]
    assert (
        manifest["runtime_fingerprint"]["upstream_sha"]
        == release["canonical_upstream_sha"]
    )
    assert (
        manifest["runtime_fingerprint"]["expected_upstream_sha"]
        == release["canonical_upstream_sha"]
    )
    assert len(manifest["runtime_fingerprint"]["files"]) == 125
    assert set(release) == {
        "schema_version",
        "release",
        "status",
        "source_manifest",
        "golden_sha",
        "canonical_upstream_sha",
        "source_scope",
        "baseline_wiring_digest",
        "runtime_payload_digest",
        "deployment_digest",
        "assembled_runtime_fingerprint",
        "verification",
        "cua_driver",
        "runtime_coherence",
        "native_agent_continuity",
        "update_contract",
    }
    assert set(release["verification"]) == {
        "golden_suite",
        "clean_upstream_rehearsal",
    }


def test_host_health_and_cron_self_repair_are_release_owned():
    manifest = json.loads(
        (ROOT / "runtime-payload-source-manifest.json").read_text(encoding="utf-8")
    )
    owned_paths = {
        entry["path"]
        for component in manifest["components"].values()
        for entry in component["files"]
    }
    assert {
        "bin/hermes-local-selfcheck.py",
        "bin/hermes-canary-reconciler.py",
        "bin/hermes-disk-retention.py",
        "bin/tool-readiness-probe.py",
        "patches/modules/cron_operator_delivery_v1.py",
        "shared-rules/host-health.md",
    } <= owned_paths

    installers = yaml.safe_load((ROOT / "installers.yaml").read_text(encoding="utf-8"))
    health_installer = next(
        row
        for row in installers["installers"]
        if row["name"] == "install_runtime_health_tools"
    )
    assert {
        "bin/hermes-local-selfcheck.py",
        "bin/hermes-canary-reconciler.py",
        "bin/hermes-disk-retention.py",
        "bin/tool-readiness-probe.py",
    } <= set(health_installer["sources"])

    host_rule = (ROOT / "shared-rules" / "host-health.md").read_text(
        encoding="utf-8"
    )
    assert "never automatically blocks a requested job" in host_rule
    assert "Allocated swap and active swap churn are distinct signals" in host_rule
    assert "Never reboot the host or stop unknown" in host_rule

    cron_patch = (
        ROOT / "patches" / "modules" / "cron_operator_delivery_v1.py"
    ).read_text(encoding="utf-8")
    assert 'SELF_REMEDIATION_MARKER = "HERMES_CRON_SELF_REMEDIATION_v1"' in cron_patch
    assert '"_cron_repair_attempt": True' in cron_patch
    assert "Do not blindly rerun external side effects" in cron_patch
    assert "Never tell the operator to inspect logs" in cron_patch
    assert "automatic repair stopped:" in cron_patch

    registry = yaml.safe_load((ROOT / "patches" / "registry.yaml").read_text())
    entry = next(row for row in registry["patches"] if row["name"] == "cron_operator_delivery_v1")
    assert entry["module"] == "modules/cron_operator_delivery_v1.py"
    assert entry["function"] == "patch_cron_operator_delivery_v1"


def test_verifier_rejects_nested_fingerprint_golden_mismatch(monkeypatch):
    verifier = load_script("public_verify_nested_golden", "verify-release.py")
    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "runtime-payload-source-manifest.json").read_text(encoding="utf-8")
    )
    manifest["runtime_fingerprint"]["golden_sha"] = "0" * 40
    read_json = verifier._read_json

    def fake_read_json(path):
        if path == verifier.RELEASE_PATH:
            return release
        if path == verifier.SOURCE_MANIFEST_PATH:
            return manifest
        return read_json(path)

    monkeypatch.setattr(verifier, "_read_json", fake_read_json)

    _, errors = verifier.verify_public_source()

    assert "source manifest assembled runtime Golden SHA mismatch" in errors


def test_release_pins_the_golden_cua_driver_contract():
    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    driver = release["cua_driver"]
    helper = ROOT / driver["helper"]["path"]
    contract_path = ROOT / driver["contract"]["path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert driver["version"] == "0.22.0"
    assert driver["tag"] == "cua-driver-rs-v0.22.0"
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


@pytest.mark.parametrize("outcome", ["success", "import_failure", "timeout", "spawn_failure"])
def test_runtime_coherence_import_probe_isolated_and_cleaned(tmp_path, monkeypatch, outcome):
    checker = load_path("public_coherence_isolation", ROOT / "checks/agent-runtime-coherence.py")
    home = tmp_path / "live"
    home.mkdir()
    protected = [home / name for name in ("state.db", "state.db-wal", "state.db-shm")]
    for path in protected:
        path.write_bytes(("original-" + path.name).encode())
    before = [(p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in protected]
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    python = runtime / "python"
    python.write_text("fixture")
    monkeypatch.setattr(checker, "validate_runtime_binding", lambda **kwargs: {"ok": True})
    probe_homes = []

    def spawn(command, **kwargs):
        env = kwargs["env"]
        probe = Path(env["HERMES_HOME"])
        probe_homes.append(probe)
        assert probe.is_dir() and probe != home
        assert Path(env["HOME"]) != tmp_path / "operator"
        assert Path(env["USERPROFILE"]) == Path(env["HOME"])
        assert list(probe.iterdir()) == []
        assert command[0] == str(python) and kwargs["cwd"] == runtime
        for name in ("state.db", "state.db-wal", "state.db-shm"):
            (probe / name).write_text("disposable probe state")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 45)
        if outcome == "spawn_failure":
            raise OSError("fixture spawn failure")
        return SimpleNamespace(returncode=0 if outcome == "success" else 1,
            stdout='HERMES_RUNTIME_COHERENCE={"origins":{}}\n', stderr="")

    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "operator"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(checker.subprocess, "run", spawn)
    result = checker.run_probe(runtime_root=runtime, runtime_python=python, hermes_home=home, agent_id="fixture")
    assert result["kind"] == {"success": "coherent"}.get(outcome, outcome)
    assert result["hermes_home"] == str(home)
    assert len(probe_homes) == 1 and not probe_homes[0].parent.exists()
    assert before == [(p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in protected]
    receipt = home / "state/health/runtime-coherence.json"
    checker.atomic_write(receipt, result)
    assert json.loads(receipt.read_text())["hermes_home"] == str(home)


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
        == "e5cbbcef89aa9635971d77ba9977535cd23aabc6"
    )
    assert (
        blobs["patches/modules/telegram_dm_topic_recovery_root_guard_v1.py"]
        == "e91c52c8bb8525dbba25d932d51bcca422ad3147"
    )


def test_registry_has_only_explained_retirable_patches():
    registry = yaml.safe_load(
        (ROOT / "patches" / "registry.yaml").read_text(encoding="utf-8")
    )
    patches = registry["patches"]
    assert len(patches) == 49
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
    assert "bin/native-agent-continuity.py" in destinations
    assert "bin/native-session-runner.py" in destinations
    assert "bin/client-selfheal-heartbeat.sh" in destinations
    assert "bin/client-selfheal-heartbeat.ps1" in destinations
    assert "config/native-agent-continuity-v1.json" in destinations
    assert not any(path.startswith("patches/") for path in destinations)
    assert not any(path.startswith("shared-defaults/") for path in destinations)
    assert not any(path.startswith("kit/systemd/") for path in destinations)


def test_native_agent_continuity_overlay_is_exact_and_manifest_driven():
    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    contract = json.loads(
        (ROOT / "contracts/native-agent-continuity-release-v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert contract["source_commit"] == (
        "3408573b4ca02b1fd45bd969ff87fea15c0d065f"
    )
    assert contract["activation"] == "manifest_driven_existing_selfheal"
    assert contract["platforms"] == ["linux", "macos", "windows"]
    assert len(contract["files"]) == 10
    assert release["native_agent_continuity"]["package_digest"] == contract[
        "package_digest"
    ]
    assert any(
        row["path"].endswith("native-session-luna-carder.py")
        for row in contract["files"]
    )
    assert (
        json.loads(
            (ROOT / "native-continuity/config/native-agent-continuity-v1.json").read_text()
        )["boundaries"]["daily_card_limit"]
        is None
    )


def test_verifier_rejects_native_continuity_package_drift(monkeypatch):
    verifier = load_script("public_verify_native_continuity_drift", "verify-release.py")
    release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    errors = []
    monkeypatch.setattr(
        verifier,
        "NATIVE_CONTINUITY_CONTRACT_PATH",
        ROOT / "native-continuity/config/native-agent-continuity-v1.json",
    )

    verifier.verify_native_agent_continuity_contract(release, errors)

    assert errors == ["release native_agent_continuity contract digest mismatch"]


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
    assert config["approvals"]["mode"] == "off"
    assert "approvals.mode" in changed


@pytest.mark.parametrize("explicit_mode", ["manual", "smart", "off"])
def test_profile_permission_default_preserves_explicit_client_mode(
    tmp_path, explicit_mode
):
    installer = load_script(
        f"public_install_permissions_{explicit_mode}", "install-profile.py"
    )
    config_path = tmp_path / "config.yaml"
    python = tmp_path / "runtime" / "venv" / "bin" / "python"
    config_path.write_text(
        yaml.safe_dump(
            {
                "approvals": {
                    "mode": explicit_mode,
                    "deny": ["git push --force*"],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    changed = installer.ensure_public_config(config_path, tmp_path, python)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["approvals"] == {
        "mode": explicit_mode,
        "deny": ["git push --force*"],
    }
    assert "approvals.mode" not in changed


def test_profile_permission_default_rejects_non_mapping_approvals(tmp_path):
    installer = load_script("public_install_permissions_invalid", "install-profile.py")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("approvals: manual\n", encoding="utf-8")

    with pytest.raises(ValueError, match="config approvals must be a mapping"):
        installer.ensure_public_config(
            config_path,
            tmp_path,
            tmp_path / "runtime" / "venv" / "bin" / "python",
        )


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
                    "after": {"installed": True, "version": "0.22.0"},
                    "doctor_ready": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    receipt = installer.ensure_cua_driver(runtime_python, home)

    assert receipt["after"]["version"] == "0.22.0"
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
                    "after": {"installed": True, "version": "0.22.0"},
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


def test_prepare_home_forces_the_existing_candidate_back_to_the_release_pin(
    tmp_path, monkeypatch
):
    assembler = load_script("public_pinned_staging", "assemble-runtime.py")
    runtime = tmp_path / "runtime"
    staging = tmp_path / "staging"
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "scripts" / "install.sh").write_text(
        "#!/bin/bash\n", encoding="utf-8"
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        (runtime / ".hermes-bootstrap-complete").write_text(
            "installer-state\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(assembler, "run", fake_run)
    monkeypatch.setattr(assembler, "candidate_python_proof", lambda *args: {"sqlite_version": [3, 51, 3]})
    monkeypatch.setenv("UV_PYTHON", "/unsafe/python")
    monkeypatch.setenv("UV_PYTHON_PREFERENCE", "system")
    monkeypatch.setenv("PYTHONPATH", "/unsafe/modules")
    monkeypatch.setenv("VIRTUAL_ENV", "/unsafe/venv")

    proof = assembler.prepare_posix_dependencies(runtime, staging)
    assert proof["sqlite_version"] == [3, 51, 3]
    if assembler.sys.platform == "darwin" and assembler.platform.machine() == "arm64":
        assert proof["download_catalog"]["sha256"] == assembler.MACOS_PYTHON_CATALOG_SHA256
        assert calls[0][1]["env"]["UV_PYTHON_DOWNLOADS_JSON_URL"] == (ROOT / assembler.MACOS_PYTHON_CATALOG).as_uri()

    command, kwargs = calls[0]
    assert command == [
        "bash",
        str(runtime / "scripts" / "install.sh"),
        "--skip-setup",
        "--skip-browser",
        "--dir",
        str(runtime),
        "--hermes-home",
        str(staging),
        "--commit",
        assembler.RELEASE["canonical_upstream_sha"],
        "--force-commit",
    ]
    assert kwargs["env"]["HOME"] == str(staging / ".installer-user")
    assert kwargs["env"]["HERMES_HOME"] == str(staging)
    assert kwargs["env"]["UV_MANAGED_PYTHON"] == "1"
    assert "UV_PYTHON_PREFERENCE" not in kwargs["env"]
    assert kwargs["env"]["UV_PYTHON_INSTALL_DIR"] == str(runtime / ".hermes-runtime" / "python")
    assert not {"UV_PYTHON", "PYTHONPATH", "VIRTUAL_ENV"} & kwargs["env"].keys()
    # Exercise uv's real option parser and interpreter discovery without downloads.
    uv = shutil.which("uv")
    if uv:
        discovery_env = dict(kwargs["env"], UV_PYTHON_DOWNLOADS="never", UV_NO_CONFIG="1")
        discovered = subprocess.run([uv, "python", "find", "3.11"], env=discovery_env,
                                    capture_output=True, text=True, check=False)
        assert discovered.returncode != 0
        assert not discovered.stdout.strip()
        assert "No interpreter found" in discovered.stderr

    assert (
        runtime / ".hermes-bootstrap-complete"
    ).read_text(encoding="utf-8") == "installer-state\n"
    runtime_manifest = load_script(
        "public_bootstrap_installer_state", "runtime-payload-manifest.py"
    )
    assert runtime_manifest.is_runtime_state_or_artifact(
        ".hermes-bootstrap-complete"
    )


def test_assembler_restores_promisor_metadata_for_partial_local_source(
    tmp_path, monkeypatch
):
    assembler = load_script("public_partial_clone", "assemble-runtime.py")
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    writes = []

    def fake_subprocess_run(argv, **_kwargs):
        key = argv[-1]
        if key == "remote.origin.promisor":
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if key == "remote.origin.partialclonefilter":
            return SimpleNamespace(returncode=0, stdout="blob:none\n", stderr="")
        raise AssertionError(argv)

    def fake_run(argv, **_kwargs):
        if argv[-3:] == ["remote", "get-url", "origin"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/NousResearch/hermes-agent.git\n",
                stderr="",
            )
        writes.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(assembler.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(assembler, "run", fake_run)

    assembler.restore_partial_clone_metadata(output, source)

    assert [argv[3:] for argv in writes] == [
        [
            "remote",
            "set-url",
            "origin",
            "https://github.com/NousResearch/hermes-agent.git",
        ],
        ["config", "remote.origin.promisor", "true"],
        ["config", "remote.origin.partialclonefilter", "blob:none"],
        ["config", "extensions.partialClone", "origin"],
    ]


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


def test_runtime_manifest_cli_uses_declared_public_files(tmp_path, monkeypatch, capsys):
    module = load_script("public_manifest_declared", "runtime-payload-manifest.py")
    manifest = json.loads((ROOT / "runtime-payload-source-manifest.json").read_text())
    declared = manifest["runtime_fingerprint"]["files"]
    def fingerprint(runtime, upstream, golden, expected_files):
        assert runtime == tmp_path.resolve()
        assert expected_files == declared
        return {"verified": True}
    monkeypatch.setattr(module, "runtime_fingerprint", fingerprint)
    assert module.main(["--source-manifest", str(ROOT / "runtime-payload-source-manifest.json"),
                        "--runtime-dir", str(tmp_path), "--compact"]) == 0
    assert json.loads(capsys.readouterr().out)["runtime_fingerprint"]["verified"]


def test_runtime_manifest_excludes_only_candidate_private_python_store():
    module = load_script("public_manifest_managed_python", "runtime-payload-manifest.py")
    assert module.is_runtime_state_or_artifact(".hermes-runtime/python/cpython/lib/libpython.dylib")
    assert not module.is_runtime_state_or_artifact(".hermes-runtime/injected.py")
    assert not module.is_runtime_state_or_artifact("gateway/.hermes-runtime/python/injected.py")


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
            "after": {"version": "0.22.0"},
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
        "9da6d455c9e1f2bf74bb9f47766ee9fc52e17bfb/scripts/install.ps1"
        in instructions
    )
    assert (
        "522941b9d678898392d31fc239cc229f6852a0f1bac8f266f7b81f8991f239d1"
        in instructions
    )
    assert "-m hermes_cli.main setup" in instructions
    assert "gateway install" in instructions
    assert "gateway status" in instructions
    assert instructions.count("hermes-local-selfcheck.py") >= 3
    assert "manifest-required capability/canary check passes" in instructions
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
        r"/(?:Users|home)/([^/\s]+)/(?:\.hermes|hermes-agent)"
    )
    windows_runtime_route = re.compile(
        r"C:\\+Users\\+([^\\\s]+)\\+\.hermes", re.IGNORECASE
    )
    numeric_identity_fixture = re.compile(
        r"(?:chat_id|user_id)=[\"']\d{6,}"
    )
    for relative in sorted(paths):
        text = (ROOT / relative).read_text(encoding="utf-8", errors="ignore")
        for match in posix_runtime_route.finditer(text):
            assert match.group(1) in {"Agent", "hermes-test"}, (
                f"{relative} contains a non-neutral POSIX runtime route"
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


@pytest.mark.parametrize("version, accepted", [([3, 50, 4], False), ([3, 50, 7], False), ([3, 51, 3], True), ([3, 53, 1], True), ([3, "51", 3], False)])
def test_candidate_python_sqlite_floor_and_isolation(tmp_path, monkeypatch, version, accepted):
    assembler = load_script("public_python_floor", "assemble-runtime.py")
    runtime = tmp_path / "candidate"
    base = runtime / ".hermes-runtime" / "python" / "cpython"
    base.mkdir(parents=True)
    target = base / "python"
    target.write_text("fixture interpreter")
    python = runtime / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(target)
    probe_homes = []

    def fake_run(argv, **kwargs):
        assert argv[:3] == [str(python), "-I", "-c"]
        home = Path(kwargs["env"]["HERMES_HOME"])
        assert home.is_dir() and home != tmp_path / "live"
        assert not list(home.iterdir())
        probe_homes.append(home)
        return SimpleNamespace(stdout=json.dumps({"python": str(python), "prefix": str(runtime / "venv"), "base_prefix": str(base), "python_version": [3, 11, 15], "sqlite_version": version}))

    monkeypatch.setattr(assembler, "run", fake_run)
    if accepted:
        assert assembler.candidate_python_proof(runtime, {"HERMES_HOME": str(tmp_path / "live")})["sqlite_version"] == version
    else:
        with pytest.raises(RuntimeError):
            assembler.candidate_python_proof(runtime, {})
    assert probe_homes and all(not path.exists() for path in probe_homes)


def test_candidate_python_rejects_external_seed(tmp_path, monkeypatch):
    assembler = load_script("public_python_external", "assemble-runtime.py")
    runtime = tmp_path / "candidate"
    python = runtime / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    monkeypatch.setattr(assembler, "run", lambda *args, **kwargs: pytest.fail("must reject before execution"))
    with pytest.raises(RuntimeError, match="private managed store"):
        assembler.candidate_python_proof(runtime, {})


def test_candidate_python_cleans_probe_after_execution_failure(tmp_path, monkeypatch):
    assembler = load_script("public_python_failure", "assemble-runtime.py")
    runtime = tmp_path / "candidate"
    python = runtime / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    target = runtime / ".hermes-runtime" / "python" / "python"
    target.parent.mkdir(parents=True)
    target.write_text("fixture")
    python.symlink_to(target)
    homes = []
    def fail(argv, **kwargs):
        homes.append(Path(kwargs["env"]["HERMES_HOME"]))
        raise RuntimeError("failed interpreter")
    monkeypatch.setattr(assembler, "run", fail)
    with pytest.raises(RuntimeError, match="failed interpreter"):
        assembler.candidate_python_proof(runtime, {})
    assert homes and not homes[0].exists()


@pytest.mark.parametrize("system,arch,expected", [("darwin", "arm64", True), ("darwin", "x86_64", False), ("linux", "aarch64", False)])
def test_candidate_python_catalog_is_platform_scoped(tmp_path, monkeypatch, system, arch, expected):
    assembler = load_script("public_python_catalog_scope", "assemble-runtime.py")
    monkeypatch.setattr(assembler.sys, "platform", system)
    monkeypatch.setattr(assembler.platform, "machine", lambda: arch)
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS_JSON_URL", "https://untrusted.invalid/catalog")
    calls = []
    monkeypatch.setattr(assembler, "run", lambda argv, **kw: calls.append(kw))
    monkeypatch.setattr(assembler, "candidate_python_proof", lambda *args: {"sqlite_version": [3, 53, 1]})
    proof = assembler.prepare_posix_dependencies(tmp_path / "candidate", tmp_path / "staging")
    assert ("download_catalog" in proof) is expected
    assert ("UV_PYTHON_DOWNLOADS_JSON_URL" in calls[0]["env"]) is expected
    if expected:
        catalog = ROOT / assembler.MACOS_PYTHON_CATALOG
        assert hashlib.sha256(catalog.read_bytes()).hexdigest() == proof["download_catalog"]["sha256"]
        assert calls[0]["env"]["UV_PYTHON_DOWNLOADS_JSON_URL"] == catalog.as_uri()


def test_candidate_python_catalog_tamper_blocks_installer(tmp_path, monkeypatch):
    assembler = load_script("public_python_catalog_tamper", "assemble-runtime.py")
    monkeypatch.setattr(assembler.sys, "platform", "darwin")
    monkeypatch.setattr(assembler.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(assembler, "ROOT", tmp_path)
    catalog = tmp_path / assembler.MACOS_PYTHON_CATALOG
    catalog.parent.mkdir()
    catalog.write_text("{}")
    monkeypatch.setattr(assembler, "run", lambda *args, **kw: pytest.fail("must not install with changed catalog"))
    with pytest.raises(RuntimeError, match="catalog digest mismatch"):
        assembler.prepare_posix_dependencies(tmp_path / "candidate", tmp_path / "staging")
