from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
SEARCH_SERVER = REPO / "mcp-servers/search/src/search_mcp/server.py"
WEB_SEARCH_SERVER = REPO / "mcp-servers/web-search/src/web_search_mcp/server.py"


class _FastMCPStub:
    def __init__(self, name: str):
        self.name = name

    def tool(self):
        def decorator(fn):
            return fn

        return decorator

    def run(self):
        return None


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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


class SearchMcpTests(unittest.TestCase):
    def setUp(self):
        self.old_env = {key: os.environ.get(key) for key in ["SEARXNG_URL", "FIRECRAWL_URL", "FIRECRAWL_API_KEY"]}
        os.environ["SEARXNG_URL"] = "http://searxng.local"
        os.environ["FIRECRAWL_URL"] = "http://firecrawl.local"
        os.environ.pop("FIRECRAWL_API_KEY", None)
        self.search_mod = _load(SEARCH_SERVER, f"search_mcp_{id(self)}")
        self.web_mod = _load(WEB_SEARCH_SERVER, f"web_search_mcp_{id(self)}")

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_search_normalizes_searxng_results(self):
        payload = {
            "results": [
                {"title": "One", "url": "https://example.com/1", "content": "First", "engine": "test"},
                {"title": "Two", "url": "https://example.com/2", "content": "Second", "engine": "test"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            result = self.search_mod.search("alpha beta", max_results=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [{"title": "One", "url": "https://example.com/1", "snippet": "First", "source": "test"}])
        request = urlopen.call_args.args[0]
        self.assertIn("/search?", request.full_url)
        self.assertIn("q=alpha+beta", request.full_url)

    def test_web_search_kept_as_distinct_tool_surface(self):
        with patch("urllib.request.urlopen", return_value=_Response({"results": [{"title": "Doc", "link": "https://docs.example"}]})):
            result = self.web_mod.web_search("docs")

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "searxng")
        self.assertEqual(result["results"][0]["url"], "https://docs.example")

    def test_scrape_uses_firecrawl_payload(self):
        payload = {"data": {"markdown": "# Title", "metadata": {"title": "Title"}}}
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            result = self.search_mod.scrape_url("https://example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["markdown"], "# Title")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://firecrawl.local/v1/scrape")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["url"], "https://example.com")
        self.assertEqual(body["formats"], ["markdown"])

    def test_errors_are_fail_soft(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            result = self.web_mod.scrape("https://example.com")

        self.assertFalse(result["ok"])
        self.assertEqual(result["backend"], "firecrawl")
        self.assertIn("offline", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
