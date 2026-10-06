from __future__ import annotations
from flow1c import context as svc_context
from flow1c import sources as svc_sources
from flow1c import system as svc_system
from flow1c.workflow import state as svc_workflow_state
from flow1c.storage import read_json
import sys
from flow1c.storage import write_json
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
import test_flow1c as fixtures
from scripts.flow1c_query_policy import (
    analyze_query,
    compare_optimization,
    infer_query_request_intent,
    select_query_intent,
)
from scripts.flow1c_query_schema import parse_metadata_xml

flow1c = fixtures.flow1c
XML = b"<MetaDataObject><Document><Properties><Name>Order</Name></Properties>\n<ChildObjects><Attribute><Properties><Name>Amount</Name><Type><Type>xs:decimal</Type></Type></Properties></Attribute>\n<TabularSection><Properties><Name>Lines</Name></Properties><ChildObjects>\n<Attribute><Properties><Name>Quantity</Name></Properties></Attribute>\n</ChildObjects></TabularSection></ChildObjects></Document></MetaDataObject>"


class QueryPolicyTests(unittest.TestCase):

    def test_intent_and_exported_fields_are_exact(self) -> None:
        self.assertEqual(select_query_intent("Составь запрос", None), "create")
        self.assertEqual(select_query_intent("Написать запрос 1С", None), "create")
        self.assertEqual(select_query_intent("Улучши производительность", None), "optimize")
        self.assertEqual(select_query_intent("Проверь запрос", None), "review")
        schema = parse_metadata_xml(XML)
        self.assertEqual(schema["tables"][0]["name"], "Документ.Order")
        self.assertEqual(schema["tables"][0]["fields"][0]["type_tokens"], ["xs:decimal"])
        self.assertEqual(schema["tables"][1]["fields"][0]["name"], "Quantity")
        self.assertEqual(schema["completeness"], "PARTIAL")
        namespaced = XML.replace(b"<MetaDataObject>", b'<MetaDataObject xmlns="urn:test">')
        self.assertEqual(parse_metadata_xml(namespaced)["object_name"], "Order")

    def test_only_clear_query_artifact_requests_are_routed(self) -> None:
        self.assertEqual(
            infer_query_request_intent("Написать запрос 1С: документы Реализация товаров и услуг"),
            "create",
        )
        self.assertEqual(infer_query_request_intent("Проверь запрос 1С"), "review")
        self.assertEqual(infer_query_request_intent("Оптимизировать запрос 1С"), "optimize")
        self.assertEqual(infer_query_request_intent("Нужен запрос 1С по регистратору"), "create")
        self.assertIsNone(infer_query_request_intent("Как написать запрос 1С?"))
        self.assertIsNone(infer_query_request_intent("Подготовь запрос к API"))

    def test_query_checker_never_calls_unknown_field_confirmed(self) -> None:
        schema = {**parse_metadata_xml(XML), "id": "QM-001"}
        report = analyze_query(
            "ВЫБРАТЬ O.Amount, O.Amout, O.Number ИЗ Документ.Order КАК O ГДЕ O.Amount > &Minimum",
            [schema],
        )
        fields = {item["name"]: item["status"] for item in report["fields"]}
        self.assertEqual(fields["Amount"], "CONFIRMED_IN_XML")
        self.assertEqual(
            next((item for item in report["fields"] if item["name"] == "Amount"))["type_tokens"],
            ["xs:decimal"],
        )
        self.assertEqual(fields["Amout"], "UNKNOWN")
        self.assertEqual(fields["Number"], "UNKNOWN")
        self.assertEqual(report["parameters"], [{"name": "Minimum", "type": "UNKNOWN"}])
        self.assertEqual(report["check_result"], "PARTIAL")
        self.assertFalse(report["platform_executed"])

    def test_literals_are_not_mistaken_for_fields_and_unclosed_text_fails(self) -> None:
        report = analyze_query('ВЫБРАТЬ "O.Fake" КАК Text ИЗ Документ.Order КАК O', [])
        self.assertEqual(report["fields"], [])
        bad = analyze_query('ВЫБРАТЬ "broken', [])
        self.assertEqual(bad["check_result"], "FAIL")
        self.assertIn("UNCLOSED_STRING", {item["code"] for item in bad["diagnostics"]})

    def test_unsupported_metadata_layout_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one supported"):
            parse_metadata_xml(b"<MetaDataObject/>")
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            parse_metadata_xml(
                b'<!DOCTYPE MetaDataObject [<!ENTITY x "boom">]><MetaDataObject>&x;</MetaDataObject>'
            )

    def test_optimization_flags_changed_parameters(self) -> None:
        before = analyze_query("ВЫБРАТЬ O.Amount ИЗ Документ.Order КАК O ГДЕ O.Amount > &Limit", [])
        after = analyze_query("ВЫБРАТЬ O.Amount ИЗ Документ.Order КАК O", [])
        self.assertIn(
            "BASELINE_PARAMETER_CHANGE",
            {item["code"] for item in compare_optimization(before, after)},
        )

    def test_left_join_filter_and_aggregation_are_risks_not_false_errors(self) -> None:
        query = "ВЫБРАТЬ O.Amount, СУММА(L.Quantity) ИЗ Документ.Order КАК O ЛЕВОЕ СОЕДИНЕНИЕ Документ.Order.Lines КАК L ПО L.Ref = O.Ref ГДЕ L.Quantity > 0"
        report = analyze_query(query, [])
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertIn("OUTER_JOIN_FILTER_RISK", codes)
        self.assertIn("JOIN_CARDINALITY_UNVERIFIED", codes)
        self.assertIn("AGGREGATION_GRAIN_UNVERIFIED", codes)
        self.assertEqual(report["check_result"], "PARTIAL")


class QueryCliTests(unittest.TestCase):
    setUp = fixtures.NaturalLanguageGateTests.setUp
    tearDown = fixtures.NaturalLanguageGateTests.tearDown

    def call(self, command: str, *, expected: int = 0, **data):
        stdout, stderr = (io.StringIO(), io.StringIO())
        with (
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = flow1c.main([command, "--json-stdin"])
        self.assertEqual(code, expected, stderr.getvalue() or stdout.getvalue())
        return json.loads(stdout.getvalue())

    def begin(self, intent: str = "create"):
        return self.call(
            "agent-begin",
            operation="query-analysis",
            mode="explore",
            query_intent=intent,
            summary="Составь запрос по документам",
        )

    def test_create_requires_checked_exact_candidate(self) -> None:
        gate = self.begin()
        self.assertEqual(gate["query_intent"], "create")
        self.assertIn("flow1c_query_check", gate["available_actions"])
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"], summary="Результат")
        query = "ВЫБРАТЬ O.Amount ИЗ Документ.Order КАК O"
        checked = self.call(
            "query-check",
            gate_id=gate["gate_id"],
            text=query,
            expected_result="Одна строка на документ",
            assumptions=[],
        )
        self.assertEqual(checked["report"]["verification_level"], "STATIC")
        self.assertEqual(checked["report"]["check_result"], "PARTIAL")
        repeated = self.call(
            "query-check",
            gate_id=gate["gate_id"],
            text=query,
            expected_result="Одна строка на документ",
            assumptions=[],
        )
        self.assertEqual(repeated["report"]["id"], checked["report"]["id"])
        candidate_path = Path(checked["candidate_path"])
        candidate_path.write_text(query + " changed", encoding="utf-8")
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"], summary="Результат")
        self.call(
            "query-check",
            gate_id=gate["gate_id"],
            text=query,
            expected_result="Одна строка на документ",
        )
        done = self.call("agent-complete", gate_id=gate["gate_id"], summary="Результат")
        self.assertEqual(done["state"], "CONSULTATION_COMPLETE")
        self.assertEqual(done["query_text"], query)
        self.assertFalse(done["query_verification"]["platform_executed"])

    def test_optimize_preserves_baseline_and_blocks_lexical_error(self) -> None:
        gate = self.begin("optimize")
        self.call("query-check", expected=2, gate_id=gate["gate_id"], text="ВЫБРАТЬ 1")
        checked = self.call(
            "query-check",
            gate_id=gate["gate_id"],
            text='ВЫБРАТЬ "broken',
            baseline_text="ВЫБРАТЬ 1",
            expected_result="Одна строка",
            changes=["Убрано лишнее чтение"],
        )
        self.assertEqual(checked["report"]["check_result"], "FAIL")
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"], summary="Ошибочный")
        self.assertEqual(
            (
                svc_workflow_state.request_root(gate, product_root=flow1c.ROOT)
                / "query-baseline.txt"
            ).read_text(encoding="utf-8"),
            "ВЫБРАТЬ 1",
        )

    def test_completion_preserves_crlf_in_checked_text(self) -> None:
        gate = self.begin()
        query = "ВЫБРАТЬ O.Amount\r\nИЗ Документ.Order КАК O"
        self.call(
            "query-check",
            gate_id=gate["gate_id"],
            text=query,
            expected_result="Сумма каждого документа",
        )
        done = self.call("agent-complete", gate_id=gate["gate_id"], summary="Кандидат сохранён")
        self.assertEqual(done["query_text"], query)

    def test_exact_xml_schema_and_stale_source(self) -> None:
        gate = self.begin()
        source = svc_workflow_state.request_root(gate, product_root=flow1c.ROOT) / "object.xml"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(XML)
        gate["artifacts"].append({"path": "object.xml"})
        svc_workflow_state.save_request(gate, product_root=flow1c.ROOT)
        schema = self.call(
            "query-schema", gate_id=gate["gate_id"], source="request", path="object.xml"
        )["schema"]
        query = "ВЫБРАТЬ O.Amount ИЗ Документ.Order КАК O"
        checked = self.call(
            "query-check",
            gate_id=gate["gate_id"],
            text=query,
            schema_ids=[schema["id"]],
            expected_result="Сумма каждого документа",
        )
        self.assertEqual(checked["report"]["fields"][0]["status"], "CONFIRMED_IN_XML")
        source.write_bytes(XML.replace(b"Amount", b"Amout"))
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"], summary="Результат")
        self.call(
            "query-check",
            expected=2,
            gate_id=gate["gate_id"],
            text=query,
            schema_ids=[schema["id"]],
            expected_result="Сумма каждого документа",
        )

    def test_inferred_intent_still_requires_final_check(self) -> None:
        gate = self.call(
            "agent-begin", operation="query-analysis", mode="explore", summary="Проверь запрос"
        )
        self.assertEqual(gate["query_contract_version"], 1)
        self.call(
            "agent-complete", expected=2, gate_id=gate["gate_id"], summary="Непроверенный результат"
        )

    def test_misrouted_1c_query_is_checked(self) -> None:
        gate = self.call(
            "agent-begin",
            operation="consultation",
            mode="explore",
            summary="Написать запрос 1С: документы Реализация товаров и услуг, суммы по регистратору",
        )
        self.assertEqual(gate["requested_operation"], "consultation")
        self.assertEqual(gate["operation"], "query-analysis")
        self.assertEqual(gate["query_intent"], "create")
        self.assertEqual(gate["query_contract_version"], 1)
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"], summary="Готово")

    def test_unavailable_rlm_exposes_static_query_check(self) -> None:
        gate = self.begin()
        with mock.patch.object(svc_sources, "rlm_readiness", return_value=(False, ["offline"])):
            unavailable = self.call(
                "source-query",
                expected=1,
                gate_id=gate["gate_id"],
                source="configuration",
                query="Найти регистр",
                reason="schema",
            )
        self.assertEqual(unavailable["code"], "RLM_SOURCE_UNAVAILABLE")
        self.assertIn("flow1c_query_check", unavailable["available_actions"])
        self.assertEqual(unavailable["next_actions"], ["continue-static-query-check"])
        self.call(
            "query-check",
            gate_id=gate["gate_id"],
            text="ВЫБРАТЬ 1 КАК Число",
            expected_result="Одна строка",
            assumptions=["Схема не проверена"],
        )
        done = self.call("agent-complete", gate_id=gate["gate_id"], summary="Статический кандидат")
        self.assertEqual(done["query_verification"]["verification_level"], "STATIC")

    def test_general_method_remains_consultation(self) -> None:
        gate = self.call(
            "agent-begin",
            operation="consultation",
            mode="explore",
            summary="Как написать запрос 1С для сверки регистров?",
        )
        self.assertEqual(gate["operation"], "consultation")
        self.assertNotIn("query_contract_version", gate)

    def test_existing_legacy_gate_keeps_completion(self) -> None:
        gate = self.begin()
        gate["query_contract_version"] = 0
        svc_workflow_state.save_request(gate, product_root=flow1c.ROOT)
        done = self.call("agent-complete", gate_id=gate["gate_id"], summary="Старый результат")
        self.assertEqual(done["state"], "CONSULTATION_COMPLETE")
        self.assertNotIn("query_text", done)

    def test_current_schema_requires_matching_rlm_path(self) -> None:
        gate = self.begin()
        config = self.root / "configuration"
        (config / "Documents").mkdir(parents=True)
        (config / "Documents" / "Order.xml").write_bytes(XML)
        local = read_json(self.root / svc_context.LOCAL_CONFIG_FILE)
        local["configuration_path"] = str(config)
        write_json(self.root / svc_context.LOCAL_CONFIG_FILE, local)
        evidence_path, evidence = svc_workflow_state.evidence_for_gate(
            gate, product_root=flow1c.ROOT
        )
        evidence["rlm_queries"].append(
            {
                "id": "RLM-001",
                "source": {"kind": "configuration", "path": str(config)},
                "status": "retrieved_unvalidated",
                "result": {"stdout": "Other.xml"},
            }
        )
        write_json(evidence_path, evidence)
        with mock.patch.object(svc_system, "resolve_1c_source_root", return_value=config):
            blocked = self.call(
                "query-schema",
                expected=2,
                gate_id=gate["gate_id"],
                source="configuration",
                path="Documents/Order.xml",
                rlm_evidence_id="RLM-001",
            )
        self.assertIn("does not identify this exact XML path", blocked["errors"][0]["message"])
        evidence["rlm_queries"][0]["result"]["stdout"] = "Documents/Order.xml"
        write_json(evidence_path, evidence)
        with mock.patch.object(svc_system, "resolve_1c_source_root", return_value=config):
            result = self.call(
                "query-schema",
                gate_id=gate["gate_id"],
                source="configuration",
                path="Documents/Order.xml",
                rlm_evidence_id="RLM-001",
            )
        self.assertEqual(result["state"], "CONFIRMED_IN_XML")

    def test_printed_rlm_result_is_not_field_confirmation(self) -> None:
        gate = self.begin()
        config = self.root / "configuration"
        config.mkdir()
        local = read_json(self.root / svc_context.LOCAL_CONFIG_FILE)
        local["configuration_path"] = str(config)
        write_json(self.root / svc_context.LOCAL_CONFIG_FILE, local)
        with (
            mock.patch.object(svc_system, "resolve_1c_source_root", return_value=config),
            mock.patch.object(svc_sources, "rlm_readiness", return_value=(True, [])),
            mock.patch.object(
                svc_sources,
                "rlm_session_execute",
                return_value=({"stdout": "not found", "error": None}, None),
            ),
        ):
            result = self.call(
                "source-query",
                gate_id=gate["gate_id"],
                source="configuration",
                query="Find exact field",
                code="print('not found')",
                reason="schema",
            )
        self.assertEqual(result["state"], "RETRIEVED_UNVALIDATED")
        _, evidence = svc_workflow_state.evidence_for_gate(gate, product_root=flow1c.ROOT)
        self.assertEqual(evidence["rlm_queries"][0]["status"], "retrieved_unvalidated")


if __name__ == "__main__":
    unittest.main()
