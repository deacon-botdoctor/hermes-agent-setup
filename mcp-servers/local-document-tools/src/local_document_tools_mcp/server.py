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
DOCUMENT_ROOTS_ENV = "LOCAL_DOCUMENT_TOOLS_ROOTS"

mcp = FastMCP("local-document-tools") if FastMCP else None


def _tool(fn):
    if mcp is None:
        return fn
    return mcp.tool()(fn)


def _safe_path(path: str) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    if not candidate.exists():
        raise FileNotFoundError(path)
    if not candidate.is_file():
        raise IsADirectoryError(path)
    resolved = candidate.resolve(strict=True)
    roots = _document_roots()
    if not any(_is_relative_to(resolved, root) for root in roots):
        allowed = os.pathsep.join(str(root) for root in roots)
        raise PermissionError(f"{path} is outside {DOCUMENT_ROOTS_ENV}: {allowed}")
    return resolved


def _document_roots() -> list[Path]:
    configured = os.environ.get(DOCUMENT_ROOTS_ENV)
    raw_roots = configured.split(os.pathsep) if configured else [os.getcwd()]
    roots: list[Path] = []
    for root in raw_roots:
        if not root:
            continue
        roots.append(Path(root).expanduser().resolve(strict=True))
    if not roots:
        roots.append(Path(os.getcwd()).resolve(strict=True))
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_text(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(MAX_READ_BYTES)
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
def document_convert(path: str, target_format: str = "text", source_format: str | None = None) -> dict[str, Any]:
    try:
        file_path = _safe_path(path)
    except Exception as exc:
        return {"ok": False, "path": path, "error": str(exc), "error_code": "input_error", "text": ""}

    source = (source_format or file_path.suffix.lstrip(".") or "text").lower()
    target = target_format.lower()
    if target not in {"text", "txt", "text/plain"}:
        return _unsupported_conversion(path, source, target)
    if source in {"html", "htm", "text/html"} or file_path.suffix.lower() in {".html", ".htm"}:
        return html_to_text(str(file_path))
    if source in {"text", "txt", "text/plain"} or file_path.suffix.lower() in {".txt", ".md", ".csv", ".tsv"}:
        return extract_text(str(file_path))
    return _unsupported_conversion(path, source, target)


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


def _unsupported_conversion(path: str, source: str, target: str) -> dict[str, Any]:
    return {
        "ok": False,
        "path": path,
        "error": f"unsupported_conversion: {source} to {target}",
        "error_code": "unsupported_conversion",
        "text": "",
    }


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
