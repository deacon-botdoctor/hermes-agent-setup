#!/usr/bin/env python3
"""Require a hash-bound release receipt for outbound PDFs when configured."""
from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Optional

MARKER = "HERMES_PDF_RELEASE_RECEIPT_GATE_v1"
BACKUP_SUFFIX = ".bak-pre-pdf-release-receipt-gate-v1"

IMPORT_ANCHOR = "import os\n"
IMPORT_REPLACEMENT = "import os\nimport hashlib\nimport json\n"

ANCHOR = '''            if safe_path:
                safe_media.append((safe_path, bool(is_voice)))
            else:
                logger.warning("Skipping unsafe MEDIA directive path: %s", _log_safe_path(raw))
'''

REPLACEMENT = '''            if safe_path:
                # HERMES_PDF_RELEASE_RECEIPT_GATE_v1
                release_root_raw = os.environ.get("HERMES_PDF_RELEASE_RECEIPT_DIR", "").strip()
                if release_root_raw and Path(safe_path).suffix.lower() == ".pdf":
                    release_root = Path(release_root_raw).expanduser().resolve()
                    pdf_path = Path(safe_path).expanduser().resolve()
                    receipt_path = Path(str(pdf_path) + ".delivery.json")
                    expected_kind = os.environ.get(
                        "HERMES_PDF_RELEASE_RECEIPT_KIND", "hermes-pdf-release-receipt"
                    ).strip()
                    try:
                        pdf_path.relative_to(release_root)
                        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                        expected_sha = str((receipt.get("pdf") or {}).get("sha256") or "")
                        actual_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
                        receipt_ok = (
                            receipt.get("kind") == expected_kind
                            and receipt.get("ok") is True
                            and receipt.get("status") == "pass"
                            and str((receipt.get("pdf") or {}).get("path") or "") == str(pdf_path)
                            and expected_sha == actual_sha
                        )
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        receipt_ok = False
                    if not receipt_ok:
                        logger.warning(
                            "Skipping PDF without a valid release receipt: %s", _log_safe_path(raw)
                        )
                        continue
                safe_media.append((safe_path, bool(is_voice)))
            else:
                logger.warning("Skipping unsafe MEDIA directive path: %s", _log_safe_path(raw))
'''


NATIVE_ANCHOR = '        return [\n            (safe_path, bool(is_voice)) for media_path, is_voice in media_files or []\n            if (safe_path := _validated_delivery_path(media_path, session_key, "MEDIA directive path"))]\n'
NATIVE_REPLACEMENT = (
    "        safe_media = []\n"
    "        for media_path, is_voice in media_files or []:\n"
    "            raw = str(media_path)\n"
    "            safe_path = _validated_delivery_path(media_path, session_key, 'MEDIA directive path')\n"
    + REPLACEMENT[:REPLACEMENT.rindex("            else:")]
    + "        return safe_media\n"
)

def patch_source(source: str) -> Optional[str]:
    if MARKER in source:
        return None
    if source.count(IMPORT_ANCHOR) != 1:
        raise RuntimeError("[pdf_release_receipt_gate] import anchor mismatch")
    if source.count(NATIVE_ANCHOR) == 1:
        patched = source.replace(IMPORT_ANCHOR, IMPORT_REPLACEMENT, 1).replace(NATIVE_ANCHOR, NATIVE_REPLACEMENT, 1)
        ast.parse(patched)
        return patched
    if source.count(ANCHOR) != 1:
        raise RuntimeError("[pdf_release_receipt_gate] delivery anchor mismatch")
    patched = source.replace(IMPORT_ANCHOR, IMPORT_REPLACEMENT, 1).replace(ANCHOR, REPLACEMENT, 1)
    ast.parse(patched)
    return patched


def patch_pdf_release_receipt_gate_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "gateway" / "platforms" / "base.py"
    if not target.exists():
        raise RuntimeError(f"[pdf_release_receipt_gate] runtime target missing: {target}")
    source = target.read_text(encoding="utf-8")
    patched = patch_source(source)
    if patched is None:
        return False
    backup = Path(str(target) + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(patched, encoding="utf-8")
    return True
