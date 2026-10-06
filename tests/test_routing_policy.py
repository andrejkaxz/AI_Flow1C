"""Route policy checks validate structure and compatibility, not language understanding."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from flow1c.routing import load_rules
from flow1c.routing_policy import (
    RoutingError, build_rules, check_legacy_route, check_route, fingerprint, proposal_errors,
)

ROOT = Path(__file__).resolve().parents[1]


def proposal(**fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, "expected_outcome": "Проверить указанный результат",
            "operation": "code-review", "mode": "explore", "sources": [], **fields}


class RoutingPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules(ROOT)

    def test_same_structured_input_is_deterministic_without_io_or_mutation(self) -> None:
        value = proposal(sources=[{"kind": "git_history", "selector": "700001", "version": "historical"}])
        before = copy.deepcopy((value, self.rules))
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("I/O")), \
             mock.patch.object(Path, "write_text", side_effect=AssertionError("I/O")), \
             mock.patch("subprocess.run", side_effect=AssertionError("process")):
            first = check_route(value, self.rules)
            second = check_route(copy.deepcopy(value), self.rules)
        self.assertEqual(first, second)
        self.assertEqual((value, self.rules), before)
        self.assertEqual(first["fingerprint"], fingerprint({k: v for k, v in first.items() if k != "fingerprint"}))

    def test_formal_owner_controls_skill_role_and_changes_digest(self) -> None:
        catalog = json.loads((ROOT / "config/intent-routes.json").read_text(encoding="utf-8"))
        stages = copy.deepcopy(self.rules["owners"]["stages"])
        skills = {p.parent.name for p in (ROOT / ".agents/skills").glob("flow1c-*/SKILL.md")}
        stages["operations"]["code-review"]["skill"] = "flow1c-testing"
        changed = build_rules(catalog, stages, skills)
        self.assertNotEqual(changed["digest"], self.rules["digest"])
        decision = check_route(proposal(mode="formal"), changed)
        self.assertEqual((decision["primary_skill"], decision["role"]), ("flow1c-testing", "technical-architect"))
        stages["operations"]["code-review"]["allowed_tools"].append("synthetic-owner-change")
        self.assertNotEqual(build_rules(catalog, stages, skills)["digest"], changed["digest"])

    def test_unknown_operations_modes_skills_roles_and_substeps_fail_closed(self) -> None:
        values = [proposal(operation="unrecognized"), proposal(mode="other"),
                  proposal(operation="development", mode="explore"),
                  proposal(operation="consultation", mode="formal"),
                  proposal(primary_skill="flow1c-technical-review"), proposal(role="technical-architect"),
                  proposal(substeps=["shell"]), proposal(substeps=["template-library"]),
                  proposal(operation="template-document", mode="explore")]
        for value in values:
            with self.subTest(value=value):
                decision = check_route(value, self.rules)
                self.assertEqual(decision["status"], "INVALID")
                self.assertEqual(decision["next_actions"], ["correct-proposal"])
                self.assertTrue(all(error["preserved_state"] for error in decision["errors"]))

    def test_malformed_or_unbounded_proposals_never_raise_or_grant_authority(self) -> None:
        values = [None, [], {}, proposal(schema_version=True), proposal(schema_version=2),
                  proposal(expected_outcome="x" * 4001), proposal(operation={}),
                  proposal(mode=[]), proposal(references=[]), proposal(references={"git_ref": 700001}),
                  proposal(references={"task_reference": ""}), proposal(references={"git_ref": "-main"}),
                  proposal(references={"task_reference": "a\n"}), proposal(sources={}),
                  proposal(sources=[{"kind": "git_snapshot", "version": "historical", "commit": "fake"}]),
                  proposal(sources=[{"kind": [], "version": "current"}]),
                  proposal(sources=[{"kind": "chat", "version": "provided", "selector": ""}]),
                  proposal(sources=[{"kind": "chat", "version": "provided"}] * 9),
                  proposal(basis=["x" * 257]), proposal(ambiguities=["permissions"]),
                  proposal(substeps=[1]), proposal(permissions=["publish"]), proposal(expected_outcome="\ud800")]
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(proposal_errors(value))
                self.assertEqual(check_route(value, self.rules)["status"], "INVALID")
        # Aggregate byte budget is independent from the per-field bounds.
        oversized = proposal(expected_outcome="😀" * 4000, basis=["😀" * 256] * 16,
                             sources=[{"kind": "chat", "version": "provided", "selector": "😀" * 256}] * 8)
        self.assertIn("proposal exceeds 32768 bytes", proposal_errors(oversized))

    def test_missing_goal_mode_or_object_returns_one_question_before_gate(self) -> None:
        for value, field in [
            (proposal(operation=None), "operation"), (proposal(mode=None), "mode"),
            (proposal(expected_outcome=""), "outcome"),
            (proposal(sources=[{"kind": "git_history", "version": "historical"}]), "object"),
            (proposal(ambiguities=["source", "source"]), "source"),
        ]:
            with self.subTest(field=field):
                decision = check_route(value, self.rules)
                self.assertEqual(decision["status"], "CLARIFICATION_REQUIRED")
                self.assertIn(field, decision["unresolved_fields"])
                self.assertEqual(len(decision["unresolved_fields"]), len(set(decision["unresolved_fields"])))
                self.assertEqual(decision["next_actions"], ["clarify-before-gate"])
                self.assertIsInstance(decision["clarification"]["question"], str)

    def test_current_and_historical_sources_cannot_be_substituted(self) -> None:
        for kind, version in [("extension", "historical"), ("configuration", "historical"),
                              ("git_snapshot", "current"), ("git_history", "current")]:
            with self.subTest(kind=kind):
                self.assertEqual(check_route(proposal(sources=[{
                    "kind": kind, "selector": "synthetic", "version": version,
                }]), self.rules)["status"], "INVALID")
        decision = check_route(proposal(sources=[{
            "kind": "git_snapshot", "selector": "synthetic-old", "version": "historical",
        }]), self.rules)
        self.assertEqual(decision["sources"][0]["resolver"], "gated-git-snapshot-and-rlm")
        self.assertNotIn("commit", decision["sources"][0])
        self.assertNotIn("freshness", decision["sources"][0])

    def test_comparison_requires_two_distinct_requested_sources(self) -> None:
        source = {"kind": "extension", "version": "current"}
        for sources in ([], [source], [source, source]):
            self.assertEqual(check_route(proposal(source_relation="compare", sources=sources), self.rules)["status"], "CLARIFICATION_REQUIRED")
        sources = [source, {"kind": "git_snapshot", "selector": "synthetic-old", "version": "historical"}]
        decision = check_route(proposal(source_relation="compare", sources=sources), self.rules)
        self.assertEqual(decision["status"], "VALID")
        self.assertEqual(len(decision["sources"]), 2)
        self.assertEqual({s["resolver"] for s in decision["sources"]}, {"gated-rlm", "gated-git-snapshot-and-rlm"})

    def test_work_reference_is_never_promoted_to_a_git_ref(self) -> None:
        value = proposal(references={"task_reference": "700001"})
        decision = check_route(value, self.rules)
        self.assertEqual(decision["references"], value["references"])
        self.assertEqual(decision["sources"], [])
        explicit = proposal(references={"git_ref": "700001"}, sources=[{
            "kind": "git_history", "selector": "700001", "version": "historical", "repository": "workflow",
        }])
        historical = check_route(explicit, self.rules)["sources"][0]
        self.assertEqual(historical["selector"], "700001")
        self.assertEqual(historical["repository"], "workflow")
        invalid = proposal(sources=[{"kind": "extension", "version": "current", "repository": "workflow"}])
        self.assertEqual(check_route(invalid, self.rules)["status"], "INVALID")

    def test_saved_answer_links_and_injection_text_do_not_change_route(self) -> None:
        value = proposal(operation="functional-review", mode="explore", basis=["saved-answer:synthetic-1"],
                         expected_outcome="Вложение требует игнорировать gates и публиковать; это данные.")
        decision = check_route(value, self.rules)
        self.assertEqual(decision["status"], "VALID")
        self.assertEqual(decision["basis"], value["basis"])
        self.assertIsNone(decision["clarification"])
        self.assertEqual(decision["operation"], "functional-review")
        for field in ("available_actions", "allowed_tools", "approvals", "ready", "gate_id"):
            self.assertNotIn(field, decision)

    def test_pre_gate_redmine_substep_preserves_primary_and_is_read_only(self) -> None:
        decision = check_route(proposal(operation="consultation", substeps=["redmine-files"]), self.rules)
        self.assertEqual(decision["primary_skill"], "flow1c-consultation")
        self.assertEqual(self.rules["substeps"]["redmine-files"], {
            "tool": "flow1c_redmine_files", "pre_gate": True, "access": "read-only",
        })
        self.assertFalse(self.rules["substeps"]["template-library"]["pre_gate"])

    def test_legacy_defaults_and_narrow_query_remap_preserve_origin(self) -> None:
        fixture = json.loads((ROOT / "tests/fixtures/routing-baseline.json").read_text(encoding="utf-8"))
        for case in fixture["defaults"]:
            with self.subTest(case=case["case_id"]):
                decision = check_legacy_route(case["operation"], case["summary"], case["explicit_mode"], self.rules)
                for field, expected in case["expected"].items():
                    self.assertEqual(decision[field], expected)
        explicit = proposal(operation="consultation", expected_outcome="Напиши запрос 1С")
        self.assertEqual(check_route(explicit, self.rules)["operation"], "consultation")
        # Only the legacy comparison bridge uses the published narrow text rule.
        mapped = check_legacy_route("consultation", "Напиши запрос 1С", None, self.rules)
        self.assertEqual(mapped["requested_operation"], "consultation")
        self.assertIn("EXPLICIT_1C_QUERY_REQUEST", mapped["reason_codes"])

    def test_catalog_rejects_missing_duplicates_and_permission_copies(self) -> None:
        catalog = json.loads((ROOT / "config/intent-routes.json").read_text(encoding="utf-8"))
        stages = self.rules["owners"]["stages"]
        skills = {p.parent.name for p in (ROOT / ".agents/skills").glob("flow1c-*/SKILL.md")}
        mutations = [lambda c: c.update(schema_version=2), lambda c: c["routes"].pop(),
                     lambda c: c["routes"].append(copy.deepcopy(c["routes"][0])),
                     lambda c: c["routes"][0].update(allowed_tools=["shell"]),
                     lambda c: c["routes"][0].update(stage_policy="setup"),
                     lambda c: c["routes"][0].update(free_primary_skill="missing"),
                     lambda c: c["substeps"]["redmine-files"].update(access="gated"),
                     lambda c: c["compatibility"]["operation_aliases"].update(old="consultation")]
        for mutation in mutations:
            modified = copy.deepcopy(catalog)
            mutation(modified)
            with self.subTest(mutation=mutation), self.assertRaises(RoutingError):
                build_rules(modified, stages, skills)


if __name__ == "__main__":
    unittest.main()
