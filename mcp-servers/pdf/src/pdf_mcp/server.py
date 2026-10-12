"""pdf MCP — markdown/HTML to PDF rendering for Hermes.

Solves Deacon's recurring "WHY ARE THESE NOT DOWNLOADABLE PDFS" frustration.
Multi-backend renderer:
  1. weasyprint (preferred — highest quality, needs cairo/pango on macOS)
  2. fpdf2 (fallback — pure python, no system deps, simpler typography)

Saves PDFs to $HERMES_HOME/state/pdfs/<timestamp>-<slug>.pdf and returns the
file path. The caller (agent) attaches via Telegram or other delivery tool.

Tools:
  - render_markdown(markdown, title?, output_name?, css?) — markdown → PDF
  - render_html(html, title?, output_name?, css?)         — HTML → PDF
  - list_pdfs(limit?)                                      — list recent renders
  - delete_pdf(filename)                                   — clean up specific file
  - pdf_status()                                           — server health + backend in use
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer as FastMCP

# Silence weasyprint's verbose fontTools subsetting noise — it pollutes
# the gateway log with hundreds of DEBUG/INFO lines per render.
for _name in ("fontTools", "fontTools.subset", "fontTools.ttLib", "weasyprint"):
    logging.getLogger(_name).setLevel(logging.WARNING)

SERVER_NAME = "pdf"
mcp = FastMCP(SERVER_NAME)

DEFAULT_HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
).expanduser()
PDF_DIR = DEFAULT_HERMES_HOME / "state" / "pdfs"

DEFAULT_CSS = """
@page { size: letter; margin: 0.75in; }
body {
  font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 11pt; line-height: 1.5; color: #1a1a1a;
}
h1 { font-size: 22pt; margin: 0 0 0.5em; border-bottom: 2px solid #333; padding-bottom: 6pt; }
h2 { font-size: 16pt; margin: 1.2em 0 0.4em; color: #222; }
h3 { font-size: 13pt; margin: 1em 0 0.3em; color: #333; }
p { margin: 0.5em 0; }
ul, ol { margin: 0.5em 0; padding-left: 1.5em; }
li { margin: 0.25em 0; }
code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
       background: #f4f4f4; padding: 1pt 4pt; border-radius: 2pt; font-size: 10pt; }
pre { background: #f4f4f4; padding: 8pt; border-radius: 4pt; overflow-x: auto;
      font-size: 9.5pt; line-height: 1.35; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; margin: 0.7em 0; width: 100%; }
th, td { border: 1px solid #ccc; padding: 4pt 8pt; text-align: left; }
th { background: #f0f0f0; font-weight: 600; }
blockquote { border-left: 3px solid #888; margin: 0.7em 0; padding: 0.2em 1em; color: #555; }
a { color: #0366d6; text-decoration: none; }
hr { border: 0; border-top: 1px solid #ccc; margin: 1em 0; }
"""


def _have_weasyprint() -> bool:
    try:
        import weasyprint  # noqa: F401
        return True
    except Exception:
        return False


def _have_fpdf() -> bool:
    try:
        import fpdf  # noqa: F401
        return True
    except Exception:
        return False


def _have_markdown() -> bool:
    try:
        import markdown  # noqa: F401
        return True
    except Exception:
        return False


def _slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-").lower()
    return text[:max_len] or "untitled"


def _output_path(output_name: str | None, title: str | None) -> Path:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if output_name:
        slug = _slugify(output_name)
    elif title:
        slug = _slugify(title)
    else:
        slug = "untitled"
    if not slug.endswith(".pdf"):
        slug = slug + ".pdf"
    return PDF_DIR / f"{ts}-{slug}"


def _render_via_weasyprint(html_doc: str, out_path: Path) -> dict[str, Any]:
    from weasyprint import HTML  # type: ignore
    HTML(string=html_doc).write_pdf(str(out_path))
    return {"backend": "weasyprint"}


def _render_via_fpdf(plain_text: str, out_path: Path, title: str | None) -> dict[str, Any]:
    """Fallback renderer — strips HTML tags, renders as plain text via fpdf2.
    Quality is lower than weasyprint (no headings/tables/etc.) but works
    without system deps. Letter page (mm units), 19mm margins.
    """
    from fpdf import FPDF  # type: ignore
    pdf = FPDF(format="letter", unit="mm")  # letter ~ 215.9 x 279.4 mm
    pdf.set_margins(left=19, top=19, right=19)  # ~0.75in
    pdf.set_auto_page_break(auto=True, margin=19)
    pdf.add_page()
    available_w = pdf.w - pdf.l_margin - pdf.r_margin  # ~178mm
    if title:
        pdf.set_font("Helvetica", style="B", size=18)
        pdf.multi_cell(available_w, 9, title.encode("latin-1", errors="replace").decode("latin-1"))
        pdf.ln(3)
    pdf.set_font("Helvetica", size=11)
    # Strip HTML tags for plain-text rendering
    plain = re.sub(r"<[^>]+>", "", plain_text)
    plain = plain.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # Collapse triple-blank-runs to keep visual rhythm
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    for line in plain.splitlines():
        safe = line.encode("latin-1", errors="replace").decode("latin-1")
        if safe.strip():
            pdf.multi_cell(available_w, 5, safe)
        else:
            pdf.ln(3)
    pdf.output(str(out_path))
    return {"backend": "fpdf2", "note": "plain-text fallback; install pango/cairo (brew install pango glib) for full markdown formatting via weasyprint"}


def _markdown_to_html(md: str, title: str | None, css: str | None) -> str:
    """Convert markdown to a styled HTML document."""
    try:
        import markdown
        body_html = markdown.markdown(
            md,
            extensions=["extra", "tables", "fenced_code", "sane_lists"],
        )
    except Exception:
        # crude fallback if markdown unavailable
        body_html = "<pre>" + md + "</pre>"
    title_html = f"<title>{title}</title>" if title else ""
    css_block = css if css is not None else DEFAULT_CSS
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/>{title_html}<style>{css_block}</style></head>
<body>{body_html}</body></html>"""


@mcp.tool()
def render_markdown(
    markdown: str,
    title: str = "",
    output_name: str = "",
    css: str = "",
) -> dict[str, Any]:
    """Render markdown text to a PDF file.

    Args:
        markdown: Markdown source text. Supports tables, fenced code, sane lists.
        title: Document title (used for the H1 if not in markdown, and for filename slug).
        output_name: Custom filename (without .pdf). Defaults to title or 'untitled'.
        css: Custom CSS to override the default. Empty = use default style.

    Returns the saved file path. Uses weasyprint if available, falls back to fpdf2.
    """
    if not markdown.strip():
        return {"ok": False, "error": "empty markdown"}
    out = _output_path(output_name, title)
    html_doc = _markdown_to_html(markdown, title or None, css or None)
    if _have_weasyprint():
        try:
            info = _render_via_weasyprint(html_doc, out)
        except Exception as exc:
            # Fall through to fpdf2 if weasyprint has runtime issues
            info = {"backend_error": f"weasyprint failed: {exc}; trying fpdf2"}
            if _have_fpdf():
                info.update(_render_via_fpdf(html_doc, out, title or None))
            else:
                return {"ok": False, "error": str(exc)[:300]}
    elif _have_fpdf():
        info = _render_via_fpdf(html_doc, out, title or None)
    else:
        return {"ok": False, "error": "no PDF backend available — install weasyprint or fpdf2"}
    return {
        "ok": True,
        "path": str(out),
        "filename": out.name,
        "size_bytes": out.stat().st_size,
        "title": title or None,
        **info,
    }


@mcp.tool()
def render_html(
    html: str,
    title: str = "",
    output_name: str = "",
    css: str = "",
) -> dict[str, Any]:
    """Render HTML directly to PDF (no markdown step).

    Args:
        html: Full HTML body content (will be wrapped if not a complete document).
        title, output_name, css: Same as render_markdown.

    Use when you have already-formatted HTML (e.g., from a template engine).
    """
    if not html.strip():
        return {"ok": False, "error": "empty html"}
    out = _output_path(output_name, title)
    if "<html" in html.lower():
        html_doc = html
    else:
        css_block = css if css else DEFAULT_CSS
        title_html = f"<title>{title}</title>" if title else ""
        html_doc = f"""<!doctype html><html><head><meta charset="utf-8"/>{title_html}<style>{css_block}</style></head><body>{html}</body></html>"""
    if _have_weasyprint():
        try:
            info = _render_via_weasyprint(html_doc, out)
        except Exception as exc:
            info = {"backend_error": f"weasyprint failed: {exc}; trying fpdf2"}
            if _have_fpdf():
                info.update(_render_via_fpdf(html_doc, out, title or None))
            else:
                return {"ok": False, "error": str(exc)[:300]}
    elif _have_fpdf():
        info = _render_via_fpdf(html_doc, out, title or None)
    else:
        return {"ok": False, "error": "no PDF backend available"}
    return {
        "ok": True,
        "path": str(out),
        "filename": out.name,
        "size_bytes": out.stat().st_size,
        "title": title or None,
        **info,
    }


@mcp.tool()
def list_pdfs(limit: int = 25) -> dict[str, Any]:
    """List recently rendered PDFs in the output directory."""
    if not PDF_DIR.exists():
        return {"ok": True, "pdf_dir": str(PDF_DIR), "count": 0, "pdfs": []}
    files = sorted(PDF_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return {
        "ok": True,
        "pdf_dir": str(PDF_DIR),
        "count": len(files),
        "pdfs": [
            {
                "filename": f.name,
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
            for f in files
        ],
    }


@mcp.tool()
def delete_pdf(filename: str) -> dict[str, Any]:
    """Delete a specific PDF file from the output directory.

    Args:
        filename: Just the filename (no path traversal allowed).
    """
    if "/" in filename or ".." in filename:
        return {"ok": False, "error": "invalid filename (no path components allowed)"}
    target = PDF_DIR / filename
    if not target.exists():
        return {"ok": False, "error": f"not found: {filename}"}
    target.unlink()
    return {"ok": True, "deleted": str(target)}


@mcp.tool()
def pdf_status() -> dict[str, Any]:
    """Server status + backend availability."""
    weasyprint_works = False
    weasyprint_error = None
    if _have_weasyprint():
        try:
            import weasyprint
            # Trigger lazy load of GTK libs
            weasyprint.HTML(string="<p>x</p>").write_pdf(str(PDF_DIR / ".healthcheck.pdf"))
            weasyprint_works = True
            (PDF_DIR / ".healthcheck.pdf").unlink(missing_ok=True)
        except Exception as exc:
            weasyprint_error = str(exc)[:200]
    return {
        "ok": True,
        "server": SERVER_NAME,
        "version": "0.1.0",
        "hermes_home": str(DEFAULT_HERMES_HOME),
        "pdf_dir": str(PDF_DIR),
        "pdf_dir_exists": PDF_DIR.exists(),
        "weasyprint_importable": _have_weasyprint(),
        "weasyprint_renders": weasyprint_works,
        "weasyprint_error": weasyprint_error,
        "fpdf2_available": _have_fpdf(),
        "markdown_available": _have_markdown(),
        "active_backend": (
            "weasyprint" if weasyprint_works else
            "fpdf2" if _have_fpdf() else
            "none"
        ),
    }


def main() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
