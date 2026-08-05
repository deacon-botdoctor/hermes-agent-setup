#!/usr/bin/env python3
"""
auth-preflight.py — Validate model credentials before gateway startup.

Exit codes:
  0 = success (model responded)
  1 = auth failure (401/403)
  2 = other failure (timeout, network, bad config)

Writes failure state to ~/.hermes/state/auth-preflight-fail.json on failure.
Clears it on success.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_FILE = HERMES_HOME / "state" / "auth-preflight-fail.json"
CONFIG_PATH = HERMES_HOME / "config.yaml"
DOTENV_PATH = HERMES_HOME / ".env"
TIMEOUT = 10


def load_env_value(key):
    """Read a key from .env file."""
    if DOTENV_PATH.exists():
        for line in DOTENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip().strip("'\"")
                if val:
                    return val
    return os.environ.get(key, "")


def load_config():
    """Read model and provider config from config.yaml."""
    try:
        import yaml
    except ImportError:
        # Fallback: basic yaml parsing for simple structure
        text = CONFIG_PATH.read_text()
        # Just extract what we need with string parsing
        model = provider = api_url = api_key_env = ""
        in_model = in_providers = False
        current_provider = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "model:":
                in_model = True
                in_providers = False
                continue
            if stripped == "providers:":
                in_providers = True
                in_model = False
                continue
            if in_model and stripped.startswith("default:"):
                model = stripped.split(":", 1)[1].strip().strip("'\"")
            if in_model and stripped.startswith("provider:"):
                provider = stripped.split(":", 1)[1].strip().strip("'\"")
            if in_providers and not stripped.startswith("-") and stripped.endswith(":") and "  " not in line[:4]:
                current_provider = stripped[:-1].strip()
            if in_providers and current_provider == provider:
                if stripped.startswith("api:"):
                    api_url = stripped.split(":", 1)[1].strip().strip("'\"")
                    # Handle full URL with colon
                    if "http" not in api_url:
                        api_url = ":".join(stripped.split(":")[1:]).strip().strip("'\"")
                if stripped.startswith("api_key_env:"):
                    api_key_env = stripped.split(":", 1)[1].strip().strip("'\"")
        return model, provider, api_url, api_key_env

    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    m = data.get("model", {}) or {}
    model = m.get("default", "")
    provider = m.get("provider", "")

    providers = data.get("providers", {}) or {}
    p = providers.get(provider, {}) or {}
    api_url = p.get("api", "")
    api_key_env = p.get("api_key_env", "")

    return model, provider, api_url, api_key_env


def write_failure(error_type, message):
    """Write failure state to disk."""
    from datetime import datetime, timezone
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error_type": error_type,
        "message": message,
    }, indent=2))


def clear_failure():
    """Remove failure state on success."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def main():
    if not CONFIG_PATH.exists():
        print("PREFLIGHT FAIL: config.yaml not found", file=sys.stderr)
        write_failure("config", "config.yaml not found")
        return 2

    model, provider, api_url, api_key_env = load_config()

    if not model or not api_url:
        print(f"PREFLIGHT FAIL: incomplete config (model={model!r}, api={api_url!r})", file=sys.stderr)
        write_failure("config", f"incomplete config: model={model}, api={api_url}")
        return 2

    api_key = load_env_value(api_key_env) if api_key_env else ""
    if not api_key:
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    if not api_key:
        print(f"PREFLIGHT FAIL: no API key for {api_key_env}", file=sys.stderr)
        write_failure("auth", f"no API key found for {api_key_env}")
        return 1

    # Make a minimal completion request
    url = api_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "say ok"}],
        "max_tokens": 3,
        "temperature": 0,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        body = json.loads(resp.read())
        # Success = HTTP 200 with a choices array (content may be null for reasoning models)
        if body.get("choices"):
            print(f"PREFLIGHT OK: {provider}/{model} responded (HTTP 200)")
            clear_failure()
            return 0
        else:
            print(f"PREFLIGHT FAIL: {provider}/{model} returned empty response", file=sys.stderr)
            write_failure("api", "empty response from model")
            return 2

    except urllib.error.HTTPError as e:
        status = e.code
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", str(e))[:200]
        except Exception:
            detail = str(e)[:200]
        print(f"PREFLIGHT FAIL: HTTP {status} from {provider}/{model}: {detail}", file=sys.stderr)
        write_failure("auth" if status in (401, 403) else "api", f"HTTP {status}: {detail}")
        return 1 if status in (401, 403) else 2

    except Exception as e:
        print(f"PREFLIGHT FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        write_failure("network", f"{type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
