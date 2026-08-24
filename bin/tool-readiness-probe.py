#!/usr/bin/env python3
"""
Tool Readiness Probe — verify the agent can actually use each tool end-to-end.

Runs locally on a client machine. Outputs structured JSON to stdout.
No writes, no side effects, read-only + network smoke tests.

Usage: python3 tool-readiness-probe.py [--no-smoke] [--output PATH]
  Live network smoke tests run by default. Browser readiness remains
  non-mutating and reports manual_canary_required instead of opening a session.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", str(Path.home()))) if os.environ.get("HERMES_HOME") else Path.home()
HERMES = Path(os.environ["HERMES_HOME"]) if os.environ.get("HERMES_HOME") else HOME / ".hermes"


def runtime_python(root: Path, *, windows: bool | None = None) -> Path:
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return root / "venv" / "Scripts" / "python.exe"
    return root / "venv" / "bin" / "python3"


def python_script_command(script: Path, *, windows: bool | None = None) -> list[str]:
    """Return an executable command for a Python script on every fleet OS."""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return [sys.executable, str(script)]
    return [str(script)]


def resolve_runtime() -> tuple[Path, Path]:
    explicit_root = os.environ.get("HERMES_AGENT_ROOT")
    if explicit_root:
        root = Path(explicit_root).expanduser()
        return root, runtime_python(root)
    try:
        binding = json.loads((HERMES / "state/runtime-binding.json").read_text(encoding="utf-8"))
        root = Path(str(binding.get("runtime_root") or "")).expanduser()
        python = Path(str(binding.get("runtime_python") or "")).expanduser()
        if binding.get("status") == "active" and root.is_dir() and python.is_file():
            return root, python
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    root = HERMES / "hermes-agent"
    return root, runtime_python(root)


AGENT_ROOT, VENV_PYTHON = resolve_runtime()

# ── helpers ──────────────────────────────────────────────────────────────────


def load_config() -> dict:
    cfg_path = HERMES / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(cfg_path.read_text()) or {}
    except ImportError:
        # Fallback: use venv python
        r = subprocess.run(
            [
                str(VENV_PYTHON),
                "-c",
                f"import yaml, json; print(json.dumps(yaml.safe_load(open('{cfg_path}').read()) or {{}}))",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    return {}


def load_env() -> dict[str, str]:
    env_path = HERMES / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                value = v.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                env[k.strip()] = value

    # Runtime wrappers load several secrets from Keychain/.env.secrets before
    # starting Hermes. Treat presence in the process environment as valid
    # plumbing without printing values into the readiness report.
    for key in [
        "OPENROUTER_API_KEY",
        "MATON_API_KEY",
        "GEMINI_API_KEY",
        "MINIMAX_API_KEY",
        "COMPOSIO_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "SEARXNG_URL",
        "NOUS_API_KEY",
        "TELEGRAM_BOT_TOKEN",
    ]:
        if not env.get(key) and os.environ.get(key):
            env[key] = os.environ[key]
    return env


def expand_env_refs(value: str, env: dict[str, str]) -> tuple[str, list[str]]:
    """Expand shell-style environment references without mutating os.environ."""
    unresolved: list[str] = []

    def replace(match: re.Match) -> str:
        key = match.group(1) or match.group(2)
        resolved = env.get(key)
        if resolved is None:
            unresolved.append(key)
            return match.group(0)
        return str(resolved)

    expanded = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, str(value))
    return expanded, sorted(set(unresolved))


def composio_verifier_failure_detail(output: str, returncode: int) -> str:
    """Return a useful non-secret failure class instead of a stack-trace tail."""
    http_matches = re.findall(r"HTTP\s+(\d{3})", output, flags=re.I)
    code_matches = re.findall(r'["\']code["\']\s*:\s*(\d+)', output, flags=re.I)
    if http_matches:
        suffix = f" code={code_matches[-1]}" if code_matches else ""
        return f"HTTP {http_matches[-1]}{suffix}"
    if re.search(r"timed?\s*out|timeout", output, flags=re.I):
        return "timeout"
    if re.search(r"COMPOSIO_API_KEY is not set", output):
        return "COMPOSIO_API_KEY unavailable to verifier"
    return f"verifier exit={returncode} (no classified error)"


def http_check(url: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        from urllib.request import Request, urlopen

        req = Request(url, method="GET")
        resp = urlopen(req, timeout=timeout)
        return True, resp.read().decode("utf-8", errors="replace")[:500]
    except Exception as e:
        return False, str(e)[:200]


def resolve_command(cmd: str) -> str | None:
    if not cmd:
        return None
    expanded = os.path.expandvars(os.path.expanduser(cmd))
    if Path(expanded).exists():
        return expanded
    return shutil.which(expanded)


def local_url_variants(url: str) -> list[str]:
    variants = [url]
    if "localhost" in url:
        variants.append(url.replace("localhost", "[::1]"))
        variants.append(url.replace("localhost", "127.0.0.1"))
    return list(dict.fromkeys(variants))


def http_post(url: str, body: dict, headers: dict = None, timeout: int = 10) -> tuple[bool, dict | str]:
    try:
        from urllib.request import Request, urlopen

        data = json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = Request(url, data=data, headers=hdrs, method="POST")
        resp = urlopen(req, timeout=timeout)
        return True, json.loads(resp.read().decode())
    except Exception as e:
        return False, str(e)[:200]


def process_running(name: str) -> bool:
    r = subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=5)
    return r.returncode == 0


def hermes_version() -> str:
    git_dir = AGENT_ROOT / ".git"
    if git_dir.exists():
        try:
            r = subprocess.run(
                ["git", "-C", str(AGENT_ROOT), "describe", "--tags", "--always"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        if r.returncode == 0:
            return r.stdout.strip()
    return "unknown"


KNOWN_PROVIDER_ENVS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "xai": "XAI_API_KEY",
}
PROVIDER_MANAGED_AUTH = {"openai-codex"}


def provider_credential_available(section: dict, env: dict, provider: str) -> bool:
    """Return whether a route has an explicit, environment, or provider-managed credential."""
    api_key = str(section.get("api_key", "") or "").strip()
    if api_key.startswith("${") and api_key.endswith("}"):
        if env.get(api_key[2:-1]):
            return True
    elif api_key:
        return True
    api_key_env = str(section.get("api_key_env", "") or "").strip()
    if api_key_env and env.get(api_key_env):
        return True
    expected_env = KNOWN_PROVIDER_ENVS.get(provider, "")
    if expected_env and env.get(expected_env):
        return True
    return provider in PROVIDER_MANAGED_AUTH


# ── tool checks ──────────────────────────────────────────────────────────────


def check_firecrawl(cfg: dict, env: dict, smoke: bool) -> dict:
    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}

    web_cfg = cfg.get("web", {})
    backend = web_cfg.get("backend", "")
    if backend != "firecrawl":
        result["status"] = "broken"
        result["config"] = "backend_mismatch"
        result["detail"] = f"web.backend={backend or 'unset'}; fleet policy requires firecrawl"
        result["fix_hint"] = "Set web.backend=firecrawl and configure the local or cloud Firecrawl route"
        return result

    env_url = env.get("FIRECRAWL_API_URL", "")
    env_key = env.get("FIRECRAWL_API_KEY", "")

    if not env_url and not env_key:
        result["plumbing"] = "missing_env"
        result["status"] = "broken"
        result["detail"] = (
            "Hermes provider route is unbound: set FIRECRAWL_API_URL for local "
            "Firecrawl or FIRECRAWL_API_KEY for cloud Firecrawl"
        )
        result["fix_hint"] = "Bind FIRECRAWL_API_URL or FIRECRAWL_API_KEY in the Hermes runtime environment"
        return result

    # The runtime provider reads FIRECRAWL_API_URL/FIRECRAWL_API_KEY. A
    # config-only web.firecrawl_url is descriptive legacy state and cannot bind
    # the provider or prove the user path.
    target_url = env_url or "https://api.firecrawl.dev"
    result["route"] = (
        "local"
        if target_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
        else "cloud"
    )
    if smoke:
        provider_smoke = """
import asyncio
from tools.web_tools import web_extract_tool

async def main():
    result = await web_extract_tool(
        ["https://example.com"],
        format="markdown",
    )
    print(result)

asyncio.run(main())
"""
        runtime_env = os.environ.copy()
        runtime_env.update({key: str(value) for key, value in env.items() if value})
        try:
            probe = subprocess.run(
                [str(VENV_PYTHON), "-c", provider_smoke],
                cwd=AGENT_ROOT,
                env=runtime_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            probe = None
            provider_error = f"{type(exc).__name__}: {exc}"
        else:
            provider_error = (probe.stderr or probe.stdout or "provider returned no output")[-500:]
        provider_output = probe.stdout if probe is not None else ""
        if probe is not None and probe.returncode == 0 and "Example Domain" in provider_output:
            result["smoke"] = "ok"
        else:
            result["smoke"] = "fail"
            result["status"] = "broken"
            result["detail"] = f"Hermes web_extract provider smoke failed: {provider_error}"
    elif target_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        failures = []
        for candidate in local_url_variants(target_url):
            ok, body = http_check(candidate)
            if ok:
                break
            failures.append(f"{candidate}: {body}")
        if not ok:
            result["plumbing"] = "service_down"
            result["status"] = "broken"
            result["detail"] = "Firecrawl not responding: " + "; ".join(failures[:3])
    else:
        result["status"] = "degraded"
        result["detail"] = "Authenticated Firecrawl route configured but live smoke was not run"

    return result


def _browser_pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(0x100000 | 0x1000, False, pid)
            if not handle:
                return kernel32.GetLastError() == 5
            try:
                return kernel32.WaitForSingleObject(handle, 0) == 0x102
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def _browser_pid_file_alive(path: Path) -> bool | None:
    try:
        return _browser_pid_exists(int(path.read_text(encoding="utf-8").strip()))
    except (ValueError, OSError):
        return None


def check_browser(cfg: dict, env: dict, smoke: bool) -> dict:
    """Check Hermes' native per-task agent-browser path and session pressure."""
    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}
    browser_tool = AGENT_ROOT / "tools" / "browser_tool.py"
    if not browser_tool.exists():
        result.update(
            {
                "status": "broken",
                "plumbing": "native_tool_missing",
                "detail": f"Native Hermes browser tool is missing: {browser_tool}",
                "fix_hint": "Repair/update hermes-agent; do not add browser-lane or a second browser daemon.",
            }
        )
        return result

    python_candidates = (
        VENV_PYTHON,
        AGENT_ROOT / ".venv" / "bin" / "python3",
        HERMES / "venv" / "bin" / "python3",
        HOME / "venv" / "bin" / "python3",
        AGENT_ROOT / "venv" / "Scripts" / "python.exe",
        HERMES / "venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    )
    python = next((candidate for candidate in python_candidates if candidate.exists()), Path(sys.executable))
    probe_env = dict(env)
    probe_env.update(os.environ)
    probe_env["PYTHONPATH"] = str(AGENT_ROOT)
    code = """
import os
try:
    import json
    from tools import browser_tool
    try:
        from tools import browser_use_cli
        browser_use_mode = browser_use_cli.is_browser_use_cli_mode()
    except Exception:
        browser_use_mode = False
    if browser_use_mode:
        runtime = "browser_use_cli"
        backend = "browser-use"
        ready = True
    elif browser_tool._is_camofox_mode():
        runtime = "camofox"
        backend = "camofox"
        ready = bool(browser_tool.check_browser_requirements())
    elif browser_tool._get_cdp_override():
        runtime = "cdp_override"
        backend = "cdp_override"
        ready = bool(browser_tool.check_browser_requirements())
    else:
        provider = browser_tool._get_cloud_provider()
        runtime = "cloud_browser" if provider is not None else "native_agent_browser"
        backend = provider.provider_name() if provider is not None else "agent_browser"
        ready = bool(browser_tool.check_browser_requirements())
    payload = json.dumps({
        "ready": ready,
        "runtime": runtime,
        "backend": backend,
        "inactivity_timeout_sec": browser_tool.BROWSER_SESSION_INACTIVITY_TIMEOUT,
    }).encode()
    os.write(1, payload + b"\\n")
    os._exit(0)
except BaseException as exc:
    os.write(2, (type(exc).__name__ + ": " + str(exc)).encode(errors="replace"))
    os._exit(1)
"""
    proc = None
    native = {}
    try:
        proc = subprocess.run(
            [str(python), "-B", "-c", code],
            cwd=str(AGENT_ROOT),
            env=probe_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        result.update({"status": "broken", "plumbing": "requirements_probe_failed", "detail": str(exc)})
    else:
        detail = (proc.stderr or proc.stdout).strip()[-300:]
        if proc.returncode != 0:
            result.update(
                {
                    "status": "broken",
                    "plumbing": "requirements_probe_failed",
                    "detail": detail or f"requirements probe exited {proc.returncode}",
                }
            )
        else:
            try:
                native = json.loads(proc.stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                result.update(
                    {
                        "status": "broken",
                        "plumbing": "requirements_probe_failed",
                        "detail": str(exc),
                    }
                )
    if result["plumbing"] != "requirements_probe_failed" and not native.get("ready"):
        runtime = native.get("runtime", "unknown")
        native_runtime = runtime == "native_agent_browser"
        result.update(
            {
                "status": "broken",
                "plumbing": "requirements_missing",
                "detail": ((proc.stderr or proc.stdout).strip()[-300:] if proc else "")
                or (
                    "agent-browser CLI or Chromium is unavailable"
                    if native_runtime
                    else f"Configured {native.get('backend', runtime)} browser backend is unavailable"
                ),
                "fix_hint": (
                    "Repair the existing per-client agent-browser/Chromium install; "
                    "do not spawn a second browser/profile."
                    if native_runtime
                    else f"Repair the configured {runtime} backend or select the local agent-browser path."
                ),
            }
        )
    timeout_sec = int(native.get("inactivity_timeout_sec") or 120)
    socket_root = Path("/tmp") if sys.platform == "darwin" else Path(tempfile.gettempdir())
    session_dirs = []
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    for path in socket_root.glob("agent-browser-*"):
        try:
            if current_uid is None or path.stat().st_uid == current_uid:
                session_dirs.append(path)
        except OSError:
            pass
    orphan_ages = []
    live_count = 0
    now = time.time()
    for path in session_dirs:
        session_name = path.name.removeprefix("agent-browser-")
        try:
            age = max(0, int(now - path.stat().st_mtime))
        except OSError:
            continue
        owner_alive = _browser_pid_file_alive(path / f"{session_name}.owner_pid")
        daemon_alive = _browser_pid_file_alive(path / f"{session_name}.pid")
        if owner_alive is True and daemon_alive is True:
            live_count += 1
        else:
            orphan_ages.append(age)
    result.update(
        {
            "runtime": native.get("runtime", "unknown"),
            "browser_backend": native.get("backend", "unknown"),
            "inactivity_timeout_sec": timeout_sec,
            "session_artifact_count": len(session_dirs),
            "live_session_artifact_count": live_count,
            "orphan_session_artifact_count": len(orphan_ages),
            "oldest_session_artifact_age_sec": max(orphan_ages, default=0),
        }
    )
    if result["status"] == "ok" and (len(session_dirs) > 4 or max(orphan_ages, default=0) > timeout_sec * 2):
        result.update(
            {
                "status": "degraded",
                "plumbing": "session_pressure",
                "detail": (
                    f"Found {len(session_dirs)} agent-browser session artifacts, "
                    f"{len(orphan_ages)} orphaned; oldest orphan is {max(orphan_ages, default=0)}s"
                ),
            }
        )

    if smoke:
        # Do not create sessions from a fleet-wide audit. agent-browser 0.27.0
        # on Spark was verified to hang on close and leak its Chromium tree.
        # Interactive canaries remain an explicit, isolated rollout step.
        result["smoke"] = "manual_canary_required"
    return result


def check_auxiliary_task(cfg: dict, env: dict, task: str) -> dict:
    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}

    aux = cfg.get("auxiliary", {})
    section = aux.get(task, {})

    if not section or not section.get("provider"):
        result["config"] = "not_configured"
        result["detail"] = f"auxiliary.{task} not configured; optional for this runtime"
        return result

    provider = section.get("provider", "")
    model = section.get("model", "")
    base_url = section.get("base_url", "")
    api_key = section.get("api_key", "")
    api_key_env = section.get("api_key_env", "")

    issues = []

    # Anti-pattern: base_url without any resolvable key source.
    # auxiliary_client resolves api_key_env from process env or HERMES_HOME/.env
    # before deciding whether a base_url route is usable.
    if base_url and not api_key and not (api_key_env and env.get(api_key_env)):
        issues.append("base_url set without api_key or resolvable api_key_env")
        result["plumbing"] = "auth_gap"
        result["status"] = "broken"
        result["fix_hint"] = (
            f"Set api_key/api_key_env for auxiliary.{task}, or remove base_url to use provider defaults"
        )

    # Anti-pattern: anthropic provider
    if provider == "anthropic":
        issues.append("provider is anthropic (banned from Hermes runtime)")
        result["plumbing"] = "banned_provider"
        result["status"] = "broken"

    # Check provider env var
    if not base_url and not api_key:
        expected_env = KNOWN_PROVIDER_ENVS.get(provider, "")
        if expected_env and not env.get(expected_env):
            issues.append(f"{expected_env} not set in .env for provider '{provider}'")
            result["plumbing"] = "missing_env"
            result["status"] = "broken"

    # Known broken route: Codex ChatGPT accounts do not support mini models for vision.
    if task == "vision" and provider == "openai-codex" and "mini" in str(model):
        issues.append(
            "auxiliary.vision uses openai-codex mini, which is not supported for vision on ChatGPT Codex accounts"
        )
        result["plumbing"] = "unsupported_vision_model"
        result["status"] = "broken"
        result["fix_hint"] = "Set auxiliary.vision.provider=openrouter and model=google/gemini-2.5-flash"

    if issues:
        result["detail"] = "; ".join(issues)

    return result


def check_vision(cfg: dict, env: dict, smoke: bool) -> dict:
    return check_auxiliary_task(cfg, env, "vision")


def check_compression(cfg: dict, env: dict, smoke: bool) -> dict:
    result = check_auxiliary_task(cfg, env, "compression")

    # Also check top-level compression summary config
    comp = cfg.get("compression", {})
    summary_base_url = comp.get("summary_base_url", "")

    summary_provider = str(comp.get("summary_provider", "") or "").strip()
    summary_route = {
        "api_key": comp.get("summary_api_key", ""),
        "api_key_env": comp.get("summary_api_key_env", ""),
    }
    if summary_base_url and not provider_credential_available(summary_route, env, summary_provider):
        if result.get("status") == "ok":
            result["status"] = "degraded"
        result.setdefault("detail", "")
        result["detail"] += "; compression.summary_base_url has no accountable credential source"
        result["fix_hint"] = "Configure the summary provider credential source or remove summary_base_url"

    return result


def check_anamnesis(cfg: dict, env: dict, smoke: bool) -> dict:
    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}

    mcp = cfg.get("mcp_servers", {})
    ana = mcp.get("anamnesis", {})

    if not ana:
        result["config"] = "not_configured"
        result["detail"] = "No anamnesis MCP server configured; optional for this runtime"
        return result

    cmd = ana.get("command", "")
    if cmd and not Path(cmd).exists():
        result["plumbing"] = "missing_binary"
        result["status"] = "broken"
        result["detail"] = f"MCP command not found: {cmd}"
        return result

    mcp_env = ana.get("env", {})
    qdrant_url = mcp_env.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
    ok, body = http_check(f"{qdrant_url}/collections")
    if not ok:
        result["plumbing"] = "qdrant_down"
        result["status"] = "broken"
        result["detail"] = f"Qdrant not responding at {qdrant_url}"
        return result

    embed_provider = mcp_env.get("EMBED_PROVIDER", "ollama")

    if embed_provider == "ollama":
        ok, body = http_check("http://localhost:11434/api/tags")
        if ok:
            if '"models":[]' in body or '"models": []' in body:
                result["plumbing"] = "no_embed_models"
                result["status"] = "broken"
                result["detail"] = "Ollama running but has zero models — embeddings will fail"
                result["fix_hint"] = "Either pull nomic-embed-text or switch EMBED_PROVIDER to openai with OpenRouter"
        else:
            result["plumbing"] = "ollama_down"
            result["status"] = "broken"
            result["detail"] = "EMBED_PROVIDER=ollama but Ollama not responding"
    elif embed_provider == "openai":
        api_key_ref = mcp_env.get("OPENAI_API_KEY", "")
        if api_key_ref.startswith("${") and api_key_ref.endswith("}"):
            var_name = api_key_ref[2:-1]
            if not env.get(var_name):
                result["plumbing"] = "missing_env"
                result["status"] = "broken"
                result["detail"] = f"OPENAI_API_KEY references ${{{var_name}}} but it's not set in .env"

    db_path = mcp_env.get("ANAMNESIS_DB", "")
    if not db_path:
        args = ana.get("args", []) if isinstance(ana.get("args"), list) else []
        if "--memory-db" in args:
            idx = args.index("--memory-db")
            if idx + 1 < len(args):
                db_path = args[idx + 1]
    if not db_path:
        db_path = str(HOME / ".anamnesis" / "memory.db")
    db_path = os.path.expandvars(os.path.expanduser(str(db_path)))
    if not Path(db_path).exists():
        result["plumbing"] = "no_db"
        result["status"] = "broken"
        result["detail"] = f"Anamnesis DB not found: {db_path}"

    if smoke and result["status"] == "ok":
        embed_url = mcp_env.get("OPENAI_EMBED_URL", "")
        api_key = ""
        api_key_ref = mcp_env.get("OPENAI_API_KEY", "")
        if api_key_ref.startswith("${"):
            var_name = api_key_ref[2:-1]
            api_key = env.get(var_name, "")
        else:
            api_key = api_key_ref

        if embed_provider == "openai" and embed_url and api_key:
            model = mcp_env.get("EMBED_MODEL", "text-embedding-3-small")
            ok, resp = http_post(
                embed_url, {"model": model, "input": "test query"}, headers={"Authorization": f"Bearer {api_key}"}
            )
            if ok and isinstance(resp, dict) and resp.get("data"):
                result["smoke"] = "ok"
            else:
                result["smoke"] = "fail"
                result["status"] = "degraded"
                result["detail"] = f"Embedding smoke test failed: {str(resp)[:100]}"

    return result


def check_email(cfg: dict, env: dict, smoke: bool) -> dict:
    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}

    # Current Google/Gmail tooling may use Composio remote MCP URLs. A local
    # COMPOSIO_API_KEY is not required for that path; only degrade if neither the
    # remote MCP path nor legacy/local keys are present.
    mcp_servers = cfg.get("mcp_servers", {}) if isinstance(cfg.get("mcp_servers"), dict) else {}
    composio_servers = []
    for name, server in mcp_servers.items():
        if not isinstance(server, dict) or server.get("enabled") is False:
            continue
        metadata = server.get("metadata", {}) if isinstance(server.get("metadata"), dict) else {}
        env_cfg = server.get("env", {}) if isinstance(server.get("env"), dict) else {}
        desc = str(metadata.get("description", "")).lower()
        if "composio" in name.lower() and ("gmail" in name.lower() or "google" in name.lower() or "gmail" in desc):
            if server.get("url") or env_cfg.get("COMPOSIO_MCP_URL"):
                composio_servers.append(name)

    if composio_servers:
        result["detail"] = (
            "Composio remote MCP configured for email/google tooling "
            f"({len(composio_servers)} server(s)); COMPOSIO_API_KEY not required"
        )
        if smoke:
            preferred = next(
                (name for name in composio_servers if name.lower() == "composio-gmail"), composio_servers[0]
            )
            first = mcp_servers[preferred]
            url = first.get("url") or (first.get("env", {}) or {}).get("COMPOSIO_MCP_URL", "")
            if url:
                smoke_env = dict(os.environ)
                resolved_url, unresolved = expand_env_refs(str(url), env)
                if unresolved:
                    result["smoke"] = "fail"
                    result["status"] = "degraded"
                    result["detail"] = (
                        f"Composio remote MCP route unresolved for {preferred}: "
                        f"missing env {','.join(unresolved)}"
                    )
                    return result
                smoke_env["COMPOSIO_MCP_URL"] = resolved_url
                if env.get("COMPOSIO_API_KEY"):
                    smoke_env["COMPOSIO_API_KEY"] = env["COMPOSIO_API_KEY"]
                # Use the owning verifier so readiness exercises the same
                # authenticated wrapper and transport as the live MCP route.
                # A raw SDK client cannot load the route token held by that
                # wrapper and therefore produces a false 401.
                verifier = HERMES / "bin/composio-remote-mcp-verify.py"
                if not verifier.is_file():
                    result["smoke"] = "fail"
                    result["status"] = "degraded"
                    result["detail"] = f"Composio remote MCP verifier missing for {preferred}"
                    return result
                probe = subprocess.run(
                    python_script_command(verifier),
                    env=smoke_env,
                    capture_output=True,
                    text=True,
                    timeout=170,
                )
                if probe.returncode == 0:
                    result["smoke"] = "ok"
                else:
                    result["smoke"] = "fail"
                    result["status"] = "degraded"
                    output = "\n".join(part for part in (probe.stdout, probe.stderr) if part)
                    result["detail"] = (
                        f"Composio remote MCP verifier failed for {preferred}: "
                        f"{composio_verifier_failure_detail(output, probe.returncode)}"
                    )
        return result

    composio_key = env.get("COMPOSIO_API_KEY", "")
    if composio_key:
        result["detail"] = "Legacy Composio API key configured; Maton not required"
        return result

    maton_key = env.get("MATON_API_KEY", "")
    if not maton_key:
        result["config"] = "not_configured"
        result["detail"] = "No email tooling configured; optional for this runtime"
        return result

    return result


def check_delegation(cfg: dict, env: dict, smoke: bool) -> dict:
    deleg = cfg.get("delegation", {})
    if not deleg or not deleg.get("provider"):
        # No separate delegation config is fine — Hermes delegates through the main model/fallback chain
        return {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}

    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}
    provider = deleg.get("provider", "")
    base_url = deleg.get("base_url", "")
    has_credential = provider_credential_available(deleg, env, provider)

    if base_url and not has_credential:
        result["plumbing"] = "auth_gap"
        result["status"] = "broken"
        result["detail"] = "base_url set without an accountable credential source"
        result["fix_hint"] = f"Configure credentials for '{provider}' or remove the delegation base_url"
    elif not base_url and not has_credential:
        expected_env = KNOWN_PROVIDER_ENVS.get(provider, "")
        if expected_env:
            result["plumbing"] = "missing_env"
            result["status"] = "broken"
            result["detail"] = f"{expected_env} not set for delegation provider '{provider}'"

    if provider == "anthropic":
        result["plumbing"] = "banned_provider"
        result["status"] = "broken"
        result["detail"] = "Delegation uses anthropic (banned)"

    return result


def check_mcp_servers(cfg: dict, env: dict, smoke: bool) -> dict:
    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip", "servers": {}}

    mcp = cfg.get("mcp_servers", {})
    if not mcp:
        result["config"] = "none"
        result["status"] = "ok"
        result["detail"] = "No MCP servers configured"
        return result

    broken = 0
    for name, server_cfg in mcp.items():
        if name == "anamnesis":
            continue  # checked separately
        if not isinstance(server_cfg, dict):
            continue
        if server_cfg.get("enabled") is False or server_cfg.get("tier") == "disabled":
            result["servers"][name] = {"status": "skipped", "detail": "disabled"}
            continue
        cmd = server_cfg.get("command", "")
        resolved_cmd = resolve_command(cmd) if cmd else None
        if cmd and not resolved_cmd:
            result["servers"][name] = {"status": "broken", "detail": f"Command not found on PATH: {cmd}"}
            broken += 1
            continue
        if resolved_cmd:
            server_cfg = dict(server_cfg)
            server_cfg["command"] = resolved_cmd
            cmd = resolved_cmd
        # Startup readiness: a launchable command is not enough -- the server
        # entrypoint must actually load. Catches the "binary exists but the
        # python module/script was orphaned by an upgrade" failure class that
        # otherwise only surfaces as a silent MCP connection failure at runtime.
        launch = _mcp_launch_readiness(cmd, server_cfg)
        if launch is not None and launch.get("status") == "broken":
            result["servers"][name] = launch
            broken += 1
        else:
            result["servers"][name] = launch or {"status": "ok"}

    if broken > 0:
        result["status"] = "degraded"
        result["detail"] = f"{broken} MCP server(s) failed startup readiness"

    return result


def _mcp_launch_readiness(cmd: str, server_cfg: dict) -> dict | None:
    """Verify an MCP server entrypoint can load, without fully starting it.

    - ``python -m <module>``: resolve the module with the server's own env
      (notably PYTHONPATH) via importlib.find_spec in a subprocess. A missing
      module (e.g. an upgrade that orphaned the package) reports ``broken``.
    - script-path entrypoint: confirm the .py file exists on disk.
    Returns None when the launch shape is unrecognized (treated as ok).
    """
    args = server_cfg.get("args", []) or []
    server_env = dict(os.environ)
    for k, v in (server_cfg.get("env", {}) or {}).items():
        if isinstance(v, str):
            server_env[k] = os.path.expandvars(v)
    base = os.path.basename(cmd).lower()
    is_python = base.startswith("python")
    if is_python and "-m" in args:
        mi = args.index("-m")
        if mi + 1 < len(args):
            module = args[mi + 1]
            try:
                probe = subprocess.run(
                    [
                        cmd,
                        "-c",
                        "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 3)",
                        module,
                    ],
                    env=server_env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "broken", "detail": f"module probe error for {module}: {exc}"}
            if probe.returncode != 0:
                tail = (probe.stderr or probe.stdout or "").strip().splitlines()
                detail = tail[-1] if tail else f"module not importable: {module}"
                return {"status": "broken", "detail": f"{module}: {detail}"}
            return {"status": "ok"}
    shell_names = {"sh", "bash", "zsh"}
    is_shell = base in shell_names or base.endswith("/sh") or base.endswith("/bash") or base.endswith("/zsh")
    if is_shell:
        payload = " ".join(a for a in args if isinstance(a, str))
        scripts = re.findall(r"(?:^|\s)(/[^\s;]+\.py)(?=\s|;|$)", payload)
        for script in scripts:
            if not Path(os.path.expandvars(script)).exists():
                return {"status": "broken", "detail": f"Entrypoint script not found: {script}"}
        return {"status": "ok"} if scripts else None
    # A wrapper may receive a full ``sh -lc`` payload as one argument. Do not
    # mistake that command string for a literal script path; command existence
    # was already verified above, and the wrapper owns its nested launch.
    script = next(
        (
            a
            for a in reversed(args)
            if isinstance(a, str)
            and a.endswith(".py")
            and not re.search(r"[\s;&|<>]", a)
        ),
        None,
    )
    if script and not Path(os.path.expandvars(script)).exists():
        return {"status": "broken", "detail": f"Entrypoint script not found: {script}"}
    return None


def scan_anti_patterns(cfg: dict, env: dict) -> list[dict]:
    patterns = []

    # Check all auxiliary sections for base_url + api_key_env (no api_key)
    aux = cfg.get("auxiliary", {})
    for task, section in aux.items():
        if not isinstance(section, dict):
            continue
        api_key_env = str(section.get("api_key_env", "")).strip()
        if section.get("base_url") and not section.get("api_key") and not (api_key_env and env.get(api_key_env)):
            patterns.append(
                {
                    "pattern": "base_url_without_key_source",
                    "location": f"auxiliary.{task}",
                    "detail": "base_url is set but no api_key or resolvable api_key_env is available",
                    "fix": f"Set api_key/api_key_env for auxiliary.{task}, or remove base_url",
                }
            )
        if section.get("provider") == "anthropic":
            patterns.append(
                {
                    "pattern": "anthropic_provider",
                    "location": f"auxiliary.{task}",
                    "detail": "Anthropic banned from Hermes runtime",
                }
            )

    # Check top-level compression
    comp = cfg.get("compression", {})
    summary_provider = str(comp.get("summary_provider", "") or "").strip()
    summary_route = {
        "api_key": comp.get("summary_api_key", ""),
        "api_key_env": comp.get("summary_api_key_env", ""),
    }
    if comp.get("summary_base_url") and not provider_credential_available(summary_route, env, summary_provider):
        patterns.append(
            {
                "pattern": "base_url_without_key_source",
                "location": "compression.summary_base_url",
                "detail": "summary_base_url has no accountable credential source",
            }
        )

    # Check delegation
    deleg = cfg.get("delegation", {})
    delegation_provider = str(deleg.get("provider", "") or "").strip()
    if deleg.get("base_url") and not provider_credential_available(deleg, env, delegation_provider):
        patterns.append(
            {
                "pattern": "base_url_without_key_source",
                "location": "delegation",
                "detail": "delegation base_url has no accountable credential source",
            }
        )

    # Check fallback providers for anthropic
    for i, fb in enumerate(cfg.get("fallback_providers", [])):
        if isinstance(fb, dict) and fb.get("provider") == "anthropic":
            patterns.append(
                {
                    "pattern": "anthropic_provider",
                    "location": f"fallback_providers[{i}]",
                    "detail": "Anthropic in fallback chain",
                }
            )

    # Check main model
    model = cfg.get("model", {})
    if model.get("provider") == "anthropic":
        patterns.append(
            {"pattern": "anthropic_provider", "location": "model.provider", "detail": "Primary model uses anthropic"}
        )

    return patterns


def check_api_key_validity(cfg: dict, env: dict, smoke: bool) -> dict:
    """Verify the primary OpenRouter API key is actually valid."""
    result = {"status": "ok", "config": "ok", "plumbing": "ok", "smoke": "skip"}

    key = env.get("OPENROUTER_API_KEY", "")
    if not key:
        provider = cfg.get("model", {}).get("provider", "")
        if provider == "openrouter":
            result["status"] = "broken"
            result["plumbing"] = "missing_env"
            result["detail"] = "OPENROUTER_API_KEY not set but provider is openrouter"
        return result

    if key.startswith("REUSE") or key.startswith("REPLACE") or len(key) < 20:
        result["status"] = "broken"
        result["plumbing"] = "placeholder_key"
        result["detail"] = f"OPENROUTER_API_KEY is a placeholder: {key[:20]}"
        return result

    if smoke:
        try:
            from urllib.request import Request, urlopen

            req = Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"}, method="GET")
            resp = urlopen(req, timeout=10)
            import json

            data = json.loads(resp.read().decode())
            if data.get("data", {}).get("label"):
                result["smoke"] = "ok"
            else:
                result["smoke"] = "ok"
        except Exception as e:
            err = str(e)
            if "401" in err or "User not found" in err:
                result["status"] = "broken"
                result["smoke"] = "fail"
                result["detail"] = "OpenRouter key rejected (401) — expired or invalid"
            elif "403" in err:
                result["status"] = "broken"
                result["smoke"] = "fail"
                result["detail"] = "OpenRouter key forbidden (403) — account issue"
            else:
                result["smoke"] = "degraded"
                result["detail"] = f"Key check failed: {err[:100]}"

    return result


# ── main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Tool Readiness Probe")
    smoke = parser.add_mutually_exclusive_group()
    smoke.add_argument("--smoke", dest="smoke", action="store_true", help="Run live smoke tests (default)")
    smoke.add_argument("--no-smoke", dest="smoke", action="store_false", help="Skip live network smoke tests")
    parser.set_defaults(smoke=True)
    parser.add_argument("--output", type=Path, help="Atomically write the JSON report to this path")
    args = parser.parse_args()

    cfg = load_config()
    env = load_env()

    client_id = cfg.get("client_identity", "unknown")

    tools = {}
    checks = [
        ("api_key_validity", check_api_key_validity),
        ("firecrawl", check_firecrawl),
        ("browser", check_browser),
        ("vision", check_vision),
        ("compression", check_compression),
        ("anamnesis", check_anamnesis),
        ("email", check_email),
        ("delegation", check_delegation),
        ("mcp_servers", check_mcp_servers),
    ]

    # Also check all auxiliary tasks
    aux_tasks = ["web_extract", "session_search", "skills_hub", "approval", "mcp", "flush_memories"]
    for task in aux_tasks:
        checks.append((f"aux_{task}", lambda c, e, s, t=task: check_auxiliary_task(c, e, t)))

    for name, check_fn in checks:
        try:
            tools[name] = check_fn(cfg, env, args.smoke)
        except Exception as e:
            tools[name] = {"status": "error", "detail": f"Probe crashed: {e}"}

    anti_patterns = scan_anti_patterns(cfg, env)

    summary = {"total": 0, "ok": 0, "degraded": 0, "broken": 0, "error": 0}
    for t in tools.values():
        summary["total"] += 1
        status = t.get("status", "error")
        if status in summary:
            summary[status] += 1

    report = {
        "client": client_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "runtime_root": str(AGENT_ROOT),
        "python_executable": sys.executable,
        "hermes_version": hermes_version(),
        "tools": tools,
        "anti_patterns": anti_patterns,
        "summary": summary,
    }

    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{args.output.name}.tmp-", dir=str(args.output.parent))
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, args.output)
        finally:
            tmp.unlink(missing_ok=True)
    print(rendered, end="")


if __name__ == "__main__":
    main()
