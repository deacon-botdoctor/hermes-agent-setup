from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]
PAYLOAD = ROOT / "patches/payloads/llm-attempt-receipts-v1/agent/llm_attempt_receipts.py"


def load_receipts(monkeypatch, tmp_path):
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        SimpleNamespace(get_hermes_home=lambda: tmp_path),
    )
    spec = importlib.util.spec_from_file_location("receipt_test_module", PAYLOAD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ledger_write_failure_does_not_change_provider_result(tmp_path, monkeypatch, caplog):
    receipts = load_receipts(monkeypatch, tmp_path)

    def fail_open(*args, **kwargs):
        raise OSError("read-only ledger")

    monkeypatch.setattr(receipts.os, "open", fail_open)
    response = object()

    assert receipts.execute_main_attempt(
        lambda: response,
        task="conversation",
        provider="provider",
        model="model",
        base_url="",
        api_mode="chat",
        logical_request_id="request",
        session_id="",
        turn_id="",
        platform="",
        api_key=None,
        retry_count=0,
        is_fallback=False,
        fallback_cause=None,
    ) is response
    assert "LLM attempt receipt ledger write failed: OSError" in caplog.text


def test_openrouter_missing_usage_is_recorded_without_enrichment(tmp_path, monkeypatch):
    receipts = load_receipts(monkeypatch, tmp_path)
    payload = receipts._usage_payload(
        SimpleNamespace(usage=None),
        provider="openrouter",
        model="model",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat",
    )

    assert payload["usage_status"] == "unavailable"
    assert payload["cost_status"] == "unknown"


def test_reconciler_uses_active_profile_runtime(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "reconcile_test_module", ROOT / "bin/llm-attempt-reconcile.py"
    )
    assert spec and spec.loader
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    helper = runtime / "agent/llm_attempt_receipts.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("def reconcile_events(events):\n    return {'events': events}\n")
    (home / "state").mkdir(parents=True)
    (home / "state/public-setup-current.json").write_text(
        json.dumps(
            {
                "kind": "botdoctor_public_profile_install",
                "status": "completed",
                "hermes_home": str(home),
                "runtime_dir": str(runtime),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert reconcile._load_runtime_module().reconcile_events([{"id": 1}]) == {
        "events": [{"id": 1}]
    }


def test_public_release_metadata_verifies():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin/verify-release.py"), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
