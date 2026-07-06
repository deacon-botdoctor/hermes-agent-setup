from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


class _Context:
    def __init__(self):
        self.logger = _Logger()


class PluginFloorTests(unittest.TestCase):
    MODULES = [
        "autodream",
        "composio_onboarding",
        "hermes_lcm",
        "task_ledger",
        "telegram_transcript",
    ]

    def test_floor_plugins_register_without_runtime_apis(self):
        for module_name in self.MODULES:
            with self.subTest(module_name=module_name):
                module = importlib.import_module(f"plugins.{module_name}")
                result = module.register(_Context())
                self.assertEqual(result["status"], "placeholder")
                self.assertIn("plugin", result)

    def test_floor_plugins_import_from_copied_plugin_directory(self):
        repo_root = Path(__file__).resolve().parents[1]
        plugins_dir = repo_root / "plugins"
        script = """
import importlib
import json
import sys

plugins_dir = sys.argv[1]
repo_root = sys.argv[2]
modules = json.loads(sys.argv[3])
sys.path = [plugins_dir] + [
    path for path in sys.path
    if path not in ("", plugins_dir, repo_root)
]

class Logger:
    def info(self, message):
        pass

class Context:
    logger = Logger()

for module_name in modules:
    module = importlib.import_module(module_name)
    result = module.register(Context())
    assert result["status"] == "placeholder", result
"""
        subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(plugins_dir),
                str(repo_root),
                json.dumps(self.MODULES),
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
