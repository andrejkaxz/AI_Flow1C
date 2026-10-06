from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import cc_query_inspector as inspector


class CcQueryInspectorTests(unittest.TestCase):
    def test_rejects_absolute_and_parent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for unsafe in (str((root / "x.xml").resolve()), "../x.xml"):
                with self.assertRaises(inspector.CcInspectionError):
                    inspector.resolve_input_path(
                        source="request", relative_path=unsafe, request_root=root,
                        request_artifacts=[], configuration_root=None, extension_root=None,
                    )

    def test_request_xml_must_belong_to_current_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "other.xml").write_text("<x/>", encoding="utf-8")
            with self.assertRaisesRegex(inspector.CcInspectionError, "accepted artifact"):
                inspector.resolve_input_path(
                    source="request", relative_path="other.xml", request_root=root,
                    request_artifacts=[], configuration_root=None, extension_root=None,
                )

    def test_skd_is_limited_to_template_xml_and_meta_excludes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "Object.xml"
            template = root / "Template.xml"
            ordinary.write_text("<MetaDataObject/>", encoding="utf-8")
            template.write_text("<DataCompositionSchema/>", encoding="utf-8")
            with self.assertRaisesRegex(inspector.CcInspectionError, "Template.xml"):
                inspector.validate_operation("skd-full", ordinary)
            with self.assertRaisesRegex(inspector.CcInspectionError, "skd-info"):
                inspector.validate_operation("meta-full", template)

    def test_only_fixed_operations_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Object.xml"
            target.write_text("<MetaDataObject/>", encoding="utf-8")
            with self.assertRaisesRegex(inspector.CcInspectionError, "unsupported"):
                inspector.validate_operation("run-command", target)

    def test_runner_enforces_timeout_and_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / ".claude/skills/meta-info/scripts/meta-info.ps1"
            script.parent.mkdir(parents=True)
            script.write_text("# fixture", encoding="utf-8")
            target = root / "Object.xml"
            target.write_text("<MetaDataObject/>", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="x" * 50, stderr="")
            with mock.patch.object(inspector.subprocess, "run", return_value=completed) as run:
                result = inspector.run_inspection(
                    checkout=root, operation="meta-overview", target=target, max_chars=10,
                )
            self.assertEqual(result["output"], "x" * 10)
            self.assertTrue(result["truncated"])
            self.assertEqual(run.call_args.kwargs["timeout"], inspector.TIMEOUT_SECONDS)
            command = run.call_args.args[0]
            self.assertIn("-NoProfile", command)
            self.assertIn("-NonInteractive", command)
            self.assertEqual(command[command.index("-File") + 1], str(script.resolve()))

            with mock.patch.object(inspector.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 30)):
                with self.assertRaisesRegex(inspector.CcInspectionUnavailable, "timeout"):
                    inspector.run_inspection(checkout=root, operation="meta-overview", target=target)

    def test_missing_cc_skills_is_a_recoverable_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "Object.xml"
            target.write_text("<MetaDataObject/>", encoding="utf-8")
            with self.assertRaises(inspector.CcInspectionUnavailable):
                inspector.run_inspection(checkout=root, operation="meta-overview", target=target)


if __name__ == "__main__":
    unittest.main()
