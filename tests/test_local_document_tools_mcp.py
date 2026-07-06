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


class _FastMCPStub:
    def __init__(self, name: str):
        self.name = name

    def tool(self):
        def decorator(fn):
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
        os.environ["LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES"] = "1000"
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        if self.old_limit is None:
            os.environ.pop("LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES", None)
        else:
            os.environ["LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES"] = self.old_limit
        self.tmp.cleanup()

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
