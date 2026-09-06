from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable


WALL_MARKERS = (
    "javascript is not available",
    "enable javascript",
    "checking your browser",
    "verify you are human",
    "captcha",
    "cloudflare",
    "access denied",
    "please enable cookies",
    "log in to continue",
    "sign in to continue",
    "please log in",
)


def looks_walled(text: str) -> bool:
    low = (text or "").lower()
    if len(low) < 600:
        return any(marker in low for marker in WALL_MARKERS)
    return any(marker in low[:1200] for marker in ("checking your browser", "verify you are human", "captcha"))


def _page_status(markdown: str) -> str:
    if not (markdown or "").strip():
        return "empty"
    if looks_walled(markdown):
        return "walled"
    return "ok"


def _obscura_path(obscura_bin: str | None = None) -> str | None:
    candidates = [
        obscura_bin,
        os.environ.get("OBSCURA_BIN"),
        str(Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser() / "bin" / "obscura"),
        shutil.which("obscura"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def obscura_fetch_markdown(
    url: str,
    *,
    timeout_sec: int = 45,
    obscura_bin: str | None = None,
) -> dict[str, Any]:
    binary = _obscura_path(obscura_bin)
    if not binary:
        return {
            "ok": False,
            "backend": "obscura",
            "status": "unavailable",
            "detail": "obscura binary not found",
        }
    timeout_sec = max(5, min(int(timeout_sec or 45), 90))
    cmd = [
        binary,
        "fetch",
        "--dump",
        "markdown",
        "--timeout",
        str(timeout_sec),
        "--wait",
        os.environ.get("OBSCURA_FETCH_WAIT", "2"),
        "--quiet",
        url,
    ]
    if os.environ.get("OBSCURA_STEALTH", "1").lower() not in {"0", "false", "no"}:
        cmd.insert(2, "--stealth")
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec + 10,
        )
    except Exception as exc:
        return {"ok": False, "backend": "obscura", "status": "error", "detail": str(exc)[:300]}
    markdown = proc.stdout or ""
    status = _page_status(markdown) if proc.returncode == 0 else "error"
    if proc.returncode == 0 and status == "ok":
        return {
            "ok": True,
            "backend": "obscura",
            "status": "ok",
            "url": url,
            "chars": len(markdown),
            "markdown": markdown,
        }
    detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}")[:300]
    return {
        "ok": False,
        "backend": "obscura",
        "status": status,
        "detail": detail,
        "chars": len(markdown),
        "markdown": markdown if markdown else None,
    }


def resilient_scrape(
    url: str,
    firecrawl_fetch: Callable[[], dict[str, Any]],
    *,
    timeout_sec: int = 45,
    obscura_bin: str | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    firecrawl: dict[str, Any]
    try:
        firecrawl = firecrawl_fetch()
    except Exception as exc:
        firecrawl = {"ok": False, "error": str(exc)[:300]}
    if firecrawl.get("ok"):
        markdown = firecrawl.get("markdown") or ""
        status = _page_status(markdown)
        attempts.append({
            "backend": "firecrawl",
            "status": status,
            "detail": f"{len(markdown)} chars",
        })
        if status == "ok":
            out = dict(firecrawl)
            out.update({"ok": True, "backend": "firecrawl", "via": "firecrawl", "attempts": attempts})
            return out
    else:
        attempts.append({
            "backend": "firecrawl",
            "status": "error",
            "detail": str(firecrawl.get("error") or firecrawl.get("detail") or "scrape unsuccessful")[:300],
        })

    obs = obscura_fetch_markdown(url, timeout_sec=timeout_sec, obscura_bin=obscura_bin)
    attempts.append({
        "backend": "obscura",
        "status": obs.get("status", "error"),
        "detail": str(obs.get("detail") or f"{obs.get('chars', 0)} chars")[:300],
    })
    if obs.get("ok"):
        obs["via"] = "obscura"
        obs["attempts"] = attempts
        return obs

    next_step = "interactive_browser"
    if any(a["status"] == "walled" for a in attempts):
        next_step = "captcha_human"
    return {"ok": False, "url": url, "attempts": attempts, "next": next_step}
