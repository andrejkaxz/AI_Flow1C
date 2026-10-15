"""Local memory lifecycle and gate boundaries on an isolated documentation fixture."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from flow1c import cli, context_manifest, documentation, memory_store, storage
from flow1c.errors import WorkflowError
from flow1c.handoff_policy import digest
from flow1c.workflow import memory_actions, state

ROOT = Path(__file__).resolve().parents[1]


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="m-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.product = self.base / "p"
        self.product.mkdir()
        for folder in ("config", "schemas", "templates", "docs"):
            shutil.copytree(ROOT / folder, self.product / folder)
        shutil.copytree(ROOT / ".agents" / "skills", self.product / ".agents" / "skills")
        self.docs = self.base / "d"
        documentation.initialize(self.docs, self.product / "templates/project-documentation")
        storage.write_json(self.product / ".flow1c.local.json", {"schema_version": 2,
                           "documentation_path": str(self.docs), "project_reference": "SYN-1"})
        self.gate = state.new_gate("status", None, "READY", mode="formal",
                                   summary="Remember synthetic decision", product_root=self.product)
        self.gate["answers"] = [{"question": "Decision?", "answer": "Use a weekly review."}]
        state.save_gate(self.gate, product_root=self.product)

    def call(self, action: str, request: dict) -> dict:
        return memory_actions.command(argparse.Namespace(gate_id=self.gate["gate_id"], action=action,
                                                      request=request), product_root=self.product).value

    def source(self) -> dict:
        return {"type": "dialogue_answer", "answer_index": 0, "locator": "answer to Decision?",
                "sha256": digest(self.gate["answers"][0])}

    def remember(self, **updates: object) -> dict:
        request = {"kind": "decision", "subject": "Review cadence", "aliases": ["weekly"],
                   "text": "A weekly review was requested.", "basis": "user_statement", "source": self.source()}
        request.update(updates)
        return self.call("remember", request)

    def test_persistence_cas_stale_and_forget(self) -> None:
        first = self.remember()
        self.assertEqual(first["revision"], 1)
        self.assertEqual(self.remember()["memory_id"], first["memory_id"])
        self.assertEqual(self.remember(memory_id=first["memory_id"], expected_revision=1)["state"], "UNCHANGED")
        search = self.call("search", {"query": "weekly"})
        self.assertEqual(search["matches"][0]["memory_id"], first["memory_id"])
        self.assertEqual(search["matches"][0]["state"], "WORKING")
        self.assertEqual(self.call("read", {"memory_id": first["memory_id"], "revision": 1})["state"], "WORKING")
        old_source = self.source()
        self.gate["answers"][0]["answer"] = "Use a monthly review."
        state.save_gate(self.gate, product_root=self.product)
        self.assertEqual(self.call("read", {"memory_id": first["memory_id"], "revision": 1})["state"], "STALE")
        with self.assertRaisesRegex(WorkflowError, "SOURCE_STALE"):
            self.remember(memory_id=first["memory_id"], expected_revision=1, source=old_source)
        updated = self.remember(memory_id=first["memory_id"], expected_revision=1,
                                text="A monthly review was requested.")
        self.assertEqual(updated["revision"], 2)
        with self.assertRaisesRegex(WorkflowError, "CONFLICT"):
            self.remember(memory_id=first["memory_id"], expected_revision=1,
                          text="A monthly review was requested.")
        with self.assertRaisesRegex(WorkflowError, "CONFLICT"):
            self.call("read", {"memory_id": first["memory_id"], "revision": 1})
        with self.assertRaisesRegex(WorkflowError, "CONFIRMATION"):
            self.call("forget", {"memory_id": first["memory_id"], "expected_revision": 2, "confirmed": True})
        self.gate["summary"] = "Удалить запись памяти по просьбе пользователя"
        state.save_gate(self.gate, product_root=self.product)
        self.assertEqual(self.call("forget", {"memory_id": first["memory_id"],
                                         "expected_revision": 2, "confirmed": True})["state"], "FORGOTTEN")
        self.assertFalse(self.call("search", {"query": "review"})["matches"])

    def test_scope_corruption_and_closed_gate(self) -> None:
        first = self.remember()
        _, directory, project_id = memory_store.project(product_root=self.product)
        record = memory_store.record_path(directory, first["memory_id"])
        record.write_text('{bad', encoding="utf-8")
        self.assertFalse(self.call("search", {"query": "review"})["scan_complete"])
        with self.assertRaisesRegex(WorkflowError, "CORRUPT"):
            self.call("read", {"memory_id": first["memory_id"], "revision": 1})
        stream = io.StringIO()
        payload = {"gate_id": self.gate["gate_id"], "action": "read",
                   "request": {"memory_id": first["memory_id"], "revision": 1}}
        with mock.patch.object(cli, "ROOT", self.product), mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(stream):
            self.assertEqual(cli.main(["memory", "--json-stdin"]), 2)
        self.assertEqual(json.loads(stream.getvalue())["errors"][0]["code"], "MEMORY_CORRUPT")
        storage.write_json(record, {"schema_version": 2})
        with self.assertRaisesRegex(WorkflowError, "MEMORY_VERSION"):
            self.call("read", {"memory_id": first["memory_id"], "revision": 1})
        malformed = {"schema_version": 1, "memory_id": first["memory_id"],
                     "project_id": project_id, "source": {"type": []}}
        storage.write_json(record, malformed)
        with self.assertRaisesRegex(WorkflowError, "MEMORY_CORRUPT"):
            self.call("read", {"memory_id": first["memory_id"], "revision": 1})
        self.gate["completed_at"] = storage.utc_now()
        state.save_gate(self.gate, product_root=self.product)
        with self.assertRaisesRegex(WorkflowError, "completed gate"):
            self.call("search", {"query": "review"})
        self.assertEqual(len(project_id), 64)

    def test_layout_and_project_reference_isolation(self) -> None:
        first = self.remember()
        self.assertIn("/.flow1c/.workspace/memory", memory_store.project(product_root=self.product)[1].as_posix())
        local = storage.read_json(self.product / ".flow1c.local.json")
        local["project_reference"] = "SYN-2"
        storage.write_json(self.product / ".flow1c.local.json", local)
        with self.assertRaisesRegex(WorkflowError, "MEMORY_SCOPE"):
            self.call("read", {"memory_id": first["memory_id"], "revision": 1})

    def test_layout_change_rejects_old_gate_and_legacy_layout_works(self) -> None:
        (self.docs / ".flow1c" / "layout.json").unlink()
        with self.assertRaisesRegex(WorkflowError, "MEMORY_SCOPE"):
            self.call("overview", {"query": "pilot"})
        new_gate = state.new_gate("status", None, "READY", mode="formal",
                                  summary="Legacy memory", product_root=self.product)
        new_gate["answers"] = [{"question": "When?", "answer": "Friday"}]
        state.save_gate(new_gate, product_root=self.product)
        self.gate = new_gate
        self.remember()
        self.assertTrue((self.docs / ".workspace" / "memory").is_dir())

    def test_git_ignore_is_exact_and_preserves_user_lines(self) -> None:
        subprocess.run(["git", "init", str(self.docs)], check=True, capture_output=True)
        (self.docs / ".gitignore").write_text("custom-data/\n", encoding="utf-8")
        self.remember()
        lines = (self.docs / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["custom-data/", "/.flow1c/.workspace/memory/"])
        self.remember()
        self.assertEqual((self.docs / ".gitignore").read_text(encoding="utf-8").splitlines(), lines)

    def test_accepted_document_requires_covered_context_and_becomes_stale(self) -> None:
        draft = state.new_gate("consultation", None, "READY", mode="draft",
                               summary="Read source", product_root=self.product)
        root = state.request_root(draft, product_root=self.product)
        source_file = root / "sources" / "brief.md"
        storage.write_text(source_file, "Introduction.\nWeekly review is required.")
        sha = storage.sha256(source_file)
        entry = {"entry_id": "src-synthetic", "path": "sources/brief.md", "sha256": sha,
                 "length": len(source_file.read_bytes().decode("utf-8-sig"))}
        manifest = {"entries": [entry]}
        coverage = {"ranges": {}}
        request = {"kind": "project_fact", "subject": "Review", "aliases": [],
                   "text": "Weekly review is required.", "basis": "accepted_document",
                   "source": {"type": "context_entry", "entry_id": entry["entry_id"],
                              "locator": "line:2", "sha256": sha}}
        self.gate = draft
        with mock.patch.object(context_manifest, "_load", return_value=(root, root, manifest, coverage, {})):
            with self.assertRaisesRegex(WorkflowError, "not read"):
                self.call("remember", request)
            coverage["ranges"][entry["entry_id"]] = [[0, 14]]
            with self.assertRaisesRegex(WorkflowError, "not covered"):
                self.call("remember", request)
            coverage["ranges"][entry["entry_id"]] = [[0, entry["length"]]]
            result = self.call("remember", request)
        self.assertEqual(result["state"], "SAVED")
        storage.write_text(source_file, "Monthly review is required.")
        self.assertEqual(self.call("read", {"memory_id": result["memory_id"], "revision": 1})["state"], "STALE")

    def test_overview_reads_a_sealed_completed_handoff(self) -> None:
        local = storage.read_json(self.product / ".flow1c.local.json")
        local.pop("project_reference")
        storage.write_json(self.product / ".flow1c.local.json", local)
        self.gate = state.new_gate("status", None, "READY", mode="formal",
                                   summary="Find prior results", product_root=self.product)
        def cli_call(command: str, payload: dict) -> tuple[int, dict]:
            stream = io.StringIO()
            with mock.patch.object(cli, "ROOT", self.product), mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(stream):
                exit_code = cli.main([command, "--json-stdin"])
            return exit_code, json.loads(stream.getvalue())

        exit_code, producer = cli_call("agent-begin", {"operation": "consultation", "mode": "explore", "summary": "Review pilot cadence"})
        self.assertEqual(exit_code, 0, producer)
        historical = storage.read_json(state.gate_path(producer["gate_id"], product_root=self.product))
        historical.pop("memory_project_id")
        historical.pop("memory_data_root")
        historical.pop("memory_lifecycle_version")
        historical.pop("memory_review_required")
        state.save_gate(historical, product_root=self.product)
        exit_code, completed = cli_call("agent-complete", {"gate_id": producer["gate_id"], "summary": "Weekly review agreed"})
        self.assertEqual(exit_code, 0, completed)
        result = self.call("overview", {"query": "review"})
        self.assertIn(producer["gate_id"], [item["gate_id"] for item in result["handoffs"]])
        self.assertTrue(result["scan_complete"])
        selected = next(item for item in result["handoffs"] if item["gate_id"] == producer["gate_id"])
        saved = self.call("remember", {"kind": "project_fact", "subject": "Pilot review", "aliases": [],
                                       "text": "A weekly review was agreed in the completed request.",
                                       "basis": "completed_result",
                                       "source": {"type": "sealed_handoff", "gate_id": producer["gate_id"],
                                                  "sha256": selected["sha256"], "locator": "completion summary"}})
        self.assertEqual(self.call("read", {"memory_id": saved["memory_id"], "revision": 1})["state"], "WORKING")

    def test_dialogue_note_and_cli_share_record(self) -> None:
        self.gate["notes"] = [{"kind": "user_answer", "text": "The pilot ends Friday.",
                                "created_at": storage.utc_now()}]
        state.save_gate(self.gate, product_root=self.product)
        request = {"kind": "project_fact", "subject": "Pilot date", "aliases": [],
                   "text": "The user said the pilot ends Friday.", "basis": "user_statement",
                   "source": {"type": "dialogue_note", "note_index": 0,
                              "locator": "user answer on pilot date"}}
        stream = io.StringIO()
        payload = {"gate_id": self.gate["gate_id"], "action": "remember", "request": request}
        with mock.patch.object(cli, "ROOT", self.product), mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(stream):
            self.assertEqual(cli.main(["memory", "--json-stdin"]), 0)
        saved = json.loads(stream.getvalue())
        self.assertEqual(saved["revision"], 1)
        result = self.call("read", {"memory_id": saved["memory_id"], "revision": 1})
        self.assertEqual(result["state"], "WORKING")
        self.assertEqual(result["record"]["source"]["note_index"], 0)

    def test_fresh_cli_process_reads_persisted_record(self) -> None:
        saved = self.remember()
        payload = {"gate_id": self.gate["gate_id"], "action": "read",
                   "request": {"memory_id": saved["memory_id"], "revision": 1}}
        program = ("import sys; from pathlib import Path; from flow1c import cli; "
                   "cli.ROOT = Path(sys.argv[1]); raise SystemExit(cli.main(['memory', '--json-stdin']))")
        process = subprocess.run([sys.executable, "-c", program, str(self.product)],
                                 input=json.dumps(payload), cwd=ROOT, text=True,
                                 encoding="utf-8", capture_output=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["record"]["memory_id"], saved["memory_id"])

    def test_forget_accepts_saved_user_deletion_note(self) -> None:
        saved = self.remember()
        self.gate["notes"] = [{"kind": "user_answer", "text": "Удалить запись памяти о пилоте.",
                                "created_at": storage.utc_now()}]
        state.save_gate(self.gate, product_root=self.product)
        result = self.call("forget", {"memory_id": saved["memory_id"],
                                      "expected_revision": 1, "confirmed": True})
        self.assertEqual(result["state"], "FORGOTTEN")


if __name__ == "__main__":
    unittest.main()
