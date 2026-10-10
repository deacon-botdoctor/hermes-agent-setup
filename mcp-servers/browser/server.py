#!/usr/bin/env python3
"""browser — generic CDP browser automation MCP for Enoch.

Drives Brave on Mini via Chrome DevTools Protocol (port 9222).
No domain restrictions. Use for any web automation: navigate, click,
type, read, screenshot, eval JS.

Tools:
  browser_health      — CDP status + tab list
  browser_navigate    — navigate to URL (creates tab if needed)
  browser_click       — click element by CSS selector
  browser_type        — type text into element
  browser_get_text    — get page or element text
  browser_get_html    — get page or element HTML
  browser_screenshot  — screenshot as base64 PNG
  browser_wait        — sleep N ms
  browser_eval        — evaluate arbitrary JS
  browser_close_tab   — close tab by URL hint

Required env:
  BROWSER_CDP_URL  (default: http://127.0.0.1:9222)
"""
from __future__ import annotations
import asyncio, base64, json, os, subprocess, sys, time, uuid
from pathlib import Path
from typing import Any
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path.home() / "hermes-mcp-local-stack" / "shared"))
from mcp_stdio import Tool, serve  # type: ignore

try:
    import websockets  # type: ignore
except ImportError:
    websockets = None  # type: ignore

CDP_URL = os.environ.get("BROWSER_CDP_URL", "http://127.0.0.1:9222")

# === [HERMES_BROWSER_TAB_GC_v1] 2026-05-25 ===
# Tab garbage collector: tracks tabs created by THIS MCP and auto-closes them
# after _TAB_GC_MAX_AGE_SEC. Pre-existing tabs (not opened by this MCP) are
# NEVER touched — only tabs in _mcp_created_tabs are eligible.
# Lazy janitor: runs at the top of each tool handler; no background thread.
_TAB_GC_MAX_AGE_SEC = int(os.environ.get("HERMES_BROWSER_TAB_GC_MAX_AGE_SEC", "3600"))
_TAB_GC_ENABLED = os.environ.get("HERMES_BROWSER_TAB_GC", "1").lower() in ("1", "true", "yes")
_mcp_created_tabs: dict[str, float] = {}  # tab_id -> created_at unix ts
_mcp_tab_leases: dict[str, str] = {}  # tab_id -> Host Steward lease id
# === END [HERMES_BROWSER_TAB_GC_v1] globals ===

_HOST_STEWARD_TTL_SECONDS = max(60, min(_TAB_GC_MAX_AGE_SEC, 30 * 86400))


def _host_steward_create_tab() -> tuple[str, str]:
    """Create one CDP tab only after durable Host Steward ownership exists."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    steward = home / "bin" / "hermes-host-steward.py"
    if steward.is_symlink() or not steward.is_file():
        raise RuntimeError("Host Steward is unavailable; refusing an unowned browser tab")
    task_id = f"browser-mcp:{os.getpid()}:{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(steward),
                "--hermes-home",
                str(home),
                "create-browser-tab",
                "--task-id",
                task_id,
                "--endpoint",
                CDP_URL,
                "--ttl",
                str(_HOST_STEWARD_TTL_SECONDS),
                *([] if _TAB_GC_ENABLED else ["--protected"]),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        payload = json.loads(result.stdout) if result.stdout else {}
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Host Steward registration failed; refusing an unowned browser tab"
        ) from exc
    target_id = payload.get("target_id") if isinstance(payload, dict) else None
    lease_id = payload.get("lease_id") if isinstance(payload, dict) else None
    if (
        not isinstance(target_id, str)
        or not target_id
        or not isinstance(lease_id, str)
        or len(lease_id) != 32
    ):
        if isinstance(lease_id, str) and len(lease_id) == 32:
            _host_steward_release_lease(lease_id)
        raise RuntimeError("Host Steward rejected browser-tab ownership")
    return target_id, lease_id


def _host_steward_release_lease(lease_id: str) -> bool:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    steward = home / "bin" / "hermes-host-steward.py"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(steward),
                "--hermes-home",
                str(home),
                "release-lease",
                "--lease-id",
                lease_id,
                "--apply",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        payload = json.loads(result.stdout) if result.stdout else {}
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return False
    return result.returncode == 0 and payload.get("outcome") in {
        "released",
        "already_gone",
        "preserved",
    }


def _extract_domain(url: str | None) -> str:
    """Return sanitized domain from URL, or empty string."""
    if not url or not isinstance(url, str):
        return ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _event(tool: str, **fields) -> dict:
    """Build the structured event dict for trace capture (Phase 0).

    Always includes tool name; callers add tool-specific fields. Empty/None
    values are dropped to keep the event compact.
    """
    out = {"tool": tool}
    for k, v in fields.items():
        if v is None or v == "":
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# Phase 1 — Strategy memory (per-domain markdown scratchpad)
# ---------------------------------------------------------------------------

_STRATEGY_DIR = __import__("pathlib").Path.home() / ".hermes" / "data" / "browser-strategies"
_STRATEGY_MAX_BYTES = 64 * 1024
_STRATEGY_SECTIONS = ("What works", "Known endpoints", "Selectors that resolve", "Pitfalls")


def _strategy_path(domain: str):
    """Return Path for this domain's strategy.md, or None if domain invalid."""
    if not domain or not isinstance(domain, str):
        return None
    safe = "".join(c for c in domain.lower() if c.isalnum() or c in ".-_")
    safe = safe.strip(".-_")
    if not safe or len(safe) > 200:
        return None
    return _STRATEGY_DIR / f"{safe}.md"


def _read_strategy(domain: str) -> str:
    """Return the raw markdown content of a domain's strategy.md, or empty string."""
    p = _strategy_path(domain)
    if not p or not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")[:_STRATEGY_MAX_BYTES]
    except Exception:
        return ""


def _new_strategy_template(domain: str) -> str:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    body = [f"# Browser strategy memory: {domain}", "", f"_Last verified: {ts}_", ""]
    for section in _STRATEGY_SECTIONS:
        body.append(f"## {section}")
        body.append("")
        body.append("_(none yet)_")
        body.append("")
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Phase 2 — Trace store (per-domain event history)
# ---------------------------------------------------------------------------

_TRACE_DB = __import__("pathlib").Path.home() / ".hermes" / "data" / "durable-threads.db"


def _ensure_trace_schema(conn) -> None:
    """Idempotent schema creation. durable-threads.db is shared with breadcrumb +
    tool_playbook + agent_threads; we add browser_traces alongside."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS browser_traces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          domain TEXT NOT NULL,
          tab_id TEXT,
          tool TEXT NOT NULL,
          url TEXT,
          selector TEXT,
          selector_resolved INTEGER,
          event_json TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_browser_traces_domain_ts "
        "ON browser_traces(domain, ts DESC)"
    )


def _record_trace(event: dict, tab_id: str | None, url: str | None) -> None:
    """Best-effort write of one event to browser_traces. Silent on failure."""
    if not isinstance(event, dict):
        return
    domain = event.get("domain") or _extract_domain(url)
    if not domain:
        return
    try:
        import sqlite3 as _sql, json as _j, time as _t
        _TRACE_DB.parent.mkdir(parents=True, exist_ok=True)
        with _sql.connect(str(_TRACE_DB), timeout=5) as conn:
            _ensure_trace_schema(conn)
            sel_resolved = event.get("selector_resolved")
            sel_resolved_int = (1 if sel_resolved is True else 0 if sel_resolved is False else None)
            conn.execute(
                "INSERT INTO browser_traces (ts, domain, tab_id, tool, url, selector, selector_resolved, event_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(_t.time()),
                    domain,
                    tab_id,
                    event.get("tool", "?"),
                    url,
                    event.get("selector"),
                    sel_resolved_int,
                    _j.dumps(event, ensure_ascii=False)[:8000],
                ),
            )
    except Exception:
        pass


def _fetch_traces(domain: str, since_minutes: int = 60, limit: int = 100) -> list[dict]:
    """Read recent traces for a domain. Returns list of dicts."""
    if not domain or not _TRACE_DB.exists():
        return []
    try:
        import sqlite3 as _sql, json as _j, time as _t
        cutoff = int(_t.time()) - max(60, int(since_minutes)) * 60
        with _sql.connect(str(_TRACE_DB), timeout=5) as conn:
            try:
                rows = conn.execute(
                    "SELECT ts, tab_id, tool, url, selector, selector_resolved, event_json "
                    "FROM browser_traces WHERE domain = ? AND ts >= ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (domain, cutoff, max(1, min(int(limit), 500))),
                ).fetchall()
            except _sql.OperationalError:
                return []
        out = []
        for ts, tab_id, tool, url, selector, sel_resolved_int, event_json in rows:
            try:
                event = _j.loads(event_json) if event_json else {}
            except Exception:
                event = {}
            out.append({
                "ts": ts,
                "tab_id": tab_id,
                "tool": tool,
                "url": url,
                "selector": selector,
                "selector_resolved": (True if sel_resolved_int == 1 else False if sel_resolved_int == 0 else None),
                "event": event,
            })
        return out
    except Exception:
        return []


def _attach_trace(result: dict) -> dict:
    """Side-effect helper: extract event from a tool result, write it to the trace
    store (if it has a domain), and return the result unchanged. Tools call this
    on their way out so the writer runs once per tool call."""
    try:
        event = result.get("event") if isinstance(result, dict) else None
        if not event:
            return result
        tab_id = result.get("tab_id") if isinstance(result, dict) else None
        url = result.get("url") if isinstance(result, dict) else None
        _record_trace(event, tab_id=tab_id, url=url)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Phase 3 — Skill graduation (mature strategy → reusable SKILL.md)
# ---------------------------------------------------------------------------

_SKILL_DIR = __import__("pathlib").Path.home() / ".hermes" / "data" / "browser-skills"
_GRADUATION_USE_THRESHOLD = 3
_GRADUATION_SUCCESS_THRESHOLD = 2


def _ensure_stats_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS browser_strategy_stats (
          domain TEXT PRIMARY KEY,
          use_count INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0,
          last_use_ts INTEGER,
          last_failure_ts INTEGER,
          graduated_at INTEGER,
          graduated_skill_path TEXT
        )
    """)


def _bump_stat(domain: str, *, use: bool = False, success: bool = False) -> None:
    """Best-effort counter increment for graduation gating. Silent on failure."""
    if not domain or not (use or success):
        return
    try:
        import sqlite3 as _sql, time as _t
        _TRACE_DB.parent.mkdir(parents=True, exist_ok=True)
        with _sql.connect(str(_TRACE_DB), timeout=5) as conn:
            _ensure_stats_schema(conn)
            now = int(_t.time())
            conn.execute(
                "INSERT INTO browser_strategy_stats (domain, use_count, success_count, last_use_ts) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET "
                "  use_count = use_count + ?, "
                "  success_count = success_count + ?, "
                "  last_use_ts = CASE WHEN ? > 0 THEN ? ELSE last_use_ts END",
                (
                    domain,
                    1 if use else 0,
                    1 if success else 0,
                    now if use else None,
                    1 if use else 0,
                    1 if success else 0,
                    1 if use else 0,
                    now,
                ),
            )
    except Exception:
        pass


def _read_stats(domain: str) -> dict:
    if not domain:
        return {}
    try:
        import sqlite3 as _sql
        if not _TRACE_DB.exists():
            return {}
        with _sql.connect(str(_TRACE_DB), timeout=5) as conn:
            try:
                row = conn.execute(
                    "SELECT use_count, success_count, last_use_ts, last_failure_ts, "
                    "       graduated_at, graduated_skill_path "
                    "FROM browser_strategy_stats WHERE domain = ?",
                    (domain,),
                ).fetchone()
            except _sql.OperationalError:
                return {}
        if not row:
            return {}
        return {
            "domain": domain,
            "use_count": row[0] or 0,
            "success_count": row[1] or 0,
            "last_use_ts": row[2],
            "last_failure_ts": row[3],
            "graduated_at": row[4],
            "graduated_skill_path": row[5],
        }
    except Exception:
        return {}


def _skill_path(domain: str):
    """Path to the graduated SKILL.md for this domain, or None if invalid."""
    if not domain or not isinstance(domain, str):
        return None
    safe = "".join(c for c in domain.lower() if c.isalnum() or c in ".-_")
    safe = safe.strip(".-_")
    if not safe or len(safe) > 200:
        return None
    return _SKILL_DIR / safe / "SKILL.md"


def _read_skill(domain: str) -> str:
    p = _skill_path(domain)
    if not p or not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")[:_STRATEGY_MAX_BYTES]
    except Exception:
        return ""


def _is_eligible_for_graduation(domain: str) -> tuple[bool, str]:
    stats = _read_stats(domain)
    if not stats:
        return False, "no usage stats yet — build strategy first via browser_strategy_append"
    if stats.get("graduated_at"):
        return False, f"already graduated at ts={stats['graduated_at']}; path={stats.get('graduated_skill_path')}"
    use = stats.get("use_count", 0)
    succ = stats.get("success_count", 0)
    if use < _GRADUATION_USE_THRESHOLD:
        return False, f"use_count {use} < threshold {_GRADUATION_USE_THRESHOLD}"
    if succ < _GRADUATION_SUCCESS_THRESHOLD:
        return False, f"success_count {succ} < threshold {_GRADUATION_SUCCESS_THRESHOLD}"
    p = _strategy_path(domain)
    if not p or not p.exists():
        return False, "no strategy.md to graduate"
    return True, "eligible"


def _graduate_skill(domain: str, force: bool = False) -> tuple[bool, str, str]:
    """Promote strategy.md → SKILL.md. Returns (ok, reason, skill_path_str)."""
    if not force:
        ok, reason = _is_eligible_for_graduation(domain)
        if not ok:
            return False, reason, ""
    strategy_text = _read_strategy(domain)
    if not strategy_text:
        return False, "strategy.md missing or empty", ""
    sk_path = _skill_path(domain)
    if not sk_path:
        return False, f"invalid domain: {domain!r}", ""

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    # Compose SKILL.md by wrapping the strategy with frontmatter + helper hooks.
    skill_text = f"""---
name: browser-{domain}
description: "Graduated browser skill for {domain} — selectors, endpoints, and known pitfalls learned through the Autobrowse loop."
version: 1.0.0
metadata:
  hermes:
    kind: browser
    domain: {domain}
    graduated_at: {ts}
    graduated_from: ~/.hermes/data/browser-strategies/{domain}.md
---

# Browser skill: {domain}

Graduated from per-domain strategy memory after the Autobrowse loop converged.
Strategy.md scratchpad at ~/.hermes/data/browser-strategies/{domain}.md remains
mutable for further exploration; this SKILL.md is the canonical "what we know
works" record. Subsequent navigations to {domain} will surface this skill
INSTEAD of the strategy unless skill_memory is missing.

{strategy_text}

## Deterministic helpers

_(Phase 3.5 will add Python helper functions referenced from this section.)_
"""
    sk_path.parent.mkdir(parents=True, exist_ok=True)
    sk_path.write_text(skill_text, encoding="utf-8")

    # Update stats: mark graduated
    try:
        import sqlite3 as _sql, time as _t
        with _sql.connect(str(_TRACE_DB), timeout=5) as conn:
            _ensure_stats_schema(conn)
            conn.execute(
                "UPDATE browser_strategy_stats SET graduated_at = ?, graduated_skill_path = ? "
                "WHERE domain = ?",
                (int(_t.time()), str(sk_path), domain),
            )
            # Insert if no row existed (force-graduate edge case)
            conn.execute(
                "INSERT OR IGNORE INTO browser_strategy_stats "
                "(domain, use_count, success_count, last_use_ts, graduated_at, graduated_skill_path) "
                "VALUES (?, 0, 0, ?, ?, ?)",
                (domain, int(_t.time()), int(_t.time()), str(sk_path)),
            )
    except Exception:
        pass

    return True, "graduated", str(sk_path)


# ---------------------------------------------------------------------------
# Phase 3.5 — Deterministic helpers (Python functions referenced by SKILL.md)
# ---------------------------------------------------------------------------

import re as _helper_re

_HELPER_NAME_RE = _helper_re.compile(r"^[a-z][a-z0-9_]{1,62}$")


def _helper_module_path(domain: str):
    """Path to the helpers.py module for this domain, or None if invalid."""
    p = _skill_path(domain)
    if not p:
        return None
    return p.parent / "helpers.py"


def _list_helpers(domain: str) -> list[dict]:
    """Return [{name, signature, doc}] for each helper defined in helpers.py."""
    p = _helper_module_path(domain)
    if not p or not p.exists():
        return []
    try:
        import ast as _ast
        tree = _ast.parse(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and not node.name.startswith("_"):
            args = []
            for a in node.args.args:
                args.append(a.arg)
            if node.args.vararg:
                args.append(f"*{node.args.vararg.arg}")
            if node.args.kwarg:
                args.append(f"**{node.args.kwarg.arg}")
            doc = _ast.get_docstring(node) or ""
            out.append({
                "name": node.name,
                "signature": f"{node.name}({', '.join(args)})",
                "doc": doc[:300],
            })
    return out


def _write_helper(domain: str, helper_name: str, code: str, docstring: str = "") -> tuple[bool, str]:
    """Write or replace a single helper function in the domain's helpers.py.

    `code` should be the function body (everything after `def name(args):`),
    NOT the full def line. The helper must use only stdlib + json + urllib;
    we do not enforce this, but agents should default to lightweight code.
    Returns (ok, reason).
    """
    if not domain:
        return False, "empty domain"
    if not _HELPER_NAME_RE.match(helper_name or ""):
        return False, f"invalid helper_name {helper_name!r} — must match {_HELPER_NAME_RE.pattern}"
    if not isinstance(code, str) or not code.strip():
        return False, "empty code"

    p = _helper_module_path(domain)
    if not p:
        return False, f"invalid domain: {domain!r}"

    p.parent.mkdir(parents=True, exist_ok=True)

    # Build the new function source
    doc_block = ""
    if docstring:
        doc_block = f'    """{docstring.strip()}"""\n'
    # Strip leading whitespace from each line, then re-indent at 4 spaces
    body_lines = code.replace("\r\n", "\n").rstrip().split("\n")
    indented = "\n".join(("    " + ln if ln else "") for ln in body_lines)
    new_func = f"def {helper_name}(*args, **kwargs):\n{doc_block}{indented}\n"

    # Validate the new function parses on its own
    try:
        import ast as _ast
        _ast.parse(new_func)
    except SyntaxError as e:
        return False, f"helper code has syntax error: {e}"

    # If module exists, replace existing function with same name; else create new module
    if p.exists():
        existing = p.read_text(encoding="utf-8")
        # Find the function block by AST and splice
        try:
            tree = _ast.parse(existing)
        except SyntaxError:
            return False, "existing helpers.py has syntax error — fix manually before writing"
        replaced = False
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef) and node.name == helper_name:
                # Replace lines node.lineno-1 to node.end_lineno
                lines = existing.split("\n")
                start = node.lineno - 1
                end = (node.end_lineno or start + 1)
                new_lines = lines[:start] + new_func.rstrip().split("\n") + lines[end:]
                existing = "\n".join(new_lines)
                replaced = True
                break
        if not replaced:
            # Append new function at end
            existing = existing.rstrip() + "\n\n\n" + new_func
        # Final validation
        try:
            _ast.parse(existing)
        except SyntaxError as e:
            return False, f"resulting helpers.py would not parse: {e}"
        p.write_text(existing, encoding="utf-8")
    else:
        # Create new module with header
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        header = f"""\"\"\"Deterministic helpers for browser skill: {domain}

Auto-generated by browser_skill_helper_write. Each helper is a Python function
the agent can call directly via browser_skill_helper_call to bypass live
browser exploration. Use only stdlib + json + urllib for portability.

Generated: {ts}
\"\"\"
from __future__ import annotations
import json
import urllib.request
import urllib.parse


"""
        p.write_text(header + new_func, encoding="utf-8")

    return True, "written"


def _call_helper(domain: str, helper_name: str, kwargs: dict | None = None) -> dict:
    """Import the helpers module and invoke the named function with kwargs."""
    p = _helper_module_path(domain)
    if not p or not p.exists():
        return {"ok": False, "error": "no helpers module for this domain"}
    if not _HELPER_NAME_RE.match(helper_name or ""):
        return {"ok": False, "error": f"invalid helper_name {helper_name!r}"}
    try:
        import importlib.util as _iu, hashlib as _h
        # Use a unique module name so re-imports pick up edits
        mod_id = "_helpers_" + _h.md5(str(p).encode("utf-8")).hexdigest()[:12] + "_" + str(int(__import__("time").time()))
        spec = _iu.spec_from_file_location(mod_id, str(p))
        if spec is None or spec.loader is None:
            return {"ok": False, "error": "could not build module spec"}
        mod = _iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, helper_name, None)
        if fn is None or not callable(fn):
            return {"ok": False, "error": f"helper {helper_name!r} not found in module"}
        result = fn(**(kwargs or {}))
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}


def _append_strategy(domain: str, observation: str, section: str = "What works") -> tuple[bool, str]:
    """Append an observation as a bullet under the given section.

    Creates strategy.md from template if missing. Updates Last verified stamp.
    Returns (ok, reason).
    """
    p = _strategy_path(domain)
    if not p:
        return False, f"invalid domain: {domain!r}"
    obs = (observation or "").strip()
    if not obs:
        return False, "empty observation"
    if section not in _STRATEGY_SECTIONS:
        return False, f"unknown section {section!r}; expected one of {_STRATEGY_SECTIONS}"
    obs = obs.replace("\n", " ").strip()[:500]

    from datetime import datetime, timezone
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        text = p.read_text(encoding="utf-8")
    else:
        text = _new_strategy_template(domain)

    # Update Last verified stamp
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    import re as _re
    text = _re.sub(r"_Last verified: [^_]+_", f"_Last verified: {ts}_", text, count=1)

    # Find the section and append the bullet right after the heading
    section_marker = f"## {section}"
    idx = text.find(section_marker)
    if idx < 0:
        # Section missing — append it at end
        text = text.rstrip() + f"\n\n## {section}\n\n- {obs}\n"
    else:
        # Insert the bullet right after the section heading + blank line
        # If the section currently has the placeholder "_(none yet)_", replace it
        section_end = text.find("\n## ", idx + len(section_marker))
        if section_end < 0:
            section_end = len(text)
        section_block = text[idx:section_end]
        if "_(none yet)_" in section_block:
            section_block = section_block.replace("_(none yet)_", f"- {obs}", 1)
        else:
            # Append a new bullet at end of section block
            section_block = section_block.rstrip() + f"\n- {obs}\n\n"
        text = text[:idx] + section_block + text[section_end:]

    if len(text.encode("utf-8")) > _STRATEGY_MAX_BYTES:
        return False, f"strategy file would exceed {_STRATEGY_MAX_BYTES} bytes — distill before appending"

    p.write_text(text, encoding="utf-8")
    _bump_stat(domain, success=True)
    return True, "appended"


SKIP_URL_PREFIXES = (
    "chrome://", "chrome-extension://", "devtools://",
    "about:", "data:", "",
)


# ---------------------------------------------------------------------------
# CDP primitives
# ---------------------------------------------------------------------------

def _http_json(path: str) -> Any:
    with urllib.request.urlopen(f"{CDP_URL}{path}", timeout=5) as r:
        return json.loads(r.read())


def _list_tabs() -> list[dict]:
    try:
        return [t for t in _http_json("/json") if t.get("type") == "page"]
    except Exception as e:
        raise RuntimeError(f"CDP not reachable at {CDP_URL}: {e}") from e


def _find_tab(hint: str | None) -> dict | None:
    tabs = _list_tabs()
    if not tabs:
        return None
    if hint:
        hint_lower = hint.lower()
        for t in tabs:
            if hint_lower in (t.get("url") or "").lower():
                return t
        return None
    # No hint: return first navigable tab
    for t in tabs:
        url = t.get("url") or ""
        if not any(url.startswith(p) for p in SKIP_URL_PREFIXES):
            return t
    return tabs[0]


async def _cdp_commands(ws_url: str, commands: list[dict], timeout: float = 20.0) -> list[dict]:
    if websockets is None:
        raise RuntimeError("websockets not installed in this venv")
    async with websockets.connect(ws_url, max_size=20_000_000) as ws:
        for cmd in commands:
            await ws.send(json.dumps(cmd))
        results: dict[int, dict] = {}
        wanted = {c["id"] for c in commands}
        while wanted:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            mid = msg.get("id")
            if mid in wanted:
                results[mid] = msg
                wanted.discard(mid)
    return [results[c["id"]] for c in commands]


def _cdp(ws_url: str, commands: list[dict], timeout: float = 20.0) -> list[dict]:
    return asyncio.run(_cdp_commands(ws_url, commands, timeout))


def _eval(tab: dict, js: str, timeout: float = 15.0) -> Any:
    ws = tab.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError("tab has no debugger URL")
    results = _cdp(ws, [{
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": js, "returnByValue": True, "awaitPromise": True},
    }], timeout=timeout)
    r = results[0]
    if "exceptionDetails" in r.get("result", {}):
        detail = r["result"]["exceptionDetails"]
        raise RuntimeError(f"JS exception: {detail.get('text','')}: {detail.get('exception',{}).get('description','')[:300]}")
    return r.get("result", {}).get("result", {}).get("value")


def _navigate_tab(tab: dict, url: str, wait_ms: int = 2000) -> dict:
    ws = tab.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError("tab has no debugger URL")
    _cdp(ws, [{"id": 1, "method": "Page.navigate", "params": {"url": url}}])
    if wait_ms > 0:
        time.sleep(wait_ms / 1000)
    # Refresh tab info
    for t in _list_tabs():
        if t.get("id") == tab.get("id"):
            return t
    return tab


def _create_tab(url: str, wait_ms: int = 2000) -> dict:
    target_id, lease_id = _host_steward_create_tab()
    # Publish to the fast in-process GC immediately. If CDP lookup/navigation
    # fails, close this exact steward-created target; a failed close remains in
    # the GC index for retry and in the durable lease for reconciliation.
    _mcp_created_tabs[target_id] = time.time()
    _mcp_tab_leases[target_id] = lease_id
    try:
        tab = None
        for t in _list_tabs():
            if t.get("id") == target_id:
                tab = t
                break
        if tab is None:
            raise RuntimeError("Host Steward created a tab that CDP did not publish")
        return _navigate_tab(tab, url, wait_ms)
    except Exception:
        try:
            _http_json(f"/json/close/{target_id}")
            if _host_steward_release_lease(lease_id):
                _mcp_created_tabs.pop(target_id, None)
                _mcp_tab_leases.pop(target_id, None)
        except Exception:
            pass
        raise


# === [HERMES_BROWSER_TAB_GC_v1] janitor helpers ===
def _gc_tabs() -> int:
    """Close MCP-created tabs older than _TAB_GC_MAX_AGE_SEC. Returns count closed.

    Only touches tabs in _mcp_created_tabs (tabs this MCP created). User tabs and
    tabs created by other tools are NEVER closed.
    """
    if not _TAB_GC_ENABLED or not _mcp_created_tabs:
        return 0
    now = time.time()
    to_close = [tid for tid, ts in list(_mcp_created_tabs.items())
                if (now - ts) > _TAB_GC_MAX_AGE_SEC]
    if not to_close:
        return 0
    try:
        live_ids = {t["id"] for t in _list_tabs()}
    except Exception:
        return 0
    closed = 0
    for tid in to_close:
        if tid not in live_ids:
            if _host_steward_release_lease(_mcp_tab_leases.get(tid, "")):
                _mcp_created_tabs.pop(tid, None)
                _mcp_tab_leases.pop(tid, None)
            continue
        try:
            # Chromium /json/close/<id> closes the target by HTTP — no WebSocket needed
            _http_json(f"/json/close/{tid}")
            if _host_steward_release_lease(_mcp_tab_leases.get(tid, "")):
                _mcp_created_tabs.pop(tid, None)
                _mcp_tab_leases.pop(tid, None)
                closed += 1
        except Exception:
            pass
    return closed
# === END [HERMES_BROWSER_TAB_GC_v1] janitor helpers ===


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def browser_health(_args: dict) -> dict:
    _gc_tabs()  # [HERMES_BROWSER_TAB_GC_v1] lazy janitor
    try:
        version = _http_json("/json/version")
        tabs = _list_tabs()
        return {
            "ok": True,
            "browser": version.get("Browser", "unknown"),
            "cdp_url": CDP_URL,
            "tab_count": len(tabs),
            "tabs": [{"id": t["id"], "url": t.get("url","")[:120], "title": t.get("title","")[:80]} for t in tabs],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def browser_navigate(args: dict) -> dict:
    _gc_tabs()  # [HERMES_BROWSER_TAB_GC_v1] lazy janitor
    url: str = args["url"]
    hint: str | None = args.get("tab_url_hint")
    new_tab: bool = args.get("new_tab", False)
    wait_ms: int = int(args.get("wait_ms", 2000))

    if new_tab:
        tab = _create_tab(url, wait_ms)
        final_url = tab.get("url", url)
        final_domain = _extract_domain(final_url)
        result = {
            "ok": True, "tab_id": tab.get("id"), "url": final_url, "action": "new_tab",
            "domain": final_domain,
            "event": _event("browser_navigate", domain=final_domain, action="new_tab",
                            url_requested=url, url_after=final_url, url_changed=(final_url != url)),
        }
        skill = _read_skill(final_domain)
        if skill:
            result["skill_memory"] = skill
            _bump_stat(final_domain, use=True)
        else:
            strategy = _read_strategy(final_domain)
            if strategy:
                result["strategy_memory"] = strategy
                _bump_stat(final_domain, use=True)
        return _attach_trace(result)

    tab = _find_tab(hint)
    if tab is None:
        tab = _create_tab(url, wait_ms)
        final_url = tab.get("url", url)
        final_domain = _extract_domain(final_url)
        result = {
            "ok": True, "tab_id": tab.get("id"), "url": final_url, "action": "new_tab",
            "domain": final_domain,
            "event": _event("browser_navigate", domain=final_domain, action="new_tab",
                            url_requested=url, url_after=final_url, url_changed=(final_url != url)),
        }
        skill = _read_skill(final_domain)
        if skill:
            result["skill_memory"] = skill
            _bump_stat(final_domain, use=True)
        else:
            strategy = _read_strategy(final_domain)
            if strategy:
                result["strategy_memory"] = strategy
                _bump_stat(final_domain, use=True)
        return _attach_trace(result)

    url_before = tab.get("url", "")
    tab = _navigate_tab(tab, url, wait_ms)
    final_url = tab.get("url", url)
    final_domain = _extract_domain(final_url)
    result = {
        "ok": True, "tab_id": tab.get("id"), "url": final_url, "action": "navigated",
        "domain": final_domain,
        "event": _event("browser_navigate", domain=final_domain, action="navigated",
                        url_before=url_before, url_requested=url, url_after=final_url,
                        url_changed=(final_url != url)),
    }
    skill = _read_skill(final_domain)
    if skill:
        result["skill_memory"] = skill
        result["skill_hint"] = (
            f"GRADUATED browser skill exists for {final_domain}. This is the canonical "
            "record of what works on this domain — selectors, endpoints, pitfalls. "
            "Trust it for routine interactions. If something fails, fall back to "
            "exploration via browser_strategy_append (the strategy.md scratchpad is "
            "still mutable for new observations)."
        )
        _bump_stat(final_domain, use=True)
    else:
        strategy = _read_strategy(final_domain)
        if strategy:
            result["strategy_memory"] = strategy
            result["strategy_hint"] = (
                f"Per-domain strategy memory exists for {final_domain}. Read strategy_memory "
                "before exploring; it lists what works, known endpoints, selectors that resolve, "
                "and pitfalls. Append new observations via browser_strategy_append."
            )
            _bump_stat(final_domain, use=True)
    return _attach_trace(result)


def browser_click(args: dict) -> dict:
    selector: str = args["selector"]
    hint: str | None = args.get("tab_url_hint")
    wait_ms: int = int(args.get("wait_ms", 500))

    tab = _find_tab(hint)
    if tab is None:
        return {"ok": False, "error": "No matching tab found"}

    js = f"""
    (function() {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return {{found: false, selector: {json.dumps(selector)}}};
        el.scrollIntoView({{block:'center'}});
        el.click();
        return {{found: true, tag: el.tagName, text: (el.innerText||'').slice(0,100)}};
    }})()
    """
    result = _eval(tab, js)
    if wait_ms > 0:
        time.sleep(wait_ms / 1000)
    found = result.get("found", False) if isinstance(result, dict) else False
    domain = _extract_domain(tab.get("url"))
    return _attach_trace({
        "ok": found, "detail": result, "url": tab.get("url"),
        "domain": domain, "selector": selector, "selector_resolved": found,
        "event": _event("browser_click", domain=domain, selector=selector,
                        selector_resolved=found,
                        tag=(result.get("tag") if isinstance(result, dict) else None)),
    })


def browser_type(args: dict) -> dict:
    selector: str = args["selector"]
    text: str = args["text"]
    hint: str | None = args.get("tab_url_hint")
    clear_first: bool = args.get("clear_first", True)

    tab = _find_tab(hint)
    if tab is None:
        return {"ok": False, "error": "No matching tab found"}

    clear_js = f"const _el = document.querySelector({json.dumps(selector)}); if (_el) _el.value = '';" if clear_first else ""
    js = f"""
    (function() {{
        {clear_js}
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return {{found: false}};
        el.focus();
        el.value = {json.dumps(text)};
        el.dispatchEvent(new Event('input', {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        return {{found: true, tag: el.tagName, value_length: el.value.length}};
    }})()
    """
    result = _eval(tab, js)
    found = result.get("found", False) if isinstance(result, dict) else False
    domain = _extract_domain(tab.get("url"))
    return _attach_trace({
        "ok": found, "detail": result, "url": tab.get("url"),
        "domain": domain, "selector": selector, "selector_resolved": found,
        "event": _event("browser_type", domain=domain, selector=selector,
                        selector_resolved=found,
                        value_length=(result.get("value_length") if isinstance(result, dict) else None)),
    })


def browser_get_text(args: dict) -> dict:
    hint: str | None = args.get("tab_url_hint")
    selector: str | None = args.get("selector")

    tab = _find_tab(hint)
    if tab is None:
        return {"ok": False, "error": "No matching tab found"}

    if selector:
        js = f"(document.querySelector({json.dumps(selector)}) || {{}}).innerText || ''"
    else:
        js = "document.body.innerText || ''"

    text = _eval(tab, js) or ""
    domain = _extract_domain(tab.get("url"))
    selector_resolved = (len(text) > 0) if selector else None
    return _attach_trace({
        "ok": True, "url": tab.get("url",""), "length": len(text), "text": text[:50000],
        "domain": domain, "selector": selector, "selector_resolved": selector_resolved,
        "event": _event("browser_get_text", domain=domain, selector=selector,
                        selector_resolved=selector_resolved, text_length=len(text)),
    })


def browser_get_html(args: dict) -> dict:
    hint: str | None = args.get("tab_url_hint")
    selector: str | None = args.get("selector")

    tab = _find_tab(hint)
    if tab is None:
        return {"ok": False, "error": "No matching tab found"}

    if selector:
        js = f"(document.querySelector({json.dumps(selector)}) || {{}}).outerHTML || ''"
    else:
        js = "document.documentElement.outerHTML || ''"

    html = _eval(tab, js) or ""
    domain = _extract_domain(tab.get("url"))
    selector_resolved = (len(html) > 0) if selector else None
    return _attach_trace({
        "ok": True, "url": tab.get("url",""), "length": len(html), "html": html[:100000],
        "domain": domain, "selector": selector, "selector_resolved": selector_resolved,
        "event": _event("browser_get_html", domain=domain, selector=selector,
                        selector_resolved=selector_resolved, html_length=len(html)),
    })


def browser_screenshot(args: dict) -> dict:
    hint: str | None = args.get("tab_url_hint")

    tab = _find_tab(hint)
    if tab is None:
        return {"ok": False, "error": "No matching tab found"}

    ws = tab.get("webSocketDebuggerUrl")
    if not ws:
        return {"ok": False, "error": "tab has no debugger URL"}

    results = _cdp(ws, [{"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}], timeout=15.0)
    data = results[0].get("result", {}).get("data", "")
    domain = _extract_domain(tab.get("url"))
    bytes_len = len(base64.b64decode(data)) if data else 0
    return _attach_trace({
        "ok": bool(data), "url": tab.get("url",""), "format": "png", "base64": data, "bytes": bytes_len,
        "domain": domain,
        "event": _event("browser_screenshot", domain=domain, bytes=bytes_len),
    })


def browser_wait(args: dict) -> dict:
    ms: int = int(args.get("ms", 1000))
    ms = min(ms, 30000)
    time.sleep(ms / 1000)
    return {"ok": True, "waited_ms": ms}


def browser_eval(args: dict) -> dict:
    js: str = args["js"]
    hint: str | None = args.get("tab_url_hint")
    timeout: float = float(args.get("timeout_sec", 15))

    tab = _find_tab(hint)
    if tab is None:
        return {"ok": False, "error": "No matching tab found"}

    result = _eval(tab, js, timeout=timeout)
    domain = _extract_domain(tab.get("url"))
    return_type = type(result).__name__ if result is not None else "None"
    return _attach_trace({
        "ok": True, "url": tab.get("url",""), "result": result,
        "domain": domain,
        "event": _event("browser_eval", domain=domain, return_type=return_type,
                        result_size=(len(result) if isinstance(result, (str, list, dict)) else None)),
    })


def browser_close_tab(args: dict) -> dict:
    hint: str = args["tab_url_hint"]
    tab = _find_tab(hint)
    if tab is None:
        return {"ok": False, "error": f"No tab matching '{hint}'"}

    version = _http_json("/json/version")
    ws_url = version["webSocketDebuggerUrl"]
    tab_id = tab.get("id")
    _cdp(ws_url, [{"id": 1, "method": "Target.closeTarget", "params": {"targetId": tab_id}}])
    lease_id = _mcp_tab_leases.get(tab_id, "")
    if lease_id and _host_steward_release_lease(lease_id):
        _mcp_created_tabs.pop(tab_id, None)
        _mcp_tab_leases.pop(tab_id, None)
    domain = _extract_domain(tab.get("url"))
    return _attach_trace({
        "ok": True, "closed_tab_id": tab_id, "url": tab.get("url",""),
        "domain": domain,
        "event": _event("browser_close_tab", domain=domain, tab_id=tab_id),
    })


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="browser_health",
        description="Check CDP connectivity to Brave and list open tabs. Call this first to confirm browser automation is available.",
        input_schema={"type": "object", "properties": {}},
        handler=browser_health,
    ),
    Tool(
        name="browser_navigate",
        description=(
            "Navigate a browser tab to a URL. If tab_url_hint is given, finds the tab whose URL contains that string. "
            "If no matching tab exists, creates a new one. Set new_tab=true to always open a new tab. "
            "wait_ms (default 2000) controls how long to wait after navigation."
        ),
        input_schema={
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "description": "Full URL to navigate to"},
                "tab_url_hint": {"type": "string", "description": "Substring to match against current tab URLs"},
                "new_tab": {"type": "boolean", "description": "Always open a new tab (default false)"},
                "wait_ms": {"type": "integer", "description": "Ms to wait after navigation (default 2000)"},
            },
        },
        handler=browser_navigate,
    ),
    Tool(
        name="browser_click",
        description=(
            "Click an element in the browser by CSS selector. Use tab_url_hint to target a specific tab. "
            "Returns ok:true if element was found and clicked."
        ),
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element to click"},
                "tab_url_hint": {"type": "string", "description": "Substring to match against tab URL"},
                "wait_ms": {"type": "integer", "description": "Ms to wait after click (default 500)"},
            },
        },
        handler=browser_click,
    ),
    Tool(
        name="browser_type",
        description=(
            "Type text into a form field by CSS selector. Sets value and fires input/change events. "
            "clear_first=true (default) clears existing value before typing."
        ),
        input_schema={
            "type": "object",
            "required": ["selector", "text"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the input element"},
                "text": {"type": "string", "description": "Text to type"},
                "tab_url_hint": {"type": "string", "description": "Substring to match against tab URL"},
                "clear_first": {"type": "boolean", "description": "Clear field before typing (default true)"},
            },
        },
        handler=browser_type,
    ),
    Tool(
        name="browser_get_text",
        description=(
            "Get visible text from a browser tab. If selector is given, returns text of that element only. "
            "Otherwise returns full page body text (up to 50k chars)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tab_url_hint": {"type": "string", "description": "Substring to match against tab URL"},
                "selector": {"type": "string", "description": "Optional CSS selector to scope to a specific element"},
            },
        },
        handler=browser_get_text,
    ),
    Tool(
        name="browser_get_html",
        description=(
            "Get HTML from a browser tab. If selector is given, returns outerHTML of that element. "
            "Otherwise returns full page HTML (up to 100k chars). Useful for reading form structures or dashboard content."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tab_url_hint": {"type": "string", "description": "Substring to match against tab URL"},
                "selector": {"type": "string", "description": "Optional CSS selector to scope to a specific element"},
            },
        },
        handler=browser_get_html,
    ),
    Tool(
        name="browser_screenshot",
        description=(
            "Take a PNG screenshot of a browser tab. Returns base64-encoded image. "
            "Use tab_url_hint to target a specific tab."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tab_url_hint": {"type": "string", "description": "Substring to match against tab URL"},
            },
        },
        handler=browser_screenshot,
    ),
    Tool(
        name="browser_wait",
        description="Wait N milliseconds (max 30000). Use between actions when a page needs time to load or render.",
        input_schema={
            "type": "object",
            "properties": {
                "ms": {"type": "integer", "description": "Milliseconds to wait (default 1000, max 30000)"},
            },
        },
        handler=browser_wait,
    ),
    Tool(
        name="browser_eval",
        description=(
            "Evaluate arbitrary JavaScript in a browser tab and return the result. "
            "Supports async/await via awaitPromise. Use for complex DOM inspection or interactions "
            "not covered by other tools."
        ),
        input_schema={
            "type": "object",
            "required": ["js"],
            "properties": {
                "js": {"type": "string", "description": "JavaScript expression to evaluate"},
                "tab_url_hint": {"type": "string", "description": "Substring to match against tab URL"},
                "timeout_sec": {"type": "number", "description": "Seconds to wait for result (default 15)"},
            },
        },
        handler=browser_eval,
    ),
    Tool(
        name="browser_close_tab",
        description="Close a browser tab identified by a URL substring hint.",
        input_schema={
            "type": "object",
            "required": ["tab_url_hint"],
            "properties": {
                "tab_url_hint": {"type": "string", "description": "Substring to match against tab URL"},
            },
        },
        handler=browser_close_tab,
    ),
    Tool(
        name="browser_strategy_read",
        description=(
            "Read the per-domain strategy.md memory for a given domain. "
            "Returns markdown with sections 'What works', 'Known endpoints', "
            "'Selectors that resolve', 'Pitfalls'. Empty string if no strategy "
            "exists yet. Strategy memory is auto-attached to browser_navigate "
            "results when a strategy file exists, but you can also fetch it "
            "explicitly to plan a multi-step interaction."
        ),
        input_schema={
            "type": "object",
            "required": ["domain"],
            "properties": {
                "domain": {"type": "string", "description": "Domain (e.g. craigslist.org). www. is stripped automatically."},
            },
        },
        handler=lambda args: {
            "ok": True,
            "domain": args.get("domain", ""),
            "strategy_memory": _read_strategy(args.get("domain", "")),
        },
    ),
    Tool(
        name="browser_strategy_append",
        description=(
            "Append an observation to the per-domain strategy.md under one of "
            "the named sections. Creates strategy.md from template on first call. "
            "Use this AFTER a successful browser interaction to record what "
            "worked: a CSS selector that resolved, an endpoint discovered via "
            "network panel, an anti-bot pitfall, etc. Future browser_navigate "
            "calls to this domain will surface the accumulated memory."
        ),
        input_schema={
            "type": "object",
            "required": ["domain", "observation"],
            "properties": {
                "domain": {"type": "string", "description": "Domain (e.g. craigslist.org). www. is stripped automatically."},
                "observation": {"type": "string", "description": "Single-line observation, max 500 chars. Be specific (selector, endpoint, exact behavior)."},
                "section": {
                    "type": "string",
                    "description": "Which section to append under. Defaults to 'What works'.",
                    "enum": ["What works", "Known endpoints", "Selectors that resolve", "Pitfalls"],
                },
            },
        },
        handler=lambda args: (lambda ok, reason: {"ok": ok, "domain": args.get("domain", ""), "section": args.get("section", "What works"), "reason": reason})(*_append_strategy(args.get("domain", ""), args.get("observation", ""), args.get("section", "What works"))),
    ),
    Tool(
        name="browser_strategy_status",
        description=(
            "Inspect graduation readiness for a domain. Returns use_count, success_count, "
            "thresholds (default 3 uses, 2 successes), graduation status, and SKILL.md path "
            "if already graduated. Call this before browser_skill_graduate to see if a "
            "domain is ready to promote."
        ),
        input_schema={
            "type": "object",
            "required": ["domain"],
            "properties": {
                "domain": {"type": "string", "description": "Domain to check status for."},
            },
        },
        handler=lambda args: (lambda stats, ok, reason: {
            "ok": True,
            "domain": args.get("domain", ""),
            "stats": stats,
            "thresholds": {"use_count": _GRADUATION_USE_THRESHOLD, "success_count": _GRADUATION_SUCCESS_THRESHOLD},
            "eligible_for_graduation": ok,
            "reason": reason,
            "has_strategy": bool(_read_strategy(args.get("domain", ""))),
            "has_skill": bool(_read_skill(args.get("domain", ""))),
        })(_read_stats(args.get("domain", "")), *_is_eligible_for_graduation(args.get("domain", ""))),
    ),
    Tool(
        name="browser_skill_graduate",
        description=(
            "Promote a domain's strategy.md scratchpad into a graduated SKILL.md. "
            "Eligibility: use_count >= 3 AND success_count >= 2 (configurable). "
            "Use force=true to graduate immediately when you know the strategy is "
            "complete (e.g. after a thorough first-run distillation). Once graduated, "
            "future browser_navigate calls will surface skill_memory INSTEAD of "
            "strategy_memory — strategy.md remains mutable for new observations but "
            "the skill is the canonical record."
        ),
        input_schema={
            "type": "object",
            "required": ["domain"],
            "properties": {
                "domain": {"type": "string", "description": "Domain to graduate (matches strategy.md filename)."},
                "force": {"type": "boolean", "description": "Skip eligibility thresholds (default false). Use only when you've manually verified the strategy is mature."},
            },
        },
        handler=lambda args: (lambda ok, reason, path: {
            "ok": ok,
            "domain": args.get("domain", ""),
            "reason": reason,
            "skill_path": path,
        })(*_graduate_skill(args.get("domain", ""), force=bool(args.get("force", False)))),
    ),
    Tool(
        name="browser_skill_helper_list",
        description=(
            "List deterministic helpers defined for a graduated browser skill. "
            "Returns each helper's name, signature, and docstring. Helpers are "
            "Python functions in ~/.hermes/data/browser-skills/<domain>/helpers.py "
            "that the agent can invoke directly via browser_skill_helper_call to "
            "bypass live browser interaction (the Browserbase 'subsequent runs cheap' insight)."
        ),
        input_schema={
            "type": "object",
            "required": ["domain"],
            "properties": {
                "domain": {"type": "string", "description": "Domain (must already have a graduated skill)."},
            },
        },
        handler=lambda args: {
            "ok": True,
            "domain": args.get("domain", ""),
            "helpers": _list_helpers(args.get("domain", "")),
        },
    ),
    Tool(
        name="browser_skill_helper_write",
        description=(
            "Write or replace a deterministic helper function in a graduated browser skill. "
            "Provide just the function body (NOT the def line) — the system wraps it as "
            "`def <helper_name>(*args, **kwargs):`. Helpers should use only stdlib + json + "
            "urllib for portability. Use this AFTER you've discovered an endpoint or pattern "
            "via browser exploration that would benefit from deterministic re-execution. "
            "Idempotent: writing the same helper_name replaces the existing function."
        ),
        input_schema={
            "type": "object",
            "required": ["domain", "helper_name", "code"],
            "properties": {
                "domain": {"type": "string", "description": "Domain (must already have a graduated skill)."},
                "helper_name": {"type": "string", "description": "Snake-case function name, max 62 chars."},
                "code": {"type": "string", "description": "Function body — everything after the def line. Will be auto-indented."},
                "docstring": {"type": "string", "description": "One-line docstring describing what the helper does."},
            },
        },
        handler=lambda args: (lambda ok, reason: {
            "ok": ok,
            "domain": args.get("domain", ""),
            "helper_name": args.get("helper_name", ""),
            "reason": reason,
        })(*_write_helper(args.get("domain", ""), args.get("helper_name", ""), args.get("code", ""), args.get("docstring", ""))),
    ),
    Tool(
        name="browser_skill_helper_call",
        description=(
            "Invoke a deterministic helper from a graduated browser skill, passing kwargs "
            "to the function. Returns the function's return value. Helpers run in the MCP "
            "subprocess so they can use stdlib + urllib.request directly. Use this for routine "
            "interactions where you've already established what works — avoids the cost of "
            "live browser exploration."
        ),
        input_schema={
            "type": "object",
            "required": ["domain", "helper_name"],
            "properties": {
                "domain": {"type": "string", "description": "Domain (must have helpers.py with the named function)."},
                "helper_name": {"type": "string", "description": "Helper to invoke."},
                "kwargs": {"type": "object", "description": "Keyword arguments to pass to the helper. Optional.", "additionalProperties": True},
            },
        },
        handler=lambda args: _call_helper(args.get("domain", ""), args.get("helper_name", ""), args.get("kwargs") or {}),
    ),
    Tool(
        name="browser_strategy_distill",
        description=(
            "Pull recent browser interaction traces for a domain so you can synthesize "
            "what worked into strategy.md. Returns the current strategy_memory plus a "
            "list of recent traces (selectors that resolved/missed, navigations, eval "
            "shapes). After reading the traces, call browser_strategy_append for each "
            "concrete observation worth keeping (selectors that consistently resolve, "
            "endpoints discovered, pitfalls). Use this at the end of a successful "
            "multi-step browser task — first run is exploration, distill turns it into "
            "permanent memory so subsequent runs are fast."
        ),
        input_schema={
            "type": "object",
            "required": ["domain"],
            "properties": {
                "domain": {"type": "string", "description": "Domain to pull traces for."},
                "since_minutes": {"type": "integer", "description": "Time window in minutes (default 60, min 1, max 1440)."},
                "limit": {"type": "integer", "description": "Max traces to return (default 50, max 500)."},
            },
        },
        handler=lambda args: (lambda traces: {
            "ok": True,
            "domain": args.get("domain", ""),
            "trace_count": len(traces),
            "traces": traces,
            "current_strategy": _read_strategy(args.get("domain", "")),
            "distill_prompt": (
                "Review the traces above. For each pattern that recurs (selector that "
                "consistently resolves, endpoint that returned data, navigation that "
                "succeeded), call browser_strategy_append with a concrete one-line "
                "observation. Use 'Selectors that resolve' for CSS selectors, "
                "'Known endpoints' for URLs/APIs, 'What works' for general patterns, "
                "and 'Pitfalls' for things that failed or required workarounds. "
                "Skip noise; record only what would help on the next visit."
            ),
        })(_fetch_traces(args.get("domain", ""), int(args.get("since_minutes") or 60), int(args.get("limit") or 50))),
    ),
]

if __name__ == "__main__":
    serve("browser", "1.0.0", TOOLS)
