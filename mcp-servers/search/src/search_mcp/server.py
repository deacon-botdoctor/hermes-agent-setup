"""search MCP — Hermes's one comprehensive research surface.

Bundles, behind a single server, what used to be five servers:
  - web-search / deep-search  → free SearXNG keyword search + Firecrawl fetch
  - exa                       → paid neural/semantic escalation
  - wayback                   → archive-and-read for login-walled pages
  - verification              → primary-source fetch + immutable audit trail
plus the state-externalizing research harness (candidate pool, dedupe,
quality-curation, verification records, durable artifacts).

Design:
  * FREE-FIRST. The free local stack handles most work. Exa costs credits and
    is selected per call via `mode` ("free" | "exa" | "auto").
  * HYBRID API. Simple one-shot tools for quick lookups, plus an optional
    deep research SESSION (research_*) that runs every result through the
    harness so the agent curates quality vs. low-value sources (without
    discarding them) and verifies claims before reporting.

Env (all optional, sensible local defaults):
  SEARXNG_URL    default http://127.0.0.1:8888
  FIRECRAWL_URL  default http://127.0.0.1:3002
  EXA_API_KEY    required only for mode="exa"/auto-escalation
  HERMES_HOME    default ~/.hermes  (sessions + audit log live under state/)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer as FastMCP

from .harness import ResearchHarness, ResearchTask, HarnessLimits

logging.getLogger("httpx").setLevel(logging.WARNING)

SERVER_NAME = "search"
VERSION = "1.0.0"
mcp = FastMCP(SERVER_NAME)

# ── Backends / config ───────────────────────────────────────────────────────
SEARXNG = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")
FIRECRAWL = os.environ.get("FIRECRAWL_URL", "http://127.0.0.1:3002")
EXA_BASE = "https://api.exa.ai"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
SESSION_ROOT = HERMES_HOME / "state" / "search-sessions"
AUDIT_LOG = HERMES_HOME / "state" / "verification-log.jsonl"
_SHARED_FETCH_DIR = Path(__file__).resolve().parents[3] / "shared"
_RUNTIME_FETCH_DIR = HERMES_HOME / "lib"
for _fetch_dir in (_RUNTIME_FETCH_DIR, _SHARED_FETCH_DIR):
    if _fetch_dir.exists() and str(_fetch_dir) not in sys.path:
        sys.path.insert(0, str(_fetch_dir))
from hermes_resilient_fetch import looks_walled as _shared_looks_walled, resilient_scrape

_UA = "Hermes-Search/1.0 (+botdoctor.io research)"
_SEARCH_TIMEOUT = 25.0
_SEARXNG_RETRIES = 3       # retry transient empty result sets
_SEARXNG_BACKOFF = 0.6     # seconds, multiplied by attempt number
_SCRAPE_TIMEOUT = 50.0
_SCRAPE_RETRIES = 2
_EXA_TIMEOUT = 45.0
_FETCH_TIMEOUT = 30.0
_MAX_FETCH_BYTES = 2_000_000

# When mode="auto", escalate to Exa if the free pass yields fewer than this many
# usable (non-walled, has-content) pages.
_AUTO_ESCALATE_MIN_USABLE = 2


def _exa_key() -> str | None:
    return os.environ.get("EXA_API_KEY") or None


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Free stack: SearXNG + Firecrawl ─────────────────────────────────────────
def _searxng(query: str, limit: int) -> list[dict]:
    # SearXNG intermittently returns an empty result set for a valid query
    # (upstream engine rate-limits/timeouts), even on a 200. Retry empties a few
    # times with light backoff; only raise if every attempt errored outright.
    params = {"q": query, "format": "json", "pageno": 1}
    results: list[dict] = []
    last_exc: Exception | None = None
    for attempt in range(_SEARXNG_RETRIES):
        try:
            with httpx.Client(timeout=_SEARCH_TIMEOUT, headers={"User-Agent": _UA}) as c:
                r = c.get(f"{SEARXNG}/search", params=params)
                r.raise_for_status()
                data = r.json()
            results = list(data.get("results") or [])
            if results:
                break
        except Exception as exc:
            last_exc = exc
        if attempt < _SEARXNG_RETRIES - 1:
            time.sleep(_SEARXNG_BACKOFF * (attempt + 1))
    if not results and last_exc is not None:
        raise last_exc
    out = []
    for item in results[:limit]:
        out.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "engine": item.get("engine", ""),
        })
    return out


def _firecrawl_scrape_once(url: str) -> dict[str, Any]:
    payload = {"url": url, "formats": ["markdown"]}
    last_err = None
    for _ in range(_SCRAPE_RETRIES + 1):
        try:
            with httpx.Client(timeout=_SCRAPE_TIMEOUT, headers={"User-Agent": _UA}) as c:
                r = c.post(f"{FIRECRAWL}/v1/scrape", json=payload)
                d = r.json()
            if d.get("success") and d.get("data"):
                page = d["data"]
                md = page.get("markdown") or ""
                return {"ok": True, "url": url, "chars": len(md), "markdown": md,
                        "metadata": page.get("metadata") or {}}
            last_err = d.get("error", "scrape unsuccessful")
        except Exception as exc:
            last_err = str(exc)[:200]
    return {"ok": False, "url": url, "error": last_err}


def _firecrawl_scrape(url: str) -> dict[str, Any]:
    return resilient_scrape(
        url,
        lambda: _firecrawl_scrape_once(url),
        timeout_sec=int(_SCRAPE_TIMEOUT),
    )


def _looks_walled(text: str) -> bool:
    return _shared_looks_walled(text)


# ── Paid stack: Exa ─────────────────────────────────────────────────────────
def _exa_search(query: str, num_results: int, include_text: bool, max_chars: int, category: str) -> dict[str, Any]:
    key = _exa_key()
    if not key:
        return {"ok": False, "error": "EXA_API_KEY not configured on this runtime"}
    num_results = max(1, min(int(num_results or 6), 20))
    payload: dict[str, Any] = {"query": query, "numResults": num_results, "type": "auto"}
    if category.strip():
        payload["category"] = category.strip()
    if include_text:
        payload["contents"] = {"text": {"maxCharacters": max(200, int(max_chars))}}
    try:
        with httpx.Client(timeout=_EXA_TIMEOUT) as c:
            r = c.post(f"{EXA_BASE}/search",
                       headers={"x-api-key": key, "Content-Type": "application/json"},
                       json=payload)
            if r.status_code != 200:
                return {"ok": False, "http_status": r.status_code, "error": r.text[:300]}
            data = r.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    results = []
    for x in data.get("results", []):
        results.append({
            "title": x.get("title"), "url": x.get("url"),
            "published_date": x.get("publishedDate"), "author": x.get("author"),
            "score": x.get("score"), "text": (x.get("text") or None),
        })
    return {"ok": True, "results": results}


def _exa_contents(urls: list[str], max_chars: int) -> dict[str, Any]:
    key = _exa_key()
    if not key:
        return {"ok": False, "error": "EXA_API_KEY not configured on this runtime"}
    urls = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
    if not urls:
        return {"ok": False, "error": "no urls provided"}
    payload = {"urls": urls[:10], "text": {"maxCharacters": max(200, int(max_chars))}, "livecrawl": "fallback"}
    try:
        with httpx.Client(timeout=_EXA_TIMEOUT) as c:
            r = c.post(f"{EXA_BASE}/contents",
                       headers={"x-api-key": key, "Content-Type": "application/json"},
                       json=payload)
            if r.status_code != 200:
                return {"ok": False, "http_status": r.status_code, "error": r.text[:300]}
            data = r.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    out = [{"url": x.get("url"), "title": x.get("title"), "text": x.get("text")}
           for x in data.get("results", [])]
    return {"ok": True, "results": out}


def _exa_find_similar(url: str, num_results: int) -> dict[str, Any]:
    key = _exa_key()
    if not key:
        return {"ok": False, "error": "EXA_API_KEY not configured on this runtime"}
    num_results = max(1, min(int(num_results or 6), 20))
    try:
        with httpx.Client(timeout=_EXA_TIMEOUT) as c:
            r = c.post(f"{EXA_BASE}/findSimilar",
                       headers={"x-api-key": key, "Content-Type": "application/json"},
                       json={"url": url.strip(), "numResults": num_results})
            if r.status_code != 200:
                return {"ok": False, "http_status": r.status_code, "error": r.text[:300]}
            data = r.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    results = [{"title": x.get("title"), "url": x.get("url"), "score": x.get("score")}
               for x in data.get("results", [])]
    return {"ok": True, "results": results}


# ── Archive + mechanical fetch ──────────────────────────────────────────────
def _archive_and_read(url: str) -> dict[str, Any]:
    archive_url = None
    text = None
    try:
        with httpx.Client(timeout=_SCRAPE_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True) as c:
            save = c.get(f"https://web.archive.org/save/{url.strip()}")
            cl = save.headers.get("content-location")
            if cl:
                archive_url = "https://web.archive.org" + cl
            if not archive_url:
                avail = c.get("https://archive.org/wayback/available", params={"url": url})
                snap = (avail.json().get("archived_snapshots") or {}).get("closest") or {}
                archive_url = snap.get("url")
            if archive_url:
                r = c.get(archive_url)
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))[:4000]
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)[:200], "archive_url": archive_url}
    return {"ok": bool(archive_url), "url": url, "archive_url": archive_url, "text": text}


def _mechanical_fetch(url: str, timeout: int) -> dict[str, Any]:
    timeout = float(max(1, min(int(timeout or 30), 60)))
    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True) as c:
            r = c.get(url)
            raw = r.content[:_MAX_FETCH_BYTES]
            try:
                text = raw.decode(r.encoding or "utf-8", errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
            return {"ok": r.status_code < 400, "url": str(r.url), "http_status": r.status_code,
                    "content_type": r.headers.get("content-type", ""), "byte_count": len(r.content),
                    "truncated": len(r.content) > _MAX_FETCH_BYTES, "content": text}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)[:300]}


# ── Session helpers (deep research mode) ────────────────────────────────────
def _session_dir(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", session_id or "")
    if not safe:
        raise ValueError("invalid session_id")
    return SESSION_ROOT / safe


def _state_path(session_id: str) -> Path:
    return _session_dir(session_id) / "state.json"


def _load(session_id: str) -> ResearchHarness:
    return ResearchHarness.load_state(_state_path(session_id))


def _audit_append(record: dict[str, Any]) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# =============================================================================
# SIMPLE (stateless) TOOLS — quick lookups
# =============================================================================
@mcp.tool()
def search(
    query: str,
    mode: str = "auto",
    num_results: int = 8,
    scrape_top: int = 3,
    max_chars: int = 4000,
    category: str = "",
) -> dict[str, Any]:
    """One-shot research search: find ranked results AND auto-read the top pages.

    This is the default search tool. It returns real page CONTENT, not just
    snippets, so prefer it over a bare snippet search when you need to read.

    mode:
      - "free" (default behaviour, no cost): SearXNG keyword search + Firecrawl
        page fetch of the top hits. Use for almost everything.
      - "exa" (PAID, costs credits): Exa neural/semantic search with clean text
        extraction. Use only when free recall is poor (obscure person, niche
        org), you need "find pages that mean X" semantics, or the free scraper
        chokes on a page.
      - "auto": run FREE first; only if it yields too few usable pages does it
        escalate to Exa (requires EXA_API_KEY). Free-first by design.

    Args:
        query: Natural-language or keyword query.
        mode: "free" | "exa" | "auto" (default "auto", which is free-first).
        num_results: Ranked results to return (default 8, max 20).
        scrape_top: How many top results to auto-read for full text (default 3, max 6).
        max_chars: Truncate each page's text (default 4000).
        category: Optional Exa category hint (only used in exa/auto-escalation).

    Returns {mode_used, results:[{title,url,snippet}], pages:[{url,markdown,walled}], cost}.
    """
    if not query.strip():
        return {"ok": False, "error": "empty query"}
    mode = (mode or "auto").strip().lower()
    if mode not in {"free", "exa", "auto"}:
        mode = "auto"
    num_results = max(1, min(int(num_results or 8), 20))
    scrape_top = max(0, min(int(scrape_top or 3), 6))

    # Direct paid path.
    if mode == "exa":
        ex = _exa_search(query, num_results, True, max_chars, category)
        if not ex.get("ok"):
            return {"ok": False, "mode_used": "exa", "error": ex.get("error"), "http_status": ex.get("http_status")}
        results = [{"title": x["title"], "url": x["url"], "snippet": (x.get("text") or "")[:300]} for x in ex["results"]]
        pages = [{"url": x["url"], "chars": len(x.get("text") or ""), "walled": False, "markdown": (x.get("text") or "")[:max_chars]}
                 for x in ex["results"][:scrape_top] if x.get("text")]
        return {"ok": True, "query": query, "mode_used": "exa", "cost": "exa-credits",
                "result_count": len(results), "results": results, "scraped": len(pages), "pages": pages}

    # Free path (also the first half of auto).
    try:
        results = _searxng(query, num_results)
    except Exception as exc:
        if mode == "auto" and _exa_key():
            ex = _exa_search(query, num_results, True, max_chars, category)
            if ex.get("ok"):
                results = [{"title": x["title"], "url": x["url"], "snippet": (x.get("text") or "")[:300]} for x in ex["results"]]
                pages = [{"url": x["url"], "chars": len(x.get("text") or ""), "walled": False, "markdown": (x.get("text") or "")[:max_chars]}
                         for x in ex["results"][:scrape_top] if x.get("text")]
                return {"ok": True, "query": query, "mode_used": "exa(auto-escalated:searxng-down)", "cost": "exa-credits",
                        "result_count": len(results), "results": results, "scraped": len(pages), "pages": pages}
        return {"ok": False, "mode_used": "free", "error": f"search failed: {str(exc)[:200]}"}

    pages = []
    usable = 0
    for res in results[:scrape_top]:
        url = res.get("url")
        if not url:
            continue
        scraped = _firecrawl_scrape(url)
        if scraped.get("ok"):
            md = scraped["markdown"][:max_chars]
            walled = _looks_walled(md)
            if not walled and len(md) > 200:
                usable += 1
            pages.append({"url": url, "chars": scraped["chars"], "walled": walled, "markdown": md,
                          "via": scraped.get("via") or scraped.get("backend"),
                          "attempts": scraped.get("attempts", [])})
        else:
            pages.append({"url": url, "error": scraped.get("error"), "walled": None,
                          "attempts": scraped.get("attempts", []), "next": scraped.get("next")})

    # auto escalation: weak free recall -> try Exa to enrich.
    if mode == "auto" and usable < _AUTO_ESCALATE_MIN_USABLE and _exa_key():
        ex = _exa_search(query, num_results, True, max_chars, category)
        if ex.get("ok") and ex["results"]:
            seen = {p["url"] for p in pages}
            for x in ex["results"]:
                if x.get("url") and x["url"] not in seen and x.get("text"):
                    pages.append({"url": x["url"], "chars": len(x["text"]), "walled": False,
                                  "markdown": x["text"][:max_chars], "via": "exa"})
            exa_results = [{"title": x["title"], "url": x["url"], "snippet": (x.get("text") or "")[:300]} for x in ex["results"]]
            merged = results + [r for r in exa_results if r["url"] not in {rr.get("url") for rr in results}]
            return {"ok": True, "query": query, "mode_used": "auto(free+exa)", "cost": "exa-credits",
                    "result_count": len(merged), "results": merged, "scraped": len(pages), "pages": pages}

    return {"ok": True, "query": query, "mode_used": "free", "cost": "free",
            "result_count": len(results), "results": results, "scraped": len(pages), "pages": pages}


@mcp.tool()
def fetch_page(url: str, claim: str = "", prefer: str = "firecrawl", timeout_sec: int = 30) -> dict[str, Any]:
    """Fetch ONE page's content to read or verify a claim against the primary source.

    Use to read a specific URL directly instead of trusting a paraphrase.

    Args:
        url: Public page URL (must start with http:// or https://).
        claim: Optional claim being checked (echoed back for context).
        prefer: "firecrawl" (clean markdown, default) or "mechanical" (raw HTTP).
        timeout_sec: Request timeout (default 30, max 60).

    Returns content/markdown, fetch status, and whether it looks login-walled.
    """
    if not url.strip():
        return {"ok": False, "error": "empty url"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "url must start with http:// or https://"}
    if (prefer or "firecrawl").lower() == "firecrawl":
        sc = _firecrawl_scrape(url)
        if sc.get("ok"):
            md = sc["markdown"]
            return {"ok": True, "url": url, "via": sc.get("via") or sc.get("backend"), "chars": sc["chars"],
                    "walled": _looks_walled(md), "claim": claim or None, "content": md,
                    "attempts": sc.get("attempts", [])}
        attempts = sc.get("attempts", [])
        challenge_blocked = sc.get("next") == "captcha_human" or any(
            attempt.get("status") == "walled" for attempt in attempts
        )
        if challenge_blocked:
            return {
                "ok": False,
                "url": url,
                "via": sc.get("via") or "resilient_scrape",
                "error": sc.get("error") or "resilient scrape unavailable",
                "attempts": attempts,
                "next": sc.get("next"),
                "claim": claim or None,
            }
        # fall through to mechanical on plain transport/service failure
    mech = _mechanical_fetch(url, timeout_sec)
    mech["via"] = "mechanical"
    if claim:
        mech["claim"] = claim
    return mech


@mcp.tool()
def find_social_handles(name: str, context: str = "") -> dict[str, Any]:
    """Enumerate a person's social/web presence via targeted free searches.

    Runs a multi-query sweep and groups hits by platform (x/twitter, instagram,
    facebook, linkedin, youtube, site/blog). Snippets often contain the bio or
    affiliation verbatim. For login-walled profiles (X), follow with archive_and_read.

    Args:
        name: Subject's full name.
        context: Optional disambiguator (city, party, org, employer).
    """
    if not name.strip():
        return {"ok": False, "error": "empty name"}
    ctx = context.strip()
    nq = f'"{name}"'
    queries = [nq, f"{nq} twitter", f"{nq} X.com"]
    if ctx:
        queries += [f"{nq} {ctx}", f"{nq} {ctx} twitter", f"{nq} {ctx} X.com",
                    f"{nq} {ctx} instagram", f"{nq} {ctx} linkedin"]
    seen: dict[str, dict] = {}
    for q in queries:
        try:
            for res in _searxng(q, 10):
                url = res.get("url", "")
                if url and url not in seen:
                    seen[url] = res
        except Exception:
            continue
    groups: dict[str, list] = {"x_twitter": [], "instagram": [], "facebook": [],
                               "linkedin": [], "youtube": [], "site_or_blog": []}
    for url, res in seen.items():
        low = url.lower()
        entry = {"url": url, "title": res.get("title", ""), "snippet": res.get("snippet", "")}
        if "x.com" in low or "twitter.com" in low:
            groups["x_twitter"].append(entry)
        elif "instagram.com" in low:
            groups["instagram"].append(entry)
        elif "facebook.com" in low:
            groups["facebook"].append(entry)
        elif "linkedin.com" in low:
            groups["linkedin"].append(entry)
        elif "youtube.com" in low or "youtu.be" in low:
            groups["youtube"].append(entry)
        else:
            groups["site_or_blog"].append(entry)
    total = sum(len(v) for v in groups.values())
    return {"ok": True, "name": name, "context": ctx or None, "total_candidates": total, "groups": groups}


@mcp.tool()
def archive_and_read(url: str) -> dict[str, Any]:
    """For a login-walled or volatile URL: request a Wayback capture, then read it.

    Use on X/Twitter profiles and pages that block direct scraping. Creates a
    citable archive permalink and returns the archived copy's text.

    Args:
        url: The page URL to archive and read.
    """
    if not url.strip():
        return {"ok": False, "error": "empty url"}
    out = _archive_and_read(url)
    if out.get("archive_url"):
        out["note"] = "Cite archive_url as the durable permalink."
    return out


@mcp.tool()
def list_verifications(limit: int = 25) -> dict[str, Any]:
    """Return recent immutable verification audit records (newest first).

    Backs the evidence discipline that bans second-hand citation. Records are
    appended by research_verify and persist across sessions.
    """
    limit = max(1, min(int(limit or 25), 200))
    if not AUDIT_LOG.exists():
        return {"ok": True, "log_path": str(AUDIT_LOG), "count": 0, "records": []}
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    recs = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            continue
        if len(recs) >= limit:
            break
    return {"ok": True, "log_path": str(AUDIT_LOG), "count": len(recs), "records": recs}


@mcp.tool()
def search_status() -> dict[str, Any]:
    """Health of every backend (SearXNG, Firecrawl, Exa key) + version + paths."""
    out: dict[str, Any] = {"ok": True, "server": SERVER_NAME, "version": VERSION,
                           "exa_key_configured": _exa_key() is not None,
                           "session_root": str(SESSION_ROOT), "audit_log": str(AUDIT_LOG),
                           "policy": "free-first; Exa is paid and selected via mode."}
    try:
        with httpx.Client(timeout=8, headers={"User-Agent": _UA}) as c:
            out["searxng"] = c.get(f"{SEARXNG}/").status_code
    except Exception as exc:
        out["searxng_error"] = str(exc)[:120]
    try:
        with httpx.Client(timeout=8, headers={"User-Agent": _UA}) as c:
            out["firecrawl"] = c.get(f"{FIRECRAWL}/").status_code
    except Exception as exc:
        out["firecrawl_error"] = str(exc)[:120]
    return out


# =============================================================================
# DEEP RESEARCH SESSION TOOLS — harness-backed evidence management
# =============================================================================
@mcp.tool()
def research_start(
    query: str,
    lane: str = "general",
    topic: str = "general",
    client_lock: str = "",
    objective: str = "Find, curate, and verify evidence before producing a report.",
    max_candidates: int = 80,
    max_curated: int = 30,
) -> dict[str, Any]:
    """Open a deep-research SESSION backed by the evidence harness.

    Use for non-trivial research where you must gather many sources, weigh
    quality vs. low-value sources (without losing them), verify claims, and
    leave durable artifacts. Returns a session_id to pass to the other
    research_* tools. For a quick lookup, use `search` instead.

    Returns {session_id, state_path}.
    """
    if not query.strip():
        return {"ok": False, "error": "empty query"}
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    task = ResearchTask(query=query.strip(), lane=lane.strip() or "general",
                        topic=topic.strip() or "general", client_lock=client_lock.strip(),
                        objective=objective.strip())
    limits = HarnessLimits(max_candidates=max(10, int(max_candidates or 80)),
                           max_curated=max(5, int(max_curated or 30)))
    harness = ResearchHarness(task, limits)
    sp = _state_path(session_id)
    harness.write_state(sp)
    return {"ok": True, "session_id": session_id, "state_path": str(sp),
            "next": "research_search(session_id, query, mode) → research_curate → research_verify → research_report"}


@mcp.tool()
def research_search(
    session_id: str,
    query: str,
    mode: str = "auto",
    num_results: int = 8,
    scrape_top: int = 3,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """Search within a session AND auto-add every result to the candidate pool.

    Same free/exa/auto mode semantics as `search`, but results flow into the
    harness (deduped by URL + content hash). Returns the added candidate IDs and
    refreshed compact context so you can pick what to curate.

    Args:
        session_id: From research_start.
        query: Search query.
        mode: "free" | "exa" | "auto" (free-first).
    """
    try:
        harness = _load(session_id)
    except Exception as exc:
        return {"ok": False, "error": f"load session failed: {str(exc)[:160]}"}
    res = search(query=query, mode=mode, num_results=num_results, scrape_top=scrape_top, max_chars=max_chars)
    if not res.get("ok"):
        return res
    added: list[dict[str, str]] = []
    # snippet-only candidates for every ranked result
    pages_by_url = {p.get("url"): p for p in res.get("pages", []) if p.get("url")}
    for r in res.get("results", []):
        url = r.get("url")
        if not url:
            continue
        page = pages_by_url.get(url)
        if page and page.get("markdown"):
            cid, status = harness.add_web_result(
                url=url, title=r.get("title", ""), text=page["markdown"],
                source_type="web_page", fetch_status=("blocked" if page.get("walled") else "ok"),
                trust_notes=("looks login-walled" if page.get("walled") else ""))
        else:
            cid, status = harness.add_candidate(
                source_type="web_snippet", url=url, title=r.get("title", ""),
                text=r.get("snippet", ""), fetch_status="metadata_only")
        if status == "added":
            added.append({"id": cid, "url": url, "title": r.get("title", "")})
    harness.add_search_record(f"research_search[{res.get('mode_used')}]", query, len(res.get("results", [])), len(added))
    harness.write_state(_state_path(session_id))
    return {"ok": True, "session_id": session_id, "mode_used": res.get("mode_used"), "cost": res.get("cost"),
            "added_count": len(added), "added": added, "metrics": harness.metrics(),
            "context": harness.render_context(6000)}


@mcp.tool()
def research_curate(
    session_id: str,
    add_ids: list[str] | None = None,
    remove_ids: list[str] | None = None,
    importance: dict[str, str] | None = None,
    rationale: str = "",
) -> dict[str, Any]:
    """Promote/demote candidates into the curated evidence set.

    This is the quality judgement step: keep strong sources, down-rank weak
    ones (they stay in the pool, not discarded). Importance ∈
    very_high|high|fair|low. Provide a rationale so the report can justify it.

    Args:
        session_id: From research_start.
        add_ids: Candidate IDs to add/retag in the curated set.
        remove_ids: Candidate IDs to remove from the curated set (kept in pool).
        importance: Optional {candidate_id: importance} map.
        rationale: Why these are (or aren't) good sources.
    """
    try:
        harness = _load(session_id)
    except Exception as exc:
        return {"ok": False, "error": f"load session failed: {str(exc)[:160]}"}
    result = harness.curate(add_ids=add_ids or [], remove_ids=remove_ids or [],
                            importance=importance or {}, rationale=rationale)
    harness.write_state(_state_path(session_id))
    return {"ok": True, "session_id": session_id, "result": result, "metrics": harness.metrics()}


@mcp.tool()
def research_verify(
    session_id: str,
    claim: str,
    candidate_ids: list[str],
    required_terms: list[str] | None = None,
    method: str = "term-match against fetched source text",
) -> dict[str, Any]:
    """Verify a claim against specific candidates' fetched text + log an audit record.

    Marks each candidate supported/unclear/unsupported with a quote, and appends
    an immutable record to the cross-session verification audit log. Verify
    concrete claims before stating them as fact in the report.

    Args:
        session_id: From research_start.
        claim: The factual claim to check.
        candidate_ids: Candidate IDs whose text should support the claim.
        required_terms: Optional explicit terms that must appear (else derived from claim).
    """
    try:
        harness = _load(session_id)
    except Exception as exc:
        return {"ok": False, "error": f"load session failed: {str(exc)[:160]}"}
    if not claim.strip() or not candidate_ids:
        return {"ok": False, "error": "claim and candidate_ids are required"}
    rec = harness.verify(claim=claim, candidate_ids=candidate_ids, required_terms=required_terms)
    harness.write_state(_state_path(session_id))
    # immutable audit trail (cross-session)
    supported = [cid for cid, s in rec.status_by_candidate.items() if s == "supported"]
    result_word = "confirmed" if supported else ("refuted" if all(s == "unsupported" for s in rec.status_by_candidate.values()) else "unclear")
    evidence_url = ""
    if supported:
        cand = harness.state.candidates.get(supported[0])
        evidence_url = (cand.url or cand.source_uri) if cand else ""
    try:
        _audit_append({"ts": _utc(), "session_id": session_id, "claim": claim.strip(),
                       "method": method, "result": result_word,
                       "evidence_url": evidence_url or None,
                       "supported_count": rec.supported_count,
                       "checked": len(rec.status_by_candidate)})
    except Exception:
        pass
    return {"ok": True, "session_id": session_id, "claim": claim,
            "supported_count": rec.supported_count, "checked": len(rec.status_by_candidate),
            "status_by_candidate": rec.status_by_candidate, "quotes_by_candidate": rec.quotes_by_candidate}


@mcp.tool()
def research_context(session_id: str, max_chars: int = 12000) -> dict[str, Any]:
    """Render the compact harness working state (curated set, pool, verifications).

    Read this to see what you've gathered before deciding next steps or writing
    the report — instead of re-dumping raw tool output.
    """
    try:
        harness = _load(session_id)
    except Exception as exc:
        return {"ok": False, "error": f"load session failed: {str(exc)[:160]}"}
    return {"ok": True, "session_id": session_id, "metrics": harness.metrics(),
            "context": harness.render_context(max(2000, int(max_chars or 12000)))}


@mcp.tool()
def research_note(session_id: str, open_question: str) -> dict[str, Any]:
    """Record an open question / gap to resolve before the research is complete."""
    try:
        harness = _load(session_id)
    except Exception as exc:
        return {"ok": False, "error": f"load session failed: {str(exc)[:160]}"}
    harness.note_open_question(open_question)
    harness.write_state(_state_path(session_id))
    return {"ok": True, "session_id": session_id, "open_questions": harness.state.open_questions}


@mcp.tool()
def research_report(session_id: str) -> dict[str, Any]:
    """Write durable artifacts (state.json, evidence.md, report.md, manifest.json).

    Call when research is complete. Returns the artifact paths and final metrics.
    The report/evidence are built from the CURATED + VERIFIED set, so curate and
    verify before calling this.
    """
    try:
        harness = _load(session_id)
    except Exception as exc:
        return {"ok": False, "error": f"load session failed: {str(exc)[:160]}"}
    out_dir = _session_dir(session_id) / "artifacts"
    artifacts = harness.write_artifacts(out_dir)
    return {"ok": True, "session_id": session_id, "artifacts": artifacts, "metrics": harness.metrics()}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
