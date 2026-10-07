"""Deterministic context selection and coverage cannot perform I/O."""

import unittest
from unittest import mock
from pathlib import Path

from flow1c import context_policy as policy


class ContextPolicyTests(unittest.TestCase):
    def test_policy_selection_identity_and_coverage_are_pure(self):
        with mock.patch.object(Path, "open", side_effect=AssertionError("I/O")):
            specs = policy.source_specs({"operation": "functional-review", "mode": "formal", "context_role": "functional-architect"}, {}, [])
            self.assertTrue(all(s["required"] for s in specs))
            self.assertEqual(len(specs), 3)
            self.assertEqual(policy.digest(specs), policy.digest(specs))
            self.assertEqual(policy.parts(16001, 8000, True)[-1]["end"], 16001)
            ranges = policy.merge_ranges([[0, 4000], [8000, 9000]], 4000, 8000)
            self.assertEqual(ranges, [[0, 9000]])

    def test_review_parts_require_complete_original_ranges(self):
        entry = {"entry_id": "source", "required": True, "readable": True, "parts": policy.parts(16001, 8000, True)}
        manifest = {"entries": [entry]}
        coverage = {"ranges": {"source": [[0, 7999], [8000, 16001]]}}
        self.assertEqual(policy.unread_parts(manifest, coverage), ["source:part-00001"])
        coverage["ranges"]["source"] = [[0, 16001]]
        self.assertTrue(policy.coverage_summary(manifest, coverage)["complete"])
        entry.update(readable=False, parts=[])
        self.assertFalse(policy.coverage_summary(manifest, coverage)["complete"])

    def test_free_review_requires_accepted_spec_and_does_not_invent_work_item(self):
        gate = {"operation": "functional-review", "mode": "explore"}
        specs = policy.source_specs(gate, {}, [])
        self.assertEqual(len(specs), 1)
        self.assertTrue(specs[0]["required"])
        specs = policy.source_specs(gate, {}, [{"path": "input/example.docx", "derived_path": "input/example.docx.md", "sha256": "a", "derived_sha256": "b"}])
        self.assertEqual(specs[0]["original_path"], "input/example.docx")
        self.assertEqual(specs[0]["accepted_sha256"], "b")

    def test_limits_reject_booleans_unknown_version_and_unbounded_values(self):
        limits = {"schema_version": 1, "compact_chars": 12000, "page_chars": 8000, "response_chars": 24000, "max_source_bytes": 10485760, "max_entries": 1000}
        for change in ({"schema_version": True}, {"schema_version": 2}, {"page_chars": 8001}, {"max_entries": 0}, {"compact_chars": 100}):
            with self.subTest(change=change), self.assertRaises(policy.ContextError):
                policy.validate_limits({**limits, **change})

    def test_formal_registry_workbook_is_covered_by_normalized_requirements_basis(self):
        gate = {"operation": "functional-review", "mode": "formal", "context_role": "functional-architect"}
        specs = policy.source_specs(gate, {}, [{"category": "requirements_workbook", "relative_path": "inbox/synthetic/originals/registry.xlsx"}])
        self.assertEqual(len(specs), 3)
        self.assertIn("input/requirements.snapshot.yaml", {s["path"] for s in specs})
        self.assertTrue(all(s["required"] for s in specs))

    def test_compact_large_metadata_and_index_are_explicitly_abbreviated(self):
        entries = [{"entry_id": f"source-{i}", "path": "input/" + "x" * 200, "reason": "accepted artifact", "required": False, "readable": True, "length": 100, "parts": []} for i in range(100)]
        manifest = {"entries": entries}
        scope = {"goal": "цель" * 10000, "decisions": ["решение" * 10000]}
        text = policy.compact_text(manifest, scope, {"ranges": {}}, 12000)
        self.assertLessEqual(len(text), 12000)
        self.assertIn("abbreviated; read scope", text)
        self.assertIn("entries omitted", text)
