from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ANAMNESIS = REPO / "mcp-servers/anamnesis/src/anamnesis_mcp/server.py"
VISUAL = REPO / "mcp-servers/visual-identity/src/visual_identity_mcp/server.py"
TELEGRAM = REPO / "mcp-servers/telegram-admin/src/telegram_admin_mcp/server.py"


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


def _load(path: Path, name: str):
    _install_mcp_stub()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class KnowledgeAdminMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_env = {
            key: os.environ.get(key)
            for key in ["ANAMNESIS_DB", "VISUAL_IDENTITY_MANIFEST", "VISUAL_IDENTITY_ROOT", "TELEGRAM_DIRECTORY"]
        }

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_anamnesis_records_and_searches_memory(self):
        os.environ["ANAMNESIS_DB"] = str(self.root / "memory.db")
        mod = _load(ANAMNESIS, f"anamnesis_{id(self)}")

        recorded = mod.memory_record("Friday deploys need extra care", kind="lesson", source="test")
        self.assertTrue(recorded["ok"])
        result = mod.memory_search("Friday")

        self.assertTrue(result["ok"])
        self.assertEqual(result["memories"][0]["kind"], "lesson")
        self.assertIn("extra care", result["memories"][0]["content"])
        self.assertEqual(mod.memory_status()["memories_total"], 1)

    def test_anamnesis_search_sanitizes_fts_syntax(self):
        os.environ["ANAMNESIS_DB"] = str(self.root / "memory.db")
        mod = _load(ANAMNESIS, f"anamnesis_syntax_{id(self)}")

        mod.memory_record("Friday deploys need extra care", kind="lesson", source="test")
        result = mod.memory_search("Friday: OR")

        self.assertTrue(result["ok"])
        self.assertEqual(result["memories"][0]["content"], "Friday deploys need extra care")

    def test_visual_identity_search_scrubs_outside_root_path(self):
        manifest = self.root / "visual-assets.json"
        manifest.write_text(
            json.dumps(
                {
                    "assets": [
                        {"id": "logo", "label": "Primary Logo", "kind": "logo", "tags": ["brand"], "path": "logo.png"},
                        {"id": "bad", "label": "Bad", "kind": "raw", "path": "../outside.png"},
                    ]
                }
            )
        )
        os.environ["VISUAL_IDENTITY_MANIFEST"] = str(manifest)
        os.environ["VISUAL_IDENTITY_ROOT"] = str(self.root)
        mod = _load(VISUAL, f"visual_{id(self)}")

        result = mod.visual_identity_search("logo")
        self.assertTrue(result["ok"])
        self.assertEqual(result["assets"][0]["id"], "logo")
        self.assertEqual(result["assets"][0]["path"], "logo.png")
        bad = mod.visual_identity_get("bad")
        self.assertIsNone(bad["asset"]["path"])

    def test_visual_identity_loads_top_level_list_manifest(self):
        manifest = self.root / "visual-assets.json"
        manifest.write_text(
            json.dumps(
                [
                    {"id": "logo", "label": "Primary Logo", "kind": "logo", "tags": ["brand"], "path": "logo.png"},
                    "ignored",
                ]
            )
        )
        os.environ["VISUAL_IDENTITY_MANIFEST"] = str(manifest)
        os.environ["VISUAL_IDENTITY_ROOT"] = str(self.root)
        mod = _load(VISUAL, f"visual_list_{id(self)}")

        result = mod.visual_identity_search("logo")

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["assets"],
            [{"id": "logo", "label": "Primary Logo", "kind": "logo", "tags": ["brand"], "path": "logo.png"}],
        )
        self.assertEqual(mod.visual_identity_status()["assets_total"], 1)

    def test_telegram_admin_lookup_returns_scrubbed_fields(self):
        directory = self.root / "telegram-directory.json"
        directory.write_text(
            json.dumps(
                {
                    "channels": [
                        {
                            "id": "client-main",
                            "name": "Client Main",
                            "purpose": "Delivery",
                            "message_thread_id": 123,
                            "bot_token": "must-not-return",
                        }
                    ]
                }
            )
        )
        os.environ["TELEGRAM_DIRECTORY"] = str(directory)
        mod = _load(TELEGRAM, f"telegram_{id(self)}")

        result = mod.telegram_admin_lookup("client")
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["message_thread_id"], 123)
        self.assertNotIn("bot_token", result["matches"][0])

    def test_telegram_admin_lookup_does_not_search_hidden_fields(self):
        directory = self.root / "telegram-directory.json"
        directory.write_text(
            json.dumps(
                {
                    "channels": [
                        {
                            "id": "client-main",
                            "name": "Client Main",
                            "purpose": "Delivery",
                            "message_thread_id": 123,
                            "bot_token": "must-not-query",
                        }
                    ]
                }
            )
        )
        os.environ["TELEGRAM_DIRECTORY"] = str(directory)
        mod = _load(TELEGRAM, f"telegram_hidden_{id(self)}")

        result = mod.telegram_admin_lookup("must-not-query")

        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
