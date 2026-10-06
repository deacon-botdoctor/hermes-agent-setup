"""Keep failed-mutation diagnostics internal for exact cron silence replies."""
import ast
from pathlib import Path

MARKER = 'HERMES_CRON_SILENT_MUTATION_FOOTER_v1'
ANCHOR = '''            if footer:
                final_response = final_response.rstrip() + "\\n\\n" + footer
'''
REPLACEMENT = '''            if footer:
                # HERMES_CRON_SILENT_MUTATION_FOOTER_v1: mirror cron.scheduler's
                # _is_cron_silence_response exactly; reports quoting it still deliver.
                if (getattr(agent, "platform", None) == "cron"
                        and isinstance(final_response, str)
                        and final_response.strip().upper() in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}):
                    logger.warning(
                        "file_mutation_verifier: count=%d reason=cron_silence_footer_suppressed",
                        len(_failed),
                        extra={"failed_mutation_count": len(_failed),
                               "reason": "cron_silence_footer_suppressed"},
                    )
                    return final_response
                final_response = final_response.rstrip() + "\\n\\n" + footer
'''
PERSIST_ANCHOR = '''        _close_transcript_tail(agent, messages, final_response, interrupted, _recovered_from_stream)
'''
PERSIST_REPLACEMENT = '''        # Decide silent cron presentation before transcript persistence. Other turns
        # retain the existing post-persistence footer order.
        if (not interrupted and getattr(agent, "platform", None) == "cron"
                and isinstance(final_response, str)
                and final_response.strip().upper() in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}):
            final_response = _append_file_mutation_footer(agent, final_response, logger)
            _cron_silence_footer_checked[0] = True
        _close_transcript_tail(agent, messages, final_response, interrupted, _recovered_from_stream)
'''
# The closure flag also prevents duplicate warning records after persistence.
EDITS = ((ANCHOR, REPLACEMENT),
         ('    def _persist_step():\n', '    _cron_silence_footer_checked = [False]\n    def _persist_step():\n'),
         (PERSIST_ANCHOR, PERSIST_REPLACEMENT),
         ('    if final_response and not interrupted:\n        final_response = _append_file_mutation_footer(agent, final_response, logger)\n',
          '    if final_response and not interrupted and not _cron_silence_footer_checked[0]:\n        final_response = _append_file_mutation_footer(agent, final_response, logger)\n'))


def patch_cron_silent_mutation_footer_v1(root: Path) -> bool:
    target = Path(root) / 'agent/turn_finalizer.py'
    source = target.read_text(encoding='utf-8')
    if MARKER in source:
        if not all(after in source for _, after in EDITS):
            raise RuntimeError('cron silence footer postimage drift')
        return False
    for before, after in EDITS:
        if source.count(before) != 1:
            raise RuntimeError('cron silence footer anchor drift')
        source = source.replace(before, after, 1)
    ast.parse(source)
    target.write_text(source, encoding='utf-8')
    return True
