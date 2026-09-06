#!/usr/bin/env python3
# ruff: noqa: E501 -- embedded upstream source anchors preserve exact lines
"""Keep cron receipts detailed while making human delivery glanceable."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_CRON_OPERATOR_DELIVERY_v1"
NO_EMPTY_SUCCESS_MARKER = "HERMES_CRON_NO_EMPTY_SUCCESS_NOISE_v1"
SELF_REMEDIATION_MARKER = "HERMES_CRON_SELF_REMEDIATION_v1"
LONG_SUCCESS_DELIVERY_MARKER = "HERMES_CRON_LONG_SUCCESS_DELIVERY_v1"
PLAIN_SUCCESS_DELIVERY_MARKER = "HERMES_CRON_PLAIN_SUCCESS_DELIVERY_v1"
JOB_ITERATION_BUDGET_MARKER = "HERMES_CRON_JOB_ITERATION_BUDGET_v1"
TARGET = Path("cron/scheduler.py")
CONTENT_POLICY_TARGET = Path("agent/conversation_loop.py")

HELPER_ANCHOR = """def _parse_wake_gate(script_output: str) -> bool:
"""

HELPER_SOURCE = r"""# __MARKER__
# HERMES_CRON_NO_EMPTY_SUCCESS_NOISE_v1
# HERMES_CRON_SELF_REMEDIATION_v1
_CRON_OPERATOR_FALLBACK = SILENT_MARKER
_CRON_OPERATOR_WITHHELD = (
    "completed, but its result was withheld because it did not meet the delivery "
    "safety contract. Review the saved receipt."
)


class _CronOperatorFailure(str):
    def __new__(cls, value: str, kind: str):
        instance = super().__new__(cls, value)
        instance.kind = kind
        return instance


def _cron_operator_failure_exception(kind: str, exception_type, message: str) -> Exception:
    exc = exception_type(message)
    exc._cron_operator_kind = kind
    return exc


def _cron_operator_has_unicode_control(text: str) -> bool:
    import unicodedata

    return any(unicodedata.category(character) in {"Cf", "Cs", "Zl", "Zp"} for character in text)


def _cron_operator_job_name(job_name: str) -> str:
    raw_name = str(job_name or "")
    name = " ".join(raw_name.split())
    if name.lower().endswith(" cron"):
        name = name[:-5].rstrip()
    unsafe = (
        not name
        or len(name) > 80
        or bool(re.fullmatch(r"(?:[0-9a-fA-F]{12}|[0-9]{6,}|[0-9a-fA-F-]{32,})", name))
        or _cron_operator_has_hard_detail(raw_name)
    )
    return "Scheduled job" if unsafe else name


def _cron_operator_has_hard_detail(text: str) -> bool:
    from gateway.platforms.base import MEDIA_DELIVERY_EXTS

    if not text or "\n" in text or "\r" in text:
        return True
    if re.search(r"[\x00-\x1f\x7f-\x9f]", text):
        return True
    if _cron_operator_has_unicode_control(text):
        return True
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(value, (dict, list)):
            return True
    if re.search(
        r"(?<!\w)(?:[A-Za-z][A-Za-z0-9+.-]*:/{1,3}|~[/\\]|\.\.?[/\\]|"
        r"[/\\]|[A-Za-z]:[/\\])\S+",
        text,
    ):
        return True
    for match in re.finditer(
        r"(?<![@\w])(?:[A-Za-z0-9_-]+\.)+([A-Za-z0-9]+)\b|"
        r"(?<!\w)\.([A-Za-z0-9]+)\b",
        text,
    ):
        extension = next(group for group in match.groups() if group)
        if (
            f".{extension.lower()}" in MEDIA_DELIVERY_EXTS
            or extension.isalpha()
            and extension.islower()
            and 2 <= len(extension) <= 10
        ):
            return True
    if re.search(
        r"\b(?:(?i:(?:[A-Za-z][A-Za-z0-9]*[_-])*"
        r"(?:id|pid|uuid|token|trace[_ -]?id))|"
        r"[A-Za-z][A-Za-z0-9]*(?:Id|ID|Token|TOKEN))\s*[:=#]\s*[A-Za-z0-9_-]+\b",
        text,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:[0-9a-f]{12,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b",
            text,
            re.I,
        )
    )

def _cron_operator_failure_message(failure_kind: str) -> str:
    if failure_kind == "blocked_config":
        failure_kind = "configuration"
    return {
        "safety": (
            "was stopped by a safety check. No action was taken; the saved receipt "
            "records the protected boundary."
        ),
        "configuration": (
            "was blocked by configuration, and automatic recovery did not complete. "
            "The saved receipt records the remaining blocker."
        ),
        "script": (
            "failed in its script or runtime, and automatic recovery did not "
            "complete. The saved receipt records the failure and repair attempt."
        ),
        "runtime": (
            "failed in the local runtime, and automatic recovery did not complete. "
            "The saved receipt records both attempts."
        ),
        "interrupted": (
            "was interrupted during gateway shutdown. Its next scheduled run "
            "remains enabled."
        ),
        "provider_auth": (
            "could not authenticate with its configured provider. No credentials "
            "were changed automatically."
        ),
        "provider_limit": (
            "stopped at a provider limit. No quota or billing change was made."
        ),
        "timeout": (
            "timed out, and one automatic recovery attempt did not complete. "
            "The saved receipt records both attempts."
        ),
        "execution": (
            "failed during execution, and automatic recovery did not complete. "
            "The saved receipt records both attempts."
        ),
        "pre_repair": (
            "failed before automatic recovery could start. The saved receipt "
            "records the runtime failure."
        ),
    }.get(
        failure_kind,
        "failed during execution, and automatic recovery did not complete. "
        "The saved receipt records both attempts.",
    )


_CRON_REPAIRABLE_FAILURE_KINDS = frozenset(
    {"configuration", "script", "runtime", "timeout", "execution"}
)
_CRON_REPAIR_RECOVERED_PREFIXES = (
    "repaired and verified:",
    "recovered and verified:",
)
_CRON_REPAIR_STOPPED_PREFIX = "automatic repair stopped:"
_CRON_REPAIR_CIRCUIT_SECONDS = 4 * 60 * 60


def _cron_repair_signature(
    job: dict, failure_kind: str, output_file: Path, failure_detail: object = None
) -> str:
    import hashlib
    import os

    target_sha = ""
    roots = []
    if os.environ.get("HERMES_HOME"):
        roots.append(Path(os.environ["HERMES_HOME"]))
    roots.extend(output_file.parents[:3])
    for root in roots:
        marker = root / "state" / "golden-target-sha"
        try:
            target_sha = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if target_sha:
            break
    basis = {
        "job": str(job.get("id") or job.get("name") or "unknown"),
        "failure_kind": failure_kind,
        # Volatile diagnostic text must not bypass the repair cooldown.
        "schedule": job.get("schedule") or job.get("cron"),
        "script": job.get("script"),
        "monitor_script": job.get("monitor_script"),
        "enabled_toolsets": job.get("enabled_toolsets"),
        "provider": job.get("provider"),
        "model": job.get("model"),
        "target_sha": target_sha,
    }
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _cron_repair_circuit_path(job: dict, output_file: Path) -> Path:
    import hashlib

    identity = str(job.get("id") or job.get("name") or "unknown")
    name = hashlib.sha256(identity.encode()).hexdigest()[:24] + ".json"
    return output_file.parent / ".repair-circuit" / name


def _cron_repair_circuit_open(
    job: dict, failure_kind: str, output_file: Path, failure_detail: object = None
) -> bool:
    import time

    path = _cron_repair_circuit_path(job, output_file)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        state.get("status") == "failed"
        and state.get("signature")
        == _cron_repair_signature(job, failure_kind, output_file, failure_detail)
        and time.time() - float(state.get("failed_at_epoch") or 0)
        < _CRON_REPAIR_CIRCUIT_SECONDS
    )


def _set_cron_repair_circuit(
    job: dict,
    failure_kind: str,
    output_file: Path,
    *,
    failed: bool,
    failure_detail: object = None,
) -> None:
    import os
    import time

    path = _cron_repair_circuit_path(job, output_file)
    if not failed:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "status": "failed",
        "signature": _cron_repair_signature(
            job, failure_kind, output_file, failure_detail
        ),
        "failed_at_epoch": time.time(),
        "cooldown_seconds": _CRON_REPAIR_CIRCUIT_SECONDS,
    }
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _cron_repair_prompt(job: dict, failure_kind: str, output_file: Path) -> str:
    name = _cron_operator_job_name(job.get("name") or job.get("id"))
    return (
        "A scheduled job failed and this is an internal repair turn, not an "
        "operator handoff. Inspect the saved receipt and the owning job's local "
        "script/configuration, identify the cause, make the smallest safe local "
        "repair, and verify it proportionally. Preserve the original job's "
        "authorization and data boundaries. Do not change credentials, billing, "
        "permissions, destructive data, or public/client delivery outside the "
        "job's existing contract. Do not blindly rerun external side effects; "
        "rerun only after proving idempotency or by using a dry-run/read-only "
        "verification path. Never tell the operator to inspect logs, review the "
        "receipt, or repair/retry/reconfigure the system. If repaired, finish with "
        f"exactly one sentence beginning '{name} cron — repaired and verified:' "
        f"or '{name} cron — recovered and verified:'. If a real authority or "
        "safety boundary prevents repair, finish with exactly one sentence "
        f"beginning '{name} cron — automatic repair stopped:' and name the "
        "specific blocker plus what you already attempted. Keep paths, IDs, raw "
        "logs, and secrets in the receipt, not the final response.\n\n"
        f"Failure category: {failure_kind}\n"
        f"Saved receipt: {output_file}"
    )


def _cron_repair_outcome(job: dict, response: str) -> tuple[str, bool]:
    name = _cron_operator_job_name(job.get("name") or job.get("id"))
    required_prefix = f"{name} cron — "
    text = str(response or "").strip()
    if not text.startswith(required_prefix):
        return "", False
    body = text[len(required_prefix):].strip()
    lowered = body.lower()
    recovered = any(lowered.startswith(prefix) for prefix in _CRON_REPAIR_RECOVERED_PREFIXES)
    stopped = lowered.startswith(_CRON_REPAIR_STOPPED_PREFIX)
    if not (recovered or stopped):
        return "", False
    detail = body.split(":", 1)[1].strip() if ":" in body else body
    if re.match(
        r"(?i)^(?:please\s+)?(?:you\s+(?:need|must|should|can)\s+|"
        r"review\b|inspect\b|repair\b|fix\b|retry\b|rerun\b|restart\b|"
        r"reconfigure\b|configure\b)",
        detail,
    ):
        return "", False
    formatted = _format_cron_operator_delivery_with_media(
        name,
        text,
        success=True,
        job_lane="model",
        failure_kind="execution",
    )
    if not formatted or formatted == SILENT_MARKER:
        return "", False
    if recovered:
        return f"{name} cron — automatic repair attempted; the original run remains failed.", False
    return formatted, False


def _append_cron_repair_receipt(output_file: Path, repair_doc: str) -> None:
    try:
        with Path(output_file).open("a", encoding="utf-8") as handle:
            handle.write("\n\n## Automatic repair attempt\n\n")
            handle.write(str(repair_doc or "No repair receipt was produced.").strip())
            handle.write("\n")
    except Exception:
        logger.warning("Could not append automatic repair receipt", exc_info=True)


def _cron_repair_claim_lost(local_scope: dict) -> bool:
    if bool(local_scope.get("side_effect_ownership_lost", False)):
        return True
    probe = local_scope.get("_fire_claim_ownership_lost")
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:
        return True


def _attempt_cron_failure_remediation(
    job: dict,
    *,
    failure_kind: str,
    output_file: Path,
    deferred_agents: list,
    failure_detail: object = None,
) -> tuple[str, bool]:
    if job.get("no_agent"):
        name = _cron_operator_job_name(job.get("name") or job.get("id"))
        return f"{name} cron — failed in its script or runtime. Automatic repair skipped: script-only execution. Review the saved receipt.", False
    if failure_kind == "blocked_config":
        failure_kind = "configuration"
    if failure_kind not in _CRON_REPAIRABLE_FAILURE_KINDS:
        return "", False
    if _cron_repair_circuit_open(
        job, failure_kind, output_file, failure_detail
    ):
        name = _cron_operator_job_name(job.get("name") or job.get("id"))
        _append_cron_repair_receipt(
            output_file,
            "Automatic repair was not repeated because the same job configuration, "
            "runtime release, and failure class already failed inside the bounded cooldown.",
        )
        return (
            f"{name} cron — automatic repair paused: the same configuration and "
            "runtime already failed automatic recovery; the saved receipt records "
            "the suppressed repeat.",
            False,
        )

    repair_job = dict(job)
    repair_job.update(
        {
            "_cron_repair_attempt": True,
            "no_agent": False,
            "script": None,
            "monitor_script": None,
            "monitor_url": None,
            "monitor_state": None,
            "context_from": None,
            "skills": [],
            "skill": None,
            # Preserve the original job tool allowlist; native config still applies.
            # Keep job-bound provider, endpoint, model and snapshot constraints.
            "prompt": _cron_repair_prompt(job, failure_kind, output_file),
            "deliver": "local",
        }
    )
    try:
        repair_success, repair_doc, repair_response, _repair_error = run_job(
            repair_job,
            defer_agent_teardown=deferred_agents,
            extra_prompt=None,
        )
    except Exception:
        logger.warning("Automatic cron repair attempt raised", exc_info=True)
        _set_cron_repair_circuit(
            job,
            failure_kind,
            output_file,
            failed=True,
            failure_detail=failure_detail,
        )
        return "", False

    _append_cron_repair_receipt(output_file, repair_doc)
    if not repair_success:
        _set_cron_repair_circuit(
            job,
            failure_kind,
            output_file,
            failed=True,
            failure_detail=failure_detail,
        )
        return "", False
    delivery, recovered = _cron_repair_outcome(job, repair_response)
    _set_cron_repair_circuit(
        job,
        failure_kind,
        output_file,
        failed=not recovered,
        failure_detail=failure_detail,
    )
    return delivery, recovered


# HERMES_CRON_LONG_SUCCESS_DELIVERY_v1
# HERMES_CRON_PLAIN_SUCCESS_DELIVERY_v1
def _cron_operator_delivery_candidate(value: str) -> str:
    raw = str(value or "")
    # Line-oriented cron reports are ordinary operator prose. Preserve LF/CRLF
    # boundaries, while leaving tabs, lone CR, Unicode controls and all existing
    # path/identifier/JSON detail checks disqualifying.
    if "\r" in raw.replace("\r\n", ""):
        return ""
    normalized_newlines = raw.replace("\r\n", "\n")
    if _cron_operator_has_hard_detail(normalized_newlines.replace("\n", " ")):
        return ""
    normalized = "\n".join(
        " ".join(line.split()) for line in normalized_newlines.split("\n")
    ).strip()
    return normalized if normalized and len(normalized) <= 3500 else ""


def _format_cron_operator_delivery(
    job_name: str,
    output: str,
    *,
    success: bool,
    job_lane: str,
    failure_kind: str,
) -> str:
    name = _cron_operator_job_name(job_name)
    prefix = f"{name} cron — "
    if not success:
        return prefix + _cron_operator_failure_message(failure_kind)

    text = str(output or "").strip()
    if not text or _is_cron_silence_response(text):
        return SILENT_MARKER

    candidate = ""
    if job_lane == "script" and len(text) <= 4096:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            # Keep the existing message-then-summary fallback: a rejected
            # message must not hide a safe summary in the same receipt.
            for field in ("message", "summary"):
                value = payload.get(field)
                if isinstance(value, str):
                    candidate = _cron_operator_delivery_candidate(value)
                    if candidate:
                        break
        else:
            candidate = _cron_operator_delivery_candidate(text)
    elif job_lane == "model":
        required_prefix = f"{name} cron — "
        value = text[len(required_prefix):] if text.startswith(required_prefix) else text
        candidate = _cron_operator_delivery_candidate(value)

    return prefix + (candidate or _CRON_OPERATOR_WITHHELD)


def _format_cron_operator_delivery_with_media(
    job_name: str,
    output: str,
    *,
    success: bool,
    job_lane: str,
    failure_kind: str,
) -> str:
    from gateway.platforms.base import BasePlatformAdapter

    original = str(output or "")
    media_files, visible = BasePlatformAdapter.extract_media(original)
    formatted = _format_cron_operator_delivery(
        job_name,
        visible,
        success=success,
        job_lane=job_lane,
        failure_kind=failure_kind,
    )
    if formatted == SILENT_MARKER:
        if not media_files or not success or _is_cron_silence_response(visible):
            return formatted
        # A valid media directive is already the report. Do not add a
        # content-free success caption merely because visible text was empty.
        formatted = ""
    if not success or not media_files:
        return formatted

    directives = []
    if "[[as_document]]" in original:
        directives.append("[[as_document]]")
    if any(is_voice for _, is_voice in media_files):
        directives.append("[[audio_as_voice]]")
    directives.extend(f"MEDIA:{path}" for path, _ in media_files)
    return "\n".join(part for part in (formatted, *directives) if part)


def _is_cron_operator_delivery(content: str) -> bool:
    first_line = str(content or "").splitlines()[0] if content else ""
    if first_line.startswith(("MEDIA:", "[[as_document]]", "[[audio_as_voice]]")):
        return True
    return bool(re.match(r"^[^\r\n]{1,100} cron — ", first_line))


""".replace("__MARKER__", MARKER)

SELF_REMEDIATION_FAILURE_OLD = """def _cron_operator_failure_message(failure_kind: str) -> str:
    if failure_kind == "blocked_config":
        failure_kind = "configuration"
    return {
        "safety": (
            "was blocked by a safety check. Audit its prompt and configuration; "
            "the saved receipt has the reason."
        ),
        "configuration": (
            "was blocked by configuration. Repair the job setup; the saved receipt "
            "has the reason."
        ),
        "script": (
            "failed in its script or runtime. Review the saved receipt and repair "
            "the script or configuration."
        ),
        "runtime": "failed in the local runtime. Review the saved receipt and retry.",
        "interrupted": "was interrupted during gateway shutdown and needs to run again.",
        "provider_auth": (
            "could not authenticate with its configured provider. Repair the provider "
            "credentials; the saved receipt has the reason."
        ),
        "provider_limit": (
            "hit a provider limit. Check quota or billing and retry after reset; "
            "the saved receipt has the reason."
        ),
        "timeout": (
            "timed out before completing. Retry later; "
            "the saved receipt has the reason."
        ),
        "execution": "failed during execution. Review the saved receipt and retry.",
    }.get(failure_kind, "failed during execution. Review the saved receipt and retry.")
"""
_SELF_REMEDIATION_HELPER_START = HELPER_SOURCE.index("def _cron_operator_failure_message(")
_SELF_REMEDIATION_HELPER_END = HELPER_SOURCE.index(
    "\ndef _format_cron_operator_delivery(",
    _SELF_REMEDIATION_HELPER_START,
)
SELF_REMEDIATION_FAILURE_NEW = (
    f"# {SELF_REMEDIATION_MARKER}\n" + HELPER_SOURCE[_SELF_REMEDIATION_HELPER_START:_SELF_REMEDIATION_HELPER_END]
)

_LONG_SUCCESS_HELPER_START = HELPER_SOURCE.index(
    f"# {LONG_SUCCESS_DELIVERY_MARKER}\n"
)
_LONG_SUCCESS_CONSTANT_START = HELPER_SOURCE.index("_CRON_OPERATOR_WITHHELD = (")
_LONG_SUCCESS_CONSTANT_END = HELPER_SOURCE.index(
    "\n\n", _LONG_SUCCESS_CONSTANT_START
) + 2
LONG_SUCCESS_CONSTANT_NEW = HELPER_SOURCE[
    _LONG_SUCCESS_CONSTANT_START:_LONG_SUCCESS_CONSTANT_END
]
_LONG_SUCCESS_HELPER_END = HELPER_SOURCE.index(
    "\ndef _format_cron_operator_delivery_with_media(",
    _LONG_SUCCESS_HELPER_START,
)
LONG_SUCCESS_DELIVERY_NEW = HELPER_SOURCE[
    _LONG_SUCCESS_HELPER_START:_LONG_SUCCESS_HELPER_END
]


def _upgrade_long_success_delivery(source: str) -> str:
    if LONG_SUCCESS_DELIVERY_MARKER in source:
        start = source.index(f"# {LONG_SUCCESS_DELIVERY_MARKER}\n")
        end = source.index("\ndef _format_cron_operator_delivery_with_media(", start)
        current = source[start:end]
        if PLAIN_SUCCESS_DELIVERY_MARKER not in current:
            # A prior carrier treated formatting as a safety boundary: it silently
            # discarded ordinary script prose and model text without a display prefix.
            return source[:start] + LONG_SUCCESS_DELIVERY_NEW + source[end:]
        return source
    fallback = "_CRON_OPERATOR_FALLBACK = SILENT_MARKER\n"
    if LONG_SUCCESS_CONSTANT_NEW not in source:
        source = _replace_once(
            source,
            fallback,
            fallback + LONG_SUCCESS_CONSTANT_NEW,
            "long-success safety fallback",
        )
    old_start = source.index("def _format_cron_operator_delivery(")
    old_end = source.index(
        "\ndef _format_cron_operator_delivery_with_media(", old_start
    )
    return source[:old_start] + LONG_SUCCESS_DELIVERY_NEW + source[old_end:]

EMPTY_SUCCESS_FALLBACK_OLD = """_CRON_OPERATOR_FALLBACK = "completed. Details are available in the run receipt."
"""
EMPTY_SUCCESS_FALLBACK_NEW = """# HERMES_CRON_NO_EMPTY_SUCCESS_NOISE_v1
_CRON_OPERATOR_FALLBACK = SILENT_MARKER
"""

EMPTY_SUCCESS_RETURN_OLD = """    return prefix + (candidate or _CRON_OPERATOR_FALLBACK)
"""
EMPTY_SUCCESS_RETURN_NEW = """    return prefix + candidate if candidate else _CRON_OPERATOR_FALLBACK
"""

EMPTY_MEDIA_FALLBACK_OLD = """        name = _cron_operator_job_name(job_name)
        formatted = f"{name} cron — {_CRON_OPERATOR_FALLBACK}"
"""
EMPTY_MEDIA_FALLBACK_NEW = """        # A valid media directive is already the report. Do not add a
        # content-free success caption merely because visible text was empty.
        formatted = ""
"""

EMPTY_MEDIA_RETURN_OLD = """    return formatted + "\\n" + "\\n".join(directives)
"""
EMPTY_MEDIA_RETURN_NEW = """    return "\\n".join(part for part in (formatted, *directives) if part)
"""

MEDIA_ONLY_DELIVERY_OLD = """def _is_cron_operator_delivery(content: str) -> bool:
    first_line = str(content or "").splitlines()[0] if content else ""
    return bool(re.match(r"^[^\\r\\n]{1,100} cron — ", first_line))
"""
MEDIA_ONLY_DELIVERY_NEW = """def _is_cron_operator_delivery(content: str) -> bool:
    first_line = str(content or "").splitlines()[0] if content else ""
    if first_line.startswith(("MEDIA:", "[[as_document]]", "[[audio_as_voice]]")):
        return True
    return bool(re.match(r"^[^\\r\\n]{1,100} cron — ", first_line))
"""

CRON_HINT_OLD = """        "the output yourself. Just produce your report/output as your "
        "final response and the system handles the rest. "
        "SILENT: If there is genuinely nothing new to report, respond "
"""

CRON_HINT_PREVIOUS = """        "the output yourself. Just produce your report/output as your "
        "final response and the system handles the rest. "
        f"FORMAT: Start with '{_cron_operator_job_name(job.get('name') or job.get('id'))} "
        "cron — <plain-language outcome>'. Keep it to one clear sentence. "
        "Include numbers only when they change a decision. Never include IDs, "
        "log timestamps, paths, token or iteration counts, tool traces, raw JSON, "
        "or logs; those belong in the saved run receipt. "
        "SILENT: If there is genuinely nothing new to report, respond "
"""

CRON_HINT_NEW = CRON_HINT_PREVIOUS.replace(
    "        f\"FORMAT: Start with '{_cron_operator_job_name(job.get('name') or job.get('id'))} \"\n"
    "        \"cron — <plain-language outcome>'. Keep it to one clear sentence. \"\n",
    '        "FORMAT: Write a concise plain-language report. The system adds the job label. "\n',
)

DELIVERY_BOUNDARY_OLD = """            if blocked_config and not success:
                # Blocked-config alert: bypass the generic failure summarizer
                # (whose auth/timeout heuristics would mislabel this as a
                # provider runtime failure) — say plainly that config
                # validation blocked the run and nothing was spent.
                _pf_text = re.sub(
                    r"\\[blocked_config[^\\]]*\\]\\s*", "", str(error)
                ).strip()
                deliver_content = (
                    f"⛔ Cron '{job.get('name') or job['id']}' blocked by "
                    f"configuration validation (no LLM call was made): "
                    f"{_pf_text} "
                    "This alert is sent once; the job stays blocked until "
                    "the configuration is fixed."
                )
            else:
                deliver_content = final_response if success else _summarize_cron_failure_for_delivery(job, error)
            # Treat whitespace-only final responses the same as empty
"""

DELIVERY_BOUNDARY_LATEST_OLD = """            if blocked_config and not success:
                # Blocked-config alert: bypass the generic failure summarizer
                # (whose auth/timeout heuristics would mislabel this as a
                # provider runtime failure) — say plainly that config
                # validation blocked the run and nothing was spent.
                _pf_text = re.sub(
                    r"\\[blocked_config[^\\]]*\\]\\s*", "", str(error)
                ).strip()
                deliver_content = (
                    f"⛔ Cron '{job.get('name') or job['id']}' blocked by "
                    f"configuration validation (no LLM call was made): "
                    f"{_pf_text} "
                    "This alert is sent once; the job stays blocked until "
                    "the configuration is fixed."
                )
            else:
                deliver_content = final_response if success else _summarize_cron_failure_for_delivery(job, error)
                if drift_skip and not success:
                    # Drift-skip alert: bypass the generic summarizer's
                    # 180-char truncation (it would eat the remediation
                    # command) and strip the internal marker — deliver the
                    # guard's own actionable message intact.
                    _drift_text = re.sub(
                        r"\\[drift_skip[^\\]]*\\]\\s*", "", str(error)
                    ).strip()
                    deliver_content = (
                        f"⚠️ Cron '{job.get('name') or job['id']}' skipped: "
                        f"{_drift_text}"
                    )
            # Treat whitespace-only final responses the same as empty
"""

DELIVERY_BOUNDARY_STREAK_OLD = """            if blocked_config and not success:
                # Blocked-config alert: bypass the generic failure summarizer
                # (whose auth/timeout heuristics would mislabel this as a
                # provider runtime failure) — say plainly that config
                # validation blocked the run and nothing was spent.
                _pf_text = re.sub(
                    r"\\[blocked_config[^\\]]*\\]\\s*", "", str(error)
                ).strip()
                deliver_content = (
                    f"⛔ Cron '{job.get('name') or job['id']}' blocked by "
                    f"configuration validation (no LLM call was made): "
                    f"{_pf_text} "
                    "This alert is sent once; the job stays blocked until "
                    "the configuration is fixed."
                )
            else:
                deliver_content = final_response if success else (
                    _summarize_cron_failure_for_delivery(job, error)
                    + _failure_streak_nudge(job)
                )
                if drift_skip and not success:
                    # Drift-skip alert: bypass the generic summarizer's
                    # 180-char truncation (it would eat the remediation
                    # command) and strip the internal marker — deliver the
                    # guard's own actionable message intact.
                    _drift_text = re.sub(
                        r"\\[drift_skip[^\\]]*\\]\\s*", "", str(error)
                    ).strip()
                    deliver_content = (
                        f"⚠️ Cron '{job.get('name') or job['id']}' skipped: "
                        f"{_drift_text}"
                    )
            # Treat whitespace-only final responses the same as empty
"""

DELIVERY_BOUNDARY_INCIDENT_OLD = """            if blocked_config and not success:
                # Blocked-config alert: bypass the generic failure summarizer
                # (whose auth/timeout heuristics would mislabel this as a
                # provider runtime failure) — say plainly that config
                # validation blocked the run and nothing was spent.
                _pf_text = re.sub(
                    r"\\[blocked_config[^\\]]*\\]\\s*", "", str(error)
                ).strip()
                deliver_content = (
                    f"⛔ Cron '{job.get('name') or job['id']}' blocked by "
                    f"configuration validation (no LLM call was made): "
                    f"{_pf_text} "
                    "This alert is sent once; the job stays blocked until "
                    "the configuration is fixed."
                )
            else:
                if success:
                    deliver_content = final_response
                else:
                    # Durable failure incident: record this job+error
                    # signature once and, when the operator already acked it,
                    # suppress the per-run failure ping (the streak nudge and
                    # the failure summarizer stay intact for un-acked
                    # failures). Best-effort — an incident-store error never
                    # breaks the delivery path (see _upsert_incident_for_failure).
                    incident_acked, failure_incident_id = _upsert_incident_for_failure(
                        job, error or "", output_file=output_file
                    )
                    if incident_acked and not drift_skip:
                        deliver_content = ""
                    else:
                        deliver_content = (
                            _summarize_cron_failure_for_delivery(job, error)
                            + _failure_streak_nudge(job)
                        )
                if drift_skip and not success:
                    # Drift-skip alert: bypass the generic summarizer's
                    # 180-char truncation (it would eat the remediation
                    # command) and strip the internal marker — deliver the
                    # guard's own actionable message intact.
                    # Deliberately NOT gated on incident ack: a drift skip
                    # means the run was never attempted and the message
                    # carries the remediation command — acking the failure
                    # signature silences failure pings, not drift alerts
                    # (which already alert once via the drift_alerted marker).
                    _drift_text = re.sub(
                        r"\\[drift_skip[^\\]]*\\]\\s*", "", str(error)
                    ).strip()
                    deliver_content = (
                        f"⚠️ Cron '{job.get('name') or job['id']}' skipped: "
                        f"{_drift_text}"
                    )
            # Treat whitespace-only final responses the same as empty
"""

DELIVERY_BOUNDARY_V1 = """            deliver_content = _format_cron_operator_delivery_with_media(
                job.get("name") or job.get("id"),
                final_response if success else error,
                success=success,
                job_lane="script" if job.get("no_agent") else "model",
                failure_kind=failure_kind,
            )
            # Treat whitespace-only final responses the same as empty
"""

DELIVERY_BOUNDARY_NEW = """            remediation_delivery = ""
            remediation_recovered = False
            remediation_suppressed = blocked_config_silent or bool(
                locals().get("drift_skip_silent", False)
            ) or _cron_repair_claim_lost(locals())
            if not success and not remediation_suppressed:
                remediation_delivery, remediation_recovered = (
                    _attempt_cron_failure_remediation(
                        job,
                        failure_kind=failure_kind,
                        output_file=output_file,
                        deferred_agents=_deferred_agents,
                        failure_detail=error,
                    )
                )
                # A repair-model report cannot change the original workload outcome.
            if remediation_delivery:
                deliver_content = remediation_delivery
            elif blocked_config and not success:
                # Preserve upstream's actionable no-spend configuration alert
                # instead of leaking its internal marker through the generic
                # failure formatter.
                _pf_text = re.sub(
                    r"\\[blocked_config[^\\]]*\\]\\s*", "", str(error)
                ).strip()
                deliver_content = (
                    f"⛔ Cron '{job.get('name') or job['id']}' blocked by "
                    f"configuration validation (no LLM call was made): "
                    f"{_pf_text} "
                    "This alert is sent once; the job stays blocked until "
                    "the configuration is fixed."
                )
            else:
                deliver_content = _format_cron_operator_delivery_with_media(
                    job.get("name") or job.get("id"),
                    final_response if success else error,
                    success=success,
                    job_lane="script" if job.get("no_agent") else "model",
                    failure_kind=failure_kind,
                )
            # Treat whitespace-only final responses the same as empty
"""

DELIVERY_BOUNDARY_INCIDENT_NEW = """            remediation_delivery = ""
            remediation_recovered = False
            remediation_suppressed = blocked_config_silent or bool(
                locals().get("drift_skip_silent", False)
            ) or _cron_repair_claim_lost(locals())
            if not success and not remediation_suppressed:
                remediation_delivery, remediation_recovered = (
                    _attempt_cron_failure_remediation(
                        job,
                        failure_kind=failure_kind,
                        output_file=output_file,
                        deferred_agents=_deferred_agents,
                        failure_detail=error,
                    )
                )
                # A repair-model report cannot change the original workload outcome.
            if remediation_delivery:
                deliver_content = remediation_delivery
            elif blocked_config and not success:
                # Preserve upstream's actionable no-spend configuration alert
                # instead of leaking its internal marker through the generic
                # failure formatter.
                _pf_text = re.sub(
                    r"\\[blocked_config[^\\]]*\\]\\s*", "", str(error)
                ).strip()
                deliver_content = (
                    f"⛔ Cron '{job.get('name') or job['id']}' blocked by "
                    f"configuration validation (no LLM call was made): "
                    f"{_pf_text} "
                    "This alert is sent once; the job stays blocked until "
                    "the configuration is fixed."
                )
            else:
                deliver_content = _format_cron_operator_delivery_with_media(
                    job.get("name") or job.get("id"),
                    final_response if success else error,
                    success=success,
                    job_lane="script" if job.get("no_agent") else "model",
                    failure_kind=failure_kind,
                )
                if not success:
                    deliver_content += _failure_streak_nudge(job)
            # Preserve upstream's durable incident acknowledgement without
            # letting it bypass a successful bounded repair.
            if not success and not blocked_config:
                incident_acked, failure_incident_id = _upsert_incident_for_failure(
                    job, error or "", output_file=output_file
                )
                if incident_acked and not drift_skip:
                    deliver_content = ""
            if drift_skip and not success:
                # Preserve the complete upstream remediation command; the
                # generic formatter intentionally bounds ordinary failures.
                _drift_text = re.sub(
                    r"\\[drift_skip[^\\]]*\\]\\s*", "", str(error)
                ).strip()
                deliver_content = (
                    f"⚠️ Cron '{job.get('name') or job['id']}' skipped: "
                    f"{_drift_text}"
                )
            # Treat whitespace-only final responses the same as empty
"""

OUTER_EXCEPTION_DELIVERY_OLD = """                delivery_error = _deliver_result(
                    job,
                    _summarize_cron_failure_for_delivery(job, _err_text),
                    adapters=adapters,
                    loop=loop,
                )
"""

OUTER_EXCEPTION_DELIVERY_NEW = """                delivery_error = _deliver_result(
                    job,
                    _format_cron_operator_delivery_with_media(
                        job.get("name") or job.get("id"),
                        _err_text,
                        success=False,
                        job_lane="script" if job.get("no_agent") else "model",
                        failure_kind="pre_repair",
                    ),
                    adapters=adapters,
                    loop=loop,
                )
"""

OUTER_EXCEPTION_DELIVERY_LATEST_OLD = """                        _summarize_cron_failure_for_delivery(job, _err_text)
                        + _failure_streak_nudge(job),
"""

OUTER_EXCEPTION_DELIVERY_LATEST_NEW = """                        _format_cron_operator_delivery_with_media(
                            job.get("name") or job.get("id"),
                            _err_text,
                            success=False,
                            job_lane="script" if job.get("no_agent") else "model",
                            failure_kind="pre_repair",
                        )
                        + _failure_streak_nudge(job),
"""

WRAP_RESPONSE_OLD = """    if wrap_response:
        task_name = job.get("name", job["id"])
"""

WRAP_RESPONSE_NEW = """    if wrap_response and not _is_cron_operator_delivery(content):
        task_name = job.get("name", job["id"])
"""

PROMPT_SAFETY_OLD = """        return False, blocked_doc, "", str(block_exc)
"""

PROMPT_SAFETY_NEW = """        return False, blocked_doc, "", _CronOperatorFailure(str(block_exc), "safety")
"""

CREDENTIAL_SAFETY_OLD = """        _guard_job_credential_exfil(job)
"""

CREDENTIAL_SAFETY_NEW = """        try:
            _guard_job_credential_exfil(job)
        except Exception as exc:
            raise _cron_operator_failure_exception("safety", RuntimeError, str(exc)) from exc
"""

MODEL_CONFIG_OLD = """        if not (isinstance(model, str) and model.strip()):
            raise RuntimeError(
                f"Cron job '{job_name}' has no model configured "
"""

MODEL_CONFIG_NEW = """        if not (isinstance(model, str) and model.strip()):
            raise _cron_operator_failure_exception(
                "configuration",
                RuntimeError,
                f"Cron job '{job_name}' has no model configured "
"""

CWD_TIMEOUT_OLD = """            raise TimeoutError(
                f"Timed out waiting for the TERMINAL_CWD "
"""

CWD_TIMEOUT_NEW = """            raise _cron_operator_failure_exception(
                "runtime",
                TimeoutError,
                f"Timed out waiting for the TERMINAL_CWD "
"""

INTERRUPTED_OLD = """                success = False
                error = (
                    "Interrupted by gateway shutdown before the run finished "
"""

INTERRUPTED_NEW = """                success = False
                failure_kind = "interrupted"
                error = (
                    "Interrupted by gateway shutdown before the run finished "
"""

PROVIDER_AUTH_OLD = """            if runtime is None:
                raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
"""

PROVIDER_AUTH_NEW = """            if runtime is None:
                raise _cron_operator_failure_exception(
                    "provider_auth", RuntimeError, format_runtime_provider_error(auth_exc)
                ) from auth_exc
"""

PROVIDER_AUTH_LATEST_OLD = """            if runtime is None:
                raise RuntimeError(format_runtime_provider_error(resolve_exc)) from resolve_exc
"""

PROVIDER_AUTH_LATEST_NEW = """            if runtime is None:
                raise _cron_operator_failure_exception(
                    "provider_auth", RuntimeError, format_runtime_provider_error(resolve_exc)
                ) from resolve_exc
"""

PROVIDER_CONFIG_OLD = """        except Exception as exc:
            message = format_runtime_provider_error(exc)
            raise RuntimeError(message) from exc
"""

PROVIDER_CONFIG_NEW = """        except Exception as exc:
            message = format_runtime_provider_error(exc)
            if isinstance(exc, ValueError):
                raise _cron_operator_failure_exception(
                    "configuration", RuntimeError, message
                ) from exc
            raise RuntimeError(message) from exc
"""

PROVIDER_CONFIG_LATEST_OLD = """            if not (is_auth or is_transient_net):
                raise RuntimeError(format_runtime_provider_error(resolve_exc)) from resolve_exc
"""

PROVIDER_CONFIG_LATEST_NEW = """            if not (is_auth or is_transient_net):
                raise _cron_operator_failure_exception(
                    "configuration" if isinstance(resolve_exc, ValueError) else "runtime",
                    RuntimeError,
                    format_runtime_provider_error(resolve_exc),
                ) from resolve_exc
"""

UNKNOWN_TOOLSET_OLD = """            if _unknown_toolsets:
                raise RuntimeError(
                    "Cron job requests unknown toolset(s): "
                    + ", ".join(_unknown_toolsets)
                )
"""

UNKNOWN_TOOLSET_NEW = """            if _unknown_toolsets:
                raise _cron_operator_failure_exception(
                    "configuration",
                    RuntimeError,
                    "Cron job requests unknown toolset(s): "
                    + ", ".join(_unknown_toolsets),
                )
"""

MODEL_DRIFT_OLD = """                raise RuntimeError(
                    f"Skipped to prevent unintended spend: global inference config "
"""

MODEL_DRIFT_NEW = """                raise _cron_operator_failure_exception(
                    "configuration",
                    RuntimeError,
                    f"Skipped to prevent unintended spend: global inference config "
"""

MODEL_DRIFT_LATEST_OLD = """                raise RuntimeError(
                    f"{_drift_marker} Skipped to prevent unintended spend: global "
"""

MODEL_DRIFT_LATEST_NEW = """                raise _cron_operator_failure_exception(
                    "configuration",
                    RuntimeError,
                    f"{_drift_marker} Skipped to prevent unintended spend: global "
"""

INACTIVITY_TIMEOUT_OLD = """            raise TimeoutError(
                f"Cron job '{job_name}' idle for "
"""

INACTIVITY_TIMEOUT_NEW = """            raise _cron_operator_failure_exception(
                "timeout",
                TimeoutError,
                f"Cron job '{job_name}' idle for "
"""

RESULT_FAILURE_OLD = """        if result.get("failed") is True or (result.get("completed") is False and not max_iteration_summary):
            _err_text = (
                result.get("error")
                or final_response_text
                or "agent reported failure"
            )
            raise RuntimeError(_err_text)
"""

RESULT_FAILURE_NEW = """        _result_turn_failure_kind = {
            "empty_response_exhausted": "execution",
            "guardrail_halt": "safety",
            "ollama_runtime_context_too_small": "configuration",
            "partial_stream_recovery": "execution",
            "session_persistence_failed": "runtime",
        }.get(turn_exit_reason) or (
            "runtime" if turn_exit_reason.startswith("local_processing_error(") else
            "execution" if turn_exit_reason.startswith("error_near_max_iterations(") else
            None
        )
        if (
            result.get("failed") is True
            or (result.get("completed") is False and not max_iteration_summary)
            or _result_turn_failure_kind
        ):
            _err_text = (
                result.get("error")
                or final_response_text
                or "agent reported failure"
            )
            _result_failure_kind = {
                "auth": "provider_auth",
                "auth_permanent": "provider_auth",
                "rate_limit": "provider_limit",
                "upstream_rate_limit": "provider_limit",
                "billing": "provider_limit",
                "timeout": "timeout",
                "local_resource_exhaustion": "runtime",
                "content_policy_blocked": "safety",
                "ssl_cert_verification": "runtime",
                "model_not_found": "configuration",
                "provider_policy_blocked": "safety",
                "format_error": "configuration",
                "invalid_encrypted_content": "configuration",
                "multimodal_tool_content_unsupported": "configuration",
                "thinking_signature": "configuration",
                "oauth_long_context_beta_forbidden": "configuration",
                "llama_cpp_grammar_pattern": "configuration",
                "context_overflow": "configuration",
                "payload_too_large": "configuration",
                "image_too_large": "configuration",
                "long_context_tier": "configuration",
            }.get(str(result.get("failure_reason") or "")) or (
                "interrupted" if result.get("interrupted") is True else
                "configuration" if result.get("compression_exhausted") is True else
                _result_turn_failure_kind
            )
            if _result_failure_kind:
                raise _cron_operator_failure_exception(
                    _result_failure_kind, RuntimeError, _err_text
                )
            raise RuntimeError(_err_text)
"""

MEDIA_ADAPTER_SIGNATURE_OLD = """def _send_media_via_adapter(
    adapter,
    chat_id: str,
    media_files: list,
    metadata: dict | None,
    loop,
    job: dict,
    platform=None,
) -> None:
"""

MEDIA_ADAPTER_SIGNATURE_NEW = """def _send_media_via_adapter(
    adapter,
    chat_id: str,
    media_files: list,
    metadata: dict | None,
    loop,
    job: dict,
    platform=None,
    force_document: bool = False,
) -> None:
"""

MEDIA_ADAPTER_SIGNATURE_LATEST_OLD = """def _send_media_via_adapter(
    adapter,
    chat_id: str,
    media_files: list,
    metadata: dict | None,
    loop,
    job: dict,
    platform=None,
) -> list:
"""

MEDIA_ADAPTER_SIGNATURE_LATEST_NEW = """def _send_media_via_adapter(
    adapter,
    chat_id: str,
    media_files: list,
    metadata: dict | None,
    loop,
    job: dict,
    platform=None,
    force_document: bool = False,
) -> list:
"""

MEDIA_IMAGE_ROUTE_OLD = """            elif ext in _IMAGE_EXTS:
                coro = adapter.send_image_file(chat_id=chat_id, image_path=media_path, metadata=metadata)
"""

MEDIA_IMAGE_ROUTE_NEW = """            elif ext in _IMAGE_EXTS and not force_document:
                coro = adapter.send_image_file(chat_id=chat_id, image_path=media_path, metadata=metadata)
"""

MEDIA_EXTRACT_OLD = """    # Extract MEDIA: tags so attachments are forwarded as files, not raw text
    from gateway.platforms.base import BasePlatformAdapter
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
"""

MEDIA_EXTRACT_NEW = """    # Extract MEDIA: tags so attachments are forwarded as files, not raw text
    from gateway.platforms.base import BasePlatformAdapter
    force_document = "[[as_document]]" in delivery_content
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
"""

MEDIA_EXTRACT_LATEST_OLD = """    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
    requested_media = [(str(p), v) for p, v in media_files]
"""

MEDIA_EXTRACT_LATEST_NEW = """    force_document = "[[as_document]]" in delivery_content
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
    requested_media = [(str(p), v) for p, v in media_files]
"""

MEDIA_ADAPTER_CALL_OLD = """                    _send_media_via_adapter(
                        runtime_adapter,
                        chat_id,
                        media_files,
                        routed_media_metadata or None,
                        loop,
                        job,
                        platform=platform,
                    )
"""

MEDIA_ADAPTER_CALL_NEW = """                    _send_media_via_adapter(
                        runtime_adapter,
                        chat_id,
                        media_files,
                        routed_media_metadata or None,
                        loop,
                        job,
                        platform=platform,
                        force_document=(
                            force_document and platform == Platform.TELEGRAM
                        ),
                    )
"""

MEDIA_ADAPTER_CALL_LATEST_OLD = """                    _media_errors = _send_media_via_adapter(
                        runtime_adapter,
                        chat_id,
                        media_files,
                        routed_media_metadata or None,
                        loop,
                        job,
                        platform=platform,
                    )
"""

MEDIA_ADAPTER_CALL_LATEST_NEW = """                    _media_errors = _send_media_via_adapter(
                        runtime_adapter,
                        chat_id,
                        media_files,
                        routed_media_metadata or None,
                        loop,
                        job,
                        platform=platform,
                        force_document=(
                            force_document and platform == Platform.TELEGRAM
                        ),
                    )
"""

MEDIA_STANDALONE_CALL_OLD = """            coro = _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files)
"""

MEDIA_STANDALONE_CALL_NEW = """            coro = _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files, force_document=(force_document and platform == Platform.TELEGRAM))
"""

MEDIA_STANDALONE_FALLBACK_OLD = """                        future = pool.submit(asyncio.run, _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files))
"""

MEDIA_STANDALONE_FALLBACK_NEW = """                        future = pool.submit(asyncio.run, _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files, force_document=(force_document and platform == Platform.TELEGRAM)))
"""

CONTENT_POLICY_RESULT_OLD = """        "failed": True,
        "error": f"content_policy_blocked: {error_detail}",
"""

CONTENT_POLICY_RESULT_NEW = """        "failed": True,
        "error": f"content_policy_blocked: {error_detail}",
        "failure_reason": "content_policy_blocked",
"""

NONRETRYABLE_RESULT_OLD = """                    return {
                        "final_response": _nonretryable_summary,
                        "messages": messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "failed": True,
                        "error": _nonretryable_summary,
                    }
"""

NONRETRYABLE_RESULT_NEW = """                    return {
                        "final_response": _nonretryable_summary,
                        "messages": messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "failed": True,
                        "error": _nonretryable_summary,
                        "failure_reason": classified.reason.value,
                    }
"""

NOUS_RATE_LIMIT_RESULT_OLD = """                            "failed": True,
                            "error": _nous_msg,
"""

NOUS_RATE_LIMIT_RESULT_NEW = """                            "failed": True,
                            "error": _nous_msg,
                            "failure_reason": "rate_limit",
"""

COMPACTION_DISABLED_RESULT_OLD = """                        "failed": True,
                        "compaction_disabled": True,
"""

COMPACTION_DISABLED_RESULT_NEW = """                        "failed": True,
                        "compaction_disabled": True,
                        "failure_reason": classified.reason.value,
"""

UNPARSEABLE_OUTPUT_CAP_RESULT_OLD = """                            "failed": True,
                        }

                    # Error is about the INPUT being too large.  Only reduce
"""

UNPARSEABLE_OUTPUT_CAP_RESULT_NEW = """                            "failed": True,
                            "failure_reason": classified.reason.value,
                        }

                    # Error is about the INPUT being too large.  Only reduce
"""

THINKING_EXHAUSTED_RESULT_OLD = """                            "partial": True,
                            "error": _exhaust_error,
                        }
"""

THINKING_EXHAUSTED_RESULT_NEW = """                            "partial": True,
                            "error": _exhaust_error,
                            "failure_reason": "format_error",
                        }
"""

SCRATCHPAD_EXHAUSTED_RESULT_OLD = """                    return {
                        "final_response": "Incomplete REASONING_SCRATCHPAD after 2 retries",
                        "messages": rolled_back_messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": "Incomplete REASONING_SCRATCHPAD after 2 retries"
                    }
"""

SCRATCHPAD_EXHAUSTED_RESULT_NEW = """                    return {
                        "final_response": "Incomplete REASONING_SCRATCHPAD after 2 retries",
                        "messages": rolled_back_messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": "Incomplete REASONING_SCRATCHPAD after 2 retries",
                        "failure_reason": "format_error",
                    }
"""

CONTINUATION_EXHAUSTED_RESULT_OLD = """                                "completed": False,
                                "partial": True,
                                "error": "Response remained truncated after 4 continuation attempts",
                            }
"""

CONTINUATION_EXHAUSTED_RESULT_NEW = """                                "completed": False,
                                "partial": True,
                                "error": "Response remained truncated after 4 continuation attempts",
                                "failure_reason": "format_error",
                            }
"""

INVALID_JSON_TRUNCATION_RESULT_OLD = """                            "completed": False,
                            "partial": True,
                            "error": _final_response,
                        }
"""

INVALID_JSON_TRUNCATION_RESULT_NEW = """                            "completed": False,
                            "partial": True,
                            "error": _final_response,
                            "failure_reason": "format_error",
                        }
"""

INVALID_TOOL_RESULT_OLD = """                        return {
                            "final_response": _final_response,
                            "messages": messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": _final_response
                        }
"""

INVALID_TOOL_RESULT_NEW = """                        return {
                            "final_response": _final_response,
                            "messages": messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "failure_reason": "format_error",
                            "error": _final_response,
                        }
"""

ROLLBACK_TRUNCATION_RESULT_OLD = """                            "messages": rolled_back_messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": "Response truncated due to output length limit"
                        }
"""

ROLLBACK_TRUNCATION_RESULT_NEW = """                            "messages": rolled_back_messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": "Response truncated due to output length limit",
                            "failure_reason": "format_error",
                        }
"""

FIRST_TRUNCATION_RESULT_OLD = """                            "messages": messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "failed": True,
                            "error": "First response truncated due to output length limit"
                        }
"""

FIRST_TRUNCATION_RESULT_NEW = """                            "messages": messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "failed": True,
                            "error": "First response truncated due to output length limit",
                            "failure_reason": "format_error",
                        }
"""

CODEX_INCOMPLETE_RESULT_OLD = """                return {
                    "final_response": "Codex response remained incomplete after 3 continuation attempts",
                    "messages": messages,
                    "api_calls": api_call_count,
                    "completed": False,
                    "partial": True,
                    "error": "Codex response remained incomplete after 3 continuation attempts",
                }
"""

CODEX_INCOMPLETE_RESULT_NEW = """                return {
                    "final_response": "Codex response remained incomplete after 3 continuation attempts",
                    "messages": messages,
                    "api_calls": api_call_count,
                    "completed": False,
                    "partial": True,
                    "error": "Codex response remained incomplete after 3 continuation attempts",
                    "failure_reason": "format_error",
                }
"""

RUN_ONE_KIND_OLD = """        delivery_error = None
        blocked_config = False
"""

RUN_ONE_KIND_NEW = """        failure_kind = "script" if job.get("no_agent") else "execution"
        if isinstance(error, _CronOperatorFailure):
            failure_kind = error.kind
        delivery_error = None
        blocked_config = False
"""

RUN_ONE_KIND_LATEST_OLD = """        blocked_config = False
        side_effect_ownership_lost = False
"""

RUN_ONE_KIND_LATEST_NEW = """        failure_kind = "script" if job.get("no_agent") else "execution"
        if isinstance(error, _CronOperatorFailure):
            failure_kind = error.kind
        blocked_config = False
        side_effect_ownership_lost = False
"""

BLOCKED_CONFIG_OLD = """            blocked_config_silent = (
                bool(error) and BLOCKED_CONFIG_SILENT_MARKER in str(error)
            )
            blocked_config = blocked_config_silent or (
                bool(error) and BLOCKED_CONFIG_MARKER in str(error)
            )
"""

BLOCKED_CONFIG_NEW = """            blocked_config_silent = (
                failure_kind == "blocked_config"
                and bool(error)
                and BLOCKED_CONFIG_SILENT_MARKER in str(error)
            )
            blocked_config = failure_kind == "blocked_config"
"""

MISSING_SCRIPT_OLD = """            return False, "", "", err
"""

MISSING_SCRIPT_NEW = """            return False, "", "", _CronOperatorFailure(err, "configuration")
"""

MISSING_SCRIPT_LATEST_OLD = """    return False, doc, alert, reason
"""

MISSING_SCRIPT_LATEST_NEW = """    return False, doc, alert, _CronOperatorFailure(reason, "configuration")
"""

MONITOR_FAILURE_OLD = """            return False, _mon_doc, _mon_alert, _mon.error
"""

MONITOR_FAILURE_NEW = """            return False, _mon_doc, _mon_alert, _CronOperatorFailure(_mon.error, "script")
"""

BLOCKED_RETURN_OLD = """            return False, blocked_doc, "", f"{marker} {_pf_reason}"
"""

BLOCKED_RETURN_NEW = """            return False, blocked_doc, "", _CronOperatorFailure(
                f"{marker} {_pf_reason}", "blocked_config"
            )
"""

ERROR_RESULT_OLD = """    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
"""

ERROR_RESULT_NEW = """    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        error_kind = getattr(e, "_cron_operator_kind", None)
        if error_kind:
            error_msg = _CronOperatorFailure(error_msg, error_kind)
"""


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"cron operator delivery {label} anchor drift: {count}")
    return source.replace(old, new, 1)


def _patch_optional_outer_exception_delivery(source: str) -> str:
    if (
        OUTER_EXCEPTION_DELIVERY_NEW in source
        or OUTER_EXCEPTION_DELIVERY_LATEST_NEW in source
    ):
        return source
    candidates = (
        OUTER_EXCEPTION_DELIVERY_OLD,
        OUTER_EXCEPTION_DELIVERY_LATEST_OLD,
    )
    replacements = (
        OUTER_EXCEPTION_DELIVERY_NEW,
        OUTER_EXCEPTION_DELIVERY_LATEST_NEW,
    )
    counts = tuple(source.count(candidate) for candidate in candidates)
    if sum(counts) > 1:
        raise RuntimeError(
            f"cron operator delivery outer exception anchor drift: {sum(counts)}"
        )
    if sum(counts) == 1:
        index = counts.index(1)
        return source.replace(candidates[index], replacements[index], 1)
    return source


def _has_native_scoped_cron_cwd(source: str) -> bool:
    """Hermes 0.21 removed the process-global TERMINAL_CWD lock entirely."""
    return all(
        seam in source
        for seam in (
            "record_session_cwd as _record_tool_session_cwd",
            "_record_tool_session_cwd(_cron_task_id, _job_workdir)",
            "_clear_tool_session_cwd(_cron_task_id)",
        )
    )


JOB_ITERATION_BUDGET_OLD = """        # Max iterations
        max_iterations = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns") or 90
"""
JOB_ITERATION_BUDGET_NEW = """        # Max iterations
        max_iterations = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns") or 90
        # HERMES_CRON_JOB_ITERATION_BUDGET_v1
        job_max_iterations = job.get("max_iterations")
        if isinstance(job_max_iterations, int) and 1 <= job_max_iterations <= 90:
            max_iterations = min(max_iterations, job_max_iterations)
"""
JOB_ITERATION_BUDGET_LATEST_OLD = """        max_iterations = _resolve_turn_limit(_mt)
"""
JOB_ITERATION_BUDGET_LATEST_NEW = """        max_iterations = _resolve_turn_limit(_mt)
        # HERMES_CRON_JOB_ITERATION_BUDGET_v1
        job_max_iterations = job.get("max_iterations")
        if isinstance(job_max_iterations, int) and 1 <= job_max_iterations <= 90:
            max_iterations = min(max_iterations, job_max_iterations)
"""


def _patch_optional_job_iteration_budget(source: str) -> str:
    if JOB_ITERATION_BUDGET_MARKER in source:
        return source
    candidates = (JOB_ITERATION_BUDGET_OLD, JOB_ITERATION_BUDGET_LATEST_OLD)
    replacements = (JOB_ITERATION_BUDGET_NEW, JOB_ITERATION_BUDGET_LATEST_NEW)
    counts = tuple(source.count(candidate) for candidate in candidates)
    if sum(counts) == 0:
        return source
    if sum(counts) != 1:
        raise RuntimeError(f"cron job iteration budget anchor drift: {sum(counts)}")
    index = counts.index(1)
    return source.replace(candidates[index], replacements[index], 1)


_REPAIR_BOUNDARY_UPGRADES = [('            "enabled_toolsets": None,\n', '            # Preserve the original job tool allowlist; native config still applies.\n'), ('    return formatted, recovered\n', '    if recovered:\n        return f"{name} cron — automatic repair attempted; the original run remains failed.", False\n    return formatted, False\n'), ('    if not media_files:\n        return formatted\n', '    if not success or not media_files:\n        return formatted\n'), ('                if remediation_recovered:\n                    success = True\n                    final_response = remediation_delivery\n                    error = None\n', '                # A repair-model report cannot change the original workload outcome.\n')]


_REPAIR_BOUNDARY_UPGRADES.append(('            "provider": None,\n            "model": None,\n            "base_url": None,\n            "provider_snapshot": None,\n            "model_snapshot": None,\n', '            # Keep job-bound provider, endpoint, model and snapshot constraints.\n'))

_REPAIR_BOUNDARY_UPGRADES.extend([('        "failure_kind": failure_kind,\n        "failure_detail": str(failure_detail or "")[:1000],\n', '        "failure_kind": failure_kind,\n        # Volatile diagnostic text must not bypass the repair cooldown.\n'), ('def _attempt_cron_failure_remediation(\n    job: dict,\n    *,\n    failure_kind: str,\n    output_file: Path,\n    deferred_agents: list,\n    failure_detail: object = None,\n) -> tuple[str, bool]:\n    if failure_kind == "blocked_config":\n', 'def _attempt_cron_failure_remediation(\n    job: dict,\n    *,\n    failure_kind: str,\n    output_file: Path,\n    deferred_agents: list,\n    failure_detail: object = None,\n) -> tuple[str, bool]:\n    if job.get("no_agent"):\n        name = _cron_operator_job_name(job.get("name") or job.get("id"))\n        return f"{name} cron — failed in its script or runtime. Automatic repair skipped: script-only execution. Review the saved receipt.", False\n    if failure_kind == "blocked_config":\n')])

def _upgrade_repair_boundaries(source: str) -> str:
    for before, after in _REPAIR_BOUNDARY_UPGRADES:
        if before in source:
            source = _replace_once(source, before, after, "repair authorization/outcome boundary")
        elif after not in source:
            raise RuntimeError("cron repair authorization/outcome boundary drift")
    return source


def patch_source(source: str) -> str:
    if MARKER in source:
        baseline_required = (
            "def _format_cron_operator_delivery(",
            "failure_kind=failure_kind,",
            "if wrap_response and not _is_cron_operator_delivery(content):",
            "class _CronOperatorFailure(str):",
            "_cron_operator_failure_exception(",
            'failure_kind = "interrupted"',
            "_cron_operator_has_unicode_control",
            "MEDIA_DELIVERY_EXTS",
            'force_document = "[[as_document]]" in delivery_content',
            '"rate_limit": "provider_limit"',
            '"ssl_cert_verification": "runtime"',
            '"format_error": "configuration"',
            '"context_overflow": "configuration"',
            '"long_context_tier": "configuration"',
            'result.get("interrupted") is True',
            'result.get("compression_exhausted") is True',
        )
        baseline_missing = [item for item in baseline_required if item not in source]
        if baseline_missing:
            raise RuntimeError("cron operator delivery marker is incomplete: " + ", ".join(baseline_missing))
        source = _upgrade_long_success_delivery(source)
        if CRON_HINT_PREVIOUS in source:
            source = _replace_once(source, CRON_HINT_PREVIOUS, CRON_HINT_NEW, "plain-report prompt")
        source = _patch_optional_job_iteration_budget(source)
        if NO_EMPTY_SUCCESS_MARKER not in source:
            source = _replace_once(
                source,
                EMPTY_SUCCESS_FALLBACK_OLD,
                EMPTY_SUCCESS_FALLBACK_NEW,
                "empty-success fallback",
            )
            source = _replace_once(
                source,
                EMPTY_SUCCESS_RETURN_OLD,
                EMPTY_SUCCESS_RETURN_NEW,
                "empty-success return",
            )
            source = _replace_once(
                source,
                EMPTY_MEDIA_FALLBACK_OLD,
                EMPTY_MEDIA_FALLBACK_NEW,
                "empty media fallback",
            )
            source = _replace_once(
                source,
                EMPTY_MEDIA_RETURN_OLD,
                EMPTY_MEDIA_RETURN_NEW,
                "empty media return",
            )
            source = _replace_once(
                source,
                MEDIA_ONLY_DELIVERY_OLD,
                MEDIA_ONLY_DELIVERY_NEW,
                "media-only delivery wrapper",
            )
        if SELF_REMEDIATION_MARKER not in source:
            source = _replace_once(
                source,
                SELF_REMEDIATION_FAILURE_OLD,
                SELF_REMEDIATION_FAILURE_NEW,
                "self-remediation helpers",
            )
            source = _replace_once(
                source,
                DELIVERY_BOUNDARY_V1,
                DELIVERY_BOUNDARY_NEW,
                "self-remediation delivery boundary",
            )
        source = _patch_optional_outer_exception_delivery(source)
        required = (
            *baseline_required,
            NO_EMPTY_SUCCESS_MARKER,
            SELF_REMEDIATION_MARKER,
            "def _attempt_cron_failure_remediation(",
            '"_cron_repair_attempt": True',
            "remediation_recovered = False",
        )
        missing = [item for item in required if item not in source]
        if missing:
            raise RuntimeError("cron operator delivery marker is incomplete: " + ", ".join(missing))
        return _upgrade_repair_boundaries(source)

    anchors = (
        ("helper", HELPER_ANCHOR, HELPER_SOURCE + HELPER_ANCHOR),
        ("cron prompt", CRON_HINT_OLD, CRON_HINT_NEW),
        (
            "delivery boundary",
            (
                DELIVERY_BOUNDARY_OLD,
                DELIVERY_BOUNDARY_LATEST_OLD,
                DELIVERY_BOUNDARY_STREAK_OLD,
                DELIVERY_BOUNDARY_INCIDENT_OLD,
            ),
            (
                DELIVERY_BOUNDARY_NEW,
                DELIVERY_BOUNDARY_NEW,
                DELIVERY_BOUNDARY_NEW,
                DELIVERY_BOUNDARY_INCIDENT_NEW,
            ),
        ),
        ("legacy response wrapper", WRAP_RESPONSE_OLD, WRAP_RESPONSE_NEW),
        ("prompt safety kind", PROMPT_SAFETY_OLD, PROMPT_SAFETY_NEW),
        ("credential safety kind", CREDENTIAL_SAFETY_OLD, CREDENTIAL_SAFETY_NEW),
        ("model configuration kind", MODEL_CONFIG_OLD, MODEL_CONFIG_NEW),
        ("runtime failure kind", CWD_TIMEOUT_OLD, CWD_TIMEOUT_NEW),
        ("interruption kind", INTERRUPTED_OLD, INTERRUPTED_NEW),
        (
            "provider authentication kind",
            (PROVIDER_AUTH_OLD, PROVIDER_AUTH_LATEST_OLD),
            (PROVIDER_AUTH_NEW, PROVIDER_AUTH_LATEST_NEW),
        ),
        (
            "provider configuration kind",
            (PROVIDER_CONFIG_OLD, PROVIDER_CONFIG_LATEST_OLD),
            (PROVIDER_CONFIG_NEW, PROVIDER_CONFIG_LATEST_NEW),
        ),
        ("unknown toolset kind", UNKNOWN_TOOLSET_OLD, UNKNOWN_TOOLSET_NEW),
        (
            "model drift kind",
            (MODEL_DRIFT_OLD, MODEL_DRIFT_LATEST_OLD),
            (MODEL_DRIFT_NEW, MODEL_DRIFT_LATEST_NEW),
        ),
        ("provider inactivity kind", INACTIVITY_TIMEOUT_OLD, INACTIVITY_TIMEOUT_NEW),
        ("structured provider failure kind", RESULT_FAILURE_OLD, RESULT_FAILURE_NEW),
        (
            "run one failure kind",
            (RUN_ONE_KIND_OLD, RUN_ONE_KIND_LATEST_OLD),
            (RUN_ONE_KIND_NEW, RUN_ONE_KIND_LATEST_NEW),
        ),
        ("blocked configuration kind", BLOCKED_CONFIG_OLD, BLOCKED_CONFIG_NEW),
        (
            "missing script kind",
            (MISSING_SCRIPT_OLD, MISSING_SCRIPT_LATEST_OLD),
            (MISSING_SCRIPT_NEW, MISSING_SCRIPT_LATEST_NEW),
        ),
        ("monitor failure kind", MONITOR_FAILURE_OLD, MONITOR_FAILURE_NEW),
        ("blocked return kind", BLOCKED_RETURN_OLD, BLOCKED_RETURN_NEW),
        ("exception failure kind", ERROR_RESULT_OLD, ERROR_RESULT_NEW),
        (
            "media adapter signature",
            (MEDIA_ADAPTER_SIGNATURE_OLD, MEDIA_ADAPTER_SIGNATURE_LATEST_OLD),
            (MEDIA_ADAPTER_SIGNATURE_NEW, MEDIA_ADAPTER_SIGNATURE_LATEST_NEW),
        ),
        ("media image route", MEDIA_IMAGE_ROUTE_OLD, MEDIA_IMAGE_ROUTE_NEW),
        (
            "media extraction",
            (MEDIA_EXTRACT_OLD, MEDIA_EXTRACT_LATEST_OLD),
            (MEDIA_EXTRACT_NEW, MEDIA_EXTRACT_LATEST_NEW),
        ),
        (
            "media adapter call",
            (MEDIA_ADAPTER_CALL_OLD, MEDIA_ADAPTER_CALL_LATEST_OLD),
            (MEDIA_ADAPTER_CALL_NEW, MEDIA_ADAPTER_CALL_LATEST_NEW),
        ),
        ("standalone media call", MEDIA_STANDALONE_CALL_OLD, MEDIA_STANDALONE_CALL_NEW),
        (
            "standalone media fallback",
            MEDIA_STANDALONE_FALLBACK_OLD,
            MEDIA_STANDALONE_FALLBACK_NEW,
        ),
    )
    patched = source
    native_unknown_toolset_marker = "HERMES_CRON_UNKNOWN_TOOLSET_VALIDATION_v1"
    if (
        UNKNOWN_TOOLSET_OLD not in patched
        and native_unknown_toolset_marker not in patched
        and "def _resolve_cron_enabled_toolsets(" in patched
    ):
        checked_resolver = f'''\n\ndef _resolve_cron_enabled_toolsets_checked(job: dict, cfg: dict) -> list[str] | None:
    """Fail closed before inference when a stored cron toolset is unknown."""
    # {native_unknown_toolset_marker}
    # HERMES_CRON_TOOLSET_VALIDATION_v1
    resolved = _resolve_cron_enabled_toolsets(job, cfg)
    if resolved is None:
        return None
    from agent.outcome_stop import unknown_requested_toolsets
    from hermes_cli.tools_config import enabled_mcp_server_names
    from tools.registry import registry as _tool_registry

    # HERMES_CRON_CONFIGURED_MCP_TOOLSET_VALIDATION_v2: a configured cold MCP
    # is valid before its on-demand activation registers live definitions.
    unknown = unknown_requested_toolsets(
        resolved,
        _tool_registry.get_registered_toolset_names(),
        _tool_registry.get_registered_toolset_aliases(),
        enabled_mcp_server_names(cfg),
    )
    if unknown:
        raise _cron_operator_failure_exception(
            "configuration",
            RuntimeError,
            "Cron job requests unknown toolset(s): " + ", ".join(unknown),
        )
    return resolved
'''
        patched = _replace_once(
            patched,
            "\n\ndef _resolve_job_reasoning_config(",
            checked_resolver + "\n\ndef _resolve_job_reasoning_config(",
            "native unknown toolset validator",
        )
        patched = _replace_once(
            patched,
            "enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),",
            "enabled_toolsets=_resolve_cron_enabled_toolsets_checked(job, _cfg),",
            "native unknown toolset validator call",
        )
    for label, anchor, replacement in anchors:
        candidates = anchor if isinstance(anchor, tuple) else (anchor,)
        replacements = replacement if isinstance(replacement, tuple) else None
        counts = tuple(patched.count(candidate) for candidate in candidates)
        if (
            label == "runtime failure kind"
            and sum(counts) == 0
            and _has_native_scoped_cron_cwd(patched)
        ):
            # The failure path no longer exists: each cron run owns a scoped
            # cwd record and never waits on the old process-global lock.
            continue
        if (
            label == "unknown toolset kind"
            and sum(counts) == 0
            and native_unknown_toolset_marker in patched
        ):
            continue
        if sum(counts) != 1:
            raise RuntimeError(f"cron operator delivery {label} anchor drift: {sum(counts)}")
        selected_index = counts.index(1)
        selected = candidates[selected_index]
        selected_replacement = replacements[selected_index] if replacements else replacement
        patched = patched.replace(selected, selected_replacement, 1)
    patched = _patch_optional_outer_exception_delivery(patched)
    return _patch_optional_job_iteration_budget(patched)


def patch_content_policy_source(source: str) -> str:
    patched = source
    if CONTENT_POLICY_RESULT_NEW not in patched:
        count = patched.count(CONTENT_POLICY_RESULT_OLD)
        if count != 1:
            raise RuntimeError(f"cron operator delivery content policy result anchor drift: {count}")
        patched = patched.replace(
            CONTENT_POLICY_RESULT_OLD,
            CONTENT_POLICY_RESULT_NEW,
            1,
        )
    if NONRETRYABLE_RESULT_NEW not in patched:
        count = patched.count(NONRETRYABLE_RESULT_OLD)
        if count != 1:
            raise RuntimeError(f"cron operator delivery nonretryable result anchor drift: {count}")
        patched = patched.replace(
            NONRETRYABLE_RESULT_OLD,
            NONRETRYABLE_RESULT_NEW,
            1,
        )
    terminal_results = (
        (
            "Nous rate-limit result",
            NOUS_RATE_LIMIT_RESULT_OLD,
            NOUS_RATE_LIMIT_RESULT_NEW,
        ),
        (
            "compaction-disabled result",
            COMPACTION_DISABLED_RESULT_OLD,
            COMPACTION_DISABLED_RESULT_NEW,
        ),
        (
            "unparseable output-cap result",
            UNPARSEABLE_OUTPUT_CAP_RESULT_OLD,
            UNPARSEABLE_OUTPUT_CAP_RESULT_NEW,
        ),
        (
            "thinking exhaustion result",
            THINKING_EXHAUSTED_RESULT_OLD,
            THINKING_EXHAUSTED_RESULT_NEW,
        ),
        (
            "scratchpad exhaustion result",
            SCRATCHPAD_EXHAUSTED_RESULT_OLD,
            SCRATCHPAD_EXHAUSTED_RESULT_NEW,
        ),
        (
            "continuation exhaustion result",
            CONTINUATION_EXHAUSTED_RESULT_OLD,
            CONTINUATION_EXHAUSTED_RESULT_NEW,
        ),
        (
            "invalid JSON truncation result",
            INVALID_JSON_TRUNCATION_RESULT_OLD,
            INVALID_JSON_TRUNCATION_RESULT_NEW,
        ),
        (
            "invalid tool exhaustion result",
            INVALID_TOOL_RESULT_OLD,
            INVALID_TOOL_RESULT_NEW,
        ),
        (
            "rollback truncation result",
            ROLLBACK_TRUNCATION_RESULT_OLD,
            ROLLBACK_TRUNCATION_RESULT_NEW,
        ),
        (
            "first truncation result",
            FIRST_TRUNCATION_RESULT_OLD,
            FIRST_TRUNCATION_RESULT_NEW,
        ),
        (
            "Codex incomplete result",
            CODEX_INCOMPLETE_RESULT_OLD,
            CODEX_INCOMPLETE_RESULT_NEW,
        ),
    )
    for label, anchor, replacement in terminal_results:
        if replacement in patched:
            continue
        count = patched.count(anchor)
        if count != 1:
            raise RuntimeError(f"cron operator delivery {label} anchor drift: {count}")
        patched = patched.replace(anchor, replacement, 1)
    return patched


_NATIVE_D363_COMMIT = "d3630f853239e8c41ce7201e09fbdf39bcbc5431"
_NATIVE_D363_PAYLOAD = Path(__file__).resolve().parents[1] / "payloads" / "cron-operator-delivery-d363-v1"


def _is_native_d363(hermes_dir: Path) -> bool:
    import subprocess
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=hermes_dir, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == _NATIVE_D363_COMMIT


def _patch_native_d363(hermes_dir: Path, *, dry_run: bool = False) -> bool:
    """Install only the reviewed native residual on its exact pre/post images."""
    import hashlib
    import json
    import re
    import subprocess
    if not _is_native_d363(hermes_dir):
        raise RuntimeError("native carrier requires exact d363 HEAD")
    manifest = json.loads((_NATIVE_D363_PAYLOAD / "manifest.json").read_text())
    patch = (_NATIVE_D363_PAYLOAD / "native.patch").read_bytes()
    digest = lambda value: hashlib.sha256(value).hexdigest() if value is not None else None
    if manifest["upstream_commit"] != _NATIVE_D363_COMMIT or digest(patch) != manifest["patch_sha256"]:
        raise RuntimeError("native d363 payload integrity mismatch")
    # Reviewed unified hunks are the owned source seam. Unrelated prior Golden
    # changes may compose in the same file; every hunk must still be byte exact.
    hunks = {}
    name = None
    old, new = [], []
    active = False
    def flush():
        if active:
            hunks.setdefault(name, []).append(("".join(old), "".join(new)))
    for line in patch.decode().splitlines(keepends=True):
        if line.startswith("--- "):
            flush()
            active = False
        elif line.startswith("+++ b/"):
            name = line[6:].rstrip("\n")
        elif line.startswith("@@ "):
            flush()
            old, new = [], []
            active = True
        elif active:
            if line.startswith((" ", "-")):
                old.append(line[1:])
            if line.startswith((" ", "+")):
                new.append(line[1:])
    flush()
    if set(hunks) != set(manifest["files"]):
        raise RuntimeError("native d363 payload file inventory mismatch")
    originals = {}
    states = []
    upgrades = {}
    def source_states(name, original):
        expected = manifest["files"][name]
        if expected["pre"] == [None]:
            if digest(original) in expected["post"]:
                return ["post"]
            if original is None:
                return ["pre"]
            raise RuntimeError(f"native d363 source drift: {name}")
        if original is None:
            raise RuntimeError(f"native d363 source missing: {name}")
        content = original.decode()
        result = []
        def occurrences(fragment):
            return len(re.findall(r"(?m)^" + re.escape(fragment), content))
        for before, after in hunks[name]:
            if after and occurrences(after) == 1:
                result.append("post")
            elif before and occurrences(before) == 1:
                result.append("pre")
            else:
                raise RuntimeError(f"native d363 source drift at owned hunk: {name}")
        return result
    for name in manifest["files"]:
        path = hermes_dir / name
        if path.is_symlink():
            raise RuntimeError(f"native d363 source is a symlink: {name}")
        original = path.read_bytes() if path.exists() else None
        originals[name] = original
        if name == 'cron/scheduler_operator.py' and original is not None:
            before, after = (b'            "provider": None,\n            "model": None,\n            "base_url": None,\n            "provider_snapshot": None,\n            "model_snapshot": None,\n', b'            # Keep job-bound provider, endpoint, model and snapshot constraints.\n')
            if original.count(before) == 1 and after not in original:
                candidate = original.replace(before, after, 1)
                if all(state == "post" for state in source_states(name, candidate)):
                    upgrades[name] = candidate
                    original = candidate
        states.extend(source_states(name, original))
    if all(state == "post" for state in states):
        if upgrades and not dry_run:
            for name, content in upgrades.items():
                (hermes_dir / name).write_bytes(content)
        return bool(upgrades)
    if not all(state == "pre" for state in states):
        raise RuntimeError("native d363 partial installation refused")
    check = subprocess.run(["git", "apply", "--check", "-"], cwd=hermes_dir, input=patch, capture_output=True)
    if check.returncode:
        raise RuntimeError("native d363 patch preflight failed: " + check.stderr.decode())
    if dry_run:
        return True
    try:
        subprocess.run(["git", "apply", "-"], cwd=hermes_dir, input=patch, capture_output=True, check=True)
        for name in manifest["files"]:
            if any(state != "post" for state in source_states(name, (hermes_dir / name).read_bytes())):
                raise RuntimeError(f"native d363 postimage mismatch: {name}")
    except Exception:
        for name, original in originals.items():
            path = hermes_dir / name
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise
    return True

def patch_cron_operator_delivery_v1(hermes_dir: Path) -> bool:
    if _is_native_d363(hermes_dir):
        return _patch_native_d363(hermes_dir)
    target = Path(hermes_dir) / TARGET
    if not target.is_file():
        return False
    content_policy_target = Path(hermes_dir) / CONTENT_POLICY_TARGET
    source = target.read_text(encoding="utf-8")
    patched = patch_source(source)
    if not content_policy_target.is_file():
        raise RuntimeError(f"cron operator delivery required file missing: {content_policy_target}")
    content_policy_source = content_policy_target.read_text(encoding="utf-8")
    patched_content_policy = patch_content_policy_source(content_policy_source)
    if patched == source and patched_content_policy == content_policy_source:
        return False
    if patched != source:
        target.write_text(patched, encoding="utf-8")
    if patched_content_policy != content_policy_source:
        content_policy_target.write_text(patched_content_policy, encoding="utf-8")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    print("patched" if patch_cron_operator_delivery_v1(args.hermes_dir) else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
