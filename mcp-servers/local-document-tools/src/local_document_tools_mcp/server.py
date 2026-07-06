from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - keeps tests runnable without mcp installed.
    FastMCP = None  # type: ignore[assignment]


MAX_READ_BYTES = int(os.environ.get("LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES", "2000000"))

mcp = FastMCP("local-document-tools") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _safe_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise FileNotFoundError(path)
    if not candidate.is_file():
        raise IsADirectoryError(path)
    return candidate


def _read_text(path: Path) -> str:
    data = path.read_bytes()[:MAX_READ_BYTES]
    return data.decode("utf-8", errors="replace")


@_tool
def document_info(path: str) -> dict[str, Any]:
    try:
        file_path = _safe_path(path)
    except Exception as exc:
        return {"ok": False, "path": path, "error": str(exc)}
    return {
        "ok": True,
        "path": str(file_path),
        "name": file_path.name,
        "suffix": file_path.suffix.lower(),
        "size_bytes": file_path.stat().st_size,
    }


@_tool
def extract_text(path: str) -> dict[str, Any]:
    try:
        file_path = _safe_path(path)
        text = _read_text(file_path)
    except Exception as exc:
        return {"ok": False, "path": path, "error": str(exc), "text": ""}
    if file_path.suffix.lower() in {".html", ".htm"}:
        text = _html_to_text(text)
    return {"ok": True, "path": str(file_path), "text": text}


@_tool
def html_to_text(path: str) -> dict[str, Any]:
    try:
        file_path = _safe_path(path)
        text = _html_to_text(_read_text(file_path))
    except Exception as exc:
        return {"ok": False, "path": path, "error": str(exc), "text": ""}
    return {"ok": True, "path": str(file_path), "text": text}


@_tool
def merge_text_documents(paths: list[str], separator: str = "\n\n---\n\n") -> dict[str, Any]:
    chunks: list[str] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        result = extract_text(path)
        if result.get("ok"):
            chunks.append(str(result.get("text", "")))
        else:
            errors.append({"path": path, "error": str(result.get("error", "unknown error"))})
    return {"ok": not errors, "text": separator.join(chunks), "errors": errors, "documents": len(chunks)}


def _html_to_text(source: str) -> str:
    without_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", source)
    with_breaks = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h[1-6]>", "\n", without_scripts)
    stripped = re.sub(r"(?s)<[^>]+>", " ", with_breaks)
    decoded = html.unescape(stripped)
    lines = [" ".join(line.split()) for line in decoded.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def main() -> None:
    if mcp is None:
        raise SystemExit("The mcp package is required to run the local-document-tools MCP server.")
    mcp.run()


if __name__ == "__main__":
    main()
