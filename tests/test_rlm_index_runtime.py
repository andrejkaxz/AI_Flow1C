from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from scripts import rlm_index_runtime as runtime
from scripts.rlm_index_policy import receipt_is_current, validation_succeeded


class IndexPolicyTests(unittest.TestCase):
    def test_receipt_requires_all_evidence_and_expires(self):
        receipt = dict(schema_version=1, validated_at=100, source_fingerprint="source",
                       index_identity="index", tool_version="1.41.0")
        arguments = dict(now=101, max_age_seconds=10, source_fingerprint="source",
                         index_identity="index", tool_version="1.41.0")
        self.assertTrue(receipt_is_current(receipt, **arguments))
        for key, value in (("now", 111), ("now", 99), ("source_fingerprint", "changed"),
                           ("index_identity", "replaced"), ("tool_version", "1.42.0")):
            with self.subTest(key=key, value=value):
                self.assertFalse(receipt_is_current(receipt, **{**arguments, key: value}))
        self.assertFalse(receipt_is_current({**receipt, "validated_at": True}, **arguments))

    def test_exit_code_alone_does_not_validate_index(self):
        self.assertTrue(validation_succeeded(0, "same", "same", "fresh"))
        self.assertFalse(validation_succeeded(1, "same", "same", "fresh"))
        self.assertFalse(validation_succeeded(0, "same", "changed", "fresh"))
        self.assertFalse(validation_succeeded(0, "same", "same", "incomplete/building"))


class IndexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "Configuration.xml").write_text("<Configuration/>")
        (self.source / "Module.bsl").write_text("// fixture")
        self.database = self.root / "index.db"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO index_meta VALUES ('built_at','1')")
        self.fake = self.root / "index_cli.py"
        self.fake.write_text(
            "import os,sys,pathlib,time\n"
            "source=pathlib.Path(sys.argv[-1])\n"
            "if sys.argv[2]=='info':\n"
            " print('Status: '+('fresh' if os.environ.get('RLM_INDEX_MAX_AGE_DAYS')=='2147483647' else 'stale (age)'))\n"
            "else:\n"
            " print('Updated: Added=0 Changed=0 Removed=0')\n", encoding="utf-8")
        self.manager = runtime.IndexManager(self.root, self.source, command=[sys.executable, str(self.fake)],
                                            tool_version="1.41.0", database=self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def job(self):
        record = dict(schema_version=2, job_id="fixture", state="RUNNING", mode="update",
                      stdout_log=str(self.manager.directory / "stdout.log"), stderr_log=str(self.manager.directory / "stderr.log"))
        runtime.write_json(self.manager.job_path, record)
        return record

    def test_zero_delta_update_validates_old_index_without_changing_database(self):
        original = self.database.read_bytes()
        self.job()
        with mock.patch.dict(os.environ, {"RLM_INDEX_MAX_AGE_DAYS": "7"}):
            self.manager.run_worker("fixture")
            self.assertEqual(os.environ["RLM_INDEX_MAX_AGE_DAYS"], "7")
        job = runtime.read_json(self.manager.job_path)
        self.assertEqual(job["state"], "FRESH", job.get("detail"))
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(self.database.read_bytes(), original)
        with mock.patch.object(self.manager, "info", side_effect=AssertionError("no expensive rescan")), \
                mock.patch.object(runtime.subprocess, "Popen", side_effect=AssertionError("no duplicate job")):
            self.assertEqual(self.manager.ensure()["state"], "FRESH")

    def test_receipt_invalidates_for_xml_change_even_when_cli_says_fresh(self):
        self.job()
        self.manager.run_worker("fixture")
        (self.source / "Configuration.xml").write_text("<Changed/>")
        with mock.patch.object(self.manager, "info", return_value={"status": "fresh", "detail": "fresh"}):
            self.assertEqual(self.manager.status()["state"], "STALE")

    def test_database_replacement_and_tool_upgrade_invalidate_receipt(self):
        self.job()
        self.manager.run_worker("fixture")
        self.manager.tool_version = "1.42.0"
        self.assertFalse(self.manager.receipt_current())
        self.manager.tool_version = "1.41.0"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE index_meta SET value='2'")
        self.assertFalse(self.manager.receipt_current())

    def test_live_job_polling_never_runs_info_or_launches_another_job(self):
        job = self.job()
        job.update(worker_identity=runtime.process_identity(os.getpid()), started_epoch=time.time())
        runtime.write_json(self.manager.job_path, job)
        with mock.patch.object(self.manager, "info", side_effect=AssertionError("no scan while running")), \
                mock.patch.object(runtime.subprocess, "Popen", side_effect=AssertionError("no duplicate")):
            self.assertEqual(self.manager.status()["state"], "RUNNING")
            self.assertEqual(self.manager.ensure(force=True)["state"], "RUNNING")

    def test_reused_pid_is_not_a_live_job(self):
        job = self.job()
        job["worker_identity"] = {"pid": os.getpid(), "created": "wrong"}
        runtime.write_json(self.manager.job_path, job)
        self.assertEqual(self.manager.status()["state"], "RECOVERY_REQUIRED")

    def test_worker_failure_never_issues_receipt_and_keeps_logs(self):
        self.fake.write_text("import sys; print('fixture error',file=sys.stderr); sys.exit(3)")
        self.job()
        self.manager.run_worker("fixture")
        self.assertFalse(self.manager.receipt_path.exists())
        result = self.manager.status()
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("fixture error", Path(result["stderr_log"]).read_text())
        with self.assertRaises(runtime.IndexRuntimeError):
            self.manager.ensure()

    def test_source_change_during_worker_never_issues_receipt(self):
        self.fake.write_text(self.fake.read_text() +
            "\nif sys.argv[2]!='info': (source/'Configuration.xml').write_text('<changed/>')\n")
        self.job()
        self.manager.run_worker("fixture")
        self.assertEqual(self.manager.status()["state"], "FAILED")
        self.assertFalse(self.manager.receipt_path.exists())

    def test_incomplete_index_with_success_exit_is_rejected(self):
        self.job()
        with mock.patch.object(self.manager, "info", return_value={"status": "incomplete/building", "detail": "incomplete"}):
            self.manager.run_worker("fixture")
        self.assertEqual(self.manager.status()["state"], "FAILED")
        self.assertFalse(self.manager.receipt_path.exists())

    def test_unknown_cli_output_is_not_treated_as_missing_index(self):
        self.fake.write_text("print('unexpected output')")
        with self.assertRaises(runtime.IndexRuntimeError):
            self.manager.status()

    def test_checkpoint_lock_prevents_parallel_starts(self):
        with runtime.job_lock(self.manager.lock_path):
            with self.assertRaises(runtime.IndexRuntimeError):
                self.manager.ensure()

    def test_force_update_is_idempotent_for_the_same_saved_request(self):
        job = self.job()
        job["request_id"] = "saved-update"
        runtime.write_json(self.manager.job_path, job)
        self.manager.run_worker("fixture")
        with mock.patch.object(runtime.subprocess, "Popen", side_effect=AssertionError("no duplicate forced job")):
            self.assertEqual(self.manager.ensure(force=True, request_id="saved-update")["state"], "FRESH")

    def test_log_path_escape_is_rejected_without_writing_the_target(self):
        target = self.root / "user-file.txt"
        target.write_text("preserve")
        job = self.job()
        job["stdout_log"] = str(target)
        runtime.write_json(self.manager.job_path, job)
        self.manager.run_worker("fixture")
        self.assertEqual(self.manager.status()["state"], "FAILED")
        self.assertEqual(target.read_text(), "preserve")

    def test_worker_timeout_stops_only_its_child_and_preserves_failure_record(self):
        self.fake.write_text("import time; time.sleep(10)")
        self.job()
        self.manager.run_worker("fixture", timeout=0.1)
        job = runtime.read_json(self.manager.job_path)
        self.assertEqual(job["state"], "FAILED")
        self.assertFalse(self.manager.receipt_path.exists())
        self.assertIsNone(runtime.process_identity(job["child_identity"]["pid"]))
