from __future__ import annotations

import asyncio
import hashlib
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
    assert release["assembled_runtime_fingerprint"] == {
        "digest": "de5542cfd444b76b56c7b63d77cc2698d68d276d7f53c07ef188117e75b68067",
        "file_count": 32,
    }
    assert manifest["assembled_runtime_fingerprint"]["digest"] == (
        release["assembled_runtime_fingerprint"]["digest"]
    )
    assert manifest["assembled_runtime_fingerprint"]["file_count"] == 32
    assert len(manifest["assembled_runtime_fingerprint"]["files"]) == 32
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
        "assembled_runtime_fingerprint",
        "verification",
        "update_contract",
    }
    assert set(release["verification"]) == {
        "golden_suite",
        "clean_upstream_rehearsal",
    }

    blobs = {
        entry["path"]: entry["blob"]
        for component in manifest["components"].values()
        for entry in component["files"]
    }
    assert (
        blobs["patches/modules/codex_401_paid_fallback_circuit_v1.py"]
        == "6eea4c3c69f1177e8173e37e2e920877f7fc82f5"
    )
    assert (
        blobs["patches/modules/telegram_dm_topic_recovery_root_guard_v1.py"]
        == "9b3c05096964e4d27c8b126a05760a4ecb35fb56"
    )


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


def test_service_circuit_bindings_match_native_definitions(tmp_path):
    binder = load_script("public_service_binding", "bind-service-circuit.py")
    home = tmp_path / "profile"
    runtime = home / "state" / "runtime-candidates" / "release"
    runtime.mkdir(parents=True)
    circuit = home / "state" / "codex-401-circuit.json"

    unit = (
        "[Service]\n"
        f"ExecStart={runtime}/venv/bin/python -m hermes_cli.main gateway run\n"
        f'Environment="HERMES_HOME={home}"\n'
    ).encode()
    dropin = binder._systemd_binding(unit, home, runtime).decode()
    assert f"HERMES_CODEX_401_CIRCUIT_STATE={circuit}" in dropin

    plist = binder.plistlib.dumps(
        {
            "Label": "ai.hermes.gateway",
            "ProgramArguments": [
                str(runtime / "venv" / "bin" / "python"),
                "-m",
                "hermes_cli.main",
                "gateway",
                "run",
            ],
            "EnvironmentVariables": {"HERMES_HOME": str(home)},
        }
    )
    bound_plist = binder.plistlib.loads(
        binder._launchd_binding(plist, home, runtime)
    )
    assert bound_plist["EnvironmentVariables"][
        "HERMES_CODEX_401_CIRCUIT_STATE"
    ] == str(circuit)

    cmd = (
        "@echo off\r\n"
        f'cd /d "{runtime}"\r\n'
        f'set "HERMES_HOME={home}"\r\n'
    ).encode()
    vbs = (
        f'env.Item("HERMES_HOME") = "{home}"\r\n'
        f'sh.CurrentDirectory = "{runtime}"\r\n'
    ).encode()
    bound_cmd, bound_vbs = binder._windows_launcher_bindings(
        cmd, vbs, home, runtime
    )
    assert str(circuit).encode() in bound_cmd
    assert str(circuit).encode() in bound_vbs
    task_xml = (
        "<Task><Actions><Exec><Command>wscript.exe</Command>"
        f'<Arguments>//B //Nologo "{home / "gateway-service" / "Hermes.vbs"}"'
        "</Arguments></Exec></Actions></Task>"
    )
    assert binder._task_targets_vbs(
        task_xml, home / "gateway-service" / "Hermes.vbs"
    )
    assert not binder._task_targets_vbs(
        task_xml.replace("Hermes.vbs\"", "Hermes.vbs.evil\""),
        home / "gateway-service" / "Hermes.vbs",
    )


def test_service_binding_backup_is_reversible(tmp_path):
    binder = load_script(
        "public_service_binding_rollback", "bind-service-circuit.py"
    )
    home = tmp_path / "profile"
    home.mkdir()
    service_dir = home / "gateway-service"
    definition = service_dir / "Hermes.cmd"
    companion = service_dir / "Hermes.vbs"
    service_dir.mkdir()
    definition.write_bytes(b"before\n")
    companion.write_bytes(b"before-vbs\n")

    backup = binder._apply_binding(
        home,
        "windows",
        {
            definition: (b"after\n", 0o640),
            companion: (b"after-vbs\n", 0o640),
        },
    )

    assert definition.read_bytes() == b"after\n"
    assert binder._restore_backup(home, backup) == 2
    assert definition.read_bytes() == b"before\n"
    assert companion.read_bytes() == b"before-vbs\n"

    pending_backup = binder._apply_binding(
        home,
        "windows",
        {
            definition: (b"after\n", 0o640),
            companion: (b"after-vbs\n", 0o640),
        },
    )
    receipt_path = pending_backup / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["status"] = "pending"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    definition.write_bytes(b"before\n")
    definition.chmod(0o644)

    assert binder._restore_backup(home, pending_backup) == 2
    assert definition.read_bytes() == b"before\n"
    assert companion.read_bytes() == b"before-vbs\n"


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
    transcript_hook = (
        ROOT / "hooks" / "telegram-transcript" / "HOOK.yaml"
    ).read_text(encoding="utf-8")
    assert "HERMES_ENABLE_TELEGRAM_TRANSCRIPT" not in transcript_source
    assert "Bounded topic-local Telegram continuity" in transcript_hook


def test_windows_installer_is_pinned_and_paths_are_split():
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert (
        "raw.githubusercontent.com/NousResearch/hermes-agent/"
        "3ef6bbd201263d354fd83ec55b3c306ded2eb72a/scripts/install.ps1"
        in instructions
    )
    assert (
        "b5bdf0e959677de0168f8cfb5f9175c7b57adf5c4319a1c2fc9bec1f46fbdb6e"
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
    assert instructions.count("--initialize-staging") >= 2
    assert "bind-service-circuit.py" in instructions
    assert "gateway install --no-start-now" in instructions


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
    operating_patterns = (
        ROOT
        / "mcp-servers"
        / "capability-router"
        / "operating-patterns.capability-entry.json"
    ).read_text(encoding="utf-8")
    reflection = (
        ROOT / "skills" / "fleet" / "nightly-client-reflection-default" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "--route windows-host" in papercuts
    assert "--target example-agent" in papercuts
    assert "C:\\\\Users\\\\Agent\\\\.hermes" in papercuts
    assert "Durable Jobs" in operating_patterns
    assert "the operator explicitly authorizes" in operating_patterns
    assert "escalate_to_operator" in reflection
