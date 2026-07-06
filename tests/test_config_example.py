from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO / "config/config.example.yaml"


class ExampleConfigTests(unittest.TestCase):
    def test_human_delay_mode_stays_string_after_yaml_parse(self):
        config = yaml.safe_load(EXAMPLE_CONFIG.read_text())

        self.assertEqual(config["human_delay"]["mode"], "off")
        self.assertIsInstance(config["human_delay"]["mode"], str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
