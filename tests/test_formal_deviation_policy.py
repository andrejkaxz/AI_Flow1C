from __future__ import annotations

import copy
import unittest

from scripts.flow1c_policy import apply_deviation, available_actions, build_conditions


DOC_POLICY = {
    "provisional_allowed": True,
    "deviation_policy": {
        "allow_formal_documents": True,
        "waivable_categories": ["required_input", "conditional_input", "status", "approval"],
    },
}


class FormalDeviationPolicyTests(unittest.TestCase):
    def gate(self, *, operation="functional-spec"):
        return {
            "operation": operation,
            "mode": "formal",
            "state": "NEEDS_INPUT",
            "work_item_exists": True,
            "conditions": build_conditions(DOC_POLICY, conditional_missing=[
                {"id": "meeting_materials", "label": "Материалы встреч"},
            ]),
            "deviations": [],
        }

    def test_only_selected_visible_condition_is_waived(self):
        gate = self.gate()
        original = copy.deepcopy(gate)
        result = apply_deviation(gate, {
            "reason": "Риск принят",
            "user_statement": "Продолжай формально без материалов встреч.",
            "deviation_type": "process",
            "scope": "gate",
            "condition_ids": ["missing:meeting_materials"],
        }, DOC_POLICY)
        self.assertEqual(result["state"], "READY_WITH_DEVIATIONS")
        self.assertEqual(result["mode"], "formal")
        self.assertEqual(result["compliance"], "DEVIATED")
        self.assertEqual(result["remaining_blockers"], [])
        self.assertEqual(gate, original)
        self.assertEqual(result["deviations"][0]["actor"], "user")
        self.assertEqual(result["deviations"][0]["condition_ids"], ["missing:meeting_materials"])

    def test_unknown_or_hard_condition_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_deviation(self.gate(), {
                "reason": "continue", "user_statement": "continue",
                "deviation_type": "process", "scope": "gate", "condition_ids": ["later"],
            }, DOC_POLICY)
        gate = self.gate()
        gate["conditions"].append({"id": "hard", "category": "path_safety", "message": "path", "source": "guard", "waivable": True, "blocking": True})
        with self.assertRaises(ValueError):
            apply_deviation(gate, {
                "reason": "continue", "user_statement": "continue",
                "deviation_type": "process", "scope": "gate", "condition_ids": ["hard"],
            }, DOC_POLICY)

    def test_nonwaivable_blocker_keeps_gate_blocked(self):
        gate = self.gate()
        gate["conditions"].append({"id": "approval:human", "category": "approval", "message": "approval missing", "source": "stage", "waivable": False, "blocking": True})
        result = apply_deviation(gate, {
            "reason": "continue", "user_statement": "continue",
            "deviation_type": "process", "scope": "gate", "condition_ids": ["missing:meeting_materials"],
        }, DOC_POLICY)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual([item["id"] for item in result["remaining_blockers"]], ["approval:human"])

    def test_sequential_decisions_keep_previously_waived_conditions(self):
        gate = self.gate()
        gate["conditions"].append({
            "id": "missing:second", "category": "required_input", "message": "Second input",
            "source": "stage", "waivable": True, "blocking": True,
        })
        first = apply_deviation(gate, {
            "reason": "accept first", "user_statement": "Continue without meeting materials",
            "deviation_type": "process", "scope": "gate",
            "condition_ids": ["missing:meeting_materials"],
        }, DOC_POLICY)
        second = apply_deviation(first, {
            "reason": "accept second", "user_statement": "Continue without the second input",
            "deviation_type": "process", "scope": "gate", "condition_ids": ["missing:second"],
        }, DOC_POLICY)
        self.assertEqual(second["state"], "READY_WITH_DEVIATIONS")
        self.assertEqual(second["remaining_blockers"], [])

    def test_registry_bypass_is_typed_and_hard_stages_reject_it(self):
        gate = {
            "operation": "functional-spec", "mode": "formal", "state": "BLOCKED", "work_item_exists": False,
            "conditions": [{"id": "missing:registry_traceability", "category": "registry_traceability", "message": "registry", "source": "registry", "waivable": True, "blocking": True}],
            "deviations": [],
        }
        result = apply_deviation(gate, {
            "reason": "No registry available", "user_statement": "Use my reference provisionally.",
            "deviation_type": "registry_bypass", "scope": "work-item", "condition_ids": ["missing:registry_traceability"],
        }, DOC_POLICY)
        self.assertEqual(result["state"], "NEEDS_CONFIRMATION")
        hard = {**gate, "operation": "development"}
        with self.assertRaises(ValueError):
            apply_deviation(hard, {
                "reason": "bypass", "user_statement": "bypass",
                "deviation_type": "registry_bypass", "scope": "work-item", "condition_ids": ["missing:registry_traceability"],
            }, {"provisional_allowed": False, "deviation_policy": {"allow_formal_documents": False, "waivable_categories": []}})

    def test_state_aware_actions_do_not_advertise_blocked_stage_tools(self):
        stage = {"allowed_tools": ["flow1c_context", "flow1c_dialogue", "flow1c_write", "flow1c_complete", "flow1c_action"]}
        self.assertEqual(available_actions(mode="formal", operation="functional-spec", state="BLOCKED", stage=stage), ["flow1c_dialogue", "flow1c_action"])
        self.assertEqual(available_actions(mode="formal", operation="functional-spec", state="COMPLETE_WITH_DEVIATIONS", stage=stage), [])


if __name__ == "__main__":
    unittest.main()
