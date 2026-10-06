from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flow1c.context import RuntimeContext, documentation_root, load_context
from flow1c.errors import WorkflowError
from flow1c import storage

ROOT = Path(__file__).resolve().parents[1]


class RuntimeFoundationTests(unittest.TestCase):
    def test_import_does_not_touch_files_streams_or_start_processes(self) -> None:
        source = """
import builtins, io, os, pathlib, subprocess, sys, tempfile
def forbidden(*args, **kwargs):
    raise AssertionError('Import performed I/O')
class Stream:
    reconfigure = forbidden
pathlib.Path.read_text = pathlib.Path.write_text = pathlib.Path.mkdir = forbidden
pathlib.Path.exists = pathlib.Path.stat = forbidden
builtins.open = io.open = forbidden
subprocess.run = subprocess.Popen = forbidden
os.getenv = tempfile.mkstemp = forbidden
sys.stdin = sys.stdout = sys.stderr = Stream()
try:
    import flow1c
    import flow1c.context
    import flow1c.storage
    import flow1c.workflow.dialogue
finally:
    sys.stdin, sys.stdout, sys.stderr = sys.__stdin__, sys.__stdout__, sys.__stderr__
"""
        result = subprocess.run([sys.executable, "-S", "-c", source], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_atomic_replace_preserves_previous_state_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.json"
            storage.write_json(path, {"state": "READY", "approval": "user"})
            original = path.read_bytes()
            with mock.patch.object(storage.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    storage.write_json(path, {"state": "COMPLETE"})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_bom_unicode_and_sha256_keep_the_persisted_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps({"text": "\ud83d\ude00\ud800", "nested": ["\udc00"]}), encoding="utf-8-sig")
            value = storage.read_json(path)
            self.assertEqual(value, {"text": "😀�", "nested": ["�"]})
            storage.write_json(path, value)
            self.assertEqual(path.read_bytes(), (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            self.assertEqual(storage.sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_and_invalid_config_preserve_errors_and_user_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(WorkflowError, "Run scripts/bootstrap.ps1 first"):
                load_context(root)
            storage.write_json(root / ".flow1c.json", {"version": 1})
            context = load_context(root)
            self.assertIsInstance(context, RuntimeContext)
            self.assertEqual(context.local_config, {})
            self.assertEqual(context.documentation_root, root)
            local = {"documentation_path": str(root / "docs"), "user_field": {"retained": True}}
            storage.write_json(root / ".flow1c.local.json", local)
            before = (root / ".flow1c.local.json").read_bytes()
            context = load_context(root)
            self.assertEqual(context.local_config, local)
            self.assertEqual(context.documentation_root, (root / "docs").resolve())
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), before)
            (root / ".flow1c.local.json").write_text("invalid", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "Cannot read JSON"):
                load_context(root)

    def test_containment_resolves_paths_and_unreadable_sources_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(storage.path_is_within(root / "sub" / "file.md", root))
            self.assertFalse(storage.path_is_within(root / ".." / "escape.md", root))
            self.assertTrue(storage.is_reparse_or_symlink(root / "missing"))
            self.assertEqual(documentation_root(root, {}), root)
            path = root / "sub"
            with mock.patch.object(Path, "stat", return_value=mock.Mock(st_file_attributes=0x400)), \
                    mock.patch.object(Path, "is_symlink", return_value=False), \
                    mock.patch("stat.FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True):
                self.assertTrue(storage.is_reparse_or_symlink(path))


if __name__ == "__main__":
    unittest.main()
