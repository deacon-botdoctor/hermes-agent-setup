#!/usr/bin/env python3
"""Expose one principal's local, tenant-isolated GBrain as a stdio MCP server."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SERVER = {"name": "gbrain", "version": "3.0.0"}
PRINCIPAL_NAME = os.environ.get("GBRAIN_PRINCIPAL_NAME", "Client").strip() or "Client"
MAX_OUTPUT_CHARS = 200_000
MAX_CONTENT_CHARS = 50_000
LOCK_TIMEOUT_SECONDS = 90


def annotations(*, read_only: bool, idempotent: bool = True) -> dict[str, bool]:
    return {
        "readOnlyHint": read_only,
        "destructiveHint": False,
        "idempotentHint": idempotent,
        "openWorldHint": False,
    }


def schema(
    properties: dict[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        value["required"] = list(required)
    return value


TOOLS: dict[str, dict[str, Any]] = {
    "get_page": {
        "description": "Read one local principal GBrain page by exact slug.",
        "inputSchema": schema({"slug": {"type": "string"}}, ("slug",)),
    },
    "list_pages": {
        "description": "List local principal GBrain pages.",
        "inputSchema": schema(
            {
                "type": {"type": "string"},
                "tag": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        ),
    },
    "search": {
        "description": "Keyword-search the local principal GBrain.",
        "inputSchema": schema({"query": {"type": "string"}}, ("query",)),
    },
    "query": {
        "description": "Hybrid-search the local principal GBrain.",
        "inputSchema": schema(
            {"query": {"type": "string"}, "expand": {"type": "boolean"}},
            ("query",),
        ),
    },
    "get_stats": {
        "description": "Return local GBrain page, chunk, and embedding counters.",
        "inputSchema": schema({}),
    },
    "get_health": {
        "description": "Return the local GBrain health dashboard.",
        "inputSchema": schema({}),
    },
    "backlinks": {
        "description": "Read incoming links for one local GBrain page.",
        "inputSchema": schema({"slug": {"type": "string"}}, ("slug",)),
    },
    "graph": {
        "description": "Traverse the local GBrain graph from one page.",
        "inputSchema": schema(
            {
                "slug": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            ("slug",),
        ),
    },
    "code_def": {
        "description": "Find a code symbol definition in indexed repositories.",
        "inputSchema": schema(
            {"symbol": {"type": "string"}, "lang": {"type": "string"}},
            ("symbol",),
        ),
    },
    "put_page": {
        "description": f"Create or update one page in {PRINCIPAL_NAME}'s local GBrain.",
        "inputSchema": schema(
            {"slug": {"type": "string"}, "content": {"type": "string"}},
            ("slug", "content"),
        ),
        "read_only": False,
    },
    "tag_page": {
        "description": "Add a tag to one local GBrain page.",
        "inputSchema": schema(
            {"slug": {"type": "string"}, "tag": {"type": "string"}},
            ("slug", "tag"),
        ),
        "read_only": False,
    },
    "untag_page": {
        "description": "Remove a tag from one local GBrain page.",
        "inputSchema": schema(
            {"slug": {"type": "string"}, "tag": {"type": "string"}},
            ("slug", "tag"),
        ),
        "read_only": False,
    },
    "link_pages": {
        "description": "Create a typed link between two local GBrain pages.",
        "inputSchema": schema(
            {
                "from_slug": {"type": "string"},
                "to_slug": {"type": "string"},
                "type": {"type": "string"},
            },
            ("from_slug", "to_slug"),
        ),
        "read_only": False,
    },
    "unlink_pages": {
        "description": "Remove a link between two local GBrain pages.",
        "inputSchema": schema(
            {"from_slug": {"type": "string"}, "to_slug": {"type": "string"}},
            ("from_slug", "to_slug"),
        ),
        "read_only": False,
    },
    "timeline_add": {
        "description": "Add a dated timeline entry to one local GBrain page.",
        "inputSchema": schema(
            {
                "slug": {"type": "string"},
                "date": {"type": "string"},
                "text": {"type": "string"},
            },
            ("slug", "date", "text"),
        ),
        "read_only": False,
        "idempotent": False,
    },
}
for definition in TOOLS.values():
    definition["annotations"] = annotations(
        read_only=bool(definition.pop("read_only", True)),
        idempotent=bool(definition.pop("idempotent", True)),
    )


def bounded_string(arguments: dict[str, Any], name: str, limit: int = 500) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(
            f"{name} must be a non-empty string of at most {limit} characters"
        )
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} contains a control character")
    return value.strip()


def bounded_slug(arguments: dict[str, Any], name: str = "slug") -> str:
    value = bounded_string(arguments, name, 240)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,239}", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"{name} is not a safe GBrain slug")
    return value


def bounded_content(arguments: dict[str, Any]) -> str:
    value = arguments.get("content")
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"content must be a non-empty string of at most {MAX_CONTENT_CHARS} characters"
        )
    if "\x00" in value:
        raise ValueError("content contains a NUL character")
    return value


def build_get(arguments: dict[str, Any]) -> list[str]:
    return ["get", bounded_slug(arguments)]


def build_list(arguments: dict[str, Any]) -> list[str]:
    command = ["list"]
    if "type" in arguments:
        command.extend(["--type", bounded_string(arguments, "type", 80)])
    if "tag" in arguments:
        command.extend(["--tag", bounded_string(arguments, "tag", 80)])
    if "limit" in arguments:
        limit = arguments["limit"]
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 through 100")
        command.extend(["-n", str(limit)])
    return command


def build_search(arguments: dict[str, Any]) -> list[str]:
    return ["search", bounded_string(arguments, "query", 2_000)]


def build_query(arguments: dict[str, Any]) -> list[str]:
    command = ["query", bounded_string(arguments, "query", 2_000)]
    if arguments.get("expand") is False:
        command.append("--no-expand")
    elif "expand" in arguments and arguments["expand"] is not True:
        raise ValueError("expand must be a boolean")
    return command


def build_no_args(command: str) -> Callable[[dict[str, Any]], list[str]]:
    def builder(arguments: dict[str, Any]) -> list[str]:
        if arguments:
            raise ValueError(f"{command} does not accept arguments")
        return [command]

    return builder


def build_backlinks(arguments: dict[str, Any]) -> list[str]:
    return ["backlinks", bounded_slug(arguments)]


def build_graph(arguments: dict[str, Any]) -> list[str]:
    command = ["graph", bounded_slug(arguments)]
    if "depth" in arguments:
        depth = arguments["depth"]
        if type(depth) is not int or not 1 <= depth <= 4:
            raise ValueError("depth must be an integer from 1 through 4")
        command.extend(["--depth", str(depth)])
    return command


def build_code_def(arguments: dict[str, Any]) -> list[str]:
    command = ["code-def", bounded_string(arguments, "symbol")]
    if "lang" in arguments:
        command.extend(["--lang", bounded_string(arguments, "lang", 40)])
    return command


def build_put(arguments: dict[str, Any]) -> list[str]:
    bounded_content(arguments)
    return ["put", bounded_slug(arguments)]


def build_tag(arguments: dict[str, Any], command: str) -> list[str]:
    return [command, bounded_slug(arguments), bounded_string(arguments, "tag", 80)]


def build_link(arguments: dict[str, Any], command: str) -> list[str]:
    value = [command, bounded_slug(arguments, "from_slug"), bounded_slug(arguments, "to_slug")]
    if command == "link" and "type" in arguments:
        value.extend(["--type", bounded_string(arguments, "type", 80)])
    return value


def build_timeline_add(arguments: dict[str, Any]) -> list[str]:
    date = bounded_string(arguments, "date", 40)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T[^\s]{1,30})?", date):
        raise ValueError("date must be an ISO date or datetime")
    return [
        "timeline-add",
        bounded_slug(arguments),
        date,
        bounded_string(arguments, "text", 2_000),
    ]


BUILDERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "get_page": build_get,
    "list_pages": build_list,
    "search": build_search,
    "query": build_query,
    "get_stats": build_no_args("stats"),
    "get_health": build_no_args("health"),
    "backlinks": build_backlinks,
    "graph": build_graph,
    "code_def": build_code_def,
    "put_page": build_put,
    "tag_page": lambda arguments: build_tag(arguments, "tag"),
    "untag_page": lambda arguments: build_tag(arguments, "untag"),
    "link_pages": lambda arguments: build_link(arguments, "link"),
    "unlink_pages": lambda arguments: build_link(arguments, "unlink"),
    "timeline_add": build_timeline_add,
}

WRITE_TOOLS = {
    "put_page",
    "tag_page",
    "untag_page",
    "link_pages",
    "unlink_pages",
    "timeline_add",
}


def commit_vault_write(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    raw_vault = os.environ.get("GBRAIN_VAULT", "")
    if not raw_vault:
        raise RuntimeError("GBRAIN_VAULT is required for durable GBrain writes")
    vault = Path(raw_vault).expanduser()
    git = shutil.which("git")
    if (
        not git
        or not vault.is_absolute()
        or vault.is_symlink()
        or not vault.is_dir()
        or (vault / ".git").is_symlink()
        or not (vault / ".git").is_dir()
    ):
        raise RuntimeError("tenant GBrain vault Git boundary is unavailable or unsafe")
    root = subprocess.run(
        [git, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=vault,
        timeout=30,
    )
    if root.returncode or Path(root.stdout.strip()).resolve() != vault.resolve():
        raise RuntimeError("tenant GBrain vault is not the exact Git worktree root")
    slugs = []
    for key in ("slug", "from_slug", "to_slug"):
        if key in arguments:
            slugs.append(bounded_slug(arguments, key))
    paths = sorted({f"{slug}.md" for slug in slugs})
    if not paths:
        raise RuntimeError("durable GBrain write has no bounded vault path")
    staged = subprocess.run(
        [git, "add", "--", *paths],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=vault,
        timeout=30,
    )
    if staged.returncode:
        raise RuntimeError("tenant GBrain vault staging failed")
    changed = subprocess.run(
        [git, "diff", "--cached", "--quiet", "--", *paths],
        check=False,
        cwd=vault,
        timeout=30,
    )
    committed = changed.returncode == 1
    if changed.returncode not in (0, 1):
        raise RuntimeError("tenant GBrain vault staged-change proof failed")
    if committed:
        result = subprocess.run(
            [git, "commit", "-m", f"GBrain {name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            cwd=vault,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError("tenant GBrain vault commit failed")
    head = subprocess.run(
        [git, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=vault,
        timeout=30,
    )
    status = subprocess.run(
        [git, "status", "--porcelain=v1", "--", *paths],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=vault,
        timeout=30,
    )
    if (
        head.returncode
        or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip())
        or status.returncode
        or status.stdout.strip()
    ):
        raise RuntimeError("tenant GBrain vault commit verification failed")
    return {"committed": committed, "head": head.stdout.strip(), "paths": paths}


def validate_gbrain(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"GBrain executable is missing or unsafe: {path}")
    return path.resolve()


def validate_lock_file(path: Path | None) -> Path | None:
    if path is None:
        return None
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("GBrain lock path is unsafe")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError("GBrain lock parent is missing or unsafe")
    if path.exists() and not path.is_file():
        raise RuntimeError("GBrain lock path is unsafe")
    return path


@contextlib.contextmanager
def serialized_gbrain(lock_file: Path | None):
    if lock_file is None:
        yield
        return
    handle = lock_file.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while not acquired:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("GBrain process lock timed out")
                    time.sleep(0.1)
        else:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while not acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("GBrain process lock timed out")
                    time.sleep(0.1)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def response(
    request_id: Any,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def call_tool(
    gbrain: Path,
    name: str,
    arguments: dict[str, Any],
    lock_file: Path | None = None,
) -> dict[str, Any]:
    builder = BUILDERS.get(name)
    if builder is None:
        return {
            "content": [{"type": "text", "text": f"unknown GBrain tool: {name}"}],
            "isError": True,
        }
    try:
        command = builder(arguments)
    except ValueError as exc:
        return {
            "content": [{"type": "text", "text": str(exc)}],
            "isError": True,
        }
    environment = os.environ.copy()
    environment.setdefault("NO_COLOR", "1")
    try:
        with serialized_gbrain(lock_file):
            result = subprocess.run(
                [str(gbrain), *command],
                input=bounded_content(arguments) if name == "put_page" else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
                env=environment,
            )
            vault_commit = (
                commit_vault_write(name, arguments)
                if result.returncode == 0 and name in WRITE_TOOLS
                else None
            )
            if result.returncode == 0 and name == "put_page":
                slug = bounded_slug(arguments)
                readback = subprocess.run(
                    [str(gbrain), "get", slug],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=90,
                    check=False,
                    env=environment,
                )
                if readback.returncode != 0:
                    result = readback
                else:
                    output = readback.stdout
                    receipt = {
                        "status": "verified",
                        "slug": slug,
                        "bytes": len(output.encode("utf-8")),
                        "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                        "vault_commit": vault_commit,
                    }
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(receipt, sort_keys=True),
                            }
                        ],
                        "isError": False,
                    }
    except (subprocess.TimeoutExpired, TimeoutError):
        return {
            "content": [{"type": "text", "text": "GBrain operation timed out"}],
            "isError": True,
        }
    output = (result.stdout or result.stderr).strip()
    if result.returncode == 0 and name in WRITE_TOOLS:
        output = json.dumps(
            {
                "status": "verified",
                "tool": name,
                "output": output[:10_000],
                "vault_commit": vault_commit,
            },
            sort_keys=True,
        )
    return {
        "content": [{"type": "text", "text": output[:MAX_OUTPUT_CHARS]}],
        "isError": result.returncode != 0,
    }


def handle(
    gbrain: Path,
    message: dict[str, Any],
    lock_file: Path | None = None,
) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        return response(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER,
            },
        )
    if method == "ping":
        return response(request_id, {})
    if method == "tools/list":
        return response(
            request_id,
            {
                "tools": [
                    {"name": name, **definition} for name, definition in TOOLS.items()
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return response(
                request_id,
                error={"code": -32602, "message": "invalid tool call"},
            )
        return response(request_id, call_tool(gbrain, name, arguments, lock_file))
    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method.startswith("resources") else "prompts"
        return response(request_id, {key: []})
    return response(request_id, error={"code": -32601, "message": "method not found"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gbrain", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path)
    args = parser.parse_args(argv)
    try:
        gbrain = validate_gbrain(args.gbrain)
        lock_file = validate_lock_file(args.lock_file)
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise TypeError("request must be a JSON object")
                value = handle(gbrain, message, lock_file)
            except Exception as exc:  # noqa: BLE001 - MCP request boundary
                value = response(
                    None,
                    error={"code": -32603, "message": str(exc)[:500]},
                )
            if value is not None:
                print(json.dumps(value, separators=(",", ":")), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - process boundary
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
