#!/usr/bin/env python3
"""Retire active Anamnesis/Qdrant runtime bindings without deleting memory data."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RETIRED_SERVERS = frozenset({"anamnesis", "hermes-lcm", "autodream"})
RETIRED_TOOLSET_VALUES = RETIRED_SERVERS | frozenset(
    f"mcp-{name}" for name in RETIRED_SERVERS
)
RETIRED_PLUGINS = frozenset(
    {"anamnesis", "anamnesis-ingest", "hermes-lcm", "autodream"}
)
RETIRED_SCHEDULER_TOKENS = (
    "anamnesis",
    "hermes-lcm",
    "autodream",
    "lcm_freshness",
)
LEGACY_POLICY_TITLE = "# Knowledge Store Policy — GBrain + Anamnesis"
LEGACY_AGENT_SENTENCE = (
    "Durable artifacts go to GBrain; concise preferences/actions/pointers go to Anamnesis."
)
NATIVE_AGENT_SENTENCE = (
    "Durable artifacts go to GBrain; durable user preferences belong in native Hermes "
    "`USER.md`, and bounded conversational continuity belongs in `memories/MEMORY.md`."
)
CANONICAL_RULE_NAMES = frozenset(
    {"knowledge-store-policy.md", "capability-discovery.md", "knowledge-routing.md"}
)
KNOWN_TEXT_REWRITES = {
    "Hot by default: anamnesis, ": "Hot by default: ",
    "Configured MCP connectors visible to this runtime: anamnesis, ":
        "Configured MCP connectors visible to this runtime: ",
    "and anamnesis memory.": "and native Hermes memory.",
    "and Anamnesis —": "and native Hermes memory —",
    (
        "to anamnesis (container: `work`, confidence: 1.0) — before moving on to "
        "the next task"
    ): (
        "to native Hermes memory and GBrain — before moving on to the next task"
    ),
    "- Anamnesis/memory is advisor/skill-first; use concrete on-demand MCP capabilities only when needed.":
        "- Native Hermes memory is built in; do not route recall through an external memory MCP.",
    "- `anamnesis` - local semantic recall over Sarah's memories, sessions, and case notes.\n": "",
    "**Working memory stays light on purpose.** Anamnesis holds concise, high-signal\n  cards":
        "**Working memory stays light on purpose.** Native Hermes memory holds concise,\n  high-signal cards",
    "working memory (Anamnesis).": "working memory (native Hermes memory).",
    "Concise preferences/actions/pointers go to Anamnesis when available.":
        "Durable user preferences belong in native Hermes `USER.md`; bounded nearby "
        "continuity belongs in `memories/MEMORY.md`.",
    "Multiple read-only / lookup tool calls (`anamnesis_search`, `telegram_directory_*`,":
        "Multiple read-only / lookup tool calls (`telegram_directory_*`,",
    "(anamnesis, gmail, calendar, drive, sheets, docs, notion":
        "(gmail, calendar, drive, sheets, docs, notion",
    '✓ capability_id="anamnesis.recall"          ← dotted: explicitly the read action\n': "",
    '✗ capability_id="anamnesis"                 ← bare: ambiguous (could route to write)\n': "",
    (
        "MCP hot path is intentionally small. Current Mini `cap-router-only` defaults "
        "keep `capability-router` and `telegram-admin` hot; `anamnesis`, `gbrain`, and "
        "most other connectors are on-demand through capability-router. Use "
        "capability-router before claiming a capability is unavailable."
    ): (
        "MCP hot path is intentionally small. Current Mini `cap-router-only` defaults "
        "keep `capability-router` and `telegram-admin` hot; `gbrain` and most other "
        "connectors are on-demand through capability-router. Native Hermes memory "
        "handles bounded continuity. Use capability-router before claiming a capability "
        "is unavailable."
    ),
    (
        "- Concise preferences, decisions, commitments, next actions, continuity notes, "
        "and GBrain slug pointer cards go to Anamnesis."
    ): (
        "- Durable user preferences go to native Hermes `USER.md`; bounded nearby "
        "continuity goes to `memories/MEMORY.md`."
    ),
    (
    "- Use `~/.hermes/bin/knowledge-write` for durable artifact persistence so the "
        "full artifact lands in GBrain and the pointer card lands in Anamnesis."
    ): (
        "- Use `~/.hermes/bin/knowledge-write` for durable artifact persistence in "
        "GBrain; keep only a concise native-memory pointer when ordinary turns need it."
    ),
    (
        "- Use ~/.hermes/bin/knowledge-write for durable artifact persistence so the "
        "full artifact lands in GBrain and the pointer card lands in Anamnesis."
    ): (
        "- Use ~/.hermes/bin/knowledge-write for durable artifact persistence in "
        "GBrain; keep only a concise native-memory pointer when ordinary turns need it."
    ),
    (
        "- Search Anamnesis with `container=work` and, when available, "
        "`client_id=botdoctor-marketing`; use GBrain for durable marketing docs, "
        "campaign artifacts, website/source-of-truth notes, and prior reports."
    ): (
        "- Search session history and native Hermes memory for nearby context; use "
        "GBrain for durable marketing docs, campaign artifacts, website/source-of-truth "
        "notes, and prior reports."
    ),
    (
        "- Store future marketing relevance lessons as concise Anamnesis cards using "
        "`client_id=botdoctor-marketing`, `memory_type=preference`, `decision`, "
        "`handoff`, or `general` with `metadata.domain=marketing`, and "
        "`priority=notable` unless it is a hard rule."
    ): (
        "- Store durable marketing preferences in native Hermes `USER.md`; file reusable "
        "marketing lessons and hard rules in GBrain."
    ),
    "Common on-demand connectors on this runtime include: anamnesis, ":
        "Common on-demand connectors on this runtime include: ",
    "- local Qdrant only when the service is actually live":
        "- native Hermes memory",
    "use Anamnesis for concise actionable memory only":
        "use native Hermes memory for concise actionable continuity",
    "such as `anamnesis.recall` or `gmail-inbox.gmail_inbox_list_unread`":
        "such as `gmail-inbox.gmail_inbox_list_unread`",
    "- `--summary` — one-line summary for the Anamnesis pointer card.":
        "- `--summary` — one-line summary for the durable decision record.",
    (
        "This lands the full note in GBrain under the `decisions/` slug and a pointer "
        "card in Anamnesis (memory_type=decision)."
    ): (
        "This lands the full note in GBrain under the `decisions/` slug; keep a concise "
        "native-memory pointer only when ordinary turns need it."
    ),
    "3. Other memory/search systems (session search, Anamnesis) and local workspace files.":
        "3. Native Hermes memory, session search, and local workspace files.",
    "on project/person keywords → Anamnesis/memory → local project files.":
        "on project/person keywords → native Hermes memory → local project files.",
    "MCPs (anamnesis, openrouter-image, etc.)":
        "MCPs (memory, openrouter-image, etc.)",
    "telegram-transcript, qdrant, ollama, capability-router":
        "telegram-transcript, ollama, capability-router",
    "env files, anamnesis directories": "env files, memory-provider directories",
    '"I searched anamnesis, Obsidian, env files, memory."':
        '"I searched the memory system, Obsidian, env files, memory."',
    "Access via filesystem or anamnesis MCP when available":
        "Access via filesystem or native Hermes memory when available",
    "| OllamaServer | 11434 | Local embeddings for anamnesis |\n": "",
    "| QdrantServer | 6333 | Vector DB for semantic memory |\n": "",
    "If the wording is loose or indirect, search anamnesis with a natural-language query.":
        "If the wording is loose or indirect, search native Hermes memory and session history.",
    "anamnesis = loose-context recall over stored memories plus vault search":
        "native Hermes memory and session history provide nearby-context recall; the vault provides durable search",
    "Search anamnesis with a loose natural-language query.":
        "Search native Hermes memory and session history with the user's natural-language wording.",
    "Anamnesis/vault search, then `memory-recall.ps1`.":
        "Native Hermes memory and vault search, then `memory-recall.ps1`.",
    "Files, memory, the Obsidian vault, and anamnesis are the continuity layer.":
        "Files, native Hermes memory, session history, and the Obsidian vault are the continuity layer.",
    "hot path is anamnesis, Gmail, and calendar":
        "hot path is native Hermes memory, Gmail, and calendar",
    "anamnesis MCP is enabled for semantic recall over stored memories":
        "native Hermes memory is enabled for nearby-context recall; use the vault for durable search",
    "anamnesis recall": "native Hermes memory and session recall",
    (
        '- Not at the implementation level: "I searched anamnesis, the file\n'
        '  system, the Obsidian vault, env files, memory..."'
    ): (
        '- Not at the implementation level: "I searched the internal memory system, '
        'the file\n  system, the Obsidian vault, and environment files..."'
    ),
    "MCPs (anamnesis, openrouter-image, browser-lane, mac-control, etc.)":
        "MCPs (memory, openrouter-image, browser-lane, mac-control, etc.)",
    (
        '"The memory is managed by the anamnesis MCP, which writes to qdrant collection '
        '<foo>_memories..."'
    ): (
        '"The memory is managed by an internal provider, which writes to an internal '
        'collection..."'
    ),
    (
        "Use Anamnesis only for concise actionable memory cards, preferences, "
        "commitments, and pointers to GBrain slugs."
    ): (
        "Use native Hermes memory only for concise actionable continuity, durable user "
        "preferences, commitments, and pointers to GBrain slugs."
    ),
    "Do not stuff full reports or source documents into Anamnesis.":
        "Do not stuff full reports or source documents into native Hermes memory.",
}
KNOWN_CONFIG_REWRITES = {
    "working memory (that is anamnesis)":
        "bounded conversational memory (that is native Hermes memory)",
    "memory routed through anamnesis":
        "bounded continuity handled by native Hermes memory",
}
LEGACY_TERM_RE = re.compile(r"(?i)(?:anamnesis|qdrant)")
NEGATIVE_CONTEXT_RE = re.compile(
    r"(?i)\b(?:retired|forbidden|absent|removed|remove|do not|does not|don't|not use|not at|never use)\b"
)
NEUTRAL_DEFINITION_RE = re.compile(r"(?i)^\s*[-*]?\s*anamnesis\s+means\b")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_yaml(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - install-host-artifacts requires PyYAML
        raise ValueError("PyYAML is required to normalize config.yaml") from exc
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("config.yaml root must be a mapping")
    return data


def _mapping_end(lines: list[str], start: int, parent_indent: int) -> int:
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[index]) - len(lines[index].lstrip(" "))
        if indent <= parent_indent:
            return index
    return len(lines)


def remove_mapping_children(text: str, parent: str, children: set[str]) -> tuple[str, list[str]]:
    """Remove exact direct YAML mapping children while preserving unrelated formatting."""
    lines = text.splitlines(keepends=True)
    parent_re = re.compile(rf"^(?P<indent> *){re.escape(parent)}:\s*(?:#.*)?$")
    child_re = re.compile(r"^(?P<indent> +)(?P<name>[A-Za-z0-9_-]+):(?:\s|$)")
    removed: list[str] = []
    index = 0
    while index < len(lines):
        match = parent_re.match(lines[index].rstrip("\r\n"))
        if not match:
            index += 1
            continue
        parent_indent = len(match.group("indent"))
        end = _mapping_end(lines, index, parent_indent)
        cursor = index + 1
        while cursor < end:
            child = child_re.match(lines[cursor].rstrip("\r\n"))
            if not child or len(child.group("indent")) <= parent_indent:
                cursor += 1
                continue
            direct_indent = len(child.group("indent"))
            name = child.group("name")
            block_end = cursor + 1
            while block_end < end:
                candidate = lines[block_end].strip()
                if candidate and not candidate.startswith("#"):
                    indent = len(lines[block_end]) - len(lines[block_end].lstrip(" "))
                    if indent <= direct_indent:
                        break
                block_end += 1
            if name in children:
                del lines[cursor:block_end]
                removed.append(name)
                end -= block_end - cursor
                continue
            cursor = block_end
        index = end
    return "".join(lines), removed


def _yaml_scalar(value: str) -> str:
    return value.strip().strip("'\"")


def remove_mapping_sequence_values(
    text: str, parent: str, values: frozenset[str]
) -> tuple[str, list[str]]:
    """Remove exact values from direct child sequences under one YAML mapping."""
    lines = text.splitlines(keepends=True)
    parent_re = re.compile(rf"^(?P<indent> *){re.escape(parent)}:\s*(?:#.*)?$")
    child_re = re.compile(
        r"^(?P<indent> +)(?P<name>[A-Za-z0-9_-]+):(?P<rest>.*?)(?P<newline>\r?\n)?$"
    )
    item_re = re.compile(
        r"^(?P<indent> +)-\s*(?P<value>[^#\r\n]+?)(?:\s+#.*)?(?P<newline>\r?\n)?$"
    )
    removed: list[str] = []
    index = 0
    while index < len(lines):
        parent_match = parent_re.match(lines[index].rstrip("\r\n"))
        if not parent_match:
            index += 1
            continue
        parent_indent = len(parent_match.group("indent"))
        end = _mapping_end(lines, index, parent_indent)
        cursor = index + 1
        while cursor < end:
            child = child_re.match(lines[cursor])
            if not child or len(child.group("indent")) <= parent_indent:
                cursor += 1
                continue
            child_indent = len(child.group("indent"))
            block_end = cursor + 1
            while block_end < end:
                candidate = lines[block_end].strip()
                if candidate and not candidate.startswith("#"):
                    indent = len(lines[block_end]) - len(lines[block_end].lstrip(" "))
                    if indent < child_indent or (
                        indent == child_indent
                        and not lines[block_end].lstrip().startswith("-")
                    ):
                        break
                block_end += 1

            rest = child.group("rest").strip()
            if rest.startswith("[") and rest.endswith("]"):
                tokens = [token.strip() for token in rest[1:-1].split(",") if token.strip()]
                kept = [
                    token for token in tokens if _yaml_scalar(token).lower() not in values
                ]
                removed.extend(
                    _yaml_scalar(token).lower()
                    for token in tokens
                    if _yaml_scalar(token).lower() in values
                )
                if kept != tokens:
                    newline = child.group("newline") or ""
                    lines[cursor] = (
                        f"{child.group('indent')}{child.group('name')}: "
                        f"[{', '.join(kept)}]{newline}"
                    )
            elif not rest or rest.startswith("#"):
                item_index = cursor + 1
                while item_index < block_end:
                    item = item_re.match(lines[item_index])
                    if item and _yaml_scalar(item.group("value")).lower() in values:
                        removed.append(_yaml_scalar(item.group("value")).lower())
                        del lines[item_index]
                        block_end -= 1
                        end -= 1
                        continue
                    item_index += 1
                remaining = [
                    line
                    for line in lines[cursor + 1:block_end]
                    if line.strip() and not line.lstrip().startswith("#")
                ]
                if not remaining and any(value in removed for value in values):
                    newline = child.group("newline") or ""
                    lines[cursor] = (
                        f"{child.group('indent')}{child.group('name')}: []{newline}"
                    )
            cursor = block_end
        index = end
    return "".join(lines), removed


def _retired_python_command(value: Any) -> bool:
    normalized = str(value or "").replace("\\", "/").lower()
    return (
        "/mcp-servers/anamnesis/" in normalized
        and "/python" in normalized
        and ("/.venv/" in normalized or "/venv/" in normalized)
    )


def replace_exact_command_scalars(
    text: str, replacements: dict[str, str]
) -> tuple[str, int]:
    """Replace exact YAML command scalars while preserving comments and quoting."""
    lines = text.splitlines(keepends=True)
    count = 0
    command_re = re.compile(r"^(?P<prefix>\s*command:\s*)(?P<body>.*?)(?P<newline>\r?\n)?$")
    for index, line in enumerate(lines):
        match = command_re.match(line)
        if not match:
            continue
        body = match.group("body")
        scalar, separator, comment = body.partition(" #")
        stripped = scalar.strip()
        value = _yaml_scalar(stripped)
        replacement = replacements.get(value)
        if replacement is None:
            continue
        quote = stripped[:1] if stripped[:1] in {"'", '"'} else ""
        rendered = f"{quote}{replacement}{quote}"
        suffix = f" #{comment}" if separator else ""
        lines[index] = (
            f"{match.group('prefix')}{rendered}{suffix}{match.group('newline') or ''}"
        )
        count += 1
    return "".join(lines), count


def normalize_config(
    text: str, hermes_python: str | None = None
) -> tuple[str, list[str]]:
    before = load_yaml(text)
    servers = before.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        raise ValueError("config.yaml mcp_servers must be a mapping")
    active = {
        str(name) for name in servers if str(name).lower() in RETIRED_SERVERS
    }
    updated, removed = remove_mapping_children(text, "mcp_servers", set(active))
    updated, removed_toolsets = remove_mapping_sequence_values(
        updated, "platform_toolsets", RETIRED_TOOLSET_VALUES
    )
    updated, removed_policy = remove_mapping_sequence_values(
        updated, "mcp_policy", RETIRED_SERVERS
    )
    updated, removed_plugins = remove_mapping_sequence_values(
        updated, "plugins", RETIRED_PLUGINS
    )
    parsed_after_removal = load_yaml(updated)
    remaining_servers_after_removal = parsed_after_removal.get("mcp_servers") or {}
    retired_python_commands = {
        str(entry.get("command"))
        for entry in remaining_servers_after_removal.values()
        if isinstance(entry, dict) and _retired_python_command(entry.get("command"))
    }
    replaced_python_commands = 0
    if retired_python_commands:
        if not hermes_python:
            raise ValueError(
                "remaining MCP servers depend on the retired Anamnesis interpreter"
            )
        updated, replaced_python_commands = replace_exact_command_scalars(
            updated,
            {command: str(hermes_python) for command in retired_python_commands},
        )
    config_rewritten = False
    for legacy, replacement in KNOWN_CONFIG_REWRITES.items():
        if legacy in updated:
            updated = updated.replace(legacy, replacement)
            config_rewritten = True
    after = load_yaml(updated)
    remaining = {
        str(name)
        for name in (after.get("mcp_servers") or {})
        if str(name).lower() in RETIRED_SERVERS
    }
    remaining_toolsets = {
        str(value)
        for values in (after.get("platform_toolsets") or {}).values()
        if isinstance(values, list)
        for value in values
        if str(value).lower() in RETIRED_TOOLSET_VALUES
    }
    remaining_policy = {
        str(value)
        for values in (after.get("mcp_policy") or {}).values()
        if isinstance(values, list)
        for value in values
        if str(value).lower() in RETIRED_SERVERS
    }
    remaining_plugins = {
        str(value)
        for values in (after.get("plugins") or {}).values()
        if isinstance(values, list)
        for value in values
        if str(value).lower() in RETIRED_PLUGINS
    }
    remaining_python_commands = {
        str(entry.get("command"))
        for entry in (after.get("mcp_servers") or {}).values()
        if isinstance(entry, dict) and _retired_python_command(entry.get("command"))
    }
    if (
        remaining
        or remaining_toolsets
        or remaining_policy
        or remaining_plugins
        or remaining_python_commands
        or set(removed) != active
    ):
        raise ValueError(
            "unable to remove retired runtime bindings exactly: "
            + ", ".join(
                sorted(
                    remaining
                    | remaining_toolsets
                    | remaining_policy
                    | remaining_plugins
                    | remaining_python_commands
                    | active
                )
            )
        )
    changes = [f"config:mcp_servers.{name}" for name in sorted(removed)]
    if removed_toolsets:
        changes.append("config:platform_toolsets.retired-memory")
    if removed_policy:
        changes.append("config:mcp_policy.retired-memory")
    if removed_plugins:
        changes.append("config:plugins.retired-memory")
    if replaced_python_commands:
        changes.append("config:retired-memory-python")
    if config_rewritten:
        changes.append("config:descriptions.retired-memory")
    return updated, changes


def instruction_paths(home: Path) -> list[Path]:
    paths = []
    for root in (home, home / "workspace"):
        for name in ("AGENTS.md", "SOUL.md", "TOOLS.md", "USER.md"):
            path = root / name
            if path.is_file():
                paths.append(path)
    shared_rules = home / "shared-rules"
    if shared_rules.is_dir():
        paths.extend(sorted(shared_rules.glob("*.md")))
    for profile in sorted((home / "profiles").glob("*")):
        if not profile.is_dir():
            continue
        if ".archive-" in profile.name:
            continue
        for name in ("AGENTS.md", "SOUL.md", "TOOLS.md", "USER.md"):
            path = profile / name
            if path.is_file():
                paths.append(path)
        profile_rules = profile / "shared-rules"
        if profile_rules.is_dir():
            paths.extend(sorted(profile_rules.glob("*.md")))
    return list(dict.fromkeys(paths))


def retired_scheduler_paths(home: Path) -> list[Path]:
    """Return user-owned scheduler definitions that still invoke retired memory."""
    account_home = home.parent
    roots = (
        account_home / ".config" / "systemd" / "user",
        account_home / "Library" / "LaunchAgents",
    )
    matches: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            try:
                if not path.is_file() or path.stat().st_size > 1024 * 1024:
                    continue
                searchable = f"{path.name}\n{path.read_text(encoding='utf-8', errors='replace')}".lower()
            except OSError:
                continue
            if any(token in searchable for token in RETIRED_SCHEDULER_TOKENS):
                matches.append(path)
    return matches


def active_directives(path: Path, text: str) -> list[dict[str, Any]]:
    findings = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if (
            LEGACY_TERM_RE.search(line)
            and not NEGATIVE_CONTEXT_RE.search(line)
            and not NEUTRAL_DEFINITION_RE.search(line)
        ):
            findings.append({"path": str(path), "line": line_number})
    return findings


def plan_changes(
    home: Path, rules_source: Path, hermes_python: str | None = None
) -> tuple[dict[Path, str], list[Path], list[str], list[dict[str, Any]]]:
    replacements: dict[Path, str] = {}
    changes: list[str] = []
    findings: list[dict[str, Any]] = []
    config = home / "config.yaml"
    if config.is_file():
        original = config.read_text(encoding="utf-8")
        updated, config_changes = normalize_config(original, hermes_python)
        if updated != original:
            replacements[config] = updated
            changes.extend(config_changes)

    canonical_rules = {
        name: (rules_source / name).read_text(encoding="utf-8")
        for name in CANONICAL_RULE_NAMES
    }
    for path in instruction_paths(home):
        original = path.read_text(encoding="utf-8", errors="strict")
        updated = replacements.get(path, original)
        if path.name == "knowledge-store-policy.md" and (
            LEGACY_POLICY_TITLE in updated or active_directives(path, updated)
        ):
            updated = canonical_rules[path.name]
            changes.append(f"policy:{path.relative_to(home).as_posix()}")
        elif path.name in {"capability-discovery.md", "knowledge-routing.md"} and active_directives(path, updated):
            updated = canonical_rules[path.name]
            changes.append(f"policy:{path.relative_to(home).as_posix()}")
        if LEGACY_AGENT_SENTENCE in updated:
            updated = updated.replace(LEGACY_AGENT_SENTENCE, NATIVE_AGENT_SENTENCE)
            changes.append(f"instruction:{path.relative_to(home).as_posix()}")
        for legacy, replacement in KNOWN_TEXT_REWRITES.items():
            if legacy in updated:
                updated = updated.replace(legacy, replacement)
                changes.append(f"instruction:{path.relative_to(home).as_posix()}")
        updated, anamnesis_inventory_count = re.subn(
            r"(?m)^- `anamnesis` — semantic recall over[^\r\n]*(?:\r?\n)?",
            "- Native Hermes memory handles nearby context; durable records go to GBrain.\n",
            updated,
        )
        updated, qdrant_inventory_count = re.subn(
            r"(?mi)^- Qdrant collection:[^\r\n]*(?:\r?\n)?",
            "",
            updated,
        )
        updated, qdrant_endpoint_count = re.subn(
            r"(?mi)^- \*\*Qdrant\*\*\s+—[^\r\n]*(?:\r?\n)?",
            "",
            updated,
        )
        if (
            anamnesis_inventory_count
            or qdrant_inventory_count
            or qdrant_endpoint_count
        ):
            changes.append(f"instruction:{path.relative_to(home).as_posix()}")
        if updated != original:
            replacements[path] = updated

    for path in instruction_paths(home):
        text = replacements.get(path)
        if text is None:
            text = path.read_text(encoding="utf-8", errors="strict")
        findings.extend(active_directives(path, text))
    scheduler_paths = retired_scheduler_paths(home)
    changes.extend(f"scheduler:{path.name}" for path in scheduler_paths)
    return replacements, scheduler_paths, list(dict.fromkeys(changes)), findings


def _rollback_member(home: Path, path: Path) -> tuple[str, Path]:
    resolved_home = home.resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(resolved_home):
        return "hermes_home", resolved.relative_to(resolved_home)
    account_home = resolved_home.parent
    if resolved.is_relative_to(account_home):
        return "account_home", resolved.relative_to(account_home)
    raise ValueError(f"retirement path is outside the runtime account: {path}")


def _prepare_scheduler_retirement(path: Path) -> dict[str, Any]:
    """Stop a retired user scheduler before any source files are changed."""
    action: dict[str, Any] = {"path": str(path), "command": None, "returncode": None}
    if ".config/systemd/user" in path.as_posix() and path.suffix == ".service":
        command = ["systemctl", "--user", "disable", "--now", path.name]
    elif "/Library/LaunchAgents/" in path.as_posix() and path.suffix == ".plist":
        command = ["launchctl", "bootout", f"gui/{os.getuid()}", str(path)]
    else:
        command = []
    if command and shutil.which(command[0]):
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        action["command"] = command[:3]
        action["returncode"] = proc.returncode
        # systemctl returns 0 for an already-disabled inactive unit. launchctl
        # returns non-zero when a plist was not loaded, which is also safe to retire.
        if command[0] == "systemctl" and proc.returncode != 0:
            raise RuntimeError(
                f"unable to stop retired scheduler {path.name}: {proc.stderr[-240:]}"
            )
    return action


def _reload_scheduler_managers(paths: list[Path]) -> None:
    if any(
        ".config/systemd/user" in path.as_posix() and path.suffix == ".service"
        for path in paths
    ) and shutil.which("systemctl"):
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            timeout=30,
            check=False,
        )


def apply_replacements(
    home: Path, replacements: dict[Path, str], scheduler_paths: list[Path]
) -> tuple[Path, list[dict[str, Any]]]:
    rollback = home / "state" / "rollbacks" / f"retired-memory-surfaces-{utc_stamp()}"
    rollback.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "files": []}
    scheduler_actions = [
        _prepare_scheduler_retirement(path) for path in scheduler_paths
    ]
    for path, updated in replacements.items():
        scope, relative = _rollback_member(home, path)
        backup = rollback / scope / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        tmp = path.with_name(f".{path.name}.retired-memory-{os.getpid()}")
        tmp.write_text(updated, encoding="utf-8")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
        manifest["files"].append(
            {"scope": scope, "path": relative.as_posix(), "operation": "replace"}
        )
    for path in scheduler_paths:
        scope, relative = _rollback_member(home, path)
        backup = rollback / scope / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.unlink()
        manifest["files"].append(
            {"scope": scope, "path": relative.as_posix(), "operation": "retire"}
        )
    _reload_scheduler_managers(scheduler_paths)
    (rollback / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return rollback, scheduler_actions


def restore(home: Path, rollback: Path) -> dict[str, Any]:
    manifest = json.loads((rollback / "manifest.json").read_text(encoding="utf-8"))
    restored = []
    for row in manifest.get("files") or []:
        relative = Path(str(row["path"]))
        scope = str(row.get("scope") or "hermes_home")
        root = home if scope == "hermes_home" else home.parent if scope == "account_home" else None
        if root is None:
            raise ValueError(f"invalid rollback scope: {scope}")
        source = rollback / scope / relative
        destination = root / relative
        if not source.is_file() or not destination.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"invalid rollback member: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        restored.append(relative.as_posix())
    _reload_scheduler_managers(
        [
            (home if str(row.get("scope") or "hermes_home") == "hermes_home" else home.parent)
            / Path(str(row["path"]))
            for row in manifest.get("files") or []
            if row.get("operation") == "retire"
        ]
    )
    return {"ok": True, "status": "restored", "restored": restored, "rollback": str(rollback)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--rules-source", type=Path)
    parser.add_argument("--hermes-python")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args(argv)
    home = args.hermes_home.expanduser().resolve()

    if args.rollback:
        payload = restore(home, args.rollback.expanduser().resolve())
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.rules_source is None or not args.rules_source.is_dir():
        parser.error("--rules-source must name Golden's canonical shared-rules directory")
    missing_rules = sorted(
        name for name in CANONICAL_RULE_NAMES if not (args.rules_source / name).is_file()
    )
    if missing_rules:
        parser.error("--rules-source is missing: " + ", ".join(missing_rules))

    replacements, scheduler_paths, changes, findings = plan_changes(
        home, args.rules_source, args.hermes_python
    )
    if findings:
        payload = {
            "ok": False,
            "status": "blocked_unknown_directive",
            "changes": changes,
            "findings": findings,
            "credential_values_recorded": False,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    if not replacements and not scheduler_paths:
        payload = {
            "ok": True,
            "status": "idempotent",
            "changes": [],
            "credential_values_recorded": False,
        }
    elif args.dry_run or not args.apply:
        payload = {
            "ok": True,
            "status": "would_update",
            "changes": changes,
            "credential_values_recorded": False,
        }
    else:
        rollback, scheduler_actions = apply_replacements(
            home, replacements, scheduler_paths
        )
        payload = {
            "ok": True,
            "status": "installed",
            "changes": changes,
            "rollback": str(rollback),
            "scheduler_actions": scheduler_actions,
            "credential_values_recorded": False,
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
