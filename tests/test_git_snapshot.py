from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from scripts import flow1c_git


class GitSnapshotTests(unittest.TestCase):
    def test_snapshot_is_idempotent_and_does_not_change_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as workspace:
            repository = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repository, check=True)
            (repository / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "source"], cwd=repository, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True,
                                  capture_output=True, text=True).stdout.strip()
            status_before = subprocess.run(["git", "status", "--porcelain=v1"], cwd=repository, check=True,
                                           capture_output=True, text=True).stdout
            first = flow1c_git.create_snapshot(repository, Path(workspace), "11111111-1111-4111-8111-111111111111", head)
            second = flow1c_git.create_snapshot(repository, Path(workspace), "11111111-1111-4111-8111-111111111111", head)
            status_after = subprocess.run(["git", "status", "--porcelain=v1"], cwd=repository, check=True,
                                          capture_output=True, text=True).stdout
            self.assertEqual(first["state"], "CREATED")
            self.assertEqual(second["state"], "REUSED")
            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertEqual(status_before, status_after)
            self.assertTrue((Path(first["source_path"]) / "Module.bsl").is_file())


if __name__ == "__main__":
    unittest.main()
