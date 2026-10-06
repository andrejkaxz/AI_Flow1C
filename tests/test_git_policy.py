from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from scripts.flow1c_git_policy import (migrate_record, select_final_merge,
                                       semantic_fingerprint, validate_transition)


class GitPolicyTests(unittest.TestCase):
    def test_final_merge_prefers_strong_evidence_then_latest_position(self) -> None:
        selected, ambiguous = select_final_merge([
            {"commit": "a" * 40, "evidence_level": "EXACT_TOPOLOGY", "target_position": 1},
            {"commit": "b" * 40, "evidence_level": "EXACT_TOPOLOGY", "target_position": 8},
            {"commit": "c" * 40, "evidence_level": "HEURISTIC_CANDIDATE", "target_position": 99},
        ])
        self.assertEqual(selected["commit"], "b" * 40)
        self.assertFalse(ambiguous)

    def test_fingerprint_ignores_output_limits_and_order_of_ref_set(self) -> None:
        left = semantic_fingerprint({"action": "merge-search", "refs": ["b", "a"], "max_chars": 1})
        right = semantic_fingerprint({"refs": ["a", "b"], "action": "merge-search", "max_chars": 999})
        self.assertEqual(left, right)

    def test_terminal_state_rejects_latest_merge_and_duplicate_search(self) -> None:
        state = {"cache": {}, "terminal": {"merge-search": {"evidence_id": "GIT-001"}}}
        with self.assertRaisesRegex(ValueError, "INVALID_NEXT_ACTION"):
            validate_transition(state, "latest-merge", "new")
        with self.assertRaisesRegex(ValueError, "ALREADY_RESOLVED"):
            validate_transition(state, "merge-search", "new")

    def test_legacy_not_merged_migrates_without_losing_status(self) -> None:
        migrated = migrate_record({"schema_version": 1, "status": "NOT_MERGED"})
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["integration_status"], "NO_INTEGRATION_EVIDENCE")
        self.assertEqual(migrated["legacy_status"], "NOT_MERGED")

    def test_schema_is_valid_json_and_requires_v2(self) -> None:
        schema = json.loads((ROOT / "schemas" / "git-analysis.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 2)


if __name__ == "__main__":
    unittest.main()
