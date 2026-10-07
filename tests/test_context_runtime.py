"""Real persisted context versions, safe paths, cursors, restart and completion."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flow1c import context_manifest as context, storage
from flow1c.context_policy import ContextError
from flow1c.workflow import state, complete
from test_cli_contract import CliScenario
from test_role_context import prepare_context


class ContextRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scenario = CliScenario(Path(self.temporary.name))
        self.root = self.scenario.root
        result = self.scenario.run(["agent-begin", "--json-stdin"], {"operation": "functional-review", "mode": "explore", "summary": "Review the accepted specification"})
        self.assertEqual(result["exit_code"], 0, result)
        self.gate = storage.read_json(state.gate_path(self.scenario.gate_id, product_root=self.root))
        self.request = state.request_root(self.gate, product_root=self.root)
        self.source = self.request / "input/spec.md"
        self.text = "# Exact specification\r\n" + "Контроль количества.\n" * 1100 + "THE END"
        storage.atomic_write_bytes(self.source, self.text.encode("utf-8"))
        self.accept(self.source)

    def accept(self, source, **extra):
        path, evidence = state.evidence_for_gate(self.gate, product_root=self.root)
        evidence["artifacts"] = [{"path": source.relative_to(self.request).as_posix(), "sha256": storage.sha256(source), **extra}]
        storage.write_json(path, evidence)

    def build(self):
        return context.build(self.gate, product_root=self.root)

    def manifest(self):
        _, evidence = state.evidence_for_gate(self.gate, product_root=self.root)
        store = self.request / "contexts" / self.gate["gate_id"]
        return store, storage.read_json(store / evidence["context_manifest"]["path"])

    def read(self, entry_id=None, **kwargs):
        if entry_id is None:
            entry_id = self.manifest()[1]["entries"][0]["entry_id"]
        return context.read(self.gate, argparse.Namespace(entry_id=entry_id, **kwargs), product_root=self.root)

    def read_all(self):
        cursor, text = "", ""
        while True:
            page = self.read(cursor=cursor)
            self.assertLessEqual(len(page["content"]), 8000)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, indent=2)), 24000)
            text += page["content"]
            cursor = page["next_cursor"]
            if not cursor:
                return text, page

    def test_large_source_exact_ranges_resume_and_idempotent_rebuild(self):
        result = self.build()
        self.assertLessEqual(len(result["content"]), 12000)
        self.assertFalse(result["coverage"]["complete"])
        first = self.read()
        # A distinct CLI process continues only the issued cursor after restart.
        response = self.scenario.run(["context-read", "--json-stdin"], {"gate_id": self.gate["gate_id"], "entry_id": first["entry_id"], "cursor": first["next_cursor"]})
        self.assertEqual(response["exit_code"], 0, response)
        self.assertEqual(response["stdout"]["start"], first["end"])
        self.assertEqual(self.build()["manifest_id"], result["manifest_id"])
        text, last = self.read_all()
        self.assertEqual(text, self.text)
        self.assertTrue(last["coverage"]["complete"])
        self.assertTrue(self.build()["coverage"]["complete"])
        self.assertEqual(context.completion_errors(self.gate, product_root=self.root), [])

    def test_sections_and_cursors_cannot_mark_unread_prefix_covered(self):
        self.build()
        second = self.read(section_id="part-00002")
        self.assertEqual(second["start"], 8000)
        self.assertFalse(second["coverage"]["complete"])
        first = self.read(max_chars=10)
        for kwargs in ({"cursor": "fake"}, {"cursor": first["next_cursor"], "section_id": "part-00002"}, {"section_id": "unknown"}, {"max_chars": True}, {"max_chars": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContextError):
                self.read(**kwargs)

    def test_changed_source_rejects_cursor_and_rebuild_requires_fresh_intake(self):
        self.build()
        cursor = self.read()["next_cursor"]
        storage.write_text(self.source, "changed")
        with self.assertRaisesRegex(ContextError, "changed"):
            self.read(cursor=cursor)
        with self.assertRaises(ContextError):
            self.build()
        self.assertIn("CONTEXT_CURSOR_STALE", context.completion_errors(self.gate, product_root=self.root)[0])
        self.accept(self.source)
        self.build()
        with self.assertRaises(ContextError):
            self.read(cursor=cursor)
        self.assertFalse(self.build()["coverage"]["complete"])

    def test_new_intake_and_saved_answer_invalidate_scope_without_new_gate(self):
        self.build()
        self.read()
        self.gate.setdefault("notes", []).append({"text": "Review only the selected document"})
        state.save_gate(self.gate, product_root=self.root)
        with self.assertRaises(ContextError):
            self.read()
        rebuilt = self.build()
        exact_scope = self.read(entry_id="scope", max_chars=8000)
        self.assertIn("Review only the selected document", exact_scope["content"])
        path, evidence = state.evidence_for_gate(self.gate, product_root=self.root)
        new = self.request / "input/second.md"
        storage.write_text(new, "another accepted document")
        evidence["artifacts"].append({"path": "input/second.md", "sha256": storage.sha256(new)})
        storage.write_json(path, evidence)
        with self.assertRaises(ContextError):
            self.read()
        self.assertNotEqual(self.build()["manifest_id"], rebuilt["manifest_id"])

    def test_integrity_unknown_versions_ownership_and_modified_coverage(self):
        self.build()
        path, evidence = state.evidence_for_gate(self.gate, product_root=self.root)
        saved = json.loads(json.dumps(evidence))
        entry_id = self.manifest()[1]["entries"][0]["entry_id"]
        for field in ("schema_version", "path", "sha256"):
            corrupted = json.loads(json.dumps(saved))
            corrupted["context_manifest"][field] = {"schema_version": 99, "path": "../evidence.json", "sha256": "0" * 64}[field]
            storage.write_json(path, corrupted)
            with self.subTest(field=field), self.assertRaises(ContextError):
                self.read(entry_id=entry_id)
        storage.write_json(path, saved)
        store, _ = self.manifest()
        coverage_file = store / saved["context_manifest"]["coverage"]["path"]
        coverage_file.write_text("{}", encoding="utf-8")
        with self.assertRaises(ContextError):
            self.read()

    def test_coverage_owner_unknown_version_and_invalid_ranges_are_rejected(self):
        self.build()
        path, evidence = state.evidence_for_gate(self.gate, product_root=self.root)
        store, manifest = self.manifest()
        saved = json.loads(json.dumps(evidence))
        initial = storage.read_json(store / saved["context_manifest"]["coverage"]["path"])
        entry = manifest["entries"][0]["entry_id"]
        for change in ({"gate_id": "other"}, {"schema_version": 2}, {"ranges": {entry: [[0, 9999999]]}}, {"ranges": {entry: [[True, 5]]}}, {"cursors": {"forged": {"offset": 8000, "entry_id": entry, "section_id": "whole", "sha256": "a" * 64}}}):
            evidence = json.loads(json.dumps(saved))
            evidence["context_manifest"]["coverage"] = context._immutable(store, "coverage", {**initial, **change})
            storage.write_json(path, evidence)
            with self.subTest(change=change), self.assertRaises(ContextError):
                self.read(entry_id=entry)
            with self.assertRaises(ContextError):
                self.build()

    def test_empty_contract_cannot_downgrade_to_legacy_completion(self):
        path, evidence = state.evidence_for_gate(self.gate, product_root=self.root)
        evidence["context_manifest"] = {}
        storage.write_json(path, evidence)
        self.assertTrue(context.completion_errors(self.gate, product_root=self.root))
        with self.assertRaises(ContextError):
            self.build()

    def test_raw_managed_parent_and_evidence_links_checked_before_resolve(self):
        self.build()
        real_check = storage.is_reparse_or_symlink
        for suspect in (self.root / ".workspace/drafts", Path(self.gate["evidence_path"])):
            with self.subTest(suspect=str(suspect)), mock.patch.object(storage, "is_reparse_or_symlink", side_effect=lambda p: p == suspect or real_check(p)):
                with self.assertRaises(ContextError):
                    self.read(entry_id="index")

    def test_context_cli_refuses_unknown_fields_without_mutating_evidence(self):
        self.build()
        path, _ = state.evidence_for_gate(self.gate, product_root=self.root)
        before = path.read_bytes()
        for field in ("path", "handler", "action"):
            result = self.scenario.run(["context-read", "--json-stdin"], {"gate_id": self.gate["gate_id"], "entry_id": "index", field: "untrusted"})
            self.assertEqual(result["exit_code"], 2, result)
            self.assertEqual(result["stdout"]["errors"][0]["code"], "CONTEXT_INVALID")
        self.assertEqual(path.read_bytes(), before)

    def test_arbitrary_path_and_bsl_sources_are_never_read(self):
        self.build()
        for entry in ("../input/spec.md", str(self.source), "input/spec.md"):
            with self.subTest(entry=entry), self.assertRaises(ContextError):
                self.read(entry_id=entry)
        path, evidence = state.evidence_for_gate(self.gate, product_root=self.root)
        for unsafe in ("../secret.txt", "/secret.txt", "C:/secret.txt", "input/../secret.txt", "input\\secret.txt"):
            evidence["artifacts"] = [{"path": unsafe}]
            storage.write_json(path, evidence)
            with self.subTest(unsafe=unsafe), self.assertRaises(ContextError):
                self.build()
        bsl = self.request / "input/module.bsl"
        storage.write_text(bsl, "Secret BSL facts")
        self.accept(bsl)
        built = self.build()
        self.assertFalse(built["coverage"]["complete"])
        with self.assertRaises(ContextError):
            self.read()

    def test_symlink_or_windows_junction_parent_is_rejected(self):
        outside = Path(self.temporary.name) / "outside-context"
        outside.mkdir()
        target = self.request / "linked"
        try:
            os.symlink(outside, target, target_is_directory=True)
        except OSError:
            # Exercise the same parent check for Windows reparse points without admin rights.
            target.mkdir()
            real_check = storage.is_reparse_or_symlink
            with mock.patch.object(storage, "is_reparse_or_symlink", side_effect=lambda p: p == target or real_check(p)):
                with self.assertRaises(ContextError):
                    context._safe(self.request, "linked/file.md")
        else:
            with self.assertRaises(ContextError):
                context._safe(self.request, "linked/file.md")

    def test_original_extraction_provenance_and_byte_limits(self):
        original = self.request / "input/spec.docx"
        storage.atomic_write_bytes(original, b"synthetic accepted binary")
        self.accept(original, derived_path="input/spec.md", derived_sha256=storage.sha256(self.source))
        self.build()
        self.read()
        storage.atomic_write_bytes(original, b"changed original")
        with self.assertRaises(ContextError):
            self.read()
        self.accept(self.source)
        limits = storage.read_json(self.root / "config/context.json")
        limits["max_source_bytes"] = 100
        storage.write_json(self.root / "config/context.json", limits)
        self.assertFalse(self.build()["coverage"]["complete"])
        with self.assertRaisesRegex(ContextError, "extraction"):
            self.read()

    def test_response_envelope_accounts_for_escaped_control_characters(self):
        storage.atomic_write_bytes(self.source, ("\x01" * 16000).encode("utf-8"))
        self.accept(self.source)
        self.build()
        text, last = self.read_all()
        self.assertEqual(text, "\x01" * 16000)
        self.assertTrue(last["coverage"]["complete"])

    def test_interrupted_coverage_commit_preserves_previous_cursor(self):
        self.build()
        first = self.read(max_chars=20)
        evidence_path, _ = state.evidence_for_gate(self.gate, product_root=self.root)
        previous = evidence_path.read_bytes()
        original_write = storage.write_json
        def fail(path, value):
            if path == evidence_path:
                raise OSError("interrupted before authoritative replace")
            return original_write(path, value)
        with mock.patch.object(storage, "write_json", side_effect=fail):
            with self.assertRaises(OSError):
                self.read(cursor=first["next_cursor"], max_chars=20)
        self.assertEqual(evidence_path.read_bytes(), previous)
        self.assertEqual(self.read(cursor=first["next_cursor"], max_chars=20)["start"], 20)

    def test_index_is_paginated_and_does_not_count_as_original_read(self):
        self.build()
        page = self.read(entry_id="index", max_chars=100)
        self.assertTrue(page["next_cursor"])
        self.assertFalse(page["coverage"]["complete"])
        self.assertEqual(self.read(entry_id="index", cursor=page["next_cursor"], max_chars=100)["start"], 100)

    def test_complete_refuses_unread_review_without_closing_gate(self):
        self.build()
        result = self.scenario.run(["agent-complete", "--json-stdin"], {"gate_id": self.gate["gate_id"], "summary": "All reviewed"})
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["stdout"]["code"], "CONTEXT_SCOPE_REQUIRED")
        saved = storage.read_json(state.gate_path(self.gate["gate_id"], product_root=self.root))
        self.assertEqual(saved["state"], "READY")
        self.read_all()
        result = self.scenario.run(["agent-complete", "--json-stdin"], {"gate_id": self.gate["gate_id"], "summary": "Read every part"})
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"]["state"], "CONSULTATION_COMPLETE")
        result = self.scenario.run(["context-read", "--json-stdin"], {"gate_id": self.gate["gate_id"], "entry_id": "index"})
        self.assertEqual(result["exit_code"], 2)

    def test_formal_review_requires_all_specification_basis_and_traceability(self):
        item = prepare_context(self.root, "registry")
        evidence_path = item / "evidence/functional-review.json"
        gate = {**self.gate, "mode": "formal", "code": "TASK-42", "work_reference": "TASK-42", "context_role": "functional-architect", "evidence_path": str(evidence_path)}
        storage.write_json(item / "input/artifacts.json", {"artifacts": []})
        storage.write_json(evidence_path, {"schema_version": 2, "gate_id": gate["gate_id"], "operation": "functional-review", "code": "TASK-42", "artifacts": [], "rlm_queries": [], "source_reads": [], "changed_files": []})
        result = context.build(gate, product_root=self.root)
        self.assertEqual(result["coverage"]["unread_required_count"], 3)
        root, store, manifest, _, _ = context._load(gate, self.root)
        self.assertTrue(all(e["required"] for e in manifest["entries"]))
        for entry in manifest["entries"]:
            context.read(gate, argparse.Namespace(entry_id=entry["entry_id"]), product_root=self.root)
        self.assertEqual(context.completion_errors(gate, product_root=self.root), [])
        manifest_path = item / "manifest.yaml"
        work_manifest = storage.read_json(manifest_path)
        work_manifest["requirements"].append("SYNTHETIC-ADDED")
        storage.write_json(manifest_path, work_manifest)
        self.assertIn("CONTEXT_CURSOR_STALE", context.completion_errors(gate, product_root=self.root)[0])
        work_manifest["requirements"].pop()
        storage.write_json(manifest_path, work_manifest)
        (item / "specification/functional-spec.md").unlink()
        self.assertIn("CONTEXT_CURSOR_STALE", context.completion_errors(gate, product_root=self.root)[0])

    def test_real_formal_intake_documentation_paths_and_legacy_derived_hash(self):
        item = prepare_context(self.root, "registry")
        storage.write_json(item / "input/artifacts.json", {"schema_version": 1, "artifacts": [], "confirmed_absent": []})
        gate = state.new_gate("functional-review", "TASK-42", "READY", product_root=self.root,
                              work_reference="TASK-42", context_role="functional-architect", summary="Synthetic formal review")
        evidence_path = item / "evidence/formal.json"
        storage.write_json(evidence_path, {"schema_version": 2, "gate_id": gate["gate_id"], "operation": "functional-review", "code": "TASK-42", "artifacts": [], "rlm_queries": [], "source_reads": [], "changed_files": []})
        gate["evidence_path"] = str(evidence_path)
        state.save_gate(gate, product_root=self.root)
        attachment = Path(self.temporary.name) / "meeting.txt"
        storage.write_text(attachment, "Synthetic accepted meeting: quantity must match")
        response = self.scenario.run(["artifact-intake", "--json-stdin"], {"gate_id": gate["gate_id"], "source": [str(attachment)], "category": "meeting_materials"})
        self.assertEqual(response["exit_code"], 0, response)
        artifact = storage.read_json(item / "input/artifacts.json")["artifacts"][0]
        self.assertTrue(artifact["relative_path"].startswith("work-items/TASK-42/"))
        self.assertTrue(artifact["derived_path"].startswith("work-items/TASK-42/"))
        self.assertNotIn("derived_sha256", artifact)  # Published formal intake contract.
        response = self.scenario.run(["agent-context", "--json-stdin"], {"gate_id": gate["gate_id"], "view": "compact"})
        self.assertEqual(response["exit_code"], 0, response)
        gate = storage.read_json(state.gate_path(gate["gate_id"], product_root=self.root))
        _, _, manifest, _, _ = context._load(gate, self.root)
        entry = next(e for e in manifest["entries"] if e["reason"] == "accepted artifact")
        self.assertTrue(entry["readable"])
        self.assertTrue(entry["path"].startswith("input/derived/"))
        page = context.read(gate, argparse.Namespace(entry_id=entry["entry_id"]), product_root=self.root)
        self.assertIn("quantity must match", page["content"])
        original = item / entry["original"]["path"]
        storage.write_text(original, "changed original")
        with self.assertRaises(ContextError):
            context.read(gate, argparse.Namespace(entry_id=entry["entry_id"]), product_root=self.root)

    def test_compact_gate_cannot_drop_coverage_by_switching_to_full(self):
        self.build()
        # Formal route has a valid full view; switching it must retain the compact obligation.
        from flow1c.workflow import dialogue
        formal = {**self.gate, "mode": "formal", "code": "TASK-42", "work_reference": "TASK-42", "context_role": "functional-architect"}
        evidence_pair = state.evidence_for_gate(self.gate, product_root=self.root)
        with mock.patch.object(state, "load_gate", return_value=formal), mock.patch.object(state, "require_gate_tool"), mock.patch.object(state, "evidence_for_gate", return_value=evidence_pair), mock.patch.object(dialogue, "build_context_for_reference", return_value=self.source):
            with self.assertRaisesRegex(ContextError, "coverage contract"):
                dialogue.agent_context(argparse.Namespace(gate_id=formal["gate_id"]), product_root=self.root)
