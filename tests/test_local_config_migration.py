from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate-local-config.py"
SPEC = importlib.util.spec_from_file_location("migrate_local_config", MODULE_PATH)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class LocalConfigMigrationTests(unittest.TestCase):
    def test_legacy_config_is_backed_up_and_unknown_fields_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".flow1c.local.json"
            backup = root / "backup"
            path.write_text(
                json.dumps({"documentation_path": "C:/docs", "custom_user_value": {"keep": True}}),
                encoding="utf-8",
            )

            result = migration.migrate_file(path, backup)
            updated = json.loads(path.read_text(encoding="utf-8"))
            original = json.loads((backup / "local-config.before-migration.json").read_text(encoding="utf-8"))

            self.assertEqual(result["state"], "MIGRATED")
            self.assertEqual(updated["schema_version"], 2)
            self.assertEqual(updated["template_library"]["storage_kind"], "documentation")
            self.assertEqual(updated["custom_user_value"], {"keep": True})
            self.assertNotIn("schema_version", original)

    def test_current_config_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".flow1c.local.json"
            path.write_text('{"schema_version": 2, "value": "same"}\n', encoding="utf-8")
            before = path.read_bytes()

            result = migration.migrate_file(path, root / "backup")

            self.assertEqual(result["state"], "CURRENT")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((root / "backup").exists())

    def test_newer_schema_is_blocked_without_modifying_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".flow1c.local.json"
            path.write_text('{"schema_version": 99, "value": "keep"}\n', encoding="utf-8")
            before = path.read_bytes()

            with self.assertRaises(migration.MigrationError):
                migration.migrate_file(path, Path(directory) / "backup")

            self.assertEqual(path.read_bytes(), before)

    def test_non_object_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".flow1c.local.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(migration.MigrationError):
                migration.migrate_file(path)


if __name__ == "__main__":
    unittest.main()
