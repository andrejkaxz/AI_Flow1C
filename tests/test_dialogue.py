from __future__ import annotations

import io
import json
import os
import ctypes
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import test_flow1c as fixtures

flow1c = fixtures.flow1c


class DialogueTests(unittest.TestCase):
    setUp = fixtures.NaturalLanguageGateTests.setUp
    tearDown = fixtures.NaturalLanguageGateTests.tearDown
    make_work_item = fixtures.NaturalLanguageGateTests.make_work_item

    def call(self, command, *, expected=0, **data):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(flow1c.sys, "stdin", io.StringIO(json.dumps(data))), redirect_stdout(stdout), redirect_stderr(stderr):
            code = flow1c.main([command, "--json-stdin"])
        self.assertEqual(code, expected, stderr.getvalue() or stdout.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else {"error": stderr.getvalue()}

    def begin(self, **kwargs):
        return self.call("agent-begin", **{"operation": "functional-spec", "mode": "draft", "summary": "Подготовить описание приёмки товара", **kwargs})

    def document(self, gate, content="# Приёмка\n\nОператор сверяет количество товара с накладной."):
        return self.call("agent-write", gate_id=gate["gate_id"], target="draft", path="result.md", content=content)

    def configure_extension(self):
        extension = self.root / "extension"
        extension.mkdir(exist_ok=True)
        local = flow1c.read_json(self.root / flow1c.LOCAL_CONFIG_FILE)
        local["extension_path"] = str(extension)
        flow1c.write_json(self.root / flow1c.LOCAL_CONFIG_FILE, local)
        return extension

    def test_chat_only_draft_needs_no_setup_or_rlm(self):
        (self.root / flow1c.LOCAL_CONFIG_FILE).unlink()
        (self.root / flow1c.CONFIG_FILE).unlink()
        with mock.patch.object(flow1c, "rlm_readiness", side_effect=AssertionError("must not call RLM")):
            gate = self.begin()
            self.call("agent-dialogue", gate_id=gate["gate_id"], action="record", answer="Оператор сверяет количество", kind="user_answer")
            result = self.document(gate)
            done = self.call("agent-complete", gate_id=gate["gate_id"])
        self.assertIn(".workspace", result["absolute_path"])
        self.assertEqual(done["state"], "DRAFT_COMPLETE")
        self.assertEqual(done["document_status"], "UNVERIFIED_DRAFT")
        self.assertFalse((self.root / "work-items").exists())
        saved = json.loads((flow1c.request_root(gate) / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["notes"][0]["text"], "Оператор сверяет количество")

    def test_new_and_resumed_draft_gates_publish_current_actions(self):
        gate = self.begin()
        self.assertIn("flow1c_source_read", gate["available_actions"])
        path = flow1c.gate_path(gate["gate_id"])
        stale = flow1c.read_json(path)
        stale["available_actions"] = [item for item in stale["available_actions"] if item != "flow1c_source_read"]
        stale["answers"] = [{"question": "saved", "answer": "value"}]
        flow1c.write_json(path, stale)

        resumed = flow1c.load_gate(gate["gate_id"])

        self.assertIn("flow1c_source_read", resumed["available_actions"])
        self.assertEqual(resumed["answers"], stale["answers"])
        self.assertEqual(flow1c.read_json(path)["available_actions"], resumed["available_actions"])

    def test_numeric_code_review_ref_defaults_to_explore_without_registry_lookup(self):
        with mock.patch.object(flow1c, "load_manifest", side_effect=AssertionError("registry/work item lookup is forbidden")):
            gate = self.call(
                "agent-begin", operation="code-review", code="9760",
                summary="Проведи review ветки 9760",
            )

        self.assertEqual(gate["mode"], "explore")
        self.assertEqual(gate["git_ref"], "9760")
        self.assertIsNone(gate["task_reference"])
        self.assertIsNone(gate["work_reference"])
        self.assertEqual(gate["mode_selection"]["reason"], "read_only_review_default")
        self.assertEqual(gate["mode_history"], [gate["mode_selection"]])

    def test_free_git_inspection_resolves_numeric_branch(self):
        extension = self.configure_extension()
        subprocess.run(["git", "init", "-b", "main"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=extension, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=extension, check=True)
        (extension / "module.bsl").write_text("Процедура Тест()\nКонецПроцедуры", encoding="utf-8")
        subprocess.run(["git", "add", "module.bsl"], cwd=extension, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "branch", "9760"], cwd=extension, check=True)
        subprocess.run(["git", "switch", "9760"], cwd=extension, check=True, capture_output=True)
        (extension / "module.bsl").write_text("Процедура Тест()\n    Возврат;\nКонецПроцедуры", encoding="utf-8")
        subprocess.run(["git", "add", "module.bsl"], cwd=extension, check=True)
        subprocess.run(["git", "commit", "-m", "feature"], cwd=extension, check=True, capture_output=True)
        feature_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=extension, check=True,
                                        text=True, capture_output=True).stdout.strip()
        subprocess.run(["git", "switch", "main"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "merge", "--no-ff", "9760", "-m", "merge 9760"], cwd=extension, check=True, capture_output=True)
        merge_commit = subprocess.run(["git", "rev-parse", "main"], cwd=extension, check=True,
                                      text=True, capture_output=True).stdout.strip()
        gate = self.call("agent-begin", operation="code-review", code="9760", summary="Проведи review ветки 9760")

        result = self.call("agent-git-inspect", gate_id=gate["gate_id"], repository="extension", action="resolve")

        self.assertEqual(result["state"], "READ")
        self.assertEqual(result["commit"], feature_commit)
        self.assertFalse(result["is_merge"])
        integration = self.call(
            "agent-git-inspect", gate_id=gate["gate_id"], repository="extension",
            action="integration", git_refs=["9760"], target_ref="main",
        )
        found = integration["integrations"][0]
        self.assertEqual(found["status"], "MERGE_FOUND")
        self.assertEqual(found["source_commit"], feature_commit)
        self.assertEqual(found["merge_commit"], merge_commit)
        self.assertEqual(found["review_ref"], merge_commit)
        diff = self.call(
            "agent-git-inspect", gate_id=gate["gate_id"], repository="extension",
            action="diff", git_ref=found["review_ref"],
        )
        self.assertIn("module.bsl", "\n".join(diff["files"]))
        self.assertIn("Возврат", diff["diff"])
        evidence = flow1c.read_json(Path(gate["evidence_path"]))
        self.assertEqual(evidence["git_reads"][0]["git_ref"], "9760")
        self.assertEqual([item["action"] for item in evidence["git_reads"]], ["resolve", "integration", "diff"])

    def test_git_integration_batches_fast_forward_unmerged_and_missing_refs(self):
        extension = self.configure_extension()
        subprocess.run(["git", "init", "-b", "main"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=extension, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=extension, check=True)
        (extension / "module.bsl").write_text("base", encoding="utf-8")
        subprocess.run(["git", "add", "module.bsl"], cwd=extension, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "switch", "-c", "ff-branch"], cwd=extension, check=True, capture_output=True)
        (extension / "module.bsl").write_text("fast-forward", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "ff feature"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "switch", "main"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "merge", "--ff-only", "ff-branch"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "switch", "-c", "open-branch"], cwd=extension, check=True, capture_output=True)
        (extension / "module.bsl").write_text("not merged", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "open feature"], cwd=extension, check=True, capture_output=True)
        subprocess.run(["git", "switch", "main"], cwd=extension, check=True, capture_output=True)
        gate = self.call(
            "agent-begin", operation="code-review", git_ref="ff-branch",
            summary="Проведи review веток ff-branch и open-branch",
        )

        result = self.call(
            "agent-git-inspect", gate_id=gate["gate_id"], repository="extension", action="integration",
            git_refs=["ff-branch", "open-branch", "missing-branch"], target_ref="main",
        )

        statuses = {item["git_ref"]: item["status"] for item in result["integrations"]}
        self.assertEqual(statuses, {
            "ff-branch": "NO_MERGE_COMMIT", "open-branch": "NOT_MERGED",
            "missing-branch": "SOURCE_NOT_FOUND",
        })

    def test_formal_missing_item_does_not_accept_an_invented_answer(self):
        gate = self.begin(mode="formal", expected=2)
        self.assertEqual(gate["state"], "BLOCKED")
        self.assertTrue(gate["awaiting_user_input"])

        result = self.call(
            "agent-dialogue", expected=2, gate_id=gate["gate_id"], action="answer",
            answer="Продолжить", resolution="independent-draft",
        )

        self.assertEqual(result["state"], "BLOCKED")
        saved = flow1c.load_gate(gate["gate_id"])
        self.assertEqual(saved["state"], "BLOCKED")
        self.assertEqual(saved.get("answers", []), [])

    def test_free_mode_cannot_open_publication_gate(self):
        gate = self.call(
            "agent-begin", expected=1, operation="publish", mode="explore",
            summary="Посмотреть возможность публикации",
        )
        self.assertEqual(gate["state"], "BLOCKED")
        self.assertNotIn("flow1c_action", gate["available_actions"])

    def test_explore_cannot_read_exact_extension_source(self):
        extension = self.configure_extension()
        (extension / "Module.bsl").write_text("Сообщить(\"test\");", encoding="utf-8")
        gate = self.begin(operation="workflow-review", mode="explore")

        result = self.call("agent-source-read", expected=2, gate_id=gate["gate_id"],
                           source="extension", path="Module.bsl")

        self.assertIn("unavailable in explore mode", result["errors"][0]["message"])

    def test_draft_source_read_records_deduplicated_evidence_and_can_complete(self):
        extension = self.configure_extension()
        source = extension / "CommonModules" / "Orders.bsl"
        source.parent.mkdir()
        source.write_text("Процедура Выполнить()\nКонецПроцедуры", encoding="utf-8")
        original = source.read_bytes()
        gate = self.begin()

        first = self.call("agent-source-read", gate_id=gate["gate_id"], source="extension",
                          path="CommonModules/Orders.bsl", max_chars=1000)
        second = self.call("agent-source-read", gate_id=gate["gate_id"], source="extension",
                           path="CommonModules/Orders.bsl", max_chars=1000)

        evidence = flow1c.read_json(Path(gate["evidence_path"]))
        self.assertEqual(first["content"], "Процедура Выполнить()\nКонецПроцедуры")
        self.assertEqual(first["sha256"], flow1c.sha256(source))
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        self.assertEqual(len(evidence["source_reads"]), 1)
        self.assertEqual(evidence["source_reads"][0]["path"], "CommonModules/Orders.bsl")
        self.assertEqual(evidence["source_reads"][0]["source"], "extension")
        self.assertIn("created_at", evidence["source_reads"][0])
        self.assertEqual(source.read_bytes(), original)
        self.document(gate, "# Черновик\n\nUNVERIFIED_DRAFT\n\nТочный файл расширения прочитан для анализа.")
        completed = self.call("agent-complete", gate_id=gate["gate_id"])
        self.assertEqual(completed["state"], "DRAFT_COMPLETE")
        self.assertEqual(completed["document_status"], "UNVERIFIED_DRAFT")

    def test_draft_source_read_preserves_path_type_size_and_source_guards(self):
        extension = self.configure_extension()
        (extension / "large.xml").write_text("x" * 20, encoding="utf-8")
        (extension / "payload.exe").write_bytes(b"binary")
        outside = self.root / "outside.bsl"
        outside.write_text("outside", encoding="utf-8")
        gate = self.begin()

        cases = [
            ({"source": "configuration", "path": "large.xml"}, "configuration reads are forbidden"),
            ({"source": "extension", "path": "../outside.bsl"}, "outside the configured checkout"),
            ({"source": "extension", "path": "missing.bsl"}, "does not exist"),
            ({"source": "extension", "path": "payload.exe"}, "Only BSL, XML, JSON and Markdown"),
            ({"source": "extension", "path": "large.xml", "max_chars": 5}, "Source exceeds 5 characters"),
        ]
        for values, message in cases:
            with self.subTest(values=values):
                result = self.call("agent-source-read", expected=2, gate_id=gate["gate_id"], **values)
                self.assertIn(message, result["errors"][0]["message"])

        self.assertEqual(flow1c.read_json(Path(gate["evidence_path"]))["source_reads"], [])

    def test_missing_or_invalid_extension_path_needs_input_without_blocking_draft(self):
        gate = self.begin()
        for extension_path, code in ((None, "EXTENSION_PATH_NOT_CONFIGURED"),
                                     (str(self.root / "missing-extension"), "EXTENSION_PATH_INVALID")):
            local = flow1c.read_json(self.root / flow1c.LOCAL_CONFIG_FILE)
            if extension_path is None:
                local.pop("extension_path", None)
            else:
                local["extension_path"] = extension_path
            flow1c.write_json(self.root / flow1c.LOCAL_CONFIG_FILE, local)

            result = self.call("agent-source-read", expected=1, gate_id=gate["gate_id"],
                               source="extension", path="Module.bsl")

            self.assertEqual(result["state"], "NEEDS_INPUT")
            self.assertEqual(result["available_actions"], ["flow1c_dialogue"])
            self.assertEqual(result["errors"][0]["code"], code)
            self.assertEqual(flow1c.load_gate(gate["gate_id"])["state"], "READY")

    def test_record_replaces_lone_surrogates_and_keeps_utf8_json_valid(self):
        gate = self.begin()

        result = self.call(
            "agent-dialogue",
            gate_id=gate["gate_id"],
            action="record",
            answer="Пользователь сообщил: \udc98",
            kind="user_answer",
        )

        self.assertEqual(result["notes"][0]["text"], "Пользователь сообщил: \ufffd")
        saved_path = flow1c.gate_path(gate["gate_id"])
        saved_bytes = saved_path.read_bytes()
        saved = json.loads(saved_bytes.decode("utf-8"))
        self.assertEqual(saved["notes"][0]["text"], "Пользователь сообщил: \ufffd")
        self.assertNotIn(b"\xed\xb2\x98", saved_bytes)

    def test_json_stdin_is_reconfigured_from_ansi_to_utf8(self):
        payload = json.dumps({"answer": "Пользователь"}, ensure_ascii=False).encode("utf-8")
        stream = io.TextIOWrapper(io.BytesIO(payload), encoding="cp1251")
        try:
            with mock.patch.object(flow1c.sys, "stdin", stream):
                flow1c.configure_stdio_utf8()
                self.assertEqual(flow1c.read_json_stdin()["answer"], "Пользователь")
        finally:
            stream.detach()

    def test_question_answer_resume_preserves_gate_and_rejects_repeat(self):
        gate = self.begin()
        waiting = self.call("agent-dialogue", gate_id=gate["gate_id"], action="ask", question="Кто принимает товар?")
        self.assertEqual(waiting["state"], "WAITING_USER")
        self.assertEqual(flow1c.load_gate(gate["gate_id"])["state"], "WAITING_USER")
        self.call("agent-write", expected=2, gate_id=gate["gate_id"], target="draft", path="result.md", content="too soon")
        resumed = self.call("agent-dialogue", gate_id=gate["gate_id"], action="answer", answer="Кладовщик")
        self.assertEqual(resumed["gate_id"], gate["gate_id"])
        self.assertEqual(resumed["state"], "READY")
        self.call("agent-dialogue", expected=2, gate_id=gate["gate_id"], action="ask", question="Кто принимает товар?")

    def test_user_can_decline_document_gate_inputs_and_finish_with_visible_deviation(self):
        self.make_work_item()
        gate = self.begin(mode="formal", code="G-001", expected=1)
        self.assertEqual(gate["state"], "NEEDS_INPUT")
        deviated = self.call(
            "agent-dialogue", gate_id=gate["gate_id"], action="deviate",
            answer="Пользователь отказался предоставлять дополнительные материалы и принимает риск черновика.",
            deviation_type="process", scope="gate",
            user_statement="Пользователь отказался предоставлять дополнительные материалы и принимает риск черновика.",
            condition_ids=[item["id"] for item in gate["conditions"] if item["waivable"]],
        )
        self.assertEqual(deviated["state"], "READY_WITH_DEVIATIONS")
        written = self.call(
            "agent-write", gate_id=gate["gate_id"], target="work-item",
            path="specification/functional-spec.md", content="# Черновая ФС\n\nДоступных данных достаточно для предварительного описания.",
        )
        self.assertEqual(written["state"], "WRITTEN")
        content = Path(written["absolute_path"]).read_text(encoding="utf-8")
        self.assertIn("UNVERIFIED_DRAFT", content)
        evidence = flow1c.read_json(Path(deviated["evidence_path"]))
        evidence["context_sha256"] = "a" * 64
        evidence["rlm_queries"] = [{"evidence_id": "RLM-test", "result": {"stdout": "ok"}}]
        flow1c.write_json(Path(deviated["evidence_path"]), evidence)
        completed = self.call("agent-complete", gate_id=gate["gate_id"])
        self.assertEqual(completed["state"], "COMPLETE_WITH_DEVIATIONS")
        self.assertFalse(completed["ready"])
        self.assertEqual(completed["compliance"], "DEVIATED")

    def test_declining_missing_reference_continues_as_independent_draft(self):
        gate = self.begin(mode="formal", expected=2)
        self.assertEqual(gate["state"], "BLOCKED")
        self.assertTrue(gate["awaiting_user_input"])
        self.assertEqual(gate["clarification"]["options"], ["provide-reference", "create-provisional", "independent-draft"])
        deviated = self.call(
            "agent-dialogue", gate_id=gate["gate_id"], action="answer", resolution="independent-draft",
            answer="Отдельного номера нет; продолжить без формальной привязки.",
        )
        self.assertEqual(deviated["mode"], "draft")
        self.assertEqual(deviated["state"], "READY_WITH_DEVIATIONS")
        self.assertIsNone(deviated["code"])
        self.assertEqual(deviated["compliance"], "DEVIATED")
        self.document(deviated)
        completed = self.call("agent-complete", gate_id=gate["gate_id"])
        self.assertEqual(completed["state"], "DRAFT_COMPLETE")
        self.assertEqual(completed["compliance"], "DEVIATED")
        self.assertTrue(completed["deviations"])

    def test_registry_bypass_creates_provisional_work_item_and_deviated_document(self):
        gate = self.begin(mode="formal", code="USER-TASK-7", expected=2)
        self.assertEqual(gate["state"], "BLOCKED")
        bypassed = self.call(
            "agent-dialogue", gate_id=gate["gate_id"], action="deviate",
            deviation_type="registry_bypass", actor="user", scope="work-item",
            answer="Пользователь согласовал работу без валидированного реестра.",
            user_statement="Пользователь согласовал работу без валидированного реестра.",
            condition_ids=["missing:registry_traceability"],
        )
        self.assertEqual(bypassed["state"], "NEEDS_CONFIRMATION")
        started = self.call(
            "agent-action", gate_id=gate["gate_id"], action="provisional-start",
            parameters_json=json.dumps({"task_reference": "USER-TASK-7", "title": "Приёмка товара",
                                        "user_brief": "Описать приёмку товара без реестра."}),
        )
        self.assertEqual(started["traceability_mode"], "provisional")
        item = flow1c.work_item_root("USER-TASK-7")
        manifest = flow1c.read_json(item / "manifest.yaml")
        self.assertEqual(manifest["registry"]["status"], "bypassed")
        self.assertEqual(manifest["requirements"], [])
        self.assertTrue((item / "input" / "user-brief.md").is_file())
        self.assertTrue((item / "input" / "artifacts.json").is_file())

        gate = self.begin(mode="formal", code="USER-TASK-7", allow_incomplete_draft=True, expected=1)
        self.assertEqual(gate["state"], "NEEDS_INPUT")
        statement = "Продолжить provisional-черновик без дополнительных материалов."
        gate = self.call(
            "agent-dialogue", gate_id=gate["gate_id"], action="deviate",
            answer=statement, deviation_type="process", scope="gate", user_statement=statement,
            condition_ids=[item["id"] for item in gate["conditions"] if item["waivable"]],
        )
        self.assertEqual(gate["state"], "READY_WITH_DEVIATIONS")

        written = self.call(
            "agent-write", gate_id=gate["gate_id"], target="work-item",
            path="specification/functional-spec.md", content="# ФС\n\nПредварительное описание приёмки.",
        )
        self.assertIn("UNVERIFIED_DRAFT", Path(written["absolute_path"]).read_text(encoding="utf-8"))
        evidence = flow1c.read_json(Path(gate["evidence_path"]))
        evidence["context_sha256"] = "a" * 64
        evidence["rlm_queries"] = [{"evidence_id": "RLM-test", "result": {"stdout": "ok"}}]
        flow1c.write_json(Path(gate["evidence_path"]), evidence)
        completed = self.call("agent-complete", gate_id=gate["gate_id"])
        self.assertEqual(completed["state"], "COMPLETE_WITH_DEVIATIONS")
        self.assertFalse(completed["ready"])

    def test_deviation_does_not_waive_later_evidence_failures(self):
        self.make_work_item()
        gate = self.begin(mode="formal", code="G-001", expected=1)
        statement = "Продолжить без дополнительных входных материалов."
        gate = self.call(
            "agent-dialogue", gate_id=gate["gate_id"], action="deviate",
            answer=statement, deviation_type="process", scope="gate", user_statement=statement,
            condition_ids=[item["id"] for item in gate["conditions"] if item["waivable"]],
        )
        self.call(
            "agent-write", gate_id=gate["gate_id"], target="work-item",
            path="specification/functional-spec.md", content="# Черновая ФС\n\nПредварительное описание.",
        )

        completed = self.call("agent-complete", expected=2, gate_id=gate["gate_id"])

        self.assertEqual(completed["state"], "NON_COMPLIANT")
        self.assertIn("role context was not built", completed["errors"])
        self.assertIn("no gated RLM evidence was recorded", completed["errors"])

    def test_deviated_free_request_accepts_folder_without_artifacts_field(self):
        gate = self.begin(mode="formal", expected=2)
        deviated = self.call(
            "agent-dialogue", gate_id=gate["gate_id"], action="answer", resolution="independent-draft",
            answer="Отдельного номера нет; продолжить без формальной привязки.",
        )
        folder = self.root / "free request sources"
        folder.mkdir()
        (folder / "source.md").write_text("Материал свободного запроса", encoding="utf-8")

        accepted = self.call("artifact-intake", gate_id=gate["gate_id"], source=[str(folder)])

        self.assertEqual(accepted["state"], "ACCEPTED")
        self.assertEqual(accepted["copied"][0]["name"], "source.md")
        saved = flow1c.load_gate(gate["gate_id"])
        self.assertEqual(saved["artifacts"], accepted["copied"])

    def test_mismatch_keeps_registry_and_work_item_unchanged(self):
        item = self.make_work_item()
        original = (item / "manifest.yaml").read_bytes()
        gate = self.begin(code="G-001", requirements=["REQ-999"], expected=1)
        self.assertEqual(gate["clarification"]["reason"], "requirement_mismatch")
        self.call("agent-dialogue", expected=2, gate_id=gate["gate_id"], action="answer", answer="Продолжай")
        resumed = self.call("agent-dialogue", gate_id=gate["gate_id"], action="answer", answer="Отдельный черновик", resolution="independent-draft")
        self.assertIsNone(resumed["code"])
        self.assertIsNone(resumed["reference_code"])
        self.document(resumed)
        self.assertEqual(original, (item / "manifest.yaml").read_bytes())

    def test_formal_semantic_mismatch_can_start_independent_draft(self):
        self.make_work_item()
        gate = self.begin(mode="formal", code="G-001", mismatch="Запрошен другой процесс", expected=1)
        result = self.call("agent-dialogue", gate_id=gate["gate_id"], action="answer", answer="Независимый черновик", resolution="independent-draft")
        self.assertEqual(result["mode"], "draft")
        self.assertNotEqual(result["gate_id"], gate["gate_id"])
        self.document(result)

    def test_folder_intake_and_bounded_search(self):
        folder = self.root / "arbitrary user folder"
        folder.mkdir()
        (folder / "source.md").write_text("Оператор принимает товар", encoding="utf-8")
        gate = self.begin()
        accepted = self.call("artifact-intake", gate_id=gate["gate_id"], source=[str(folder)])
        self.assertEqual(accepted["next"], "continue_same_gate")
        found = self.call("agent-inspect", gate_id=gate["gate_id"], query="принимает", scope="request")
        self.assertTrue(any("Оператор" in item["content"] for item in found["items"]))
        self.call("agent-inspect", expected=2, gate_id=gate["gate_id"], path="../.flow1c.local.json", scope="workflow")
        self.call("agent-inspect", expected=2, gate_id=gate["gate_id"], path=".flow1c.local.json", scope="workflow")

    def test_inbox_docx_joins_free_gate_and_retries_extraction(self):
        source = self.root / "spec.docx"
        source.write_bytes(b"example docx")
        inbox = flow1c.persist_intake_files(
            [(source, Path(source.name))], [], code=None, category="redmine_attachments",
            received_via="redmine", source_record={"system": "redmine", "issue_id": 42},
        )
        self.assertIsNone(inbox["copied"][0]["derived_path"])
        gate = self.call("agent-begin", operation="consultation", mode="explore", summary="Проверить спецификацию")
        with mock.patch.dict(flow1c.sys.modules, {"markitdown": None}):
            failed = self.call("artifact-intake", gate_id=gate["gate_id"], promote_intake_id=inbox["intake_id"])
        self.assertEqual(failed["copied"][0]["extraction_status"], "failed")
        self.assertEqual(len(flow1c.load_gate(gate["gate_id"])["artifacts"]), 1)

        class Converter:
            def convert(self, path):
                return types.SimpleNamespace(text_content="Текст спецификации")

        with mock.patch.dict(flow1c.sys.modules, {"markitdown": types.SimpleNamespace(MarkItDown=Converter)}):
            recovered = self.call("artifact-intake", gate_id=gate["gate_id"], promote_intake_id=inbox["intake_id"])
        record = recovered["copied"][0]
        self.assertEqual(record["extraction_status"], "ready")
        self.assertEqual(record["source"]["issue_id"], 42)
        self.assertEqual(len(flow1c.load_gate(gate["gate_id"])["artifacts"]), 1)
        read = self.call("agent-inspect", gate_id=gate["gate_id"], scope="request", path=record["derived_path"])
        self.assertIn("Текст спецификации", read["items"][0]["content"])
        self.assertTrue((self.root / inbox["copied"][0]["relative_path"]).is_file())

    def test_folder_intake_excludes_its_request_destination(self):
        documentation = self.root / "documentation"
        documentation.mkdir()
        (documentation / "source.md").write_text("Source outside the request", encoding="utf-8")
        local = flow1c.read_json(self.root / flow1c.LOCAL_CONFIG_FILE)
        local["documentation_path"] = str(documentation)
        flow1c.write_json(self.root / flow1c.LOCAL_CONFIG_FILE, local)
        gate = self.begin()

        accepted = self.call("artifact-intake", gate_id=gate["gate_id"], source=[str(documentation)])

        self.assertEqual(len(accepted["copied"]), 1)
        self.assertEqual(accepted["copied"][0]["name"], "source.md")
        request_root = flow1c.request_root(gate)
        self.assertTrue(any(flow1c.path_is_within(Path(path), request_root) for path in accepted["skipped"]))

    def test_free_intake_shortens_long_destination_name(self):
        folder = self.root / "incoming"
        folder.mkdir()
        source = folder / (("long-name-" * 18) + ".md")
        source.write_text("Long path evidence", encoding="utf-8")
        gate = self.begin()

        accepted = self.call("artifact-intake", gate_id=gate["gate_id"], source=[str(source)])

        record = accepted["copied"][0]
        target = flow1c.request_root(gate) / record["path"]
        self.assertEqual(record["name"], source.name)
        self.assertNotEqual(target.name, source.name)
        self.assertLessEqual(len(str(target)), flow1c.MAX_STORED_ARTIFACT_PATH)
        self.assertEqual(target.read_text(encoding="utf-8"), "Long path evidence")

    def test_unavailable_rlm_does_not_block_general_draft(self):
        gate = self.begin()
        with mock.patch.object(flow1c, "rlm_readiness", return_value=(False, ["offline"])):
            result = self.call("source-query", expected=1, gate_id=gate["gate_id"], source="configuration", query="object", reason="source")
        self.assertEqual(result["state"], "NEEDS_INPUT")
        self.assertEqual(result["code"], "RLM_SOURCE_UNAVAILABLE")
        self.assertEqual(result["next_actions"], ["bootstrap-rlm-if-missing", "start-rlm", "ensure-source-indexes", "retry-source-query"])
        self.document(gate)
        self.call("agent-complete", gate_id=gate["gate_id"])

    def test_missing_or_heading_only_document_cannot_complete(self):
        gate = self.begin()
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"])
        self.document(gate, "# Empty heading")
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"])

    def test_explore_result_and_no_extension_mutation(self):
        gate = self.begin(operation="workflow-review", mode="explore")
        self.call("agent-write", expected=2, gate_id=gate["gate_id"], target="extension", path="x.bsl", content="x")
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"])
        result = self.call("agent-complete", gate_id=gate["gate_id"], summary="Предложено упростить диалог")
        self.assertEqual(result["state"], "CONSULTATION_COMPLETE")
        blocked = self.begin(operation="development", expected=1)
        self.assertEqual(blocked["state"], "BLOCKED")

    def test_independent_gates_keep_separate_sources(self):
        first, second = self.begin(), self.begin()
        self.assertNotEqual(first["evidence_path"], second["evidence_path"])
        source = self.root / "private-request.md"
        source.write_text("Only first request", encoding="utf-8")
        self.call("artifact-intake", gate_id=first["gate_id"], source=[str(source)])
        found = self.call("agent-inspect", gate_id=second["gate_id"], query="Only first request")
        self.assertEqual(found["items"], [])

    def test_formal_gates_do_not_overwrite_evidence(self):
        self.make_work_item()
        kwargs = {"code": "G-001", "mode": "formal", "allow_incomplete_draft": True, "expected": 1}
        first = self.begin(**kwargs)
        original = Path(first["evidence_path"]).read_bytes()
        second = self.begin(**kwargs)
        self.assertNotEqual(first["evidence_path"], second["evidence_path"])
        self.assertEqual(Path(first["evidence_path"]).read_bytes(), original)

    def test_legacy_gate_requires_reassessment_and_is_preserved(self):
        gate = self.begin()
        path = flow1c.gate_path(gate["gate_id"])
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("policy_version")
        flow1c.write_json(path, legacy)
        before = path.read_bytes()
        self.call("agent-complete", expected=2, gate_id=gate["gate_id"])
        self.assertEqual(before, path.read_bytes())

    def test_promotion_copies_provenance_without_status_change(self):
        item = self.make_work_item()
        flow1c.write_json(self.root / "registry/normalized/requirements.json", {"REQ-001": {"text": "Requirement"}})
        flow1c.write_json(self.root / "registry/normalized/links.json", {"requirement_to_specification": {"REQ-001": "G-001"}})
        original = (item / "manifest.yaml").read_bytes()
        gate = self.begin()
        self.document(gate)
        self.call("agent-complete", gate_id=gate["gate_id"])
        self.call("draft-promote", expected=2, gate_id=gate["gate_id"], code="G-001", requirements=["REQ-001"])
        self.call("draft-promote", expected=2, gate_id=gate["gate_id"], code="G-001", requirements=["REQ-999"], confirmed=True)
        attached = self.call("draft-promote", gate_id=gate["gate_id"], code="G-001", requirements=["REQ-001"], confirmed=True)
        self.assertTrue((Path(attached["destination"]) / "provenance.json").is_file())
        self.assertEqual((item / "manifest.yaml").read_bytes(), original)
        self.assertTrue((flow1c.request_root(gate) / "result.md").exists())

    def test_blocked_publish_never_calls_external_handler(self):
        self.make_work_item()
        gate = self.begin(operation="publish", mode="formal", code="G-001", expected=2)
        with mock.patch.object(flow1c, "cmd_pr_create") as publish:
            self.call("agent-action", expected=2, gate_id=gate["gate_id"], action="publish", parameters_json='{"confirmed": true}')
        publish.assert_not_called()

    def test_update_action_forwards_repair_and_external_update_flags(self):
        gate = self.begin(operation="update", mode="formal")
        completed = flow1c.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"state": "READY", "ready": True}), stderr="",
        )
        with mock.patch.object(flow1c, "command_path", return_value="powershell.exe"), \
                mock.patch.object(flow1c, "run_update_command", return_value=completed) as run:
            result = self.call(
                "agent-action", gate_id=gate["gate_id"], action="update",
                parameters_json=json.dumps({
                    "confirmed": True,
                    "allowExternalUpdates": True,
                    "repair_prerequisites": True,
                }),
            )
        command = run.call_args.args[0]
        self.assertIn("-AllowExternalUpdates", command)
        self.assertIn("-RepairPrerequisites", command)
        self.assertEqual(result["state"], "READY")
        self.assertEqual(flow1c.load_gate(gate["gate_id"])["action_completed"], "update")

    @unittest.skipUnless(os.name == "nt", "Windows process handle regression")
    def test_update_command_returns_after_parent_exits_with_background_child(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "parent.py"
            parent.write_text(
                "import subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], "
                "stdout=sys.stdout, stderr=sys.stderr)\n"
                "print(f'{child.pid}|update complete')\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            result = flow1c.run_update_command([sys.executable, str(parent)])
            elapsed = time.monotonic() - started
            child_id, message = result.stdout.strip().split("|", 1)
            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(child_id))
            try:
                if handle:
                    ctypes.windll.kernel32.WaitForSingleObject(handle, 5000)
            finally:
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
            self.assertLess(elapsed, 2.5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(message, "update complete")

    def test_update_available_does_not_mark_gate_complete(self):
        gate = self.begin(operation="update", mode="formal")
        available = flow1c.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"state": "UPDATE_AVAILABLE", "ready": False}), stderr="",
        )
        with mock.patch.object(flow1c, "command_path", return_value="powershell.exe"), \
                mock.patch.object(flow1c, "run_update_command", return_value=available):
            self.call(
                "agent-action", gate_id=gate["gate_id"], action="update",
                parameters_json='{"confirmed": true}',
            )
        self.assertIsNone(flow1c.load_gate(gate["gate_id"]).get("action_completed"))

    def test_update_completion_retries_after_invalid_output_without_closing_gate(self):
        gate = self.begin(operation="update", mode="formal")
        ready = flow1c.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"state": "READY", "ready": True}), stderr="",
        )
        with mock.patch.object(flow1c, "command_path", return_value="powershell.exe"), \
                mock.patch.object(flow1c, "run_update_command", return_value=ready):
            self.call("agent-action", gate_id=gate["gate_id"], action="update",
                      parameters_json='{"confirmed": true}')

        invalid = self.call("agent-complete", expected=2, gate_id=gate["gate_id"], output="unexpected.md")
        self.assertEqual(invalid["state"], "BLOCKED")
        self.assertIn("Omit output and retry", invalid["errors"][0]["message"])
        saved = flow1c.load_gate(gate["gate_id"])
        self.assertEqual(saved["state"], "READY")
        self.assertEqual(saved["action_completed"], "update")

        completed = self.call("agent-complete", gate_id=gate["gate_id"])
        self.assertEqual(completed["state"], "COMPLETE")

    def test_legacy_update_output_override_gate_can_complete_once(self):
        gate = self.begin(operation="update", mode="formal")
        saved = flow1c.load_gate(gate["gate_id"])
        saved.update(state="NON_COMPLIANT", action_completed="update",
                     completion_errors=["output override is not allowed; expected no output file"])
        flow1c.save_gate(saved)

        completed = self.call("agent-complete", gate_id=gate["gate_id"])
        self.assertEqual(completed["state"], "COMPLETE")
        self.assertEqual(flow1c.load_gate(gate["gate_id"])["completion_errors"], [])

    def test_update_action_accepts_legacy_rlm_message_before_json(self):
        gate = self.begin(operation="update", mode="formal")
        ready = flow1c.subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='RLM MCP is already running: http://127.0.0.1:9000/mcp\n{"state":"READY","ready":true}',
            stderr="",
        )
        with mock.patch.object(flow1c, "command_path", return_value="powershell.exe"), \
                mock.patch.object(flow1c, "run_update_command", return_value=ready):
            result = self.call("agent-action", gate_id=gate["gate_id"], action="update",
                               parameters_json='{"confirmed": true}')
        self.assertEqual(result["state"], "READY")
        self.assertEqual(flow1c.load_gate(gate["gate_id"])["action_completed"], "update")
        self.assertEqual(self.call("agent-complete", gate_id=gate["gate_id"])["state"], "COMPLETE")

    def test_missing_update_action_record_can_retry_without_new_gate(self):
        gate = self.begin(operation="update", mode="formal")
        missing = self.call("agent-complete", expected=2, gate_id=gate["gate_id"])
        self.assertEqual(missing["state"], "BLOCKED")
        self.assertEqual(flow1c.load_gate(gate["gate_id"])["state"], "READY")

        saved = flow1c.load_gate(gate["gate_id"])
        saved.update(state="NON_COMPLIANT", completion_errors=["the requested workflow action has not completed"])
        flow1c.save_gate(saved)
        ready = flow1c.subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state":"READY","ready":true}', stderr="",
        )
        with mock.patch.object(flow1c, "command_path", return_value="powershell.exe"), \
                mock.patch.object(flow1c, "run_update_command", return_value=ready):
            retried = self.call("agent-action", gate_id=gate["gate_id"], action="update",
                                parameters_json='{"confirmed": true}')
        self.assertEqual(retried["state"], "READY")
        self.assertEqual(self.call("agent-complete", gate_id=gate["gate_id"])["state"], "COMPLETE")

    def test_update_action_rejects_unparseable_success_output(self):
        gate = self.begin(operation="update", mode="formal")
        malformed = flow1c.subprocess.CompletedProcess(args=[], returncode=0, stdout="not JSON", stderr="")
        with mock.patch.object(flow1c, "command_path", return_value="powershell.exe"), \
                mock.patch.object(flow1c, "run_update_command", return_value=malformed):
            result = self.call("agent-action", expected=2, gate_id=gate["gate_id"], action="update",
                               parameters_json='{"confirmed": true}')
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIsNone(flow1c.load_gate(gate["gate_id"]).get("action_completed"))

    def test_update_background_job_resumes_same_gate_and_approved_options(self):
        gate = self.begin(operation="update", mode="formal")
        update_id = "3707565f-c9a8-45cb-acd0-dd1cd1e709e5"
        waiting = flow1c.subprocess.CompletedProcess(args=[], returncode=0, stderr="", stdout=json.dumps({
            "state": "WAITING_BACKGROUND", "ready": False, "update_id": update_id,
            "options": {"allow_external_updates": True}, "jobs": [{"state": "RUNNING", "pid": 123}],
        }))
        ready = flow1c.subprocess.CompletedProcess(args=[], returncode=0, stderr="", stdout=json.dumps({
            "state": "READY", "ready": True, "update_id": update_id,
        }))
        with mock.patch.object(flow1c, "command_path", return_value="powershell.exe"), \
                mock.patch.object(flow1c, "run_update_command", side_effect=[waiting, ready]) as run:
            result = self.call("agent-action", gate_id=gate["gate_id"], action="update",
                               parameters_json='{"confirmed":true,"allow_external_updates":true}')
            self.assertEqual(result["state"], "WAITING_BACKGROUND")
            saved = flow1c.load_gate(gate["gate_id"])
            self.assertEqual(saved["state"], "WAITING_BACKGROUND")
            self.assertNotIn("action_completed", saved)
            self.call("agent-complete", expected=2, gate_id=gate["gate_id"])
            self.call("agent-action", gate_id=gate["gate_id"], action="update", parameters_json='{"confirmed":true}')
        command = run.call_args_list[-1].args[0]
        self.assertEqual(command[command.index("-UpdateId") + 1], update_id)
        self.assertEqual(command[command.index("-WaitSeconds") + 1], "30")
        self.assertIn("-AllowExternalUpdates", command)
        self.assertEqual(self.call("agent-complete", gate_id=gate["gate_id"])["state"], "COMPLETE")

    def setup_publication(self):
        item = self.make_work_item(status="functional_review")
        local = flow1c.read_json(self.root / flow1c.LOCAL_CONFIG_FILE)
        local["gitea"] = {"reviewers": {"functional": ["human"]}}
        flow1c.write_json(self.root / flow1c.LOCAL_CONFIG_FILE, local)
        flow1c.write_json(item / "input/artifacts.json", {"artifacts": [], "confirmed_absent": ["organizational_approvals"]})
        (item / "specification/functional-spec.md").write_text("Содержательная спецификация", encoding="utf-8")
        evidence = {"completion_state": "COMPLETE", "gate_id": "test-gate", "operation": "functional-spec", "validation_snapshot": flow1c.publication_snapshot("G-001")}
        flow1c.write_json(item / "evidence/test.json", evidence)
        return item

    def test_publication_rejects_stale_document_and_wrong_phase(self):
        item = self.setup_publication()
        flow1c.validate_publication("G-001", "specification")
        with self.assertRaises(flow1c.WorkflowError):
            flow1c.validate_publication("G-001", "technical")
        (item / "specification/functional-spec.md").write_text("Changed", encoding="utf-8")
        with self.assertRaises(flow1c.WorkflowError):
            flow1c.validate_publication("G-001", "specification")
        with mock.patch.object(flow1c, "run_git") as git, mock.patch.object(flow1c, "api_request") as api:
            with self.assertRaises(flow1c.WorkflowError):
                flow1c.cmd_pr_create(type("A", (), {"code": "G-001", "phase": "specification"})())
        git.assert_not_called()
        api.assert_not_called()

    def test_publication_rejects_stale_git_snapshot(self):
        item = self.setup_publication()
        saved = flow1c.read_json(item / "evidence/test.json")
        saved["validation_snapshot"]["extension"] = {"head": "old", "dirty": ""}
        flow1c.write_json(item / "evidence/test.json", saved)
        current = {**saved["validation_snapshot"], "extension": {"head": "new", "dirty": ""}}
        with mock.patch.object(flow1c, "publication_snapshot", return_value=current):
            with self.assertRaises(flow1c.WorkflowError):
                flow1c.validate_publication("G-001", "specification")

    def test_no_code_action_requires_action_and_output(self):
        gate = self.begin(operation="registry", mode="formal", allow_incomplete_draft=True, expected=1)
        result = self.call("agent-complete", expected=2, gate_id=gate["gate_id"])
        self.assertEqual(result["state"], "BLOCKED")
        self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()
