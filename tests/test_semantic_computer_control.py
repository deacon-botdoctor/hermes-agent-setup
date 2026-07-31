from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "bin/check-semantic-computer-control.py"
CONTRACT_PATH = ROOT / "contracts/semantic-computer-control-v2.json"


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_semantic_computer_control", CHECKER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = load_checker()


def write_config(home: Path, enabled: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["semantic-computer-control-guard"],
                    "entries": {
                        "semantic-computer-control-guard": {
                            "semantic_control_only": enabled
                        }
                    },
                },
                "platform_toolsets": {
                    "cli": ["computer_use"],
                    "telegram": ["computer_use"],
                },
            }
        ),
        encoding="utf-8",
    )


def install_candidate(home: Path, runtime_root: Path) -> None:
    plugin = home / "plugins/semantic-computer-control-guard"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "semantic-computer-control-guard",
                "name": "semantic-computer-control-guard",
                "provides_hooks": [
                    "pre_tool_call",
                    "post_tool_call",
                    "on_session_end",
                ],
            }
        ),
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(
        "from .guard import register\n", encoding="utf-8"
    )
    (plugin / "guard.py").write_text(
        "_BLOCKED_CALLS = set()\n"
        "def _pin_standard_permission_mode():\n"
        "    return 'action_inflight'\n"
        "def register(ctx):\n"
        "    # direct desktop scripting or input injection is forbidden\n"
        "    # raw coordinates are forbidden\n"
        "    return None\n",
        encoding="utf-8",
    )
    skill = home / "skills/fleet/golden-computer-use-v2/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Golden computer use v2\n", encoding="utf-8")
    runtime = runtime_root / "tools/computer_use/tool.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "def _cua_permission_mode(session_id: str) -> str:\n"
        "    return 'standard'\n",
        encoding="utf-8",
    )


def test_contract_is_valid():
    contract = CHECK.load_contract(CONTRACT_PATH)

    assert contract["contract_id"] == "semantic-computer-control-v2"
    assert contract["probe_action"] == "list_windows"


def test_non_opted_in_runtime_is_not_applicable(tmp_path):
    home = tmp_path / ".hermes"
    write_config(home, False)

    result = CHECK.audit(
        CHECK.load_contract(CONTRACT_PATH), home, tmp_path / "runtime"
    )

    assert result["ok"] is True
    assert result["status"] == "not_applicable"


def test_required_audit_fails_closed_when_opt_in_is_missing(tmp_path):
    home = tmp_path / ".hermes"
    write_config(home, False)

    result = CHECK.audit(
        CHECK.load_contract(CONTRACT_PATH),
        home,
        tmp_path / "runtime",
        required=True,
    )

    assert result["ok"] is False
    assert "config:semantic-control-not-enabled" in result["gaps"]


def test_ready_candidate_proves_wiring_and_runtime_seam(tmp_path):
    home = tmp_path / ".hermes"
    runtime = tmp_path / "runtime"
    write_config(home, True)
    install_candidate(home, runtime)

    result = CHECK.audit(CHECK.load_contract(CONTRACT_PATH), home, runtime)

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["gaps"] == []
    assert result["surfaces"] == ["cli", "telegram"]


def test_missing_guard_marker_fails_closed(tmp_path):
    home = tmp_path / ".hermes"
    runtime = tmp_path / "runtime"
    write_config(home, True)
    install_candidate(home, runtime)
    guard = home / "plugins/semantic-computer-control-guard/guard.py"
    guard.write_text(
        guard.read_text(encoding="utf-8").replace("action_inflight", "removed"),
        encoding="utf-8",
    )

    result = CHECK.audit(CHECK.load_contract(CONTRACT_PATH), home, runtime)

    assert result["ok"] is False
    assert "plugin:guard-marker-missing:action_inflight" in result["gaps"]


def test_missing_lazy_skill_fails_closed(tmp_path):
    home = tmp_path / ".hermes"
    runtime = tmp_path / "runtime"
    write_config(home, True)
    install_candidate(home, runtime)
    (home / "skills/fleet/golden-computer-use-v2/SKILL.md").unlink()

    result = CHECK.audit(CHECK.load_contract(CONTRACT_PATH), home, runtime)

    assert result["ok"] is False
    assert "skill:missing" in result["gaps"]


def test_semantic_probe_uses_only_list_windows(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs

        class Result:
            returncode = 0
            stdout = json.dumps({"ok": True, "window_count": 3})
            stderr = ""

        return Result()

    monkeypatch.setattr(CHECK.subprocess, "run", fake_run)
    result = CHECK.semantic_probe(
        tmp_path / ".hermes",
        tmp_path / "runtime",
        Path(sys.executable),
    )

    assert result == {"ok": True, "window_count": 3}
    assert '"action": "list_windows"' in captured["command"][2]
    assert '"capture"' not in captured["command"][2]
    assert captured["kwargs"]["env"]["HERMES_HOME"].endswith(".hermes")


def test_requested_probe_failure_blocks_readiness(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    runtime = tmp_path / "runtime"
    write_config(home, True)
    install_candidate(home, runtime)
    monkeypatch.setattr(
        CHECK,
        "semantic_probe",
        lambda *_args, **_kwargs: {"ok": False, "detail": "driver unavailable"},
    )

    result = CHECK.audit(
        CHECK.load_contract(CONTRACT_PATH), home, runtime, run_probe=True
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert "probe:semantic-list-windows-failed" in result["gaps"]
