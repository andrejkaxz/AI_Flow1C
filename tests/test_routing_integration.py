"""Route CLI, begin, saved-gate and projection regressions without live services."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

from flow1c import cli, routing, storage
from flow1c.errors import WorkflowError
from flow1c.workflow import state
from jsonschema import Draft202012Validator
from test_cli_contract import CliScenario

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("route_projections", ROOT / "scripts/generate-route-projections.py")
projections = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(projections)


class RoutingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scenario = CliScenario(Path(self.temp.name))
        self.root = self.scenario.root

    def call(self, command: str, request: dict | None = None) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch.object(cli, "ROOT", self.root), \
             mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(output):
            code = cli.main([command, "--json"] if command == "route-catalog" else [command, "--json-stdin"])
        return code, json.loads(output.getvalue())

    def proposal(self, **fields) -> dict:
        return {"schema_version": 1, "operation": "functional-spec", "mode": "draft",
                "expected_outcome": "Synthetic draft", "sources": [], **fields}

    def begin(self, proposal=None, **fields) -> tuple[int, dict]:
        proposal = self.proposal() if proposal is None else proposal
        return self.call("agent-begin", {"operation": proposal.get("operation", "consultation"),
                                         "mode": proposal.get("mode"),
                                         "summary": proposal["expected_outcome"],
                                         "route_proposal": proposal, **fields})

    def test_catalog_and_check_are_read_only_and_work_from_other_cwd(self) -> None:
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        catalog = self.scenario.run(["route-catalog", "--json"], outside=True)
        self.assertEqual(catalog["exit_code"], 0)
        self.assertEqual(len(catalog["stdout"]["routes"]), 19)
        self.assertNotIn("owners", catalog["stdout"])
        schema = json.loads((ROOT / "schemas/route-proposal.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["stdout"]["proposal_schema"], schema)
        checked = self.scenario.run(["route-check", "--json-stdin"], self.proposal(), outside=True)
        self.assertEqual(checked["exit_code"], 0)
        self.assertEqual(checked["stdout"]["status"], "VALID")
        self.assertEqual(before, {path.relative_to(self.root) for path in self.root.rglob("*")})

    def test_ambiguity_and_incompatible_fields_never_create_a_gate(self) -> None:
        for changes, code, status in (
            ({"ambiguities": ["source"]}, 1, "CLARIFICATION_REQUIRED"),
            ({"primary_skill": "flow1c-functional-review"}, 2, "INVALID"),
            ({"approval": True}, 2, "INVALID"),
            ({"sources": [{"kind": "configuration", "version": "historical"}]}, 2, "INVALID"),
            ({"gate_id": "11111111-1111-4111-8111-111111111111"}, 2, "INVALID"),
        ):
            with self.subTest(changes=changes):
                actual, decision = self.begin(self.proposal(**changes))
                self.assertEqual((actual, decision["status"]), (code, status))
                self.assertNotIn("gate_id", decision)
                self.assertFalse((self.root / ".workspace").exists())

    def test_begin_rejects_conflicting_outer_fields_and_untrusted_digest(self) -> None:
        for fields in ({"mode": "formal"}, {"operation": "development"}, {"summary": "Other goal"},
                       {"task_reference": "OTHER"}):
            proposal = self.proposal(references={"task_reference": "SYNTHETIC"})
            code, decision = self.begin(proposal, **fields)
            self.assertEqual((code, decision["status"]), (2, "INVALID"))
        proposal = self.proposal(catalog_digest="a" * 64)
        self.assertEqual(self.begin(proposal)[0], 2)
        self.assertFalse((self.root / ".workspace").exists())

    def test_formal_route_does_not_bypass_prerequisites(self) -> None:
        code, gate = self.begin(self.proposal(mode="formal"))
        self.assertEqual((code, gate["state"]), (2, "BLOCKED"))
        self.assertEqual(gate["route_decision"]["status"], "VALID")
        self.assertNotIn("flow1c_write", gate["available_actions"])
        self.assertFalse(gate.get("work_item_exists"))
        loaded = state.load_gate(gate["gate_id"], product_root=self.root)
        self.assertEqual(loaded["state"], "BLOCKED")
        self.assertEqual(loaded["skill"], "flow1c-functional-spec")

    def test_first_persisted_gate_contains_checked_decision(self) -> None:
        original = state.save_gate
        writes = []

        def recorded(gate, **kwargs):
            writes.append(deepcopy(gate))
            return original(gate, **kwargs)

        with mock.patch.object(state, "save_gate", side_effect=recorded):
            code, gate = self.begin()
        self.assertEqual(code, 0)
        self.assertTrue(all(record["route_decision"]["status"] == "VALID" for record in writes))
        self.assertEqual(gate["route_origin"], "structured")
        schema = json.loads((ROOT / "schemas/route-decision.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(gate["route_decision"])

    def test_structured_blocked_formal_can_become_an_independent_draft(self) -> None:
        _, gate = self.begin(self.proposal(mode="formal"))
        code, changed = self.call("agent-dialogue", {
            "gate_id": gate["gate_id"], "action": "answer", "resolution": "independent-draft",
            "answer": "Продолжим независимым черновиком из чата",
        })
        self.assertEqual((code, changed["gate_id"], changed["mode"]), (0, gate["gate_id"], "draft"))
        self.assertEqual(changed["skill"], "flow1c-consultation")
        self.assertEqual(changed["route_proposal"]["mode"], "draft")
        loaded = state.load_gate(gate["gate_id"], product_root=self.root)
        self.assertEqual(loaded["answers"][0]["answer"], "Продолжим независимым черновиком из чата")

    def test_checked_draft_survives_process_restart_and_completion(self) -> None:
        proposal = self.proposal()
        first = self.scenario.run(["agent-begin", "--json-stdin"], {
            "operation": proposal["operation"], "summary": proposal["expected_outcome"],
            "mode": "draft", "route_proposal": proposal,
        }, outside=True)
        self.assertEqual(first["exit_code"], 0)
        gate_id = self.scenario.gate_id
        self.scenario.run(["agent-dialogue", "--json-stdin"], {
            "gate_id": gate_id, "action": "record", "answer": "Saved synthetic answer",
        })
        self.scenario.run(["agent-write", "--json-stdin"], {
            "gate_id": gate_id, "target": "draft", "path": "result.md", "content": "Synthetic result",
        })
        result = self.scenario.run(["agent-complete", "--json-stdin"], {"gate_id": gate_id}, outside=True)
        self.assertEqual((result["exit_code"], result["stdout"]["state"]), (0, "DRAFT_COMPLETE"))
        self.assertEqual(len(list((self.root / ".workspace/agent-gates").glob("*.json"))), 1)
        saved = storage.read_json(state.gate_path(gate_id, product_root=self.root))
        self.assertEqual(saved["notes"][0]["text"], "Saved synthetic answer")

    def test_saved_decision_is_recomputed_and_incompatible_proposal_preserves_state(self) -> None:
        _, gate = self.begin()
        gate["route_decision"]["catalog_digest"] = "a" * 64
        state.save_gate(gate, product_root=self.root)
        loaded = state.load_gate(gate["gate_id"], product_root=self.root)
        self.assertEqual(loaded["route_decision"]["catalog_digest"], routing.load_rules(self.root)["digest"])
        saved = storage.read_json(state.gate_path(gate["gate_id"], product_root=self.root))
        self.assertEqual(saved["route_decision"], loaded["route_decision"])
        loaded["route_proposal"]["mode"] = "formal"
        state.save_gate(loaded, product_root=self.root)
        before = state.gate_path(gate["gate_id"], product_root=self.root).read_bytes()
        with self.assertRaisesRegex(WorkflowError, "ROUTE_RECOVERY_REQUIRED"):
            state.load_gate(gate["gate_id"], product_root=self.root)
        self.assertEqual(before, state.gate_path(gate["gate_id"], product_root=self.root).read_bytes())

    def test_dialogue_mode_change_checks_same_gate_and_keeps_answers(self) -> None:
        _, gate = self.begin(self.proposal(operation="consultation", mode="explore"))
        code, changed = self.call("agent-dialogue", {"gate_id": gate["gate_id"], "action": "record",
                                                   "answer": "Save a draft", "mode": "draft"})
        self.assertEqual(code, 0)
        self.assertEqual(changed["gate_id"], gate["gate_id"])
        self.assertEqual(changed["route_proposal"]["mode"], "draft")
        self.assertEqual(state.load_gate(gate["gate_id"], product_root=self.root)["mode"], "draft")

    def test_unsupported_saved_decision_version_is_preserved_for_recovery(self) -> None:
        _, gate = self.begin()
        for field, version in (("schema_version", 2), ("policy_version", 2), ("schema_version", True)):
            changed = deepcopy(gate)
            changed["route_decision"][field] = version
            state.save_gate(changed, product_root=self.root)
            path = state.gate_path(gate["gate_id"], product_root=self.root)
            before = path.read_bytes()
            with self.assertRaisesRegex(WorkflowError, "unsupported saved RouteDecision version"):
                state.load_gate(gate["gate_id"], product_root=self.root)
            self.assertEqual(before, path.read_bytes())

    def test_legacy_begin_and_old_saved_gate_keep_working(self) -> None:
        code, gate = self.call("agent-begin", {"operation": "consultation", "mode": "explore", "summary": "Explain a method"})
        self.assertEqual((code, gate["mode"], gate["route_origin"]), (0, "explore", "legacy"))
        self.assertTrue(gate["route_decision"]["reason_codes"][0].startswith("LEGACY_"))
        for field in ("route_decision", "route_origin", "route_proposal"):
            gate.pop(field, None)
        state.save_gate(gate, product_root=self.root)
        self.assertEqual(state.load_gate(gate["gate_id"], product_root=self.root)["state"], "READY")

    def test_route_check_bounds_and_unknown_fields_cannot_change_cli_handler(self) -> None:
        for value in ([1], {"handler": "agent-write"}, {"schema_version": 1, "expected_outcome": "x" * 33000}):
            output = io.StringIO()
            with mock.patch.object(cli, "ROOT", self.root), \
                 mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(value))), redirect_stdout(output):
                self.assertEqual(cli.main(["route-check", "--json-stdin"]), 2)
            self.assertFalse((self.root / ".workspace").exists())

    def test_generated_blocks_preserve_manual_text_and_fail_closed_on_bad_markers(self) -> None:
        block = projections.render(list(routing.load_rules(ROOT)["routes"].values()))
        before = "manual before\n" + projections.START + "old" + projections.END + "\nmanual after\n"
        after = projections.replace_block(before, block)
        self.assertTrue(after.startswith("manual before\n"))
        self.assertTrue(after.endswith("\nmanual after\n"))
        self.assertEqual(projections.replace_block(after, block), after)
        with self.assertRaises(ValueError):
            projections.replace_block(projections.START + "manual", block)
        self.assertEqual(projections.generate(ROOT, check=True), [])

    def test_evidence_v1_v2_resume_and_future_versions_preserve_saved_bytes(self) -> None:
        _, gate = self.begin()
        path, evidence = state.evidence_for_gate(gate, product_root=self.root)
        for version in (1, 2):
            record = {**evidence, "schema_version": version, "artifacts": [{"synthetic": "preserved"}]}
            storage.write_json(path, record)
            before = path.read_bytes()
            self.assertEqual(state.validate_json_record(record, ROOT / "schemas/evidence.schema.json"), [])
            _, loaded = state.evidence_for_gate(gate, product_root=self.root)
            self.assertEqual(loaded["schema_version"], 2)
            self.assertEqual(loaded["artifacts"], record["artifacts"])
            self.assertEqual(before, path.read_bytes())
        for version in (3, True, "2", None):
            record = {**evidence, "schema_version": version}
            storage.write_json(path, record)
            before = path.read_bytes()
            self.assertTrue(state.validate_json_record(record, ROOT / "schemas/evidence.schema.json"))
            with self.assertRaisesRegex(WorkflowError, "unsupported schema version"):
                state.evidence_for_gate(gate, product_root=self.root)
            self.assertEqual(before, path.read_bytes())

    def test_projection_check_detects_drift_without_writing(self) -> None:
        original = Path.read_text

        def drift(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path == ROOT / "AGENTS.md":
                return text.replace("Interpret the user's goal", "Outdated routing instruction")
            return text

        with mock.patch.object(Path, "read_text", drift), \
             mock.patch.object(Path, "write_text", side_effect=AssertionError("check must not write")):
            self.assertEqual(projections.generate(ROOT, check=True), ["AGENTS.md"])

    def test_client_eval_fixture_has_the_python_package_and_route_tools(self) -> None:
        from scripts.opencode_evals import build_fixture
        from subprocess import run
        import sys

        destination = Path(self.temp.name) / "model-fixture"
        workflow, *_ = build_fixture(destination, {"id": "synthetic-package-smoke"})
        result = run([sys.executable, "-B", "scripts/flow1c.py", "route-catalog", "--json"],
                     cwd=workflow, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["routes"]), 19)


if __name__ == "__main__":
    unittest.main()
