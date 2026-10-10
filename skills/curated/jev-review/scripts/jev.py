#!/usr/bin/env python3
"""Portable choice reviews. Results are evidence, never action authorization."""
import argparse
import hashlib
import json
import math
import os
import sys
import urllib.request

MODEL = "jev-1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_REQUEST_BYTES = 80000
MAX_RESPONSE_BYTES = 262144
TIMEOUT = 20


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _loads(data):
    def reject_constant(value):
        raise ValueError("nonfinite number")
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=reject_constant)


def _result(status, reason, source=None, request=None, answers=None, usage=None):
    return {"schema": "jev-review/v1", "advisory_only": True,
            "review_required": True, "status": status, "reason": reason,
            "source_sha256": source, "request_sha256": request,
            "answers": answers or {}, "usage": usage or {}}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError()


def _prepare(payload):
    _json_value(payload)
    if not isinstance(payload, dict) or set(payload) != {"state", "questions"}:
        raise ValueError()
    state = payload["state"]
    if not isinstance(state, (str, dict, list)) or (isinstance(state, str) and not state.strip()):
        raise ValueError()
    questions = payload["questions"]
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 16:
        raise ValueError()
    for qid, question in questions.items():
        if not _text(qid) or not isinstance(question, dict):
            raise ValueError()
        if set(question) != {"type", "instructions", "criteria"}:
            raise ValueError()
        if question["type"] != "choice" or not _text(question["instructions"]):
            raise ValueError()
        criteria = question["criteria"]
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 32:
            raise ValueError()
        if not all(_text(k) and _text(v) for k, v in criteria.items()):
            raise ValueError()
    state_bytes = canonical(state)
    body = {"model": MODEL, "state": state if isinstance(state, str)
            else state_bytes.decode("utf-8"), "questions": questions}
    request_bytes = canonical(body)
    return request_bytes, hashlib.sha256(state_bytes).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request_bytes, key, timeout):
    request = urllib.request.Request(
        ENDPOINT, data=request_bytes,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _answers(data, questions):
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise ValueError()
    if set(data["answers"]) != set(questions):
        raise ValueError()
    clean = {}
    for qid, question in questions.items():
        answer = data["answers"][qid]
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise ValueError()
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in question["criteria"]:
            raise ValueError()
        if not _probability(answer.get("confidence")):
            raise ValueError()
        if "probabilities" in answer:
            probabilities = answer["probabilities"]
            if not isinstance(probabilities, dict):
                raise ValueError()
            if not set(probabilities).issubset(question["criteria"]):
                raise ValueError()
            if not all(_probability(value) for value in probabilities.values()):
                raise ValueError()
        clean[qid] = {"type": "choice", "choice": choice,
                      "confidence": answer["confidence"]}
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError()
    clean_usage = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        if field in usage:
            if type(usage[field]) is not int or usage[field] < 0:
                raise ValueError()
            clean_usage[field] = usage[field]
    return clean, clean_usage


def review(payload, key=None, *, transport=None, dry_run=False):
    """Review a choice payload. Optional transport(bytes, key, timeout) -> bytes.

    None reads TYPESAFE_API_KEY; an explicit empty key never reads the environment.
    Dry runs validate without reading credentials or calling the transport.
    """
    try:
        request_bytes, source = _prepare(payload)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return _result("invalid", "invalid_input")
    request_hash = hashlib.sha256(request_bytes).hexdigest()
    def result(status, reason, **kwargs):
        return _result(status, reason, source, request_hash, **kwargs)
    if len(request_bytes) > MAX_REQUEST_BYTES:
        return result("invalid", "request_too_large")
    if dry_run:
        return result("validated", "ok")
    if key is None:
        key = os.environ.get("TYPESAFE_API_KEY")
    if not isinstance(key, str) or not key.strip():
        return result("unavailable", "missing_key")
    try:
        response = (transport or _transport)(request_bytes, key, TIMEOUT)
    except Exception:
        return result("unavailable", "provider_unavailable")
    if not isinstance(response, bytes):
        return result("unavailable", "invalid_response")
    if len(response) > MAX_RESPONSE_BYTES:
        return result("unavailable", "response_too_large")
    try:
        answers, usage = _answers(_loads(response), payload["questions"])
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return result("unavailable", "invalid_response")
    return result("reviewed", "ok", answers=answers, usage=usage)


def self_test():
    payload = {"state": {"receipt": False}, "questions": {"status": {
        "type": "choice", "instructions": "Check receipt evidence.",
        "criteria": {"missing": "No receipt exists.", "present": "Receipt exists."}}}}
    def mock(body, key, timeout):
        assert _loads(body)["model"] == MODEL and key == "offline-test"
        assert timeout == TIMEOUT
        return canonical({"answers": {"status": {"type": "choice", "choice": "missing",
                                                "confidence": 0.9}}})
    assert review({}, key="")["status"] == "invalid"
    assert review(payload, key="")["reason"] == "missing_key"
    assert review(payload, key="offline-test", transport=mock)["status"] == "reviewed"
    assert review(payload, dry_run=True)["status"] == "validated"
    return _result("validated", "ok")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError()


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSON file, or - for stdin")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.self_test:
            result = self_test()
        else:
            try:
                if args.input == "-":
                    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
                else:
                    with open(args.input, "rb") as source:
                        raw = source.read(MAX_REQUEST_BYTES + 1)
            except OSError:
                result = _result("invalid", "input_unreadable")
            else:
                if len(raw) > MAX_REQUEST_BYTES:
                    result = _result("invalid", "request_too_large")
                else:
                    result = review(_loads(raw), dry_run=args.dry_run)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        result = _result("invalid", "invalid_input")
    print(json.dumps(result, allow_nan=False, separators=(",", ":")))
    return {"reviewed": 0, "validated": 0, "invalid": 2, "unavailable": 3}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
