"""Read-only client for styles.refero.design.

Pull 1-3 styles for ideation. Never write DESIGN.md into a project.
Never clone a named product as a client brand.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = os.environ.get("REFERO_API_BASE", "https://styles.refero.design").rstrip("/")
CACHE_DIR = Path(os.environ.get("REFERO_CACHE_DIR") or (Path.home() / ".hermes" / "cache" / "refero-styles")).expanduser()
CACHE_TTL_S = int(os.environ.get("REFERO_CACHE_TTL_S") or 86400)
USER_AGENT = os.environ.get("REFERO_USER_AGENT", "BotDoctor-refero-styles/1.0")
MAX_PAGES = int(os.environ.get("REFERO_MAX_PAGES") or 12)
PAGE_GAP_S = float(os.environ.get("REFERO_PAGE_GAP_S") or 0.25)
REQUEST_TIMEOUT_S = int(os.environ.get("REFERO_TIMEOUT_S") or 20)


class ReferoError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _read_cache(name: str) -> Any | None:
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    ts = payload.get("cached_at")
    if not isinstance(ts, (int, float)) or (_now() - ts) > CACHE_TTL_S:
        return None
    return payload.get("data")


def _write_cache(name: str, data: Any) -> None:
    path = _cache_path(name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"cached_at": _now(), "data": data}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ReferoError(f"Refero HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ReferoError(f"Refero unreachable: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReferoError(f"Refero returned non-JSON for {url}") from exc


def _host_from_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _card(style: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    url = str(style.get("url") or "")
    fr_raw = style.get("fullResult")
    fr = fr_raw if isinstance(fr_raw, dict) else {}
    ds_raw = fr.get("designSystem")
    ds = ds_raw if isinstance(ds_raw, dict) else {}
    north = style.get("northStar") or ds.get("northStar")
    card = {
        "id": style.get("id"),
        "siteName": style.get("siteName"),
        "url": url,
        "hostname": _host_from_url(url),
        "colorScheme": style.get("colorScheme") or ds.get("theme"),
        "industry": style.get("industry") or ds.get("industry"),
        "northStar": north,
        "fonts": style.get("fonts"),
    }
    if extra:
        card.update(extra)
    return card


def refresh_catalog() -> dict[str, Any]:
    for path in CACHE_DIR.glob("*") if CACHE_DIR.exists() else []:
        if path.is_file():
            path.unlink()
    catalog = list_catalog(force=True)
    return {"ok": True, "styles": len(catalog), "cache_dir": str(CACHE_DIR)}


def list_catalog(force: bool = False) -> list[dict[str, Any]]:
    cached = None if force else _read_cache("catalog.json")
    if isinstance(cached, list) and cached:
        return cached
    styles: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        payload = _get_json(f"{API_BASE}/api/styles?page={page}")
        if not isinstance(payload, dict):
            break
        batch = payload.get("styles")
        if not isinstance(batch, list) or not batch:
            break
        styles.extend([item for item in batch if isinstance(item, dict)])
        nxt = payload.get("nextPage")
        if isinstance(nxt, int) and nxt > page:
            page = nxt
        elif payload.get("nextCursor") or (isinstance(nxt, int) and nxt == page + 1):
            page += 1
        else:
            if len(batch) < 10:
                break
            page += 1
        if page <= MAX_PAGES:
            time.sleep(PAGE_GAP_S)
    _write_cache("catalog.json", styles)
    return styles


def search_styles(query: str, limit: int = 8) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty_query", "styles": []}
    terms = [t.lower() for t in re.split(r"\s+", q) if t]
    scored: list[tuple[int, dict[str, Any]]] = []
    for style in list_catalog():
        blob = " ".join(
            str(style.get(k) or "")
            for k in ("siteName", "northStar", "industry", "colorScheme", "url", "fonts")
        ).lower()
        score = 0
        for term in terms:
            if term in blob:
                score += 3 if term in str(style.get("siteName") or "").lower() else 1
                if term in str(style.get("northStar") or "").lower():
                    score += 2
        if score:
            scored.append((score, _card(style, {"score": score})))
    scored.sort(key=lambda row: (-row[0], str(row[1].get("siteName") or "")))
    hits = [row[1] for row in scored[: max(1, min(int(limit), 12))]]
    return {"ok": True, "query": q, "count": len(hits), "styles": hits, "note": "Pick 1-3. Client overlay still wins. Do not clone a named product."}


def _match_style(spec: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = spec.strip().lower().removeprefix("www.")
    if not needle:
        return None
    for style in catalog:
        if str(style.get("id") or "").lower() == needle:
            return style
        if str(style.get("siteName") or "").lower() == needle:
            return style
        host = _host_from_url(str(style.get("url") or ""))
        if host == needle or host.startswith(needle):
            return style
    return None


def get_style(spec: str) -> dict[str, Any]:
    spec = (spec or "").strip()
    if not spec:
        return {"ok": False, "error": "empty_spec"}
    catalog = list_catalog()
    matched = _match_style(spec, catalog)
    style_id = str((matched or {}).get("id") or spec)
    cache_name = f"style-{style_id}.json"
    cached = _read_cache(cache_name)
    if isinstance(cached, dict) and cached.get("style"):
        detail = cached
    else:
        payload = _get_json(f"{API_BASE}/api/styles/{urllib.parse.quote(style_id)}")
        if not isinstance(payload, dict) or not isinstance(payload.get("style"), dict):
            return {"ok": False, "error": "not_found", "spec": spec}
        detail = {"style": payload["style"], "similar": payload.get("similar") or []}
        _write_cache(cache_name, detail)
    style = detail["style"]
    ds = ((style.get("fullResult") or {}) if isinstance(style.get("fullResult"), dict) else {}).get("designSystem") or {}
    similar = []
    for item in detail.get("similar") or []:
        if isinstance(item, dict):
            similar.append(_card(item))
    return {
        "ok": True,
        "style": _card(style),
        "designSystem": ds if isinstance(ds, dict) else {},
        "similar": similar[:8],
        "note": "Extract tokens and do/don't. Do not save this as the client brand system.",
    }


def similar_styles(spec: str, limit: int = 5) -> dict[str, Any]:
    got = get_style(spec)
    if not got.get("ok"):
        return got
    hits = got.get("similar") or []
    return {"ok": True, "spec": spec, "count": min(len(hits), int(limit)), "styles": hits[: max(1, min(int(limit), 8))]}


def _md_list(title: str, values: list[Any]) -> list[str]:
    lines = [f"## {title}", ""]
    for item in values:
        if isinstance(item, str) and item.strip():
            lines.append(f"- {item.strip()}")
        elif isinstance(item, dict):
            name = item.get("name") or item.get("role") or item.get("title") or item.get("family") or "item"
            extra = item.get("hex") or item.get("size") or item.get("family") or item.get("content") or item.get("why") or ""
            lines.append(f"- {name}: {extra}".rstrip(": "))
    lines.append("")
    return lines


def render_design_md(spec: str) -> dict[str, Any]:
    got = get_style(spec)
    if not got.get("ok"):
        return got
    style = got["style"]
    ds = got.get("designSystem") or {}
    site = style.get("siteName") or spec
    lines = [
        f"# DESIGN.md — {site} (Refero reference)",
        "",
        "This is a directional reference, not a client brand file. Client overlay wins.",
        "Do not copy this file into a client repo as the source of truth.",
        "Do not clone this named product as the client identity.",
        "",
        f"- Site: {style.get('url') or ''}",
        f"- Style id: {style.get('id') or ''}",
        f"- Color scheme: {style.get('colorScheme') or ds.get('theme') or ''}",
        f"- Industry: {style.get('industry') or ds.get('industry') or ''}",
        f"- North star: {style.get('northStar') or ds.get('northStar') or ''}",
        "",
    ]
    desc = ds.get("description")
    if desc:
        lines.extend(["## Description", "", str(desc).strip(), ""])
    colors = ds.get("colors") if isinstance(ds.get("colors"), list) else []
    if colors:
        lines.extend(["## Colors", ""])
        lines.append("| Name | Hex | Role |")
        lines.append("| --- | --- | --- |")
        for color in colors:
            if not isinstance(color, dict):
                continue
            lines.append(f"| {color.get('name', '')} | {color.get('hex', '')} | {str(color.get('role', '')).replace('|', '/')} |")
        lines.append("")
    typefaces = ds.get("typography") if isinstance(ds.get("typography"), list) else []
    if typefaces:
        lines.extend(["## Typography", ""])
        for face in typefaces:
            if not isinstance(face, dict):
                continue
            lines.append(f"- {face.get('family', 'typeface')} ({face.get('weight', '')}): {face.get('role', '')}")
            if face.get("substitute"):
                lines.append(f"  - substitute: {face.get('substitute')}")
        lines.append("")
    scale = ds.get("typeScale") if isinstance(ds.get("typeScale"), list) else []
    if scale:
        lines.extend(["## Type scale", ""])
        for row in scale:
            if isinstance(row, dict):
                lines.append(f"- {row.get('role')}: {row.get('size')}px / lh {row.get('lineHeight')}")
        lines.append("")
    spacing = ds.get("spacing") if isinstance(ds.get("spacing"), dict) else {}
    if spacing:
        lines.extend(["## Spacing", "", "```json", json.dumps(spacing, indent=2), "```", ""])
    if isinstance(ds.get("dos"), list) and ds["dos"]:
        lines.extend(_md_list("Do", ds["dos"]))
    if isinstance(ds.get("donts"), list) and ds["donts"]:
        lines.extend(_md_list("Don't", ds["donts"]))
    components = ds.get("components") if isinstance(ds.get("components"), list) else []
    if components:
        lines.extend(["## Components", ""])
        for comp in components[:12]:
            if isinstance(comp, dict):
                lines.append(f"- {comp.get('name')}: {comp.get('role') or comp.get('description') or ''}")
        lines.append("")
    markdown = "\n".join(lines).rstrip() + "\n"
    return {
        "ok": True,
        "spec": spec,
        "siteName": site,
        "id": style.get("id"),
        "markdown": markdown,
        "wrote_file": False,
        "note": "Returned in-session only. Not written to disk.",
    }


def status() -> dict[str, Any]:
    catalog = _read_cache("catalog.json")
    count = len(catalog) if isinstance(catalog, list) else 0
    return {
        "ok": True,
        "api_base": API_BASE,
        "cache_dir": str(CACHE_DIR),
        "catalog_cached": count,
        "write_enabled": False,
    }
