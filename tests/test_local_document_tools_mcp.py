from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "mcp-servers/local-document-tools/src/local_document_tools_mcp/server.py"
SRC = REPO / "mcp-servers/local-document-tools/src"


class _FastMCPStub:
    def __init__(self, name: str):
        self.name = name
        self.tools = []

    def tool(self):
        def decorator(fn):
            self.tools.append(fn.__name__)
            return fn

        return decorator

    def run(self):
        return None


def _install_mcp_stub() -> None:
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FastMCPStub
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = server_mod
    sys.modules["mcp.server.fastmcp"] = fastmcp_mod


def _load():
    _install_mcp_stub()
    spec = importlib.util.spec_from_file_location("local_document_tools_mcp_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class LocalDocumentToolsTests(unittest.TestCase):
    def setUp(self):
        self.old_limit = os.environ.get("LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES")
        self.old_roots = os.environ.get("LOCAL_DOCUMENT_TOOLS_ROOTS")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES"] = "1000"
        os.environ["LOCAL_DOCUMENT_TOOLS_ROOTS"] = str(self.root)
        self.mod = _load()

    def tearDown(self):
        if self.old_limit is None:
            os.environ.pop("LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES", None)
        else:
            os.environ["LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES"] = self.old_limit
        if self.old_roots is None:
            os.environ.pop("LOCAL_DOCUMENT_TOOLS_ROOTS", None)
        else:
            os.environ["LOCAL_DOCUMENT_TOOLS_ROOTS"] = self.old_roots
        self.tmp.cleanup()

    def test_registers_document_convert_tool(self):
        self.assertIn("document_convert", self.mod.mcp.tools)

    def test_package_exports_document_convert(self):
        _install_mcp_stub()
        sys.path.insert(0, str(SRC))
        try:
            sys.modules.pop("local_document_tools_mcp", None)
            sys.modules.pop("local_document_tools_mcp.server", None)
            package = importlib.import_module("local_document_tools_mcp")
        finally:
            sys.path.remove(str(SRC))

        self.assertIs(package.document_convert, package.server.document_convert)
        self.assertIn("document_convert", package.__all__)

    def test_document_info_and_extract_text(self):
        path = self.root / "note.txt"
        path.write_text("hello\nworld")

        info = self.mod.document_info(str(path))
        self.assertTrue(info["ok"])
        self.assertEqual(info["suffix"], ".txt")

        extracted = self.mod.extract_text(str(path))
        self.assertTrue(extracted["ok"])
        self.assertEqual(extracted["text"], "hello\nworld")

    def test_html_to_text_strips_scripts_and_tags(self):
        path = self.root / "page.html"
        path.write_text("<h1>Title</h1><script>bad()</script><p>Hello &amp; goodbye</p>")

        result = self.mod.html_to_text(str(path))
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Title\nHello & goodbye")

    def test_document_convert_supports_html_to_text(self):
        path = self.root / "page.html"
        path.write_text("<h1>Title</h1><p>Hello &amp; goodbye</p>")

        result = self.mod.document_convert(str(path), target_format="text", source_format="text/html")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Title\nHello & goodbye")

    def test_document_convert_reports_unsupported_conversion(self):
        path = self.root / "document.pdf"
        path.write_text("%PDF-1.7")

        result = self.mod.document_convert(str(path), target_format="text", source_format="pdf")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "unsupported_conversion")
        self.assertIn("unsupported_conversion", result["error"])

    def test_rejects_files_outside_configured_roots(self):
        with tempfile.TemporaryDirectory() as outside:
            existing_path = Path(outside) / "secret.txt"
            missing_path = Path(outside) / "missing.txt"
            existing_path.write_text("secret")

            existing_result = self.mod.extract_text(str(existing_path))
            missing_result = self.mod.extract_text(str(missing_path))

        self.assertFalse(existing_result["ok"])
        self.assertFalse(missing_result["ok"])
        self.assertEqual(existing_result["error"], "path is outside configured document roots")
        self.assertEqual(missing_result["error"], "path is outside configured document roots")
        self.assertEqual(existing_result["error"], missing_result["error"])
        self.assertNotIn(str(self.root), existing_result["error"])
        self.assertNotIn(str(self.root), missing_result["error"])

    def test_read_text_limits_bytes_from_open_file(self):
        path = self.root / "large.txt"
        path.write_text("abcdef")
        self.mod.MAX_READ_BYTES = 4

        result = self.mod.extract_text(str(path))

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "abcd")

    def test_merge_text_documents_reports_partial_errors(self):
        one = self.root / "one.txt"
        two = self.root / "two.html"
        missing = self.root / "missing.txt"
        one.write_text("One")
        two.write_text("<p>Two</p>")

        result = self.mod.merge_text_documents([str(one), str(two), str(missing)], separator="\n--\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["documents"], 2)
        self.assertEqual(result["text"], "One\n--\nTwo")
        self.assertEqual(result["errors"][0]["path"], str(missing))


if __name__ == "__main__":
    unittest.main(verbosity=2)
