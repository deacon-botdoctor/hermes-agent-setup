#!/usr/bin/env python3
"""Fail-closed validator for the local per-attempt LLM receipt journal."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


def _profile_runtime_source(home: Path) -> Path | None:
    receipt_path = home / "state" / "public-setup-current.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("kind") != "botdoctor_public_profile_install"
        or receipt.get("status") != "completed"
        or receipt.get("hermes_home") != str(home)
    ):
        return None
    runtime_dir = receipt.get("runtime_dir")
    if not isinstance(runtime_dir, str) or not Path(runtime_dir).is_absolute():
        return None
    source = Path(runtime_dir) / "agent" / "llm_attempt_receipts.py"
    return source if source.is_file() else None


def _load_runtime_module():
    home = Path(
        os.environ.get("HERMES_HOME", str(Path(__file__).resolve().parents[1]))
    ).expanduser().resolve()
    candidates = [
        _profile_runtime_source(home),
        Path(__file__).resolve().parents[1] / "agent/llm_attempt_receipts.py",
        Path(__file__).resolve().parents[1] / "patches/payloads/llm-attempt-receipts-v1/agent/llm_attempt_receipts.py",
        home / "hermes-agent/agent/llm_attempt_receipts.py",
    ]
    source = next((path for path in candidates if path is not None and path.exists()), None)
    if source is None:
        raise RuntimeError("llm_attempt_receipts.py is not installed")
    runtime_root = source.parent.parent
    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))
    spec = importlib.util.spec_from_file_location("llm_attempt_receipts_runtime", source)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path(
            os.environ.get(
                "HERMES_LLM_ATTEMPT_LEDGER",
                str(
                    Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
                    / "state/llm-attempt-receipts.jsonl"
                ),
            )
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not args.ledger.exists():
        result = {
            "status": "fail",
            "attempts": 0,
            "terminal_attempts": 0,
            "violations": [{"reason": "ledger_missing", "path": str(args.ledger)}],
        }
    else:
        events = []
        violations = []
        for line_no, line in enumerate(
            args.ledger.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                violations.append({"line": line_no, "reason": "invalid_json", "detail": str(exc)})
                continue
            if isinstance(value, dict):
                events.append(value)
            else:
                violations.append({"line": line_no, "reason": "non_object"})
        result = _load_runtime_module().reconcile_events(events)
        result["violations"] = violations + result["violations"]
        result["status"] = "pass" if not result["violations"] else "fail"

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
