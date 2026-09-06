#!/usr/bin/env python3
"""Install the reviewed bounded-answer tool-guardrail repair."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PAYLOAD_DIR = Path(__file__).resolve().parents[1] / "payloads" / "tool-guardrail-answer-v1"
MANIFEST_PATH = PAYLOAD_DIR / "manifest.json"
MARKER_RELATIVE = Path(".golden-runtime-carriers/tool-guardrail-answer-v1.json")
IDEMPOTENCY = "HERMES_TOOL_GUARDRAIL_ANSWER_CARRIER_v1"
KNOWN_SEMANTIC_CHECKS = {
    "answer_only_guardrail_recovery",
    "consecutive_empty_web_search",
    "guardrail_recovery_finalization",
    "sequential_web_search_batches",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob_oid(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
        usedforsecurity=False,
    ).hexdigest()


def _git_object_oid(kind: str, data: bytes) -> str:
    return hashlib.sha1(
        kind.encode("ascii") + b" " + str(len(data)).encode("ascii") + b"\0" + data,
        usedforsecurity=False,
    ).hexdigest()


def _verify_reviewed_source_bundle(
    provenance: Path, manifest: dict[str, Any]
) -> None:
    seed_object = PAYLOAD_DIR / str(manifest.get("source_seed_commit") or "")
    if (
        not seed_object.is_file()
        or _sha256(seed_object) != manifest.get("source_seed_commit_sha256")
        or _git_object_oid("commit", seed_object.read_bytes())
        != manifest["source_seed_head"]
    ):
        raise RuntimeError("tool-guardrail answer carrier seed commit mismatch")

    with tempfile.TemporaryDirectory(prefix="golden-toolguardrail-provenance-") as tmp:
        repo = Path(tmp)
        commands = (
            (["git", "init", "-q", str(repo)], None),
            (
                ["git", "-C", str(repo), "hash-object", "-w", "-t", "commit", "--stdin"],
                seed_object.read_bytes(),
            ),
            (
                [
                    "git",
                    "-C",
                    str(repo),
                    "update-ref",
                    "refs/heads/source-seed",
                    manifest["source_seed_head"],
                ],
                None,
            ),
            (["git", "-C", str(repo), "bundle", "unbundle", str(provenance)], None),
        )
        for command, input_bytes in commands:
            result = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    "tool-guardrail answer carrier provenance unpack failed"
                )

        commit = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "cat-file",
                "commit",
                manifest["reviewed_source_head"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if commit.returncode:
            raise RuntimeError("tool-guardrail answer carrier reviewed commit missing")
        header = commit.stdout.split("\n\n", 1)[0].splitlines()
        trees = [line.split(maxsplit=1)[1] for line in header if line.startswith("tree ")]
        parents = [
            line.split(maxsplit=1)[1] for line in header if line.startswith("parent ")
        ]
        if trees != [manifest["reviewed_source_tree"]] or parents != [
            manifest["reviewed_source_parent"]
        ]:
            raise RuntimeError("tool-guardrail answer carrier reviewed commit mismatch")
        for relative, expected in manifest["postimage_git_blobs"].items():
            postimage = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "rev-parse",
                    f"{manifest['reviewed_source_head']}:{relative}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if postimage.returncode or postimage.stdout.strip() != expected:
                raise RuntimeError(
                    "tool-guardrail answer carrier reviewed postimage mismatch: "
                    + relative
                )


def _load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise RuntimeError("unsupported tool-guardrail answer carrier manifest")
    for field in (
        "base_commit",
        "source_seed_head",
        "reviewed_source_head",
        "reviewed_source_parent",
        "reviewed_source_tree",
        "payload_finalized_against_golden_parent",
    ):
        value = manifest.get(field)
        if not isinstance(value, str) or len(value) != 40 or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise RuntimeError(f"tool-guardrail answer carrier {field} is invalid")
    if manifest["reviewed_source_parent"] != manifest["source_seed_head"]:
        raise RuntimeError("tool-guardrail answer carrier source parent mismatch")
    payload = PAYLOAD_DIR / str(manifest.get("patch") or "")
    if not payload.is_file() or _sha256(payload) != manifest.get("patch_sha256"):
        raise RuntimeError("tool-guardrail answer carrier payload mismatch")
    provenance = PAYLOAD_DIR / str(manifest.get("provenance_bundle") or "")
    if (
        not provenance.is_file()
        or _sha256(provenance) != manifest.get("provenance_bundle_sha256")
    ):
        raise RuntimeError("tool-guardrail answer carrier provenance bundle mismatch")
    bundle_header = provenance.read_bytes().split(b"\n\n", 1)[0]
    prerequisites = [
        line[1:41].decode("ascii", errors="ignore")
        for line in bundle_header.splitlines()
        if line.startswith(b"-")
    ]
    if prerequisites != [manifest["source_seed_head"]]:
        raise RuntimeError("tool-guardrail answer carrier provenance base mismatch")
    bundle_heads = subprocess.run(
        ["git", "bundle", "list-heads", str(provenance)],
        capture_output=True,
        text=True,
        check=False,
    )
    advertised = [
        line.split(maxsplit=1)[0]
        for line in bundle_heads.stdout.splitlines()
        if line.strip()
    ]
    if bundle_heads.returncode or advertised != [manifest["reviewed_source_head"]]:
        raise RuntimeError("tool-guardrail answer carrier provenance head mismatch")
    _verify_reviewed_source_bundle(provenance, manifest)
    postimages = manifest.get("postimage_git_blobs")
    fragments = manifest.get("required_fragments")
    if (
        not isinstance(postimages, dict)
        or set(postimages) != set(fragments or {})
        or any(not isinstance(items, list) or not items for items in (fragments or {}).values())
    ):
        raise RuntimeError("tool-guardrail answer carrier postimage contract is invalid")
    semantic_checks = manifest.get("semantic_checks", [])
    if (
        not isinstance(semantic_checks, list)
        or len(semantic_checks) != len(set(semantic_checks))
        or any(check not in KNOWN_SEMANTIC_CHECKS for check in semantic_checks)
    ):
        raise RuntimeError("tool-guardrail answer carrier semantic contract is invalid")
    return manifest


def _marker_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotency": IDEMPOTENCY,
        "base_commit": manifest["base_commit"],
        "source_seed_head": manifest["source_seed_head"],
        "reviewed_source_head": manifest["reviewed_source_head"],
        "reviewed_source_tree": manifest["reviewed_source_tree"],
        "payload_finalized_against_golden_parent": manifest[
            "payload_finalized_against_golden_parent"
        ],
        "patch_sha256": manifest["patch_sha256"],
        "provenance_bundle_sha256": manifest["provenance_bundle_sha256"],
        "required_fragments": manifest["required_fragments"],
        "semantic_checks": manifest.get("semantic_checks", []),
    }


def _write_marker(root: Path, manifest: dict[str, Any]) -> None:
    marker = root / MARKER_RELATIVE
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(_marker_payload(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def _fragment_mismatches(root: Path, manifest: dict[str, Any]) -> list[str]:
    mismatches = []
    for relative, fragments in manifest["required_fragments"].items():
        path = root / relative
        if not path.is_file():
            mismatches.append(relative)
            continue
        source = path.read_text(encoding="utf-8")
        if any(fragment not in source for fragment in fragments):
            mismatches.append(relative)
    for relative in _semantic_mismatches(root, manifest):
        if relative not in mismatches:
            mismatches.append(relative)
    return mismatches


def _name(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Name) and node.id == value


def _attribute(node: ast.AST, owner: str, value: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == value
        and _name(node.value, owner)
    )


def _assigns_empty_search_zero(nodes: list[ast.stmt]) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(
            _attribute(target, "self", "_empty_web_search_streak")
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value == 0
        for node in nodes
    )


def _consecutive_empty_search_semantics(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    constant_ok = any(
        isinstance(node, ast.Assign)
        and any(_name(target, "EMPTY_WEB_SEARCH_HALT_AFTER") for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value == 8
        for node in tree.body
    )
    controller = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "ToolCallGuardrailController"
        ),
        None,
    )
    if not constant_ok or controller is None:
        return False
    methods = {
        node.name: node
        for node in controller.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reset = methods.get("reset_for_turn")
    before = methods.get("before_call")
    after = methods.get("after_call")
    preserves_post_call_halt = before is not None and any(
        isinstance(node, ast.If)
        and any(
            _attribute(child, "self", "_halt_decision")
            for child in ast.walk(node.test)
        )
        and any(
            isinstance(child, ast.Return)
            and _attribute(child.value, "self", "_halt_decision")
            for child in node.body
        )
        for node in before.body
    )
    if (
        reset is None
        or after is None
        or not _assigns_empty_search_zero(reset.body)
        or not preserves_post_call_halt
    ):
        return False
    for outer in ast.walk(after):
        if not (
            isinstance(outer, ast.If)
            and isinstance(outer.test, ast.Compare)
            and _name(outer.test.left, "tool_name")
            and len(outer.test.ops) == 1
            and isinstance(outer.test.ops[0], ast.Eq)
            and len(outer.test.comparators) == 1
            and isinstance(outer.test.comparators[0], ast.Constant)
            and outer.test.comparators[0].value == "web_search"
        ):
            continue
        for empty_branch in outer.body:
            if not isinstance(empty_branch, ast.If):
                continue
            has_not_failed = any(
                isinstance(node, ast.UnaryOp)
                and isinstance(node.op, ast.Not)
                and _name(node.operand, "failed")
                for node in ast.walk(empty_branch.test)
            )
            has_empty_result_call = any(
                isinstance(node, ast.Call)
                and _name(node.func, "_web_search_result_is_empty")
                for node in ast.walk(empty_branch.test)
            )
            if not has_not_failed or not has_empty_result_call:
                continue
            resets_failed_or_nonempty = _assigns_empty_search_zero(empty_branch.orelse)
            increments_streak = any(
                isinstance(node, ast.AugAssign)
                and _attribute(node.target, "self", "_empty_web_search_streak")
                and isinstance(node.op, ast.Add)
                and isinstance(node.value, ast.Constant)
                and node.value.value == 1
                for node in empty_branch.body
            )
            counter_is_streak = any(
                isinstance(node, ast.Assign)
                and any(_name(target, "empty_count") for target in node.targets)
                and _attribute(node.value, "self", "_empty_web_search_streak")
                for node in empty_branch.body
            )
            halts_at_constant = False
            for threshold in empty_branch.body:
                if not (
                    isinstance(threshold, ast.If)
                    and isinstance(threshold.test, ast.Compare)
                    and _name(threshold.test.left, "empty_count")
                    and len(threshold.test.ops) == 1
                    and isinstance(threshold.test.ops[0], ast.GtE)
                    and len(threshold.test.comparators) == 1
                    and _name(
                        threshold.test.comparators[0],
                        "EMPTY_WEB_SEARCH_HALT_AFTER",
                    )
                ):
                    continue
                constructs_halt = any(
                    isinstance(node, ast.Assign)
                    and any(_name(target, "decision") for target in node.targets)
                    and isinstance(node.value, ast.Call)
                    and _name(node.value.func, "ToolGuardrailDecision")
                    and any(
                        keyword.arg == "action"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "halt"
                        for keyword in node.value.keywords
                    )
                    and any(
                        keyword.arg == "code"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "repeated_empty_web_search_halt"
                        for keyword in node.value.keywords
                    )
                    for node in threshold.body
                )
                stores_halt = any(
                    isinstance(node, ast.Assign)
                    and any(
                        _attribute(target, "self", "_halt_decision")
                        for target in node.targets
                    )
                    and _name(node.value, "decision")
                    for node in threshold.body
                )
                returns_halt = any(
                    isinstance(node, ast.Return) and _name(node.value, "decision")
                    for node in threshold.body
                )
                if constructs_halt and stores_halt and returns_halt:
                    halts_at_constant = True
                    break
            if (
                resets_failed_or_nonempty
                and increments_streak
                and counter_is_streak
                and halts_at_constant
            ):
                return True
    return False


def _answer_only_recovery_semantics(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    run = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "run_conversation"
        ),
        None,
    )
    if run is None:
        return False
    request_fence_initialized = any(
        isinstance(node, ast.Assign)
        and any(
            _name(target, "_guardrail_recovery_request_sent")
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
        for node in ast.walk(run)
    )
    one_retry_budget = any(
        isinstance(node, ast.Assign)
        and any(_name(target, "max_retries") for target in node.targets)
        and isinstance(node.value, ast.IfExp)
        and _name(node.value.test, "_guardrail_recovery_attempted")
        and isinstance(node.value.body, ast.Constant)
        and node.value.body.value == 1
        for node in ast.walk(run)
    )
    refuses_second_request = any(
        isinstance(node, ast.If)
        and {
            child.id
            for child in ast.walk(node.test)
            if isinstance(child, ast.Name)
        }
        >= {
            "_guardrail_recovery_attempted",
            "_guardrail_recovery_request_sent",
        }
        and any(
            isinstance(child, ast.Call)
            and _name(child.func, "_finish_guardrail_recovery_without_answer")
            for statement in node.body
            for child in ast.walk(statement)
        )
        for node in ast.walk(run)
    )
    marks_request_sent = any(
        isinstance(node, ast.Assign)
        and any(
            _name(target, "_guardrail_recovery_request_sent")
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
        for node in ast.walk(run)
    )
    stable_tool_schema = any(
        isinstance(node, ast.Assign)
        and any(_name(target, "tools_for_api") for target in node.targets)
        and _attribute(node.value, "agent", "tools")
        for node in ast.walk(run)
    )
    transport = next(
        (
            node
            for node in ast.walk(run)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_perform_api_call"
        ),
        None,
    )
    final_answer_only = transport is not None and all(
        any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and _name(target.value, "next_api_kwargs")
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == key
                for target in node.targets
            )
            and (
                isinstance(node.value, ast.Subscript)
                and _name(node.value.value, "api_kwargs")
                and isinstance(node.value.slice, ast.Constant)
                and node.value.slice.value == "tools"
                if key == "tools"
                else isinstance(node.value, ast.Constant)
                and node.value.value == expected
            )
            for node in ast.walk(transport)
        )
        for key, expected in (
            ("tools", None),
            ("tool_choice", "none"),
            ("parallel_tool_calls", False),
        )
    )
    anthropic_disables_tools = transport is not None and any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Dict)
        and any(
            isinstance(target, ast.Subscript)
            and _name(target.value, "next_api_kwargs")
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "tool_choice"
            for target in node.targets
        )
        and any(
            isinstance(key, ast.Constant)
            and key.value == "type"
            and isinstance(value, ast.Constant)
            and value.value == "none"
            for key, value in zip(node.value.keys, node.value.values)
        )
        for node in ast.walk(transport)
    )
    has_budget_grace = any(
        isinstance(node, ast.While)
        and {
            child.id
            for child in ast.walk(node.test)
            if isinstance(child, ast.Name)
        }
        >= {
            "_guardrail_recovery_attempted",
            "_guardrail_recovery_request_sent",
        }
        for node in ast.walk(run)
    )
    request_uses_tools_for_api = any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_build_api_kwargs"
        and any(
            keyword.arg == "tools_for_api"
            and _name(keyword.value, "tools_for_api")
            for keyword in call.keywords
        )
        for call in ast.walk(run)
    )
    if not all(
        (
            request_fence_initialized,
            one_retry_budget,
            refuses_second_request,
            marks_request_sent,
            stable_tool_schema,
            final_answer_only,
            anthropic_disables_tools,
            has_budget_grace,
            request_uses_tools_for_api,
        )
    ):
        return False
    for outer in ast.walk(run):
        if not (
            isinstance(outer, ast.If)
            and _attribute(outer.test, "assistant_message", "tool_calls")
        ):
            continue
        for recovery in outer.body:
            if not (
                isinstance(recovery, ast.If)
                and _name(recovery.test, "_guardrail_recovery_attempted")
                and recovery.body
                and isinstance(recovery.body[-1], ast.Break)
            ):
                continue
            has_controlled_halt = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_toolguard_controlled_halt_response"
                for node in ast.walk(recovery)
            )
            dispatches_tool = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_execute_tool_calls"
                for node in ast.walk(recovery)
            )
            if has_controlled_halt and not dispatches_tool:
                return True
    return False


def _sequential_web_search_batch_semantics(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    execute = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_execute_tool_calls_concurrent"
        ),
        None,
    )
    if execute is None:
        return False
    return any(
        isinstance(node, ast.If)
        and _positive_web_search_any(node.test)
        and any(
            isinstance(child, ast.Return)
            and isinstance(child.value, ast.Call)
            and isinstance(child.value.func, ast.Attribute)
            and child.value.func.attr == "_execute_tool_calls_sequential"
            for child in node.body
        )
        for node in ast.walk(execute)
    )


def _positive_web_search_any(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _name(node.func, "any")
        and len(node.args) == 1
        and any(
            isinstance(child, ast.Compare)
            and isinstance(child.left, ast.Attribute)
            and child.left.attr == "name"
            and len(child.ops) == 1
            and isinstance(child.ops[0], ast.Eq)
            and len(child.comparators) == 1
            and isinstance(child.comparators[0], ast.Constant)
            and child.comparators[0].value == "web_search"
            for child in ast.walk(node.args[0])
        )
    )


def _segmented_web_search_batch_semantics(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    execute = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "execute_tool_calls_segmented"
        ),
        None,
    )
    return execute is not None and any(
        isinstance(node, ast.If)
        and any(
            isinstance(child, ast.UnaryOp)
            and isinstance(child.op, ast.Not)
            and _positive_web_search_any(child.operand)
            for child in ast.walk(node.test)
        )
        and any(
            isinstance(child, ast.Call)
            and _name(child.func, "execute_tool_calls_sequential")
            for statement in node.orelse
            for child in ast.walk(statement)
        )
        for node in ast.walk(execute)
    )


def _guardrail_recovery_finalization_semantics(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    has_success_classification = any(
        isinstance(node, ast.Assign)
        and any(
            _name(target, "successful_guardrail_recovery")
            for target in node.targets
        )
        and any(
            isinstance(child, ast.Constant)
            and child.value == "guardrail_recovery_answer"
            for child in ast.walk(node.value)
        )
        for node in ast.walk(tree)
    )
    text_success_includes_recovery = any(
        isinstance(node, ast.Assign)
        and any(_name(target, "successful_text_response") for target in node.targets)
        and any(
            _name(child, "successful_guardrail_recovery")
            for child in ast.walk(node.value)
        )
        for node in ast.walk(tree)
    )
    completion_uses_text_success = any(
        isinstance(node, ast.Assign)
        and any(_name(target, "completed") for target in node.targets)
        and any(
            _name(child, "successful_text_response")
            for child in ast.walk(node.value)
        )
        for node in ast.walk(tree)
    )
    return (
        has_success_classification
        and text_success_includes_recovery
        and completion_uses_text_success
    )


def _semantic_mismatches(root: Path, manifest: dict[str, Any]) -> list[str]:
    checks = set(manifest.get("semantic_checks", []))
    mismatches = []
    conversation = root / "agent/conversation_loop.py"
    guardrails = root / "agent/tool_guardrails.py"
    runtime = root / "run_agent.py"
    executor = root / "agent/tool_executor.py"
    finalizer = root / "agent/turn_finalizer.py"
    if (
        "answer_only_guardrail_recovery" in checks
        and not _answer_only_recovery_semantics(conversation)
    ):
        mismatches.append("agent/conversation_loop.py")
    if (
        "consecutive_empty_web_search" in checks
        and not _consecutive_empty_search_semantics(guardrails)
    ):
        mismatches.append("agent/tool_guardrails.py")
    if (
        "sequential_web_search_batches" in checks
        and (
            not _sequential_web_search_batch_semantics(runtime)
            or not _segmented_web_search_batch_semantics(executor)
        )
    ):
        mismatches.extend(["run_agent.py", "agent/tool_executor.py"])
    if (
        "guardrail_recovery_finalization" in checks
        and not _guardrail_recovery_finalization_semantics(finalizer)
    ):
        mismatches.append("agent/turn_finalizer.py")
    return mismatches


def _postimage_mismatches(root: Path, manifest: dict[str, Any]) -> list[str]:
    return [
        relative
        for relative, expected in manifest["postimage_git_blobs"].items()
        if not (root / relative).is_file() or _git_blob_oid(root / relative) != expected
    ]


def _repo_head(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _apply_payload(root: Path, payload: Path, *, check: bool) -> None:
    command = ["git", "apply", "--no-index", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    command.append(str(payload))
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "git apply failed").strip()
        raise RuntimeError(f"tool-guardrail answer carrier rejected: {detail}")


def _guard_mixed_recovery_answers(root: Path, manifest: dict[str, Any]) -> bool:
    """Reject undispatched calls before the legacy recovery answer is persisted."""
    relative = "agent/conversation_loop.py"
    if relative not in manifest["postimage_git_blobs"]:
        return False
    path = root / relative
    source = path.read_text(encoding="utf-8")
    before = ('        final_response = agent._strip_think_blocks(response_content or "").strip()\n'
              '        if not final_response:\n')
    after = before.replace('if not final_response:',
                           'if getattr(response_message, "tool_calls", None) or not final_response:')
    if source.count(after) == 1 and before not in source:
        return False
    if source.count(before) != 1 or after in source:
        raise RuntimeError("tool-guardrail recovery message boundary drift")
    source = source.replace(before, after, 1)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    return True


def patch_tool_guardrail_answer_carrier_v1(root: Path) -> bool:
    """Apply the pin-rooted repair before dependent Golden loop patches."""
    root = Path(root).resolve()
    if _repo_head(root) == "d3630f853239e8c41ce7201e09fbdf39bcbc5431":
        return _patch_d363_tool_guardrail_answer(root)
    manifest = _load_manifest()
    marker = root / MARKER_RELATIVE

    if marker.exists():
        installed = json.loads(marker.read_text(encoding="utf-8"))
        if installed != _marker_payload(manifest):
            raise RuntimeError("tool-guardrail answer carrier marker provenance mismatch")
        mismatches = _fragment_mismatches(root, manifest)
        if mismatches:
            raise RuntimeError(
                "tool-guardrail answer carrier drifted: " + ", ".join(mismatches)
            )
        return _guard_mixed_recovery_answers(root, manifest)

    if not _postimage_mismatches(root, manifest):
        _guard_mixed_recovery_answers(root, manifest)
        _write_marker(root, manifest)
        return True

    head = _repo_head(root)
    if head is not None and head != manifest["base_commit"]:
        raise RuntimeError(
            f"tool-guardrail answer carrier requires base {manifest['base_commit']}, got {head}"
        )

    payload = PAYLOAD_DIR / manifest["patch"]
    _apply_payload(root, payload, check=True)
    _apply_payload(root, payload, check=False)
    mismatches = _postimage_mismatches(root, manifest)
    if mismatches:
        raise RuntimeError(
            "tool-guardrail answer carrier postimage verification failed: "
            + ", ".join(mismatches)
        )
    _guard_mixed_recovery_answers(root, manifest)
    _write_marker(root, manifest)
    return True

# Exact d363 carrier: separate from the provenance-bound cb5a8 repair above.
D363_PAYLOAD_DIR = Path(__file__).resolve().parents[1] / "payloads" / "tool-guardrail-answer-d363-v1"
D363_MANIFEST_PATH = D363_PAYLOAD_DIR / "manifest.json"
D363_MARKER_RELATIVE = Path(".golden-runtime-carriers/tool-guardrail-answer-d363-v1.json")


def _load_d363_manifest() -> dict[str, Any]:
    manifest = json.loads(D363_MANIFEST_PATH.read_text(encoding="utf-8"))
    payload = D363_PAYLOAD_DIR / str(manifest.get("patch") or "")
    if manifest.get("schema_version") != 1 or not payload.is_file() or _sha256(payload) != manifest.get("patch_sha256"):
        raise RuntimeError("d363 tool-guardrail carrier payload mismatch")
    if manifest.get("base_commit") != "d3630f853239e8c41ce7201e09fbdf39bcbc5431":
        raise RuntimeError("d363 tool-guardrail carrier base mismatch")
    fragments = manifest.get("required_fragments")
    initial = manifest.get("postimage_git_blobs")
    composed = manifest.get("composed_postimage_git_blobs")

    def _valid_blob_map(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and set(fragments).issubset(value)
            and all(
                isinstance(blob, str)
                and len(blob) == 40
                and all(char in "0123456789abcdef" for char in blob)
                for blob in value.values()
            )
        )

    if (
        not isinstance(fragments, dict)
        or not fragments
        or not _valid_blob_map(initial)
        or not _valid_blob_map(composed)
        or set(initial) != set(composed)
    ):
        raise RuntimeError("d363 tool-guardrail carrier postimage contract is invalid")
    return manifest


def _d363_postimage_mismatches(root: Path, postimages: dict[str, str]) -> list[str]:
    """Return exact blob mismatches for one reviewed source composition."""
    return [
        relative
        for relative, expected in postimages.items()
        if not (root / relative).is_file() or _git_blob_oid(root / relative) != expected
    ]


def _d363_postimage_variant(root: Path, manifest: dict[str, Any]) -> str | None:
    """Match only a reviewed full postimage; fragments are never an acceptance path."""
    if not _d363_postimage_mismatches(root, manifest["postimage_git_blobs"]):
        return "initial"
    if not _d363_postimage_mismatches(root, manifest["composed_postimage_git_blobs"]):
        return "ordered_checkpoint"
    return None


def _d363_marker_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    # The marker is an installation receipt for the initial exact postimage.  Later ports may
    # move the checked targets only to the separately reviewed composed full-postimage variant.
    return {
        "idempotency": "HERMES_D363_TOOL_GUARDRAIL_ANSWER_v1",
        "base_commit": manifest["base_commit"],
        "patch_sha256": manifest["patch_sha256"],
        "initial_postimage_git_blobs": manifest["postimage_git_blobs"],
    }


def _d363_marker_is_valid(installed: Any, manifest: dict[str, Any]) -> bool:
    expected = _d363_marker_payload(manifest)
    if installed == expected:
        return True
    # Compatibility for the initial receipt emitted before composed variants were introduced.
    return installed == {
        "idempotency": expected["idempotency"],
        "base_commit": expected["base_commit"],
        "patch_sha256": expected["patch_sha256"],
        "postimage_git_blobs": expected["initial_postimage_git_blobs"],
    }


def _patch_d363_tool_guardrail_answer(root: Path) -> bool:
    manifest = _load_d363_manifest()
    marker = root / D363_MARKER_RELATIVE
    if marker.exists():
        if not _d363_marker_is_valid(json.loads(marker.read_text(encoding="utf-8")), manifest):
            raise RuntimeError("d363 tool-guardrail carrier marker mismatch")
        if _d363_postimage_variant(root, manifest) is None:
            raise RuntimeError("d363 tool-guardrail carrier drifted")
        return False
    if _repo_head(root) != manifest["base_commit"]:
        raise RuntimeError(f"d363 tool-guardrail carrier requires base {manifest['base_commit']}, got {_repo_head(root)}")
    payload = D363_PAYLOAD_DIR / manifest["patch"]
    _apply_payload(root, payload, check=True)
    _apply_payload(root, payload, check=False)
    if _d363_postimage_variant(root, manifest) != "initial":
        raise RuntimeError("d363 tool-guardrail carrier postimage verification failed")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(_d363_marker_payload(manifest), sort_keys=True) + "\n", encoding="utf-8")
    return True
