import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flow1c_sections_policy", ROOT / "scripts" / "flow1c_sections_policy.py")
policy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(policy)
BASE_SPEC = importlib.util.spec_from_file_location("flow1c_policy", ROOT / "scripts" / "flow1c_policy.py")
base_policy = importlib.util.module_from_spec(BASE_SPEC)
assert BASE_SPEC.loader
BASE_SPEC.loader.exec_module(base_policy)


def catalog():
    text = (ROOT / "standards" / "functional-specification-sections.md").read_text(encoding="utf-8")
    return policy.parse_catalog_markdown(text)


class SectionPolicyTests(unittest.TestCase):
    def test_cli_catalog_exposes_content_contract_without_changing_summary(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/flow1c.py"), "section-catalog"],
            cwd=ROOT, check=True, capture_output=True, encoding="utf-8",
        )
        response = json.loads(result.stdout)
        value = catalog()
        self.assertEqual(response["schema_version"], 1)
        self.assertEqual(response["sections"], policy.catalog_summary(value))
        self.assertEqual(response["contracts"], {s["section_id"]: s for s in value["sections"]})
        technical = response["contracts"]["technical-implementation"]
        self.assertIn("прошедшем времени", technical["purpose"])
        self.assertIn("внесённых разработчиком", technical["required_items"][4]["what_to_obtain"])
        self.assertIn("не отражённых в дизайне", technical["required_items"][4]["what_to_obtain"])

    def test_catalog_has_unique_aliases_and_five_technical_items(self):
        value = catalog()
        policy.validate_catalog(value)
        technical = next(item for item in value["sections"] if item["section_id"] == "technical-implementation")
        self.assertEqual(len(technical["required_items"]), 5)
        self.assertEqual({item["item_id"] for item in technical["required_items"]}, {
            "new-common-modules", "common-module-properties", "new-procedures-functions",
            "changed-procedures-functions", "attribute-dimension-resource-properties",
        })

    def test_alias_resolution_preserves_catalog_order_and_rejects_unknown(self):
        result = policy.resolve_section_names(["тестовый сценарий", "technical-implementation"], catalog())
        self.assertEqual([item["section_id"] for item in result], ["technical-implementation", "test-scenario"])
        with self.assertRaises(policy.SectionPolicyError) as error:
            policy.resolve_section_names(["неизвестный раздел"], catalog())
        self.assertEqual(error.exception.code, "SECTION_UNKNOWN")

    def test_checklist_requires_explicit_or_open_status_for_all_items(self):
        section = next(item for item in catalog()["sections"] if item["section_id"] == "technical-implementation")
        checklist = policy.evaluate_checklist(section, [{"item_id": "new-common-modules", "status": "described"}])
        self.assertEqual(checklist[0]["status"], "described")
        self.assertTrue(all(item["status"] == "open" for item in checklist[1:]))

    def test_technical_body_rejects_provenance_but_allows_metadata_names(self):
        clean = "В форме Номенклатуры поле `flow1c_ProductForm` отображается в группе «Планирование»."
        policy.validate_section_content("technical-implementation", clean)
        for fragment in ("iss/9514", "PR !41", "MR !78", "ветка `iss/9902`", "задача 9202"):
            with self.subTest(fragment=fragment), self.assertRaises(policy.SectionPolicyError) as error:
                policy.validate_section_content("technical-implementation", clean + " Источник: " + fragment)
            self.assertEqual(error.exception.code, "SECTION_PROVENANCE_IN_BODY")
        policy.validate_section_content("test-scenario", "Сценарий для PR !41")

    def test_approval_is_invalidated_by_one_character(self):
        state = {"state": "DRAFT_READY", "version": 1, "content_sha256": policy.content_sha256("abc\n")}
        approved = policy.approve_content(state, "abc\n", approved_by="user", approval_statement="Согласовано", approved_at="2026-09-22T00:00:00Z")
        policy.ensure_write_authorized(approved, "abc\n", write_command=True)
        with self.assertRaises(policy.SectionPolicyError) as error:
            policy.ensure_write_authorized(approved, "abcd\n", write_command=True)
        self.assertEqual(error.exception.code, "APPROVAL_STALE")

    def test_state_transitions_and_explicit_write_command(self):
        self.assertEqual(policy.transition_state("COLLECTING_INPUT", "save"), "DRAFT_READY")
        self.assertEqual(policy.transition_state("APPROVED", "write-plan"), "WRITE_PENDING")
        with self.assertRaises(policy.SectionPolicyError) as error:
            policy.ensure_write_authorized({"state": "APPROVED"}, "text", write_command=False)
        self.assertEqual(error.exception.code, "WRITE_COMMAND_REQUIRED")

    def test_section_language_routes_to_draft_not_formal(self):
        selected = base_policy.select_request_mode("functional-spec", "Опиши раздел технической реализации по задаче", None)
        self.assertEqual(selected["mode"], "draft")
        self.assertEqual(base_policy.select_request_mode("functional-section", "Подготовь только тестовый сценарий", None)["mode"], "draft")


if __name__ == "__main__":
    unittest.main()
