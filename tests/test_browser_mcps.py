from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
BROWSER_SERVER = REPO / "mcp-servers/browser/src/browser_mcp/server.py"
BROWSER_LANE_SERVER = REPO / "mcp-servers/browser-lane/src/browser_lane_mcp/server.py"


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


class _Response:
    def __init__(self, payload):
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


class BrowserMcpTests(unittest.TestCase):
    def setUp(self):
        self.old_env = {key: os.environ.get(key) for key in ["BROWSER_CDP_URL", "BROWSER_LANE_SOCKET"]}
        os.environ["BROWSER_CDP_URL"] = "http://cdp.local:9230"

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_browser_status_reads_cdp_version(self):
        browser = _load(BROWSER_SERVER, f"browser_mcp_{id(self)}")
        payload = {"Browser": "HeadlessChrome/test", "webSocketDebuggerUrl": "ws://cdp/devtools/browser/1"}
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            result = browser.browser_status()

        self.assertTrue(result["ok"])
        self.assertEqual(result["version"]["Browser"], "HeadlessChrome/test")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://cdp.local:9230/json/version")
        self.assertEqual(request.get_method(), "GET")

    def test_browser_open_is_registered_and_opens_new_target(self):
        browser = _load(BROWSER_SERVER, f"browser_mcp_open_{id(self)}")
        payload = {"id": "target-1", "url": "https://example.com/a b"}
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as urlopen:
            result = browser.browser_open("https://example.com/a b")

        self.assertIn("browser_open", browser.mcp.tools)
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/json/new?https%3A%2F%2Fexample.com%2Fa%20b")
        self.assertEqual(result["data"]["id"], "target-1")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://cdp.local:9230/json/new?https%3A%2F%2Fexample.com%2Fa%20b")
        self.assertEqual(request.get_method(), "PUT")

    def test_list_targets_fail_soft(self):
        browser = _load(BROWSER_SERVER, f"browser_mcp_fail_{id(self)}")
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            result = browser.list_targets()

        self.assertFalse(result["ok"])
        self.assertEqual(result["targets"], [])
        self.assertIn("offline", result["error"])

    def test_browser_lane_socket_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "daemon.sock"
            os.environ["BROWSER_LANE_SOCKET"] = str(socket_path)
            lane = _load(BROWSER_LANE_SERVER, f"browser_lane_mcp_{id(self)}")

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)

            def serve_once():
                conn, _ = server.accept()
                with conn:
                    request = conn.recv(65536)
                    self.assertIn(b'"command": "open"', request)
                    conn.sendall(json.dumps({"ok": True, "page_id": "p1"}).encode("utf-8") + b"\n")
                server.close()

            thread = threading.Thread(target=serve_once)
            thread.start()
            result = lane.browser_lane_command("open", url="https://example.com")
            thread.join(timeout=2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["page_id"], "p1")

    def test_browser_lane_open_is_registered_and_delegates_to_open_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "daemon.sock"
            os.environ["BROWSER_LANE_SOCKET"] = str(socket_path)
            lane = _load(BROWSER_LANE_SERVER, f"browser_lane_open_{id(self)}")

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)

            def serve_once():
                conn, _ = server.accept()
                with conn:
                    request = json.loads(conn.recv(65536).decode("utf-8"))
                    self.assertEqual(request["command"], "open")
                    self.assertEqual(request["params"]["url"], "https://example.com")
                    conn.sendall(json.dumps({"ok": True, "page_id": "p2"}).encode("utf-8") + b"\n")
                server.close()

            thread = threading.Thread(target=serve_once)
            thread.start()
            result = lane.browser_lane_open("https://example.com")
            thread.join(timeout=2)

        self.assertIn("browser_lane_open", lane.mcp.tools)
        self.assertTrue(result["ok"])
        self.assertEqual(result["page_id"], "p2")

    def test_browser_lane_status_missing_socket(self):
        os.environ["BROWSER_LANE_SOCKET"] = "/tmp/missing-browser-lane.sock"
        lane = _load(BROWSER_LANE_SERVER, f"browser_lane_missing_{id(self)}")
        result = lane.browser_lane_status()
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
