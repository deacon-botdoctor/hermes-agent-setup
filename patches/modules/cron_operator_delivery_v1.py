"""Keep cron receipts detailed while making human delivery glanceable."""


from __future__ import annotations


from pathlib import Path


MARKER = "HERMES_CRON_OPERATOR_DELIVERY_v1"


TARGET = Path("cron/scheduler.py")


CURRENT_PROMPT_TARGET = Path("cron/scheduler_prompt.py")


WITHHELD_CONSTANT_OLD = '''_CRON_OPERATOR_WITHHELD = (
    "completed, but its result was withheld because it did not meet the delivery "
    "safety contract. Review the saved receipt."
)


'''


HELPER_SOURCE = r"""# __MARKER__
# HERMES_CRON_NO_EMPTY_SUCCESS_NOISE_v1
# HERMES_CRON_SELF_REMEDIATION_v1
_CRON_OPERATOR_FALLBACK = SILENT_MARKER
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
    label = re.sub(r"https?://[^\s<>]+", "", raw_name)
    name = " ".join(_cron_operator_delivery_candidate(label).split())
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
    # Delivery is not an answer-format validator. Only concrete credential
    # material belongs at this boundary; routing and permissions are enforced
    # independently by the delivery adapter and tool runtime.
    from urllib.parse import unquote, urlsplit

    for match in re.finditer(r"https?://[^\s<>()\[\]{}]+", text):
        try:
            parsed = urlsplit(match.group(0))
            if parsed.username is not None or parsed.password is not None:
                return True
        except ValueError:
            continue
        decoded = unquote("/".join((parsed.path, parsed.query, parsed.fragment)))
        if re.search(
            r"(?:^|[/?&])(?:token|access[_-]?token|api[_-]?key|password|secret|session|signature|sig)\s*=\s*[^&#/\s]+",
            decoded, re.I,
        ):
            return True
    return bool(re.search(
        r"\bauthorization\s*:\s*bearer\s+\S+|"
        r"\b(?:access[_-]?token|api[_-]?key|password|client[_-]?secret)\s*[:=]\s*\S+",
        text, re.I,
    ))


def _cron_operator_failure_message(failure_kind: str) -> str:
    if failure_kind == "blocked_config":
        failure_kind = "configuration"
    return {
        "delivery_contract": "result was retained privately because it failed delivery validation.",
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
        return f"{name} — automatic repair attempted; the original run remains failed.", False
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
    if output_file is None:
        # Self-removal has no durable receipt and must not start a repair turn.
        return "", False
    output_file = Path(output_file)
    if job.get("no_agent"):
        name = _cron_operator_job_name(job.get("name") or job.get("id"))
        return SILENT_MARKER, False
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
            f"{name} — automatic repair paused: the same configuration and "
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
# HERMES_CRON_USER_FACING_DELIVERY_v1
def _cron_operator_delivery_candidate(value: str) -> str:
    raw = str(value or "").strip()
    if _cron_operator_has_hard_detail(raw):
        return ""
    if re.search(r"failed in (?:its script|the local runtime)|review the saved receipt", raw, re.I):
        return ""
    # Match public URLs first so their slashes are not treated as local paths.
    # Credential-bearing URLs were rejected above.
    raw = re.sub(
        r"(?P<url>https?://[^\s<>()\[\]{}]+)|"
        r"(?P<quote>[\x22\x27])(?:[A-Za-z]:[\\/]|\\\\|~/|/).*?(?P=quote)|"
        r"(?<!\w)(?:[A-Za-z]:[\\/]|\\\\|~/|/)[^\s<>]+",
        lambda match: match.group("url") or "",
        raw,
    )
    return "\n".join(" ".join(line.split()).strip() for line in raw.splitlines()).strip()


def _format_cron_operator_delivery(
    job_name: str,
    output: str,
    *,
    success: bool,
    job_lane: str,
    failure_kind: str,
) -> str:
    name = _cron_operator_job_name(job_name)
    prefix = f"{name} — "
    if not success:
        if failure_kind in {"script", "runtime"}:
            return SILENT_MARKER
        return prefix + _cron_operator_failure_message(failure_kind)

    text = str(output or "").strip()
    if not text or text.upper() in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}:
        return SILENT_MARKER

    candidate = ""
    if job_lane == "script":
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and any(isinstance(payload.get(key), str) for key in ("message", "summary")):
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
        legacy_prefix = f"{name} cron — "
        if text.startswith(prefix):
            candidate = _cron_operator_delivery_candidate(text)
        else:
            value = text[len(legacy_prefix):] if text.startswith(legacy_prefix) else text
            candidate = _cron_operator_delivery_candidate(value)

    if candidate.startswith(prefix):
        return candidate
    if not candidate:
        return _CronOperatorFailure(SILENT_MARKER, "delivery_contract")
    return prefix + candidate


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
    if isinstance(formatted, _CronOperatorFailure):
        return formatted
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


def _cron_operator_success_error(job: dict, final_response: str):
    # Classify rejected output before incidents, routing and final run status.
    formatted = final_response
    if not isinstance(formatted, _CronOperatorFailure):
        formatted = _format_cron_operator_delivery_with_media(
            job.get("name") or job.get("id"), final_response,
            success=True, job_lane="script" if job.get("no_agent") else "model",
            failure_kind="execution",
        )
    if isinstance(formatted, _CronOperatorFailure):
        return _CronOperatorFailure(
            "Cron result rejected by delivery safety contract; original output retained in run receipt.",
            "delivery_contract",
        )
    return None


def _is_cron_operator_delivery(content: str) -> bool:
    first_line = str(content or "").splitlines()[0] if content else ""
    if first_line.startswith(("MEDIA:", "[[as_document]]", "[[audio_as_voice]]")):
        return True
    return bool(re.match(r"^[^\r\n]{1,100} (?:cron )?— ", first_line))


""".replace("__MARKER__", MARKER)


CURRENT_NO_AGENT_FAILURE_OLD = '''        # Deliver the error: a silently broken watchdog is the worst-case outcome.
        alert = (
            f"⚠ Cron watchdog '{job_name}' script failed\\n\\n"
            f"{output}\\n\\n"
            f"Time: {now_iso}"
        )
        return False, f"{header}**Status:** script failed\\n\\n{output}\\n", alert, output
'''


CURRENT_NO_AGENT_FAILURE_NEW = '''        # Keep the full failure in the durable run document, but never send raw
        # script output (paths, JSON, counters, or tracebacks) to a human lane.
        alert = _format_cron_operator_delivery_with_media(
            job_name, output, success=False, job_lane="script", failure_kind="script"
        )
        return False, f"{header}**Status:** script failed\\n\\n{output}\\n", alert, output
'''


CURRENT_NO_AGENT_SUCCESS_OLD = '''    return True, f"{header}\\n---\\n\\n{output}\\n", output, None
'''


CURRENT_NO_AGENT_SUCCESS_NEW = '''    return True, f"{header}\\n---\\n\\n{output}\\n", _format_cron_operator_delivery_with_media(
        job_name, output, success=True, job_lane="script", failure_kind="script"
    ), None
'''


CURRENT_DELIVERY_SUCCESS_OLD = '''    elif success:
        deliver_content = final_response
'''


CURRENT_DELIVERY_SUCCESS_NEW = '''    elif success:
        deliver_content = _format_cron_operator_delivery_with_media(
            job.get("name") or job.get("id"),
            final_response,
            success=True,
            job_lane="model",
            failure_kind="execution",
        )
'''


CURRENT_DELIVERY_FAILURE_OLD = '                _summarize_cron_failure_for_delivery(job, error) + _failure_streak_nudge(job)\n'


CURRENT_DELIVERY_FAILURE_NEW = '                _format_cron_operator_delivery_with_media(\n                    job.get("name") or job.get("id"), error, success=False,\n                    job_lane="script" if job.get("no_agent") else "model",\n                    failure_kind=getattr(error, "kind", "script" if job.get("no_agent") else "execution"),\n                ) + _failure_streak_nudge(job)\n'


CURRENT_DELIVERY_REMEDIATION_ANCHOR = '''    # Whitespace-only == empty: skip delivery; the guard below marks it a soft failure.
    d.should_deliver = bool(deliver_content.strip()) and not _silent_alert
'''


CURRENT_DELIVERY_REMEDIATION_LEGACY = '''    if not d.success and not blocked_config and not drift_skip:
        # A bounded repair turn is allowed only after the original receipt is
        # durable.  It never changes the original workload's failure state.
        repair_delivery, _recovered = _attempt_cron_failure_remediation(
            job,
            failure_kind="script" if job.get("no_agent") else "execution",
            output_file=output_file,
            deferred_agents=deferred_agents,
            failure_detail=error,
        )
        if repair_delivery:
            deliver_content = repair_delivery
    # Whitespace-only == empty: skip delivery; the guard below marks it a soft failure.
    d.should_deliver = bool(deliver_content.strip()) and not _silent_alert
'''


CURRENT_DELIVERY_REMEDIATION_RETIRED_DRIFT = CURRENT_DELIVERY_REMEDIATION_LEGACY.replace(
    "if not d.success and not blocked_config and not drift_skip:",
    "if not d.success and not d.blocked_config and not any(\n"
    "        marker in str(d.error or '')\n"
    "        for marker in (DRIFT_SKIP_MARKER, DRIFT_SKIP_SILENT_MARKER)\n"
    "    ):",
).replace("failure_detail=error,", "failure_detail=d.error,").replace(
    'failure_kind="script" if job.get("no_agent") else "execution",',
    'failure_kind=getattr(d.error, "kind", "script" if job.get("no_agent") else "execution"),',
)


CURRENT_DELIVERY_REMEDIATION_UNGUARDED = CURRENT_DELIVERY_REMEDIATION_RETIRED_DRIFT.replace(
    "if not d.success and not d.blocked_config and not any(\n"
    "        marker in str(d.error or '')\n"
    "        for marker in (DRIFT_SKIP_MARKER, DRIFT_SKIP_SILENT_MARKER)\n"
    "    ):",
    "if not d.success and not d.blocked_config:",
)


CURRENT_DELIVERY_REMEDIATION_NEW = CURRENT_DELIVERY_REMEDIATION_UNGUARDED.replace(
    "if not d.success and not d.blocked_config:",
    "if (not d.success and not d.blocked_config and not d.incident_acked\n"
    "            and not d.agent_declared\n"
    "            and output_file is not None\n"
    "            and not _is_interrupted(job[\"id\"], execution_token)):",
)


CURRENT_PRERUN_FAILURE_OLD = '''        _ran_ok, _script_output = prerun_script
        if _ran_ok and not _parse_wake_gate(_script_output):
'''


CURRENT_PRERUN_FAILURE_NEW = '''        _ran_ok, _script_output = prerun_script
        # A failed collection is not model input or a successful client report.
        # Preserve the error for the existing incident/repair and failure lane.
        if not _ran_ok:
            header = _job_doc_header(job_name, job_id,
                                     _hermes_now().strftime("%Y-%m-%d %H:%M:%S"),
                                     "pre-agent script")
            return (False, f"{header}**Status:** script failed\\n\\n{_script_output}\\n",
                    "", _script_output), None
        if _ran_ok and not _parse_wake_gate(_script_output):
'''


def _upgrade_sensitive_url_guard(source: str) -> str:
    # Existing marker-bearing runtimes also need the corrected URL guard.
    return source.replace(
        r"(?:access[_-]?token|api[_-]?key|auth(?:entication)?|",
        r"(?:token|access[_-]?token|api[_-]?key|auth(?:entication)?|",
    )


def _patch_current_split_scheduler(hermes_dir: Path) -> bool:
    """Patch Hermes' split cron implementation (scheduler + prompt owner)."""
    scheduler = hermes_dir / TARGET
    prompt = hermes_dir / CURRENT_PROMPT_TARGET
    if not scheduler.is_file() or not prompt.is_file():
        return False
    scheduler_source = _upgrade_sensitive_url_guard(scheduler.read_text(encoding="utf-8"))
    prompt_source = prompt.read_text(encoding="utf-8")
    scheduler_source = _upgrade_rejected_success(scheduler_source) if MARKER in scheduler_source else scheduler_source
    previous_failure = CURRENT_DELIVERY_FAILURE_NEW.replace(
        'getattr(error, "kind", "script" if job.get("no_agent") else "execution")',
        '"script" if job.get("no_agent") else "execution"',
    )
    scheduler_source = scheduler_source.replace(previous_failure, CURRENT_DELIVERY_FAILURE_NEW)
    scheduler_source = scheduler_source.replace(
        CURRENT_DELIVERY_REMEDIATION_RETIRED_DRIFT, CURRENT_DELIVERY_REMEDIATION_NEW)
    scheduler_source = scheduler_source.replace(
        CURRENT_DELIVERY_REMEDIATION_UNGUARDED, CURRENT_DELIVERY_REMEDIATION_NEW)
    previous_remediation = CURRENT_DELIVERY_REMEDIATION_RETIRED_DRIFT.replace(
        'getattr(d.error, "kind", "script" if job.get("no_agent") else "execution")',
        '"script" if job.get("no_agent") else "execution"',
    )
    scheduler_source = scheduler_source.replace(previous_remediation, CURRENT_DELIVERY_REMEDIATION_NEW)
    if CURRENT_PRERUN_FAILURE_NEW not in scheduler_source:
        scheduler_source = _replace_once(
            scheduler_source, CURRENT_PRERUN_FAILURE_OLD, CURRENT_PRERUN_FAILURE_NEW,
            "split pre-agent script failure boundary",
        )
    if MARKER not in scheduler_source:
        scheduler_source = _replace_once(
            scheduler_source,
            'SILENT_MARKER = "[SILENT]"\n',
            'SILENT_MARKER = "[SILENT]"\n\n' + HELPER_SOURCE,
            "split scheduler helper",
        )
    scheduler_source = _replace_once(
        scheduler_source,
        CURRENT_NO_AGENT_FAILURE_OLD,
        CURRENT_NO_AGENT_FAILURE_NEW,
        "split no-agent failure delivery",
    ) if CURRENT_NO_AGENT_FAILURE_NEW not in scheduler_source else scheduler_source
    scheduler_source = _replace_once(
        scheduler_source,
        CURRENT_NO_AGENT_SUCCESS_OLD,
        CURRENT_NO_AGENT_SUCCESS_NEW,
        "split no-agent success delivery",
    ) if CURRENT_NO_AGENT_SUCCESS_NEW not in scheduler_source else scheduler_source
    scheduler_source = _replace_once(
        scheduler_source,
        CURRENT_DELIVERY_SUCCESS_OLD,
        CURRENT_DELIVERY_SUCCESS_NEW,
        "split model success delivery",
    ) if CURRENT_DELIVERY_SUCCESS_NEW not in scheduler_source else scheduler_source
    scheduler_source = _replace_once(
        scheduler_source,
        CURRENT_DELIVERY_FAILURE_OLD,
        CURRENT_DELIVERY_FAILURE_NEW,
        "split failure delivery",
    ) if CURRENT_DELIVERY_FAILURE_NEW not in scheduler_source else scheduler_source
    if CURRENT_DELIVERY_REMEDIATION_LEGACY in scheduler_source:
        scheduler_source = _replace_once(
            scheduler_source,
            CURRENT_DELIVERY_REMEDIATION_LEGACY,
            CURRENT_DELIVERY_REMEDIATION_NEW,
            "split repair delivery state migration",
        )
    scheduler_source = _replace_once(
        scheduler_source,
        CURRENT_DELIVERY_REMEDIATION_ANCHOR,
        CURRENT_DELIVERY_REMEDIATION_NEW,
        "split repair delivery boundary",
    ) if CURRENT_DELIVERY_REMEDIATION_NEW not in scheduler_source else scheduler_source
    # The owning run body already keeps original agents live until after
    # `_save_compose_deliver`; hand repair agents through that exact list too.
    old_signature = '''    adapters, loop, verbose: bool, execution_token,
) -> None:
'''
    new_signature = '''    adapters, loop, verbose: bool, execution_token, deferred_agents: list,
) -> None:
'''
    if new_signature not in scheduler_source:
        scheduler_source = _replace_once(
            scheduler_source, old_signature, new_signature, "split delivery signature"
        )
    old_call = '''                d, fence, final_response, output, adapters=adapters, loop=loop, verbose=verbose,
                execution_token=execution_token)
'''
    new_call = '''                d, fence, final_response, output, adapters=adapters, loop=loop, verbose=verbose,
                execution_token=execution_token, deferred_agents=_deferred_agents)
'''
    if new_call not in scheduler_source:
        scheduler_source = _replace_once(
            scheduler_source, old_call, new_call, "split delivery deferred agents"
        )
    rejection_anchor = "    (\n        deliver_content, d.blocked_config, _silent_alert, d.incident_acked, d.failure_incident_id,\n"
    rejection_guard = """    if d.success:
        rejection = _cron_operator_success_error(job, final_response)
        if rejection is not None:
            d.success, d.error = False, rejection

"""
    if rejection_guard not in scheduler_source:
        scheduler_source = _replace_once(scheduler_source, rejection_anchor,
                                         rejection_guard + rejection_anchor,
                                         "split rejected-success boundary")
    previous_format_hint = ('    "FORMAT: Write a concise plain-language report. The system adds the job label. "\n'
                            '    "Keep paths, IDs, raw logs, and detailed counters in the saved receipt. "\n')
    format_hint = ('    "FORMAT: Give the complete report requested by the job in clear technical English. "\n'
                   '    "Include the findings, relevant evidence, reasons, limitations, and useful next steps. "\n'
                   '    "Preserve the agent personality. The system adds the job label. "\n'
                   '    "Keep raw logs and internal identifiers in the saved receipt, but include any "\n'
                   '    "details, counts, or artifact links the user needs to understand or use the result. "\n')
    if previous_format_hint in prompt_source:
        prompt_source = _replace_once(prompt_source, previous_format_hint, format_hint,
                                     "upgrade cron report depth")
    elif format_hint not in prompt_source:
        anchor = '    "SILENT: If there is genuinely nothing new to report, respond "\n'
        prompt_source = _replace_once(prompt_source, anchor, format_hint + anchor, "split cron prompt")
    if scheduler_source == scheduler.read_text(encoding="utf-8") and prompt_source == prompt.read_text(encoding="utf-8"):
        return False
    compile(scheduler_source, str(scheduler), "exec")
    compile(prompt_source, str(prompt), "exec")
    scheduler.write_text(scheduler_source, encoding="utf-8")
    prompt.write_text(prompt_source, encoding="utf-8")
    return True


def _upgrade_rejected_success(source: str) -> str:
    import ast
    existing = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    source = source.replace(WITHHELD_CONSTANT_OLD, "")
    for name in ("_attempt_cron_failure_remediation", "_cron_operator_has_hard_detail", "_cron_operator_delivery_candidate", "_format_cron_operator_delivery", "_is_cron_operator_delivery"):
        start = HELPER_SOURCE.index(f"def {name}(")
        end = HELPER_SOURCE.find("\ndef ", start + 1)
        replacement = HELPER_SOURCE[start:] if end == -1 else HELPER_SOURCE[start:end]
        if end == -1 and source.find("\ndef ", source.index(f"def {name}(") + 1) != -1:
            replacement = replacement.removesuffix("\n")
        if name in existing and ast.dump(existing[name]) == ast.dump(ast.parse(replacement).body[0]):
            continue
        source = _replace_helper_function(source, name, replacement)
    if "def _cron_operator_success_error(" in source:
        return source
    for name in ("_cron_operator_failure_message",):
        start = HELPER_SOURCE.index(f"def {name}(")
        end = HELPER_SOURCE.index("\ndef ", start + 1)
        source = _replace_helper_function(source, name, HELPER_SOURCE[start:end])
    start = HELPER_SOURCE.index("def _format_cron_operator_delivery(")
    end = HELPER_SOURCE.index("def _is_cron_operator_delivery(", start)
    old_start = source.index("def _format_cron_operator_delivery(")
    old_end = source.index("def _is_cron_operator_delivery(", old_start)
    return source[:old_start] + HELPER_SOURCE[start:end] + source[old_end:]


def _replace_helper_function(source: str, name: str, replacement: str) -> str:
    start = source.index(f"def {name}(")
    end = source.find("\ndef ", start + 1)
    if end == -1:
        end = len(source)
    return source[:start] + replacement + source[end:]


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


OUTER_EXCEPTION_DELIVERY_SPLIT_OLD = """            _summarize_cron_failure_for_delivery(job, err_text) + _failure_streak_nudge(job),
"""


OUTER_EXCEPTION_DELIVERY_SPLIT_NEW = """            _format_cron_operator_delivery_with_media(
                job.get("name") or job.get("id"),
                err_text,
                success=False,
                job_lane="script" if job.get("no_agent") else "model",
                failure_kind="pre_repair",
            ) + _failure_streak_nudge(job),
"""


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"cron operator delivery {label} anchor drift: {count}")
    return source.replace(old, new, 1)


def _patch_optional_outer_exception_delivery(source: str) -> str:
    candidates = (
        OUTER_EXCEPTION_DELIVERY_OLD,
        OUTER_EXCEPTION_DELIVERY_LATEST_OLD,
        OUTER_EXCEPTION_DELIVERY_SPLIT_OLD,
    )
    replacements = (
        OUTER_EXCEPTION_DELIVERY_NEW,
        OUTER_EXCEPTION_DELIVERY_LATEST_NEW,
        OUTER_EXCEPTION_DELIVERY_SPLIT_NEW,
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






PRIVATE_JOBS_HELPER = """
# golden-cron-private-failure-delivery-v1
# Successful destinations are unchanged; operational failures stay tenant-local.
def _golden_private_cron_jobs(jobs):
    import os
    # Trusted launcher configuration may allow one operator-owned destination.
    operator_destination = os.environ.get("HERMES_CRON_OPERATOR_FAILURE_DESTINATION", "").strip()
    for job in jobs:
        if isinstance(job, dict):
            explicit = job.get("failure_deliver")
            if not operator_destination or explicit != operator_destination:
                job["failure_deliver"] = "local"
    return jobs

"""


def patch_private_failure_jobs(source: str) -> str:
    """Protect loaded jobs and all native saves, including reconciliation merges."""
    import ast

    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "_golden_private_cron_jobs" in functions:
        # Exact owned code, not a marker alone, proves this policy is installed.
        helper = ast.get_source_segment(source, functions["_golden_private_cron_jobs"])
        expected = PRIVATE_JOBS_HELPER[PRIVATE_JOBS_HELPER.index("def "):].strip()
        if helper != expected:
            previous = 'def _golden_private_cron_jobs(jobs):\n    for job in jobs:\n        if isinstance(job, dict):\n            job["failure_deliver"] = "local"\n    return jobs'
            if helper != previous:
                raise RuntimeError("cron private failure helper drift")
            source = source.replace(helper, expected, 1)
            return patch_private_failure_jobs(source)
    load = functions.get("load_jobs")
    saver = functions.get("_stage_jobs_payload") or functions.get("save_jobs")
    if load is None or saver is None:
        raise RuntimeError("cron job storage boundary missing")
    lines = source.splitlines(keepends=True)
    edits = []
    returns = [node for node in ast.walk(load) if isinstance(node, ast.Return)
               and ast.unparse(node.value) in {"jobs", "_golden_private_cron_jobs(jobs)"}]
    if len(returns) != 1:
        raise RuntimeError("cron load boundary drift")
    returned = returns[0]
    if ast.unparse(returned.value) == "jobs":
        edits.append((returned.lineno - 1, returned.end_lineno,
                      " " * returned.col_offset + "return _golden_private_cron_jobs(jobs)\n"))
    body = saver.body
    first = 1 if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) else 0
    insertion = body[first]
    if ast.unparse(insertion) != "_golden_private_cron_jobs(jobs)":
        edits.append((insertion.lineno - 1, insertion.lineno - 1,
                      " " * insertion.col_offset + "_golden_private_cron_jobs(jobs)\n"))
    for start, end, text in sorted(edits, reverse=True):
        lines[start:end] = [text]
    patched = "".join(lines)
    if "_golden_private_cron_jobs" not in functions:
        patched += PRIVATE_JOBS_HELPER
    compile(patched, "cron/jobs.py", "exec")
    return patched


DELIVERY_ACK_HELPER = r'''

def _record_cron_delivery_ack(job: dict, output_file, final_response: str) -> None:
    """Record confirmed delivery beside the retained output, never generated-only state."""
    import hashlib
    import json
    import os
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path

    path = Path(output_file)
    temporary = None
    try:
        receipt = {
            "version": 1,
            "job_id": str(job["id"]),
            "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "response_sha256": hashlib.sha256(final_response.encode("utf-8")).hexdigest(),
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".delivery-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(receipt, stream, sort_keys=True)
        os.replace(temporary, path.with_suffix(".delivery.json"))
    except (OSError, ValueError, UnicodeError):
        # A receipt write cannot undo a send or authorize sending it again.
        logger.exception("Delivery succeeded but acknowledgment persistence failed for job %s", job["id"])
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
'''


def patch_delivery_ack_source(source: str) -> str:
    """Attach acknowledgment to the existing confirmed-success boundary."""
    import ast
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    edits = []
    owner = "_save_compose_deliver" if any(isinstance(node, ast.FunctionDef) and node.name == "_save_compose_deliver" for node in tree.body) else "_run_one_job_body"
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name != owner:
            continue
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "_record_cron_delivery_ack" for node in ast.walk(function)):
            continue
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Assign)
                 and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                 and node.value.func.id == "_deliver_result" and len(node.value.args) > 1
                 and ast.unparse(node.value.args[1]) == "deliver_content"]
        if len(calls) != 1:
            raise RuntimeError(f"delivery acknowledgment anchor drift: {function.name}")
        node = calls[0]
        state = "d." if function.name == "_save_compose_deliver" else ""
        indent = " " * node.col_offset
        addition = (f"{indent}if {state}success and not {state}delivery_error and _resolve_delivery_targets(job):\n"
                    f"{indent}    _record_cron_delivery_ack(job, output_file, final_response)\n")
        edits.append((node.end_lineno, addition))
    for line, addition in sorted(edits, reverse=True):
        lines.insert(line, addition)
    source = "".join(lines)
    if edits and "def _record_cron_delivery_ack(" not in source:
        source += DELIVERY_ACK_HELPER
    # External workers execute this module with -m. An appended helper is not
    # defined until after __main__ returns, so a successful send raised NameError.
    # Move the existing definition; never retry or rewrite a delivery receipt.
    nodes = ast.parse(source).body
    helper = next((node for node in nodes if isinstance(node, ast.FunctionDef)
                   and node.name == "_record_cron_delivery_ack"), None)
    entry = next((node for node in nodes if isinstance(node, ast.If)
                  and ast.unparse(node.test) == "__name__ == '__main__'"), None)
    if helper is not None and entry is not None and helper.lineno > entry.lineno:
        lines = source.splitlines(keepends=True)
        definition = lines[helper.lineno - 1:helper.end_lineno]
        del lines[helper.lineno - 1:helper.end_lineno]
        lines[entry.lineno - 1:entry.lineno - 1] = definition + ["\n\n"]
        source = "".join(lines)
    # The whole reply must be a silence token; a token at the edge of a report
    # does not cancel its useful content. Other gateway lanes are unchanged.
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_is_cron_silence_response":
            lines = source.splitlines(keepends=True)
            lines[node.lineno - 1:node.end_lineno] = [
                'def _is_cron_silence_response(text: str) -> bool:\n'
                '    return isinstance(text, str) and text.strip().upper() in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}\n']
            source = "".join(lines)
            break
    compile(source, "cron/scheduler.py", "exec")
    return source


def patch_legacy_rejected_delivery(source: str) -> str:
    """Keep rejected results private on schedulers predating failure routing."""
    import ast
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name != "_run_one_job_body":
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and ast.unparse(node.targets[0]) == "should_deliver"
                    and ast.unparse(node.value) == "bool(deliver_content.strip())"):
                lines[node.lineno - 1:node.end_lineno] = [
                    " " * node.col_offset
                    + 'should_deliver = bool(deliver_content.strip()) and failure_kind != "delivery_contract"\n'
                ]
    return "".join(lines)


def patch_cron_operator_delivery_v1(hermes_dir: Path) -> bool:
    jobs = Path(hermes_dir) / "cron/jobs.py"
    # Some scheduler-only compatibility fixtures omit storage. Complete runtime
    # assembly separately requires cron/jobs.py through the release-floor gate.
    original = jobs.read_text(encoding="utf-8") if jobs.is_file() else None
    updated = patch_private_failure_jobs(original) if original is not None else None
    changed = _patch_cron_operator_delivery(hermes_dir)
    scheduler = Path(hermes_dir) / TARGET
    if scheduler.is_file():
        before_ack = scheduler.read_text(encoding="utf-8")
        after_ack = _patch_optional_outer_exception_delivery(
            patch_legacy_rejected_delivery(patch_delivery_ack_source(before_ack))
        )
        if after_ack != before_ack:
            scheduler.write_text(after_ack, encoding="utf-8")
            changed = True
    if updated is not None and updated != original:
        jobs.write_text(updated, encoding="utf-8")
        changed = True
    # Upstream edge-token suppression conflicts with Golden's whole-reply rule.
    tests = Path(hermes_dir) / "tests/cron/test_scheduler.py"
    if tests.is_file():
        import ast
        before = tests.read_text(encoding="utf-8")
        lines = before.splitlines(keepends=True)
        for node in sorted(ast.walk(ast.parse(before)), key=lambda n: getattr(n, "lineno", 0), reverse=True):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "test_silent_trailing_suppresses_delivery":
                block = "".join(lines[node.lineno - 1:node.end_lineno])
                block = block.replace("test_silent_trailing_suppresses_delivery", "test_silent_trailing_preserves_report")
                block = block.replace("must still suppress.", "Golden preserves the report.")
                block = block.replace("deliver_mock.assert_not_called()", "deliver_mock.assert_called_once()\n        assert response in deliver_mock.call_args.args[1]")
                lines[node.lineno - 1:node.end_lineno] = [block]
            elif node.name == "test_silent_is_case_insensitive":
                block = "".join(lines[node.lineno - 1:node.end_lineno])
                lines[node.lineno - 1:node.end_lineno] = [block.replace('"[silent] nothing new"', '"[silent]"')]
        after = "".join(lines)
        if after != before:
            compile(after, str(tests), "exec")
            tests.write_text(after, encoding="utf-8")
            changed = True
    return changed


def _patch_cron_operator_delivery(hermes_dir: Path) -> bool:
    return _patch_current_split_scheduler(Path(hermes_dir))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    print("patched" if patch_cron_operator_delivery_v1(args.hermes_dir) else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
