#!/usr/bin/env python3
"""Read-only, citation-aware X research through this runtime's xAI OAuth broker.

The wrapper deliberately delegates to Hermes core's ``tools.x_search_tool`` and
``tools.xai_http`` paths.  It neither reads the auth store nor handles, exports, or
caches a bearer token.  The only allowed credential contract is the calling
runtime's brokered ``xai-oauth`` capability; direct XAI_API_KEY fallback is
rejected before an X request is made.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

CAPABILITY_ID = "research.grok_x_search"
AUTH_CONTRACT = "xai-oauth"
DEFAULT_MODEL = "grok-4.6"
DEFAULT_REASONING = "high"
MAX_HANDLES = 10


@dataclass(frozen=True)
class CoreBridge:
    """The narrow, runtime-scoped Hermes core surface this wrapper may use."""

    resolve_xai_http_credentials: Callable[..., dict[str, Any]]
    x_search: Callable[..., str]


def runtime_home() -> Path:
    """Return the runtime that owns this installed wrapper, never a caller path."""
    installed_home = Path(__file__).resolve().parent.parent
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        configured_home = Path(configured).expanduser().resolve()
        if configured_home != installed_home:
            raise RuntimeError(
                "HERMES_HOME does not match this installed runtime; refusing cross-runtime X research"
            )
    return installed_home


def _core_root(home: Path) -> Path:
    """Locate only the Hermes core paired with ``home`` (profile-safe)."""
    def bound_core(owner: Path, label: str) -> Path | None:
        binding_path = owner / "state" / "runtime-binding.json"
        if not binding_path.is_file():
            return None
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            bound_root = Path(str(binding.get("runtime_root") or "")).expanduser().resolve(strict=True)
            allowed_root = (owner / "state" / "runtime-candidates").resolve(strict=True)
            bound_root.relative_to(allowed_root)
            if binding.get("status") != "active":
                raise RuntimeError(f"{label} runtime binding is not active")
            if not all(
                (bound_root / "tools" / name).is_file()
                for name in ("x_search_tool.py", "xai_http.py")
            ):
                raise RuntimeError(f"{label} active runtime lacks the approved X Search bridge")
            return bound_root
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label} active runtime binding is invalid: {exc}") from exc

    direct = bound_core(home, "calling")
    if direct is not None:
        return direct
    # A profile wrapper lives at ~/.hermes/profiles/<profile>/bin; its shared
    # core is ~/.hermes/hermes-agent.  No other client homes are considered.
    if home.parent.name == "profiles":
        shared_home = home.parent.parent
        shared = bound_core(shared_home, "shared")
        if shared is not None:
            return shared
        legacy = shared_home / "hermes-agent"
    else:
        legacy = home / "hermes-agent"
    if all((legacy / "tools" / name).is_file() for name in ("x_search_tool.py", "xai_http.py")):
        return legacy
    raise RuntimeError("approved Hermes core x_search/xai_http runtime path is unavailable")


def load_core_bridge(home: Path) -> CoreBridge:
    """Import the paired core implementation instead of implementing auth/HTTP here."""
    core_root = _core_root(home)
    core_path = str(core_root)
    if core_path not in sys.path:
        sys.path.insert(0, core_path)
    try:
        x_search_tool = importlib.import_module("tools.x_search_tool")
        xai_http = importlib.import_module("tools.xai_http")
    except Exception as exc:  # pragma: no cover - environment-specific import detail
        raise RuntimeError(f"approved Hermes core X Search bridge failed to load: {type(exc).__name__}: {exc}") from exc
    core_x_search = x_search_tool.x_search_tool

    def x_search_with_model(**kwargs: Any) -> str:
        """Forward exact model/reasoning, including on the pinned core."""
        requested_model = str(kwargs.pop("model", "") or "").strip()
        requested_reasoning = str(kwargs.pop("reasoning", "") or "").strip()
        parameters = inspect.signature(core_x_search).parameters
        forwarded = dict(kwargs)
        original_model_resolver = None
        original_reasoning_resolver = None
        if "model" in parameters:
            forwarded["model"] = requested_model
        elif requested_model:
            original_model_resolver = x_search_tool._get_x_search_model
            x_search_tool._get_x_search_model = lambda: requested_model
        if "reasoning_effort" in parameters:
            forwarded["reasoning_effort"] = requested_reasoning
        elif requested_reasoning:
            original_reasoning_resolver = x_search_tool._get_x_search_reasoning_effort
            x_search_tool._get_x_search_reasoning_effort = lambda: requested_reasoning
        try:
            return core_x_search(**forwarded)
        finally:
            if original_model_resolver is not None:
                x_search_tool._get_x_search_model = original_model_resolver
            if original_reasoning_resolver is not None:
                x_search_tool._get_x_search_reasoning_effort = original_reasoning_resolver

    return CoreBridge(
        resolve_xai_http_credentials=xai_http.resolve_xai_http_credentials,
        x_search=x_search_with_model,
    )


def broker_status(bridge: CoreBridge) -> tuple[bool, str]:
    """Ask core for this runtime's OAuth capability without exposing its bearer.

    ``resolve_xai_http_credentials`` owns auth-store/provider-pool lookup and
    refresh.  This wrapper keeps only a boolean readiness result and never
    serializes credential fields.  A non-OAuth result is rejected before the
    search tool can make a billable request.
    """
    try:
        credentials = bridge.resolve_xai_http_credentials()
    except Exception as exc:
        return False, f"xai-oauth broker unavailable: {type(exc).__name__}: {exc}"
    provider = str(credentials.get("provider") or "").strip()
    if provider != AUTH_CONTRACT:
        return False, "xai-oauth broker is unavailable for this runtime; direct credentials are not permitted"
    if not bool(str(credentials.get("api_key") or "").strip()):
        return False, "xai-oauth broker returned no usable runtime authorization"
    return True, ""


def valid_date(value: str, field: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if field == "from_date" and parsed > date.today():
        raise ValueError("from_date cannot be in the future")
    return value


def validate_request_contract(args: argparse.Namespace) -> None:
    """Reject model or reasoning substitution before any broker/core access."""
    if str(args.model or "").strip() != DEFAULT_MODEL:
        raise ValueError(f"exact Grok model required: {DEFAULT_MODEL}")
    if str(args.reasoning or "").strip() != DEFAULT_REASONING:
        raise ValueError(f"exact Grok reasoning required: {DEFAULT_REASONING}")


def request_preview(args: argparse.Namespace) -> dict[str, Any]:
    validate_request_contract(args)
    from_date = valid_date(args.from_date, "from_date")
    to_date = valid_date(args.to_date, "to_date")
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date must not be after to_date")
    handles = [handle.strip().lstrip("@") for handle in args.handle if handle.strip()]
    if len(handles) > MAX_HANDLES:
        raise ValueError(f"at most {MAX_HANDLES} --handle values")
    tool: dict[str, Any] = {"type": "x_search"}
    if handles:
        tool["allowed_x_handles"] = handles
    if from_date:
        tool["from_date"] = from_date
    if to_date:
        tool["to_date"] = to_date
    return {
        "model": args.model,
        "reasoning": {"effort": args.reasoning},
        "input": [{"role": "user", "content": args.query.strip()}],
        "tools": [tool],
        "store": False,
    }


def invoke_brokered_x_search(bridge: CoreBridge, args: argparse.Namespace) -> dict[str, Any]:
    """Use core x_search only after OAuth source selection has passed closed."""
    try:
        validate_request_contract(args)
    except ValueError as exc:
        return {"ok": False, "billable": False, "contract_blocked": True, "error": str(exc)}
    ready, error = broker_status(bridge)
    if not ready:
        return {"ok": False, "auth_blocked": True, "error": error}
    try:
        raw = bridge.x_search(
            query=args.query.strip(),
            allowed_x_handles=[handle.strip().lstrip("@") for handle in args.handle if handle.strip()],
            from_date=args.from_date,
            to_date=args.to_date,
            model=args.model,
            reasoning=args.reasoning,
        )
        response = json.loads(raw)
    except Exception as exc:
        return {"ok": False, "error": f"Hermes core X Search failed: {type(exc).__name__}: {exc}"}
    if not isinstance(response, dict):
        return {"ok": False, "error": "Hermes core X Search returned an invalid response"}
    if not response.get("success"):
        return {
            "ok": False,
            "provider": AUTH_CONTRACT,
            "error": str(response.get("error") or "xAI X Search request failed"),
            "error_type": str(response.get("error_type") or "xai_request_failed"),
        }
    if str(response.get("credential_source") or "") != AUTH_CONTRACT:
        return {
            "ok": False,
            "auth_blocked": True,
            "error": "Hermes core did not retain the xai-oauth broker source; result was rejected",
        }
    observed_model = str(response.get("model") or "").strip()
    if observed_model != args.model:
        return {
            "ok": False,
            "model_substitution": True,
            "error": f"exact Grok model required: requested {args.model}, observed {observed_model or 'missing'}",
        }
    return {
        "ok": True,
        "provider": AUTH_CONTRACT,
        "auth_class": AUTH_CONTRACT,
        "method": "x_search",
        "model": observed_model,
        "reasoning": args.reasoning,
        "query": args.query.strip(),
        "answer": response.get("answer") or "",
        "citations": list(response.get("citations") or []),
        "inline_citations": list(response.get("inline_citations") or []),
        "degraded": bool(response.get("degraded")),
        "degraded_reason": response.get("degraded_reason") or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Non-billed runtime-local xai-oauth broker check; no X request.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Non-billed runtime-local broker/request validation; no X request.",
    )
    parser.add_argument("--query", help="Specific X research question.")
    parser.add_argument(
        "--handle",
        action="append",
        default=[],
        help="Optional allowed X handle; repeat up to 10 times.",
    )
    parser.add_argument("--from-date", default="")
    parser.add_argument("--to-date", default="")
    parser.add_argument("--model", choices=[DEFAULT_MODEL], default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", choices=[DEFAULT_REASONING], default=DEFAULT_REASONING)
    args = parser.parse_args()

    try:
        validate_request_contract(args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "billable": False, "contract_blocked": True, "error": str(exc)}, sort_keys=True))
        return 2

    try:
        bridge = load_core_bridge(runtime_home())
        ready, auth_error = broker_status(bridge)
    except Exception as exc:
        ready, auth_error, bridge = False, str(exc), None

    if args.check:
        print(json.dumps({
            "ok": ready,
            "capability": CAPABILITY_ID,
            "provider": AUTH_CONTRACT,
            "auth_contract": AUTH_CONTRACT,
            "broker_scope": "calling_runtime",
            "x_native_search": ready,
            "reasoning": args.reasoning,
            "error": auth_error,
        }, sort_keys=True))
        return 0 if ready else 2

    if not args.query:
        parser.error("--query is required unless --check is used")
    try:
        preview = request_preview(args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "billable": False}, sort_keys=True))
        return 2

    if args.dry_run:
        print(json.dumps({
            "ok": ready,
            "billable": False,
            "capability": CAPABILITY_ID,
            "provider": AUTH_CONTRACT,
            "auth_contract": AUTH_CONTRACT,
            "broker_scope": "calling_runtime",
            "x_native_search_ready": ready,
            "reasoning": args.reasoning,
            "auth_blocked": not ready,
            "request": preview,
            "auth_error": auth_error,
        }, sort_keys=True))
        return 0 if ready else 2

    if not ready or bridge is None:
        print(json.dumps(
            {"ok": False, "provider": AUTH_CONTRACT, "auth_blocked": True, "error": auth_error},
            sort_keys=True,
        ))
        return 2
    result = invoke_brokered_x_search(bridge, args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else (2 if result.get("auth_blocked") else 1)


if __name__ == "__main__":
    raise SystemExit(main())
