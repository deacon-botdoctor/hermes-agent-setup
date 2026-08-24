#!/usr/bin/env python3
"""Smoke-test a Composio remote MCP URL without leaking secrets.

Defaults to the current Hermes Gmail/Google Super MCP URL. Exits non-zero unless
it can list Gmail tools and run the route's read-only profile action.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_URL = (
    "https://backend.composio.dev/v3/mcp/"
    "ccefbe08-a260-46fa-a972-26e17e2df5d4"
    "?include_composio_helper_actions=true&user_id=enoch-google-super"
)
WRAPPER = Path.home() / ".hermes/bin/composio-remote-mcp-with-env.sh"


def redact(s: str) -> str:
    s = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "[EMAIL]", s)
    s = re.sub(r"(api[_-]?key|token|authorization)[^,}\n]*", r"\1:[REDACTED]", s, flags=re.I)
    return s


def transport(*, platform: str | None = None) -> tuple[list[str], dict[str, str]]:
    """Resolve the platform's installed Composio MCP transport."""
    env = os.environ.copy()
    if (platform or os.name) != "nt":
        env["COMPOSIO_MCP_URL"] = env.get("COMPOSIO_MCP_URL", DEFAULT_URL)
        return [str(WRAPPER)], env

    import yaml

    configured_home = Path(env.get("HERMES_HOME", "")) if env.get("HERMES_HOME") else None
    hermes_home = configured_home or Path(__file__).resolve().parent.parent
    config_path = hermes_home / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    servers = config.get("mcp_servers") or {}
    candidates: list[tuple[str, dict]] = []
    for name, server in servers.items():
        if not isinstance(server, dict) or server.get("enabled") is not True:
            continue
        metadata = server.get("metadata") or {}
        toolkits = metadata.get("toolkits") or []
        if "gmail" in toolkits and str(name).startswith("composio-"):
            candidates.append((str(name), server))
    if not candidates:
        raise RuntimeError("no enabled Windows Composio Gmail MCP transport")
    _, server = sorted(candidates, key=lambda item: item[0])[0]
    command = Path(str(server.get("command") or ""))
    args = server.get("args") or []
    server_env = server.get("env") or {}
    if (
        not command.is_absolute()
        or not command.is_file()
        or not isinstance(args, list)
        or not all(isinstance(value, str) for value in args)
        or not isinstance(server_env, dict)
        or not server_env.get("COMPOSIO_API_KEY")
        or not server_env.get("COMPOSIO_MCP_URL")
    ):
        raise RuntimeError("Windows Composio Gmail MCP transport is incomplete")
    env.update({str(key): str(value) for key, value in server_env.items()})
    return [str(command), *args], env


def main() -> int:
    messages = "\n".join(
        [
            json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"hermes-composio-verify","version":"1"}}}),
            json.dumps({"jsonrpc":"2.0","method":"notifications/initialized","params":{}}),
            json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}),
            "",
        ]
    )
    try:
        command, env = transport()
        proc = subprocess.run(
            command,
            input=messages,
            text=True,
            capture_output=True,
            env=env,
            timeout=75,
        )
    except Exception as e:
        print(f"FAIL: verifier execution failed: {type(e).__name__}: {e}")
        return 2

    tools = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("id") == 2:
            tools = [t.get("name") for t in obj.get("result", {}).get("tools", [])]
    if proc.returncode not in (0, 124):
        print("FAIL: MCP wrapper exited unexpectedly")
        print(redact(proc.stderr[-2000:]))
        return 1
    profile_action = next((name for name in ("GMAIL_GET_PROFILE", "GOOGLESUPER_GET_PROFILE") if name in tools), None)
    fetch_action = next((name for name in ("GMAIL_FETCH_EMAILS", "GOOGLESUPER_FETCH_EMAILS") if name in tools), None)
    draft_action = next((name for name in ("GMAIL_CREATE_EMAIL_DRAFT", "GOOGLESUPER_CREATE_EMAIL_DRAFT") if name in tools), None)
    if not all((profile_action, fetch_action, draft_action)):
        print("FAIL: route is missing profile, fetch, or draft capability")
        if proc.stderr:
            print(redact(proc.stderr[-1000:]))
        return 1
    profile_messages = "\n".join(
        [
            json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"hermes-composio-verify","version":"1"}}}),
            json.dumps({"jsonrpc":"2.0","method":"notifications/initialized","params":{}}),
            json.dumps({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":profile_action,"arguments":{"user_id":"me"}}}),
            "",
        ]
    )
    profile = subprocess.run(
        command,
        input=profile_messages,
        text=True,
        capture_output=True,
        env=env,
        timeout=75,
    )
    profile_summary = ""
    profile_ok = False
    for line in profile.stdout.splitlines():
        try:
            obj = json.loads(line.strip())
        except Exception:
            continue
        if obj.get("id") != 3:
            continue
        text = json.dumps(obj)
        profile_summary = redact(text[:1500])
        profile_ok = (
            "error" not in obj
            and not obj.get("result", {}).get("isError")
            and not re.search(r'\\?"success(?:ful|full)?\\?"\s*:\s*false', text, flags=re.I)
        )
    if not profile_ok:
        print(f"FAIL: {profile_action} did not succeed")
        print(profile_summary or redact(profile.stderr[-2000:]))
        return 1

    print("OK: Composio remote MCP Gmail/Google Super verified")
    print(f"tools={tools}")
    print(f"profile={profile_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
