"""Preserve native ACP edit failure outcomes without changing approval policy."""
from pathlib import Path

MARKER = "HERMES_ACP_EDIT_APPROVAL_OUTCOMES_v1"

_OLD_ERROR = '''def _denied(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
'''
_NEW_ERROR = '''# HERMES_ACP_EDIT_APPROVAL_OUTCOMES_v1
class EditApprovalRequestError(RuntimeError):
    """A failed or cancelled request is not an explicit client denial."""

    def __init__(self, outcome: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.outcome = outcome
        self.retryable = retryable


def _denied(message: str, outcome: str = "denied", retryable: bool = False) -> str:
    return json.dumps({"error": message, "edit_approval": {
        "outcome": outcome, "retryable": retryable, "file_modified": False,
    }}, ensure_ascii=False)
'''

_OLD_GUARD = '''    try:
        approved = bool(requester(proposal))
    except Exception as exc:
        logger.warning("ACP edit approval requester failed: %s", exc)
        approved = False
    return None if approved else _denied("Edit approval denied by ACP client; file was not modified.")
'''
_NEW_GUARD = '''    try:
        approved = bool(requester(proposal))
    except EditApprovalRequestError as exc:
        return _denied(str(exc), exc.outcome, exc.retryable)
    except Exception as exc:
        logger.warning("ACP edit approval requester failed (%s)", type(exc).__name__)
        return _denied(
            "Edit approval denied because the request failed; no client denial was received and the file was not modified. "
            "Check the native ACP connection before retrying.", "request_failed", True)
    return None if approved else _denied("Edit approval denied by ACP client; file was not modified.")
'''

_OLD_OUTCOME = '''        outcome = getattr(response, "outcome", None)
        return getattr(outcome, "outcome", None) == "selected" and getattr(outcome, "option_id", None) == "allow_once"
'''
_NEW_OUTCOME = '''        if _timed_out:
            raise EditApprovalRequestError(
                "timeout", "Edit approval timed out without a client decision; the file was not modified. "
                "Check the native approval card before retrying.", True)
        if response is None:
            raise EditApprovalRequestError(
                "request_failed", "Edit approval request failed before a decision was received; "
                "the file was not modified. Check the native ACP connection before retrying.", True)
        outcome = getattr(response, "outcome", None)
        kind = getattr(outcome, "outcome", None)
        if kind == "cancelled":
            raise EditApprovalRequestError(
                "cancelled", "Edit approval was cancelled; the file was not modified.")
        if kind == "selected":
            option_id = getattr(outcome, "option_id", None)
            if option_id == "allow_once":
                return True
            if option_id == "deny":
                return False
        raise EditApprovalRequestError(
            "invalid_response", "Edit approval returned an unrecognized decision; the file was not modified. "
            "Inspect the native ACP client response before retrying.")
'''


def patch_acp_edit_approval_outcomes_v1(root: Path) -> bool:
    """Preflight every exact pre/postimage and syntax before changing one file."""
    path = root / "acp_adapter/edit_approval.py"
    original = path.read_text(encoding="utf-8")
    source = original
    for old, new in (
        (_OLD_ERROR, _NEW_ERROR), (_OLD_GUARD, _NEW_GUARD), (_OLD_OUTCOME, _NEW_OUTCOME),
        ('return _denied(f"Edit approval denied: could not prepare diff ({exc})")',
         'return _denied(f"Edit approval blocked: could not prepare diff ({exc})", "invalid_proposal")'),
    ):
        if source.count(new) == 1:
            continue
        if new in source or source.count(old) != 1:
            raise RuntimeError("acp_adapter/edit_approval.py: approval outcome anchor missing or ambiguous")
        source = source.replace(old, new, 1)
    compile(source, str(path), "exec")
    if source != original:
        path.write_text(source, encoding="utf-8")
    return True
