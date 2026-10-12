from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "ensure-cua-driver.py"
CONTRACT = REPO / "contracts" / "cua-driver-release-v1.json"
CURRENT_VERSION = json.loads(CONTRACT.read_text(encoding="utf-8"))["release"]["version"]


def load_helper():
    loader = SourceFileLoader("ensure_cua_driver_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def completed(command: list[str], returncode: int, stdout: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def test_release_contract_is_exact_and_complete():
    helper = load_helper()
    contract = helper.load_contract(CONTRACT)

    assert contract["release"] == {
        "repository": "trycua/cua",
        "source_commit": "ac9b1643acb61e20d5af078215e7d1b8c414db89",
        "tag": "cua-driver-rs-v0.22.0",
        "version": "0.22.0",
    }
    assert set(contract["assets"]) == {
        "linux-arm64",
        "linux-x86_64",
        "macos-universal",
        "windows-arm64",
        "windows-x86_64",
    }
    assert all(len(asset["sha256"]) == 64 for asset in contract["assets"].values())


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "macos-universal"),
        ("Darwin", "x86_64", "macos-universal"),
        ("Windows", "AMD64", "windows-x86_64"),
        ("Windows", "ARM64", "windows-arm64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "aarch64", "linux-arm64"),
    ],
)
def test_platform_asset_resolution(system, machine, expected):
    helper = load_helper()
    assert helper.platform_asset_key(system, machine) == expected


def test_unknown_architecture_fails_closed():
    helper = load_helper()

    with pytest.raises(ValueError, match="unsupported architecture"):
        helper.platform_asset_key("Linux", "riscv64")


def test_child_environment_strips_credentials(monkeypatch, tmp_path):
    helper = load_helper()
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    env = helper.child_env(tmp_path, helper.load_contract(CONTRACT))

    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["HERMES_HOME"] == str(tmp_path)
    assert env["CUA_DRIVER_RS_VERSION"] == CURRENT_VERSION
    assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"


def test_exact_driver_is_idempotent_and_doctor_green(monkeypatch, tmp_path):
    helper = load_helper()
    monkeypatch.setattr(
        helper,
        "probe_driver",
        lambda *_args, **_kwargs: {
            "installed": True,
            "path": "/driver",
            "version": CURRENT_VERSION,
        },
    )
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return completed(command, 0, json.dumps({"ok": True, "overall": "ok"}))

    monkeypatch.setattr(helper, "run", fake_run)
    code, receipt = helper.ensure_driver(
        python=Path(sys.executable),
        home=tmp_path,
        contract_path=CONTRACT,
        system="Linux",
        machine="x86_64",
    )

    assert code == 0
    assert receipt["status"] == "idempotent"
    assert receipt["doctor_ready"] is True
    assert receipt["install_attempted"] is False
    assert len(commands) == 1
    assert commands[0][1:] == ["-c", helper.DOCTOR_CODE]


def test_missing_driver_installs_pinned_version_through_native_pm(monkeypatch, tmp_path):
    helper = load_helper()
    probes = iter(
        [
            {"installed": False, "path": None, "version": None},
            {"installed": True, "path": "/driver", "version": CURRENT_VERSION},
        ]
    )
    monkeypatch.setattr(helper, "probe_driver", lambda *_args, **_kwargs: next(probes))
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[-1] == "--version":
            return completed(command, 0, f"cua-driver {CURRENT_VERSION}")
        if command[-1] == helper.DOCTOR_CODE:
            return completed(command, 0, json.dumps({"ok": True, "overall": "ok"}))
        return completed(command, 0)

    monkeypatch.setattr(helper, "run", fake_run)
    code, receipt = helper.ensure_driver(
        python=Path(sys.executable),
        home=tmp_path,
        contract_path=CONTRACT,
        system="Windows",
        machine="AMD64",
    )

    assert code == 0
    assert receipt["status"] == "installed"
    assert receipt["install_attempted"] is True
    assert commands[0][0] == sys.executable
    assert commands[0][-2] == CURRENT_VERSION
    assert receipt["asset"]["key"] == "windows-x86_64"
    assert json.loads(commands[0][-1])["sha256"] == helper.load_contract(CONTRACT)["assets"]["windows-x86_64"]["sha256"]


def test_version_mismatch_after_install_fails_closed(monkeypatch, tmp_path):
    helper = load_helper()
    monkeypatch.setattr(
        helper,
        "probe_driver",
        lambda *_args, **_kwargs: {
            "installed": True,
            "path": "/driver",
            "version": "0.12.6",
        },
    )
    monkeypatch.setattr(helper, "run", lambda command, **_kwargs: completed(command, 0))

    code, receipt = helper.ensure_driver(
        python=Path(sys.executable),
        home=tmp_path,
        contract_path=CONTRACT,
        system="Linux",
        machine="x86_64",
    )

    assert code == 1
    assert receipt["status"] == "failed"
    assert receipt["after"]["version"] == "0.12.6"


def test_degraded_doctor_is_recorded_but_only_blocks_gui_gate(monkeypatch, tmp_path):
    helper = load_helper()
    monkeypatch.setattr(
        helper,
        "probe_driver",
        lambda *_args, **_kwargs: {
            "installed": True,
            "path": "/driver",
            "version": CURRENT_VERSION,
        },
    )
    monkeypatch.setattr(
        helper,
        "run",
        lambda command, **_kwargs: completed(
            command,
            1,
            json.dumps({"ok": False, "overall": "degraded", "checks": []}),
        ),
    )

    code, receipt = helper.ensure_driver(
        python=Path(sys.executable),
        home=tmp_path,
        contract_path=CONTRACT,
        system="Linux",
        machine="x86_64",
    )
    strict_code, strict_receipt = helper.ensure_driver(
        python=Path(sys.executable),
        home=tmp_path,
        contract_path=CONTRACT,
        require_ready=True,
        system="Linux",
        machine="x86_64",
    )

    assert code == 0
    assert receipt["status"] == "idempotent"
    assert receipt["doctor_ready"] is False
    assert strict_code == 1
    assert strict_receipt["status"] == "blocked_not_ready"


def test_doctor_payload_cannot_claim_ready_when_payload_is_degraded(
    monkeypatch, tmp_path
):
    helper = load_helper()
    monkeypatch.setattr(
        helper,
        "probe_driver",
        lambda *_args, **_kwargs: {
            "installed": True,
            "path": "/driver",
            "version": CURRENT_VERSION,
        },
    )
    monkeypatch.setattr(
        helper,
        "run",
        lambda command, **_kwargs: completed(
            command,
            0,
            json.dumps({"ok": False, "overall": "degraded", "checks": []}),
        ),
    )

    code, receipt = helper.ensure_driver(
        python=Path(sys.executable),
        home=tmp_path,
        contract_path=CONTRACT,
        require_ready=True,
        system="Linux",
        machine="x86_64",
    )

    assert code == 1
    assert receipt["doctor_ready"] is False
    assert receipt["status"] == "blocked_not_ready"


def test_dry_run_never_invokes_installer(monkeypatch, tmp_path):
    helper = load_helper()
    monkeypatch.setattr(
        helper,
        "probe_driver",
        lambda *_args, **_kwargs: {"installed": False, "path": None, "version": None},
    )
    monkeypatch.setattr(
        helper,
        "run",
        lambda *_args, **_kwargs: pytest.fail("dry-run invoked a subprocess action"),
    )

    code, receipt = helper.ensure_driver(
        python=Path(sys.executable),
        home=tmp_path,
        contract_path=CONTRACT,
        dry_run=True,
        system="Darwin",
        machine="arm64",
    )

    assert code == 0
    assert receipt["status"] == "would_install"
    assert receipt["install_attempted"] is False


@pytest.mark.parametrize("drift", [None, "version", "archive"])
def test_native_install_checks_pin_before_installing(tmp_path, drift):
    helper = load_helper()
    package = tmp_path / "pm"
    package.mkdir()
    called = tmp_path / "called"
    (package / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"def ensure(name, explicit):\n    Path({str(called)!r}).write_text(name + str(explicit))\n"
    )
    expected = {"url": "https://example.invalid/cua", "sha256": "a" * 64}
    actual = dict(expected, sha256="b" * 64) if drift == "archive" else expected
    version = "0.21.0" if drift == "version" else "0.22.0"
    (package / "lock.py").write_text(
        "class Lockfile:\n    def __init__(self, path): pass\n"
        f"    def version(self, name): return {version!r}\n"
        f"    def artifacts(self, name, target): return {[actual]!r}\n"
    )
    (package / "paths.py").write_text("def lockfile_path(): return 'lock.json'\n")
    (package / "store.py").write_text("def current_target(): return 'linux-x64'\n")
    result = subprocess.run(
        [sys.executable, "-c", helper.PINNED_INSTALL_CODE, "0.22.0", json.dumps(expected)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert (result.returncode == 0) is (drift is None), result.stderr
    assert called.exists() is (drift is None)
    if drift is None:
        assert called.read_text() == "cua-driverTrue"
