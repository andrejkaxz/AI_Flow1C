"""Real documentation Git fixtures, bounded reads and guarded authoring."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from flow1c import cli, documentation, knowledge, knowledge_policy, navigation, publication, storage
from flow1c.errors import WorkflowError
from flow1c.workflow import begin, knowledge_actions, state

ROOT = Path(__file__).resolve().parents[1]


def card(name: str = "Согласование платежей", body: str = "Платёж согласуется руководителем.") -> str:
    return (f"# {name}\n\nАльтернативные термины: визирование, платёжный календарь\n\n"
            f"## Назначение\n\n{body}\n\n## Текущее поведение\n\nСогласование по сумме.\n\n"
            "## Ограничения\n\nТолько синтетический пример.\n\n## История изменений\n\n"
            "Дата реализации: не подтверждено. Дата внедрения: не подтверждено.\n"
            "Причина: дополнительная проверка лимита. Было: одна проверка. Стало: две проверки.\n\n"
            "## Источники и проверки\n\n[Задача](../../work-items/PAY-7/manifest.yaml). Проверен синтетический сценарий.\n")


class ProjectKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="flow1c-knowledge-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "product"
        self.root.mkdir()
        for folder in ("config", "schemas", "templates", "docs", ".agents/skills"):
            shutil.copytree(ROOT / folder, self.root / folder)
        shutil.copy2(ROOT / ".flow1c.json", self.root / ".flow1c.json")
        self.docs = self.base / "documentation"
        documentation.initialize(self.docs, self.root / "templates/project-documentation")
        storage.write_json(self.root / ".flow1c.local.json", {"schema_version": 2, "documentation_path": str(self.docs), "project_reference": "PROJECT-UNKNOWN"})
        self.data = documentation.data_root(self.docs)
        self.wiki = self.data / "wiki"
        storage.write_text(self.wiki / "features/payments.md", card())
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic project")
        self.gate = state.new_gate("status", None, "READY", mode="formal", product_root=self.root)
        self.validator = Draft202012Validator(json.loads((ROOT / "schemas/project-knowledge-response.schema.json").read_text(encoding="utf-8")))

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.docs), *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()

    def call(self, action: str, request: dict | None = None, gate_id: str | None = None) -> dict:
        result = knowledge_actions.command(argparse.Namespace(action=action, request=request or {}, gate_id=gate_id or self.gate["gate_id"]), product_root=self.root).value
        self.validator.validate(result)
        self.assertLessEqual(knowledge_policy.response_size(result), (request or {}).get("max_chars", 8000))
        return result

    def test_git_search_alias_and_section_read_never_use_dirty_bytes(self) -> None:
        storage.write_text(self.wiki / "features/payments.md", card(body="LOCAL ONLY"))
        result = self.call("search", {"query": "визирование"})
        match = result["matches"][0]
        self.assertEqual(match["path"], "features/payments.md")
        self.assertEqual(match["state"], "COMMITTED")
        read = self.call("read", {"path": match["path"], "snapshot": result["snapshot"], "version": match["version"], "section": "Назначение"})
        self.assertIn("руководителем", read["content"])
        self.assertNotIn("LOCAL ONLY", read["content"])
        local = self.call("read", {"source": "local", "path": match["path"], "section": "Назначение"})
        self.assertIn("LOCAL ONLY", local["content"])
        self.assertEqual(local["source_state"], "LOCAL_DRAFT")

    def test_ref_movement_and_local_version_changes_require_reselection(self) -> None:
        result = self.call("search", {"query": "визирование"})
        match = result["matches"][0]
        storage.write_text(self.wiki / "features/payments.md", card(body="Changed"))
        self.git("add", ".")
        self.git("commit", "-m", "New knowledge")
        with self.assertRaisesRegex(WorkflowError, "snapshot changed"):
            self.call("read", {"path": match["path"], "snapshot": result["snapshot"], "version": match["version"]})
        local = self.call("read", {"source": "local", "path": match["path"]})
        storage.write_text(self.wiki / match["path"], card(body="Another change"))
        with self.assertRaisesRegex(WorkflowError, "document changed"):
            self.call("read", {"source": "local", "path": match["path"], "version": local["version"]})

    def test_git_layout_is_resolved_at_the_selected_commit(self) -> None:
        old = self.git("rev-parse", "HEAD")
        (self.data / "layout.json").unlink()
        storage.write_text(self.docs / "wiki/features/payments.md", card(body="Legacy snapshot"))
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic legacy layout")
        result = self.call("read", {"ref": "main", "path": "features/payments.md"})
        self.assertEqual(result["snapshot"]["prefix"], "wiki")
        self.assertIn("Legacy snapshot", result["content"])
        result = self.call("read", {"ref": old, "path": "features/payments.md"})
        self.assertEqual(result["snapshot"]["prefix"], ".flow1c/wiki")

    def test_long_lines_paginate_without_losing_bytes_and_reject_foreign_cursor(self) -> None:
        text = card(body='"\\' * 10000)
        storage.write_text(self.wiki / "features/long.md", text)
        request = {"source": "local", "path": "features/long.md", "section": "Назначение", "max_chars": 3000}
        pieces = []
        for _ in range(100):
            result = self.call("read", request)
            pieces.append(result["content"])
            if not result["next_cursor"]:
                break
            request.update(cursor=result["next_cursor"], version=result["version"], snapshot=result["snapshot"])
        else:
            self.fail("Continuation did not terminate")
        actual_text = (self.wiki / "features/long.md").read_bytes().decode("utf-8")
        start, end = knowledge_policy.section_range(actual_text, "Назначение")
        self.assertEqual("".join(pieces), actual_text[start:end])
        request["path"] = "features/payments.md"
        with self.assertRaises(WorkflowError):
            self.call("read", request)

    def test_search_limits_incomplete_scan_and_explicit_states(self) -> None:
        for i in range(8):
            storage.write_text(self.wiki / f"features/card-{i}.md", card(name=f"Функция {i}"))
        storage.write_text(self.wiki / "features/cancelled.md", "Статус: отменено\n" + card())
        storage.write_text(self.wiki / "features/draft.md", "> UNVERIFIED_DRAFT\n" + card())
        result = self.call("search", {"source": "local", "query": "визирование"})
        self.assertLessEqual(len(result["matches"]), 5)
        self.assertTrue(result["truncated"])
        specific = self.call("read", {"source": "local", "path": "features/cancelled.md"})
        self.assertEqual(specific["document_state"], "CANCELLED")
        with mock.patch.object(knowledge_policy, "MAX_FILES", 2):
            result = self.call("search", {"source": "local", "query": "does not exist"})
        self.assertFalse(result["scan_complete"])
        self.assertTrue(result["truncated"])

    def test_containment_junctions_and_git_symlink_blobs_are_rejected(self) -> None:
        for path in ("../README.md", "/README.md", "features/../README.md", "C:/README.md", "features\\file.md"):
            with self.subTest(path=path), self.assertRaises(WorkflowError):
                self.call("read", {"source": "local", "path": path})
        blob = subprocess.run(["git", "-C", str(self.docs), "hash-object", "-w", "--stdin"], input=b"../../outside.md", capture_output=True, check=True).stdout.decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{blob},.flow1c/wiki/features/link.md")
        self.git("commit", "-m", "Synthetic symlink blob")
        with self.assertRaisesRegex(WorkflowError, "symlink"):
            self.call("search", {"query": "link"})
        outside = self.base / "outside"
        outside.mkdir()
        link = self.wiki / "linked"
        if os.name == "nt":
            created = subprocess.run(["powershell.exe", "-NoProfile", "-Command", "$null = New-Item -ItemType Junction -Path $env:FLOW1C_TEST_LINK -Target $env:FLOW1C_TEST_OUTSIDE"],
                           env={**os.environ, "FLOW1C_TEST_LINK": str(link), "FLOW1C_TEST_OUTSIDE": str(outside)}, capture_output=True)
            self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaisesRegex(WorkflowError, "symlink or junction"):
                self.call("search", {"source": "local"})
        finally:
            link.rmdir() if os.name == "nt" else link.unlink()

    def test_navigation_separates_tasks_drafts_fragments_templates_and_preserves_files(self) -> None:
        storage.write_json(self.data / "work-items/PAY-7/manifest.yaml", {"code": "PAY-7", "title": "Платежи", "status": "clarification", "approvals": {"functional_architect": "pending"}})
        storage.write_text(self.data / "work-items/PAY-7/specification/functional-spec.md", "# ФС\n\n## Раздел\n")
        storage.write_text(self.data / "work-items/fragment/analysis/notes.md", "# Решение по лимитам\n\nСодержательный фрагмент.")
        storage.write_json(self.data / "drafts/11111111-1111-4111-8111-111111111111/request.json", {"summary": "Платёжный календарь", "mode": "draft", "state": "DRAFT_COMPLETE"})
        draft = self.data / "drafts/11111111-1111-4111-8111-111111111111"
        storage.write_text(draft / "result.md", "# Расчёт лимитов\n\nРабочий результат.")
        storage.write_text(draft / "input/derived/private.md", "PRIVATE INPUT")
        storage.write_json(self.data / "drafts/22222222-2222-4222-8222-222222222222/request.json", {"summary": "Консультация", "result_summary": "Ответ без документа", "state": "CONSULTATION_COMPLETE"})
        readme = self.docs / "README.md"
        readme.write_text("# Моя документация\n\nСохранить пользовательский текст.\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in self.data.rglob("*") if p.is_file() and p.name != "results.md"}
        result = self.call("refresh", {"confirmed": True})
        rendered = (self.wiki / "results.md").read_text(encoding="utf-8")
        for word in ("PAY-7", "Платежи", "TEMPLATE", "fragment", "UNVERIFIED_DRAFT", "SUMMARY_ONLY", "Расчёт лимитов"):
            self.assertIn(word, rendered)
        self.assertNotIn("PRIVATE INPUT", rendered)
        self.assertIn("Сохранить пользовательский текст", readme.read_text(encoding="utf-8"))
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload, str(path))
        again = self.call("refresh", {"confirmed": True})
        self.assertEqual(again, result)
        self.assertEqual(readme.read_text(encoding="utf-8").count("flow1c:navigation:start"), 1)
        page = self.call("navigation", {"query": "лимит"})
        self.assertTrue(page["items"])

    def test_refresh_conflicts_fail_before_any_write(self) -> None:
        (self.wiki / "results.md").write_text("# Мой каталог", encoding="utf-8")
        before = (self.docs / "README.md").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "User-authored"):
            self.call("refresh", {"confirmed": True})
        self.assertEqual((self.docs / "README.md").read_bytes(), before)

    def test_navigation_continuation_is_complete_and_project_bound(self) -> None:
        for index in range(8):
            storage.write_text(self.data / f"drafts/request-{index}/result.md", f"# Result {index}\n\nDraft content")
        first = self.call("navigation")
        self.assertIsNotNone(first["next_cursor"])
        second = self.call("navigation", {"cursor": first["next_cursor"]})
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(len({item["path"] for item in [*first["items"], *second["items"]]}), 8)
        other = self.base / "another-project"
        shutil.copytree(self.docs, other, ignore=shutil.ignore_patterns(".git"))
        storage.write_json(self.root / ".flow1c.local.json", {"documentation_path": str(other)})
        with self.assertRaisesRegex(WorkflowError, "Navigation changed"):
            self.call("navigation", {"cursor": first["next_cursor"]})

    def test_commit_rejects_index_filters_that_change_checked_bytes(self) -> None:
        request = {"path": "features/filter-check.md", "content": card()}
        self.call("preview", request)
        written = self.call("write", {**request, "expected_version": "absent", "confirmed": True})
        # Mock only raw index read to model a transforming Git filter, avoiding a shell dependency.
        changed_blob = subprocess.CompletedProcess(args=["git", "show"], returncode=0, stdout=b"Different checked bytes", stderr=b"")
        with mock.patch.object(knowledge_actions.git_analysis, "run_git", return_value=changed_blob):
            with self.assertRaisesRegex(WorkflowError, "index bytes differ"):
                self.call("commit", {"paths": [written["path"]], "message": "Checked proposal", "branch": "wiki/filter-check", "confirmed": True})
        self.assertNotEqual(self.git("log", "-1", "--format=%s"), "Checked proposal")

    def test_preview_write_requires_exact_content_live_gate_and_user_instruction(self) -> None:
        request = {"path": "features/new-function.md", "content": card()}
        preview = self.call("preview", request)
        self.assertIn("UNVERIFIED_DRAFT", preview["diff"])
        self.assertFalse((self.wiki / request["path"]).exists())
        for mutation in ({**request, "expected_version": "absent"}, {**request, "content": card(body="Different"), "expected_version": "absent", "confirmed": True}):
            with self.assertRaises(WorkflowError):
                self.call("write", mutation)
        self.call("write", {**request, "expected_version": preview["expected_version"], "confirmed": True})
        self.assertIn("UNVERIFIED_DRAFT", (self.wiki / request["path"]).read_text(encoding="utf-8"))
        with self.assertRaises(WorkflowError):
            self.call("write", {**request, "expected_version": "absent", "confirmed": True})
        free = state.new_gate("consultation", None, "READY", mode="explore", product_root=self.root)
        self.call("search", {}, free["gate_id"])
        with self.assertRaisesRegex(WorkflowError, "formal status"):
            self.call("write", {**request, "expected_version": "absent", "confirmed": True}, free["gate_id"])
        self.gate.update(state="COMPLETE", completed_at="synthetic")
        state.save_gate(self.gate, product_root=self.root)
        with self.assertRaises(WorkflowError):
            self.call("search")

    def test_exact_commit_rejects_unrelated_staged_data_and_pr_scope(self) -> None:
        request = {"path": "features/new-function.md", "content": card()}
        self.call("preview", request)
        written = self.call("write", {**request, "expected_version": "absent", "confirmed": True})
        outsider = self.docs / "unrelated.md"
        outsider.write_text("User change", encoding="utf-8")
        self.git("add", "unrelated.md")
        before = self.git("diff", "--cached", "--name-only")
        commit_request = {"paths": [written["path"]], "message": "Knowledge proposal", "branch": "wiki/payment-proposal", "confirmed": True}
        with self.assertRaisesRegex(WorkflowError, "Unexpected staged"):
            self.call("commit", commit_request)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), before)
        self.git("reset", "--", "unrelated.md")
        result = self.call("commit", commit_request)
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD"), written["path"])
        self.assertEqual(outsider.read_text(encoding="utf-8"), "User change")
        outsider.unlink()
        # Transport is mocked as a whole: no push or external API in unit tests.
        with mock.patch.object(publication, "push_documentation_pr", return_value={"html_url": "https://gitea.invalid/project/pulls/1", "number": 1}) as push:
            result = self.call("pr", {"title": "Knowledge", "body": "Synthetic sources; deployment not confirmed", "confirmed": True})
            push.assert_called_once()
        outsider.write_text("Different feature", encoding="utf-8")
        self.git("add", "unrelated.md")
        self.git("commit", "-m", "Other change")
        with self.assertRaisesRegex(WorkflowError, "branch changed"):
            self.call("pr", {"title": "Knowledge", "body": "Sources", "confirmed": True})

    def test_status_begin_ignores_configured_work_reference_and_cli_rejects_unknown_fields(self) -> None:
        args = cli.build_parser().parse_args(["agent-begin", "--operation", "status", "--mode", "formal", "--summary", "Каталог"])
        gate = begin.agent_begin(args, product_root=self.root).value
        self.assertEqual(gate["state"], "READY", gate)
        self.assertIsNone(gate.get("code"))
        self.assertIsNone(gate.get("work_reference"))
        self.assertEqual(gate["project_reference"], "PROJECT-UNKNOWN")
        self.assertIn("flow1c_knowledge", gate["available_actions"])

    def test_cli_knowledge_json_envelope_and_baseline_status_output(self) -> None:
        output = io.StringIO()
        request = {"gate_id": self.gate["gate_id"], "action": "search", "request": {"query": "визирование"}}
        with mock.patch.object(cli, "ROOT", self.root), mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(output):
            self.assertEqual(cli.main(["knowledge", "--json-stdin"]), 0)
        self.assertLessEqual(len(output.getvalue().strip()), 8000)
        self.assertEqual(json.loads(output.getvalue())["state"], "SEARCH")
        output = io.StringIO()
        with mock.patch.object(cli, "ROOT", self.root), mock.patch.object(cli.sys, "stdin", io.StringIO('{"handler":"unsafe"}')), redirect_stdout(output):
            self.assertEqual(cli.main(["knowledge", "--json-stdin"]), 2)
        self.assertEqual(json.loads(output.getvalue())["state"], "BLOCKED")

    def test_knowledge_json_types_and_closed_permissions_are_rejected(self) -> None:
        for action, request in ((["search"], {}), ("search", {"source": []}), ("read", {"path": []}),
                                ("read", {"path": "features/payments.md", "max_chars": True}),
                                ("search", {"query": "x", "unknown": True}), ("write", {"path": [], "confirmed": True})):
            with self.subTest(action=action, request=request), self.assertRaises(WorkflowError):
                self.call(action, request)
        gate = state.new_gate("setup", None, "READY", mode="formal", product_root=self.root)
        with self.assertRaises(WorkflowError):
            self.call("search", {}, gate["gate_id"])


if __name__ == "__main__":
    unittest.main()
