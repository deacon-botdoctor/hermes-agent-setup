from __future__ import annotations

import importlib
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
    def test_floor_plugins_register_without_runtime_apis(self):
        modules = [
            "plugins.autodream",
            "plugins.composio_onboarding",
            "plugins.hermes_lcm",
            "plugins.task_ledger",
            "plugins.telegram_transcript",
        ]
        for module_name in modules:
            with self.subTest(module_name=module_name):
                module = importlib.import_module(module_name)
                result = module.register(_Context())
                self.assertEqual(result["status"], "placeholder")
                self.assertIn("plugin", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
