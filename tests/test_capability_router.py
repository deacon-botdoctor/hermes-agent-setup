from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "mcp-servers/capability-router/src/capability_router/server.py"
DEFAULT_REGISTRY = REPO / "mcp-servers/capability-router/registry.json"


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


class CapabilityRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cap-router-"))
        self.registry = self.tmp / "registry.json"
        self.usage = self.tmp / "usage.db"
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "categories": [{"id": "test", "label": "Test"}],
                    "capabilities": [
                        {
                            "id": "test.alpha",
                            "category": "test",
                            "label": "Alpha calendar tool",
                            "summary": "Calendar scheduling",
                            "mcp_server": "alpha",
                            "tool_name": "run",
                        },
                        {
                            "id": "test.beta",
                            "category": "test",
                            "label": "Beta calendar tool",
                            "summary": "Calendar scheduling",
                            "mcp_server": "beta",
                            "tool_name": "run",
                        },
                        {
                            "id": "pattern.durable-work",
                            "kind": "operating_pattern",
                            "category": "test",
                            "label": "Durable work queue",
                            "summary": "Queue long running work instead of live chat.",
                            "preferred_for": ["durable job", "queue long running work"],
                            "routing_policy": {"default_lane": "durable-workload"},
                        },
                    ],
                }
            )
        )
        self.old_env = {
            "CAPABILITY_REGISTRY": os.environ.get("CAPABILITY_REGISTRY"),
            "CAPABILITY_USAGE_DB": os.environ.get("CAPABILITY_USAGE_DB"),
        }
        os.environ["CAPABILITY_REGISTRY"] = str(self.registry)
        os.environ["CAPABILITY_USAGE_DB"] = str(self.usage)
        _install_mcp_stub()
        spec = importlib.util.spec_from_file_location(f"router_{id(self)}", SERVER)
        self.router = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(self.router)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_search_and_describe_catalog_entry(self):
        result = self.router.search_capabilities("queue durable long running job")
        hit = next(item for item in result["hits"] if item["id"] == "pattern.durable-work")
        self.assertEqual(hit["availability"], "catalog")
        self.assertTrue(hit["can_invoke_now"])

        detail = self.router.describe_capability("pattern.durable-work")
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["capability"]["routing_policy"]["default_lane"], "durable-workload")

    def test_recorded_failures_demote_search_result(self):
        before = self.router.search_capabilities("calendar", max_hits=2)
        self.assertEqual([item["id"] for item in before["hits"]], ["test.alpha", "test.beta"])

        for _ in range(3):
            self.router.record_capability_outcome("test.alpha", outcome="failure")

        after = self.router.search_capabilities("calendar", max_hits=2)
        self.assertEqual([item["id"] for item in after["hits"]], ["test.beta", "test.alpha"])
        alpha = next(item for item in after["hits"] if item["id"] == "test.alpha")
        self.assertEqual(alpha["score_breakdown"]["usage"], -6)

    def test_successes_recover_usage_score(self):
        self.router.record_capability_outcome("test.alpha", ok=False)
        self.router.record_capability_outcome("test.alpha", ok=True)
        self.router.record_capability_outcome("test.alpha", ok=True)
        status = self.router.registry_status()
        self.assertEqual(status["usage_records_total"], 3)
        self.assertEqual(status["usage_records_failure"], 1)
        self.assertEqual(status["usage_records_success"], 2)
        self.assertEqual(status["usage_by_capability"][0]["usage_score"], 0)

    def test_missing_outcome_does_not_record_usage(self):
        result = self.router.record_capability_outcome("test.alpha")
        self.assertFalse(result["ok"])
        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "missing_outcome")
        self.assertEqual(self.router.registry_status()["usage_records_total"], 0)

    def test_storage_error_is_fail_soft(self):
        self.router.USAGE_DB_PATH = self.tmp / "not-dir" / "usage.db"
        self.router.USAGE_DB_PATH.parent.write_text("blocking file")
        result = self.router.record_capability_outcome("test.alpha", ok=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["recorded"])
        self.assertIn("reason", result)

    def test_default_registry_keeps_browser_mcps_distinct(self):
        registry = json.loads(DEFAULT_REGISTRY.read_text())
        by_id = {capability["id"]: capability for capability in registry["capabilities"]}
        self.assertEqual(by_id["web.browser"]["mcp_server"], "browser")
        self.assertEqual(by_id["web.browser-lane"]["mcp_server"], "browser-lane")


if __name__ == "__main__":
    unittest.main(verbosity=2)
