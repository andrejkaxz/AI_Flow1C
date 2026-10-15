"""Persisted memory assessment, new-input invalidation and completion recovery."""
from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from flow1c import cli, context_manifest, handoff, memory_review_policy, memory_store, storage
from flow1c.errors import WorkflowError
from flow1c.workflow import complete, state
import test_project_memory as fixtures

ROOT = Path(__file__).resolve().parents[1]


class MemoryReviewTests(unittest.TestCase):
    call = fixtures.ProjectMemoryTests.call
    source = fixtures.ProjectMemoryTests.source

    def remember(self, **updates: object) -> dict:
        request = {"kind": "decision", "subject": "Review cadence", "aliases": ["weekly"],
                   "text": "A weekly review was requested.", "basis": "user_statement"}
        request.update(updates)
        request.setdefault("source", self.source() if self.gate.get("answers") else
                           {"type": "dialogue_note", "note_index": 0, "locator": "sales decision"})
        return self.call("remember", request)

    def setUp(self) -> None:
        fixtures.ProjectMemoryTests.setUp(self)
        local = self.product / ".flow1c.local.json"
        config = storage.read_json(local)
        config.pop("project_reference")
        storage.write_json(local, config)
        self.gate = state.new_gate("status", None, "READY", summary="Review cadence", product_root=self.product)
        self.gate["answers"] = [{"question": "Decision?", "answer": "Use a weekly review."}]
        state.save_gate(self.gate, product_root=self.product)

    def reload(self) -> dict:
        self.gate = state.load_gate(self.gate["gate_id"], product_root=self.product)
        return self.gate

    def command(self, command: str, payload: dict) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch.object(cli, "ROOT", self.product), \
                mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(output):
            code = cli.main([command, "--json-stdin"])
        return code, json.loads(output.getvalue())

    def start(self, mode: str = "explore", operation: str = "consultation") -> None:
        code, gate = self.command("agent-begin", {"operation": operation, "mode": mode,
                                                "summary": "Analyse the synthetic sales meeting"})
        self.assertEqual(code, 0, gate)
        self.gate = state.load_gate(gate["gate_id"], product_root=self.product)

    def finish(self) -> tuple[int, dict]:
        return self.command("agent-complete", {"gate_id": self.gate["gate_id"], "summary": "Meeting analysis completed"})

    def overview(self) -> None:
        self.call("overview", {"query": "weekly review"})

    def finding(self, record: dict | None = None, disposition: str = "saved", kind: str = "decision") -> dict:
        return {"kind": kind if record else "none", "topic": "Review cadence",
                "disposition": disposition if record else "not_reusable",
                "records": [{"memory_id": record["memory_id"], "revision": record["revision"]}] if record else [],
                "reason": "The explicit user answer defines the review cadence." if record else
                          "This request only locates an existing document; it adds no project decisions."}

    def review(self, record: dict | None = None, **kwargs: str) -> dict:
        return self.call("review", {"findings": [self.finding(record, **kwargs)]})

    def test_complete_preserves_result_until_memory_review_then_replays_receipt(self) -> None:
        self.start("draft")
        code, _ = self.command("agent-write", {"gate_id": self.gate["gate_id"], "target": "draft",
                                               "path": "result.md", "content": "Sales meeting findings"})
        self.assertEqual(code, 0)
        result = state.request_root(self.gate, product_root=self.product) / "result.md"
        original = result.read_bytes()
        code, failure = self.finish()
        self.assertEqual((code, failure["code"]), (2, "MEMORY_REVIEW_REQUIRED"))
        self.assertTrue(failure["preserved_state"])
        self.assertNotIn("completed_at", self.reload())
        self.assertEqual(result.read_bytes(), original)
        self.overview()
        self.review()
        code, first = self.finish()
        self.assertEqual(code, 0, first)
        self.assertEqual(first["memory"]["status"], "NO_NEW_FINDINGS")
        with mock.patch.object(complete, "_agent_complete", side_effect=AssertionError("replayed action")):
            self.assertEqual(self.finish(), (code, first))
        self.assertEqual(result.read_bytes(), original)

    def test_saved_findings_are_schema_valid_and_complete_reports_verified_counts(self) -> None:
        self.start()
        code, _ = self.command("agent-dialogue", {"gate_id": self.gate["gate_id"], "action": "record",
                                                 "kind": "user_answer", "answer": "Use weekly sales reviews."})
        self.assertEqual(code, 0)
        self.reload()
        self.overview()
        record = self.remember(source={"type": "dialogue_note", "note_index": 0, "locator": "sales decision"})
        reviewed = self.review(record)
        self.assertEqual(reviewed["status"], "UPDATED")
        receipt = self.reload()["memory_review"]
        Draft202012Validator(storage.read_json(ROOT / "schemas/memory-review.schema.json")).validate(receipt)
        with mock.patch.object(Path, "open", side_effect=AssertionError("policy attempted I/O")):
            memory_review_policy.validate_receipt(receipt)
        code, result = self.finish()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["memory"], {"status": "UPDATED", "records": 1, "topics": 1})
        with self.assertRaises(WorkflowError):
            self.review(record)

    def test_new_user_input_invalidates_review_and_cannot_mutate_completed_gate(self) -> None:
        self.start()
        self.overview()
        self.review()
        code, _ = self.command("agent-dialogue", {"gate_id": self.gate["gate_id"], "action": "record",
                                                 "kind": "user_answer", "answer": "New explicit project decision"})
        self.assertEqual(code, 0)
        self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")
        self.review()
        self.assertEqual(self.finish()[0], 0)
        self.assertEqual(self.command("agent-dialogue", {"gate_id": self.gate["gate_id"], "action": "record",
                                                        "kind": "user_answer", "answer": "Late change"})[0], 2)

    def test_existing_record_requires_current_read_and_does_not_duplicate(self) -> None:
        original = self.remember()
        self.start()
        self.overview()
        with self.assertRaises(WorkflowError):
            self.review(original, disposition="already_covered")
        self.call("read", {"memory_id": original["memory_id"], "revision": 1})
        self.assertEqual(self.review(original, disposition="already_covered")["status"], "ALREADY_COVERED")
        _, directory, project_id = memory_store.project(product_root=self.product)
        self.assertEqual(len(memory_store.scan(directory, project_id)[0]), 1)
        self.assertEqual(self.finish()[0], 0)

    def test_review_rejects_missing_overview_empty_reasons_and_wrong_kind(self) -> None:
        with self.assertRaises(WorkflowError):
            self.review()
        self.overview()
        record = self.remember(kind="source_location")
        with self.assertRaisesRegex(WorkflowError, "kind does not cover"):
            self.review(record, kind="project_fact")
        self.assertEqual(self.review(record, kind="source_location")["status"], "UPDATED")
        finding = self.finding()
        finding["reason"] = "done"
        for request in ({"findings": []}, {"findings": [finding]}, {"findings": [self.finding()], "confirmed": True}):
            with self.subTest(request=request), self.assertRaises(WorkflowError):
                self.call("review", request)

    def test_changed_record_or_source_cannot_reuse_review(self) -> None:
        self.overview()
        record = self.remember()
        self.review(record)
        self.reload()["answers"][0]["answer"] = "Use a monthly review."
        state.save_gate(self.gate, product_root=self.product)
        self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")
        with self.assertRaises(WorkflowError):
            self.review(record)
        updated = self.remember(memory_id=record["memory_id"], expected_revision=1, text="Use a monthly review.")
        self.review(updated)
        _, directory, project_id = memory_store.project(product_root=self.product)
        changed = memory_store.read(directory, record["memory_id"], project_id)
        changed["text"] = "A different concurrent conclusion."
        with memory_store.writer(self.docs, directory):
            memory_store.put(directory, changed)
        self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")

    def test_missing_record_and_project_switch_preserve_active_gate(self) -> None:
        self.overview()
        record = self.remember()
        self.review(record)
        _, directory, _ = memory_store.project(product_root=self.product)
        memory_store.record_path(directory, record["memory_id"]).unlink()
        self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")
        local = self.product / ".flow1c.local.json"
        config = storage.read_json(local)
        config["project_reference"] = "OTHER"
        storage.write_json(local, config)
        self.assertEqual(self.finish()[1]["reason_code"], "MEMORY_SCOPE")
        self.assertNotIn("completed_at", self.reload())

    def test_memory_io_failure_does_not_complete_or_repeat_workflow_action(self) -> None:
        self.reload()["action_completed"] = "status"
        state.save_gate(self.gate, product_root=self.product)
        self.overview()
        self.review()
        with mock.patch.object(memory_store, "record_path", side_effect=PermissionError("synthetic")):
            # No record writes are needed for an honest no-new-findings assessment.
            self.assertEqual(self.review()["status"], "NO_NEW_FINDINGS")
        with mock.patch.object(memory_store, "writer", side_effect=PermissionError("synthetic")), \
                mock.patch.object(complete, "_agent_complete", side_effect=AssertionError("completion ran")):
            code, result = self.finish()
        self.assertEqual((code, result["code"]), (2, "MEMORY_REVIEW_REQUIRED"))
        self.assertEqual(self.reload()["action_completed"], "status")
        self.assertNotIn("completed_at", self.gate)
        self.assertEqual(self.finish()[0], 0)

    def test_formal_and_template_paths_share_preflight(self) -> None:
        self.assertTrue(self.reload()["memory_review_required"])
        self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")
        self.gate = state.new_gate("template-document", None, "READY", mode="draft", product_root=self.product)
        with mock.patch("flow1c.templates.complete", side_effect=AssertionError("template completion before review")):
            self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")

    def test_legacy_and_setup_keep_existing_completion_contract(self) -> None:
        self.start()
        self.gate.pop("memory_lifecycle_version")
        self.gate.pop("memory_review_required")
        state.save_gate(self.gate, product_root=self.product)
        code, result = self.finish()
        self.assertEqual(code, 0, result)
        self.assertNotIn("memory", result)
        for operation in ("setup", "update"):
            gate = state.new_gate(operation, None, "READY", product_root=self.product)
            self.assertFalse(gate["memory_review_required"])

    def test_gate_lock_prevents_review_or_new_answers_during_completion(self) -> None:
        with handoff.writer(self.gate["gate_id"], product_root=self.product):
            with self.assertRaisesRegex(WorkflowError, "MEMORY_BUSY"):
                self.review()
            code, _ = self.command("agent-dialogue", {"gate_id": self.gate["gate_id"], "action": "record",
                                                     "kind": "user_answer", "answer": "Concurrent answer"})
            self.assertEqual(code, 2)

    def test_read_document_then_new_version_requires_fresh_review(self) -> None:
        self.start("draft")
        request_root = state.request_root(self.gate, product_root=self.product)
        document = request_root / "input/meeting.md"
        storage.write_text(document, "# Sales meeting\nUse weekly reviews.\nDiscount approval is still open.\n")
        evidence_path, evidence = state.evidence_for_gate(self.gate, product_root=self.product)
        evidence["artifacts"] = [{"path": "input/meeting.md", "sha256": storage.sha256(document)}]
        storage.write_json(evidence_path, evidence)
        context_manifest.build(self.gate, product_root=self.product)
        # The reader's returned index names the accepted, hash-pinned entry.
        index = context_manifest.read(self.gate, argparse.Namespace(entry_id="index", cursor="", max_chars=8000,
                                                                   section_id=None), product_root=self.product)
        entry_id = json.loads(index["content"].splitlines()[0])["entry_id"]
        context_manifest.read(self.gate, argparse.Namespace(entry_id=entry_id, cursor="", max_chars=8000,
                                                           section_id=None), product_root=self.product)
        self.overview()
        record = self.remember(basis="accepted_document", source={"type": "context_entry", "entry_id": entry_id,
                              "locator": "line:2", "sha256": storage.sha256(document)})
        self.review(record)
        self.assertEqual(self.reload()["memory_review"]["status"], "UPDATED")
        storage.write_text(document, "# Sales meeting\nUse monthly reviews.\n")
        self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")
        with self.assertRaises(WorkflowError):
            self.review(record)

    def test_remember_retry_after_lost_gate_write_does_not_duplicate(self) -> None:
        self.overview()
        with mock.patch.object(state, "save_gate", side_effect=PermissionError("interrupted observation")):
            with self.assertRaisesRegex(WorkflowError, "MEMORY_UNAVAILABLE"):
                self.remember()
        record = self.remember()
        self.assertEqual(record["state"], "UNCHANGED")
        self.assertEqual(self.review(record, disposition="already_covered")["status"], "ALREADY_COVERED")
        _, directory, project_id = memory_store.project(product_root=self.product)
        self.assertEqual(len(memory_store.scan(directory, project_id)[0]), 1)

    def test_output_written_after_review_requires_another_review(self) -> None:
        self.start("draft")
        self.overview()
        self.review()
        code, _ = self.command("agent-write", {"gate_id": self.gate["gate_id"], "target": "draft",
                                               "path": "result.md", "content": "A newly prepared synthetic result"})
        self.assertEqual(code, 0)
        self.assertEqual(self.finish()[1]["code"], "MEMORY_REVIEW_REQUIRED")
        self.review()
        self.assertEqual(self.finish()[0], 0)

    def test_configured_project_binding_failure_does_not_disable_memory_preflight(self) -> None:
        with mock.patch.object(memory_store, "project", side_effect=WorkflowError("synthetic scope failure")):
            self.gate = state.new_gate("consultation", None, "READY", mode="explore", product_root=self.product)
            self.assertTrue(self.gate["memory_review_required"])
            self.assertNotIn("memory_project_id", self.gate)
            code, result = self.finish()
        self.assertEqual((code, result["code"]), (2, "MEMORY_REVIEW_REQUIRED"))
        self.assertNotIn("completed_at", self.reload())


if __name__ == "__main__":
    unittest.main()
