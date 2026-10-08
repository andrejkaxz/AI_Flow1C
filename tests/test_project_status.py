"""Project-wide status must not resolve project or task references as work items."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flow1c import cli, storage
from flow1c.workflow import begin
from test_cli_contract import CliScenario

ROOT = Path(__file__).resolve().parents[1]


class ProjectStatusScopeTests(unittest.TestCase):
    def test_status_preserves_reference_context_without_loading_a_work_item(self) -> None:
        with tempfile.TemporaryDirectory(prefix="flow1c-status-") as temporary:
            base = Path(temporary).resolve()
            scenario = CliScenario(base)
            shutil.copy2(ROOT / ".flow1c.json", scenario.root / ".flow1c.json")
            docs = base / "documentation"
            docs.mkdir()
            storage.write_json(scenario.root / ".flow1c.local.json", {
                "schema_version": 2, "documentation_path": str(docs), "project_reference": "PROJECT-7"})
            for extra in ([], ["--task-reference", "TASK-9"]):
                with self.subTest(extra=extra):
                    with mock.patch.object(cli, "ROOT", scenario.root):
                        args = cli.build_parser().parse_args(["agent-begin", "--operation", "status", "--mode", "formal", "--summary", "Project status", *extra])
                    with mock.patch.object(begin.work_items, "load_manifest", side_effect=AssertionError("Status must not load work-item")):
                        gate = begin.agent_begin(args, product_root=scenario.root).value
                    self.assertEqual(gate["state"], "READY")
                    self.assertIsNone(gate["code"])
                    self.assertIsNone(gate["work_reference"])
                    self.assertEqual(gate["project_reference"], "PROJECT-7")
                    self.assertEqual(gate["status_scope"]["work_reference"], "TASK-9" if extra else "PROJECT-7")


if __name__ == "__main__":
    unittest.main()
