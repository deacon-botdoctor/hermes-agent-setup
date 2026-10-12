"""Keep background-review prompts within the tenant's memory promotion contract."""
from __future__ import annotations

import ast
from pathlib import Path

MEMORY = '''Review only supported memory candidates. Follow the current tenant's storage and promotion rules.
Native USER memory is for explicit durable preferences or repeated corrections.
Native MEMORY is for compact nearby continuity with a repeated operational need or an explicit request to keep it nearby.
Keep one-time steering, tentative assent, jokes, inferred traits, current task progress, and unresolved observations in the session.
Durable project/client facts, rosters, research, decisions, and runbooks belong in the tenant's canonical library or project records, not native memory.
Use the designated writer when available and authorized. This review has no authority to enable a writer, change permissions, or substitute another store.
Do not infer new standing rules from frustration or casual approval. Preserve explicit forget requests and distinguish them from dated corrections.
If nothing qualifies, say 'Nothing to save.' and stop. That is a successful review.
'''

SKILLS = '''Review the skill library only for verified, reusable procedures and explicit enduring workflow corrections.
Follow the tenant's promotion requirements. A passing remark, tentative assent, one-time workaround, or unverified worker report does not authorize a permanent rule.
There is no quota for skill updates. If nothing qualifies, say 'Nothing to save.' and stop. That is a successful review.
Prefer updating the relevant existing skill. Read it first, remove obsolete guidance in place, and avoid duplicate rules or session archives.
Keep user identity, client facts, account details, infrastructure state, and project status in their designated stores. Do not turn them into reusable procedures.
Preserve protected bundled and hub-installed skills. Do not delete, archive, or consolidate a pinned skill.
Keep temporary tool failures out of standing rules. Save a tested repair procedure only when it meets the tenant's promotion requirements.
'''

MARKER = 'HERMES_MEMORY_PROMOTION_SCOPE_v1'

PROMPTS = {'_MEMORY_REVIEW_PROMPT': MEMORY, '_SKILL_REVIEW_PROMPT': SKILLS,
           '_COMBINED_REVIEW_PROMPT': MEMORY + '\n' + SKILLS}


def patch_source(source):
    tree = ast.parse(source)
    assignments = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in PROMPTS:
                if name in assignments:
                    raise RuntimeError('duplicate memory-review prompt assignment')
                assignments[name] = node
    if set(assignments) != set(PROMPTS):
        raise RuntimeError('memory-review prompt anchors changed')
    lines = source.splitlines(keepends=True)
    for name, node in sorted(assignments.items(), key=lambda item: item[1].lineno, reverse=True):
        lines[node.lineno-1:node.end_lineno] = [name + ' = ' + repr(PROMPTS[name]) + '\n']
    result = ''.join(lines)
    if MARKER not in result:
        result = '# ' + MARKER + '\n' + result
    ast.parse(result)
    return result


def patch_memory_promotion_scope_v1(hermes_dir: Path) -> bool:
    target = hermes_dir / 'agent/background_review.py'
    review = patch_source(target.read_text())
    tool = hermes_dir / 'tools/memory_tool.py'
    schema = patch_tool_source(tool.read_text())
    target.write_text(review)
    tool.write_text(schema)
    return True


OLD_TARGETS = ("TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
               "notes (environment, conventions, tool quirks, lessons).")
NEW_TARGETS = ("TARGETS: 'user' = explicit durable user preferences and repeated corrections. "
               "'memory' = compact nearby continuity or a short pointer needed across tasks.")


def patch_tool_source(source):
    """Change routing prose only; retain native flags, batches, limits and approval guards."""
    tree = ast.parse(source)
    edits = []
    descriptions = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        if 'WHEN: only for facts that apply to EVERY session' in text:
            start, end = text.index('WHEN:'), text.index('IF FULL:')
            text = text[:start] + MEMORY + '\n' + text[end:]
            descriptions += 1
        elif MEMORY in text:
            descriptions += 1
        text = text.replace(OLD_TARGETS, NEW_TARGETS)
        text = text.replace("TARGET: only 'memory' is enabled for personal notes (environment, conventions, tool quirks, lessons).",
                            "TARGET: only 'memory' is enabled for compact nearby continuity or a short canonical pointer.")
        text = text.replace("TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).",
                            "TARGET: only 'user' is enabled for explicit durable preferences and repeated corrections.")
        if text != node.value:
            edits.append((node, repr(text)))
    if descriptions != 1:
        raise RuntimeError('native memory schema description anchor changed')
    # AST offsets are UTF-8 byte offsets, including on Windows.
    lines = source.encode('utf-8').splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    data = source.encode('utf-8')
    for node, replacement in sorted(edits, key=lambda item: (item[0].lineno, item[0].col_offset), reverse=True):
        a = offsets[node.lineno - 1] + node.col_offset
        b = offsets[node.end_lineno - 1] + node.end_col_offset
        data = data[:a] + replacement.encode('utf-8') + data[b:]
    result = data.decode('utf-8')
    ast.parse(result)
    return result
