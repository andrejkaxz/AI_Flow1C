"""Acceptance must not count labels, lucky retries or partial/fixture reports."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.opencode_evals import run_case
from scripts.routing_eval_policy import aggregate, assess_selection, corpus_errors
from scripts.routing_evals import assess_opencode_selection, observe, selection_observed, selection_prompt

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((ROOT / "evals/routing-cases.json").read_text(encoding="utf-8"))
CASE = next(c for c in CORPUS["cases"] if c["case_id"] == "consultation-1")
AMBIGUOUS = next(c for c in CORPUS["cases"] if c["requires_question"])


def observation(case: dict = CASE) -> dict:
    return {"calls": [{"tool": "flow1c_route_check", "status": "completed", "output": case["expected"]}],
            "questions": [], "extension_unchanged": True}


def question() -> dict:
    return {"questions": [{"question": "Что нужно проверить?"}]}


class RoutingEvalPolicyTests(unittest.TestCase):
    def test_corpus_source_selectors_are_supplied_as_user_inputs(self) -> None:
        self.assertEqual(corpus_errors(CORPUS["cases"]), [])
        case = copy.deepcopy(CASE)
        case["expected"]["sources"] = [{"kind": "attachment", "selector": "hidden-input.md", "version": "provided"}]
        self.assertTrue(corpus_errors([case]))
        case["context"] = ["Attachment hidden-input.md supplied by user"]
        self.assertEqual(corpus_errors([case]), [])
        self.assertTrue(corpus_errors([case, case]))

    def test_exact_decision_and_all_independent_dimensions(self) -> None:
        self.assertTrue(assess_selection(CASE, observation())["passed"])
        for field, value in (("operation", "publish"), ("mode", "formal"),
                             ("primary_skill", "flow1c-project-setup"), ("sources", []),
                             ("role", "analyst"), ("source_relation", "compare")):
            candidate = copy.deepcopy(observation())
            candidate["calls"][0]["output"][field] = value
            with self.subTest(field=field):
                self.assertFalse(assess_selection(CASE, candidate)["passed"])
        candidate = copy.deepcopy(observation())
        candidate["calls"][0]["output"]["substeps"] = ["redmine-upload"]
        self.assertFalse(assess_selection(CASE, candidate)["passed"])

    def test_first_wrong_or_failed_check_cannot_be_repaired_by_later_correct_check(self) -> None:
        for first in ({"tool": "flow1c_route_check", "status": "error"},
                      {"tool": "flow1c_route_check", "status": "completed", "output": {"status": "INVALID"}}):
            candidate = observation()
            candidate["calls"].insert(0, first)
            self.assertFalse(assess_selection(CASE, candidate)["matches"]["exact"])

    def test_malformed_or_absent_output_and_correct_final_prose_do_not_pass(self) -> None:
        for output in (None, "VALID", [], {}, {"status": "VALID", "sources": "chat"}):
            candidate = copy.deepcopy(observation())
            candidate["calls"][0]["output"] = output
            self.assertFalse(assess_selection(CASE, candidate)["passed"])

    def test_sources_ignore_resolver_but_require_version_selector_and_multiplicity(self) -> None:
        case = next(c for c in CORPUS["cases"] if any(s.get("selector") for s in c["expected"]["sources"])
                    and not c["requires_question"])
        candidate = copy.deepcopy(observation(case))
        candidate["calls"][0]["output"]["sources"][0]["resolver"] = "gated-tool"
        self.assertTrue(assess_selection(case, candidate)["passed"])
        for change in ({"selector": "wrong"}, {"version": "historical"}):
            altered = copy.deepcopy(candidate)
            altered["calls"][0]["output"]["sources"][0].update(change)
            self.assertFalse(assess_selection(case, altered)["matches"]["source"])
        candidate["calls"][0]["output"]["sources"] *= 2
        self.assertFalse(assess_selection(case, candidate)["matches"]["source"])

    def test_ambiguity_requires_one_real_focused_question_and_no_gate(self) -> None:
        candidate = observation(AMBIGUOUS)
        candidate["questions"] = [question()]
        self.assertTrue(assess_selection(AMBIGUOUS, candidate)["passed"])
        for questions in ([], [question(), question()], [{"questions": []}],
                          [{"questions": [{"question": ""}]}],
                          [{"questions": [{"question": "first"}, {"question": "second"}]}]):
            altered = {**candidate, "questions": questions}
            self.assertFalse(assess_selection(AMBIGUOUS, altered)["passed"])
        candidate["calls"].append({"tool": "flow1c_begin", "status": "error"})
        self.assertFalse(assess_selection(AMBIGUOUS, candidate)["passed"])

    def test_saved_answers_cannot_be_asked_again(self) -> None:
        candidate = observation()
        candidate["questions"] = [question()]
        self.assertFalse(assess_selection(CASE, candidate)["passed"])

    def test_tool_attempts_and_changed_extension_are_safety_failures(self) -> None:
        for tool in ("bash", "read", "task", "flow1c_write", "flow1c_complete", "flow1c_begin"):
            candidate = copy.deepcopy(observation())
            candidate["calls"].append({"tool": tool, "status": "error"})
            self.assertTrue(assess_selection(CASE, candidate)["safety_violations"])
        candidate = {**observation(), "extension_unchanged": False}
        self.assertTrue(assess_selection(CASE, candidate)["safety_violations"])

    def test_infrastructure_failure_does_not_count_as_successful_routing(self) -> None:
        for field, value in (("timed_out", True), ("server_exit", 0), ("provider_error", True)):
            self.assertFalse(assess_selection(CASE, {**observation(), field: value})["passed"])

    def test_aggregate_missing_duplicate_and_fixture_runs_fail_closed(self) -> None:
        result = {"case": CASE["case_id"], "variant": "candidate", "run": 1,
                  **assess_selection(CASE, observation())}
        self.assertTrue(aggregate([CASE], [result], runs=1)["selection_passed"])
        for values, runs in (([], 1), ([result, result], 1), ([result], 2)):
            self.assertFalse(aggregate([CASE], values, runs=runs)["selection_passed"])
        self.assertIsNone(aggregate([CASE], [result], runs=1, fixture_only=True)["selection_passed"])
        self.assertFalse(aggregate([CASE], [result], runs=1)["release_ready"])
        self.assertFalse(aggregate([CASE], [{**result, "passed": None}], runs=1)["selection_passed"])
        self.assertFalse(aggregate([CASE], [{**result, "passed": False, "failures": []}], runs=1)["selection_passed"])

    def test_aggregate_threshold_and_baseline_regression(self) -> None:
        good = assess_selection(CASE, observation())
        wrong = copy.deepcopy(observation())
        wrong["calls"][0]["output"]["mode"] = "formal"
        bad = assess_selection(CASE, wrong)
        rows = [{"case": CASE["case_id"], "variant": "candidate", "run": i,
                 **(bad if i == 20 else good)} for i in range(1, 21)]
        self.assertTrue(aggregate([CASE], rows, runs=20)["selection_passed"])
        rows[18].update(bad)
        self.assertFalse(aggregate([CASE], rows, runs=20)["selection_passed"])
        rows[18].update(good)
        rows += [{"case": CASE["case_id"], "variant": "baseline", "run": i, **good} for i in range(1, 21)]
        compared = aggregate([CASE], rows, runs=20, baseline=True)
        self.assertFalse(compared["selection_passed"])
        self.assertEqual(compared["baseline_regressions"], [{"case": CASE["case_id"], "run": 20}])

    def test_ambiguity_threshold_is_separate_and_absolute(self) -> None:
        good = assess_selection(CASE, observation())
        bad = assess_selection(AMBIGUOUS, observation(AMBIGUOUS))
        rows = [{"case": case["case_id"], "variant": "candidate", "run": 1, **result}
                for case, result in ((CASE, good), (AMBIGUOUS, bad))]
        report = aggregate([CASE, AMBIGUOUS], rows, runs=1)
        self.assertEqual(report["metrics"]["candidate"]["scores"]["exact"]["accuracy"], 1)
        self.assertEqual(report["metrics"]["candidate"]["scores"]["ambiguity"]["accuracy"], 0)
        self.assertFalse(report["selection_passed"])


class RoutingEvalAdapterTests(unittest.TestCase):
    def test_prompt_contains_only_inputs_and_keeps_saved_answers(self) -> None:
        case = copy.deepcopy(CASE)
        case.update(context=["Earlier discussion"], answers=["Current configuration"],
                    expected={"secret_label": "MUST_NOT_LEAK"}, rejected_routes=["MUST_NOT_LEAK"])
        prompt = selection_prompt(case)
        self.assertIn("Earlier discussion", prompt)
        self.assertIn("Current configuration", prompt)
        self.assertNotIn("MUST_NOT_LEAK", prompt)
        self.assertNotIn(case["case_id"], prompt)

    def test_observer_orders_first_attempt_by_start_and_deduplicates_snapshots(self) -> None:
        def call(identity, start, output):
            return {"type": "tool", "callID": identity, "tool": "flow1c_route_check",
                    "state": {"status": "completed", "output": json.dumps(output), "time": {"start": start}}}
        first, second = call("first", 1, {"status": "INVALID"}), call("second", 2, CASE["expected"])
        trace = [{"parts": [second, first, second]}]
        observed = observe(trace, [], extension_unchanged=True)
        self.assertEqual(len(observed["calls"]), 2)
        self.assertFalse(assess_selection(CASE, observed)["passed"])
        self.assertTrue(selection_observed(trace, []))

    def test_clarification_decision_alone_is_not_the_question(self) -> None:
        trace = [{"parts": [{"type": "tool", "tool": "flow1c_route_check", "callID": "first",
                             "state": {"status": "completed", "output": json.dumps(AMBIGUOUS["expected"])}}]}]
        self.assertFalse(selection_observed(trace, []))
        self.assertTrue(selection_observed(trace, [question()]))

    def test_model_and_reasoning_variant_must_be_observed(self) -> None:
        case = {"annotation": CASE, "target_model": "synthetic/model", "target_variant": "medium"}
        trace = [{"info": {"role": "assistant", "providerID": "synthetic", "modelID": "model", "variant": "medium"},
                  "parts": [{"type": "tool", "callID": "first", "tool": "flow1c_route_check",
                             "state": {"status": "completed", "output": json.dumps(CASE["expected"])}}]}]
        self.assertTrue(assess_opencode_selection(case, trace, [], extension_unchanged=True)["passed"])
        trace[0]["info"]["variant"] = "low"
        self.assertFalse(assess_opencode_selection(case, trace, [], extension_unchanged=True)["passed"])

    def test_server_stops_at_question_without_reply_and_preserves_trace_and_variant(self) -> None:
        requests = []
        class Server:
            returncode = None
            def poll(self):
                return None
            def terminate(self):
                pass
            def wait(self, timeout):
                pass
        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps(self.payload).encode() if self.payload is not None else b""
        def reply(request, **kwargs):
            endpoint = request.full_url.split("?", 1)[0].split("127.0.0.1:", 1)[1].split("/", 1)[1]
            body = json.loads(request.data) if request.data else None
            requests.append((endpoint, body))
            values = {"global/health": {"version": "synthetic"},
                      "doc": {"paths": {"/question": {}, "/question/{requestID}/reply": {}}},
                      "session": {"id": "synthetic-session"}, "permission": [],
                      "question": [{"id": "question-1", "sessionID": "synthetic-session", **question()}],
                      "session/synthetic-session/message": []}
            return Response(values.get(endpoint))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            extension = destination / "extension"
            extension.mkdir()
            with mock.patch("scripts.opencode_evals.build_fixture", return_value=(destination, destination, extension, destination)), \
                 mock.patch("scripts.opencode_evals.subprocess.Popen", return_value=Server()), \
                 mock.patch("scripts.opencode_evals.urllib.request.urlopen", side_effect=reply):
                result = run_case("synthetic-executable", "synthetic/model", {"annotation": AMBIGUOUS, "prompt": "look"},
                                  destination, timeout=10, assessor=assess_opencode_selection,
                                  observation_complete=selection_observed, model_variant="medium")
            self.assertTrue(result["passed"])
            self.assertTrue((destination / "questions.json").is_file())
            self.assertTrue((destination / "events.ndjson").is_file())
            self.assertFalse(any(endpoint.endswith("/reply") for endpoint, _ in requests))
            submitted = next(body for endpoint, body in requests if endpoint.endswith("/prompt_async"))
            self.assertEqual(submitted["variant"], "medium")
            self.assertTrue(any(endpoint.endswith("/abort") for endpoint, _ in requests))


if __name__ == "__main__":
    unittest.main()
