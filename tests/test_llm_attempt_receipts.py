from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "patches/payloads/llm-attempt-receipts-v1/agent/llm_attempt_receipts.py"


def load_receipts(monkeypatch, tmp_path):
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        SimpleNamespace(get_hermes_home=lambda: tmp_path),
    )
    monkeypatch.setitem(
        sys.modules,
        "agent.usage_pricing",
        SimpleNamespace(
            normalize_usage=lambda raw, **_kwargs: SimpleNamespace(
                input_tokens=(
                    raw.get("prompt_tokens", raw.get("input_tokens", 0))
                    if isinstance(raw, dict)
                    else getattr(raw, "prompt_tokens", getattr(raw, "input_tokens", 0))
                ),
                output_tokens=(
                    raw.get("completion_tokens", raw.get("output_tokens", 0))
                    if isinstance(raw, dict)
                    else getattr(raw, "completion_tokens", getattr(raw, "output_tokens", 0))
                ),
                cache_read_tokens=0,
                cache_write_tokens=0,
                reasoning_tokens=0,
                total_tokens=(
                    raw.get("total_tokens", 0)
                    if isinstance(raw, dict)
                    else getattr(raw, "total_tokens", 0)
                ),
            ),
            estimate_usage_cost=lambda _model, usage, **_kwargs: SimpleNamespace(
                amount_usd=0.0 if usage.total_tokens == 0 else 0.25,
                status="estimated",
                source="test-pricing",
            ),
        ),
    )
    spec = importlib.util.spec_from_file_location("receipt_test_module", PAYLOAD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ledger_write_failure_fails_closed_before_provider_call(tmp_path, monkeypatch):
    receipts = load_receipts(monkeypatch, tmp_path)

    def fail_open(*args, **kwargs):
        raise OSError("read-only ledger")

    monkeypatch.setattr(receipts.os, "open", fail_open)
    called = False

    def provider_call():
        nonlocal called
        called = True
        return object()

    with pytest.raises(OSError, match="read-only ledger"):
        receipts.execute_main_attempt(
            provider_call,
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
        )
    assert called is False


def test_openrouter_missing_usage_is_recorded_without_enrichment(tmp_path, monkeypatch):
    receipts = load_receipts(monkeypatch, tmp_path)
    payload = receipts._usage_payload(
        SimpleNamespace(usage=None),
        provider="openrouter",
        model="model",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat",
        api_key=None,
        openrouter_generation_id="id_unavailable",
    )

    assert payload["usage_status"] == "unavailable"
    assert payload["cost_status"] == "unknown"


def test_openrouter_zero_usage_is_not_priced_as_spend(tmp_path, monkeypatch):
    receipts = load_receipts(monkeypatch, tmp_path)
    payload = receipts._usage_payload(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            )
        ),
        provider="openrouter",
        model="model",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat",
        api_key=None,
        openrouter_generation_id="id_unavailable",
    )

    assert payload["usage_status"] == "provider_reported"
    assert payload["total_tokens"] == 0
    assert payload["cost_usd"] == 0.0
    assert payload["cost_status"] == "estimated"


def test_openrouter_empty_usage_is_not_priced_as_spend(tmp_path, monkeypatch):
    receipts = load_receipts(monkeypatch, tmp_path)
    payload = receipts._usage_payload(
        SimpleNamespace(usage={}),
        provider="openrouter",
        model="model",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat",
        api_key=None,
        openrouter_generation_id="id_unavailable",
    )

    assert payload["usage_status"] == "provider_reported"
    assert payload["total_tokens"] == 0
    assert payload["cost_usd"] == 0.0
    assert payload["cost_status"] == "estimated"


def test_openrouter_zero_usage_retains_actual_provider_cost(tmp_path, monkeypatch):
    receipts = load_receipts(monkeypatch, tmp_path)
    payload = receipts._usage_payload(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost=1.25,
            )
        ),
        provider="openrouter",
        model="model",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat",
        api_key=None,
        openrouter_generation_id="id_unavailable",
    )

    assert payload["usage_status"] == "provider_reported"
    assert payload["cost_usd"] == 1.25
    assert payload["cost_status"] == "actual"


def test_openrouter_mapping_usage_is_normalized_as_reported(tmp_path, monkeypatch):
    receipts = load_receipts(monkeypatch, tmp_path)
    observed = {}

    def normalize_usage(raw_usage, **_kwargs):
        observed["usage"] = raw_usage
        return SimpleNamespace(
            input_tokens=90,
            output_tokens=10,
            cache_read_tokens=10,
            cache_write_tokens=0,
            reasoning_tokens=0,
            total_tokens=110,
        )

    pricing = SimpleNamespace(
        normalize_usage=normalize_usage,
        estimate_usage_cost=lambda *_args, **_kwargs: SimpleNamespace(
            amount_usd=0.25,
            status="estimated",
            source="test-pricing",
        ),
    )
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", pricing)

    payload = receipts._usage_payload(
        SimpleNamespace(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 10},
            }
        ),
        provider="openrouter",
        model="model",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat",
        api_key=None,
        openrouter_generation_id="id_unavailable",
    )

    assert observed["usage"]["prompt_tokens"] == 100
    assert observed["usage"]["prompt_tokens_details"]["cached_tokens"] == 10
    assert payload["usage_status"] == "provider_reported"
    assert payload["input_tokens"] == 90
    assert payload["cost_usd"] == 0.25


def test_reconciler_loads_the_release_payload(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "reconcile_test_module", ROOT / "bin/llm-attempt-reconcile.py"
    )
    assert spec and spec.loader
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        SimpleNamespace(get_hermes_home=lambda: tmp_path),
    )

    module = reconcile._load_runtime_module()
    assert module.SCHEMA_VERSION == "botdoctor.llm-attempt.v1"
    assert callable(module.reconcile_events)


def test_reconciler_cli_falls_back_without_profile_receipt(tmp_path):
    home = tmp_path / "home"
    ledger = tmp_path / "llm-attempt-receipts.jsonl"
    ledger.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "schema_version": "botdoctor.llm-attempt.v1",
                        "attempt_id": "attempt-1",
                        "event": "started",
                    }
                ),
                json.dumps(
                    {
                        "schema_version": "botdoctor.llm-attempt.v1",
                        "attempt_id": "attempt-1",
                        "event": "terminal",
                        "surface": "main",
                        "task": "conversation",
                        "provider": "test",
                        "model": "test",
                        "outcome": "success",
                        "provider_request_id": "id_unavailable",
                        "provider_request_id_source": "unavailable",
                        "openrouter_generation_id": "id_unavailable",
                        "openrouter_generation_id_source": "unavailable",
                        "provenance_kind": "unlinked",
                        "provenance_ref": "id_unavailable",
                        "cost_status": "unknown",
                        "key_fingerprint": "id_unavailable",
                        "key_fingerprint_method": "unavailable",
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    dependency_root = tmp_path / "runtime-dependencies"
    dependency_root.mkdir()
    (dependency_root / "hermes_constants.py").write_text(
        "from pathlib import Path\n"
        "def get_hermes_home():\n"
        "    return Path.home() / '.hermes'\n",
        encoding="utf-8",
    )
    env = os.environ | {
        "HERMES_HOME": str(home),
        "HERMES_LLM_ATTEMPT_LEDGER": str(ledger),
        "PYTHONPATH": str(dependency_root),
    }

    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin/llm-attempt-reconcile.py")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout)["status"] == "pass"


def test_public_release_metadata_verifies():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin/verify-release.py"), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
