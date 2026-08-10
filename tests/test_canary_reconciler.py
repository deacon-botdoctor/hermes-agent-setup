from __future__ import annotations

import argparse
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_reconciler(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "bin" / "hermes-canary-reconciler.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_profile(module, tmp_path: Path) -> tuple[Path, dict[str, object]]:
    module.HOME = tmp_path / "home"
    module.HERMES = module.HOME / ".hermes"
    module.STATE_DIR = module.HERMES / "state"
    module.LOG_DIR = module.HERMES / "logs"
    module.CAP_PATH = module.STATE_DIR / "runtime-capabilities.json"
    module.LATEST_PATH = module.STATE_DIR / "canary-reconciler-latest.json"
    module.LOG_PATH = module.LOG_DIR / "canary-reconciler.log"
    module.LAUNCH_AGENT_DIRS = (module.HOME / "Library/LaunchAgents",)

    script = module.HERMES / "bin" / "local-canary.py"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    state = module.HERMES / "state" / "local-probe.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"checked_at": module.iso()}) + "\n", encoding="utf-8")
    canary: dict[str, object] = {
        "id": "local_probe",
        "script": str(script),
        "state": "state/local-probe.json",
        "cron_tag": "HERMES_CANARY_LOCAL_PROBE",
    }
    module.load_registry = lambda: [
        {
            "capability_id": "local_probe",
            "title": "Local probe",
            "always": True,
            "canary": canary,
        }
    ]
    return script, canary


def test_loaded_launchd_schedule_replaces_owned_cron_without_touching_other_jobs(
    tmp_path: Path, monkeypatch
):
    reconciler = load_reconciler("canary_launchd_reconciliation")
    script, canary = configure_profile(reconciler, tmp_path)
    launch_agent_dir = reconciler.LAUNCH_AGENT_DIRS[0]
    launch_agent_dir.mkdir(parents=True)
    label = "com.example.local-canary"
    with (launch_agent_dir / f"{label}.plist").open("wb") as handle:
        plistlib.dump(
            {
                "Label": label,
                "ProgramArguments": [sys.executable, str(script)],
                "StartInterval": 900,
            },
            handle,
        )

    stale_owned_cron = "*/5 * * * * /old/canary # HERMES_CANARY_LOCAL_PROBE"
    unrelated_cron = "15 3 * * * /usr/local/bin/backup # LOCAL_BACKUP"
    reconciler.current_crontab = lambda: [stale_owned_cron, unrelated_cron]
    loaded_job = (
        f"program = {sys.executable}\n"
        "run interval = 900 seconds\n"
        "arguments = {\n"
        f"0 = {sys.executable}\n"
        f"1 = {script}\n"
        "}\n"
    )

    def fake_run(command, **_kwargs):
        if command[:2] == ["launchctl", "print"] and len(command) == 3:
            return subprocess.CompletedProcess(command, 0, loaded_job, "")
        if command[:2] == ["launchctl", "print-disabled"]:
            return subprocess.CompletedProcess(command, 0, "disabled services = {}", "")
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    reconciler.run = fake_run
    installed: dict[str, str] = {}

    def fake_subprocess_run(command, **_kwargs):
        assert command[0] == "crontab"
        installed["crontab"] = Path(command[1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reconciler.subprocess, "run", fake_subprocess_run)
    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=False,
            run_canaries=False,
        )
    )

    assert result["ok"] is True
    assert result["actions"][0]["status"] == "normalized:launchd"
    assert installed["crontab"] == unrelated_cron + "\n"
    persisted = json.loads(reconciler.LATEST_PATH.read_text(encoding="utf-8"))
    assert persisted["actions"][0]["status"] == "normalized:launchd"


def test_one_shot_launchd_job_does_not_suppress_canonical_cron(tmp_path: Path):
    reconciler = load_reconciler("canary_launchd_one_shot")
    script, canary = configure_profile(reconciler, tmp_path)
    launch_agent_dir = reconciler.LAUNCH_AGENT_DIRS[0]
    launch_agent_dir.mkdir(parents=True)
    label = "com.example.one-shot-canary"
    with (launch_agent_dir / f"{label}.plist").open("wb") as handle:
        plistlib.dump(
            {
                "Label": label,
                "ProgramArguments": [sys.executable, str(script)],
                "StartInterval": 900,
            },
            handle,
        )
    reconciler.current_crontab = lambda: []
    loaded_job = (
        f"program = {sys.executable}\n"
        "arguments = {\n"
        f"0 = {sys.executable}\n"
        f"1 = {script}\n"
        "}\n"
    )

    def fake_run(command, **_kwargs):
        if command[:2] == ["launchctl", "print"] and len(command) == 3:
            return subprocess.CompletedProcess(command, 0, loaded_job, "")
        if command[:2] == ["launchctl", "print-disabled"]:
            return subprocess.CompletedProcess(command, 0, "disabled services = {}", "")
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    reconciler.run = fake_run
    expected_cron = reconciler.cron_line_for(
        canary, script, "test-agent", "Test Agent"
    )

    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=True,
            run_canaries=False,
        )
    )

    assert expected_cron.endswith("# HERMES_CANARY_LOCAL_PROBE")
    assert result["ok"] is True
    assert result["actions"][0]["status"] == "would_update"


def test_scheduler_name_collision_does_not_suppress_canonical_cron(tmp_path: Path):
    reconciler = load_reconciler("canary_scheduler_collision")
    script, canary = configure_profile(reconciler, tmp_path)
    launch_agent_dir = reconciler.LAUNCH_AGENT_DIRS[0]
    launch_agent_dir.mkdir(parents=True)
    label = "com.example.unrelated"
    with (launch_agent_dir / f"{label}.plist").open("wb") as handle:
        plistlib.dump(
            {
                "Label": label,
                "ProgramArguments": [
                    "/usr/bin/logger",
                    f"documentation mentions {script.name}",
                ],
            },
            handle,
        )

    reconciler.current_crontab = lambda: []
    loaded_job = (
        "program = /usr/bin/logger\n"
        "arguments = {\n"
        "0 = /usr/bin/logger\n"
        f"1 = documentation mentions {script.name}\n"
        "}\n"
    )

    def fake_run(command, **_kwargs):
        if command[:2] == ["launchctl", "print"] and len(command) == 3:
            return subprocess.CompletedProcess(command, 0, loaded_job, "")
        if command[:2] == ["launchctl", "print-disabled"]:
            return subprocess.CompletedProcess(command, 0, "disabled services = {}", "")
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    reconciler.run = fake_run
    expected_cron = reconciler.cron_line_for(
        canary, script, "test-agent", "Test Agent"
    )
    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=True,
            run_canaries=False,
        )
    )

    assert expected_cron.endswith("# HERMES_CANARY_LOCAL_PROBE")
    assert result["ok"] is True
    assert result["actions"][0]["status"] == "would_update"


def test_unreadable_crontab_is_a_failed_action(tmp_path: Path):
    reconciler = load_reconciler("canary_crontab_failure")
    configure_profile(reconciler, tmp_path)
    reconciler.current_crontab = lambda: None
    reconciler.LAUNCH_AGENT_DIRS = ()
    reconciler.run = lambda command, **_kwargs: subprocess.CompletedProcess(
        command, 1, "", "unavailable"
    )

    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=True,
            run_canaries=False,
        )
    )

    assert result["ok"] is False
    assert result["actions"][0]["status"] == "failed:crontab_read"
    assert result["failed_actions"] == result["actions"]
    assert all(action["status"] == "failed:crontab_read" for action in result["actions"])


def test_retired_codex_health_actor_is_not_in_default_registry():
    reconciler = load_reconciler("canary_retired_codex_default")

    assert all(entry["capability_id"] != "codex" for entry in reconciler.default_registry())
    assert reconciler.RETIRED_CRON_TAGS == ("HERMES_CODEX_EXEC_HEALTH",)


def test_retired_codex_cron_is_removed_without_touching_unrelated_lines(
    tmp_path: Path, monkeypatch
):
    reconciler = load_reconciler("canary_retired_codex_cleanup")
    configure_profile(reconciler, tmp_path)
    reconciler.load_registry = lambda: []
    retired = "*/20 * * * * /old/codex-exec-health.py # HERMES_CODEX_EXEC_HEALTH"
    unrelated = "0 4 * * * /usr/local/bin/backup # KEEP_ME"
    reconciler.current_crontab = lambda: [retired, unrelated]
    reconciler.LAUNCH_AGENT_DIRS = ()
    reconciler.run = lambda command, **_kwargs: subprocess.CompletedProcess(
        command, 1, "", "unavailable"
    )
    installed: dict[str, str] = {}

    def fake_subprocess_run(command, **_kwargs):
        assert command[0] == "crontab"
        installed["crontab"] = Path(command[1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reconciler.subprocess, "run", fake_subprocess_run)
    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=False,
            run_canaries=False,
        )
    )

    assert result["ok"] is True
    assert result["actions"][0] == {
        "capability": "retired_scheduler_cleanup",
        "canary": "HERMES_CODEX_EXEC_HEALTH",
        "action": "ensure_retired",
        "status": "updated",
    }
    assert installed["crontab"] == unrelated + "\n"


def test_retired_codex_launchd_actor_is_unloaded_and_removed(tmp_path: Path):
    reconciler = load_reconciler("canary_retired_codex_launchd")
    configure_profile(reconciler, tmp_path)
    reconciler.load_registry = lambda: []
    reconciler.current_crontab = lambda: []
    launch_agent_dir = reconciler.LAUNCH_AGENT_DIRS[0]
    launch_agent_dir.mkdir(parents=True)
    script = reconciler.HERMES / "bin/codex-exec-health.py"
    label = "com.example.codex-exec-health"
    plist = launch_agent_dir / f"{label}.plist"
    with plist.open("wb") as handle:
        plistlib.dump(
            {"Label": label, "ProgramArguments": [sys.executable, str(script)]},
            handle,
        )
    loaded_job = (
        f"program = {sys.executable}\n"
        "arguments = {\n"
        f"0 = {sys.executable}\n"
        f"1 = {script}\n"
        "}\n"
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 0, loaded_job, "")
        if command[:2] == ["launchctl", "bootout"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    reconciler.run = fake_run
    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=False,
            run_canaries=False,
        )
    )

    assert result["ok"] is True
    assert result["actions"] == [
        {
            "capability": "retired_scheduler_cleanup",
            "canary": "HERMES_CODEX_EXEC_HEALTH",
            "action": "ensure_retired",
            "scheduler": "launchd",
            "status": "updated",
        }
    ]
    assert ["launchctl", "bootout", f"gui/{reconciler.os.getuid()}/{label}"] in commands
    assert not plist.exists()


def test_retired_codex_systemd_actor_is_disabled_and_removed(tmp_path: Path):
    reconciler = load_reconciler("canary_retired_codex_systemd")
    configure_profile(reconciler, tmp_path)
    reconciler.load_registry = lambda: []
    reconciler.current_crontab = lambda: []
    reconciler.LAUNCH_AGENT_DIRS = ()
    systemd_home = reconciler.HOME / ".config/systemd/user"
    systemd_home.mkdir(parents=True)
    script = reconciler.HERMES / "bin/codex-exec-health.py"
    timer = systemd_home / "codex-exec-health.timer"
    service = systemd_home / "codex-exec-health.service"
    timer.write_text("[Timer]\nOnUnitActiveSec=20m\n", encoding="utf-8")
    service.write_text(
        f"[Service]\nExecStart={sys.executable} {script}\n", encoding="utf-8"
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["systemctl", "--user", "list-units"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] in (
            ["systemctl", "--user", "is-enabled"],
            ["systemctl", "--user", "is-active"],
        ):
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        if command[:3] == ["systemctl", "--user", "show"]:
            if command[3] == timer.name and command[4] == "--property=Triggers":
                return subprocess.CompletedProcess(command, 0, service.name + "\n", "")
            if command[3] == timer.name and command[4] == "--property=FragmentPath":
                return subprocess.CompletedProcess(command, 0, str(timer) + "\n", "")
            if command[3] == service.name and command[4] == "--property=ExecStart":
                value = f"argv[]={sys.executable} {script} ; ignore_errors=no\n"
                return subprocess.CompletedProcess(command, 0, value, "")
            if command[3] == service.name and command[4] == "--property=FragmentPath":
                return subprocess.CompletedProcess(command, 0, str(service) + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    reconciler.run = fake_run
    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=False,
            run_canaries=False,
        )
    )

    assert result["ok"] is True
    assert result["actions"] == [
        {
            "capability": "retired_scheduler_cleanup",
            "canary": "HERMES_CODEX_EXEC_HEALTH",
            "action": "ensure_retired",
            "scheduler": "systemd-user",
            "status": "updated",
        }
    ]
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        timer.name,
    ] in commands
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert ["systemctl", "--user", "mask", timer.name] in commands
    assert not timer.exists()
    assert not service.exists()


def test_retired_manager_discovered_systemd_actor_is_disabled_and_masked(
    tmp_path: Path,
):
    reconciler = load_reconciler("canary_retired_external_systemd")
    configure_profile(reconciler, tmp_path)
    reconciler.load_registry = lambda: []
    reconciler.current_crontab = lambda: []
    reconciler.LAUNCH_AGENT_DIRS = ()
    script = reconciler.HERMES / "bin/codex-exec-health.py"
    timer_name = "codex-exec-health.timer"
    service_name = "codex-exec-health.service"
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["systemctl", "--user", "list-units"]:
            value = f"{timer_name} loaded active waiting Codex health\n"
            return subprocess.CompletedProcess(command, 0, value, "")
        if command[:3] in (
            ["systemctl", "--user", "is-enabled"],
            ["systemctl", "--user", "is-active"],
        ):
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        if command[:3] == ["systemctl", "--user", "show"]:
            if command[3] == timer_name and command[4] == "--property=Triggers":
                return subprocess.CompletedProcess(command, 0, service_name + "\n", "")
            if command[3] == timer_name and command[4] == "--property=FragmentPath":
                return subprocess.CompletedProcess(
                    command, 0, f"/etc/systemd/user/{timer_name}\n", ""
                )
            if command[3] == service_name and command[4] == "--property=ExecStart":
                value = f"argv[]={sys.executable} {script} ; ignore_errors=no\n"
                return subprocess.CompletedProcess(command, 0, value, "")
            if command[3] == service_name and command[4] == "--property=FragmentPath":
                return subprocess.CompletedProcess(
                    command, 0, f"/etc/systemd/user/{service_name}\n", ""
                )
        return subprocess.CompletedProcess(command, 0, "", "")

    reconciler.run = fake_run
    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=False,
            run_canaries=False,
        )
    )

    assert result["ok"] is True
    assert result["actions"] == [
        {
            "capability": "retired_scheduler_cleanup",
            "canary": "HERMES_CODEX_EXEC_HEALTH",
            "action": "ensure_retired",
            "scheduler": "systemd-user",
            "status": "updated",
        }
    ]
    assert ["systemctl", "--user", "disable", "--now", timer_name] in commands
    assert ["systemctl", "--user", "mask", timer_name] in commands


def test_systemd_drop_in_redirect_does_not_retire_unrelated_effective_job(
    tmp_path: Path,
):
    reconciler = load_reconciler("canary_retired_systemd_redirect")
    configure_profile(reconciler, tmp_path)
    reconciler.load_registry = lambda: []
    reconciler.current_crontab = lambda: []
    reconciler.LAUNCH_AGENT_DIRS = ()
    systemd_home = reconciler.HOME / ".config/systemd/user"
    systemd_home.mkdir(parents=True)
    script = reconciler.HERMES / "bin/codex-exec-health.py"
    timer = systemd_home / "codex-exec-health.timer"
    service = systemd_home / "codex-exec-health.service"
    timer.write_text("[Timer]\nOnUnitActiveSec=20m\n", encoding="utf-8")
    service.write_text(
        f"[Service]\nExecStart={sys.executable} {script}\n", encoding="utf-8"
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["systemctl", "--user", "list-units"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] in (
            ["systemctl", "--user", "is-enabled"],
            ["systemctl", "--user", "is-active"],
        ):
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        if command[:3] == ["systemctl", "--user", "show"]:
            if command[3] == timer.name and command[4] == "--property=Triggers":
                return subprocess.CompletedProcess(command, 0, service.name + "\n", "")
            if command[3] == timer.name and command[4] == "--property=FragmentPath":
                return subprocess.CompletedProcess(command, 0, str(timer) + "\n", "")
            if command[3] == service.name and command[4] == "--property=ExecStart":
                value = "argv[]=/usr/bin/logger unrelated ; ignore_errors=no\n"
                return subprocess.CompletedProcess(command, 0, value, "")
            if command[3] == service.name and command[4] == "--property=FragmentPath":
                return subprocess.CompletedProcess(command, 0, str(service) + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    reconciler.run = fake_run
    result = reconciler.reconcile(
        argparse.Namespace(
            agent_id="test-agent",
            agent_name="Test Agent",
            dry_run=False,
            run_canaries=False,
        )
    )

    assert result["ok"] is True
    assert result["actions"] == []
    assert timer.exists()
    assert service.exists()
    assert not any(command[:3] == ["systemctl", "--user", "disable"] for command in commands)
