"""Catalog links, independent annotations, schemas and legacy behavior comparison."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from jsonschema import Draft202012Validator

from flow1c.routing import check_proposal, load_rules
from flow1c.routing_policy import RoutingError, check_route, legacy_proposal, proposal_errors
from flow1c.workflow import state as gate_state
from scripts import flow1c_policy as policy
from scripts import flow1c_query_policy as query_policy

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> Any:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class RoutingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules(ROOT)
        cls.catalog = read("config/intent-routes.json")
        cls.corpus = read("evals/routing-cases.json")
        cls.proposal_validator = Draft202012Validator(read("schemas/route-proposal.schema.json"))
        cls.decision_validator = Draft202012Validator(read("schemas/route-decision.schema.json"))

    def test_schemas_are_valid_and_catalog_references_exist(self) -> None:
        for name in ("intent-routes", "route-proposal", "route-decision"):
            Draft202012Validator.check_schema(read(f"schemas/{name}.schema.json"))
        Draft202012Validator(read("schemas/intent-routes.schema.json")).validate(self.catalog)
        operations = read("config/stages.json")["operations"]
        self.assertEqual({r["operation"] for r in self.catalog["routes"]}, set(operations))
        for route in self.catalog["routes"]:
            self.assertTrue((ROOT / route["output_contract"]).is_file(), route["output_contract"])
            self.assertEqual(route["stage_policy"], route["operation"])
            self.assertFalse(set(route) & {"allowed_tools", "writable_targets", "required_files", "required_approvals"})

    def test_corpus_has_positive_and_neighbor_cases_for_every_operation(self) -> None:
        cases = self.corpus["cases"]
        self.assertGreaterEqual(len(cases), 60)
        self.assertEqual(len({c["case_id"] for c in cases}), len(cases))
        self.assertTrue(self.corpus["synthetic"])
        self.assertEqual(self.corpus["baseline_commit"], "68b001a")
        for operation in self.rules["routes"]:
            related = [c for c in cases if c["expected"]["operation"] == operation]
            self.assertGreaterEqual(len(related), 3, operation)
            self.assertTrue(all(c["rejected_routes"] for c in related))
            self.assertTrue(any(c["split"] == "held-out" for c in related))
        for case in cases:
            self.assertTrue(case["prompt"].strip())
            self.assertIn(case["expected_outcome"], {"gate-checks-required", "clarify-before-gate"})
        published_examples = {text for route in self.catalog["routes"]
                              for field in ("activation", "boundary_examples") for text in route[field]}
        self.assertFalse({c["prompt"] for c in cases if c["split"] == "held-out"} & published_examples)

    def test_annotated_structured_routes_match_policy_and_schemas(self) -> None:
        for case in self.corpus["cases"]:
            expected = case["expected"]
            proposal = {"schema_version": 1, "expected_outcome": case["prompt"],
                        "operation": expected["operation"], "mode": expected["mode"],
                        "sources": expected["sources"], "ambiguities": case.get("ambiguities", []),
                        "source_relation": expected["source_relation"],
                        "substeps": case["allowed_substeps"],
                        "basis": [f"answer:{index}" for index, _ in enumerate(case["answers"])]}
            if case["case_id"] == "numeric-work-reference":
                proposal["references"] = {"task_reference": "700001"}
            with self.subTest(case=case["case_id"]):
                self.proposal_validator.validate(proposal)
                self.assertEqual(proposal_errors(proposal), [])
                decision = check_route(proposal, self.rules)
                self.decision_validator.validate(decision)
                self.assertEqual(decision["status"], expected["status"])
                self.assertEqual(decision["source_relation"], expected["source_relation"])
                if expected["operation"]:
                    for field in ("operation", "mode", "primary_skill", "role"):
                        self.assertEqual(decision[field], expected[field])
                self.assertEqual(case["requires_question"], decision["clarification"] is not None)

    def test_all_operation_mode_combinations_match_owner_contract(self) -> None:
        # Free primary selection is the published begin behavior, not a new specialized mapping.
        exceptions = {"query-analysis": "flow1c-query-analysis", "interview-preparation": "flow1c-interview-preparation",
                      "template-management": "flow1c-document-templates", "template-document": "flow1c-document-templates"}
        stages = read("config/stages.json")["operations"]
        for operation, route in self.rules["routes"].items():
            for mode in policy.MODES:
                with self.subTest(operation=operation, mode=mode):
                    decision = check_route({"schema_version": 1, "expected_outcome": "synthetic request",
                                            "operation": operation, "mode": mode}, self.rules)
                    self.decision_validator.validate(decision)
                    if mode not in route["modes"]:
                        self.assertEqual(decision["status"], "INVALID")
                    else:
                        expected = stages[operation]["skill"] if mode == "formal" else exceptions.get(operation, "flow1c-consultation")
                        self.assertEqual(decision["primary_skill"], expected)

    def test_legacy_comparison_uses_actual_default_and_query_owners(self) -> None:
        fixture = read("tests/fixtures/routing-baseline.json")
        for case in fixture["defaults"]:
            operation, summary, mode = case["operation"], case["summary"], case["explicit_mode"]
            effective = "query-analysis" if operation == "consultation" and mode != "draft" and query_policy.infer_query_request_intent(summary) else operation
            selected = policy.select_request_mode(effective, summary, mode)
            with self.subTest(case=case["case_id"]):
                proposal = legacy_proposal(operation, summary, mode)
                self.assertEqual((proposal["operation"], proposal["mode"]), (effective, selected["mode"]))
        self.assertEqual(fixture["runtime_versions"]["gate_schema"], gate_state.AGENT_GATE_SCHEMA_VERSION)
        self.assertEqual(fixture["runtime_versions"]["gate_policy"], gate_state.POLICY_VERSION)
        self.assertEqual(fixture["runtime_versions"]["evidence_written"], [1, 2])
        self.assertEqual(fixture["runtime_versions"]["evidence_declared_schema"], read("schemas/evidence.schema.json")["properties"]["schema_version"]["const"])

    def test_error_decisions_validate_and_diagnostics_do_not_echo_materials(self) -> None:
        values = [None, {}, {"schema_version": True, "expected_outcome": "secret-material"},
                  {"schema_version": 1, "expected_outcome": "secret-material", "operation": "unknown"},
                  {"schema_version": 1, "expected_outcome": "secret-material", "sources": [{"kind": "extension", "version": "current", "proof": "fake"}]}]
        for value in values:
            decision = check_route(value, self.rules)
            self.decision_validator.validate(decision)
            self.assertEqual(decision["status"], "INVALID")
            self.assertNotIn("secret-material", json.dumps(decision))

    def test_runtime_reads_only_product_catalog_and_never_creates_state(self) -> None:
        before = {p.name for p in ROOT.iterdir()}
        opened = []
        original = Path.read_text

        def recorded(path, *args, **kwargs):
            opened.append(path.relative_to(ROOT).as_posix())
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", recorded), \
             mock.patch.object(Path, "write_text", side_effect=AssertionError("write")), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")), \
             mock.patch("subprocess.run", side_effect=AssertionError("process")):
            decision = check_proposal({"schema_version": 1, "operation": "publish", "mode": "formal",
                                       "expected_outcome": "synthetic publication"}, product_root=ROOT)
        self.assertEqual(decision["status"], "VALID")
        self.assertEqual(opened, ["config/intent-routes.json", "config/stages.json"])
        self.assertEqual(before, {p.name for p in ROOT.iterdir()})
        self.assertNotIn("approvals", decision)

    def test_invalid_catalog_is_a_structured_recoverable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RoutingError) as failure:
                load_rules(root)
            self.assertEqual(failure.exception.code, "ROUTE_CATALOG_INVALID")
            self.assertTrue(failure.exception.as_dict()["preserved_state"])
            (root / "config").mkdir()
            (root / "config/intent-routes.json").write_text("not-json", encoding="utf-8")
            with self.assertRaises(RoutingError):
                load_rules(root)
            self.assertEqual({p.name for p in root.iterdir()}, {"config"})


if __name__ == "__main__":
    unittest.main()
