from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.opencode_evals import assess_run


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "evals/opencode-natural-language.json").read_text(encoding="utf-8"))
CASE = next(case for case in CASES if case["id"] == "technical-implementation-summary-past-tense")
BODY = """1. Новые общие модули
1.1. Новые общие модули не добавлялись.
2. Изменения свойств общих модулей
2.1. Имя, синоним и признаки общих модулей не изменялись.
3. Новые процедуры и функции
3.1. В модуле формы добавлены процедуры «ПроверкаВвода» и «ПодготовкаДанных».
4. Изменения существующих процедур и функций
4.1. В модуле объекта изменена функция «РасчётИтога».
5. Изменения свойств реквизитов, измерений и ресурсов
5.1. Вне дизайна разработчиком изменено свойство «Индексирование» реквизита «Код».
"""


def messages(*bodies: str) -> list[dict]:
    return [{"parts": [
        {"type": "tool", "callID": "begin", "tool": "flow1c_begin", "state": {"status": "completed"}},
        *[{"type": "tool", "callID": f"save-{index}", "tool": "flow1c_section", "state": {
            "status": "completed", "input": {"action": "save", "section_id": "technical-implementation", "content": body},
            "output": json.dumps({"state": "DRAFT_READY"}),
        }} for index, body in enumerate(bodies)],
        {"type": "tool", "callID": "complete", "tool": "flow1c_complete", "state": {
            "status": "completed", "output": json.dumps({"state": "DRAFT_COMPLETE"}),
        }},
        {"type": "text", "text": BODY},
    ]}]


class TechnicalImplementationEvalTests(unittest.TestCase):
    def test_summary_in_past_tense_passes(self):
        self.assertTrue(assess_run(CASE, messages(BODY), [])["passed"])

    def test_screenshot_present_tense_and_detailed_cards_fail(self):
        for defect in (
            BODY.replace("не добавлялись", "не добавляются"),
            BODY.replace("не изменялись", "не применяются"),
            BODY + "3.2. ПроверкаВвода проверяет заполнение и очищает временные данные.\n",
            BODY + "### Карточка объекта: форма\n",
            BODY + "```bsl\nПроцедура ПроверкаВвода()\nКонецПроцедуры\n```\n",
            BODY.replace("5. Изменения свойств", "Изменения свойств"),
            BODY.replace(" и «ПодготовкаДанных»", "") + "3.2. Добавлена процедура «ПодготовкаДанных».\n",
        ):
            with self.subTest(defect=defect):
                self.assertFalse(assess_run(CASE, messages(defect), [])["passed"])

    def test_latest_saved_body_is_checked_even_when_final_prose_is_correct(self):
        defect = BODY.replace("не добавлялись", "не добавляются")
        self.assertFalse(assess_run(CASE, messages(BODY, defect), [])["passed"])
        self.assertTrue(assess_run(CASE, messages(defect, BODY), [])["passed"])

    def test_final_prose_and_other_sections_cannot_substitute_for_saved_body(self):
        trace = messages(BODY)
        trace[0]["parts"][1]["state"]["input"]["section_id"] = "system-settings"
        self.assertIn("saved section text is unavailable", assess_run(CASE, trace, [])["failures"])

    def test_blocked_or_failed_save_cannot_count_as_saved_content(self):
        for status, output in (("completed", '{"state": "BLOCKED"}'),
                               ("error", '{"state": "DRAFT_READY"}'),
                               ("completed", "invalid json")):
            with self.subTest(status=status, output=output):
                trace = messages(BODY)
                trace[0]["parts"][1]["state"].update(status=status, output=output)
                self.assertFalse(assess_run(CASE, trace, [])["passed"])


if __name__ == "__main__":
    unittest.main()
