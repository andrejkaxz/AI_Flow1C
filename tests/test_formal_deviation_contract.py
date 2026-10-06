from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FormalDeviationContractTests(unittest.TestCase):
    def test_opencode_exposes_typed_deviation_and_provisional_actions(self):
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")
        self.assertIn("deviation_type", adapter)
        self.assertIn("condition_ids", adapter)
        self.assertIn("user_statement", adapter)
        self.assertIn('"provisional-start"', adapter)
        self.assertIn('"registry-reconcile"', adapter)

    def test_document_stages_declare_deviation_policy(self):
        stages = json.loads((ROOT / "config" / "stages.json").read_text(encoding="utf-8"))
        document_stages = {"functional-spec", "functional-review", "technical-design", "technical-implementation", "code-review", "testing"}
        for operation in document_stages:
            policy = stages["operations"][operation]["deviation_policy"]
            self.assertTrue(policy["allow_formal_documents"], operation)
        for operation in ("development", "publish"):
            self.assertFalse(stages["operations"][operation]["deviation_policy"]["allow_formal_documents"])

    def test_controller_requires_tool_call_for_explicit_formal_deviation(self):
        controller = (ROOT / ".opencode" / "agents" / "flow1c-controller.md").read_text(encoding="utf-8")
        self.assertIn("обязательно вызови `flow1c_dialogue action=deviate`", controller)
        self.assertIn("provisional-start", controller)
        self.assertIn("не вызывает mutation tool", controller)


if __name__ == "__main__":
    unittest.main()
