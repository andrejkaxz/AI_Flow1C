from __future__ import annotations

import json
import unittest

from scripts.opencode_evals import assess_run


class IntentRoutingEvalTests(unittest.TestCase):
    def test_pre_gate_route_checks_do_not_replace_begin(self) -> None:
        case = {"expected_states": [], "requires_completion": False}

        def trace(names):
            return [{"parts": [{"type": "tool", "callID": str(index), "tool": name,
                                 "state": {"status": "completed", "input": {}}}
                                for index, name in enumerate(names)]}]

        permitted = assess_run(case, trace(["flow1c_route_catalog", "flow1c_route_check",
                                            "flow1c_redmine_files", "flow1c_begin", "flow1c_context"]), [])
        rejected = assess_run(case, trace(["flow1c_route_check", "flow1c_context"]), [])
        self.assertTrue(permitted["passed"])
        self.assertIn("gate operation was called before flow1c_begin", rejected["failures"])

    def test_query_eval_checks_completed_candidate_not_final_prose(self) -> None:
        case = {"expected_states": ["CONSULTATION_COMPLETE"],
                "required_tool_sequence": ["flow1c_begin", "flow1c_query_check", "flow1c_complete"],
                "required_query_fragments": ["СуммаБезНДСРегл"]}

        def messages(query: str) -> list[dict]:
            return [{"parts": [
                {"type": "tool", "callID": "begin", "tool": "flow1c_begin", "state": {"status": "completed"}},
                {"type": "tool", "callID": "check", "tool": "flow1c_query_check", "state": {"status": "completed"}},
                {"type": "tool", "callID": "complete", "tool": "flow1c_complete", "state": {
                    "status": "completed", "output": json.dumps({"state": "CONSULTATION_COMPLETE", "query_text": query})}},
            ]}]

        self.assertTrue(assess_run(case, messages("ВЫБРАТЬ СуммаБезНДСРегл"), [])["passed"])
        result = assess_run(case, messages("ВЫБРАТЬ СуммаБезНДС"), [])
        self.assertIn("completed query omits required fragment: СуммаБезНДСРегл", result["failures"])

    def test_ambiguous_request_requires_question_before_gate(self) -> None:
        case = {
            "expected_states": ["CONSULTATION_COMPLETE"],
            "requires_completion": False,
            "requires_question": True,
            "question_before_begin": True,
        }
        question = [{
            "questions": [{"question": "Что проверить в документе?"}],
            "observed_epoch_ms": 2000,
            "elapsed_seconds": 1.0,
        }]
        def messages(start: int) -> list[dict]:
            return [{"parts": [{"type": "tool", "callID": "begin", "tool": "flow1c_begin",
                                "state": {"status": "completed", "time": {"start": start}}}]}]

        early = assess_run(case, messages(1000), question)
        late = assess_run(case, messages(3000), question)
        self.assertIn("gate was opened before the intent was clarified", early["failures"])
        self.assertNotIn("gate was opened before the intent was clarified", late["failures"])


if __name__ == "__main__":
    unittest.main()
